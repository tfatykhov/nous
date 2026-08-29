"""F092: ActionRouter — the server-side gate on renderer->agent actions.

Postgres-only: the allowlist check reads ``a2ui_surfaces.allowed_actions``
(``ARRAY(Text)``), which SQLite returns as a list of single characters, so a
403 here would prove nothing about the code. CI runs NOUS_TEST_DB=postgres.

The client is never trusted. Every test below is one link in that chain:
the allowlist, the nonce, the rate limit and the censor gate all live on the
server, and every rejection is AUDITED — a blocked action that left no trace
would be indistinguishable from one that was never attempted.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from nous.a2ui.actions import ActionRouter
from nous.a2ui.builders import action_review, approval_gate, heartbeat_findings
from nous.a2ui.service import SurfaceService
from nous.storage.models import A2uiAction, A2uiSurface

pytestmark = pytest.mark.postgres_only

JSON = "application/json"

APPROVAL_PARAMS = {
    "title": "Cancel the running backfill?",
    "summary": "It has processed 40% of rows.",
    "risk": "Partial state; resumable from the watermark.",
    "options": [{"id": "cancel", "label": "Cancel"}, {"id": "let_it_run", "label": "Let it run"}],
}

FINDINGS_PARAMS = {
    "findings": [{"fingerprint": "fp-abc-123", "message": "Disk at 91%.", "urgency": "high"}]
}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeFindingStore:
    """The F034.1 finding lifecycle, reduced to what the handlers call."""

    def __init__(self, *, known: bool = True) -> None:
        self._known = known
        self.calls: list[tuple[str, str]] = []
        self.outcomes: list[tuple[str, Any]] = []

    def acknowledge(self, fingerprint: str) -> bool:
        self.calls.append(("acknowledge", fingerprint))
        return self._known

    def resolve(self, fingerprint: str) -> bool:
        self.calls.append(("resolve", fingerprint))
        return self._known

    def dismiss(self, fingerprint: str) -> bool:
        self.calls.append(("dismiss", fingerprint))
        return self._known

    def record_outcome(self, fingerprint: str, signal: Any) -> None:
        self.outcomes.append((fingerprint, signal))


class BlockingHeart:
    """A Heart whose censor check aborts everything it is shown."""

    def __init__(self) -> None:
        self.checked: list[str] = []

    async def check_censors(self, text: str) -> list[Any]:
        self.checked.append(text)
        return [
            SimpleNamespace(action="abort", reason="never cancel backfills", trigger_pattern="cancel")
        ]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def a2ui_agent_id() -> str:
    return f"test-a2ui-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def a2ui_settings(settings, a2ui_agent_id: str):
    return settings.model_copy(
        update={
            "agent_id": a2ui_agent_id,
            "telegram_bot_token": None,
            "telegram_chat_id": None,
        }
    )


@pytest.fixture
def finding_store() -> FakeFindingStore:
    return FakeFindingStore()


@pytest_asyncio.fixture
async def service(db, a2ui_settings, a2ui_agent_id: str):
    """A SurfaceService with NO heart, so pushes are never censor-blocked.

    The censor tests below attach a blocking heart to the ROUTER only —
    otherwise the fixture surface could never be created in the first place.
    """
    svc = SurfaceService(db, a2ui_settings)
    yield svc
    async with db.session() as session:
        await session.execute(delete(A2uiAction).where(A2uiAction.agent_id == a2ui_agent_id))
        await session.execute(delete(A2uiSurface).where(A2uiSurface.agent_id == a2ui_agent_id))
        await session.commit()


@pytest.fixture
def router(db, a2ui_settings, service: SurfaceService, finding_store: FakeFindingStore):
    return ActionRouter(
        db,
        a2ui_settings,
        service,
        heartbeat_runner=SimpleNamespace(finding_store=finding_store),
    )


async def _surface_row(db, surface_id: str) -> A2uiSurface:
    async with db.session() as session:
        row = await session.get(A2uiSurface, surface_id)
        assert row is not None
        return row


async def _audits(db, agent_id: str) -> list[A2uiAction]:
    async with db.session() as session:
        result = await session.execute(
            select(A2uiAction)
            .where(A2uiAction.agent_id == agent_id)
            .order_by(A2uiAction.created_at)
        )
        return list(result.scalars().all())


def _body(
    name: str,
    surface_id: str,
    *,
    nonce: str | None = None,
    nonce_in_context: bool = False,
    context: dict | None = None,
    source_component_id: str | None = None,
) -> dict:
    """A renderer->agent action payload, nonce on either accepted channel."""
    action: dict[str, Any] = {
        "name": name,
        "surfaceId": surface_id,
        "context": dict(context or {}),
    }
    if source_component_id is not None:
        action["sourceComponentId"] = source_component_id
        action["context"]["sourceComponentId"] = source_component_id
    if nonce is not None:
        if nonce_in_context:
            action["context"]["surfaceNonce"] = nonce
        else:
            action["metadata"] = {"extensions": {"com_nous_nonce": nonce}}
    return {"version": "v1.0", "action": action}


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


async def test_non_json_content_type_is_refused(router: ActionRouter) -> None:
    """Content-Type is the CSRF control: no CORS middleware exists here, and

    a cross-origin simple request cannot set application/json without a
    preflight the browser will not make.
    """
    status, payload = await router.handle(
        _body("approval.choose", "whatever"), content_type="text/plain"
    )

    assert status == 415
    assert payload["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"


async def test_missing_action_fields_are_refused(router: ActionRouter) -> None:
    status, payload = await router.handle({"version": "v1.0", "action": {}}, content_type=JSON)

    assert status == 400
    assert payload["error"]["code"] == "VALIDATION_FAILED"


async def test_unknown_surface_is_not_found(router: ActionRouter) -> None:
    status, payload = await router.handle(
        _body("approval.choose", "nous:nope:nope:000000"), content_type=JSON
    )

    assert status == 404
    assert payload["error"]["code"] == "SURFACE_NOT_LIVE"


async def test_resolved_surface_rejects_further_actions(
    router: ActionRouter, service: SurfaceService, db
) -> None:
    """A card the user already answered cannot be answered twice — a stale

    browser tab replaying its last click must not re-run the handler.
    """
    surface_id = await service.push_built(approval_gate(APPROVAL_PARAMS))
    nonce = (await _surface_row(db, surface_id)).nonce
    await service.resolve(surface_id)

    status, payload = await router.handle(
        _body("approval.choose", surface_id, nonce=nonce), content_type=JSON
    )

    assert status == 404
    assert payload["error"]["code"] == "SURFACE_NOT_LIVE"


# ---------------------------------------------------------------------------
# Allowlist + nonce
# ---------------------------------------------------------------------------


async def test_action_outside_the_surface_allowlist_is_rejected_and_audited(
    router: ActionRouter, service: SurfaceService, db, a2ui_agent_id: str
) -> None:
    """allowed_actions lives on the surface row, not in the request.

    A client that invents an action name gets 403 even if the name is a real
    registered handler — the surface decides what it offers.
    """
    surface_id = await service.push_built(approval_gate(APPROVAL_PARAMS))
    nonce = (await _surface_row(db, surface_id)).nonce

    status, payload = await router.handle(
        _body("heartbeat.dismiss", surface_id, nonce=nonce), content_type=JSON
    )

    assert status == 403
    assert payload["error"]["code"] == "ACTION_NOT_ALLOWED"

    audits = await _audits(db, a2ui_agent_id)
    assert [(a.action_name, a.status) for a in audits] == [("heartbeat.dismiss", "rejected")]
    assert "not offered" in audits[0].rejection_reason


@pytest.mark.parametrize("bad_nonce", [None, "stale-nonce"])
async def test_nonce_mismatch_is_rejected_and_audited(
    router: ActionRouter,
    service: SurfaceService,
    db,
    a2ui_agent_id: str,
    bad_nonce: str | None,
) -> None:
    surface_id = await service.push_built(approval_gate(APPROVAL_PARAMS))

    status, payload = await router.handle(
        _body("approval.choose", surface_id, nonce=bad_nonce), content_type=JSON
    )

    assert status == 403
    assert payload["error"]["code"] == "NONCE_MISMATCH"

    audits = await _audits(db, a2ui_agent_id)
    assert [a.status for a in audits] == ["rejected"]
    assert (await _surface_row(db, surface_id)).status == "live", "a rejection changes nothing"


@pytest.mark.parametrize("in_context", [False, True])
async def test_nonce_is_accepted_on_both_channels(
    router: ActionRouter, service: SurfaceService, db, in_context: bool
) -> None:
    """Two accepted carriers: action.metadata.extensions.com_nous_nonce (the

    Appendix A form our builders and renderer use) and context.surfaceNonce
    (the form the spec prose describes). Both must work or a spec-compliant
    renderer would be locked out.
    """
    surface_id = await service.push_built(approval_gate(APPROVAL_PARAMS))
    nonce = (await _surface_row(db, surface_id)).nonce

    status, payload = await router.handle(
        _body(
            "approval.choose",
            surface_id,
            nonce=nonce,
            nonce_in_context=in_context,
            context={"optionId": "cancel"},
        ),
        content_type=JSON,
    )

    assert status == 200, payload


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------


async def test_rate_limit_rejects_and_audits_the_excess(
    db, a2ui_settings, service: SurfaceService, finding_store: FakeFindingStore, a2ui_agent_id: str
) -> None:
    """Server-side sliding window, keyed on agent_id.

    Uses heartbeat.acknowledge deliberately: the approval verbs RESOLVE the
    surface, so a second call would 404 on liveness before ever reaching the
    rate limiter and the test would pass for the wrong reason.
    """
    throttled = a2ui_settings.model_copy(update={"a2ui_action_rate_per_minute": 1})
    router = ActionRouter(
        db,
        throttled,
        service,
        heartbeat_runner=SimpleNamespace(finding_store=finding_store),
    )
    surface_id = await service.push_built(heartbeat_findings(FINDINGS_PARAMS))
    nonce = (await _surface_row(db, surface_id)).nonce
    body = _body(
        "heartbeat.acknowledge", surface_id, nonce=nonce, context={"fingerprint": "fp-abc-123"}
    )

    first_status, _ = await router.handle(body, content_type=JSON)
    second_status, payload = await router.handle(body, content_type=JSON)

    assert first_status == 200
    assert second_status == 429
    assert payload["error"]["code"] == "RATE_LIMITED"

    statuses = [a.status for a in await _audits(db, a2ui_agent_id)]
    assert statuses == ["completed", "rejected"]


# ---------------------------------------------------------------------------
# Censor gate
# ---------------------------------------------------------------------------


async def test_censor_abort_blocks_a_mutating_action(
    db, a2ui_settings, service: SurfaceService, a2ui_agent_id: str
) -> None:
    """A censor that fires on the surface prose stops the action.

    The match target is the surface title plus the action name and context —
    the risky text lives there, not in the opaque action token.
    """
    heart = BlockingHeart()
    router = ActionRouter(db, a2ui_settings, service, heart=heart)
    surface_id = await service.push_built(approval_gate(APPROVAL_PARAMS))
    nonce = (await _surface_row(db, surface_id)).nonce

    status, payload = await router.handle(
        _body("approval.choose", surface_id, nonce=nonce, context={"optionId": "cancel"}),
        content_type=JSON,
    )

    assert status == 403
    assert payload["error"]["code"] == "CENSORED"
    assert "never cancel backfills" in payload["error"]["message"]

    assert APPROVAL_PARAMS["title"] in heart.checked[0]
    assert (await _surface_row(db, surface_id)).status == "live"
    assert [a.status for a in await _audits(db, a2ui_agent_id)] == ["rejected"]


async def test_censor_gate_skips_non_mutating_actions(
    db, a2ui_settings, service: SurfaceService
) -> None:
    """approval.defer changes nothing, so it is not worth a censor round-trip

    (check_censors has side effects by design — it counts activations).
    """
    heart = BlockingHeart()
    router = ActionRouter(db, a2ui_settings, service, heart=heart)
    surface_id = await service.push_built(approval_gate(APPROVAL_PARAMS))
    nonce = (await _surface_row(db, surface_id)).nonce

    status, _ = await router.handle(
        _body("approval.defer", surface_id, nonce=nonce), content_type=JSON
    )

    assert status == 200
    assert heart.checked == []


# ---------------------------------------------------------------------------
# Dispatch + audit
# ---------------------------------------------------------------------------


async def test_approval_choose_completes_patches_and_resolves(
    router: ActionRouter, service: SurfaceService, db, a2ui_agent_id: str
) -> None:
    """The full happy path, end to end: handler runs, the data model is

    patched, the surface tears down, and the audit row says 'completed'.
    """
    surface_id = await service.push_built(approval_gate(APPROVAL_PARAMS))
    nonce = (await _surface_row(db, surface_id)).nonce

    status, payload = await router.handle(
        _body(
            "approval.choose",
            surface_id,
            nonce=nonce,
            context={"optionId": "cancel"},
            source_component_id="opt_0",
        ),
        content_type=JSON,
    )

    assert status == 200
    assert payload == {"ok": True, "message": "chose cancel", "resolved": True}

    surface = await _surface_row(db, surface_id)
    assert surface.status == "resolved"
    assert surface.data_model["summary"] == "Decided: cancel."

    audits = await _audits(db, a2ui_agent_id)
    assert len(audits) == 1
    assert audits[0].action_name == "approval.choose"
    assert audits[0].status == "completed"
    assert audits[0].source_component_id == "opt_0"
    assert audits[0].completed_at is not None
    assert audits[0].rejection_reason is None


async def test_audit_records_the_actor(
    router: ActionRouter, service: SurfaceService, db, a2ui_agent_id: str
) -> None:
    """Who acted — never an implied human. The REST layer passes the

    oauth2-proxy identity header when fronted; unattributed otherwise, and
    an audit row must not claim consent the server cannot demonstrate.
    """
    surface_id = await service.push_built(approval_gate(APPROVAL_PARAMS))
    nonce = (await _surface_row(db, surface_id)).nonce

    await router.handle(
        _body("approval.defer", surface_id, nonce=nonce),
        content_type=JSON,
        actor="tim@example.com",
    )

    assert [a.actor for a in await _audits(db, a2ui_agent_id)] == ["tim@example.com"]


async def test_actor_defaults_to_unattributed(
    router: ActionRouter, service: SurfaceService, db, a2ui_agent_id: str
) -> None:
    surface_id = await service.push_built(approval_gate(APPROVAL_PARAMS))
    nonce = (await _surface_row(db, surface_id)).nonce

    await router.handle(_body("approval.defer", surface_id, nonce=nonce), content_type=JSON)

    assert [a.actor for a in await _audits(db, a2ui_agent_id)] == ["unattributed"]


async def test_heartbeat_verbs_delegate_to_the_finding_store(
    router: ActionRouter, service: SurfaceService, db, finding_store: FakeFindingStore
) -> None:
    surface_id = await service.push_built(heartbeat_findings(FINDINGS_PARAMS))
    nonce = (await _surface_row(db, surface_id)).nonce

    status, payload = await router.handle(
        _body(
            "heartbeat.resolve", surface_id, nonce=nonce, context={"fingerprint": "fp-abc-123"}
        ),
        content_type=JSON,
    )

    assert status == 200
    assert finding_store.calls == [("resolve", "fp-abc-123")]
    assert [fp for fp, _ in finding_store.outcomes] == ["fp-abc-123"]

    # The triage card stays live — the other findings still need answering.
    surface = await _surface_row(db, surface_id)
    assert surface.status == "live"
    assert surface.data_model["findings"]["fp-abc-123"] == "resolve"
    assert payload["resolved"] is False


async def test_unknown_fingerprint_fails_the_action_and_audits_it(
    db, a2ui_settings, service: SurfaceService, a2ui_agent_id: str
) -> None:
    """A finding that no longer exists is a failed action, not a silent 200.

    The user pressed Resolve and nothing was resolved; they have to be told.
    """
    router = ActionRouter(
        db,
        a2ui_settings,
        service,
        heartbeat_runner=SimpleNamespace(finding_store=FakeFindingStore(known=False)),
    )
    surface_id = await service.push_built(heartbeat_findings(FINDINGS_PARAMS))
    nonce = (await _surface_row(db, surface_id)).nonce

    status, payload = await router.handle(
        _body("heartbeat.resolve", surface_id, nonce=nonce, context={"fingerprint": "ghost"}),
        content_type=JSON,
    )

    assert status == 422
    assert payload["error"]["code"] == "ACTION_FAILED"
    assert "not found" in payload["error"]["message"]

    audits = await _audits(db, a2ui_agent_id)
    assert [a.status for a in audits] == ["rejected"]
    assert "not found" in audits[0].rejection_reason


async def test_handler_exception_is_a_500_not_a_silent_success(
    router: ActionRouter, service: SurfaceService, db, a2ui_agent_id: str
) -> None:
    """A crashing handler must never report success to the renderer."""

    async def _explode(ctx: Any) -> Any:
        raise RuntimeError("boom")

    router.register("approval.defer", _explode, mutating=False)
    surface_id = await service.push_built(approval_gate(APPROVAL_PARAMS))
    nonce = (await _surface_row(db, surface_id)).nonce

    status, payload = await router.handle(
        _body("approval.defer", surface_id, nonce=nonce), content_type=JSON
    )

    assert status == 500
    assert payload["error"]["code"] == "HANDLER_FAILED"
    assert [a.status for a in await _audits(db, a2ui_agent_id)] == ["rejected"]


async def test_review_revert_is_not_offered_and_forging_it_is_rejected(
    db, a2ui_settings, service: SurfaceService, a2ui_agent_id: str
) -> None:
    """Revert is withheld until a revert executor exists (product decision on

    the impl-review finding: offering a verb with no handler 501s on click,
    inverting the builder's own "no silently failing Revert" rationale). The
    builder no longer puts ``review.revert`` in allowed_actions, so a forged
    POST for it must die at the ALLOWLIST — before any handler lookup.
    """
    router = ActionRouter(db, a2ui_settings, service)
    surface_id = await service.push_built(
        action_review(
            {
                "title": "Deleted 3 stale branches",
                "did": "Removed branches merged more than 90 days ago.",
                "compensation": {"revertible": True, "handler": "restore_branches"},
            }
        )
    )
    nonce = (await _surface_row(db, surface_id)).nonce

    status, payload = await router.handle(
        _body("review.revert", surface_id, nonce=nonce), content_type=JSON
    )

    assert status == 403
    assert payload["error"]["code"] == "ACTION_NOT_ALLOWED"
    assert [a.status for a in await _audits(db, a2ui_agent_id)] == ["rejected"]
