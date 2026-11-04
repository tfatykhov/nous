"""Write quality of decisions, censor matches and procedure outcomes.

- A decision whose description changes is linked again on the new text (decision_relink_on_update_enabled).
- A censor_triggered event says which leg matched and with what score.
- A procedure outcome records what it rests on, and an error outside the procedure's core tools can be
  neutral (procedure_outcome_attribution_enabled).
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

import pytest
from conftest import MockEmbeddingProvider
from sqlalchemy import select, text

from nous.brain.brain import Brain
from nous.brain.graph_linker import GraphLinker
from nous.brain.schemas import ReasonInput, RecordInput
from nous.cognitive.layer import CognitiveLayer
from nous.cognitive.schemas import ToolResult, TurnResult
from nous.events import Event as BusEvent
from nous.events import EventBus
from nous.handlers.decision_graph_linker import DecisionGraphLinker
from nous.heart import CensorInput, ProcedureInput
from nous.storage.models import Event, Fact

_PLACEHOLDER = "Plan: deploy the cache layer tonight"
_FINAL = ("Use Redis for the cache layer", "Compared Redis with Memcached", "Prefer mature stores")


class _Embedder(MockEmbeddingProvider):
    """The mock's hash vectors, blind to a leading "Plan: " or graph-template prefix.

    So "Plan: X" and "X" share a vector (a re-link re-derives the same edge), and the linker's
    "decision: X" / "fact: X" templates match a fact whose content is X. ``fail`` makes embed raise.
    """

    fail = False

    async def embed(self, text: str, fail_fast: bool = False) -> list[float]:
        if self.fail:
            raise RuntimeError("embedding API down")
        for prefix in ("decision: ", "fact: ", "episode: ", "Plan: "):
            text = text.removeprefix(prefix)
        return await super().embed(text)


class _Bus:
    """Collects what Brain publishes."""

    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:
        self.events.append(event)

    def of(self, event_type: str) -> list:
        return [e for e in self.events if e.type == event_type]


def _brain(db, settings, embedder, **overrides) -> Brain:
    scoped = settings.model_copy(update={"agent_id": f"test-w1-{uuid.uuid4().hex[:12]}", **overrides})
    brain = Brain(database=db, settings=scoped, embedding_provider=embedder)
    brain._bus = _Bus()
    return brain


def _record(description: str, context: str | None = None, pattern: str | None = None) -> RecordInput:
    return RecordInput(
        description=description, context=context, pattern=pattern, confidence=0.5,
        category="process", stakes="low", reasons=[ReasonInput(type="analysis", text="test reason")],
    )


async def _related_to(session, a, b) -> list[int]:
    """ltp_count of the related_to edge between two decisions ([] when there is none)."""
    src, tgt = sorted([str(a), str(b)])
    rows = await session.execute(
        text(
            "SELECT ltp_count FROM brain.graph_edges"
            " WHERE source_id = :s AND target_id = :t AND relation = 'related_to'"
        ),
        {"s": src, "t": tgt},
    )
    return [r.ltp_count for r in rows]


# ---------------------------------------------------------------------------
# Re-link on a new description
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.parametrize("enabled", [True, False])
async def test_update_links_the_decision_on_its_new_description(db, settings, session, enabled):
    brain = _brain(db, settings, _Embedder(), decision_relink_on_update_enabled=enabled)
    placeholder_twin = await brain.record(_record(_PLACEHOLDER), session=session)
    final_twin = await brain.record(_record(*_FINAL), session=session)
    decision = await brain.record(_record(_PLACEHOLDER), session=session)
    assert await _related_to(session, decision.id, placeholder_twin.id) == [0]

    await brain.update(decision.id, description=_FINAL[0], context=_FINAL[1], pattern=_FINAL[2], session=session)

    assert bool(await _related_to(session, decision.id, final_twin.id)) is enabled
    # Add-only: the edge made on the placeholder stays.
    assert await _related_to(session, decision.id, placeholder_twin.id) == [0]


@pytest.mark.postgres_only
async def test_update_that_keeps_the_description_links_nothing(db, settings, session):
    """Context and pattern move the vector onto a neighbour, but only a new description re-links."""
    brain = _brain(db, settings, _Embedder(), decision_relink_on_update_enabled=True)
    final_twin = await brain.record(_record(*_FINAL), session=session)
    decision = await brain.record(_record(_FINAL[0]), session=session)
    assert await _related_to(session, decision.id, final_twin.id) == []

    await brain.update(decision.id, description=_FINAL[0], context=_FINAL[1], pattern=_FINAL[2], session=session)
    await session.commit()
    await asyncio.sleep(0)

    assert await _related_to(session, decision.id, final_twin.id) == []
    assert brain._bus.of("decision_updated") == []


@pytest.mark.postgres_only
async def test_update_whose_embed_failed_links_nothing(db, settings, session):
    """The old vector is still stored: linking on it would repeat the placeholder's links."""
    embedder = _Embedder()
    brain = _brain(db, settings, embedder, decision_relink_on_update_enabled=True)
    decision = await brain.record(_record(_PLACEHOLDER), session=session)
    # A neighbour of the placeholder vector that the record could not link (it did not exist yet).
    late = await brain.record(_record("Some other decision text"), session=session)
    await session.execute(
        text(
            "UPDATE brain.decisions SET embedding = (SELECT embedding FROM brain.decisions WHERE id = :d)"
            " WHERE id = :l"
        ),
        {"d": decision.id, "l": late.id},
    )

    embedder.fail = True
    await brain.update(decision.id, description=_FINAL[0], session=session)
    await session.commit()
    await asyncio.sleep(0)

    assert await _related_to(session, decision.id, late.id) == []
    assert brain._bus.of("decision_updated") == []


