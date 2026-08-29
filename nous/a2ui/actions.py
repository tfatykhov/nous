"""F092: ActionRouter — the server-side gate for renderer->agent actions.

Pipeline for every POST /a2ui/action, in order:
Content-Type check -> surface exists and is live -> action name in the
surface's allowed_actions -> nonce match -> rate limit -> censor gate for
mutating handlers -> durable audit row -> handler dispatch -> audit update.

The client is never trusted: allowed_actions and the nonce live on the
surface row, the rate limit is server-side, and a rejection is audited with
``status='rejected'`` and returned in the spec's error shape — a censored
or rejected action never silently no-ops.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update

from nous.storage.models import A2uiAction, A2uiSurface

logger = logging.getLogger(__name__)


@dataclass
class ActionContext:
    surface: Any
    name: str
    context: dict
    data_model: dict | None
    services: ActionRouter


@dataclass
class ActionResult:
    ok: bool = True
    message: str = ""
    resolve_surface: bool = False
    data_patches: list[tuple[str, Any]] = field(default_factory=list)


Handler = Callable[[ActionContext], Awaitable[ActionResult]]


@dataclass
class _HandlerMeta:
    fn: Handler
    mutating: bool
    irreversible: bool


class ActionRouter:
    def __init__(
        self,
        database: Any,
        settings: Any,
        surface_service: Any,
        heart: Any = None,
        brain: Any = None,
        heartbeat_runner: Any = None,
        dag_orchestrator: Any = None,
        composer: Any = None,
    ):
        self._db = database
        self._settings = settings
        self._service = surface_service
        self._heart = heart
        self._brain = brain
        self._heartbeat = heartbeat_runner
        self._dag_orchestrator = dag_orchestrator
        # F092.1: SurfaceComposer for app.refine (recompose) and app.refresh
        # (source re-read). None => the micro-app functions report unavailable.
        self._composer = composer
        self._handlers: dict[str, _HandlerMeta] = {}
        # Phase 2: agent-side functions callable by the renderer over
        # POST /a2ui/call (spec's HTTP request-response pattern — the
        # agentFunctionResponse rides back in the HTTP response, so there is
        # no functionCallId-over-SSE correlation). Same trust pipeline as
        # actions: a read RPC is still a probe surface.
        self._functions: dict[str, Callable[[ActionContext], Awaitable[Any]]] = {}
        self._recent: list[float] = []
        self._rate_lock = asyncio.Lock()
        _register_default_handlers(self)
        _register_phase2_handlers(self)
        _register_default_functions(self)
        _register_micro_app_handlers(self)
        _register_micro_app_functions(self)

    def register(self, name: str, fn: Handler, *, mutating: bool, irreversible: bool = False) -> None:
        self._handlers[name] = _HandlerMeta(fn=fn, mutating=mutating, irreversible=irreversible)

    def register_function(self, name: str, fn: Callable[[ActionContext], Awaitable[Any]]) -> None:
        """Register an agent-side function for renderer RPC (read-only)."""
        self._functions[name] = fn

    # ------------------------------------------------------------- functions

    async def handle_call(
        self, body: dict, *, content_type: str, actor: str = "unattributed"
    ) -> tuple[int, dict]:
        """POST /a2ui/call — renderer-initiated agent function (spec pattern 1).

        Returns (status, agentFunctionResponse envelope). Functions are
        READ-ONLY by contract (mutations go through actions with their
        audit/censor pipeline), so calls are rate-limited and gated but not
        individually audited.
        """
        if "application/json" not in (content_type or ""):
            return 415, _fn_err("", "INVALID_FUNCTION_CALL", "Content-Type must be application/json")

        call = body.get("callAgentFunction") or {}
        surface_id = call.get("surfaceId") or ""
        function_call_id = call.get("functionCallId") or ""
        call_function = call.get("callFunction") or {}
        name = call_function.get("call") or ""
        args = call_function.get("args") or {}
        if not surface_id or not function_call_id or not name:
            return 400, _fn_err(
                function_call_id,
                "INVALID_FUNCTION_CALL",
                "callAgentFunction requires surfaceId, functionCallId and callFunction.call",
            )

        async with self._db.session() as session:
            surface = (
                await session.execute(
                    select(A2uiSurface).where(
                        A2uiSurface.surface_id == surface_id,
                        A2uiSurface.agent_id == self._settings.agent_id,
                    )
                )
            ).scalar_one_or_none()
        if surface is None or surface.status != "live":
            return 404, _fn_err(function_call_id, "INVALID_FUNCTION_CALL", "surface not found or not live")

        nonce = ((body.get("metadata") or {}).get("extensions") or {}).get("com_nous_nonce")
        if nonce != surface.nonce:
            return 403, _fn_err(function_call_id, "INVALID_FUNCTION_CALL", "surface nonce missing or stale")

        if not await self._rate_ok():
            return 429, _fn_err(function_call_id, "RATE_LIMITED", "too many calls; slow down")

        fn = self._functions.get(name)
        if fn is None:
            return 404, _fn_err(function_call_id, "UNKNOWN_FUNCTION", f"no agent function {name!r}")

        # No per-surface lock here, unlike handle(): functions are read-only
        # by contract, so there is no terminal transition to serialize and a
        # concurrent expiry/action changes nothing a read can corrupt.
        # (Exception: the F092.1 micro-app functions write surface
        # presentation state and take the lock themselves, scoped to their
        # writes — locking every read RPC for two writers would be backwards.)
        ctx = ActionContext(surface=surface, name=name, context=args, data_model=None, services=self)
        try:
            value = await fn(ctx)
        except ValueError as exc:
            return 422, _fn_err(function_call_id, "INVALID_FUNCTION_CALL", str(exc))
        except KeyError:
            # A micro-app write lost the race with app.close/eviction — the
            # surface is gone, which the client will learn from the
            # deleteSurface envelope; 422 (not 500) so it doesn't read as
            # a server fault.
            return 422, _fn_err(function_call_id, "INVALID_FUNCTION_CALL", "surface no longer live")
        except Exception:
            logger.exception("F092 agent function %s failed", name)
            return 500, _fn_err(function_call_id, "EXECUTION_FAILED", f"function {name!r} failed")
        return 200, {
            "version": "v1.0",
            "agentFunctionResponse": {"functionCallId": function_call_id, "value": value},
        }

    # ------------------------------------------------------------------ gate

    async def handle(self, body: dict, *, content_type: str, actor: str = "unattributed") -> tuple[int, dict]:
        """Returns (http_status, response_body).

        ``actor`` is the oauth2-proxy identity header when fronted, else
        'unattributed' — audit rows must never imply human consent the
        server cannot demonstrate (review finding: on a LAN-exposed port a
        forged action would otherwise corrupt the evidence tier).
        """
        # Content-Type is the CSRF control here: no CORS middleware exists and
        # Request.json() never checks it, but a cross-origin simple request
        # cannot carry application/json without a preflight.
        if "application/json" not in (content_type or ""):
            return 415, _err("UNSUPPORTED_MEDIA_TYPE", "", "Content-Type must be application/json")

        action = body.get("action") or {}
        name = action.get("name") or ""
        surface_id = action.get("surfaceId") or ""
        context = action.get("context") or {}
        if not name or not surface_id:
            return 400, _err("VALIDATION_FAILED", surface_id, "action.name and action.surfaceId required")

        data_model = None
        surfaces_meta = (body.get("a2uiRendererDataModel") or {}).get("surfaces") or {}
        if surface_id in surfaces_meta:
            data_model = surfaces_meta[surface_id]

        # Per-surface serialization via the SERVICE's shared lock registry
        # (codex P1): the expiry sweep takes the same lock, so expiry cannot
        # record no_objection while an action handler is mid-dispatch, and
        # two overlapping terminal actions still dispatch exactly once (the
        # status re-read inside the lock turns the loser into a 404).
        async with self._service.surface_lock(surface_id):
            return await self._handle_locked(surface_id, name, context, data_model, action, actor)

    async def _handle_locked(
        self,
        surface_id: str,
        name: str,
        context: dict,
        data_model: dict | None,
        action: dict,
        actor: str,
    ) -> tuple[int, dict]:
        # Status is read INSIDE the surface lock — a terminal action that
        # just resolved this surface makes the second request 404 here.
        # Agent-scoped (codex P1): a bare PK lookup would accept another
        # agent's surface id + nonce.
        async with self._db.session() as session:
            surface = (
                await session.execute(
                    select(A2uiSurface).where(
                        A2uiSurface.surface_id == surface_id,
                        A2uiSurface.agent_id == self._settings.agent_id,
                    )
                )
            ).scalar_one_or_none()

        if surface is None or surface.status != "live":
            return 404, _err("SURFACE_NOT_LIVE", surface_id, "surface not found or not live")

        # Rate limit FIRST (codex P2): every rejection below commits an audit
        # row, so a hostile client replaying bad names/nonces at a live
        # surface id could otherwise grow the audit table without bound —
        # the limiter must gate the audited paths, not just the handlers.
        if not await self._rate_ok():
            return 429, _err("RATE_LIMITED", surface_id, "too many actions; slow down")

        async def reject(status: int, code: str, message: str) -> tuple[int, dict]:
            await self._audit(
                surface,
                name,
                context,
                data_model,
                "rejected",
                message,
                actor,
                source_component_id=action.get("sourceComponentId"),
            )
            return status, _err(code, surface_id, message)

        if name not in (surface.allowed_actions or []):
            return await reject(403, "ACTION_NOT_ALLOWED", f"action {name!r} not offered by this surface")

        # Phase 2, spec §10.9: a form submit ships the whole client data
        # model, from a client we do not trust. Re-validate its SHAPE against
        # the authoritative model before any handler reads it: unknown keys
        # and primitive-type flips are rejected (client-side `checks` are a
        # UX affordance, not a control).
        if data_model is not None:
            shape_error = _submitted_model_error(surface.data_model or {}, data_model)
            if shape_error:
                return await reject(422, "VALIDATION_FAILED", f"submitted data model rejected: {shape_error}")

        nonce = ((action.get("metadata") or {}).get("extensions") or {}).get("com_nous_nonce") or context.get(
            "surfaceNonce"
        )
        if nonce != surface.nonce:
            return await reject(403, "NONCE_MISMATCH", "surface nonce missing or stale")

        meta = self._handlers.get(name)
        if meta is None:
            return await reject(501, "NO_HANDLER", f"no handler registered for {name!r}")

        if meta.mutating and self._heart is not None:
            from nous.a2ui.service import check_censors_chunked

            # Full content, chunked (codex P1): handlers consume the ENTIRE
            # context, so a single truncated slice let prohibited text bypass
            # an abort censor by sitting past the cut.
            match_text = f"{surface.title} {name} {context}"
            reason = await check_censors_chunked(self._heart, match_text, where="action")
            if reason is not None:
                return await reject(403, "CENSORED", f"blocked by censor: {reason}")

        audit_id = await self._audit(
            surface,
            name,
            context,
            data_model,
            "dispatched",
            None,
            actor,
            source_component_id=action.get("sourceComponentId"),
        )

        ctx = ActionContext(surface=surface, name=name, context=context, data_model=data_model, services=self)
        try:
            result = await meta.fn(ctx)
        except Exception as exc:
            logger.exception("F092 action handler %s failed", name)
            await self._audit_update(audit_id, "rejected", f"handler error: {exc}")
            return 500, _err("HANDLER_FAILED", surface_id, f"handler error: {exc}")

        # Post-dispatch surface delivery is RECONCILED, never allowed to
        # escape (codex P2): the handler's side effect has already happened,
        # so a raised patch/resolve failure would leave the audit row at
        # 'dispatched' and hand the client a 500 that invites a retry — and
        # the retry would repeat the side effect. The audit records the truth
        # (completed, with a delivery note); the surface catches up via the
        # expiry sweep or the client's next hydration.
        delivery_note = None
        try:
            for path, value in result.data_patches:
                try:
                    await self._service.update_data(surface_id, path, value)
                except KeyError:
                    pass
            if result.resolve_surface:
                await self._service.resolve(surface_id)
        except Exception:
            logger.exception("F092 post-dispatch surface update failed for %s", name)
            delivery_note = "completed; surface update failed — card may linger until resync"

        note = result.message or None
        if delivery_note:
            note = f"{result.message} ({delivery_note})" if result.message else delivery_note
        # The terminal audit write gets the same reconciliation as delivery
        # (codex P2): the handler's side effect already happened, so letting
        # this raise would 500 the client into a retry that repeats it,
        # while the row sat at 'dispatched'. A failed finalization is logged
        # loudly and reported in the response instead.
        try:
            await self._audit_update(audit_id, "completed" if result.ok else "rejected", note)
        except Exception:
            logger.exception("F092 final audit update failed for %s (audit %s)", name, audit_id)
            note = (
                f"{note} (audit finalization failed — recorded as dispatched)"
                if note
                else "audit finalization failed — recorded as dispatched"
            )
        if not result.ok:
            return 422, _err("ACTION_FAILED", surface_id, result.message)
        return 200, {
            "ok": True,
            "message": note or "",
            "resolved": result.resolve_surface and delivery_note is None,
        }

    # -------------------------------------------------------------- plumbing

    async def _rate_ok(self) -> bool:
        async with self._rate_lock:
            now = time.monotonic()
            self._recent = [t for t in self._recent if now - t < 60.0]
            if len(self._recent) >= self._settings.a2ui_action_rate_per_minute:
                return False
            self._recent.append(now)
            return True

    async def _audit(
        self,
        surface: Any,
        name: str,
        context: dict,
        data_model: dict | None,
        status: str,
        rejection_reason: str | None,
        actor: str = "unattributed",
        source_component_id: str | None = None,
    ) -> Any:
        async with self._db.session() as session:
            row = A2uiAction(
                agent_id=surface.agent_id,
                surface_id=surface.surface_id,
                action_name=name,
                actor=actor,
                # From action.sourceComponentId (the wire shape), NOT context —
                # the renderer never duplicates it into context (codex P2).
                source_component_id=source_component_id,
                context=context,
                data_model=data_model,
                status=status,
                rejection_reason=rejection_reason,
            )
            session.add(row)
            await session.commit()
            return row.id

    async def _audit_update(self, audit_id: Any, status: str, message: str | None) -> None:
        async with self._db.session() as session:
            await session.execute(
                update(A2uiAction)
                .where(A2uiAction.id == audit_id)
                .values(
                    status=status,
                    rejection_reason=message if status == "rejected" else None,
                    completed_at=datetime.now(UTC),
                )
            )
            await session.commit()


def _fn_err(function_call_id: str, code: str, message: str) -> dict:
    """agentFunctionResponse error envelope (spec: value XOR error)."""
    return {
        "version": "v1.0",
        "agentFunctionResponse": {
            "functionCallId": function_call_id,
            "error": {"code": code, "message": message},
        },
    }


def _err(code: str, surface_id: str, message: str) -> dict:
    return {"error": {"code": code, "surfaceId": surface_id, "message": message}}


# ---------------------------------------------------------------- handlers


def _register_default_handlers(router: ActionRouter) -> None:
    async def approval_choose(ctx: ActionContext) -> ActionResult:
        option = str(ctx.context.get("optionId", ""))
        # Validate against the surface's AUTHORITATIVE options (server-side
        # data model, not the client copy): a client with the nonce could
        # otherwise submit an optionId no button ever offered (codex P2).
        offered = {str(o.get("id")) for o in (ctx.surface.data_model or {}).get("options", []) if isinstance(o, dict)}
        if option not in offered:
            return ActionResult(ok=False, message=f"option {option!r} was not offered by this surface")
        return ActionResult(
            message=f"chose {option}",
            resolve_surface=True,
            data_patches=[("/summary", f"Decided: {option}.")],
        )

    async def approval_defer(ctx: ActionContext) -> ActionResult:
        # Defer must NOT resolve (codex P2): nothing reschedules a resolved
        # card, so "Ask me later" would permanently destroy the approval.
        # The card stays live until the user decides or it expires — and
        # expiry then writes the honest `no_objection` evidence.
        return ActionResult(
            message="deferred — the card stays until you decide or it expires",
            data_patches=[("/summary", "Deferred. This stays here until you decide or it expires.")],
        )

    async def review_acknowledge(ctx: ActionContext) -> ActionResult:
        return ActionResult(message="acknowledged", resolve_surface=True)

    async def review_course_correct(ctx: ActionContext) -> ActionResult:
        correction = str(ctx.context.get("correction") or "").strip()
        # The surface row is the AUTHORITY on which decision this review is
        # about (codex P1): trusting context.traceId would let a client with
        # this surface's nonce mark any other decision as failed.
        trace_id = ctx.surface.trace_id
        client_trace = ctx.context.get("traceId")
        if client_trace and str(client_trace) != str(trace_id or ""):
            return ActionResult(ok=False, message="traceId does not match this surface's decision")
        noted = False
        if trace_id and router._brain is not None:
            try:
                await router._brain.review(
                    UUID(str(trace_id)),
                    outcome="failure",
                    result=f"course-corrected via companion: {correction[:500]}",
                    reviewer="a2ui",
                )
                noted = True
            except Exception:
                logger.warning("F092 course-correct: brain.review failed", exc_info=True)
                # Fail LOUDLY and keep the card (codex P2): swallowing this
                # marked the audit completed and tore down the surface while
                # the decision was never reviewed — the user loses both the
                # retry affordance and the calibration signal.
                return ActionResult(
                    ok=False,
                    message="could not record the correction against the decision — try again",
                )
        return ActionResult(
            message="correction recorded" + (" against decision" if noted else ""),
            resolve_surface=True,
        )

    async def review_make_rule(ctx: ActionContext) -> ActionResult:
        if router._heart is None:
            return ActionResult(ok=False, message="heart unavailable")
        from nous.heart.schemas import FactInput

        rule_text = (
            f"Standing rule (from companion review of '{ctx.surface.title}'): "
            f"{ctx.context.get('correction') or 'do not repeat this action without checking first.'}"
        )
        try:
            await router._heart.learn(FactInput(content=rule_text, category="rule", source="a2ui_review"))
        except Exception as exc:
            logger.warning("F092 make-rule failed", exc_info=True)
            return ActionResult(ok=False, message=f"could not store rule: {exc}")
        return ActionResult(message="rule stored", resolve_surface=True)

    async def _heartbeat_verb(ctx: ActionContext, verb: str) -> ActionResult:
        runner = router._heartbeat
        store = getattr(runner, "finding_store", None) if runner is not None else None
        if store is None:
            return ActionResult(ok=False, message="finding store unavailable")
        fingerprint = str(ctx.context.get("fingerprint") or "")
        # Only fingerprints this surface actually rendered may be acted on
        # (codex P1): the store is global, so an unvalidated fingerprint
        # would let one surface's nonce triage ANY finding — and corrupt the
        # outcome signals the heartbeat tuner learns from.
        offered = (ctx.surface.data_model or {}).get("findings", {})
        if fingerprint not in offered:
            return ActionResult(ok=False, message=f"finding {fingerprint!r} is not on this surface")
        ok = getattr(store, verb)(fingerprint)
        if not ok:
            return ActionResult(ok=False, message=f"finding {fingerprint!r} not found")
        if verb == "resolve":
            try:
                from nous.heartbeat.schemas import OutcomeSignal

                store.record_outcome(fingerprint, OutcomeSignal.POSITIVE)
            except Exception:
                logger.debug("F092 outcome signal failed", exc_info=True)
        return ActionResult(
            message=f"{verb}d {fingerprint[:12]}",
            data_patches=[(f"/findings/{_escape_pointer(fingerprint)}", verb)],
        )

    async def hb_ack(ctx: ActionContext) -> ActionResult:
        return await _heartbeat_verb(ctx, "acknowledge")

    async def hb_resolve(ctx: ActionContext) -> ActionResult:
        return await _heartbeat_verb(ctx, "resolve")

    async def hb_dismiss(ctx: ActionContext) -> ActionResult:
        return await _heartbeat_verb(ctx, "dismiss")

    router.register("approval.choose", approval_choose, mutating=True, irreversible=True)
    router.register("approval.defer", approval_defer, mutating=False)
    router.register("review.acknowledge", review_acknowledge, mutating=False)
    router.register("review.course_correct", review_course_correct, mutating=True)
    router.register("review.make_rule", review_make_rule, mutating=True)
    router.register("heartbeat.acknowledge", hb_ack, mutating=True)
    router.register("heartbeat.resolve", hb_resolve, mutating=True)
    router.register("heartbeat.dismiss", hb_dismiss, mutating=True)


def _escape_pointer(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


_DECISION_OUTCOMES = frozenset({"success", "partial", "failure", "noise", "superseded"})


def _register_phase2_handlers(router: ActionRouter) -> None:
    """Phase 2: decision sweep + DAG monitor verbs."""

    async def decision_resolve(ctx: ActionContext) -> ActionResult:
        if router._brain is None:
            return ActionResult(ok=False, message="brain unavailable")
        decision_id = str(ctx.context.get("decisionId") or "")
        outcome = str(ctx.context.get("outcome") or "")
        # Both validated against SERVER truth (the approval.choose lesson):
        # the surface's own data model lists which decisions it offered, and
        # the outcome set is ReviewInput's Literal (migration 062).
        offered = (ctx.surface.data_model or {}).get("decisions", {})
        if decision_id not in offered:
            return ActionResult(ok=False, message=f"decision {decision_id!r} is not on this surface")
        if outcome not in _DECISION_OUTCOMES:
            return ActionResult(
                ok=False, message=f"outcome must be one of {sorted(_DECISION_OUTCOMES)}"
            )
        note = str(ctx.context.get("note") or "").strip()
        try:
            await router._brain.review(
                UUID(decision_id),
                outcome=outcome,
                result=f"resolved via companion sweep{': ' + note[:400] if note else ''}",
                reviewer="a2ui",
            )
        except Exception:
            logger.warning("F092 decision.resolve: brain.review failed", exc_info=True)
            return ActionResult(ok=False, message="could not record the outcome — try again")
        remaining = sum(1 for status in offered.values() if status == "pending") - 1
        return ActionResult(
            message=f"{outcome} recorded",
            data_patches=[(f"/decisions/{_escape_pointer(decision_id)}", outcome)],
            resolve_surface=remaining <= 0,
        )

    async def _dag_verb(ctx: ActionContext, verb: str) -> ActionResult:
        orchestrator = router._dag_orchestrator
        if orchestrator is None:
            return ActionResult(ok=False, message="DAG orchestration unavailable")
        dag_id = str((ctx.surface.data_model or {}).get("dag_id") or "")
        if not dag_id:
            return ActionResult(ok=False, message="surface carries no dag_id")
        try:
            if verb == "retry":
                node = str(ctx.context.get("node") or "")
                offered = {n["name"] for n in (ctx.surface.data_model or {}).get("nodes", [])}
                if node not in offered:
                    return ActionResult(ok=False, message=f"node {node!r} is not on this surface")
                await orchestrator.retry_node(UUID(dag_id), node)
                return ActionResult(
                    message=f"retrying {node}",
                    data_patches=[("/banner", f"Retry requested for {node}.")],
                )
            await orchestrator.cancel_dag(UUID(dag_id), reason="cancelled via companion")
            return ActionResult(message="DAG cancelled", resolve_surface=True)
        except Exception as exc:
            logger.warning("F092 dag.%s failed", verb, exc_info=True)
            return ActionResult(ok=False, message=f"dag {verb} failed: {exc}")

    async def dag_retry(ctx: ActionContext) -> ActionResult:
        return await _dag_verb(ctx, "retry")

    async def dag_cancel(ctx: ActionContext) -> ActionResult:
        return await _dag_verb(ctx, "cancel")

    router.register("decision.resolve", decision_resolve, mutating=True)
    router.register("dag.retry", dag_retry, mutating=True)
    router.register("dag.cancel", dag_cancel, mutating=True, irreversible=True)


def _register_default_functions(router: ActionRouter) -> None:
    """Agent-side functions callable over POST /a2ui/call. READ-ONLY."""

    async def expand_graph_node(ctx: ActionContext) -> Any:
        if router._brain is None:
            raise ValueError("brain unavailable")
        node_id = str(ctx.context.get("nodeId") or "")
        node_type = str(ctx.context.get("nodeType") or "fact")
        if node_type not in ("fact", "decision", "episode", "procedure", "chunk"):
            raise ValueError(f"unknown nodeType {node_type!r}")
        try:
            uid = UUID(node_id)
        except ValueError as exc:
            raise ValueError("nodeId must be a UUID") from exc
        limit = min(int(ctx.context.get("limit") or 8), 20)
        neighbors = await router._brain.neighbors(uid, node_type=node_type, limit=limit)
        return {
            "nodes": [
                {
                    "id": str(n.id),
                    "type": n.node_type,
                    "label": (n.description or "")[:120],
                }
                for n in neighbors
            ],
            "edges": [
                {
                    "source": node_id,
                    "target": str(n.id),
                    "relation": n.edge_relation,
                    "weight": n.edge_weight,
                }
                for n in neighbors
            ],
        }

    async def load_decision_detail(ctx: ActionContext) -> Any:
        if router._brain is None:
            raise ValueError("brain unavailable")
        try:
            uid = UUID(str(ctx.context.get("decisionId") or ""))
        except ValueError as exc:
            raise ValueError("decisionId must be a UUID") from exc
        detail = await router._brain.get(uid)
        if detail is None:
            raise ValueError("decision not found")
        return {
            "id": str(detail.id),
            "description": detail.description,
            "confidence": detail.confidence,
            "stakes": detail.stakes,
            "category": detail.category,
            "outcome": detail.outcome,
            "reasons": [
                {"type": r.type, "text": r.text} for r in (detail.reasons or [])
            ][:10],
        }

    router.register_function("expandGraphNode", expand_graph_node)
    router.register_function("loadDecisionDetail", load_decision_detail)


def _register_micro_app_handlers(router: ActionRouter) -> None:
    """F092.1: app.close — the ONLY action-router verb a micro-app holds.

    mutating=False (same class as approval.defer): closing an ephemeral
    read-only view mutates nothing that needs a censor round-trip, but the
    lifecycle event still writes its ordinary audit row.
    """

    async def app_close(ctx: ActionContext) -> ActionResult:
        return ActionResult(message="app closed", resolve_surface=True)

    router.register("app.close", app_close, mutating=False)


def _register_micro_app_functions(router: ActionRouter) -> None:
    """F092.1 navigable-readonly class: refine + refresh over /a2ui/call.

    Both validate against the surface's server-stored app_spec — the same
    authority pattern as the allowlist. A refine button is NOT a free-form
    prompt with a nice label; the submitted id must be one this surface
    declared at compose time.
    """

    async def app_refresh(ctx: ActionContext) -> Any:
        if router._composer is None:
            raise ValueError("micro-app composer unavailable")
        spec = ctx.surface.app_spec or {}
        # Re-runs the DECLARED sources only — no LLM. With model-supplied
        # data, "refresh" would replay whatever the model said last time;
        # with sources it is a real re-read (F092.1 §3.6).
        patches = await router._composer.refresh_data(spec)
        # handle_call is lockless because functions are read-only by
        # contract — these two micro-app functions are the exception (they
        # write surface presentation state), so they take the per-surface
        # lock THEMSELVES to serialize with app.close and LRU eviction.
        async with router._service.surface_lock(ctx.surface.surface_id):
            for key, value in patches.items():
                await router._service.update_data(ctx.surface.surface_id, f"/{key}", value)
        return {"refreshed": sorted(patches)}

    async def app_refine(ctx: ActionContext) -> Any:
        if router._composer is None:
            raise ValueError("micro-app composer unavailable")
        spec = ctx.surface.app_spec or {}
        options = {
            str(o.get("id")): o for o in spec.get("refine_options") or [] if isinstance(o, dict)
        }
        option_id = str(ctx.context.get("id") or "")
        if option_id not in options:
            raise ValueError(f"refine option {option_id!r} is not offered by this surface")
        option = options[option_id]
        intent = str(spec.get("intent") or ctx.surface.title)
        refined_intent = (
            f"{intent}\n\nRefine request: {option.get('label')}"
            + (f"\nRefine params: {json.dumps(option.get('params'))}" if option.get("params") else "")
        )
        composed = await router._composer.compose(
            refined_intent,
            archetype=str(spec.get("archetype") or "") or None,
            data_sources=spec.get("data_sources") or [],
            origin=ctx.surface.origin,
            priority=ctx.surface.priority,
        )
        # Same surface_id, delivered as updateComponents + whole-model
        # updateDataModel — never a dedup re-push, which tears down and
        # recreates (repaint, feed reorder, nonce race; rev-ui #3). The
        # composer already ran schema + grammar validation on this output.
        # The per-surface lock keeps the two-envelope delivery atomic
        # against a concurrent app.close: without it, close could land
        # between them and strand components without their data model.
        async with router._service.surface_lock(ctx.surface.surface_id):
            await router._service.update_components(
                ctx.surface.surface_id,
                composed.built.components,
                app_spec=composed.app_spec,
            )
            await router._service.update_data(
                ctx.surface.surface_id, None, composed.built.data_model
            )
        return {
            "refined": option_id,
            "fallback": composed.fallback,
            "title": composed.built.title,
        }

    router.register_function("app.refresh", app_refresh)
    router.register_function("app.refine", app_refine)


def _submitted_model_error(authoritative: dict, submitted: Any, path: str = "") -> str | None:
    """Shape-check a client-submitted data model against the server's copy.

    Keys must be a subset of the authoritative model's at every object
    level, and a primitive may not change JSON type (str/number/bool are
    each locked; int<->float is allowed). Values themselves are the point
    of a form submit and are NOT compared. Lists are treated as opaque
    values — their length and element shape are input, not structure.
    Returns an error string, or None when acceptable.
    """
    if not isinstance(submitted, dict):
        return f"expected an object at {path or '/'}"
    for key, sub_value in submitted.items():
        if key not in authoritative:
            return f"unknown key {path}/{key}"
        auth_value = authoritative[key]
        if isinstance(auth_value, dict):
            if not isinstance(sub_value, dict):
                return f"expected an object at {path}/{key}"
            nested = _submitted_model_error(auth_value, sub_value, f"{path}/{key}")
            if nested:
                return nested
        elif isinstance(auth_value, bool):
            if not isinstance(sub_value, bool):
                return f"expected a boolean at {path}/{key}"
        elif isinstance(auth_value, (int, float)):
            if isinstance(sub_value, bool) or not isinstance(sub_value, (int, float)):
                return f"expected a number at {path}/{key}"
        elif isinstance(auth_value, str):
            if not isinstance(sub_value, str):
                return f"expected a string at {path}/{key}"
        elif isinstance(auth_value, list):
            if not isinstance(sub_value, list):
                return f"expected an array at {path}/{key}"
    return None
