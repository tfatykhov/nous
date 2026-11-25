"""scripts/backfill_mentioned_at.py: each rule fills what the writer of its source stores now, and nothing else.

Legacy rows (written before every writer recorded ``mentioned_at``) are inserted by hand with a NULL said-time; rows
that already carry one are written through ``heart.learn``. Each test uses its own agent ids.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from conftest import MockEmbeddingProvider
from sqlalchemy import text

from nous.config import Settings
from nous.heart import Heart
from nous.heart.schemas import FactInput
from scripts import backfill_mentioned_at as bf

pytestmark = pytest.mark.postgres_only

DAY = datetime(2026, 3, 2, tzinfo=UTC)
SAID = datetime(2024, 5, 1, 14, 0, tzinfo=UTC)


def _at(hour: int, minute: int = 0, day: int = 0) -> datetime:
    return DAY + timedelta(days=day, hours=hour, minutes=minute)


async def _episode(db, agent: str, started: datetime, ended: datetime | None = None) -> UUID:
    async with db.session() as s:
        eid = await s.scalar(
            text(
                "INSERT INTO heart.episodes (agent_id, summary, started_at, ended_at) "
                "VALUES (:a, 's', :st, :en) RETURNING id"
            ),
            {"a": agent, "st": started, "en": ended},
        )
        await s.commit()
    return eid


async def _fact(
    db,
    agent: str,
    source: str | None,
    learned: datetime,
    *,
    mentioned: datetime | None = None,
    episode: UUID | None = None,
    category: str | None = "technical",
    superseded_by: UUID | None = None,
    reason: str | None = None,
    event_date: date | None = None,
    content: str | None = None,
) -> UUID:
    async with db.session() as s:
        fid = await s.scalar(
            text(
                "INSERT INTO heart.facts (agent_id, content, subject, category, source, learned_at, mentioned_at, "
                "source_episode_id, superseded_by, deactivation_reason, active, event_date, valid_from, valid_to) "
                "VALUES (:a, :c, 'alice', :cat, :src, :la, :ma, :ep, :sb, :r, :act, :ed, :ed, :ed) RETURNING id"
            ),
            {
                "a": agent,
                "c": content or f"fact {uuid4().hex[:8]}",
                "cat": category,
                "src": source,
                "la": learned,
                "ma": mentioned,
                "ep": episode,
                "sb": superseded_by,
                "r": reason,
                "act": superseded_by is None,
                "ed": event_date,
            },
        )
        await s.commit()
    return fid


async def _cluster_event(db, agent: str, merged: UUID, sources: list) -> None:
    data = {"merged_fact_id": str(merged), "source_fact_ids": [str(i) for i in sources], "outcome": "merged"}
    async with db.session() as s:
        await s.execute(
            text(
                "INSERT INTO nous_system.events (agent_id, event_type, data) "
                "VALUES (:a, 'f027_cluster_merge', CAST(:d AS jsonb))"
            ),
            {"a": agent, "d": json.dumps(data)},
        )
        await s.commit()


async def _rows(db, agent: str) -> dict[UUID, dict]:
    """Every column of the agent's facts, as stored."""
    async with db.session() as s:
        rows = await s.execute(
            text("SELECT id, to_jsonb(f)::text AS j FROM heart.facts f WHERE agent_id = :a"), {"a": agent}
        )
        return {r.id: json.loads(r.j) for r in rows}


async def _said(db, agent: str) -> dict[UUID, datetime | None]:
    async with db.session() as s:
        rows = await s.execute(text("SELECT id, mentioned_at FROM heart.facts WHERE agent_id = :a"), {"a": agent})
        return {r.id: r.mentioned_at for r in rows}


