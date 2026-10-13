"""F099 Phase 3a: the reserved dedup prefix of the owner's intention cards (contract section 4.3)."""

from __future__ import annotations

import ast
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select, update

import nous
from nous.a2ui.actions import EXPIRED_UNANSWERED
from nous.a2ui.builders import TEMPLATES, approval_gate
from nous.a2ui.intention_keys import (
    ACTIVE_KEY,
    INTENTION_DEDUP_PREFIX,
    parse_key,
    proposal_key,
    question_key,
)
from nous.a2ui.service import ReservedDedupKeyError, SurfaceService
from nous.storage.models import A2uiAction, A2uiSurface

ID = uuid.UUID("1234abcd-1234-5678-9abc-def012345678")  # has hex letters, so .upper() differs


def test_the_key_grammar_round_trips():
    assert INTENTION_DEDUP_PREFIX == "intention:"
    assert proposal_key(ID) == f"intention:proposal:{ID}" and question_key(ID) == f"intention:question:{ID}"
    assert ACTIVE_KEY == "intention:active"
    assert parse_key(proposal_key(ID)) == ("proposal", ID)
    assert parse_key(question_key(ID)) == ("question", ID)
    assert parse_key(ACTIVE_KEY) == ("active", None)


@pytest.mark.parametrize(
    "junk",
    [
        None,
        5,
        "",
        "intention:",
        "intention:proposal:",
        "intention:proposal:not-a-uuid",
        f"intention:other:{ID}",
        f"intention:proposal:{ID}:x",
        f"intention:proposal:{str(ID).upper()}",  # canonical lower-case form only
        f"intention:proposal:{ID.hex}",  # the dashed form only
        f"dag-approval:{ID}",
        f"Intention:proposal:{ID}",
        "intention:active:extra",
    ],
)
def test_parse_key_rejects_anything_that_is_not_exactly_ours(junk):
    assert parse_key(junk) is None


def _card(title="Restart?"):
    return approval_gate(
        {
            "title": title,
            "summary": "s",
            "risk": "r",
            "options": [{"id": "go", "label": "Go"}, {"id": "wait", "label": "Wait"}],
        }
    )


@pytest.fixture
async def svc(db, settings):
    agent = f"test-3a-keys-{uuid.uuid4().hex[:8]}"
    config = settings.model_copy(update={"agent_id": agent, "telegram_bot_token": None, "telegram_chat_id": None})
    service = SurfaceService(db, config)
    yield service
    async with db.session() as session:
        await session.execute(delete(A2uiAction).where(A2uiAction.agent_id == agent))
        await session.execute(delete(A2uiSurface).where(A2uiSurface.agent_id == agent))
        await session.commit()


@pytest.mark.parametrize("key", [proposal_key(ID), question_key(ID), ACTIVE_KEY, "intention:anything"])
async def test_push_built_refuses_the_reserved_prefix_without_the_flag(svc, key):
    with pytest.raises(ReservedDedupKeyError, match="reserved"):
        await svc.push_built(_card(), dedup_key=key)
    assert not issubclass(ReservedDedupKeyError, PermissionError)  # never read as a censor refusal


async def test_push_surface_cannot_mint_an_intention_card(svc):
    """The agent tool passes a model-supplied ``dedup_key`` straight to ``push_built`` (no ``reserved_key_ok``)."""
    from nous.a2ui.tools import register_a2ui_tools
    from nous.api.tools import ToolDispatcher

    dispatcher = ToolDispatcher()
    register_a2ui_tools(dispatcher, svc)
    text, is_error = await dispatcher.dispatch(
        "push_surface",
        {
            "template": "approval_gate",
            "params": {"title": "t", "summary": "s", "risk": "r", "options": [{"id": "a", "label": "A"}]},
            "dedup_key": proposal_key(ID),
        },
    )
    assert is_error is True and "reserved" in text


def test_the_builders_are_not_templates():
    assert not {name for name in TEMPLATES if "intention" in name}


def test_only_the_orchestrator_may_pass_reserved_key_ok():
    """``reserved_key_ok`` now unlocks BOTH reserved prefixes (conflict C11): the set of modules that pass it to
    push_built is pinned. Task 11 adds ``handlers/continuation_publisher.py`` to this set. ``push_surface`` and
    ``compose_surface`` (``tools.py``) pass no ``reserved_key_ok`` (compose_surface at ~896-900), so the reserved clause
    refuses an ``intention:`` key from either."""
    root = Path(nous.__file__).parent
    users = set()
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "push_built"
                and any(k.arg == "reserved_key_ok" for k in node.keywords)
            ):
                users.add(path.relative_to(root).as_posix())
    assert users == {
        "a2ui/service.py",
        "dag/orchestrator.py",
        "handlers/continuation_publisher.py",
        "handlers/continuation_runner.py",
    }


@pytest.mark.postgres_only
async def test_an_intention_card_that_lapses_writes_no_expired_unanswered_row(svc, db):
    """S1: the proposal or question row is the record, as the DAG node is for an approval card."""
    mine = await svc.push_built(_card(), dedup_key=proposal_key(ID), reserved_key_ok=True, notify=False)
    plain = await svc.push_built(_card("Other?"), dedup_key="plain:1", notify=False)
    past = datetime.now(UTC) - timedelta(minutes=1)
    async with db.session() as session:
        await session.execute(
            update(A2uiSurface).where(A2uiSurface.surface_id.in_([mine, plain])).values(expires_at=past)
        )
        await session.commit()
    assert await svc.expire_sweep() == 2
    async with db.session() as session:
        rows = (
            await session.execute(
                select(A2uiAction.surface_id, A2uiAction.action_name).where(A2uiAction.surface_id.in_([mine, plain]))
            )
        ).all()
        statuses = dict(
            (
                await session.execute(
                    select(A2uiSurface.surface_id, A2uiSurface.status).where(A2uiSurface.surface_id.in_([mine, plain]))
                )
            ).all()
        )
    assert statuses == {mine: "expired", plain: "expired"}
    assert (plain, EXPIRED_UNANSWERED) in rows and not [r for r in rows if r[0] == mine]
