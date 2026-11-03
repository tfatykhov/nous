"""The bounded profile card (PR1, writer only): store, writer, sleep phase and REST.

Store and writer tests run on real Postgres, each with its own agent inside the rolled-back ``session``; the LLM is a
fake client that answers with one ``store_profile_card`` tool call.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, update

from nous.config import Settings
from nous.heart.profile_card import ProfileCardStore, ProfileCardWriter
from nous.storage.models import Fact, ProfileCard

NOW = datetime.now(UTC)


def _agent() -> str:
    return f"test-profile-card-{uuid.uuid4().hex[:8]}"


def _settings(agent: str, **overrides) -> Settings:
    return Settings(_env_file=None, agent_id=agent, **overrides)


def _llm(entries: list[dict] | None = None, *, raises: Exception | None = None, no_tool: bool = False) -> AsyncMock:
    """A fake LLM client: one tool call with ``entries``, a reply without a tool call, or an error."""
    client = AsyncMock()
    if raises is not None:
        client.call = AsyncMock(side_effect=raises)
        return client
    response = MagicMock()
    response.stop_reason = "tool_use"
    response.content = (
        [{"type": "text", "text": "no card"}]
        if no_tool
        else [{"type": "tool_use", "id": "toolu_1", "name": "store_profile_card", "input": {"entries": entries or []}}]
    )
    client.call = AsyncMock(return_value=response)
    return client


def _prompt(llm: AsyncMock, call: int = -1) -> str:
    return llm.call.await_args_list[call].args[0]["messages"][0]["content"][0]["text"]


def _line(fact: Fact, text: str | None = None) -> dict:
    """An LLM entry citing ``fact`` by the 8-character id the prompt shows."""
    return {"text": text or f"The user {fact.content}", "fact_ids": [str(fact.id)[:8]]}


async def _facts(session, agent: str, *specs: dict) -> list[Fact]:
    rows = []
    for i, spec in enumerate(specs):
        fields = {
            "category": "preference",
            "subject": "user",
            "source": "user_direct",
            "confidence": 0.9,
            "learned_at": NOW - timedelta(minutes=i),
            **spec,
        }
        fields.setdefault("content", f"likes thing number {i}")
        rows.append(Fact(id=uuid.uuid4(), agent_id=agent, **fields))
    session.add_all(rows)
    await session.flush()
    return rows


def _writer(db, agent: str, llm: AsyncMock, **overrides) -> ProfileCardWriter:
    return ProfileCardWriter(db, _settings(agent, **overrides), llm)


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_an_unchanged_source_set_makes_no_llm_call_even_after_a_recall_touch(db, session):
    agent = _agent()
    f1, f2 = await _facts(session, agent, {"content": "prefers Celsius"}, {"content": "lives in Lisbon"})
    llm = _llm([_line(f1), _line(f2)])
    writer = _writer(db, agent, llm)

    first = await writer.run_sleep(session=session)
    assert first["outcome"] == "written" and llm.call.await_count == 1

    # track_access bumps recall_count and, through the trigger, updated_at: not a source change.
    await session.execute(
        update(Fact).where(Fact.id == f1.id).values(recall_count=5, last_recalled_at=NOW, confirmation_count=3)
    )
    second = await writer.run_sleep(session=session)
    assert second["outcome"] == "unchanged" and second["rewritten"] == 0
    assert llm.call.await_count == 1


@pytest.mark.postgres_only
async def test_a_rebuild_calls_the_model_even_when_the_source_set_is_unchanged(db, session):
    agent = _agent()
    (f1,) = await _facts(session, agent, {"content": "prefers Celsius"})
    llm = _llm([_line(f1, "The user prefers Celsius.")])
    writer = _writer(db, agent, llm)
    await writer.run_sleep(session=session)

    llm.call.return_value.content[0]["input"] = {"entries": [_line(f1, "The user uses Celsius.")]}
    result = await writer.rebuild(session=session)
    assert result["outcome"] == "written" and llm.call.await_count == 2
    assert (result["added"], result["removed"]) == (["The user uses Celsius."], ["The user prefers Celsius."])


@pytest.mark.postgres_only
async def test_a_written_card_records_its_provenance_and_keeps_the_previous_entries(db, session):
    agent = _agent()
    f1, f2 = await _facts(session, agent, {"content": "prefers Celsius"}, {"content": "lives in Lisbon"})
    llm = _llm([_line(f1, "The user prefers Celsius."), _line(f2, "The user lives in Lisbon.")])
    writer = _writer(db, agent, llm)
    await writer.run_sleep(session=session)

    card = await ProfileCardStore(db, agent).get(session=session)
    assert [(e["text"], e["fact_ids"]) for e in card["entries"]] == [
        ("The user prefers Celsius.", [str(f1.id)]),
        ("The user lives in Lisbon.", [str(f2.id)]),
    ]
    assert all(e["id"] for e in card["entries"]) and card["source_hash"] and card["tokens"] > 0

    await session.execute(update(Fact).where(Fact.id == f2.id).values(content="lives in Porto"))
    llm.call.return_value.content[0]["input"] = {"entries": [_line(f1, "The user prefers Celsius.")]}
    result = await writer.run_sleep(session=session)
    assert result["outcome"] == "written"
    card = await ProfileCardStore(db, agent).get(session=session)
    assert [e["text"] for e in card["entries"]] == ["The user prefers Celsius."]
    assert [e["text"] for e in card["previous_entries"]] == ["The user prefers Celsius.", "The user lives in Lisbon."]


@pytest.mark.postgres_only
async def test_an_entry_citing_an_id_outside_the_source_set_is_dropped(db, session):
    agent = _agent()
    f1, f2, f3 = await _facts(session, agent, {}, {}, {})
    llm = _llm([_line(f1), _line(f2), {"text": "The user owns a yacht.", "fact_ids": [str(uuid.uuid4())]}, _line(f3)])
    result = await _writer(db, agent, llm).run_sleep(session=session)

    assert result["outcome"] == "written" and result["dropped"] == 1
    card = await ProfileCardStore(db, agent).get(session=session)
    assert "The user owns a yacht." not in [e["text"] for e in card["entries"]]
    assert len(card["entries"]) == 3


@pytest.mark.postgres_only
@pytest.mark.parametrize(
    "reply",
    ["majority_dropped", "no_tool_call", "raises", "empty"],
)
async def test_a_bad_reply_keeps_the_old_card(db, session, reply):
    agent = _agent()
    f1, f2, f3 = await _facts(session, agent, {"content": "prefers Celsius"}, {}, {})
    good = _llm([_line(f1, "The user prefers Celsius.")])
    await _writer(db, agent, good).run_sleep(session=session)
    before = await ProfileCardStore(db, agent).get(session=session)

    await session.execute(update(Fact).where(Fact.id == f2.id).values(content="changed so the hash moves"))
    bogus = {"text": "invented", "fact_ids": [str(uuid.uuid4())]}
    llm = {
        "majority_dropped": lambda: _llm([_line(f1), bogus, dict(bogus, text="also invented")]),
        "no_tool_call": lambda: _llm(no_tool=True),
        "raises": lambda: _llm(raises=RuntimeError("API error (500)")),
        "empty": lambda: _llm([]),
    }[reply]()
    result = await _writer(db, agent, llm).run_sleep(session=session)

    assert llm.call.await_count == 1
    assert result["outcome"] == "failed_closed" and result["rewritten"] == 0
    after = await ProfileCardStore(db, agent).get(session=session)
    assert after["entries"] == before["entries"] and after["source_hash"] == before["source_hash"]


@pytest.mark.postgres_only
async def test_an_empty_source_set_writes_no_card_and_makes_no_call(db, session):
    agent = _agent()
    await _facts(session, agent, {"category": "technical"}, {"active": False})
    llm = _llm([])
    result = await _writer(db, agent, llm).run_sleep(session=session)

    assert result["outcome"] == "empty_source" and llm.call.await_count == 0
    assert await ProfileCardStore(db, agent).get(session=session) is None


@pytest.mark.postgres_only
async def test_a_pinned_fact_outside_the_source_limit_still_gets_a_verbatim_line(db, session):
    agent = _agent()
    top1, top2, _top3, pinned = await _facts(
        session,
        agent,
        {"confidence": 1.0},
        {"confidence": 1.0},
        {"confidence": 1.0},
        {"confidence": 0.1, "content": "is vegetarian", "tags": ["profile_core"]},
    )
    llm = _llm([_line(top1), _line(top2)])
    result = await _writer(db, agent, llm, profile_card_source_limit=2).run_sleep(session=session)

    assert str(pinned.id)[:8] in _prompt(llm)
    assert result["pinned_added"] == 1
    card = await ProfileCardStore(db, agent).get(session=session)
    assert {"text": "is vegetarian", "fact_ids": [str(pinned.id)]}.items() <= card["entries"][-1].items()


@pytest.mark.postgres_only
async def test_a_reflected_rule_is_not_a_source_but_a_reflected_preference_is(db, session):
    agent = _agent()
    rule, pref = await _facts(
        session,
        agent,
        {"category": "rule", "source": "sleep_reflection", "content": "always run the tests"},
        {"category": "preference", "source": "sleep_reflection", "content": "prefers short replies"},
    )
    llm = _llm([_line(rule), _line(pref)])
    await _writer(db, agent, llm).run_sleep(session=session)

    prompt = _prompt(llm)
    assert str(pref.id)[:8] in prompt and "source: sleep_reflection" in prompt
    assert str(rule.id)[:8] not in prompt
    card = await ProfileCardStore(db, agent).get(session=session)
    assert [e["fact_ids"] for e in card["entries"]] == [[str(pref.id)]]


@pytest.mark.postgres_only
async def test_the_size_bound_drops_whole_lines_from_the_tail(db, session):
    agent = _agent()
    facts = await _facts(session, agent, *({} for _ in range(5)))
    texts = [f"Line {i} " + "x" * 50 for i in range(5)]
    llm = _llm([_line(f, t) for f, t in zip(facts, texts, strict=True)])
    # 59 characters a rendered line: two lines are 119 characters (29 tokens), three are 179 (44).
    result = await _writer(db, agent, llm, profile_card_max_tokens=30).run_sleep(session=session)

    card = await ProfileCardStore(db, agent).get(session=session)
    assert [e["text"] for e in card["entries"]] == texts[:2]
    assert card["tokens"] <= 30 and result["trimmed"] == 3

    agent2 = _agent()
    facts2 = await _facts(session, agent2, *({} for _ in range(5)))
    llm2 = _llm([_line(f, f"Short line {i}") for i, f in enumerate(facts2)])
    await _writer(db, agent2, llm2, profile_card_max_entries=3).run_sleep(session=session)
    card2 = await ProfileCardStore(db, agent2).get(session=session)
    assert [e["text"] for e in card2["entries"]] == ["Short line 0", "Short line 1", "Short line 2"]


@pytest.mark.postgres_only
async def test_trimming_drops_unpinned_lines_before_a_pinned_one(db, session):
    agent = _agent()
    f1, f2, pinned = await _facts(session, agent, {}, {}, {"tags": ["profile_core"]})
    llm = _llm([_line(f1, "First"), _line(f2, "Second"), _line(pinned, "Pinned")])
    await _writer(db, agent, llm, profile_card_max_entries=2).run_sleep(session=session)

    card = await ProfileCardStore(db, agent).get(session=session)
    assert [e["text"] for e in card["entries"]] == ["First", "Pinned"]


@pytest.mark.postgres_only
async def test_a_rejected_line_is_not_written_again(db, session):
    agent = _agent()
    f1, f2 = await _facts(session, agent, {"content": "prefers Celsius"}, {"content": "lives in Lisbon"})
    entries = [_line(f1, "The user prefers Celsius."), _line(f2, "The user lives in Lisbon.")]
    llm = _llm(entries)
    writer = _writer(db, agent, llm)
    await writer.run_sleep(session=session)
    store = ProfileCardStore(db, agent)
    lisbon = next(e for e in (await store.get(session=session))["entries"] if "Lisbon" in e["text"])
    assert await store.delete_entry(lisbon["id"], session=session) is True

    # A source change forces a rewrite; the model repeats the deleted line with a valid citation.
    await session.execute(update(Fact).where(Fact.id == f1.id).values(content="prefers Celsius, always"))
    result = await writer.run_sleep(session=session)

    assert "The user lives in Lisbon." in _prompt(llm)
    assert result["outcome"] == "written" and result["rejected_dropped"] == 1
    card = await store.get(session=session)
    assert [e["text"] for e in card["entries"]] == ["The user prefers Celsius."]
    assert card["rejected"] == ["The user lives in Lisbon."]


@pytest.mark.postgres_only
async def test_an_interrupted_cycle_makes_no_llm_call(db, session):
    agent = _agent()
    await _facts(session, agent, {})
    llm = _llm([])
    result = await _writer(db, agent, llm).run_sleep(interrupted=lambda: True, session=session)

    assert result["outcome"] == "interrupted" and llm.call.await_count == 0
    assert await ProfileCardStore(db, agent).get(session=session) is None


@pytest.mark.postgres_only
async def test_the_capacity_rule_is_in_the_prompt_only_at_ninety_percent(db, session):
    agent = _agent()
    f1, f2 = await _facts(session, agent, {}, {})
    # "- " + 70 characters = 72 characters, 18 tokens: 90% of 20.
    llm = _llm([_line(f1, "y" * 70)])
    writer = _writer(db, agent, llm, profile_card_max_tokens=20)
    await writer.run_sleep(session=session)
    assert "under 70%" not in _prompt(llm, 0)

    await session.execute(update(Fact).where(Fact.id == f2.id).values(content="changed"))
    await writer.run_sleep(session=session)
    assert "under 70%" in _prompt(llm, 1)


# ---------------------------------------------------------------------------
# Sleep phase
# ---------------------------------------------------------------------------


def _handler(**settings_overrides):
    from tests.test_sleep_handler import _make_sleep_handler, _mock_settings

    return _make_sleep_handler(settings=_mock_settings(**settings_overrides))


def _sleep_event():
    from tests.test_sleep_handler import _make_event

    return _make_event("sleep_started")


_ALL_PHASES = (
    "review_decisions",
    "prune",
    "compress",
    "reflect",
    "resolve_contradictions",
    "sweep_key_conflicts",
    "stale_scan",
    "cluster_consolidation",
    "recover_abandoned_episodes",
    "graph_densification",
    "relink_open_episodes",
    "stc_consolidation",
    "prune_dead_edges",
    "prune_hub_snapshots",
    "generalize",
    "evolve_rubric",
)


async def test_no_writer_is_a_no_op_true():
    handler, *_ = _handler()
    assert handler._profile_card_writer is None
    stats = {"facts_created": 0}
    assert await handler._phase_profile_card(stats) is True and stats == {"facts_created": 0}


async def test_the_phase_counts_a_rewrite_at_top_level_and_nests_the_rest():
    handler, *_ = _handler()
    handler._profile_card_writer = AsyncMock()
    handler._profile_card_writer.run_sleep = AsyncMock(
        return_value={"outcome": "written", "rewritten": 1, "sources": 4, "dropped": 1, "entries": 3, "tokens": 40}
    )
    stats = {"facts_created": 0}
    assert await handler._phase_profile_card(stats) is True
    assert stats["profile_card_rewritten"] == 1
    assert stats["profile_card"]["outcome"] == "written" and stats["profile_card"]["dropped"] == 1
    interrupted = handler._profile_card_writer.run_sleep.await_args.kwargs["interrupted"]
    assert interrupted() is False
    handler._interrupted = True
    assert interrupted() is True


async def test_a_raising_writer_returns_false():
    handler, *_ = _handler()
    handler._profile_card_writer = AsyncMock()
    handler._profile_card_writer.run_sleep = AsyncMock(side_effect=RuntimeError("boom"))
    stats = {"facts_created": 0}
    assert await handler._phase_profile_card(stats) is False and "profile_card" not in stats


@pytest.mark.parametrize("configured", [True, False])
async def test_the_phase_runs_after_the_stale_scan_and_before_clustering_only_when_configured(configured):
    handler, _brain, _heart, bus, _ = _handler()
    for name in _ALL_PHASES:
        setattr(handler, f"_phase_{name}", AsyncMock(return_value=True))
    if configured:
        handler._profile_card_writer = AsyncMock()
        handler._profile_card_writer.run_sleep = AsyncMock(return_value={"outcome": "unchanged", "rewritten": 0})
    await handler._run_sleep(_sleep_event())
    done = bus.emit.call_args[0][0].data["phases_completed"]
    assert ("profile_card" in done) is configured
    if configured:
        assert done.index("profile_card") == done.index("stale_scan") + 1
        assert done.index("cluster_consolidation") == done.index("profile_card") + 1


async def test_a_rewrite_is_one_audited_action():
    handler, _brain, _heart, bus, _ = _handler(consolidation_audit_enabled=True)
    for name in _ALL_PHASES:
        setattr(handler, f"_phase_{name}", AsyncMock(return_value=True))
    handler._phase_prune_consolidation_actions = AsyncMock()
    handler._profile_card_writer = AsyncMock()
    handler._profile_card_writer.run_sleep = AsyncMock(return_value={"outcome": "written", "rewritten": 1})
    auditor = MagicMock()
    auditor.open = AsyncMock()
    auditor.close = AsyncMock()
    with patch("nous.handlers.sleep_handler.ConsolidationAuditor", return_value=auditor):
        await handler._run_sleep(_sleep_event())
    calls = [c for c in auditor.record.call_args_list if c.args[0] == "profile_card"]
    assert len(calls) == 1
    assert calls[0].args[1] == "rewrite" and calls[0].kwargs["after"] == {"counts": {"profile_card_rewritten": 1}}


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


def _app(db, agent: str, sleep_handler=None):
    from nous.api.rest import create_app

    runner = MagicMock()
    runner._conversations = {}
    return create_app(
        runner=runner,
        brain=AsyncMock(),
        heart=AsyncMock(),
        cognitive=AsyncMock(),
        database=db,
        settings=_settings(agent),
        sleep_handler=sleep_handler,
    )


async def _call(app, method: str, path: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path)


async def _drop_card(db, agent: str) -> None:
    async with db.session() as s:
        await s.execute(delete(ProfileCard).where(ProfileCard.agent_id == agent))
        await s.commit()


@pytest.mark.postgres_only
async def test_get_without_a_card_is_200_with_no_entries(db):
    resp = await _call(_app(db, _agent()), "GET", "/profile-card")
    assert resp.status_code == 200
    assert resp.json()["entries"] == [] and resp.json()["tokens"] == 0


@pytest.mark.postgres_only
async def test_delete_moves_the_line_to_rejected_and_an_unknown_id_is_404(db):
    agent = _agent()
    fid = str(uuid.uuid4())
    store = ProfileCardStore(db, agent)
    await store.write([{"id": "e1", "text": "The user prefers Celsius.", "fact_ids": [fid]}], "h")
    try:
        app = _app(db, agent)
        assert (await _call(app, "DELETE", "/profile-card/entries/nope")).status_code == 404
        resp = await _call(app, "DELETE", "/profile-card/entries/e1")
        assert resp.status_code == 200
        body = (await _call(app, "GET", "/profile-card")).json()
        assert body["entries"] == [] and body["rejected"] == ["The user prefers Celsius."]
        assert (await _call(app, "DELETE", "/profile-card/entries/e1")).status_code == 404
    finally:
        await _drop_card(db, agent)


@pytest.mark.postgres_only
async def test_delete_without_a_card_is_404(db):
    assert (await _call(_app(db, _agent()), "DELETE", "/profile-card/entries/e1")).status_code == 404


async def test_rebuild_is_409_with_the_writer_off(db):
    for sleep_handler in (None, MagicMock(is_sleeping=False, _profile_card_writer=None)):
        resp = await _call(_app(db, _agent(), sleep_handler), "POST", "/profile-card/rebuild")
        assert resp.status_code == 409


async def test_rebuild_is_409_while_sleeping_or_while_the_writer_runs(db):
    writer = MagicMock(busy=False)
    writer.rebuild = AsyncMock(return_value={"outcome": "written"})
    sleeping = MagicMock(is_sleeping=True, _profile_card_writer=writer)
    assert (await _call(_app(db, _agent(), sleeping), "POST", "/profile-card/rebuild")).status_code == 409
    busy = MagicMock(is_sleeping=False, _profile_card_writer=MagicMock(busy=True))
    assert (await _call(_app(db, _agent(), busy), "POST", "/profile-card/rebuild")).status_code == 409
    writer.rebuild.assert_not_awaited()

    awake = MagicMock(is_sleeping=False, _profile_card_writer=writer)
    resp = await _call(_app(db, _agent(), awake), "POST", "/profile-card/rebuild")
    assert resp.status_code == 200 and resp.json()["outcome"] == "written"
    writer.rebuild.assert_awaited_once()
