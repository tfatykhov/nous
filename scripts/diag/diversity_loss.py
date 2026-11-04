"""E-16/E-17: what the pre-turn context's diversity stage drops. Read-only.

``ContextEngine.build`` (nous/cognitive/context.py) runs ``_enforce_diversity`` on the
decision leg (key ``category``, cap 3) and the episode leg (key: first tag, cap 2). A
missing key becomes ``"unknown"``. Chat episodes are stored with no tags, so every chat
episode shares the ``"unknown"`` bucket and at most 2 survive (E-17). This script reads
the context-path retrieval traces (``nous_system.retrieval_log``, ``path = 'context'``) and
reports, per type:

- how many builds lose a candidate to diversity, and how many candidates;
- how many builds drop a candidate that scored above the lowest rendered one, compared at
  the diversity stage. "Same bucket" means a lower-scored item of the same bucket took the
  slot (an ordering effect); "cross bucket" means the cap itself cost the better item;
- how many drops the live relevance filter would keep if diversity dropped nothing (an
  estimate: conversation dedup and usage boost run in between and are not replayed);
- episodes: the tagless share, the share of drops in the ``"unknown"`` bucket, how many
  episodes a build renders, and how many drops a per-session key (cap 2) would keep. The
  session id is read from ``heart.episodes``: ``EpisodeSummary`` has no ``session_id``
  field, so a session key would need one;
- decisions: drops per category.

Scores are compared at the diversity stage, not at entry. A row whose context legs carry
their ``query`` was written by code that also records score changes over 1% as candidate
mutations; those rows use the recorded staleness / episode-recency / frame-boost mutations
("traced" cohort). Older rows record no mutations, so the script replays the two
time-based modifiers from the row's timestamp and the item's age ("replayed" cohort):
episode recency and decision staleness. Older code also sorted decisions only by entry
order before diversity, and the replay keeps that order. Frame boost changes no episode
score (an ``EpisodeSummary`` has no ``encoded_frame``); it only sorts by score.

Check: each build's legacy diversity rule is re-run on the reconstructed list, and the
report says how many builds it reproduces exactly. A low share means the reconstruction is
wrong and the inversion counts should not be trusted. The re-run assumes the default caps;
builds made with ``NOUS_CONTEXT_EPISODE_UNTAGGED_DIVERSITY_CAP`` set to anything but 2 will
not reproduce. A trace whose capture hit ``retrieval_telemetry_max_candidates`` (``truncated``)
lists only some of its candidates; such rows are left out and their count is printed.

Read-only (``SET TRANSACTION READ ONLY``). Prints counts and category names only, never
memory text. Connection from the standard DB_* env vars via ``nous.config.Settings``. The
staleness replay reads ``NOUS_STALENESS_PENALTY_ENABLED`` and
``NOUS_STALENESS_HALF_LIFE_DAYS``, and the filter estimate reads the ``NOUS_RELEVANCE_*``
settings, so set them to the deployment's values.

Usage:
  python -m scripts.diag.diversity_loss [--agent-id nous-default] [--days 30]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.cognitive.context import ContextEngine
from nous.config import Settings
from nous.storage.database import Database

# Mirrors ContextEngine.build: the leg each type enters through, its diversity cap, the
# stages that drop a candidate before diversity, and the score modifiers that run before it.
LEG_TYPE = {"context_decisions": "decision", "context_episodes": "episode"}
CAP = {"decision": 3, "episode": 2}
PRE_DIVERSITY_DROPS = {
    "decision": frozenset(),
    "episode": frozenset({"system_episode", "abandoned_episode", "temporal_tier_already_rendered"}),
}
PRE_DIVERSITY_MUTATIONS = {
    "decision": frozenset({"staleness"}),
    "episode": frozenset({"episode_recency", "frame_boost"}),
}
# ContextEngine._apply_staleness_penalty leaves these categories unpenalised.
STALENESS_EXEMPT = frozenset({"rule", "preference", "technical", "concept", "person"})
SESSION_CAP = 2
# An inversion margin above the trace's 1% mutation floor.
MARGIN = 1.02


@dataclass
class Candidate:
    id: str
    entry_score: float | None
    entry_rank: int | None
    mutations: list[dict]
    disposition: str
    stage: str | None


@dataclass
class Build:
    id: str
    at: datetime
    traced: bool
    by_type: dict[str, list[Candidate]] = field(default_factory=dict)


@dataclass
class ItemMeta:
    bucket: str  # the key _enforce_diversity computes
    born: datetime | None = None  # episodes.started_at / decisions.created_at
    category: str = ""
    session_id: str | None = None


@dataclass
class Scored:
    cand: Candidate
    score: float
    meta: ItemMeta | None


def diversity_bucket(raw) -> str:
    """The key ``ContextEngine._enforce_diversity`` derives from a tags list or a category."""
    if isinstance(raw, list):
        return raw[0].lower() if raw else "unknown"
    if isinstance(raw, str) and raw:
        return raw.strip().lower()
    return "unknown"


def replay_score(
    score: float, kind: str, at: datetime, meta: ItemMeta | None, half_life: int | None
) -> float:
    """The score after the time-based modifier that runs before diversity.

    Episodes: ``_apply_episode_recency`` then the frame boost's cap at 1.0. Decisions:
    ``_apply_staleness_penalty`` (``half_life`` None = the penalty is off). Age is taken at
    the build's timestamp, as the live code takes it at ``now``.
    """
    if meta is None or meta.born is None:
        return min(score, 1.0) if kind == "episode" else score
    if kind == "episode":
        age_days = (at - meta.born).total_seconds() / 86400
        return min(score * max(0.5, 1.0 - age_days / 60), 1.0)
    if half_life is None or meta.category in STALENESS_EXEMPT:
        return score
    age_days = (at - meta.born).days
    if age_days > 0:
        return score * max(0.5 ** (age_days / half_life), 0.3)
    return score


def traced_score(cand: Candidate, kind: str) -> float:
    """Entry score plus the last recorded pre-diversity mutation (a missing score sorts as 0,
    as the live sorts treat it)."""
    score = cand.entry_score or 0.0
    for m in cand.mutations:
        if m.get("stage") in PRE_DIVERSITY_MUTATIONS[kind] and m.get("score_after") is not None:
            score = m["score_after"]
    return score


def diversity_input(build: Build, kind: str, meta: dict[str, ItemMeta], half_life: int | None) -> list[Scored]:
    """The list ``_enforce_diversity`` read, in its order, with diversity-stage scores.

    Episodes were sorted by score (frame boost) in both cohorts; decisions were sorted by
    score after staleness only in the traced cohort, and kept entry order before that.
    """
    out = []
    for c in build.by_type.get(kind, []):
        if c.stage in PRE_DIVERSITY_DROPS[kind]:
            continue
        m = meta.get(c.id)
        score = (
            traced_score(c, kind) if build.traced
            else replay_score(c.entry_score or 0.0, kind, build.at, m, half_life)
        )
        out.append(Scored(c, score, m))
    out.sort(key=lambda s: s.cand.entry_rank or 0)
    if kind == "episode" or build.traced:
        out.sort(key=lambda s: -s.score)  # stable, as the live sorts are
    return out


def simulate_drops(items: list[Scored], key: Callable[[Scored], str], cap: int) -> set[str]:
    """Ids the order-preserving per-key cap removes, as ``_enforce_diversity`` does."""
    seen: Counter = Counter()
    dropped = set()
    for s in items:
        k = key(s)
        if seen[k] < cap:
            seen[k] += 1
        else:
            dropped.add(s.cand.id)
    return dropped


def _bucket(s: Scored) -> str:
    return s.meta.bucket if s.meta else "unknown"


def _session(s: Scored) -> str:
    # An episode with no session id (or no row) is its own bucket.
    return (s.meta.session_id if s.meta else None) or f"id:{s.cand.id}"


@dataclass
class Tally:
    builds: int = 0
    candidates: int = 0
    builds_with_drop: int = 0
    drops: int = 0
    rule_reproduced: int = 0
    builds_drop_no_rendered: int = 0
    builds_inverted: int = 0
    builds_inverted_margin: int = 0
    drops_inverted: int = 0
    drops_inverted_same_bucket: int = 0
    drops_inverted_cross_bucket: int = 0
    no_row: int = 0
    replay_compared: int = 0
    replay_off: int = 0
    drop_buckets: Counter = field(default_factory=Counter)
    # episodes only
    tagless: int = 0
    rendered_hist: Counter = field(default_factory=Counter)
    builds_3plus: int = 0
    builds_3plus_rendered_2: int = 0
    builds_3plus_rendered_over_2: int = 0
    drops_kept_by_session_key: int = 0
    # with no diversity cap, the live relevance filter run on the diversity input
    builds_drop_passes_filter: int = 0
    drops_pass_filter: int = 0
    uncapped_hist: Counter = field(default_factory=Counter)


def uncapped_survivors(engine, items: list[Scored], kind: str) -> set[str]:
    """Ids the live ``_apply_relevance_filter`` keeps when diversity drops nothing.

    An estimate of what a ``none`` key would render: it skips the stages between diversity
    and the filter (conversation dedup, usage boost), which this script cannot replay.
    """
    stubs = [SimpleNamespace(id=s.cand.id, score=s.score, source=None) for s in items]
    return {x.id for x in engine._apply_relevance_filter(stubs, kind)}


def analyse(
    builds: list[Build], meta: dict[str, ItemMeta], half_life: int | None, engine=None,
) -> dict[tuple[str, str], Tally]:
    """Tally per (type, cohort). ``engine`` (a ContextEngine with settings) enables the
    no-cap relevance-filter estimate."""
    out: dict[tuple[str, str], Tally] = {}
    for b in builds:
        cohort = "traced" if b.traced else "replayed"
        for kind in ("decision", "episode"):
            items = diversity_input(b, kind, meta, half_life)
            if not items:
                continue
            t = out.setdefault((kind, cohort), Tally())
            t.builds += 1
            t.candidates += len(items)
            t.no_row += sum(1 for s in items if s.meta is None)
            dropped = [s for s in items if s.cand.stage == "diversity"]
            rendered = [s for s in items if s.cand.disposition == "rendered"]
            if simulate_drops(items, _bucket, CAP[kind]) == {s.cand.id for s in dropped}:
                t.rule_reproduced += 1
            if b.traced:
                for s in items:
                    if s.meta is None or s.meta.born is None:
                        continue
                    t.replay_compared += 1
                    replayed = replay_score(s.cand.entry_score or 0.0, kind, b.at, s.meta, half_life)
                    if abs(replayed - s.score) > 0.01 * max(abs(s.score), 1e-9):
                        t.replay_off += 1
            passes = uncapped_survivors(engine, items, kind) if engine is not None else set()
            if kind == "episode":
                t.tagless += sum(1 for s in items if _bucket(s) == "unknown")
                t.rendered_hist[min(len(rendered), 3)] += 1
                if engine is not None:
                    t.uncapped_hist[min(len(passes), 3)] += 1
                if len(items) >= 3:
                    t.builds_3plus += 1
                    if len(rendered) == 2:
                        t.builds_3plus_rendered_2 += 1
                    elif len(rendered) > 2:
                        t.builds_3plus_rendered_over_2 += 1
            if not dropped:
                continue
            t.builds_with_drop += 1
            t.drops += len(dropped)
            t.drop_buckets.update(_bucket(s) for s in dropped)
            n_pass = sum(1 for s in dropped if s.cand.id in passes)
            if n_pass:
                t.builds_drop_passes_filter += 1
                t.drops_pass_filter += n_pass
            if kind == "episode":
                t.drops_kept_by_session_key += len(
                    {s.cand.id for s in dropped} - simulate_drops(items, _session, SESSION_CAP)
                )
            if not rendered:
                t.builds_drop_no_rendered += 1
                continue
            lowest = min(s.score for s in rendered)
            better = [s for s in dropped if s.score > lowest]
            if not better:
                continue
            t.builds_inverted += 1
            if any(s.score > lowest * MARGIN for s in better):
                t.builds_inverted_margin += 1
            t.drops_inverted += len(better)
            for s in better:
                if any(_bucket(r) == _bucket(s) and r.score < s.score for r in rendered):
                    t.drops_inverted_same_bucket += 1
                else:
                    t.drops_inverted_cross_bucket += 1
    return out


def _pct(n: int, d: int) -> str:
    return f"{n} ({n / d:.1%})" if d else f"{n}"


def render(tallies: dict[tuple[str, str], Tally]) -> str:
    lines = []
    for kind in ("decision", "episode"):
        for cohort in ("traced", "replayed"):
            t = tallies.get((kind, cohort))
            if t is None:
                continue
            lines.append(f"== {kind} | {cohort} cohort ==")
            lines.append(f"  builds with candidates at diversity: {t.builds} ({t.candidates} candidates)")
            lines.append(f"  candidates whose row is gone (bucket 'unknown', no replay): {t.no_row}")
            lines.append(f"  legacy rule reproduces the trace's drops: {_pct(t.rule_reproduced, t.builds)} builds")
            if cohort == "traced":
                lines.append(f"  replay differs from recorded score by >1%: {t.replay_off} of {t.replay_compared}")
            lines.append(f"  builds with a diversity drop: {_pct(t.builds_with_drop, t.builds)}; drops: {t.drops}")
            lines.append(f"  builds with a drop and nothing rendered: {t.builds_drop_no_rendered}")
            lines.append(
                f"  builds where a drop outscored the lowest rendered: "
                f"{_pct(t.builds_inverted, t.builds_with_drop)}; by >2%: {t.builds_inverted_margin}"
            )
            lines.append(
                f"  drops that outscored the lowest rendered: {t.drops_inverted} "
                f"(same bucket {t.drops_inverted_same_bucket}, cross bucket {t.drops_inverted_cross_bucket})"
            )
            lines.append(
                f"  drops the relevance filter would keep with no cap (estimate): {t.drops_pass_filter} "
                f"in {_pct(t.builds_drop_passes_filter, t.builds_with_drop)} builds with a drop"
            )
            if kind == "decision":
                top = ", ".join(f"{k}={v}" for k, v in t.drop_buckets.most_common(8))
                lines.append(f"  drops by category: {top}")
            else:
                lines.append(f"  candidates at diversity with no tags: {_pct(t.tagless, t.candidates)}")
                lines.append(f"  drops in the 'unknown' bucket: {_pct(t.drop_buckets['unknown'], t.drops)}")
                hist = ", ".join(
                    f"{'3+' if k == 3 else k}={t.rendered_hist[k]}" for k in sorted(t.rendered_hist)
                )
                lines.append(f"  rendered episodes per build: {hist}")
                if t.uncapped_hist:
                    hist = ", ".join(
                        f"{'3+' if k == 3 else k}={t.uncapped_hist[k]}" for k in sorted(t.uncapped_hist)
                    )
                    lines.append(f"  with no cap, the relevance filter would keep per build (estimate): {hist}")
                lines.append(
                    f"  builds with >=3 at diversity: {t.builds_3plus}; rendered exactly 2: "
                    f"{t.builds_3plus_rendered_2}; rendered >2: {t.builds_3plus_rendered_over_2}"
                )
                lines.append(
                    f"  drops a session key (cap {SESSION_CAP}) would keep: "
                    f"{_pct(t.drops_kept_by_session_key, t.drops)}"
                )
    return "\n".join(lines)


# A context row in the window. A row whose capture hit retrieval_telemetry_max_candidates
# (truncated) lists only some of its candidates, so it is left out and counted instead.
_CONTEXT_ROWS = (
    "r.agent_id = :agent_id AND r.path = 'context' AND r.candidates IS NOT NULL "
    "AND (:days = 0 OR r.timestamp > now() - make_interval(days => :days))"
)

_ROWS_SQL = f"""
SELECT r.id, r.timestamp,
       EXISTS (SELECT 1 FROM jsonb_array_elements(r.legs) l
               WHERE l->>'name' IN ('context_decisions', 'context_episodes') AND l ? 'query') AS traced,
       c->>'entry_leg' AS leg, c->>'id' AS cid,
       (c->>'entry_score')::float AS entry_score, (c->>'entry_rank')::int AS entry_rank,
       c->'mutations' AS mutations, c->>'disposition' AS disposition,
       c->>'disposition_stage' AS stage