@pytest_asyncio.fixture
async def agents(db):
    made: list[str] = []

    def new(tag: str = "x") -> str:
        made.append(f"test-said-bf-{tag}-{uuid4().hex[:8]}")
        return made[-1]

    yield new
    async with db.session() as s:
        for agent in made:
            await s.execute(text("DELETE FROM nous_system.entity_profile WHERE agent_id = :a"), {"a": agent})
            await s.execute(text("DELETE FROM nous_system.events WHERE agent_id = :a"), {"a": agent})
            await s.execute(text("DELETE FROM heart.facts WHERE agent_id = :a"), {"a": agent})
            await s.execute(text("DELETE FROM heart.episodes WHERE agent_id = :a"), {"a": agent})
        await s.commit()


async def _scenario(db, agent: str, other: str) -> tuple[dict[str, UUID], dict[str, datetime | None]]:
    """One of every kind of legacy row, and the value each must end with (None: stays NULL)."""
    ep = await _episode(db, agent, _at(9), _at(11))
    ep_late_end = await _episode(db, agent, _at(9), _at(12))
    ep_open = await _episode(db, agent, _at(9))
    ep_other = await _episode(db, other, _at(8), _at(8, 30))
    ids: dict[str, UUID] = {}
    want: dict[str, datetime | None] = {}

    async def row(name: str, source: str | None, learned: datetime, expect: datetime | None, **kw) -> UUID:
        ids[name] = await _fact(db, agent, source, learned, **kw)
        want[name] = expect
        return ids[name]

    # a: inside the turn, learned_at
    await row("user_direct", "user_direct", _at(9, 5), _at(9, 5), episode=ep)
    await row("inline", "inline_correction", _at(10), _at(10), episode=ep, category="rule")
    await row("pre_prune", "pre_prune_extraction", _at(10, 10), _at(10, 10))
    await row("attachment", "telegram-attachment", _at(10, 20), _at(10, 20), category="attachment", episode=ep)
    await row("attachment_like", "telegram-attachment", _at(10, 25), None)  # not the attachment writer's row
    await row(
        "inactive_user_direct",
        "user_direct",
        _at(10, 30),
        _at(10, 30),
        reason="superseded",
        superseded_by=ids["user_direct"],
    )
    # b: the extractors, the episode start
    await row("fact_extractor", "fact_extractor", _at(11, 5), _at(9), episode=ep)
    await row("episode_summarizer", "episode_summarizer", _at(11, 5), _at(9), episode=ep)
    await row("enumerative", "enumerative_extractor", _at(11, 6), _at(9), episode=ep)
    await row("extractor_no_episode", "fact_extractor", _at(11, 7), None)
    await row("extractor_other_agents_episode", "episode_summarizer", _at(11, 8), None, episode=ep_other)
    # b: session reflection and correction extractor, the episode end, at most learned_at
    await row("reflection", "reflection", _at(11, 1), _at(11), episode=ep)
    await row("reflection_end_after_write", "reflection", _at(11, 30), _at(11, 30), episode=ep_late_end)
    await row("reflection_not_ended", "reflection", _at(11, 40), _at(11, 40), episode=ep_open)
    await row("reflection_no_episode", "reflection", _at(11, 45), _at(11, 45))
    await row("reflection_other_agents_episode", "reflection", _at(11, 50), _at(11, 50), episode=ep_other)
    await row("correction", "correction_extraction", _at(11, 2), _at(11, 2), category="rule")
    # c: the knowledge extractor, learned_at
    await row("knowledge", "knowledge_extractor", _at(13), _at(13))
    # e: no truthful source
    for source in (
        "sleep_reflection",
        "supersede",
        "a2ui_review",
        "f024_phase3b",
        "mcp",
        "mcp:notes",
        "legacy_source",
        None,
    ):
        await row(f"kept_{source}", source, _at(14), None)
    await row("exemplar", "exemplar_extractor", _at(11, 3), None, episode=ep, category="exemplar")
    # d: a cluster merge, sources named by its event; one source fills from its episode first
    cluster = await row("cluster", "cluster_consolidation", _at(3, day=1), _at(9, 5))
    s1 = await _fact(db, agent, "user_direct", _at(9, 5), superseded_by=cluster, reason="merged")
    s2 = await _fact(db, agent, "episode_summarizer", _at(11, 5), episode=ep, superseded_by=cluster, reason="merged")
    await _cluster_event(db, agent, cluster, [s1, s2])
    ids["cluster_source_user"], want["cluster_source_user"] = s1, _at(9, 5)
    ids["cluster_source_summary"], want["cluster_source_summary"] = s2, _at(9)
    # d: a contradiction merge, the two sources stamped 'merged'; a later loser pointing at it is not a source
    contra = await row("contradiction", "contradiction_resolution", _at(3, 30, day=1), _at(13, 15))
    k1 = await _fact(db, agent, "user_direct", _at(10, 40), superseded_by=contra, reason="merged")
    k2 = await _fact(db, agent, "knowledge_extractor", _at(13, 15), superseded_by=contra, reason="merged")
    k3 = await _fact(db, agent, "user_direct", _at(20, day=1), superseded_by=contra, reason="superseded")
    for name, fid, value in (
        ("contra_k1", k1, _at(10, 40)),
        ("contra_k2", k2, _at(13, 15)),
        ("contra_loser", k3, _at(20, day=1)),
    ):
        ids[name], want[name] = fid, value
    return ids, want


