"""K-6: recall_deep can show the dated sources a merged fact was built from.

A sleep merge (F027 cluster merge, F031 MERGE) deactivates its sources and
points their ``superseded_by`` at the merged fact. Behind
``recall_merge_history_enabled`` (default off) the recall pipeline looks those
sources up for the facts it returns and ``recall_deep`` lists them, dated by
``learned_at``, under the merged fact. Non-merge supersession is never shown:
its loser is a stale belief, not a detail of the current one.
"""

from __future__ import annotations

import functools
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from test_retrieval_pipeline import _make_brain, _make_heart, _make_settings  # noqa: E402

from nous.api.retrieval_pipeline import PipelineResult, PipelineStats, failure_note, run_recall_pipeline
from nous.api.tools import _format_pipeline_text, create_nous_tools
from nous.heart.facts import FactManager
from nous.heart.schemas import FactInput, RecallResult
from nous.storage.migrator import _split_sql_statements
from nous.storage.models import Event, Fact

MIGRATION = Path(__file__).resolve().parents[1] / "sql" / "migrations" / "098_fact_merged_reason_backfill.sql"

# ---------------------------------------------------------------------------
# FactManager.get_merge_history (real Postgres)
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


async def _fact(session, agent_id: str, content: str, **cols) -> Fact:
    row = Fact(agent_id=agent_id, content=content, **cols)
    session.add(row)
    await session.flush()
    return row


async def _merged(session, agent_id: str, source: str = "cluster_consolidation") -> Fact:
    return await _fact(session, agent_id, "Project Larch: launch and budget details", source=source)


def _f027(merged: Fact | None, members: list[Fact], outcome: str = "merged", learned: Fact | None = None) -> dict:
    """An f027_cluster_merge event body as sleep_handler writes it."""
    return {
        "source_fact_ids": [str(f.id) for f in members],
        "source_count": len(members),
        "merged_fact_id": str(merged.id) if merged else None,
        "learned_fact_id": str(learned.id) if learned else None,
        "outcome": outcome,
    }


def _f031(action: str, fact1: Fact, fact2: Fact) -> dict:
    """An f031_contradiction_resolution event body (no merged-fact id)."""
    return {"raw_action": action, "applied_action": action, "fact1_id": str(fact1.id), "fact2_id": str(fact2.id)}


async def _source(session, agent_id: str, merged: Fact, content: str, days: int, reason: str | None = "merged") -> Fact:
    return await _fact(
        session,
        agent_id,
        content,
        active=False,
        superseded_by=merged.id,
        deactivation_reason=reason,
        learned_at=_T0 + timedelta(days=days),
    )