FROM nous_system.retrieval_log r, jsonb_array_elements(r.candidates) c
WHERE {_CONTEXT_ROWS} AND NOT r.truncated
  AND c->>'entry_leg' IN ('context_decisions', 'context_episodes')
ORDER BY r.timestamp
"""


async def count_truncated(session: AsyncSession, agent_id: str, days: int) -> int:
    """Context rows in the window left out because their capture was truncated."""
    return (await session.execute(
        text(f"SELECT count(*) FROM nous_system.retrieval_log r WHERE {_CONTEXT_ROWS} AND r.truncated"),
        {"agent_id": agent_id, "days": days},
    )).scalar_one()


async def load_builds(session: AsyncSession, agent_id: str, days: int) -> list[Build]:
    builds: dict[str, Build] = {}
    rows = await session.execute(text(_ROWS_SQL), {"agent_id": agent_id, "days": days})
    for r in rows:
        b = builds.setdefault(r.id, Build(r.id, r.timestamp, bool(r.traced)))
        muts = r.mutations
        if isinstance(muts, str):
            muts = json.loads(muts)
        b.by_type.setdefault(LEG_TYPE[r.leg], []).append(
            Candidate(r.cid, r.entry_score, r.entry_rank, muts or [], r.disposition, r.stage)
        )
    return list(builds.values())


async def load_meta(session: AsyncSession, builds: list[Build]) -> dict[str, ItemMeta]:
    ids = {
        kind: sorted({c.id for b in builds for c in b.by_type.get(kind, [])})
        for kind in ("decision", "episode")
    }
    meta: dict[str, ItemMeta] = {}
    if ids["episode"]:
        rows = await session.execute(text(
            "SELECT id::text AS id, tags, started_at, session_id FROM heart.episodes "
            "WHERE id::text = ANY(CAST(:ids AS text[]))"
        ), {"ids": ids["episode"]})
        for r in rows:
            meta[r.id] = ItemMeta(diversity_bucket(list(r.tags or [])), r.started_at, session_id=r.session_id)
    if ids["decision"]:
        rows = await session.execute(text(
            "SELECT id::text AS id, category, created_at FROM brain.decisions "
            "WHERE id::text = ANY(CAST(:ids AS text[]))"
        ), {"ids": ids["decision"]})
        for r in rows:
            meta[r.id] = ItemMeta(diversity_bucket(r.category), r.created_at, category=r.category or "")
    return meta


async def main() -> int:
    s = Settings()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--agent-id", default=s.agent_id)
    parser.add_argument("--days", type=int, default=30, help="Window in days; 0 = every row.")
    args = parser.parse_args()
    half_life = s.staleness_half_life_days if s.staleness_penalty_enabled else None

    db = Database(s)
    await db.connect()
    try:
        async with db.session() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            builds = await load_builds(session, args.agent_id, args.days)
            truncated = await count_truncated(session, args.agent_id, args.days)
            meta = await load_meta(session, builds)
            await session.rollback()
    finally:
        await db.disconnect()

    # Only the relevance filter is called on it; it reads nothing but settings.
    engine = ContextEngine.__new__(ContextEngine)
    engine._settings = s
    traced = sum(1 for b in builds if b.traced)
    print(
        f"diversity_loss agent={args.agent_id} days={args.days or 'all'} "
        f"context builds={len(builds)} (traced {traced}, replayed {len(builds) - traced}; "
        f"{truncated} truncated captures left out); "
        f"staleness replay: {'half-life ' + str(half_life) + 'd' if half_life else 'off'}; "
        f"relevance drop ratio {s.relevance_drop_ratio}"
    )
    print(render(analyse(builds, meta, half_life, engine)))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