async def _learned_through_heart(db, agent: str) -> tuple[UUID, UUID]:
    """A row that has a said-time and a legacy learn_fact row without one, both through heart.learn."""
    settings = Settings().model_copy(update={"agent_id": agent, "admission_control_enabled": False})
    heart = Heart(db, settings, embedding_provider=MockEmbeddingProvider())
    try:
        valued = await heart.learn(
            FactInput(content="Alice keeps her boat at the north pier.", source="user_direct", mentioned_at=SAID)
        )
        legacy = await heart.learn(FactInput(content="Alice's boat is called Petrel.", source="user_direct"))
    finally:
        await heart.close()
    return valued.id, legacy.id


async def test_each_rule_fills_what_the_writer_stores_and_nothing_else(db, agents):
    agent, other = agents(), agents("other")
    ids, want = await _scenario(db, agent, other)
    valued, legacy = await _learned_through_heart(db, agent)
    before = await _said(db, agent)
    assert before[valued] == SAID and before[legacy] is None

    report = await bf.run(db, agent, apply=True)

    after = await _said(db, agent)
    assert {name: after[fid] for name, fid in ids.items()} == want
    assert after[valued] == SAID  # a row with a value is never changed
    async with db.session() as s:
        legacy_learned = await s.scalar(text("SELECT learned_at FROM heart.facts WHERE id = :i"), {"i": legacy})
    assert after[legacy] == legacy_learned
    assert report.written == sum(1 for v in want.values() if v is not None) + 1
    rule = {fid: r for fid, _v, r in report.plan.fills}
    reason = dict(report.plan.kept)
    assert rule[ids["user_direct"]] == rule[ids["attachment"]] == bf.RULE_TURN
    assert rule[ids["fact_extractor"]] == bf.RULE_START
    assert rule[ids["reflection"]] == rule[ids["reflection_end_after_write"]] == bf.RULE_END
    assert rule[ids["reflection_other_agents_episode"]] == rule[ids["correction"]] == bf.RULE_NO_END
    assert rule[ids["knowledge"]] == bf.RULE_COMPACTION
    assert rule[ids["cluster"]] == rule[ids["contradiction"]] == bf.RULE_MERGE
    assert reason[ids["extractor_no_episode"]] == bf.KEPT_NO_EPISODE
    assert reason[ids["extractor_other_agents_episode"]] == bf.KEPT_EPISODE_NOT_FOUND
    assert reason[ids["exemplar"]] == bf.KEPT["exemplar_extractor"]
    assert reason[ids["kept_mcp"]] == reason[ids["kept_mcp:notes"]] == bf.KEPT_MCP
    assert reason[ids["kept_legacy_source"]] == reason[ids["kept_None"]] == reason[ids["attachment_like"]]
    assert reason[ids["kept_legacy_source"]] == bf.KEPT_NO_RULE


