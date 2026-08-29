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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import UUID

from sqlalchemy import update

from nous.storage.models import A2uiAction, A2uiSurface

logger = logging.getLogger(__name__)


@dataclass
class ActionContext:
    surface: Any
    name: str
    context: dict
    data_model: dict | None
    services: "ActionRouter"


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

    def register(
        self, name: str, fn: Handler, *, mutating: bool, irreversible: bool = False
    ) -> None:
        self._handlers[name] = _HandlerMeta(fn=fn, mutating=mutating, irreversible=irreversible)

    # ------------------------------------------------------------------ gate

    async def handle(self, body: dict, *, content_type: str) -> tuple[int, dict]:
        """Returns (http_status, response_body)."""
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

        async with self._db.session() as session:
            surface = await session.get(A2uiSurface, surface_id)

        if surface is None or surface.status != "live":
            return 404, _err("SURFACE_NOT_LIVE", surface_id, "surface not found or not live")

        async def reject(status: int, code: str, message: str) -> tuple[int, dict]:
            await self._audit(surface, name, context, data_model, "rejected", message)
            return status, _err(code, surface_id, message)

        if name not in (surface.allowed_actions or []):
            return await reject(403, "ACTION_NOT_ALLOWED", f"action {name!r} not offered by this surface")

        nonce = (
            ((action.get("metadata") or {}).get("extensions") or {}).get("com_nous_nonce")
            or context.get("surfaceNonce")
        )
        if nonce != surface.nonce:
            return await reject(403, "NONCE_MISMATCH", "surface nonce missing or stale")

        if not await self._rate_ok():
            return await reject(429, "RATE_LIMITED", "too many actions; slow down")

        meta = self._handlers.get(name)
        if meta is None:
            return await reject(501, "NO_HANDLER", f"no handler registered for {name!r}")

        if meta.mutating and self._heart is not None:
            match_text = f"{surface.title} {name} {context}"
            try:
                censor_hits = await self._heart.check_censors(match_text[:2000])
            except Exception:
                logger.warning("F092 censor check failed open on action", exc_info=True)
                censor_hits = []
            blocking = [m for m in censor_hits if m.action in ("abort", "refuse")]
            if blocking:
                reason = blocking[0].reason or blocking[0].trigger_pattern
                return await reject(403, "CENSORED", f"blocked by censor: {reason}")

        audit_id = await self._audit(surface, name, context, data_model, "dispatched", None)

        ctx = ActionContext(
            surface=surface, name=name, context=context, data_model=data_model, services=self
        )
        try:
            result = await meta.fn(ctx)
        except Exception as exc:
            logger.exception("F092 action handler %s failed", name)
            await self._audit_update(audit_id, "rejected", f"handler error: {exc}")
            return 500, _err("HANDLER_FAILED", surface_id, f"handler error: {exc}")

        for path, value in result.data_patches:
            try:
                await self._service.update_data(surface_id, path, value)
            except KeyError:
                pass
        if result.resolve_surface:
            await self._service.resolve(surface_id)

        await self._audit_update(audit_id, "completed" if result.ok else "rejected", result.message or None)
        if not result.ok:
            return 422, _err("ACTION_FAILED", surface_id, result.message)
        return 200, {"ok": True, "message": result.message, "resolved": result.resolve_surface}

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
    ) -> Any:
        async with self._db.session() as session:
            row = A2uiAction(
                agent_id=surface.agent_id,
                surface_id=surface.surface_id,
                action_name=name,
                source_component_id=context.get("sourceComponentId"),
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
                    completed_at=datetime.now(timezone.utc),
                )
            )
            await session.commit()


def _err(code: str, surface_id: str, message: str) -> dict:
    return {"error": {"code": code, "surfaceId": surface_id, "message": message}}


# ---------------------------------------------------------------- handlers


def _register_default_handlers(router: ActionRouter) -> None:
    async def approval_choose(ctx: ActionContext) -> ActionResult:
        option = ctx.context.get("optionId", "")
        return ActionResult(
            message=f"chose {option}",
            resolve_surface=True,
            data_patches=[("/summary", f"Decided: {option}.")],
        )

    async def approval_defer(ctx: ActionContext) -> ActionResult:
        return ActionResult(message="deferred", resolve_surface=True)

    async def review_acknowledge(ctx: ActionContext) -> ActionResult:
        return ActionResult(message="acknowledged", resolve_surface=True)

    async def review_course_correct(ctx: ActionContext) -> ActionResult:
        correction = str(ctx.context.get("correction") or "").strip()
        trace_id = ctx.context.get("traceId")
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
            await router._heart.learn(
                FactInput(content=rule_text, category="rule", source="a2ui_review")
            )
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