@pytest.mark.postgres_only
async def test_relink_does_not_count_as_ltp_reinforcement(db, settings, session):
    """Re-deriving the placeholder's own edge from the same turn's text is not new evidence."""
    brain = _brain(
        db, settings, _Embedder(), decision_relink_on_update_enabled=True, tinyhippo_lite_enabled=True,
    )
    twin = await brain.record(_record("deploy the cache layer tonight"), session=session)
    decision = await brain.record(_record(_PLACEHOLDER), session=session)
    assert await _related_to(session, decision.id, twin.id) == [0]
    # A neighbour the record could not link (it did not exist yet): its edge shows the re-link ran.
    late = await brain.record(_record("Some other decision text"), session=session)
    await session.execute(
        text(
            "UPDATE brain.decisions SET embedding = (SELECT embedding FROM brain.decisions WHERE id = :t)"
            " WHERE id = :l"
        ),
        {"t": twin.id, "l": late.id},
    )

    # "Plan: X" -> "X": a new description on the same vector, so the re-link hits the same edge.
    await brain.update(decision.id, description="deploy the cache layer tonight", session=session)

    assert await _related_to(session, decision.id, late.id) == [0]
    assert await _related_to(session, decision.id, twin.id) == [0]


@pytest.mark.postgres_only
async def test_decision_updated_is_published_once_after_commit(db, settings, session):
    brain = _brain(db, settings, _Embedder(), decision_relink_on_update_enabled=True)
    decision = await brain.record(_record(_PLACEHOLDER), session=session)

    await brain.update(decision.id, description=_FINAL[0], session=session)
    await asyncio.sleep(0)
    assert brain._bus.of("decision_updated") == [], "published before the caller committed"
    await session.commit()
    await asyncio.sleep(0)

    [event] = brain._bus.of("decision_updated")
    assert event.data == {"decision_id": str(decision.id)}
    assert event.agent_id == brain.agent_id
    # update() already wrote the audit row; the bus must not write a second one.
    assert event.persist is False