async def test_the_dry_run_writes_nothing_and_plans_what_apply_writes(db, agents):
    agent = agents()
    await _scenario(db, agent, agents("other"))
    rows = await _rows(db, agent)

    dry = await bf.run(db, agent, apply=False)

    assert await _rows(db, agent) == rows  # every column, mentioned_at and updated_at included
    assert dry.written == 0 and dry.nulls_after is None
    lines = "\n".join(bf.format_report(dry))
    assert "dry run, nothing written" in lines
    assert f"planned: fill {len(dry.plan.fills)}, stay NULL {len(dry.plan.kept)}" in lines
    assert "unchanged for " in lines and "no recency ordering changes" in lines
    applied = await bf.run(db, agent, apply=True)
    assert applied.plan.fills == dry.plan.fills and applied.plan.kept == dry.plan.kept
    assert applied.written == len(dry.plan.fills)


async def test_apply_changes_only_mentioned_at_and_the_triggers_updated_at(db, agents):
    agent = agents()
    await _scenario(db, agent, agents("other"))
    before = await _rows(db, agent)

    report = await bf.run(db, agent, apply=True)

    after = await _rows(db, agent)
    planned = {fid for fid, _v, _r in report.plan.fills}
    assert set(after) == set(before)
    for fid in before:
        rest = {k: v for k, v in before[fid].items() if k not in ("mentioned_at", "updated_at")}
        assert {k: v for k, v in after[fid].items() if k not in ("mentioned_at", "updated_at")} == rest
        moved = (
            after[fid]["mentioned_at"] != before[fid]["mentioned_at"],
            after[fid]["updated_at"] != before[fid]["updated_at"],
        )
        assert moved == ((True, True) if fid in planned else (False, False))
    assert report.rows_hash_before == report.rows_hash_after
    assert report.agent_hash_before == report.agent_hash_after
    assert sum(report.nulls_before.values()) - sum(report.nulls_after.values()) == report.written


async def test_a_second_apply_writes_nothing_and_says_so(db, agents):
    agent = agents()
    await _scenario(db, agent, agents("other"))
    first = await bf.run(db, agent, apply=True)
    assert first.written > 0
    rows = await _rows(db, agent)

    second = await bf.run(db, agent, apply=True)

    assert second.written == 0 and second.plan.fills == []
    assert await _rows(db, agent) == rows
    assert "nothing to fill" in "\n".join(bf.format_report(second))


async def test_another_agents_rows_episodes_and_events_are_never_touched_or_used(db, agents):
    agent, other = agents(), agents("other")
    ep_other = await _episode(db, other, _at(8), _at(8, 30))
    theirs = [
        await _fact(db, other, "user_direct", _at(9)),
        await _fact(db, other, "fact_extractor", _at(9, 30), episode=ep_other),
        await _fact(db, other, "reflection", _at(9, 31), episode=ep_other),
    ]
    their_rows = await _rows(db, other)
    mine_ep_other = await _fact(db, agent, "fact_extractor", _at(10), episode=ep_other)
    mine_reflection = await _fact(db, agent, "reflection", _at(10, 5), episode=ep_other)
    # Their event names my cluster merge: not my merge's record.
    mine_cluster = await _fact(db, agent, "cluster_consolidation", _at(3, day=1))
    src = await _fact(db, agent, "user_direct", _at(10, 10), superseded_by=mine_cluster, reason="merged")
    await _cluster_event(db, other, mine_cluster, [src])
    # My event lists one of their facts: my source list cannot be established.
    mine_cluster_2 = await _fact(db, agent, "cluster_consolidation", _at(4, day=1))
    src_2 = await _fact(db, agent, "user_direct", _at(10, 20), superseded_by=mine_cluster_2, reason="merged")
    await _cluster_event(db, agent, mine_cluster_2, [src_2, theirs[0]])

    report = await bf.run(db, agent, apply=True)

    assert await _rows(db, other) == their_rows
    said = await _said(db, agent)
    assert said[mine_ep_other] is None  # their episode is not joined
    assert said[mine_reflection] == _at(10, 5)  # no end of mine at hand: learned_at, never their 08:30
    assert said[mine_cluster] is None and said[mine_cluster_2] is None
    reason = dict(report.plan.kept)
    assert reason[mine_ep_other] == bf.KEPT_EPISODE_NOT_FOUND
    assert reason[mine_cluster] == bf.KEPT_NO_EVENT
    assert reason[mine_cluster_2] == bf.KEPT_BAD_LIST


