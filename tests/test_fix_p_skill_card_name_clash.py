"""A strategy card and a skill with the same name.

A strategy card is a ``heart.procedures`` row with ``kind='strategy'``: a lesson
distilled from a decision. A skill is a how-to procedure. Both kinds share one
unique index, ``(agent_id, lower(name)) WHERE active``, so only one of them can
hold a name. Every test runs production code on real rows under its own
``agent_id`` (array columns and the unique index: Postgres lane only).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from nous.api.tools import create_nous_tools
from nous.brain.brain import Brain
from nous.config import Settings
from nous.heart import Heart, ProcedureInput
from nous.heart.schemas import STRATEGY_CARD_KIND
from nous.skills.bootstrap import bootstrap_local_skills, reactivate_skills
from nous.storage.models import Event, Procedure

pytestmark = pytest.mark.postgres_only

NAME = "Deploy Checklist"
LESSON = "When a release can be switched back in one step, ship it that way."
DECISION_ID = str(uuid4())  # the decision the card was distilled from


@pytest_asyncio.fixture
async def heart(db, mock_embeddings):
    """A Heart under its own agent_id. The code under test commits through its
    own sessions, so the fixture deletes the rows again."""
    settings = Settings().model_copy(update={"agent_id": f"fix-p-{uuid4().hex[:8]}"})
    h = Heart(db, settings, embedding_provider=mock_embeddings)
    yield h
    async with db.session() as s:
        for model in (Procedure, Event):
            await s.execute(delete(model).where(model.agent_id.like(f"{h.agent_id}%")))
        await s.commit()
    await h.close()


async def _card(heart, name: str = NAME):
    """An active strategy card, stored the way the distiller stores one."""
    return await heart.procedures.store(
        ProcedureInput(
            name=name,
            domain="strategy",
            description="Reversible rollouts keep an outage short",
            implementation_notes=[LESSON],
            kind=STRATEGY_CARD_KIND,
            runtime_metadata={"source_decision_id": DECISION_ID, "outcome": "success"},
        )
    )


def _how_to(name: str = NAME, **over) -> ProcedureInput:
    fields = dict(
        name=name,
        domain="ops",
        description="How to deploy",
        implementation_notes=["Run the checks, then deploy."],
        tags=["skill"],
    )
    fields.update(over)
    return ProcedureInput(**fields)


async def _row(heart, **columns):
    """A row written directly, an active card unless told otherwise: another
    agent's, a retired one, or one whose id the test picks. Returns its id."""
    row = Procedure(**{"agent_id": heart.agent_id, "name": NAME, "kind": STRATEGY_CARD_KIND, "active": True, **columns})
    async with heart.db.session() as s:
        s.add(row)
        await s.commit()
        return row.id


async def _rows(heart) -> dict:
    """Every procedure row of the agent and its neighbours: id -> the columns the tests look at."""
    async with heart.db.session() as s:
        result = await s.execute(select(Procedure).where(Procedure.agent_id.like(f"{heart.agent_id}%")))
        return {
            p.id: {
                "name": p.name,
                "kind": p.kind,
                "active": p.active,
                "body": (p.implementation_notes or [None])[-1],
                "source": (p.runtime_metadata or {}).get("source_decision_id"),
            }
            for p in result.scalars().all()
        }


def _moved(name: str, card_id) -> str:
    """The name a card has after it gave ``name`` up."""
    return f"{name} ({card_id.hex[:6]})"


# ---------------------------------------------------------------------------
# A card gives its name up to a how-to procedure
# ---------------------------------------------------------------------------


async def test_a_how_to_procedure_is_stored_under_a_name_a_card_holds(heart, caplog):
    """The unique index is shared by both kinds, so the card held the name against
    every how-to insert: a skill, an auto-learned procedure, the MCP teach call.
    The card stays an active card of its decision, under another name, and no
    other card is touched."""
    card = await _card(heart)
    bystanders = {
        await _row(heart, agent_id=f"{heart.agent_id}-other"): NAME,  # another agent's card
        await _row(heart, active=False): NAME,  # a retired card of this agent
        (await _card(heart, "Prefer reversible rollouts")).id: "Prefer reversible rollouts",
    }

    with caplog.at_level("INFO", logger="nous.heart.procedures"):
        how_to = await heart.store_procedure(_how_to(NAME.lower()))

    rows = await _rows(heart)
    assert rows[how_to.id] == {
        "name": NAME.lower(),
        "kind": None,
        "active": True,
        "body": "Run the checks, then deploy.",
        "source": None,
    }
    assert rows[card.id] == {
        "name": _moved(NAME, card.id),
        "kind": STRATEGY_CARD_KIND,
        "active": True,
        "body": LESSON,
        "source": DECISION_ID,
    }
    assert {row_id: rows[row_id]["name"] for row_id in bystanders} == bystanders
    assert f"Strategy card {card.id} renamed to {_moved(NAME, card.id)!r}" in caplog.text