@pytest.mark.postgres_only
async def test_decision_updated_is_not_published_on_rollback(db, settings):
    brain = _brain(db, settings, _Embedder(), decision_relink_on_update_enabled=True)
    async with db.session() as own:
        decision = await brain.record(_record(_PLACEHOLDER), session=own)
        await brain.update(decision.id, description=_FINAL[0], session=own)
        await own.rollback()
    await asyncio.sleep(0)

    assert brain._bus.events == []


@pytest.mark.postgres_only
async def test_brain_owned_update_publishes_decision_updated(db, settings):
    """The live path: finalize passes no session, so update() commits its own."""
    brain = _brain(db, settings, _Embedder(), decision_relink_on_update_enabled=True)
    decision = await brain.record(_record(_PLACEHOLDER))
    try:
        await brain.update(decision.id, description=_FINAL[0])
        await asyncio.sleep(0)
        assert [e.data["decision_id"] for e in brain._bus.of("decision_updated")] == [str(decision.id)]
    finally:
        await brain.delete(decision.id)


class _SessionDB:
    """A Database stand-in whose sessions are the test's own (rolled back at the end)."""

    def __init__(self, session) -> None:
        self._session = session

    @asynccontextmanager
    async def session(self):
        yield self._session


class _BrainReader:
    def __init__(self, brain: Brain, session) -> None:
        self._brain, self._session = brain, session

    async def get(self, decision_id):
        return await self._brain.get(decision_id, session=self._session)


async def _drain(bus: EventBus) -> None:
    await asyncio.sleep(0)  # let the post-commit emits run
    while not bus._queue.empty():
        await bus._dispatch(bus._queue.get_nowait())


async def _linked_setup(db, settings, session, **overrides):
    """A brain on a real bus with DecisionGraphLinker subscribed, and a fact that states _FINAL[0]."""
    embedder = _Embedder()
    brain = _brain(db, settings, embedder, decision_relink_on_update_enabled=True, **overrides)
    fact = Fact(agent_id=brain.agent_id, content=_FINAL[0], embedding=await embedder.embed(_FINAL[0]))
    session.add(fact)
    await session.flush()
    bus = EventBus()
    brain._bus = bus
    linker = GraphLinker(_SessionDB(session), embedder, brain.settings, brain.agent_id)
    handler = DecisionGraphLinker(_BrainReader(brain, session), linker, embedder, brain.settings, bus)
    return brain, fact, bus, handler


async def _edges(session, source_id, target_id) -> list:
    rows = await session.execute(
        text("SELECT relation, ltp_count FROM brain.graph_edges WHERE source_id = :s AND target_id = :t"),
        {"s": source_id, "t": target_id},
    )
    return [tuple(r) for r in rows]


@pytest.mark.postgres_only
async def test_graph_linker_links_facts_to_the_new_description(db, settings, session):
    brain, fact, bus, _ = await _linked_setup(db, settings, session)
    # pre_turn: the placeholder is recorded and committed, and the linker runs on it.
    decision = await brain.record(_record(_PLACEHOLDER), session=session)
    await session.commit()
    await _drain(bus)
    assert await _edges(session, fact.id, decision.id) == []

    # post_turn: finalize writes the real description.
    await brain.update(decision.id, description=_FINAL[0], session=session)
    await session.commit()
    await _drain(bus)

    assert await _edges(session, fact.id, decision.id) == [("evidence_for", 0)]


@pytest.mark.postgres_only
@pytest.mark.parametrize(("event_type", "ltp"), [("decision_recorded", 1), ("decision_updated", 0)])
async def test_graph_linker_relink_does_not_count_as_ltp_reinforcement(db, settings, session, event_type, ltp):
    brain, fact, _, handler = await _linked_setup(db, settings, session, tinyhippo_lite_enabled=True)
    decision = await brain.record(_record(_FINAL[0]), session=session)
    event = BusEvent(type="decision_recorded", agent_id=brain.agent_id, data={"decision_id": str(decision.id)})
    await handler.handle(event)
    assert await _edges(session, fact.id, decision.id) == [("evidence_for", 0)]

    await handler.handle(BusEvent(type=event_type, agent_id=brain.agent_id, data={"decision_id": str(decision.id)}))

    assert await _edges(session, fact.id, decision.id) == [("evidence_for", ltp)]