@pytest.mark.postgres_only
class TestGetMergeHistory:
    async def test_returns_the_merged_sources_newest_first_with_dates(self, heart, session):
        a = heart.agent_id
        m = await _merged(session, a)
        await _source(session, a, m, "Larch launch moved to 14 March, code ZX-41", days=13)
        await _source(session, a, m, "Larch budget is 41,500 EUR", days=1)

        out = await heart.facts.get_merge_history([m.id], session=session)

        assert out == {
            m.id: {
                "sources": [
                    {"content": "Larch launch moved to 14 March, code ZX-41", "learned_at": "2026-03-14"},
                    {"content": "Larch budget is 41,500 EUR", "learned_at": "2026-03-02"},
                ],
                "dropped": 0,
            }
        }

    async def test_non_merge_supersession_is_never_history(self, heart, session):
        a = heart.agent_id
        m = await _merged(session, a)
        await _source(session, a, m, "Larch budget is 41,500 EUR", days=1)
        # A merged fact that later wins a SUPERSEDE: the loser is a stale belief.
        await _source(session, a, m, "Larch budget is 30,000 EUR", days=2, reason="superseded")
        plain = await _fact(session, a, "Larch owner is Dana", source="conversation")
        await _source(session, a, plain, "Larch owner is Sam", days=3, reason="superseded")

        out = await heart.facts.get_merge_history([m.id, plain.id], session=session)

        assert set(out) == {m.id}
        assert [s["content"] for s in out[m.id]["sources"]] == ["Larch budget is 41,500 EUR"]

    async def test_only_the_stamp_counts_at_read_time(self, heart, session):
        """An unstamped row is not history even when an event names it: the
        read path never consults events (migration 098 stamps old merges)."""
        a = heart.agent_id
        m = await _merged(session, a)
        member = await _source(session, a, m, "Larch kickoff on 2 Feb in Lyon", days=0, reason=None)
        session.add(Event(agent_id=a, event_type="f027_cluster_merge", data=_f027(m, [member])))
        await session.flush()

        assert await heart.facts.get_merge_history([m.id], session=session) == {}

    async def test_a_real_supersede_never_counts_as_a_merge(self, heart, session):
        """Through the real supersede paths: if one ever stamped 'merged', its
        loser (a stale belief) would show up as history."""
        a = heart.agent_id
        m = await _merged(session, a)
        await _source(session, a, m, "Larch budget is 41,500 EUR", days=1)
        # The merged fact later wins a SUPERSEDE (the sleep F031 path).
        loser = await _fact(session, a, "Larch budget is 30,000 EUR", source="conversation")
        assert await heart.facts.apply_supersession(m.id, loser.id, session)
        # A plain replacement (FactManager.supersede).
        old = await heart.learn(FactInput(content="The Larch project is owned by Sam Okafor."), session=session)
        new = await heart.supersede_fact(
            old.id, FactInput(content="The Larch project is owned by Dana Whitfield."), session=session
        )

        out = await heart.facts.get_merge_history([m.id, new.id], session=session)

        assert set(out) == {m.id}
        assert [s["content"] for s in out[m.id]["sources"]] == ["Larch budget is 41,500 EUR"]
        assert {
            r.deactivation_reason for r in (await session.get(Fact, loser.id), await session.get(Fact, old.id))
        } == {"superseded"}

    async def test_caps_per_fact_counts_the_dropped_and_trims_content(self, heart, session):
        a = heart.agent_id
        m = await _merged(session, a)
        for day in range(5):
            await _source(session, a, m, f"Larch detail {day}: " + "x" * 400, days=day)

        out = await heart.facts.get_merge_history([m.id], per_fact=2, max_chars=40, session=session)

        hist = out[m.id]
        assert hist["dropped"] == 3
        assert [s["learned_at"] for s in hist["sources"]] == ["2026-03-05", "2026-03-04"]
        assert all(len(s["content"]) <= 40 for s in hist["sources"])
        assert hist["sources"][0]["content"].startswith("Larch detail 4: xxx")

    async def test_no_ids_no_query(self, heart):
        assert await heart.facts.get_merge_history([]) == {}


# ---------------------------------------------------------------------------
# Pipeline: attach after ranking, top 3 by rank, flag-gated
# ---------------------------------------------------------------------------


def _fact_results(n: int) -> list[RecallResult]:
    return [RecallResult(type="fact", id=uuid4(), summary=f"merged fact {i}", score=0.9 - i * 0.1) for i in range(n)]


def _hist(detail: str, learned_at: str = "2026-03-14", dropped: int = 0) -> dict:
    return {"sources": [{"content": detail, "learned_at": learned_at}], "dropped": dropped}


def _settings(enabled: bool | None, **kw):
    s = _make_settings(graph_recall_enabled=False, **kw)
    if enabled is not None:
        s.recall_merge_history_enabled = enabled
        s.recall_merge_history_max = 6
    return s


def _heart_with_history(recall: list[RecallResult], history: dict[UUID, dict]):
    heart = _make_heart(recall_results=recall)
    heart.facts.get_merge_history = AsyncMock(return_value=history)
    return heart


def _brain():
    return _make_brain(neighbors_by_node={}, contradictions=[], decision_results=[])


async def _run(heart, settings):
    return await run_recall_pipeline(query="larch", heart=heart, brain=_brain(), settings=settings, limit=10)


