"""Fill heart.facts.mentioned_at (when a fact was said) on rows written before every writer recorded it.

One-off and optional: running it is an operator's decision. Dry run unless --apply. Agent-scoped. Only rows whose
mentioned_at IS NULL are touched, active and inactive alike; a row with a value is never changed, a row with no
truthful source stays NULL, and no value is ever later than the row's learned_at (such a row stays NULL under its own
reason). Each rule stores what the writer of that source stores now (FactInput.mentioned_at):

  a. Writers inside the turn: learned_at, their insert time in that turn. Sources user_direct (the learn_fact tool and
     run_python's learn_fact), inline_correction, pre_prune_extraction, and the attachment fact (category
     'attachment', source '<channel>-attachment').
  b. The fact, episode-summary and enumerative extractors (fact_extractor, episode_summarizer, enumerative_extractor):
     the start of the source episode, joined on id and agent; with no such episode the row stays NULL. The enumerative
     extractor has a second writer that stores no said-time (scripts/backfill_enumerative_facts.py); its rows are
     filled too, because the episode start belongs to the episode and is true whichever run wrote the row.
     The session reflection (reflection) and the correction extractor (correction_extraction): the source episode's
     end, never later than learned_at; with no end at hand (no episode, none of this agent's, not ended) learned_at,
     the time of the write, as the writer does. The correction extractor records no episode on its rows, so they all
     take learned_at.
  c. The knowledge extractor (knowledge_extractor), written at compaction: learned_at.
  d. Merged facts: the latest COALESCE(mentioned_at, learned_at) of their sources (facts.latest_statement_time), read
     after rules a-c. A cluster merge (cluster_consolidation) takes its sources from the one f027_cluster_merge event
     that names it as merged_fact_id; a contradiction merge (contradiction_resolution) only when exactly two facts
     stamped 'merged' point at it. A merge waits while one of its sources is a merge this run can still fill, and the
     rule repeats until a pass fills nothing (a merge of a merge). A merge whose sources cannot be established stays
     NULL, and so does one with a source learned after it (sources pointed at an older fact they duplicated).
  Stays NULL, counted under its reason: sleep_reflection (the episodes it reflected on are not recorded on the row),
  supersede (a REST edit or a sleep UPDATES replacement), an MCP teach call (mcp, mcp:...), the review rule
  (a2ui_review), the rubric proposal (f024_phase3b), exemplar_extractor (the live leg stores the time of its write,
  which is false for a row the exemplar backfill script wrote long after, and no column tells their rows apart), and
  any other source.

Two tiers. The default tier writes only fills equal to the row's learned_at (rules a and c, rule b with no episode end
at hand): a NULL already reads as learned_at (COALESCE(mentioned_at, learned_at)), so no reader's result or ordering
changes. The second tier, written only with --reordering-rules, holds every fill earlier than learned_at: the
extractors' episode start, the session reflection's episode end, merged facts. These change how pairs of facts order
under the recency resolvers: an extractor fill reads earlier than every in-turn fact of its episode, even one said
before the extracted statement, and ties with the rows migration 097 stamped with that episode's start. The dry run
reports both tiers per rule and per source, split by active and inactive, and how much earlier the second tier's rows
would read (median and maximum).

Side effects of --apply, in either tier:
  - updated_at moves to the time of the write on every row written (the set_updated_at trigger, never disabled here).
    Two readers: the heartbeat's stale-facts finding ("N facts not accessed in 30+ days", active facts with updated_at
    older than 30 days) leaves the written rows out for 30 days; the rollback and the chain report of
    scripts/backfill_supersession.py select superseded_by IS NOT NULL AND updated_at >= a watermark, so its rollback
    with a watermark older than this run would reactivate the written rows that have a superseded_by.
  - Entity profiles (F119) whose sources include a written fact become due, because their digest covers mentioned_at;
    the sleep phase rewrites them within its daily limit of model calls (NOUS_ENTITY_PROFILE_MAX_CALLS_PER_DAY). The
    dry run counts them per tier.

Record and undo. --apply needs --manifest PATH, a new file: before each batch commits, one JSON line per row it writes
(agent_id, id, rule, mentioned_at, learned_at) is written and flushed to disk. A run that stops prints what it
committed and where the manifest is; a batch that failed after its lines were written leaves them there, with its rows
still NULL.
--undo-from PATH sets mentioned_at back to NULL for the agent's rows the manifest lists, only where the stored value
still equals the manifest's, dry run unless --apply, and counts the rows it skips. It cannot undo: the updated_at move
(it moves again), the profiles made due (due again), a time that a merge written in between inherited from a filled
row, or a deactivation the app made on a filled time.

Mechanics. A dry run reads one snapshot in a READ ONLY transaction and writes nothing. --apply (and --undo-from with
--apply) writes in batches of --batch-size, each a short transaction of its own with lock_timeout 5 s: it locks the
batch's rows (FOR NO KEY UPDATE, in id order), hashes each row without mentioned_at and updated_at, updates
mentioned_at with its own guard (still NULL; for an undo, still the manifest's value), hashes again and rolls the
batch back and stops if any hash differs. No trigger is disabled and no table lock is taken. Every other column,
search_tsv and the embedding included, is unchanged. A run that stops keeps the batches it committed; the next run
plans again from what is stored. A second --apply finds nothing.

Usage, from the repository root (or with the nous package importable)::

    uv run python -m scripts.backfill_mentioned_at --agent-id nous-default
    uv run python -m scripts.backfill_mentioned_at --agent-id nous-default --apply --manifest said-1.jsonl --limit 50
    uv run python -m scripts.backfill_mentioned_at --agent-id nous-default --apply --manifest said-2.jsonl
    uv run python -m scripts.backfill_mentioned_at --agent-id nous-default --undo-from said-2.jsonl [--apply]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import IO
from uuid import UUID

from sqlalchemy import text

from nous.config import Settings
from nous.heart.facts import latest_statement_time
from nous.storage.database import Database

IN_TURN = frozenset({"user_direct", "inline_correction", "pre_prune_extraction"})
EPISODE_START = frozenset({"fact_extractor", "episode_summarizer", "enumerative_extractor"})
EPISODE_END = frozenset({"reflection", "correction_extraction"})
AT_COMPACTION = frozenset({"knowledge_extractor"})
CLUSTER_MERGE, CONTRADICTION_MERGE = "cluster_consolidation", "contradiction_resolution"

RULE_TURN = "a: turn time (learned_at)"
RULE_START = "b: episode start"
RULE_END = "b: episode end, at most learned_at"
RULE_NO_END = "b: no episode end at hand (learned_at)"
RULE_COMPACTION = "c: time of the write at compaction (learned_at)"
RULE_MERGE = "d: latest statement time of the merge's sources"

KEPT = {
    "sleep_reflection": "sleep reflection: the reflected episodes are not recorded on the row",
    "supersede": "supersede replacement (REST edit, sleep UPDATES): no time recorded",
    "a2ui_review": "review rule: no time in reach",
    "f024_phase3b": "rubric proposal: no time in reach",
    "exemplar_extractor": "exemplar: two writers with different rules, no column tells their rows apart",
}
KEPT_MCP = "MCP teach call: no time in reach"
KEPT_NO_RULE = "no rule for this source"
KEPT_NO_EPISODE = "extractor row without a source episode"
KEPT_EPISODE_NOT_FOUND = "source episode is not one of this agent's"
KEPT_NO_EVENT = "cluster merge: no merge event names it"
KEPT_EVENTS = "cluster merge: more than one merge event names it"
KEPT_BAD_LIST = "cluster merge: a listed source is not a fact of this agent"
KEPT_NOT_TWO = "contradiction merge: not exactly two merged sources point at it"
KEPT_SOURCE_LATER = "merge: a source was learned after it, so it was not written from them"
KEPT_LATER = "the value would be later than the row's learned_at"
KEPT_WAITING = "merge: a source merge got no time"

SAMPLE = 5
LOCK_TIMEOUT = "5s"
CANNOT_UNDO = (
    "An undo cannot take back: the updated_at move (it moves again), the entity profiles made due (due again), a time "
    "that a merge written in between inherited from a filled row, or a deactivation the app made on a filled time."
)

Fill = tuple[UUID, datetime, str]  # (fact id, value, rule)


@dataclass(frozen=True)
class FactRow:
    id: UUID
    source: str | None
    category: str | None
    learned_at: datetime
    mentioned_at: datetime | None
    source_episode_id: UUID | None
    superseded_by: UUID | None
    deactivation_reason: str | None
    active: bool


@dataclass
class Plan:
    """Every row without mentioned_at, either filled (in write order) or kept NULL with a reason."""

    facts: dict[UUID, FactRow]
    fills: list[Fill] = field(default_factory=list)  # sources before the merges that read them
    kept: list[tuple[UUID, str]] = field(default_factory=list)  # (id, reason)
    merge_pass: dict[UUID, int] = field(default_factory=dict)  # merge id -> the pass of rule d that filled it

    def changes_nothing(self, fill: Fill) -> bool:
        """A fill equal to learned_at: a NULL already reads as learned_at, so no reader sees a change."""
        return fill[1] == self.facts[fill[0]].learned_at

    def selected(self, reordering: bool) -> list[Fill]:
        """What --apply writes: the default tier, or with --reordering-rules every fill."""
        return [f for f in self.fills if reordering or self.changes_nothing(f)]


@dataclass
class Report:
    agent_id: str
    apply: bool
    reordering: bool
    plan: Plan
    nulls_before: Counter
    profiles: tuple[int, int] | None = None  # profiles made due: (default tier, with --reordering-rules)
    manifest: str | None = None
    planned: int = 0  # rows this run set out to write
    batches: int = 0
    written: int = 0
    error: str | None = None
    nulls_after: Counter | None = None
    rows_hash_before: str | None = None
    rows_hash_after: str | None = None
    agent_hash_before: str | None = None
    agent_hash_after: str | None = None


@dataclass
class UndoReport:
    agent_id: str
    apply: bool
    manifest: str
    lines: int = 0
    other_agent: int = 0  # lines of another agent, left alone
    matching: int = 0  # rows whose stored value still equals the manifest's
    changed: int = 0  # rows whose stored value differs (NULL included)
    missing: int = 0  # ids that are no fact of this agent
    batches: int = 0
    undone: int = 0
    error: str | None = None


def _direct(
    f: FactRow, episodes: dict[UUID, tuple[datetime, datetime | None]]
) -> tuple[str | None, datetime | str | None]:
    """Rules a-c for one row: (rule, value) to fill, (None, reason) to keep NULL, ("merge", None) for rule d."""
    source = f.source or ""
    if source == "mcp" or source.startswith("mcp:"):
        return None, KEPT_MCP
    if source in IN_TURN or (source.endswith("-attachment") and f.category == "attachment"):
        return RULE_TURN, f.learned_at
    if source in AT_COMPACTION:
        return RULE_COMPACTION, f.learned_at
    episode = episodes.get(f.source_episode_id) if f.source_episode_id is not None else None
    if source in EPISODE_START:
        if f.source_episode_id is None:
            return None, KEPT_NO_EPISODE
        if episode is None:
            return None, KEPT_EPISODE_NOT_FOUND
        return RULE_START, episode[0]
    if source in EPISODE_END:
        ended = episode[1] if episode is not None else None
        if ended is None:
            return RULE_NO_END, f.learned_at
        return RULE_END, min(ended, f.learned_at)
    if source in (CLUSTER_MERGE, CONTRADICTION_MERGE):
        return "merge", None
    return None, KEPT.get(source, KEPT_NO_RULE)


def _merge_sources(
    m: FactRow, facts: dict[UUID, FactRow], cluster_lists: dict[UUID, list[list[str]]], merged_into: dict
) -> tuple[list[UUID] | None, str | None]:
    """The source list the merge recorded, or (None, reason) when it cannot be established."""
    if m.source == CONTRADICTION_MERGE:
        ids = merged_into.get(m.id, [])
        if len(ids) != 2:
            return None, KEPT_NOT_TWO
    else:
        lists = cluster_lists.get(m.id, [])
        if not lists:
            return None, KEPT_NO_EVENT
        if len(lists) > 1:
            return None, KEPT_EVENTS
        try:
            ids = [UUID(s) for s in lists[0]]
        except (TypeError, ValueError):
            return None, KEPT_BAD_LIST
        if not ids or any(i not in facts for i in ids):
            return None, KEPT_BAD_LIST
    # A merge writes its fact after its sources. A source learned later means the sources were pointed at an older
    # fact whose content they duplicated: that fact was not written from them.
    if any(facts[i].learned_at > m.learned_at for i in ids):
        return None, KEPT_SOURCE_LATER
    return ids, None


def plan_fills(
    facts: dict[UUID, FactRow],
    episodes: dict[UUID, tuple[datetime, datetime | None]],
    cluster_lists: dict[UUID, list[list[str]]],
) -> Plan:
    """Classify every row of ``facts`` (one agent's) without mentioned_at. Pure: no database."""
    plan = Plan(facts=facts)
    value: dict[UUID, datetime] = {}
    merges: list[FactRow] = []
    for f in sorted((f for f in facts.values() if f.mentioned_at is None), key=lambda f: (f.learned_at, str(f.id))):
        rule, result = _direct(f, episodes)
        if rule == "merge":
            merges.append(f)
        elif rule is not None and isinstance(result, datetime):
            if result > f.learned_at:
                plan.kept.append((f.id, KEPT_LATER))
            else:
                plan.fills.append((f.id, result, rule))
                value[f.id] = result
        else:
            plan.kept.append((f.id, str(result)))

    merged_into: dict[UUID, list[UUID]] = defaultdict(list)
    for f in facts.values():
        if f.superseded_by is not None and f.deactivation_reason == "merged":
            merged_into[f.superseded_by].append(f.id)
    pending: dict[UUID, list[UUID]] = {}
    for m in merges:
        sources, reason = _merge_sources(m, facts, cluster_lists, merged_into)
        if sources is None:
            plan.kept.append((m.id, str(reason)))
        else:
            pending[m.id] = sources

    def stated(s: UUID) -> datetime:
        return value.get(s) or facts[s].mentioned_at or facts[s].learned_at

    passes = 0
    while True:
        # A source that is a merge this run can still fill decides the time; wait for it.
        ready = sorted(
            (mid for mid, sources in pending.items() if not any(s in pending for s in sources)),
            key=lambda mid: (facts[mid].learned_at, str(mid)),
        )
        if not ready:
            break
        passes += 1
        for mid in ready:  # none of them is a source of another: each waits for its pending sources
            when = latest_statement_time(stated(s) for s in pending.pop(mid))
            assert when is not None  # a source list is never empty and learned_at is NOT NULL
            if when > facts[mid].learned_at:
                plan.kept.append((mid, KEPT_LATER))
                continue
            plan.fills.append((mid, when, RULE_MERGE))
            plan.merge_pass[mid] = passes
            value[mid] = when
    plan.kept.extend((mid, KEPT_WAITING) for mid in pending)  # a cycle of merges, or a merge waiting on one
    return plan


# --- database -------------------------------------------------------------------------------------------------------

_ROW_HASH = "md5((to_jsonb(f) - 'mentioned_at' - 'updated_at')::text)"
# The whole-agent hash also leaves out what the app moves on old rows in normal use: a recall touch, a confirmation.
_AGENT_HASH = (
    "md5((to_jsonb(f) - 'mentioned_at' - 'updated_at' - 'recall_count' - 'last_recalled_at' - 'confirmation_count' "
    "- 'last_confirmed')::text)"
)
_PAIRS = "(SELECT unnest(CAST(:ids AS uuid[])) AS id, unnest(CAST(:ts AS timestamptz[])) AS t) AS v"

_LOCK_FILL = text(
    f"SELECT f.id, {_ROW_HASH} FROM heart.facts f JOIN {_PAIRS} ON f.id = v.id "
    "WHERE f.agent_id = :a AND f.mentioned_at IS NULL ORDER BY f.id FOR NO KEY UPDATE OF f"
)
_UPDATE_FILL = text(
    f"UPDATE heart.facts f SET mentioned_at = v.t FROM {_PAIRS} "
    f"WHERE f.id = v.id AND f.agent_id = :a AND f.mentioned_at IS NULL RETURNING f.id, {_ROW_HASH}"
)
_LOCK_UNDO = text(
    f"SELECT f.id, {_ROW_HASH} FROM heart.facts f JOIN {_PAIRS} ON f.id = v.id "
    "WHERE f.agent_id = :a AND f.mentioned_at = v.t ORDER BY f.id FOR NO KEY UPDATE OF f"
)
_UPDATE_UNDO = text(
    f"UPDATE heart.facts f SET mentioned_at = NULL FROM {_PAIRS} "
    f"WHERE f.id = v.id AND f.agent_id = :a AND f.mentioned_at = v.t RETURNING f.id, {_ROW_HASH}"
)


async def _read(session, agent_id: str) -> tuple[dict, dict, dict]:
    rows = await session.execute(
        text(
            "SELECT id, source, category, learned_at, mentioned_at, source_episode_id, superseded_by, "
            "deactivation_reason, COALESCE(active, true) AS active FROM heart.facts WHERE agent_id = :a"
        ),
        {"a": agent_id},
    )
    facts = {r.id: FactRow(**r._mapping) for r in rows}
    wanted = [
        f.source_episode_id
        for f in facts.values()
        if f.mentioned_at is None and f.source_episode_id is not None and f.source in EPISODE_START | EPISODE_END
    ]
    episodes: dict[UUID, tuple[datetime, datetime | None]] = {}
    if wanted:
        rows = await session.execute(
            text(
                "SELECT id, started_at, ended_at FROM heart.episodes "
                "WHERE agent_id = :a AND id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"a": agent_id, "ids": list(set(wanted))},
        )
        episodes = {r.id: (r.started_at, r.ended_at) for r in rows}
    products = [str(f.id) for f in facts.values() if f.mentioned_at is None and f.source == CLUSTER_MERGE]
    cluster_lists: dict[UUID, list[list[str]]] = defaultdict(list)
    if products:
        rows = await session.execute(
            text(
                "SELECT data->>'merged_fact_id' AS merged, ARRAY(SELECT jsonb_array_elements_text("
                "CASE WHEN jsonb_typeof(data->'source_fact_ids') = 'array' THEN data->'source_fact_ids' "
                "ELSE '[]'::jsonb END)) AS sources FROM nous_system.events "
                "WHERE agent_id = :a AND event_type = 'f027_cluster_merge' "
                "AND data->>'merged_fact_id' = ANY(CAST(:ids AS text[]))"
            ),
            {"a": agent_id, "ids": products},
        )
        for r in rows:
            cluster_lists[UUID(r.merged)].append(list(r.sources))
    return facts, episodes, cluster_lists


async def _profiles_touched(session, agent_id: str, plan: Plan) -> tuple[int, int] | None:
    """Entity profiles (F119, status 'profile') whose key has an active, unsuperseded fact the default tier, then both
    tiers, would write: an upper bound of the profiles made due. None when the database has no entity_profile table."""
    if await session.scalar(text("SELECT to_regclass('nous_system.entity_profile')")) is None:
        return None
    counts = []
    for reordering in (False, True):
        ids = [
            fid
            for fid, _v, _r in plan.selected(reordering)
            if plan.facts[fid].active and plan.facts[fid].superseded_by is None
        ]
        counts.append(
            await session.scalar(
                text(
                    "SELECT count(DISTINCT p.entity_key) FROM nous_system.entity_profile p "
                    "JOIN heart.fact_entity_keys k ON k.agent_id = p.agent_id AND k.entity_key = p.entity_key "
                    "WHERE p.agent_id = :a AND p.active AND p.status = 'profile' "
                    "AND k.fact_id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"a": agent_id, "ids": ids},
            )
            if ids
            else 0
        )
    return counts[0], counts[1]


async def _agent_hash(session, agent_id: str) -> str:
    return await session.scalar(
        text(
            f"SELECT md5(COALESCE(string_agg({_AGENT_HASH}, '' ORDER BY f.id), '')) FROM heart.facts f "
            "WHERE f.agent_id = :a"
        ),
        {"a": agent_id},
    )


async def _nulls(session, agent_id: str) -> Counter:
    rows = await session.execute(
        text(
            "SELECT COALESCE(source, '(none)') AS source, count(*) AS n FROM heart.facts "
            "WHERE agent_id = :a AND mentioned_at IS NULL GROUP BY 1"
        ),
        {"a": agent_id},
    )
    return Counter({r.source: r.n for r in rows})


def _digest(hashes: dict[UUID, str]) -> str:
    return hashlib.md5("".join(hashes[i] for i in sorted(hashes, key=str)).encode()).hexdigest()


def _record(out: IO[str], lines: list[dict]) -> None:
    """Append manifest lines and force them to disk: called before the batch they describe commits."""
    for line in lines:
        out.write(json.dumps(line) + "\n")
    out.flush()
    os.fsync(out.fileno())


async def _batches(
    db: Database,
    agent_id: str,
    pairs: list[tuple[UUID, datetime]],
    lock,
    update,
    batch_size: int,
    before_commit: Callable[[set[UUID]], None],
) -> tuple[int, int, dict, dict, str | None]:
    """Write ``pairs`` (id, time) with ``update`` in batches, each a short transaction: lock the rows, hash them,
    update, hash again, roll back on any difference. Returns (batches committed, rows written, hashes before, hashes
    after, the error that stopped it or None)."""
    before: dict[UUID, str] = {}
    after: dict[UUID, str] = {}
    batches = written = 0
    for start in range(0, len(pairs), batch_size):
        batch = pairs[start : start + batch_size]
        params = {"a": agent_id, "ids": [i for i, _t in batch], "ts": [t for _i, t in batch]}
        try:
            async with db.session() as session:
                await session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
                locked = dict((await session.execute(lock, params)).all())
                changed = dict((await session.execute(update, params)).all())
                if changed != locked:
                    await session.rollback()
                    raise RuntimeError("a column other than mentioned_at and updated_at differs after the update")
                before_commit(set(changed))
                await session.commit()
        except Exception as exc:  # stop here; the caller reports what was committed
            return (
                batches,
                written,
                before,
                after,
                f"batch {batches + 1} (rows {start + 1}-{start + len(batch)} of {len(pairs)}) rolled back: "
                f"{type(exc).__name__}: {(str(exc).splitlines() or [''])[0]}",  # a database error's first line
            )
        batches += 1
        written += len(changed)
        before.update(locked)
        after.update(changed)
    return batches, written, before, after, None


async def run(
    db: Database,
    agent_id: str,
    *,
    apply: bool,
    manifest: str | None = None,
    reordering: bool = False,
    limit: int | None = None,
    batch_size: int = 100,
) -> Report:
    """Plan from one read-only snapshot; with ``apply``, write the selected tier (its first ``limit`` rows) in batches,
    each recorded in ``manifest`` before it commits."""
    if apply and not manifest:
        raise ValueError("--apply needs --manifest: the record of every row it writes")
    async with db.session() as session:
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        facts, episodes, cluster_lists = await _read(session, agent_id)
        plan = plan_fills(facts, episodes, cluster_lists)
        report = Report(
            agent_id=agent_id,
            apply=apply,
            reordering=reordering,
            plan=plan,
            nulls_before=await _nulls(session, agent_id),
        )
        report.profiles = await _profiles_touched(session, agent_id, plan)
        if apply:
            report.agent_hash_before = await _agent_hash(session, agent_id)
        await session.rollback()
    if not apply:
        return report

    todo = plan.selected(reordering)[:limit]
    report.manifest, report.planned = manifest, len(todo)
    rule_of = {fid: rule for fid, _v, rule in todo}
    value_of = {fid: value for fid, value, _r in todo}
    with open(manifest, "x", encoding="utf-8") as out:  # a new file: never overwrite an earlier run's record

        def record(ids: set[UUID]) -> None:
            _record(
                out,
                [
                    {
                        "agent_id": agent_id,
                        "id": str(fid),
                        "rule": rule_of[fid],
                        "mentioned_at": value_of[fid].isoformat(),
                        "learned_at": facts[fid].learned_at.isoformat(),
                    }
                    for fid in sorted(ids, key=str)
                ],
            )

        report.batches, report.written, before, after, report.error = await _batches(
            db, agent_id, [(fid, v) for fid, v, _r in todo], _LOCK_FILL, _UPDATE_FILL, batch_size, record
        )
    report.rows_hash_before, report.rows_hash_after = _digest(before), _digest(after)
    if report.error is None:
        async with db.session() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            report.nulls_after = await _nulls(session, agent_id)
            report.agent_hash_after = await _agent_hash(session, agent_id)
            await session.rollback()
    return report


def _read_manifest(path: str, agent_id: str) -> tuple[dict[UUID, datetime], int, int]:
    """The manifest's rows of ``agent_id`` (id -> value written), its line count and its lines of another agent."""
    rows: dict[UUID, datetime] = {}
    lines = other = 0
    with open(path, encoding="utf-8") as f:
        for raw in f:
            if not raw.strip():
                continue
            lines += 1
            entry = json.loads(raw)
            if entry["agent_id"] != agent_id:
                other += 1
                continue
            rows[UUID(entry["id"])] = datetime.fromisoformat(entry["mentioned_at"])
    return rows, lines, other


async def undo(db: Database, agent_id: str, manifest: str, *, apply: bool, batch_size: int = 100) -> UndoReport:
    """Set mentioned_at back to NULL for the agent's rows ``manifest`` lists, where the stored value still equals it."""
    rows, lines, other = _read_manifest(manifest, agent_id)
    report = UndoReport(agent_id=agent_id, apply=apply, manifest=manifest, lines=lines, other_agent=other)
    async with db.session() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        result = await session.execute(
            text("SELECT id, mentioned_at FROM heart.facts WHERE agent_id = :a AND id = ANY(CAST(:ids AS uuid[]))"),
            {"a": agent_id, "ids": list(rows)},
        )
        stored = {r.id: r.mentioned_at for r in result}
        await session.rollback()
    matching = sorted(
        ((fid, v) for fid, v in rows.items() if fid in stored and stored[fid] == v), key=lambda p: str(p[0])
    )
    report.matching = len(matching)
    report.missing = sum(1 for fid in rows if fid not in stored)
    report.changed = len(rows) - report.matching - report.missing
    if apply:
        report.batches, report.undone, _before, _after, report.error = await _batches(
            db, agent_id, matching, _LOCK_UNDO, _UPDATE_UNDO, batch_size, lambda _ids: None
        )
    return report


# --- output ---------------------------------------------------------------------------------------------------------


def _span(seconds: float) -> str:
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d {hours}h {minutes:02d}m"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m" if minutes else f"{int(seconds)}s"


def format_report(report: Report, limit: int | None = None, calls_per_day: int | None = None) -> list[str]:
    plan = report.plan
    facts = plan.facts

    def split(ids) -> str:
        ids = list(ids)
        active = sum(1 for i in ids if facts[i].active)
        return f"active {active}, inactive {len(ids) - active}"

    def samples(ids) -> str:
        return ", ".join(str(i) for i in list(ids)[:SAMPLE])

    def by_rule(fills: list[Fill], earlier: bool) -> list[str]:
        groups: dict[str, list[Fill]] = defaultdict(list)
        for fill in fills:
            groups[fill[2]].append(fill)
        lines = []
        for rule, group in sorted(groups.items()):
            line = f"  {rule}: {len(group)} ({split(f[0] for f in group)})"
            if earlier:
                moves = [(facts[fid].learned_at - value).total_seconds() for fid, value, _r in group]
                line += f", earlier than today by median {_span(statistics.median(moves))}, max {_span(max(moves))}"
            lines.append(f"{line}  e.g. {samples(f[0] for f in group)}")
        return lines or ["  none"]

    default = [f for f in plan.fills if plan.changes_nothing(f)]
    second = [f for f in plan.fills if not plan.changes_nothing(f)]
    null_ids = [f[0] for f in plan.fills] + [fid for fid, _r in plan.kept]
    mode = "--apply" if report.apply else "dry run, nothing written"
    out = [
        f"backfill mentioned_at, agent {report.agent_id}: {mode}",
        f"facts without mentioned_at: {len(null_ids)} of {len(facts)} ({split(null_ids)})",
        f"planned: default tier {len(default)}, second tier {len(second)}, stay NULL {len(plan.kept)}",
        "",
        "default tier (--apply): fills equal to learned_at. A NULL already reads as learned_at, so no reader's result "
        "or ordering changes.",
        *by_rule(default, earlier=False),
        "",
        "second tier (only with --apply --reordering-rules): fills earlier than learned_at. They change how pairs of "
        "facts order under the recency resolvers: an extractor fill reads earlier than every in-turn fact of its "
        "episode, even one said before the extracted statement, and ties with rows migration 097 stamped with that "
        "episode's start.",
        *by_rule(second, earlier=True),
    ]
    if plan.merge_pass:
        passes = max(plan.merge_pass.values())
        out.append(f"  (rule d filled merges in {passes} pass{'' if passes == 1 else 'es'})")

    reasons: dict[str, list[UUID]] = defaultdict(list)
    for fid, reason in plan.kept:
        reasons[reason].append(fid)
    out += ["", "stays NULL, by reason:"]
    out += [f"  {r}: {len(ids)} ({split(ids)})  e.g. {samples(ids)}" for r, ids in sorted(reasons.items())] or [
        "  none"
    ]

    per_source: dict[str, list[list[UUID]]] = defaultdict(lambda: [[], [], []])
    for tier, fills in enumerate((default, second)):
        for fid, _v, _r in fills:
            per_source[facts[fid].source or "(none)"][tier].append(fid)
    for fid, _r in plan.kept:
        per_source[facts[fid].source or "(none)"][2].append(fid)
    out += ["", "by source: default tier | second tier | stays NULL"]
    for source in sorted(per_source):
        cells = " | ".join(f"{len(ids)} ({split(ids)})" if ids else "0" for ids in per_source[source])
        out.append(f"  {source}: {cells}")

    limit_word = f" (NOUS_ENTITY_PROFILE_MAX_CALLS_PER_DAY, {calls_per_day})" if calls_per_day else ""
    out += [
        "",
        "side effects of --apply, in either tier:",
        "  - updated_at moves to the time of the write on every row written (the set_updated_at trigger). Its two "
        "readers: the heartbeat's stale-facts finding ('N facts not accessed in 30+ days', active facts with "
        "updated_at older than 30 days) leaves those rows out for 30 days; the rollback and the chain report of "
        "scripts/backfill_supersession.py select superseded_by IS NOT NULL AND updated_at >= a watermark, so its "
        "rollback with a watermark older than this run would reactivate the written rows that have a superseded_by.",
    ]
    if report.profiles is None:
        out.append("  - entity profiles: this database has no entity_profile table, so none becomes due.")
    else:
        out.append(
            f"  - entity profiles that become due (their digest covers mentioned_at): at most {report.profiles[0]} "
            f"with the default tier, at most {report.profiles[1]} with --reordering-rules. The sleep phase rewrites "
            f"them within its daily limit of model calls{limit_word}."
        )
    if not report.apply:
        cut = "" if limit is None else f", at most {limit} with --limit {limit}"
        out += [
            "",
            f"--apply --manifest PATH would write the default tier, {len(default)} rows; with --reordering-rules both "
            f"tiers, {len(plan.fills)} rows{cut}. Each row written is listed in the manifest; --undo-from PATH sets "
            "them back.",
        ]
        return out

    tier = "both tiers" if report.reordering else "the default tier"
    out.append("")
    if report.error is not None:
        out += [
            f"STOPPED: {report.error}",
            f"Committed before it: {report.batches} batches, {report.written} of {report.planned} rows ({tier}), "
            f"listed in the manifest {report.manifest}. If the failed batch got as far as its manifest lines, they "
            "are there too; its rows are still NULL, and --undo-from skips them. Run the dry run again, then --apply "
            "with a new --manifest path.",
        ]
        return out
    if not plan.fills:
        out.append("nothing to fill: every row a rule can fill already has mentioned_at; nothing written")
        return out
    if not report.planned:
        out.append(f"nothing to fill in {tier}; nothing written (--reordering-rules writes the second tier)")
        return out
    out.append(
        f"wrote {report.written} of {report.planned} rows ({tier}) in {report.batches} batches; each is listed in the "
        f"manifest {report.manifest}"
    )
    out.append("rows without mentioned_at per source, before -> after:")
    after = report.nulls_after or Counter()
    for source in sorted(set(report.nulls_before) | set(after)):
        out.append(f"  {source}: {report.nulls_before[source]} -> {after[source]}")
    out.append(
        f"hash of the written rows without mentioned_at and updated_at: before {report.rows_hash_before}, "
        f"after {report.rows_hash_after} (each batch is checked row by row and rolled back on a difference)"
    )
    note = (
        "equal"
        if report.agent_hash_before == report.agent_hash_after
        else "differs: another writer changed this agent's facts during the run; the per-batch check covers the "
        "rows written here"
    )
    out.append(
        "hash of all this agent's facts without mentioned_at, updated_at and the recall and confirmation counters: "
        f"before {report.agent_hash_before}, after {report.agent_hash_after} ({note})"
    )
    return out


def format_undo(report: UndoReport) -> list[str]:
    mode = "--apply" if report.apply else "dry run, nothing written (--apply writes)"
    out = [
        f"undo mentioned_at from {report.manifest}, agent {report.agent_id}: {mode}",
        f"manifest lines: {report.lines}; of another agent, left alone: {report.other_agent}",
        f"rows whose stored value still equals the manifest's: {report.matching}",
        f"skipped, the stored value changed since (NULL included): {report.changed}",
        f"skipped, not a fact of this agent: {report.missing}",
    ]
    if report.apply:
        if report.error is not None:
            out.append(f"STOPPED: {report.error}")
        out.append(
            f"set back to NULL: {report.undone} rows in {report.batches} batches; skipped because they changed during "
            f"the undo: {report.matching - report.undone if report.error is None else 'not counted after the stop'}"
        )
    out.append(CANNOT_UNDO)
    return out


async def main_async(args: argparse.Namespace) -> int:
    settings = Settings().model_copy(update={"agent_id": args.agent_id})
    db = Database(settings)
    await db.connect()
    try:
        if args.undo_from:
            undone = await undo(db, args.agent_id, args.undo_from, apply=args.apply, batch_size=args.batch_size)
            lines, failed = format_undo(undone), undone.error is not None
        else:
            report = await run(
                db,
                args.agent_id,
                apply=args.apply,
                manifest=args.manifest,
                reordering=args.reordering_rules,
                limit=args.limit,
                batch_size=args.batch_size,
            )
            calls = getattr(settings, "entity_profile_max_calls_per_day", None)
            lines, failed = format_report(report, args.limit, calls), report.error is not None
    finally:
        await db.disconnect()
    print("\n".join(lines))
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agent-id", "--agent", dest="agent_id", required=True, help="Agent whose facts to fill.")
    parser.add_argument("--apply", action="store_true", help="Write (default: dry run, nothing written).")
    parser.add_argument(
        "--manifest", default=None, help="With --apply: a new file that records every row written (JSON lines)."
    )
    parser.add_argument(
        "--reordering-rules",
        action="store_true",
        help="Also write the second tier: fills earlier than learned_at, which change how facts order.",
    )
    parser.add_argument("--limit", type=int, default=None, help="With --apply, write only the first N selected rows.")
    parser.add_argument("--batch-size", type=int, default=100, help="Rows per transaction (default 100).")
    parser.add_argument(
        "--undo-from",
        default=None,
        metavar="MANIFEST",
        help="Set mentioned_at back to NULL for the agent's rows a manifest lists, where the stored value still "
        "equals it (dry run unless --apply). " + CANNOT_UNDO,
    )
    args = parser.parse_args()
    if args.undo_from and (args.manifest or args.reordering_rules or args.limit is not None):
        parser.error("--undo-from takes only --agent-id, --apply and --batch-size")
    if not args.undo_from and args.apply and not args.manifest:
        parser.error("--apply needs --manifest PATH, a new file that records every row written")
    if args.manifest and not args.apply:
        parser.error("--manifest is written only with --apply")
    if args.manifest and os.path.exists(args.manifest):
        parser.error(f"{args.manifest} exists: give a new path, an earlier run's record is never overwritten")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