@pytest.mark.postgres_only
@pytest.mark.parametrize(("reinforce", "expected"), [(True, 1), (False, 0)])
async def test_create_edge_reinforce_false_keeps_ltp(db, settings, session, reinforce, expected):
    scoped = settings.model_copy(update={"tinyhippo_lite_enabled": True})
    linker = GraphLinker(db, None, scoped, f"test-w1-{uuid.uuid4().hex[:12]}")
    src, tgt = uuid.uuid4(), uuid.uuid4()
    edge = dict(
        source_id=src, target_id=tgt, source_type="fact", target_type="decision",
        relation="evidence_for", weight=0.9, session=session,
    )
    assert await linker.create_edge(**edge) is not None
    assert await linker.create_edge(**edge, reinforce=reinforce) is None

    ltp = await session.scalar(
        text("SELECT ltp_count FROM brain.graph_edges WHERE source_id = :s AND target_id = :t"), {"s": src, "t": tgt},
    )
    assert ltp == expected


# ---------------------------------------------------------------------------
# Censor match telemetry
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.parametrize(
    ("pattern", "checked", "matched_by"),
    [
        # The checked text is the embedded text (cosine 1.0); the anchors keep the regex off it.
        ("^zqx-w1$", "^zqx-w1$ {reason}", "semantic"),
        ("zqx-w1", "zqx-w1 {reason}", "both"),
        # A regex-only hit scores 1.0 by convention, not as a cosine.
        ("zqx-w1", "please zqx-w1 now", "regex"),
    ],
)
async def test_censor_triggered_says_which_leg_matched(heart, session, pattern, checked, matched_by):
    reason = f"w1 telemetry {uuid.uuid4().hex}"
    censor = await heart.add_censor(
        CensorInput(trigger_pattern=pattern, reason=reason, action="steer"), session=session,
    )

    matches = await heart.check_censors(checked.format(reason=reason), session=session)

    [match] = [m for m in matches if m.id == censor.id]
    assert match.matched_by == matched_by
    assert match.score == pytest.approx(1.0, abs=1e-6)
    event = (await session.execute(
        select(Event).where(Event.event_type == "censor_triggered", Event.data["censor_id"].astext == str(censor.id))
    )).scalar_one()
    assert event.data["matched_by"] == matched_by
    assert event.data["score"] == pytest.approx(1.0, abs=1e-6)
    assert event.data["semantic_checked"] is True


class _CheckOutage(MockEmbeddingProvider):
    """Embeds censors when they are added, fails the per-turn check embed (the only fail_fast caller)."""

    async def embed(self, text: str, fail_fast: bool = False) -> list[float]:
        if fail_fast:
            raise RuntimeError("embedding API down")
        return await super().embed(text)


@pytest.mark.postgres_only
async def test_censor_triggered_says_when_the_semantic_leg_did_not_run(db, settings, session):
    """During an outage every hit is a regex hit; the event says the cosine was never computed."""
    from nous.heart import Heart

    heart = Heart(db, settings.model_copy(update={"agent_id": f"test-w1-{uuid.uuid4().hex[:12]}"}),
                  embedding_provider=_CheckOutage())
    reason = f"w1 outage {uuid.uuid4().hex}"
    censor = await heart.add_censor(
        CensorInput(trigger_pattern="zqx-w1", reason=reason, action="steer"), session=session,
    )

    matches = await heart.check_censors(f"zqx-w1 {reason}", session=session)

    assert [m.matched_by for m in matches if m.id == censor.id] == ["regex"]
    event = (await session.execute(
        select(Event).where(Event.event_type == "censor_triggered", Event.data["censor_id"].astext == str(censor.id))
    )).scalar_one()
    assert event.data["semantic_checked"] is False


