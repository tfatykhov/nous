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
    ):
        self._db = database
        self._settings = settings
        self._service = surface_service
        self._heart = heart
        self._brain = brain
        self._heartbeat = heartbeat_runner
        self._handlers: dict[str, _HandlerMeta] = {}
        self._recent: list[float] = []
        self._rate_lock = asyncio.Lock()
        _register_default_handlers(self)

    def register(self, name: str, fn: Handler, *, mutating: bool, irreversible: bool = False) -> None:
        self._handlers[name] = _HandlerMeta(fn=fn, mutating=mutating, irreversible=irreversible)

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
