"""scripts/backfill_mentioned_at.py: each rule fills what the writer of its source stores now, and nothing else.

Legacy rows (written before every writer recorded ``mentioned_at``) are inserted by hand with a NULL said-time; rows
that already carry one are written through ``heart.learn``. Each test uses its own agent ids.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from itertools import count
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


def _manifest_lines(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _without(row: dict, *columns: str) -> dict:
    return {k: v for k, v in row.items() if k not in columns}


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


@pytest.fixture
def manifest(tmp_path):
    """A new manifest path per call."""
    numbers = count(1)
    return lambda: str(tmp_path / f"manifest-{next(numbers)}.jsonl")


async def _apply_both(db, agent: str, manifest, **kw) -> bf.Report:
    """--apply --reordering-rules: both tiers."""
    return await bf.run(db, agent, apply=True, manifest=manifest(), reordering=True, **kw)


async def _scenario(db, agent: str, other: str) -> tuple[dict[str, UUID], dict[str, datetime | None]]:
    """One of every kind of legacy row, and the value each must end with under both tiers (None: stays NULL)."""
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
    # Never later than learned_at: an episode that started after the row was learned, a merge whose source reads
    # later than the merge itself.
    ep_after = await _episode(db, agent, _at(9, day=5), _at(10, day=5))
    await row("extractor_episode_after", "fact_extractor", _at(9, 9), None, episode=ep_after)
    late = await row("merge_later_than_itself", "cluster_consolidation", _at(3, 45, day=1), None)
    timed = await _fact(
        db, agent, "user_direct", _at(9, 10), mentioned=_at(9, day=2), superseded_by=late, reason="merged"
    )
    await _cluster_event(db, agent, late, [timed])
    ids["timed_source"], want["timed_source"] = timed, _at(9, day=2)
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


async def test_each_rule_fills_what_the_writer_stores_and_nothing_else(db, agents, manifest):
    agent, other = agents(), agents("other")
    ids, want = await _scenario(db, agent, other)
    valued, legacy = await _learned_through_heart(db, agent)
    before = await _said(db, agent)
    assert before[valued] == SAID and before[legacy] is None

    report = await _apply_both(db, agent, manifest)

    after = await _said(db, agent)
    assert {name: after[fid] for name, fid in ids.items()} == want
    assert after[valued] == SAID  # a row with a value is never changed
    async with db.session() as s:
        legacy_learned = await s.scalar(text("SELECT learned_at FROM heart.facts WHERE id = :i"), {"i": legacy})
    assert after[legacy] == legacy_learned
    assert report.written == sum(1 for name, v in want.items() if v is not None and name != "timed_source") + 1
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
    assert reason[ids["extractor_episode_after"]] == reason[ids["merge_later_than_itself"]] == bf.KEPT_LATER
    assert all(value <= report.plan.facts[fid].learned_at for fid, value, _r in report.plan.fills)


async def test_the_default_tier_writes_only_fills_equal_to_learned_at(db, agents, manifest):
    agent = agents()
    ids, want = await _scenario(db, agent, agents("other"))
    path = manifest()

    report = await bf.run(db, agent, apply=True, manifest=path)

    said = await _said(db, agent)
    default = [f for f in report.plan.fills if report.plan.changes_nothing(f)]
    second = [f for f in report.plan.fills if not report.plan.changes_nothing(f)]
    assert default and second
    assert report.written == report.planned == len(default)
    assert all(said[fid] == value for fid, value, _r in default)
    assert all(said[fid] is None for fid, _v, _r in second)
    assert said[ids["fact_extractor"]] is None and said[ids["cluster"]] is None and said[ids["reflection"]] is None
    assert said[ids["user_direct"]] == want["user_direct"]
    assert all(line["mentioned_at"] == line["learned_at"] for line in _manifest_lines(path))


async def test_the_dry_run_writes_nothing_and_reports_both_tiers(db, agents, manifest):
    agent = agents()
    await _scenario(db, agent, agents("other"))
    rows = await _rows(db, agent)

    dry = await bf.run(db, agent, apply=False)

    assert await _rows(db, agent) == rows  # every column, mentioned_at and updated_at included
    assert dry.written == 0 and dry.nulls_after is None and dry.manifest is None
    lines = "\n".join(bf.format_report(dry, calls_per_day=40))
    default = [f for f in dry.plan.fills if dry.plan.changes_nothing(f)]
    assert "dry run, nothing written" in lines
    assert (
        f"planned: default tier {len(default)}, second tier {len(dry.plan.fills) - len(default)}, "
        f"stay NULL {len(dry.plan.kept)}"
    ) in lines
    assert "no reader's result or ordering changes" in lines
    # fact_extractor, episode_summarizer and enumerative rows (learned 11:05, 11:05, 11:06) and an inactive merge
    # source (11:05) of an episode that started at 09:00
    assert "b: episode start: 4 (active 3, inactive 1), earlier than today by median 2h 05m, max 2h 06m" in lines
    assert "stale-facts finding" in lines and "scripts/backfill_supersession.py" in lines
    assert "daily limit of model calls (NOUS_ENTITY_PROFILE_MAX_CALLS_PER_DAY, 40)" in lines
    applied = await _apply_both(db, agent, manifest)
    assert applied.plan.fills == dry.plan.fills and applied.plan.kept == dry.plan.kept
    assert applied.written == len(dry.plan.fills)


async def test_apply_changes_only_mentioned_at_and_the_triggers_updated_at(db, agents, manifest):
    agent = agents()
    await _scenario(db, agent, agents("other"))
    before = await _rows(db, agent)

    report = await _apply_both(db, agent, manifest)

    after = await _rows(db, agent)
    planned = {fid for fid, _v, _r in report.plan.fills}
    assert set(after) == set(before)
    for fid in before:
        assert _without(after[fid], "mentioned_at", "updated_at") == _without(before[fid], "mentioned_at", "updated_at")
        moved = (
            after[fid]["mentioned_at"] != before[fid]["mentioned_at"],
            after[fid]["updated_at"] != before[fid]["updated_at"],
        )
        assert moved == ((True, True) if fid in planned else (False, False))
    assert report.rows_hash_before == report.rows_hash_after
    assert report.agent_hash_before == report.agent_hash_after
    assert sum(report.nulls_before.values()) - sum(report.nulls_after.values()) == report.written


async def test_the_manifest_lists_every_row_written_with_its_value(db, agents, manifest):
    agent = agents()
    await _scenario(db, agent, agents("other"))
    path = manifest()

    report = await bf.run(db, agent, apply=True, manifest=path, reordering=True, batch_size=4)

    said = await _said(db, agent)
    lines = _manifest_lines(path)
    planned = {fid: (value, rule) for fid, value, rule in report.plan.fills}
    assert len(lines) == report.written == len(planned)
    for line in lines:
        fid = UUID(line["id"])
        assert line["agent_id"] == agent
        assert datetime.fromisoformat(line["mentioned_at"]) == said[fid] == planned[fid][0]
        assert line["rule"] == planned[fid][1]
        assert datetime.fromisoformat(line["learned_at"]) == report.plan.facts[fid].learned_at
    assert f"each is listed in the manifest {path}" in "\n".join(bf.format_report(report))


async def test_a_stop_after_the_manifest_write_keeps_the_committed_batches_and_says_so(
    db, agents, manifest, monkeypatch
):
    agent = agents()
    await _scenario(db, agent, agents("other"))
    rows = await _rows(db, agent)
    path = manifest()
    real = bf._record
    calls: list[int] = []

    def record_then_fail_on_the_second_batch(out, lines):
        real(out, lines)
        on_disk = {line["id"] for line in _manifest_lines(path)}  # read back through another handle
        assert {line["id"] for line in lines} <= on_disk
        calls.append(1)
        if len(calls) == 2:  # the lines are on disk; the commit that follows never happens
            raise OSError("the disk went away after the manifest write")

    monkeypatch.setattr(bf, "_record", record_then_fail_on_the_second_batch)

    report = await bf.run(db, agent, apply=True, manifest=path, reordering=True, batch_size=2)

    assert report.batches == 1 and report.written == 2
    assert report.error.startswith("batch 2 (rows 3-4 of ") and "): OSError: the disk went away" in report.error
    said = await _said(db, agent)
    first, failed = report.plan.fills[:2], report.plan.fills[2:4]
    assert [said[fid] for fid, _v, _r in first] == [value for _fid, value, _r in first]
    assert [said[fid] for fid, _v, _r in failed] == [None, None]
    assert [UUID(line["id"]) for line in _manifest_lines(path)] == sorted(
        [fid for fid, _v, _r in first], key=str
    ) + sorted([fid for fid, _v, _r in failed], key=str)
    out = "\n".join(bf.format_report(report))
    assert "STOPPED: batch 2" in out
    assert f"Committed before it: 1 batches, 2 of {len(report.plan.fills)} rows (both tiers)" in out
    assert f"listed in the manifest {path}" in out

    undone = await bf.undo(db, agent, path, apply=True)

    # The committed rows are set back; the failed batch's rows were never written, so the undo leaves them alone.
    assert (undone.lines, undone.matching, undone.changed, undone.undone) == (4, 2, 2, 2)
    after = await _rows(db, agent)
    for fid, _v, _r in failed:
        assert after[fid] == rows[fid]  # updated_at included: not touched at all
    for fid, _v, _r in first:
        assert after[fid]["mentioned_at"] is None
        assert _without(after[fid], "updated_at") == _without(rows[fid], "updated_at")


async def _hold(db, fid: UUID, mode: str):
    """Another transaction holding a row lock on ``fid`` until it is rolled back."""
    conn = await db.engine.connect()
    trans = await conn.begin()
    await conn.execute(text(f"SELECT id FROM heart.facts WHERE id = :i FOR {mode}"), {"i": fid})
    return conn, trans


async def test_the_batch_lock_does_not_skip_a_row_under_a_foreign_key_check(db, agents, manifest):
    agent = agents()
    await _scenario(db, agent, agents("other"))
    first = (await bf.run(db, agent, apply=False)).plan.fills[0][0]
    conn, trans = await _hold(db, first, "KEY SHARE")  # what an insert that points at the fact takes
    try:
        report = await _apply_both(db, agent, manifest)
    finally:
        await trans.rollback()
        await conn.close()

    assert report.error is None and report.held == 0 and report.written == len(report.plan.fills)


async def test_a_row_another_transaction_holds_is_skipped_counted_and_filled_by_a_rerun(db, agents, manifest):
    agent = agents()
    await _scenario(db, agent, agents("other"))
    plan = (await bf.run(db, agent, apply=False)).plan
    held, value, _rule = plan.fills[3]  # a row of batch 2
    path = manifest()
    conn, trans = await _hold(db, held, "NO KEY UPDATE")  # the application in the middle of a write
    try:
        report = await bf.run(db, agent, apply=True, manifest=path, reordering=True, batch_size=3)
        assert (await _said(db, agent))[held] is None
    finally:
        await trans.rollback()
        await conn.close()

    # Not waited for (a wait would have ended in the lock timeout and stopped the run), counted, left NULL.
    assert report.error is None and report.held == 1 and report.written == len(plan.fills) - 1
    assert str(held) not in {line["id"] for line in _manifest_lines(path)}
    assert "skipped because another transaction held them: 1 (still NULL as of this run" in "\n".join(
        bf.format_report(report)
    )

    rerun = await _apply_both(db, agent, manifest)

    assert (rerun.written, rerun.held) == (1, 0) and (await _said(db, agent))[held] == value


async def test_undo_skips_a_row_another_transaction_holds_and_a_second_undo_sets_it_back(db, agents, manifest):
    agent = agents()
    await _scenario(db, agent, agents("other"))
    path = manifest()
    filled = await bf.run(db, agent, apply=True, manifest=path)
    held = filled.plan.selected(False)[2][0]
    conn, trans = await _hold(db, held, "NO KEY UPDATE")
    try:
        first = await bf.undo(db, agent, path, apply=True, batch_size=2)
    finally:
        await trans.rollback()
        await conn.close()

    assert first.error is None and first.held == 1 and first.undone == filled.written - 1
    assert (await _said(db, agent))[held] is not None
    assert "held them: 1 (run the undo again)" in "\n".join(bf.format_undo(first))

    second = await bf.undo(db, agent, path, apply=True)

    said = await _said(db, agent)
    assert second.undone == 1 and all(said[UUID(line["id"])] is None for line in _manifest_lines(path))


class _DiskFillsUp:
    """A manifest whose disk fills up after the first batch: the next write leaves half a line, and the close, which
    flushes it again, fails too."""

    def __init__(self, real):
        self.real, self.flushed, self.broken = real, False, False

    def write(self, text_: str) -> int:
        if self.flushed:
            self.real.write(text_[: len(text_) // 2])
            self.broken = True
            raise OSError(28, "No space left on device")
        return self.real.write(text_)

    def flush(self) -> None:
        self.real.flush()
        self.flushed = True

    def fileno(self) -> int:
        return self.real.fileno()

    def close(self) -> None:
        self.real.close()
        if self.broken:
            raise OSError(28, "No space left on device")


async def test_a_manifest_that_breaks_part_way_still_gives_the_summary(db, agents, manifest, monkeypatch):
    agent = agents()
    await _scenario(db, agent, agents("other"))
    path = manifest()
    builtin_open = open
    monkeypatch.setattr(
        bf,
        "open",
        lambda p, mode="r", **kw: (
            _DiskFillsUp(builtin_open(p, mode, **kw)) if mode == "x" else builtin_open(p, mode, **kw)
        ),
        raising=False,
    )

    report = await bf.run(db, agent, apply=True, manifest=path, reordering=True, batch_size=2)

    assert (report.batches, report.written) == (1, 2)
    assert report.error.startswith("batch 2 (rows 3-4 of ") and "No space left on device" in report.error
    assert "; closing the manifest (every committed batch's lines were already on disk): OSError" in report.error
    said = await _said(db, agent)
    assert [said[fid] is not None for fid, _v, _r in report.plan.fills[:4]] == [True, True, False, False]
    out = "\n".join(bf.format_report(report))
    assert "STOPPED: batch 2" in out and f"Committed before it: 1 batches, 2 of {len(report.plan.fills)}" in out
    assert f"Keep the manifest {path}" in out
    with builtin_open(path, encoding="utf-8") as f:
        assert len(f.read().splitlines()) == 3  # two whole lines and a cut one


async def test_undo_skips_an_unreadable_line_and_undoes_the_rest(db, agents, manifest):
    agent = agents()
    await _scenario(db, agent, agents("other"))
    path = manifest()
    filled = await bf.run(db, agent, apply=True, manifest=path)
    with open(path, "a", encoding="utf-8") as f:  # a line cut by a failed write
        f.write(json.dumps({"agent_id": agent, "id": str(uuid4()), "rule": "x", "mentioned_at": "2026"})[:40])

    report = await bf.undo(db, agent, path, apply=True)

    assert (report.lines, report.unreadable, report.undone) == (filled.written + 1, 1, filled.written)
    assert report.error is None
    assert "unreadable, skipped: 1" in "\n".join(bf.format_undo(report))


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, asyncio.CancelledError], ids=["ctrl_c", "cancelled"])
async def test_ctrl_c_in_a_batch_rolls_it_back_and_still_gives_the_summary(
    db, agents, manifest, monkeypatch, interrupt
):
    agent = agents()
    await _scenario(db, agent, agents("other"))
    path = manifest()
    real = bf._record
    calls: list[int] = []

    def record_until_interrupted(out, lines):
        calls.append(1)
        if len(calls) == 2:  # Ctrl-C in batch 2, after its update and before its commit
            raise interrupt()
        real(out, lines)

    monkeypatch.setattr(bf, "_record", record_until_interrupted)

    report = await bf.run(db, agent, apply=True, manifest=path, reordering=True, batch_size=2)

    assert (report.batches, report.written) == (1, 2)
    assert report.error.startswith("batch 2 (rows 3-4 of ") and f"): {interrupt.__name__}" in report.error
    said = await _said(db, agent)  # the task goes on: a cancellation taken in the batch does not cancel this
    assert [said[fid] is not None for fid, _v, _r in report.plan.fills[:4]] == [True, True, False, False]
    out = "\n".join(bf.format_report(report))
    assert "STOPPED: batch 2" in out and f"Keep the manifest {path}" in out


@pytest.mark.parametrize("failure", [OSError("the connection dropped"), KeyboardInterrupt()], ids=["error", "ctrl_c"])
async def test_a_failure_in_the_read_after_the_last_batch_still_gives_the_summary(
    db, agents, manifest, monkeypatch, failure
):
    agent = agents()
    await _scenario(db, agent, agents("other"))
    real = bf._nulls
    calls: list[int] = []

    async def nulls_then_fail(session, agent_id):
        calls.append(1)
        if len(calls) == 2:  # the read after the last batch
            raise failure
        return await real(session, agent_id)

    monkeypatch.setattr(bf, "_nulls", nulls_then_fail)

    report = await _apply_both(db, agent, manifest)

    assert report.error is None and report.written == len(report.plan.fills)
    assert report.after_error.startswith(f"the read after the last batch: {type(failure).__name__}")
    out = "\n".join(bf.format_report(report))
    assert f"wrote {report.written} of {report.written} rows" in out
    assert "the read after the last batch failed" in out and "Keep the manifest" in out


async def test_undo_restores_skips_changed_rows_and_never_touches_another_agent(db, agents, manifest):
    agent, other = agents(), agents("other")
    await _scenario(db, agent, other)
    theirs = await _fact(db, other, "user_direct", _at(9, 15))
    mine_before = await _rows(db, agent)
    mine_path, their_path = manifest(), manifest()
    mine = await bf.run(db, agent, apply=True, manifest=mine_path, reordering=True)
    await bf.run(db, other, apply=True, manifest=their_path)
    their_rows = await _rows(db, other)
    changed = mine.plan.fills[0][0]
    async with db.session() as s:  # the app changes one filled row after the run
        await s.execute(text("UPDATE heart.facts SET mentioned_at = :t WHERE id = :i"), {"t": SAID, "i": changed})
        await s.commit()
    with open(mine_path, "a", encoding="utf-8") as f:  # a line naming another agent's fact under this agent
        stored = their_rows[theirs]["mentioned_at"]
        f.write(json.dumps({"agent_id": agent, "id": str(theirs), "rule": "x", "mentioned_at": stored}) + "\n")
    filled = await _rows(db, agent)

    dry = await bf.undo(db, agent, mine_path, apply=False)

    assert await _rows(db, agent) == filled
    assert (dry.matching, dry.changed, dry.missing, dry.undone) == (mine.written - 1, 1, 1, 0)

    report = await bf.undo(db, agent, mine_path, apply=True)

    assert report.undone == mine.written - 1 and report.error is None
    after = await _rows(db, agent)
    for fid in mine_before:
        if fid == changed:
            assert datetime.fromisoformat(after[fid]["mentioned_at"]) == SAID
        else:
            assert after[fid]["mentioned_at"] == mine_before[fid]["mentioned_at"]
        rest = _without(mine_before[fid], "updated_at", "mentioned_at")
        assert _without(after[fid], "updated_at", "mentioned_at") == rest
    assert await _rows(db, other) == their_rows

    crossed = await bf.undo(db, agent, their_path, apply=True)  # their manifest, run for this agent

    assert (crossed.lines, crossed.other_agent, crossed.matching, crossed.undone) == (1, 1, 0, 0)
    assert await _rows(db, other) == their_rows
    assert bf.CANNOT_UNDO in bf.format_undo(crossed)


async def test_undo_skips_a_row_the_app_changes_after_the_undo_read_it(db, agents, manifest, monkeypatch):
    agent = agents()
    await _scenario(db, agent, agents("other"))
    path = manifest()
    filled = await bf.run(db, agent, apply=True, manifest=path)
    real = bf._batches
    changed = filled.plan.selected(False)[0][0]

    async def a_writer_changes_one_row_first(*args, **kwargs):
        async with db.session() as s:
            await s.execute(text("UPDATE heart.facts SET mentioned_at = :t WHERE id = :i"), {"t": SAID, "i": changed})
            await s.commit()
        return await real(*args, **kwargs)

    monkeypatch.setattr(bf, "_batches", a_writer_changes_one_row_first)

    report = await bf.undo(db, agent, path, apply=True)

    assert report.error is None and report.matching == filled.written and report.undone == filled.written - 1
    assert (await _said(db, agent))[changed] == SAID
    assert "skipped because they changed during the undo: 1" in "\n".join(bf.format_undo(report))


async def test_a_second_apply_writes_nothing_and_says_so(db, agents, manifest):
    agent = agents()
    await _scenario(db, agent, agents("other"))
    used = manifest()
    first = await bf.run(db, agent, apply=True, manifest=used, reordering=True)
    assert first.written > 0
    rows = await _rows(db, agent)

    second = await _apply_both(db, agent, manifest)

    assert second.written == 0 and second.plan.fills == []
    assert await _rows(db, agent) == rows
    assert "nothing to fill" in "\n".join(bf.format_report(second))
    with pytest.raises(ValueError, match="--manifest"):
        await bf.run(db, agent, apply=True)
    with pytest.raises(FileExistsError):  # an earlier run's record is never overwritten
        await bf.run(db, agent, apply=True, manifest=used)
    assert len(_manifest_lines(used)) == first.written


async def test_another_agents_rows_episodes_and_events_are_never_touched_or_used(db, agents, manifest):
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

    report = await _apply_both(db, agent, manifest)

    assert await _rows(db, other) == their_rows
    said = await _said(db, agent)
    assert said[mine_ep_other] is None  # their episode is not joined
    assert said[mine_reflection] == _at(10, 5)  # no end of mine at hand: learned_at, never their 08:30
    assert said[mine_cluster] is None and said[mine_cluster_2] is None
    reason = dict(report.plan.kept)
    assert reason[mine_ep_other] == bf.KEPT_EPISODE_NOT_FOUND
    assert reason[mine_cluster] == bf.KEPT_NO_EVENT
    assert reason[mine_cluster_2] == bf.KEPT_BAD_LIST


async def test_a_merge_of_a_merge_fills_on_the_second_pass_as_the_merge_writer_would(db, agents, manifest):
    agent = agents()
    ep = await _episode(db, agent, _at(9), _at(11))
    outer = await _fact(db, agent, "cluster_consolidation", _at(3, day=2))
    inner = await _fact(db, agent, "cluster_consolidation", _at(3, day=1), superseded_by=outer, reason="merged")
    u = await _fact(db, agent, "user_direct", _at(9, 5), superseded_by=inner, reason="merged")
    e = await _fact(db, agent, "episode_summarizer", _at(11, 5), episode=ep, superseded_by=inner, reason="merged")
    v = await _fact(db, agent, "user_direct", _at(8, 50), superseded_by=outer, reason="merged")
    await _cluster_event(db, agent, inner, [u, e])
    await _cluster_event(db, agent, outer, [inner, v])

    report = await _apply_both(db, agent, manifest)

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


async def test_a_merge_whose_sources_cannot_be_established_stays_null(db, agents, manifest):
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
    # Sources learned after the fact they point at: pointed at an older fact they duplicated, not merged into it.
    older_cluster = await merge("cluster_consolidation", _at(3, day=1))
    later = await _fact(db, agent, "user_direct", _at(5, day=1), superseded_by=older_cluster, reason="merged")
    await _cluster_event(db, agent, older_cluster, [later])
    older_contra = await merge("contradiction_resolution", _at(3, day=1))
    await _fact(db, agent, "user_direct", _at(9, 7), superseded_by=older_contra, reason="merged")
    await _fact(db, agent, "user_direct", _at(6, day=1), superseded_by=older_contra, reason="merged")
    # A cycle: each merge lists the other, so neither ever has a time.
    c1, c2 = await merge("cluster_consolidation", _at(3, day=1)), await merge("cluster_consolidation", _at(3, day=1))
    await _cluster_event(db, agent, c1, [c2])
    await _cluster_event(db, agent, c2, [c1])
    # A merge whose source is a merge that stays NULL reads that source as its learned_at, like any untimed source.
    over_kept = await merge("cluster_consolidation", _at(3, day=2))
    await _cluster_event(db, agent, over_kept, [no_event])

    report = await _apply_both(db, agent, manifest)

    said = await _said(db, agent)
    reason = dict(report.plan.kept)
    kept = (no_event, two_events, unknown_id, one_merged, three_merged, older_cluster, older_contra, c1, c2)
    assert [reason[m] for m in kept] == [
        bf.KEPT_NO_EVENT,
        bf.KEPT_EVENTS,
        bf.KEPT_BAD_LIST,
        bf.KEPT_NOT_TWO,
        bf.KEPT_NOT_TWO,
        bf.KEPT_SOURCE_LATER,
        bf.KEPT_SOURCE_LATER,
        bf.KEPT_WAITING,
        bf.KEPT_WAITING,
    ]
    assert all(said[m] is None for m in kept)
    assert said[over_kept] == _at(3, day=1)


async def test_limit_writes_the_first_planned_rows_and_a_later_run_the_rest(db, agents, manifest):
    agent = agents()
    ids, want = await _scenario(db, agent, agents("other"))
    dry = await bf.run(db, agent, apply=False)

    first = await _apply_both(db, agent, manifest, limit=3, batch_size=2)

    assert first.written == 3 and first.plan.fills == dry.plan.fills
    planned = [fid for fid, _v, _r in dry.plan.fills]
    said = await _said(db, agent)
    assert {fid for fid in planned if said[fid] is not None} == set(planned[:3])
    rest = await _apply_both(db, agent, manifest)
    assert rest.written == len(dry.plan.fills) - 3
    said = await _said(db, agent)
    assert {name: said[fid] for name, fid in ids.items()} == want


def _with_update(set_clause: str) -> tuple:
    lock, update, held = bf._FILL_SQL
    return lock, text(str(update).replace("SET mentioned_at = v.t", set_clause)), held


async def test_a_batch_that_would_change_another_column_is_rolled_back(db, agents, manifest, monkeypatch):
    agent = agents()
    await _scenario(db, agent, agents("other"))
    rows = await _rows(db, agent)
    monkeypatch.setattr(bf, "_FILL_SQL", _with_update("SET mentioned_at = v.t, recall_count = 7"))

    report = await _apply_both(db, agent, manifest)

    assert report.batches == report.written == 0
    assert report.error.startswith("batch 1 ") and "): RuntimeError: a column other than" in report.error
    assert await _rows(db, agent) == rows


async def test_a_database_error_stops_the_run_with_one_line(db, agents, manifest, monkeypatch):
    agent = agents()
    await _scenario(db, agent, agents("other"))
    rows = await _rows(db, agent)
    monkeypatch.setattr(bf, "_FILL_SQL", _with_update("SET mentioned_at = v.t, no_such_column = 1"))

    report = await _apply_both(db, agent, manifest)

    # The first line only: the statement and its parameters follow on later lines of a database error.
    assert report.batches == 0 and "no_such_column" in report.error and "\n" not in report.error
    assert await _rows(db, agent) == rows


async def test_a_row_given_a_value_after_the_plan_keeps_it(db, agents, manifest, monkeypatch):
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

    report = await _apply_both(db, agent, manifest)

    assert (await _said(db, agent))[taken[0]] == SAID
    assert report.written == len(report.plan.fills) - 1 and report.error is None


async def test_the_dry_run_counts_the_entity_profiles_each_tier_makes_due(db, agents):
    agent = agents()
    ep = await _episode(db, agent, _at(9), _at(11))
    keyed = {
        "alice": (await _fact(db, agent, "user_direct", _at(9)), "profile"),  # default tier
        "petrel": (await _fact(db, agent, "user_direct", _at(9, 5), mentioned=_at(9, 5)), "profile"),  # has a value
        "harbour": (await _fact(db, agent, "fact_extractor", _at(11, 5), episode=ep), "profile"),  # second tier
        "pier": (await _fact(db, agent, "user_direct", _at(9, 10)), "not_entity"),  # not asked again on a change
    }
    async with db.session() as s:
        for key, (fid, status) in keyed.items():
            await s.execute(
                text("INSERT INTO heart.fact_entity_keys (fact_id, entity_key, agent_id) VALUES (:f, :k, :a)"),
                {"f": fid, "k": key, "a": agent},
            )
            await s.execute(
                text(
                    "INSERT INTO nous_system.entity_profile (agent_id, entity_key, status, source_hash) "
                    "VALUES (:a, :k, :s, 'h')"
                ),
                {"a": agent, "k": key, "s": status},
            )
        await s.commit()

    report = await bf.run(db, agent, apply=False)

    assert report.profiles == (1, 2)
    assert "at most 1 with the default tier, at most 2 with --reordering-rules" in "\n".join(bf.format_report(report))


# ---------------------------------------------------------------------------
# Resolver pins: the two pairs of the statement-time reviews resolve as before a default-tier fill; an extractor fill
# (second tier) flips the in-turn pair it was meant to flip, and only with --reordering-rules.
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
async def test_the_later_correction_stays_current_after_the_fill(db, agents, manifest, pair, intervals):
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

    await bf.run(db, agent, apply=True, manifest=manifest())

    after = await _stored(db, [may_10, may_3])
    assert [r["mentioned_at"] for r in after] == [r["learned_at"] for r in after]
    assert _statuses(after, intervals) == [("current", "superseded")] * 2


@pytest.mark.parametrize("reordering", [False, True], ids=["default_tier", "reordering_rules"])
@pytest.mark.parametrize("intervals", [False, True], ids=["intervals_off", "intervals_on"])
async def test_an_extractor_fill_flips_the_in_turn_pair_only_with_the_reordering_rules(
    db, agents, manifest, reordering, intervals
):
    """Episode 09:00-15:00. learn_fact stores May 10 at 09:05. At 11:30 the user moves it to May 3 and only the
    episode extractor keeps that, stored at 15:01. Both legacy rows. With the extractor's episode start filled
    (09:00), May 3 reads earlier than May 10 and loses, where today it reads 15:01 and wins."""
    agent = agents()
    ep = await _episode(db, agent, _at(9), _at(15))
    may_10 = await _fact(
        db,
        agent,
        "user_direct",
        _at(9, 5),
        episode=ep,
        event_date=date(2026, 5, 10),
        content="Alice's dentist appointment is on May 10",
    )
    may_3 = await _fact(
        db,
        agent,
        "episode_summarizer",
        _at(15, 1),
        episode=ep,
        event_date=date(2026, 5, 3),
        content="Alice's dentist appointment is on May 3",
    )
    assert _statuses(await _stored(db, [may_10, may_3]), intervals) == [("current", "superseded")] * 2

    await bf.run(db, agent, apply=True, manifest=manifest(), reordering=reordering)

    after = await _stored(db, [may_10, may_3])
    assert after[1]["mentioned_at"] == (_at(9) if reordering else None)
    flipped = reordering and intervals
    assert _statuses(after, intervals) == [("superseded", "current") if flipped else ("current", "superseded")] * 2
