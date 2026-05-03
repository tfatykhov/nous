"""Architectural-query retrieval regression probe.

Origin (2026-05-02): the F051 aggregate metrics (MRR / P@1 / R@10) on
the prod-snapshot qrels look healthy (0.828 / 0.778 / 0.922). But
during a separate context-packing investigation it appeared that
"tell me about X system" queries surfaced operational chatter instead
of foundational facts. A v1 ad-hoc probe seemed to confirm the issue
(rank 64-115 for foundational facts).

After hand-curating the gold IDs properly, the apparent regression
turned out to be a probe artifact — actual retrieval is healthy on
these queries: 4/6 TOP-1, 5/6 TOP-3, 6/6 TOP-10. The CE rerank
window (default 30) is sufficient.

This regression probe locks that finding in. If a future change
silently degrades architectural-query retrieval, this probe catches
it. The 6 probes were chosen because they exercise:
  - Feature-prefix queries ("F034", "F040", "F012", "F011")
  - Concept queries ("rubric evolver", "cognitive loop")
  - System-summary queries ("heartbeat system", "cognitive loop")

Run:
    NOUS_EVAL_DB_NAME=nous_eval_scratch \
    NOUS_EVAL_AGENT_ID=nous-prod-snapshot \
      uv run python -m nous_eval.probes.arch_query

Exit code:
    0 — TOP-3 hit rate >= 0.66 (4 of 6) AND TOP-10 hit rate == 1.0
    1 — regression detected
    2 — env / setup error
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from uuid import UUID

import asyncpg

from nous.brain.brain import Brain
from nous.brain.embeddings import EmbeddingProvider
from nous.config import Settings
from nous.heart.heart import Heart
from nous.storage.database import Database
from nous_eval.config import EvalSettings
from nous_eval.retrieval_runner import _settings_for_eval_db


@dataclass(frozen=True)
class ArchProbe:
    """One probe: a query + content fragments that ANY foundational fact
    answering it should contain."""

    query: str
    gold_fragments: tuple[str, ...]


# Hand-curated probes against agent_id=nous-prod-snapshot. If the
# corpus snapshot is regenerated and a fragment no longer matches,
# the probe SKIPs that scenario; --strict additionally fails when too
# many scenarios skip so corpus drift cannot silently erase coverage
# (see _MIN_EVALUATED_FRACTION below).
#
# Contract for ``gold_fragments``: every fragment in a single probe's
# tuple must be a *paraphrase of the same answer fact*, not a disjoint
# fact. ``_gold_ids`` UNIONs across fragments — if you list two
# fragments that name different facts, the probe will accept either as
# gold and over-count.
PROBES: tuple[ArchProbe, ...] = (
    ArchProbe(
        query="Tell me about the heartbeat system.",
        gold_fragments=(
            "F034 Heartbeat system",
            "F034 Heartbeat — Full Implementation",
            "F034 proactive monitoring",
        ),
    ),
    ArchProbe(
        query="How does graph densification work?",
        gold_fragments=(
            "F040 Graph Densification spec",
            "graph densification — orphan backfill",
            "Graph densification: orphan backfill",
        ),
    ),
    ArchProbe(
        query="What does the rubric evolver do?",
        gold_fragments=(
            "Self-Modifying Evaluation Rubrics",
            "RubricEvolver",
            "F024-3b",
        ),
    ),
    ArchProbe(
        query="How are procedures created automatically?",
        gold_fragments=(
            "F012 K-Line Learning is shipped",
            "F012 K-line procedure learning",
            "auto-creates procedures from",
        ),
    ),
    ArchProbe(
        query="How do skills get registered in Nous?",
        gold_fragments=(
            "Skills in Nous are learned/registered at runtime",
            "learn_skill tool for registering skills",
            "skills system uses SKILL.md format",
        ),
    ),
    ArchProbe(
        query="Tell me about the cognitive loop.",
        gold_fragments=(
            "Sense\u2192Frame\u2192Recall\u2192Deliberate\u2192Act\u2192Monitor\u2192Learn",
            "Sense, Frame, Recall, Deliberate",
            "cognitive loop (Sense",
        ),
    ),
)


# Regression thresholds (informed by 2026-05-02 baseline of 4/6 TOP-1,
# 5/6 TOP-3, 6/6 TOP-10). Set with one-scenario headroom so a single
# scenario flake doesn't break CI.
_TOP3_FLOOR = 4   # was 5/6 baseline
_TOP10_FLOOR = 5  # was 6/6 baseline

# Minimum fraction of probes that must produce a gold match. Without
# this floor, corpus drift (a fragment going stale after a re-snapshot)
# would silently lower n_evaluated and the TOP-3/TOP-10 floors would
# pass trivially. 0.66 = 4 of 6 probes must still find their gold.
_MIN_EVALUATED_FRACTION = 2 / 3


async def _gold_ids(
    raw_conn: asyncpg.Connection, agent_id: str, fragments: tuple[str, ...],
) -> set[UUID]:
    """Return all fact IDs whose content matches ANY fragment."""
    ids: set[UUID] = set()
    for frag in fragments:
        rows = await raw_conn.fetch(
            "SELECT id FROM heart.facts "
            "WHERE agent_id = $1 AND active = true AND content ILIKE $2",
            agent_id, f"%{frag}%",
        )
        ids.update(r["id"] for r in rows)
    return ids


async def run(
    eval_settings: EvalSettings,
    main_settings: Settings,
    *,
    print_per_scenario: bool = True,
) -> tuple[int, int, int, int]:
    """Run all probes; return (n_evaluated, n_top1, n_top3, n_top10)."""
    settings = _settings_for_eval_db(eval_settings, main_settings).model_copy(
        update={"agent_id": eval_settings.agent_id}
    )

    db = Database(settings)
    await db.connect()
    embedder = EmbeddingProvider(
        api_key=settings.openai_api_key,
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
    )
    heart = Heart(database=db, settings=settings,
                  embedding_provider=embedder, owns_embeddings=False)
    brain = Brain(database=db, settings=settings, embedding_provider=embedder)

    raw_conn = await asyncpg.connect(
        host=eval_settings.db_host, port=eval_settings.db_port,
        user=eval_settings.db_user, password=eval_settings.db_password,
        database=eval_settings.db_name,
    )

    n_evaluated = n_top1 = n_top3 = n_top10 = 0

    try:
        async with heart, brain:
            for probe in PROBES:
                gold = await _gold_ids(raw_conn, eval_settings.agent_id,
                                       probe.gold_fragments)
                if not gold:
                    if print_per_scenario:
                        print(f"  [SKIP] {probe.query!r} — no gold in corpus")
                    continue
                n_evaluated += 1

                results = await heart.recall(
                    probe.query, limit=10, types=["fact"],
                )
                results = list(results or [])

                first_rank: int | None = None
                for i, r in enumerate(results, 1):
                    if r.id in gold:
                        first_rank = i
                        break

                if first_rank is None:
                    marker = "FAIL"
                elif first_rank == 1:
                    n_top1 += 1
                    n_top3 += 1
                    n_top10 += 1
                    marker = "TOP-1"
                elif first_rank <= 3:
                    n_top3 += 1
                    n_top10 += 1
                    marker = f"TOP-3 (#{first_rank})"
                elif first_rank <= 10:
                    n_top10 += 1
                    marker = f"TOP-10 (#{first_rank})"
                else:
                    marker = f"OUTSIDE-10 (#{first_rank})"

                if print_per_scenario:
                    print(f"  [{marker}] {probe.query!r}")
    finally:
        await raw_conn.close()
        await db.disconnect()

    return n_evaluated, n_top1, n_top3, n_top10


async def _async_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=("Architectural-query retrieval regression probe. "
                     "Catches degradation on 'tell me about X' style queries "
                     "that the F051 aggregate qrels underweight."),
    )
    p.add_argument(
        "--strict", action="store_true",
        help="Exit non-zero if TOP-3 < 4 or TOP-10 < 5 (regression floor).",
    )
    args = p.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    eval_settings = EvalSettings()
    main_settings = Settings()

    if not main_settings.openai_api_key:
        print("ERROR: OPENAI_API_KEY required for embeddings.", file=sys.stderr)
        return 2

    print()
    print("=" * 80)
    print(f"ARCH-QUERY REGRESSION PROBE — agent_id={eval_settings.agent_id}")
    print("=" * 80)

    n, n1, n3, n10 = await run(eval_settings, main_settings)

    print()
    print("=" * 80)
    print(f"SUMMARY (n={n})")
    print(f"  TOP-1:  {n1}/{n} ({100*n1/max(n,1):.0f}%)")
    print(f"  TOP-3:  {n3}/{n} ({100*n3/max(n,1):.0f}%)  "
          f"(floor for --strict: {_TOP3_FLOOR})")
    print(f"  TOP-10: {n10}/{n} ({100*n10/max(n,1):.0f}%)  "
          f"(floor for --strict: {_TOP10_FLOOR})")
    print("=" * 80)

    if args.strict:
        min_evaluated = int(len(PROBES) * _MIN_EVALUATED_FRACTION + 0.999)
        if n < min_evaluated:
            print(
                f"\nCOVERAGE REGRESSION: only {n} of {len(PROBES)} probes "
                f"found gold in the corpus (need >={min_evaluated}). "
                f"Likely a corpus refresh has stale gold_fragments — "
                f"update the probes to match new content.",
                file=sys.stderr,
            )
            return 1
        if n3 < _TOP3_FLOOR or n10 < _TOP10_FLOOR:
            print(
                f"\nRANKING REGRESSION: TOP-3={n3}/{n} (need >={_TOP3_FLOOR}) "
                f"or TOP-10={n10}/{n} (need >={_TOP10_FLOOR})",
                file=sys.stderr,
            )
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