class TestPipelineAttach:
    async def test_flag_off_never_looks_up(self):
        recall = _fact_results(2)
        heart = _heart_with_history(recall, {r.id: _hist("d") for r in recall})

        for settings in (_settings(None), _settings(False)):
            results, _ = await _run(heart, settings)
            assert all("merge_history" not in r.metadata for r in results)
        heart.facts.get_merge_history.assert_not_awaited()

    async def test_top_three_by_rank_and_ranking_unchanged(self):
        recall = _fact_results(6)
        # Rank 2 has no history: it takes no slot.
        history = {r.id: _hist(f"detail {i}") for i, r in enumerate(recall) if i != 1}
        off, _ = await _run(_heart_with_history(recall, history), _settings(False))
        heart = _heart_with_history(recall, history)

        on, _ = await _run(heart, _settings(True))

        assert [(r.id, r.type, r.score) for r in on] == [(r.id, r.type, r.score) for r in off]
        with_history = [r.id for r in on if "merge_history" in r.metadata]
        assert with_history == [recall[0].id, recall[2].id, recall[3].id]
        assert on[0].metadata["merge_history"] == history[recall[0].id]
        heart.facts.get_merge_history.assert_awaited_once()
        (ids,), kwargs = heart.facts.get_merge_history.await_args
        assert list(ids) == [r.id for r in recall], "every fact id in one round trip"
        assert kwargs["per_fact"] == 6

    async def test_lookup_failure_keeps_results_and_is_traced_but_not_noted(self):
        recall = _fact_results(2)
        heart = _heart_with_history(recall, {})
        heart.facts.get_merge_history = AsyncMock(side_effect=RuntimeError("db gone"))

        results, stats = await _run(heart, _settings(True))

        assert [r.id for r in results] == [r.id for r in recall]
        assert all("merge_history" not in r.metadata for r in results)
        assert stats.n_stage_errors.get("merge_history") == 1
        assert failure_note(stats) is None


# ---------------------------------------------------------------------------
# Render: recall_deep text
# ---------------------------------------------------------------------------


async def _recall_deep_text(heart, settings) -> str:
    tools = create_nous_tools(brain=_brain(), heart=heart, settings=settings)
    result = await tools["recall_deep"](query="larch", limit=5)
    return result["content"][0]["text"]


class TestRecallDeepRender:
    async def test_flag_off_text_is_byte_identical(self):
        recall = _fact_results(2)
        history = {r.id: _hist("Larch launch moved to 14 March, code ZX-41") for r in recall}

        before = await _recall_deep_text(_heart_with_history(recall, history), _settings(None))
        off = await _recall_deep_text(_heart_with_history(recall, history), _settings(False))
        on_but_none = await _recall_deep_text(_heart_with_history(recall, {}), _settings(True))

        assert off == before
        assert on_but_none == before
        assert "ZX-41" not in before

    async def test_flag_on_lists_the_dated_sources_under_the_merged_fact(self):
        recall = _fact_results(2)
        history = {
            recall[0].id: {
                "sources": [
                    {"content": "Larch launch moved to 14 March, code ZX-41", "learned_at": "2026-03-14"},
                    {"content": "Larch budget is 41,500 EUR", "learned_at": "2026-03-02"},
                ],
                "dropped": 2,
            }
        }

        text = await _recall_deep_text(_heart_with_history(recall, history), _settings(True))

        lines = text.splitlines()
        i = next(n for n, line in enumerate(lines) if "merged fact 0" in line)
        assert lines[i + 1 : i + 5] == [
            "   history (merged from 4 facts, newest first):",
            "   - [learned 2026-03-14] Larch launch moved to 14 March, code ZX-41",
            "   - [learned 2026-03-02] Larch budget is 41,500 EUR",
            "   - +2 older",
        ]
        assert lines[i + 5].startswith("2. [fact] merged fact 1")

    @pytest.mark.postgres_only
    async def test_real_merge_reaches_recall_deep(self, heart, session):
        """The real lookup, the real pipeline and the real formatter together."""
        a = heart.agent_id
        m = await _merged(session, a)
        await _source(session, a, m, "Larch launch moved to 14 March, code ZX-41", days=13)
        heart.recall = AsyncMock(return_value=[RecallResult(type="fact", id=m.id, summary=m.content, score=0.9)])
        heart.facts.get_merge_history = functools.partial(heart.facts.get_merge_history, session=session)

        text = await _recall_deep_text(heart, _settings(True))

        assert "   - [learned 2026-03-14] Larch launch moved to 14 March, code ZX-41" in text


