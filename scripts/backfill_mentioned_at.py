"""Fill heart.facts.mentioned_at (when a fact was said) on rows written before every writer recorded it.

One-off and optional: running it is an operator's decision. Dry run unless --apply. Agent-scoped. Only rows whose
mentioned_at IS NULL are touched, active and inactive alike; a row with a value is never changed, and a row with no
truthful source stays NULL. Each rule stores what the writer of that source stores now (FactInput.mentioned_at):

  a. Writers inside the turn: learned_at, their insert time in that turn. Sources user_direct (the learn_fact tool and
     run_python's learn_fact), inline_correction, pre_prune_extraction, and the attachment fact (category
     'attachment', source '<channel>-attachment').
  b. The fact, episode-summary and enumerative extractors (fact_extractor, episode_summarizer, enumerative_extractor):
     the start of the source episode, joined on id and agent; with no such episode the row stays NULL.
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
     NULL.
  Stays NULL, counted under its reason: sleep_reflection (the episodes it reflected on are not recorded on the row),
  supersede (a REST edit or a sleep UPDATES replacement), an MCP teach call (mcp, mcp:...), the review rule
  (a2ui_review), the rubric proposal (f024_phase3b), exemplar_extractor (the live leg stores the time of its write,
  the exemplar backfill script stores none, and no column tells their rows apart), and any other source.

What readers see: a rule a or c fill, and a rule b fill with no end at hand, equals learned_at, which readers already
use (COALESCE(mentioned_at, learned_at)), so no recency ordering changes for those rows. A fill from an episode or
from merge sources can read earlier than before; the dry run counts both kinds. Any fill of an active, unsuperseded
fact changes the digest of the entity profiles whose facts include it (the digest covers mentioned_at), so the sleep
phase writes those profiles again, within NOUS_ENTITY_PROFILE_MAX_CALLS_PER_DAY; the dry run gives an upper bound.

Mechanics. The dry run reads one snapshot in a READ ONLY transaction and writes nothing. --apply writes the planned
rows in batches of --batch-size, each in a short transaction of its own with lock_timeout 5 s: it locks the batch's
rows, hashes each row without mentioned_at and updated_at, sets mentioned_at where it is still NULL, hashes again and
rolls the batch back and stops if any hash differs. No trigger is disabled and no table lock is taken. The
set_updated_at trigger moves updated_at of every written row to the time of the write, as for any UPDATE: the
heartbeat's stale-facts count (active facts with updated_at older than 30 days) leaves those rows out for 30 days, and
anything that selects facts by updated_at (the rollback query of scripts/backfill_supersession.py) sees them as
changed now. Every other column, search_tsv and the embedding included, is unchanged. A second --apply finds nothing.

Usage, from the repository root (or with the nous package importable)::

    uv run python -m scripts.backfill_mentioned_at --agent-id nous-default
    uv run python -m scripts.backfill_mentioned_at --agent-id nous-default --apply --limit 50
    uv run python -m scripts.backfill_mentioned_at --agent-id nous-default --apply
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
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
KEPT_WAITING = "merge: a source merge got no time"

SAMPLE = 5
LOCK_TIMEOUT = "5s"


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
    fills: list[tuple[UUID, datetime, str]] = field(default_factory=list)  # (id, value, rule), sources before merges
    kept: list[tuple[UUID, str]] = field(default_factory=list)  # (id, reason)
    merge_pass: dict[UUID, int] = field(default_factory=dict)  # merge id -> the pass of rule d that filled it

    def read_change(self) -> Counter:
        """How the value readers use (COALESCE(mentioned_at, learned_at)) moves for the planned fills."""
        moves: Counter = Counter()
        for fid, value, _rule in self.fills:
            learned = self.facts[fid].learned_at
            moves["unchanged" if value == learned else "earlier" if value < learned else "later"] += 1
        return moves


@dataclass
class Report:
    agent_id: str
    apply: bool
    plan: Plan
    nulls_before: Counter
    profiles: int | None = None
    written: int = 0
    nulls_after: Counter | None = None
    rows_hash_before: str | None = None
    rows_hash_after: str | None = None
    agent_hash_before: str | None = None
    agent_hash_after: str | None = None


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
        pointers = merged_into.get(m.id, [])
        return (pointers, None) if len(pointers) == 2 else (None, KEPT_NOT_TWO)
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
            plan.fills.append((mid, when, RULE_MERGE))
            plan.merge_pass[mid] = passes
            value[mid] = when
    plan.kept.extend((mid, KEPT_WAITING) for mid in pending)  # a cycle of merges, or a merge waiting on one
    return plan


# --- database -------------------------------------------------------------------------------------------------------

_ROW_HASH = "md5((to_jsonb(f) - 'mentioned_at' - 'updated_at')::text)"

_LOCK_BATCH = text(
    f"SELECT f.id, {_ROW_HASH} FROM heart.facts f "
    "WHERE f.agent_id = :a AND f.id = ANY(CAST(:ids AS uuid[])) AND f.mentioned_at IS NULL FOR UPDATE"
)
_UPDATE_BATCH = text(
    "UPDATE heart.facts f SET mentioned_at = v.t "
    "FROM (SELECT unnest(CAST(:ids AS uuid[])) AS id, unnest(CAST(:ts AS timestamptz[])) AS t) AS v "
    f"WHERE f.id = v.id AND f.agent_id = :a AND f.mentioned_at IS NULL RETURNING f.id, {_ROW_HASH}"
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


async def _profiles_touched(session, agent_id: str, plan: Plan) -> int | None:
    """Entity profiles (F119) whose key has an active, unsuperseded fact this plan fills: an upper bound of the
    profiles whose digest the fill changes. None when this database has no entity_profile table."""
    if await session.scalar(text("SELECT to_regclass('nous_system.entity_profile')")) is None:
        return None
    ids = [fid for fid, _v, _r in plan.fills if plan.facts[fid].active and plan.facts[fid].superseded_by is None]
    if not ids:
        return 0
    return await session.scalar(
        text(
            "SELECT count(DISTINCT p.entity_key) FROM nous_system.entity_profile p "
            "JOIN heart.fact_entity_keys k ON k.agent_id = p.agent_id AND k.entity_key = p.entity_key "
            "WHERE p.agent_id = :a AND p.active AND k.fact_id = ANY(CAST(:ids AS uuid[]))"
        ),
        {"a": agent_id, "ids": ids},
    )


async def _agent_hash(session, agent_id: str) -> str:
    """Every column of this agent's facts except mentioned_at and updated_at, hashed in id order."""
    return await session.scalar(
        text(
            f"SELECT md5(COALESCE(string_agg({_ROW_HASH}, '' ORDER BY f.id), '')) FROM heart.facts f "
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


async def run(db: Database, agent_id: str, *, apply: bool, limit: int | None = None, batch_size: int = 500) -> Report:
    """Plan from one read-only snapshot; with ``apply``, write the plan (its first ``limit`` rows) in batches."""
    async with db.session() as session:
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        facts, episodes, cluster_lists = await _read(session, agent_id)
        plan = plan_fills(facts, episodes, cluster_lists)
        report = Report(agent_id=agent_id, apply=apply, plan=plan, nulls_before=await _nulls(session, agent_id))
        report.profiles = await _profiles_touched(session, agent_id, plan)
        if apply:
            report.agent_hash_before = await _agent_hash(session, agent_id)
        await session.rollback()
    if not apply:
        return report

    todo = plan.fills if limit is None else plan.fills[:limit]
    before: dict[UUID, str] = {}
    after: dict[UUID, str] = {}
    for start in range(0, len(todo), batch_size):
        batch = todo[start : start + batch_size]
        params = {"a": agent_id, "ids": [fid for fid, _v, _r in batch]}
        async with db.session() as session:
            await session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
            locked = dict((await session.execute(_LOCK_BATCH, params)).all())
            written = dict((await session.execute(_UPDATE_BATCH, {**params, "ts": [v for _fid, v, _r in batch]})).all())
            if written != locked:
                await session.rollback()
                raise RuntimeError(
                    f"batch at row {start}: a column other than mentioned_at and updated_at differs after the "
                    "update; the batch was rolled back and nothing more is written"
                )
            await session.commit()
        before.update(locked)
        after.update(written)
        report.written += len(written)
    report.rows_hash_before, report.rows_hash_after = _digest(before), _digest(after)
    async with db.session() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        report.nulls_after = await _nulls(session, agent_id)
        report.agent_hash_after = await _agent_hash(session, agent_id)
        await session.rollback()
    return report


# --- output ---------------------------------------------------------------------------------------------------------


def format_report(report: Report, limit: int | None = None) -> list[str]:
    plan = report.plan
    source_of = {fid: plan.facts[fid].source or "(none)" for fid in plan.facts}
    null_rows = [plan.facts[fid] for fid, *_ in plan.fills] + [plan.facts[fid] for fid, _r in plan.kept]
    active = sum(1 for f in null_rows if f.active)
    mode = "--apply" if report.apply else "dry run, nothing written (--apply writes)"
    out = [
        f"backfill mentioned_at, agent {report.agent_id}: {mode}",
        f"facts without mentioned_at: {len(null_rows)} of {len(plan.facts)} "
        f"(active {active}, inactive {len(null_rows) - active})",
        f"planned: fill {len(plan.fills)}, stay NULL {len(plan.kept)}",
        "",
        "by source: fill / stay NULL",
    ]
    per_source: dict[str, Counter] = defaultdict(Counter)
    for fid, _v, rule in plan.fills:
        per_source[source_of[fid]][rule] += 1
    for fid, reason in plan.kept:
        per_source[source_of[fid]]["NULL: " + reason] += 1
    for source in sorted(per_source):
        counts = per_source[source]
        fill = sum(n for k, n in counts.items() if not k.startswith("NULL: "))
        detail = "; ".join(f"{k} {n}" for k, n in sorted(counts.items()))
        out.append(f"  {source}: {fill} / {sum(counts.values()) - fill}  ({detail})")

    def grouped(pairs) -> list[str]:
        groups: dict[str, list[UUID]] = defaultdict(list)
        for fid, key in pairs:
            groups[key].append(fid)
        lines = [
            f"  {key}: {len(ids)}  e.g. {', '.join(str(i) for i in ids[:SAMPLE])}"
            for key, ids in sorted(groups.items())
        ]
        return lines or ["  none"]

    out += ["", "fills by rule (first ids):"] + grouped((fid, rule) for fid, _v, rule in plan.fills)
    out += ["", "stays NULL by reason (first ids):"] + grouped(plan.kept)
    moves = plan.read_change()
    out += [
        "",
        f"the time readers use (COALESCE(mentioned_at, learned_at)): unchanged for {moves['unchanged']} fills (they "
        "equal learned_at, which readers already use, so no recency ordering changes for them; the entity profile "
        f"digest below still does), earlier for {moves['earlier']}, later for {moves['later']}",
    ]
    if plan.merge_pass:
        passes = max(plan.merge_pass.values())
        out.append(f"rule d filled merges in {passes} pass{'' if passes == 1 else 'es'}")
    if report.profiles is None:
        out.append("entity profiles: no entity_profile table in this database")
    else:
        out.append(
            f"entity profiles whose facts include a filled row: at most {report.profiles}; their digest changes, so "
            "the sleep phase writes them again, within NOUS_ENTITY_PROFILE_MAX_CALLS_PER_DAY"
        )
    out.append("--apply also moves updated_at of every row it writes to the time of the write (set_updated_at)")
    if not report.apply:
        if limit is not None:
            out.append(f"--apply --limit {limit} would write the first {min(limit, len(plan.fills))} of these")
        return out

    out.append("")
    if not plan.fills:
        out.append("nothing to fill: every row a rule can fill already has mentioned_at; nothing written")
        return out
    out.append(f"wrote {report.written} of {len(plan.fills)} planned rows")
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
        f"hash of all this agent's facts without mentioned_at and updated_at: before {report.agent_hash_before}, "
        f"after {report.agent_hash_after} ({note})"
    )
    return out


async def main_async(agent_id: str, *, apply: bool, limit: int | None, batch_size: int) -> None:
    settings = Settings().model_copy(update={"agent_id": agent_id})
    db = Database(settings)
    await db.connect()
    try:
        report = await run(db, agent_id, apply=apply, limit=limit, batch_size=batch_size)
    finally:
        await db.disconnect()
    print("\n".join(format_report(report, limit)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agent-id", "--agent", dest="agent_id", required=True, help="Agent whose facts to fill.")
    parser.add_argument("--apply", action="store_true", help="Write (default: dry run, nothing written).")
    parser.add_argument("--limit", type=int, default=None, help="With --apply, write only the first N planned rows.")
    parser.add_argument("--batch-size", type=int, default=500, help="Rows per transaction (default 500).")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    asyncio.run(main_async(args.agent_id, apply=args.apply, limit=args.limit, batch_size=args.batch_size))


if __name__ == "__main__":
    main()