# ---------------------------------------------------------------------------
# Procedure outcome attribution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("core_tools", "turn_error", "failed", "label"),
    [
        (["read_file"], False, [], "none"),
        (["read_file"], True, [], "turn_error"),
        (["read_file"], True, ["read_file"], "turn_error"),
        (["read_file"], False, ["read_file"], "core_tool_error"),
        (["read_file"], False, ["bash"], "unrelated_error"),
        ([], False, ["bash"], "unrelated_error"),
        # Compared by canonical name: a stored old name matches a call under the new one.
        (["recall_recent"], False, ["recall_deep"], "core_tool_error"),
        # A retired name matches nothing, but it still is a failed call.
        (["recall_hubs"], False, ["recall_hubs"], "unrelated_error"),
    ],
)
def test_outcome_attribution(core_tools, turn_error, failed, label):
    from nous.heart.procedures import outcome_attribution

    assert outcome_attribution(core_tools, turn_error=turn_error, failed_tools=failed) == label


def test_outcome_attribution_is_quiet_about_retired_names(caplog):
    """It runs on every turn that renders a procedure, flag on or off: no per-turn warning."""
    from nous.heart.procedures import outcome_attribution

    with caplog.at_level("WARNING"):
        assert outcome_attribution(["recall_hubs"], turn_error=False, failed_tools=["recall_hubs"]) == "unrelated_error"
    assert caplog.records == []


async def _outcome_after_turn(db, heart, settings, session, *, enabled, tool_results, error=None):
    """Counters and procedure_outcome event data of a procedure rendered in one turn."""
    layer_settings = settings.model_copy(update={"procedure_outcome_attribution_enabled": enabled})
    brain = Brain(database=db, settings=layer_settings)
    layer = CognitiveLayer(brain, heart, layer_settings, identity_prompt="You are Nous.")
    proc = await heart.store_procedure(
        ProcedureInput(name=f"w1 attribution {uuid.uuid4().hex}", domain="testing", core_tools=["read_file"]),
        session=session,
    )
    sid = f"test-w1-attr-{uuid.uuid4().hex[:8]}"
    ctx = await layer.pre_turn(settings.agent_id, sid, "xyzzy foobar gibberish", session=session)
    ctx.recalled_procedure_ids = [str(proc.id)]

    await layer.post_turn(
        settings.agent_id, sid, TurnResult(response_text="Done.", tool_results=tool_results, error=error), ctx,
        session=session,
    )

    detail = await heart.get_procedure(proc.id, session=session)
    [event] = (await session.execute(
        select(Event).where(Event.event_type == "procedure_outcome", Event.data["procedure_id"].astext == str(proc.id))
    )).scalars().all()
    counts = (detail.success_count, detail.failure_count, detail.neutral_count)
    return counts, event.data


@pytest.mark.postgres_only
@pytest.mark.parametrize(
    ("enabled", "tool_results", "error", "counts", "label"),
    [
        # An unrelated tool failed: a failure today, neutral with attribution on.
        (False, [ToolResult(tool_name="bash", error="boom")], None, (0, 1, 0), "unrelated_error"),
        (True, [ToolResult(tool_name="bash", error="boom")], None, (0, 0, 1), "unrelated_error"),
        (True, [ToolResult(tool_name="read_file", error="no such file")], None, (0, 1, 0), "core_tool_error"),
        (True, [ToolResult(tool_name="read_file", result="ok")], None, (1, 0, 0), "none"),
        (True, [], "the turn failed", (0, 1, 0), "turn_error"),
    ],
)
async def test_procedure_outcome_attribution(
    db, heart, settings, session, enabled, tool_results, error, counts, label,
):
    got_counts, data = await _outcome_after_turn(
        db, heart, settings, session, enabled=enabled, tool_results=tool_results, error=error,
    )
    assert got_counts == counts
    assert data["attribution"] == label