async def test_a_merge_of_a_merge_fills_on_the_second_pass_as_the_merge_writer_would(db, agents):
    agent = agents()
    ep = await _episode(db, agent, _at(9), _at(11))
    outer = await _fact(db, agent, "cluster_consolidation", _at(3, day=2))
    inner = await _fact(db, agent, "cluster_consolidation", _at(3, day=1), superseded_by=outer, reason="merged")
    u = await _fact(db, agent, "user_direct", _at(9, 5), superseded_by=inner, reason="merged")
    e = await _fact(db, agent, "episode_summarizer", _at(11, 5), episode=ep, superseded_by=inner, reason="merged")
    v = await _fact(db, agent, "user_direct", _at(8, 50), superseded_by=outer, reason="merged")
    await _cluster_event(db, agent, inner, [u, e])
    await _cluster_event(db, agent, outer, [inner, v])

    report = await bf.run(db, agent, apply=True)

    said = await _said(db, agent)
    # Filled on pass 1 the outer merge would read the inner one's learned_at (a day later).
    assert said[inner] == _at(9, 5) and said[outer] == _at(9, 5)
    assert report.plan.merge_pass == {inner: 1, outer: 2}
    # The shipped merge writer, given the same sources now, stores the same time.
    settings = Settings().model_copy(update={"agent_id": agent})
    heart = Heart(db, settings, embedding_provider=MockEmbeddingProvider())
    try:
        fresh = await _fact(db, agent, "cluster_consolidation", _at(5, day=2))
        async with db.session() as s:
            await heart.facts._inherit_temporal_fields(fresh, [inner, v], s)
            await s.commit()
    finally:
        await heart.close()
    assert (await _said(db, agent))[fresh] == said[outer]


async def test_a_merge_whose_sources_cannot_be_established_stays_null(db, agents):
    agent = agents()

    async def merge(source: str, learned: datetime) -> UUID:
        return await _fact(db, agent, source, learned)

    no_event = await merge("cluster_consolidation", _at(3, day=1))
    await _fact(db, agent, "user_direct", _at(9), superseded_by=no_event, reason="merged")
    two_events = await merge("cluster_consolidation", _at(3, day=1))
    a = await _fact(db, agent, "user_direct", _at(9, 1), superseded_by=two_events, reason="merged")
    await _cluster_event(db, agent, two_events, [a])
    await _cluster_event(db, agent, two_events, [a])
    unknown_id = await merge("cluster_consolidation", _at(3, day=1))
    await _cluster_event(db, agent, unknown_id, [uuid4()])
    one_merged = await merge("contradiction_resolution", _at(3, day=1))
    await _fact(db, agent, "user_direct", _at(9, 2), superseded_by=one_merged, reason="merged")
    await _fact(db, agent, "user_direct", _at(9, 3), superseded_by=one_merged, reason="superseded")
    three_merged = await merge("contradiction_resolution", _at(3, day=1))
    for minute in (4, 5, 6):
        await _fact(db, agent, "user_direct", _at(9, minute), superseded_by=three_merged, reason="merged")
    # A cycle: each merge lists the other, so neither ever has a time.
    c1, c2 = await merge("cluster_consolidation", _at(3, day=1)), await merge("cluster_consolidation", _at(4, day=1))
    await _cluster_event(db, agent, c1, [c2])
    await _cluster_event(db, agent, c2, [c1])
    # A merge whose source is a merge that stays NULL reads that source as its learned_at, like any untimed source.
    over_kept = await merge("cluster_consolidation", _at(3, day=2))
    await _cluster_event(db, agent, over_kept, [no_event])

    report = await bf.run(db, agent, apply=True)

    said = await _said(db, agent)
    reason = dict(report.plan.kept)
    assert [reason[m] for m in (no_event, two_events, unknown_id, one_merged, three_merged, c1, c2)] == [
        bf.KEPT_NO_EVENT,
        bf.KEPT_EVENTS,
        bf.KEPT_BAD_LIST,
        bf.KEPT_NOT_TWO,
        bf.KEPT_NOT_TWO,
        bf.KEPT_WAITING,
        bf.KEPT_WAITING,
    ]
    assert all(said[m] is None for m in (no_event, two_events, unknown_id, one_merged, three_merged, c1, c2))
    assert said[over_kept] == _at(3, day=1)


