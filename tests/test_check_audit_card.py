"""Scorer v5: the owner's check-audit card (``builders/check_audit.py``) and its ``audit.verdict`` handler.

The handler tests call the registered handler directly, so they run on SQLite too (the router's allowlist reads a
Postgres ARRAY column, see test_a2ui_actions.py). The end-to-end test through ``ActionRouter.handle`` is Postgres-only.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from nous.a2ui.actions import ActionContext, ActionRouter
from nous.a2ui.builders import TEMPLATES
from nous.a2ui.builders.check_audit import (
    CHECK_CHARS,
    RESULT_CHARS,
    RESULT_LABEL,
    TASK_CHARS,
    check_audit,
)
from nous.a2ui.tools import _PUSH_SURFACE_SCHEMA
from nous.storage.models import A2uiAction, A2uiSurface, CheckAudit

TASK = "summarise the weekly report"
CHECK = "test -f /tmp/report.md"
RESULT = "The summary is in /tmp/report.md."


def _items(n: int = 2) -> list[dict]:
    return [{"audit_id": str(uuid.uuid4()), "task": TASK, "check": CHECK, "result": RESULT} for _ in range(n)]


def _by_id(built, cid: str) -> dict:
    return next(c for c in built.components if c["id"] == cid)


def _text(built) -> str:
    return "\n".join(str(c.get("text", "")) for c in built.components)


# ---- the card ---------------------------------------------------------------------------------------------------


def test_the_card_offers_right_and_wrong_per_item_and_nothing_else():
    items = _items(3)
    built = check_audit(items)
    built.validate()
    assert (built.kind, built.priority, built.allowed_actions) == ("check_audit", 1, ["audit.verdict"])
    assert built.expires_in.days == 14  # an answer can land within the scorer's hold on an unanswered card
    assert built.data_model == {"items": {item["audit_id"]: "pending" for item in items}}
    contexts = [c["action"]["event"]["context"] for c in built.components if c["component"] == "Button"]
    assert contexts == [
        {"auditId": item["audit_id"], "verdict": verdict} for item in items for verdict in ("right", "wrong")
    ]
    assert {c["action"]["event"]["name"] for c in built.components if c["component"] == "Button"} == {"audit.verdict"}
    assert _by_id(built, "a1_status")["text"] == {"path": f"/items/{items[1]['audit_id']}"}


def test_each_item_shows_the_task_the_check_and_the_result_labelled_as_the_agents_own_report():
    built = check_audit(_items(1))
    assert _by_id(built, "a0_result_h")["text"] == RESULT_LABEL == "The agent's own report"
    for cid, shown in (("a0_task", TASK), ("a0_check", CHECK), ("a0_result", RESULT)):
        assert _by_id(built, cid)["text"] == f"```\n{shown}\n```", cid  # one fenced block each


def test_every_model_authored_field_is_clipped():
    built = check_audit(
        [{"audit_id": str(uuid.uuid4()), "task": "t" * 5000, "check": "c" * 5000, "result": "r" * 9000}]
    )
    for cid, limit in (("a0_task", TASK_CHARS), ("a0_check", CHECK_CHARS), ("a0_result", RESULT_CHARS)):
        body = _by_id(built, cid)["text"].removeprefix("```\n").removesuffix("\n```")
        assert len(body) <= limit and "[truncated]" in body, cid
    assert (TASK_CHARS, CHECK_CHARS, RESULT_CHARS) == (600, 300, 1500)


def test_an_empty_card_is_not_built():
    with pytest.raises(ValueError):
        check_audit([])


def test_the_card_is_the_servers_alone_and_says_nothing_of_how_tasks_are_rated():
    # TEMPLATES is push_surface's menu: the model must not be able to mint this card or see it offered
    assert "check_audit" not in TEMPLATES
    assert "check_audit" not in str(_PUSH_SURFACE_SCHEMA)
    words = _text(check_audit(_items(2))).lower() + check_audit(_items(2)).title.lower()
    for word in ("score", "credit", "reward", "points"):
        assert word not in words, word


# ---- the handler ------------------------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def agent() -> str:
    return f"test-audit-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def router(db, settings, agent):
    return ActionRouter(db, settings.model_copy(update={"agent_id": agent}), SimpleNamespace())


async def _rows(db, agent, *surfaces: str | None) -> list[CheckAudit]:
    rows = [
        CheckAudit(agent_id=agent, task_id=uuid.uuid4(), node_id=uuid.uuid4(), check_kind="file_exists", surface_id=s)
        for s in surfaces
    ]
    async with db.session() as s:
        s.add_all(rows)
        await s.commit()
    return rows


async def _row(db, audit_id) -> CheckAudit:
    async with db.session() as s:
        return (await s.execute(select(CheckAudit).where(CheckAudit.id == audit_id))).scalar_one()


async def _tap(router, surface, audit_id, verdict, *, actor="unattributed"):
    ctx = ActionContext(
        surface=surface,
        name="audit.verdict",
        context={"auditId": str(audit_id), "verdict": verdict},
        data_model=None,
        services=router,
        actor=actor,
    )
    return await router._handlers["audit.verdict"].fn(ctx)


def _card(surface_id: str, *rows: CheckAudit) -> SimpleNamespace:
    return SimpleNamespace(surface_id=surface_id, data_model={"items": {str(r.id): "pending" for r in rows}})


async def test_the_verdict_handler_is_registered_as_a_mutating_action(router):
    meta = router._handlers["audit.verdict"]
    assert (meta.mutating, meta.irreversible) == (True, False)


async def test_right_and_wrong_are_recorded_once_and_patch_the_item(db, router):
    a, b = await _rows(db, router._settings.agent_id, "s-card", "s-card")
    card = _card("s-card", a, b)

    got = await _tap(router, card, a.id, "right")
    assert (got.ok, got.data_patches, got.resolve_surface) == (True, [(f"/items/{a.id}", "right")], False)
    row = await _row(db, a.id)
    assert (row.verdict, row.actor) == ("right", "companion:unattributed")
    assert row.answered_at is not None
    card.data_model["items"][str(a.id)] = "right"  # what the router's patch does to the server copy

    got = await _tap(router, card, b.id, "wrong")
    assert (got.ok, got.data_patches, got.resolve_surface) == (True, [(f"/items/{b.id}", "wrong")], True)  # the last
    assert (await _row(db, b.id)).verdict == "wrong"


async def test_the_same_verdict_again_is_a_no_op_and_a_different_one_is_refused(db, router):
    (a,) = await _rows(db, router._settings.agent_id, "s-card")
    card = _card("s-card", a)
    assert (await _tap(router, card, a.id, "right")).ok
    first = await _row(db, a.id)

    again = await _tap(router, card, a.id, "right")
    assert (again.ok, again.data_patches) == (True, [(f"/items/{a.id}", "right")])
    changed = await _tap(router, card, a.id, "wrong")
    assert (changed.ok, changed.message) == (False, "already answered right")
    after = await _row(db, a.id)
    assert (after.verdict, after.answered_at) == ("right", first.answered_at)


async def test_an_item_the_card_never_offered_is_refused(db, router):
    a, other = await _rows(db, router._settings.agent_id, "s-card", "s-card")
    card = _card("s-card", a)  # other is a real row of this card's surface, but not an item of it
    for audit_id, verdict in ((other.id, "right"), (uuid.uuid4(), "right"), ("not-a-uuid", "right")):
        got = await _tap(router, card, audit_id, verdict)
        assert (got.ok, got.message) == (False, "that item is not on this card")
    card.data_model["items"]["not-a-uuid"] = "pending"  # even when the card lists it, it must parse
    assert not (await _tap(router, card, "not-a-uuid", "right")).ok
    bad = await _tap(router, card, a.id, "maybe")
    assert (bad.ok, bad.message) == (False, "verdict must be one of ['right', 'wrong']")
    assert [(await _row(db, r.id)).verdict for r in (a, other)] == [None, None]


async def test_a_row_that_went_out_on_another_card_or_never_went_out_is_refused(db, router, agent):
    elsewhere, unpushed = await _rows(db, agent, "s-other-card", None)
    (theirs,) = await _rows(db, f"{agent}-b", "s-card")  # another agent's row, on a card of the same id
    card = _card("s-card", elsewhere, unpushed, theirs)  # a card that lists ids it does not own
    for row in (elsewhere, unpushed, theirs):
        got = await _tap(router, card, row.id, "right")
        assert (got.ok, got.message) == (False, "that item is not on this card")
        assert (await _row(db, row.id)).verdict is None


# ---- end to end, through the router ------------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_a_tap_through_the_router_records_the_verdict_and_the_action_row(db, settings, agent):
    from nous.a2ui.service import SurfaceService
    from nous.observability.task_score_gatherer import audits_without_action_row

    a2ui_settings = settings.model_copy(
        update={"agent_id": agent, "telegram_bot_token": None, "telegram_chat_id": None}
    )
    service = SurfaceService(db, a2ui_settings)
    router = ActionRouter(db, a2ui_settings, service)
    a, b = await _rows(db, agent, None, None)
    items = [{"audit_id": str(r.id), "task": TASK, "check": CHECK, "result": RESULT} for r in (a, b)]
    surface_id = await service.push_built(check_audit(items))
    async with db.session() as s:
        for r in (a, b):
            (await s.get(CheckAudit, r.id)).surface_id = surface_id
        await s.commit()
        nonce = (await s.get(A2uiSurface, surface_id)).nonce
    try:
        body = {
            "version": "v1.0",
            "action": {
                "name": "audit.verdict",
                "surfaceId": surface_id,
                "context": {"auditId": str(a.id), "verdict": "right"},
                "metadata": {"extensions": {"com_nous_nonce": nonce}},
            },
        }
        status, payload = await router.handle(body, content_type="application/json")
        assert (status, payload["resolved"]) == (200, False)
        async with db.session() as s:
            surface = await s.get(A2uiSurface, surface_id)
            assert surface.data_model["items"] == {str(a.id): "right", str(b.id): "pending"}
            actions = (await s.execute(select(A2uiAction).where(A2uiAction.agent_id == agent))).scalars().all()
            assert [(x.action_name, x.status) for x in actions] == [("audit.verdict", "completed")]
            assert await audits_without_action_row(s, agent) == 0
        assert (await _row(db, a.id)).verdict == "right"
    finally:
        async with db.session() as s:
            await s.execute(delete(A2uiAction).where(A2uiAction.agent_id == agent))
            await s.execute(delete(A2uiSurface).where(A2uiSurface.agent_id == agent))
            await s.commit()