async def test_an_inactive_how_to_procedure_leaves_the_card_its_name(heart):
    """An inactive row is outside the index: nothing has to move."""
    card = await _card(heart)

    await heart.store_procedure(_how_to(active=False))

    assert (await _rows(heart))[card.id]["name"] == NAME


@pytest.mark.parametrize("kind", ["card", "how_to"])
async def test_a_row_of_the_same_kind_still_cannot_take_the_name(heart, kind):
    """Only a card yields, and only to a how-to procedure. Card against card is
    the distiller's business (it picks a free name before it stores, and retries
    on this error); how-to against how-to is a duplicate, as it always was."""
    first = await _card(heart) if kind == "card" else await heart.store_procedure(_how_to())

    with pytest.raises(IntegrityError):
        if kind == "card":
            await _card(heart, NAME.lower())
        else:
            await heart.store_procedure(_how_to(NAME.lower()))

    assert (await _rows(heart))[first.id]["name"] == NAME


async def test_the_card_is_found_the_way_the_index_compares_names(heart):
    """The index lower-cases in the database. Python lower-cases this name
    differently (a dotted capital I becomes two code points), so a comparison
    made in Python would miss the card that the index then refuses."""
    name = "\u0130stanbul Deploy"
    card = await _card(heart, name)

    await heart.store_procedure(_how_to(name))

    assert (await _rows(heart))[card.id]["name"] == _moved(name, card.id)


async def test_a_rename_that_did_not_go_out_is_not_logged(heart, caplog):
    """The name a card moves to can be held by another active row. The rename
    then fails on the same index and the how-to procedure is not stored: a known
    limit. What this pins is the log, which must not report that rename."""
    card = await _card(heart)
    await heart.store_procedure(_how_to(_moved(NAME, card.id)))

    with caplog.at_level("INFO", logger="nous.heart.procedures"), pytest.raises(IntegrityError):
        await heart.store_procedure(_how_to(NAME.lower()))

    assert sorted(row["name"] for row in (await _rows(heart)).values()) == [NAME, _moved(NAME, card.id)]
    assert "renamed to" not in caplog.text


@pytest.mark.parametrize("first", ["skill", "card"])
async def test_a_skill_is_reactivated_when_a_card_holds_its_name(heart, monkeypatch, first):
    """A skill that was inactive for a missing requirement comes back at start-up
    once the requirement is met, also when a card took its name meanwhile. The
    rename and the reactivation are two UPDATEs, and a flush sends UPDATEs in
    primary-key order: it works whichever of the two ids sorts ``first``."""
    var = f"FIX_P_REQ_{uuid4().hex[:8].upper()}"
    low, high = sorted((uuid4(), uuid4()))
    skill_id, card_id = (low, high) if first == "skill" else (high, low)
    await _row(heart, id=skill_id, kind=None, active=False, tags=["skill"], core_concepts=[f"requires:{var}"])
    await _row(heart, id=card_id)
    monkeypatch.setenv(var, "1")

    await reactivate_skills(heart)

    rows = await _rows(heart)
    assert (rows[skill_id]["name"], rows[skill_id]["active"]) == (NAME, True)
    assert (rows[card_id]["name"], rows[card_id]["active"]) == (_moved(NAME, card_id), True)


# ---------------------------------------------------------------------------
# A lookup of a skill by name never finds a card
# ---------------------------------------------------------------------------

SKILL_MD = f"---\nname: {NAME.lower()}\ndescription: How to deploy\n---\nRun the checks, then deploy.\n"