class TestFormatterSites:
    """Every Heart Memory emission site renders the block: flat, a session
    bucket, and the no-session tail. Graph-memory neighbours are interleaved
    into the same list, so these are its sites too."""

    def _rows(self) -> list[PipelineResult]:
        hist = _hist("Larch kickoff on 2 Feb in Lyon", learned_at="2026-02-02")
        return [
            PipelineResult(
                id=uuid4(),
                type="fact",
                description="in a session",
                score=0.9,
                metadata={"source_episode_id": str(uuid4()), "merge_history": hist},
            ),
            PipelineResult(
                id=uuid4(),
                type="fact",
                description="graph neighbour",
                score=0.8,
                source="graph_expanded",
                edge_relation="co_occurs",
                metadata={"stage_origin": "heart_graph_memory", "merge_history": hist},
            ),
        ]

    @pytest.mark.parametrize("grouped", [False, True])
    def test_each_site_renders_history(self, grouped):
        text = _format_pipeline_text(self._rows(), PipelineStats(), ["all"], session_group_heart=grouped)

        assert text.count("   - [learned 2026-02-02] Larch kickoff on 2 Feb in Lyon") == 2

    def test_history_lines_are_not_collected_as_emitted(self):
        rows = self._rows()
        emitted: list = []

        _format_pipeline_text(rows, PipelineStats(), ["all"], emitted_out=emitted)

        assert emitted == [(r.id, r.type) for r in rows]


# ---------------------------------------------------------------------------
# Migration 098: stamp 'merged' on the sources merged before 094
# ---------------------------------------------------------------------------


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return "\n".join(ln for ln in _sql().splitlines() if not ln.lstrip().startswith("--"))


class TestMigration098File:
    def test_two_stamps_without_the_updated_at_trigger_then_the_index(self):
        statements = _split_sql_statements(_sql())
        assert len(statements) == 5
        assert statements[0] == "ALTER TABLE heart.facts DISABLE TRIGGER set_updated_at"
        for stamp in statements[1:3]:
            assert stamp.startswith("UPDATE heart.facts AS s")
            assert "SET deactivation_reason = 'merged'" in stamp
            assert "s.deactivation_reason IS NULL" in stamp and "s.active = false" in stamp
        assert statements[3] == "ALTER TABLE heart.facts ENABLE TRIGGER set_updated_at"
        assert statements[4].startswith("CREATE INDEX IF NOT EXISTS idx_facts_merged_sources")
        assert statements[4].endswith("WHERE deactivation_reason = 'merged'")

    def test_nothing_the_splitter_would_mangle_and_no_grants(self):
        sql = _sql()
        assert "/*" not in sql and "$$" not in sql
        assert not re.search(r"(?<!:):[A-Za-z_]", _code())  # text() reads :name as a bind parameter
        assert all(";" not in ln for ln in sql.splitlines() if ln.lstrip().startswith("--"))
        code = _code().upper()
        for word in ("GRANT", "REVOKE", "OWNER", "ROLE", "AUTHORIZATION"):
            assert not re.search(rf"\b{word}\b", code), word

    def test_the_eval_preflight_probes_the_index(self):
        from nous_eval.schema_preflight import migration_artifacts

        assert migration_artifacts(_sql()) == [("index", "idx_facts_merged_sources")]