async def test_limit_writes_the_first_planned_rows_and_a_later_run_the_rest(db, agents):
    agent = agents()
    ids, want = await _scenario(db, agent, agents("other"))
    dry = await bf.run(db, agent, apply=False)

    first = await bf.run(db, agent, apply=True, limit=3, batch_size=2)

    assert first.written == 3 and first.plan.fills == dry.plan.fills
    planned = [fid for fid, _v, _r in dry.plan.fills]
    said = await _said(db, agent)
    assert {fid for fid in planned if said[fid] is not None} == set(planned[:3])
    rest = await bf.run(db, agent, apply=True)
    assert rest.written == len(dry.plan.fills) - 3
    said = await _said(db, agent)
    assert {name: said[fid] for name, fid in ids.items()} == want


async def test_a_batch_that_would_change_another_column_is_rolled_back(db, agents, monkeypatch):
    agent = agents()
    await _scenario(db, agent, agents("other"))
    rows = await _rows(db, agent)
    monkeypatch.setattr(
        bf,
        "_UPDATE_BATCH",
        text(str(bf._UPDATE_BATCH).replace("SET mentioned_at = v.t", "SET mentioned_at = v.t, recall_count = 7")),
    )

    with pytest.raises(RuntimeError, match="rolled back"):
        await bf.run(db, agent, apply=True)

    assert await _rows(db, agent) == rows


async def test_a_row_given_a_value_after_the_plan_keeps_it(db, agents, monkeypatch):
    agent = agents()
    await _scenario(db, agent, agents("other"))
    real = bf._profiles_touched
    taken: list[UUID] = []

    async def plan_then_a_writer_fills_one(session, agent_id, plan):
        taken.append(plan.fills[0][0])
        async with db.session() as other:  # another transaction, committed while the plan is in hand
            await other.execute(
                text("UPDATE heart.facts SET mentioned_at = :t WHERE id = :i"), {"t": SAID, "i": taken[0]}
            )
            await other.commit()
        return await real(session, agent_id, plan)

    monkeypatch.setattr(bf, "_profiles_touched", plan_then_a_writer_fills_one)

    report = await bf.run(db, agent, apply=True)

    assert (await _said(db, agent))[taken[0]] == SAID
    assert report.written == len(report.plan.fills) - 1


async def test_the_dry_run_counts_the_entity_profiles_a_fill_makes_due(db, agents):
    agent = agents()
    filled = await _fact(db, agent, "user_direct", _at(9))
    valued = await _fact(db, agent, "user_direct", _at(9, 5), mentioned=_at(9, 5))
    async with db.session() as s:
        for key, fid in (("alice", filled), ("petrel", valued)):
            await s.execute(
                text("INSERT INTO heart.fact_entity_keys (fact_id, entity_key, agent_id) VALUES (:f, :k, :a)"),
                {"f": fid, "k": key, "a": agent},
            )
            await s.execute(
                text(
                    "INSERT INTO nous_system.entity_profile (agent_id, entity_key, status, source_hash) "
                    "VALUES (:a, :k, 'profile', 'h')"
                ),
                {"a": agent, "k": key},
            )
        await s.commit()

    report = await bf.run(db, agent, apply=False)

    assert report.profiles == 1  # alice's facts include the filled row, petrel's do not
    assert "at most 1;" in "\n".join(bf.format_report(report))