@pytest_asyncio.fixture
async def tools(heart):
    """The agent's tools (learn_skill, get_procedure) over the real Heart."""
    brain = Brain(database=heart.db, settings=heart.settings)
    yield create_nous_tools(brain, heart, settings=heart.settings)
    await brain.close()


def _skill_on_disk(tmp_path, name: str) -> str:
    """A workspace with one SKILL.md; returns the workspace directory."""
    skill_dir = tmp_path / "skills" / "deploy"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: How to deploy\n---\nRun the checks, then deploy.\n", encoding="utf-8"
    )
    return str(tmp_path)


async def test_bootstrap_registers_a_skill_whose_name_a_card_holds(heart, tmp_path):
    """The bootstrap asked "is this skill registered" with a lookup that returned
    the card, and skipped the skill at every start for as long as the card was
    active."""
    card = await _card(heart, NAME.lower())
    workspace = _skill_on_disk(tmp_path, NAME)

    assert await bootstrap_local_skills(workspace, heart) == 1
    assert await bootstrap_local_skills(workspace, heart) == 0  # the next start finds the skill

    rows = await _rows(heart)
    skill = next(row for row_id, row in rows.items() if row_id != card.id)
    assert (skill["name"], skill["kind"], skill["active"]) == (NAME, None, True)
    assert (rows[card.id]["kind"], rows[card.id]["active"]) == (STRATEGY_CARD_KIND, True)


async def test_learn_skill_registers_the_skill_and_leaves_the_card_a_card(heart, tools):
    """learn_skill refreshes an existing skill in place. With the card as the
    "existing skill" it rewrote the card's row: the lesson and the link to the
    decision were gone, and the reply said "updated"."""
    card = await _card(heart)

    reply = await tools["learn_skill"](source="inline", content=SKILL_MD)

    assert "Skill registered successfully" in reply["content"][0]["text"]
    rows = await _rows(heart)
    assert len(rows) == 2
    assert rows[card.id]["kind"] == STRATEGY_CARD_KIND
    assert rows[card.id]["body"] == LESSON
    assert rows[card.id]["source"] == DECISION_ID


async def test_a_skill_lookup_by_name_does_not_find_a_card(heart, tools):
    """get_procedure_by_name is what the bootstrap, learn_skill, the get_procedure
    tool and the Critic's skill picks use. None of them means a card. Asked by
    id, the tool returns the row whatever its kind, as before: a hub listing
    shows a card by its id."""
    card = await _card(heart)

    assert await heart.get_procedure_by_name(NAME) is None
    reply = await tools["get_procedure"](procedure_id=NAME)
    assert reply["content"][0]["text"] == f"No procedure found for '{NAME}'."
    reply = await tools["get_procedure"](procedure_id=str(card.id))
    assert reply["content"][0]["text"].startswith(f"**{NAME}** (strategy)")


async def test_a_superseded_card_does_not_stop_the_import_of_a_skill(heart, tmp_path):
    """The bootstrap does not re-import a skill that was consolidated into another
    procedure. A card archived that way is not that skill."""
    canonical = await heart.store_procedure(_how_to("Rollback Runbook"))
    card = await _card(heart)
    async with heart.db.session() as s:
        await s.execute(
            text("UPDATE heart.procedures SET active=false, archived_at=now(), superseded_by=:c WHERE id=:i"),
            {"c": canonical.id, "i": card.id},
        )
        await s.commit()

    assert await bootstrap_local_skills(_skill_on_disk(tmp_path, NAME), heart) == 1


async def test_without_a_card_the_bootstrap_and_learn_skill_do_what_they_did(heart, tools, tmp_path):
    """No card row (the state with both strategy-card flags off): a skill is
    registered once, found again by name in any casing, and refreshed in place."""
    workspace = _skill_on_disk(tmp_path, NAME)

    assert await bootstrap_local_skills(workspace, heart) == 1
    assert await bootstrap_local_skills(workspace, heart) == 0
    (skill_id,) = await _rows(heart)

    reply = await tools["learn_skill"](source="inline", content=SKILL_MD)

    assert "Skill updated successfully" in reply["content"][0]["text"]
    rows = await _rows(heart)
    assert list(rows) == [skill_id]
    assert rows[skill_id]["name"] == NAME.lower()
    assert (await heart.get_procedure_by_name(NAME.upper())).id == skill_id