@pytest.mark.postgres_only
async def test_migration_098_stamps_exactly_the_merge_sources(heart, session):
    agent = f"test-098-{uuid4()}"
    edited = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)  # the rows' last real edit

    async def fact(content: str, gone_to: Fact | None = None, **cols) -> Fact:
        # gone_to: an inactive pre-094 row superseded by that fact, reason NULL.
        if gone_to is not None:
            cols.update(active=False, superseded_by=gone_to.id)
        return await _fact(session, agent, content, updated_at=edited, **cols)

    m27 = await fact("cluster merge product", source="cluster_consolidation")
    member = await fact("Larch kickoff on 2 Feb in Lyon", m27)
    refused = await fact("refused cluster, then a plain SUPERSEDE", m27)
    aborted = await fact("aborted cluster, then a plain SUPERSEDE", m27)
    foreign = await fact("named only by another agent's event", m27)
    m31 = await fact("contradiction merge product", source="contradiction_resolution")
    b1 = await fact("Larch venue is hall B", m31)
    b2 = await fact("Larch venue is hall C", m31)
    lone = await fact("MERGE aborted, then lost a SUPERSEDE", m31)
    other = await fact("lone's MERGE partner, still active")
    loser = await fact("SUPERSEDE_A loser", m31)
    plain_winner = await fact("plain winner")
    await fact("plain SUPERSEDE loser, no event", plain_winner)
    session.add_all(
        [
            Event(agent_id=agent, event_type="f027_cluster_merge", data=_f027(m27, [member])),
            Event(agent_id=agent, event_type="f027_cluster_merge", data=_f027(None, [refused], "llm_refused")),
            Event(
                agent_id=agent,
                event_type="f027_cluster_merge",
                data=_f027(None, [aborted], "merge_target_inactive", learned=m27),
            ),
            Event(agent_id=f"{agent}-other", event_type="f027_cluster_merge", data=_f027(m27, [foreign])),
            Event(agent_id=agent, event_type="f031_contradiction_resolution", data=_f031("MERGE", b1, b2)),
            Event(agent_id=agent, event_type="f031_contradiction_resolution", data=_f031("MERGE", lone, other)),
            Event(agent_id=agent, event_type="f031_contradiction_resolution", data=_f031("SUPERSEDE_A", loser, m31)),
        ]
    )
    await session.flush()

    async def state() -> dict[str, tuple]:
        res = await session.execute(
            text("SELECT content, deactivation_reason, updated_at FROM heart.facts WHERE agent_id = :a"),
            {"a": agent},
        )
        return {r.content: (r.deactivation_reason, r.updated_at) for r in res}

    async def scalar(sql: str):
        return (await session.execute(text(sql))).scalar_one()

    statements = _split_sql_statements(_sql())
    for statement in statements:
        await session.execute(text(statement))
    after = await state()

    # Exactly the three merge sources are stamped, every other row stays NULL.
    assert {c: reason for c, (reason, _) in after.items() if reason is not None} == {
        "Larch kickoff on 2 Feb in Lyon": "merged",
        "Larch venue is hall B": "merged",
        "Larch venue is hall C": "merged",
    }
    # The stamp is not an edit.
    assert {ua for _, ua in after.values()} == {edited}

    # A second run stamps nothing, anywhere.
    rowcounts = [(await session.execute(text(s))).rowcount for s in statements]
    assert rowcounts[1:3] == [0, 0]
    assert await state() == after

    # The trigger is back on, and the index exists.
    assert (
        await scalar(
            "SELECT tgenabled::text FROM pg_trigger "
            "WHERE tgrelid = 'heart.facts'::regclass AND tgname = 'set_updated_at'"
        )
        == "O"
    )
    assert (
        await scalar("SELECT to_regclass('heart.idx_facts_merged_sources')::text") == "heart.idx_facts_merged_sources"
    )

    # The read path now sees the backfilled merges.
    out = await FactManager(heart.db, None, agent).get_merge_history([m27.id, m31.id], session=session)
    assert {k: {s["content"] for s in v["sources"]} for k, v in out.items()} == {
        m27.id: {"Larch kickoff on 2 Feb in Lyon"},
        m31.id: {"Larch venue is hall B", "Larch venue is hall C"},
    }