# ---------------------------------------------------------------------------
# The two resolver cases of the statement-time reviews resolve as before the fill.
# ---------------------------------------------------------------------------


def _statuses(rows: list[dict], intervals: bool) -> list[tuple[str | None, str | None]]:
    """(later, earlier) recency status in the recall_deep copy, then in the pre-turn copy, for two stored rows."""
    from test_temporal_read_render import _engine, _pfact, _rsettings, _sfact, _tags

    from nous.api.retrieval_pipeline import _resolve_recency_conflicts

    def dates(r: dict) -> dict:
        day = r["event_date"]
        return {
            "event_date": day,
            "valid_from": day,
            "valid_to": day,
            "learned_at": r["learned_at"],
            "mentioned_at": r["mentioned_at"],
        }

    earlier, later = rows
    tags = _tags(_resolve_recency_conflicts([_pfact(r["content"], **dates(r)) for r in rows], _rsettings(intervals)))
    out_earlier, out_later = _engine(temporal_resolver_intervals_enabled=intervals)._resolve_recency(
        [_sfact(r["content"], **dates(r)) for r in rows]
    )
    return [
        (tags[later["content"]][0], tags[earlier["content"]][0]),
        (out_later.recency_status, out_earlier.recency_status),
    ]


async def _stored(db, ids: list[UUID]) -> list[dict]:
    async with db.session() as s:
        rows = await s.execute(
            text("SELECT id, content, event_date, learned_at, mentioned_at FROM heart.facts WHERE id = ANY(:ids)"),
            {"ids": ids},
        )
        by_id = {r.id: dict(r._mapping) for r in rows}
    return [by_id[i] for i in ids]


@pytest.mark.parametrize("pair", ["two_learn_fact_facts", "learn_fact_then_compaction"])
@pytest.mark.parametrize("intervals", [False, True], ids=["intervals_off", "intervals_on"])
async def test_the_later_correction_stays_current_after_the_fill(db, agents, pair, intervals):
    """One episode open from 09:00: May 10 said at 09:05 (learn_fact), moved to May 3 at 11:30 (learn_fact) or kept
    by the knowledge extractor at the 13:00 compaction. Both rows are legacy: no said-time."""
    agent = agents()
    ep = await _episode(db, agent, _at(9))
    may_10 = await _fact(
        db,
        agent,
        "user_direct",
        _at(9, 5),
        episode=ep,
        event_date=date(2026, 5, 10),
        content="Alice's dentist appointment is on May 10",
    )
    if pair == "two_learn_fact_facts":
        may_3 = await _fact(
            db,
            agent,
            "user_direct",
            _at(11, 30),
            episode=ep,
            event_date=date(2026, 5, 3),
            content="Alice's dentist appointment is on May 3",
        )
    else:
        may_3 = await _fact(
            db,
            agent,
            "knowledge_extractor",
            _at(13),
            event_date=date(2026, 5, 3),
            content="Alice's dentist appointment is on May 3",
        )
    before = await _stored(db, [may_10, may_3])
    assert [r["mentioned_at"] for r in before] == [None, None]
    assert _statuses(before, intervals) == [("current", "superseded")] * 2

    await bf.run(db, agent, apply=True)

    after = await _stored(db, [may_10, may_3])
    assert [r["mentioned_at"] for r in after] == [r["learned_at"] for r in after]
    assert _statuses(after, intervals) == [("current", "superseded")] * 2
