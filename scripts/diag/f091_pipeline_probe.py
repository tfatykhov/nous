"""F091 pipeline probe: does the instrumentation fire, and does it change results?

Runs the REAL ``run_recall_pipeline`` against the local nous DB twice per query
— once with tracing off (``trace=None``) and once with a live trace — and
asserts:

  1. The (id, type, score) sequence is IDENTICAL between the two runs. This is
     the whole safety claim of the write-only collector; if it ever fails, the
     recall_deep snapshot and the nous_eval contract are both at risk.
  2. The trace actually captured something — legs, candidates, dispositions.
  3. Every candidate carries a terminal disposition (no `unaccounted`), which
     is the drift guard for a filter that drops without reporting.

Usage:
  DB_HOST=localhost uv run python -m scripts.diag.f091_pipeline_probe
"""

from __future__ import annotations

import asyncio
import sys

from nous.api.retrieval_pipeline import run_recall_pipeline
from nous.brain.brain import Brain
from nous.brain.embeddings import EmbeddingProvider
from nous.config import Settings
from nous.heart.heart import Heart
from nous.observability.retrieval_logger import RetrievalLogger
from nous.observability.retrieval_trace import UNACCOUNTED
from nous.storage.database import Database

QUERIES = [
    "what did we decide about chunk recall limits",
    "retrieval telemetry",
    "graph expansion thresholds",
    "user preferences",
]


def _fingerprint(results) -> list[tuple[str, str, float | None]]:
    return [(str(r.id), r.type, r.score) for r in results]


async def main() -> int:
    settings = Settings()
    database = Database(settings)
    await database.connect()

    failures: list[str] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        print(f"  {'PASS' if cond else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
        if not cond:
            failures.append(label)

    try:
        embeddings = (
            EmbeddingProvider(
                api_key=settings.openai_api_key,
                model=settings.embedding_model,
                dimensions=settings.embedding_dimensions,
                cache_size=settings.embedding_cache_size,
            )
            if getattr(settings, "openai_api_key", None) else None
        )
        if embeddings is None:
            print("NOTE: no OPENAI_API_KEY — running keyword-only (still a valid "
                  "on-vs-off comparison, but vector legs will be quiet)")
        brain = Brain(database, settings, embedding_provider=embeddings)
        heart = Heart(database, settings, embedding_provider=embeddings)
        rl = RetrievalLogger(candidate_sample_rate=1.0, agent_id=settings.agent_id)

        any_expansion = False
        any_candidates = False

        for q in QUERIES:
            print(f"\nQUERY {q!r}")

            off_results, off_stats = await run_recall_pipeline(
                query=q, heart=heart, brain=brain, settings=settings, limit=10,
                trace=None,
            )
            tr = rl.start(query=q, path="pipeline", session_id="probe")
            on_results, on_stats = await run_recall_pipeline(
                query=q, heart=heart, brain=brain, settings=settings, limit=10,
                trace=tr,
            )
            rl.commit(tr)

            # CONTROL: a second untraced run. This probe hits a LIVE database
            # and the pipeline itself writes (`track_access` on keyed-leg
            # survivors, F044 recall-touch), so "off != on" alone cannot
            # distinguish "tracing changed results" from "the corpus moved".
            off2_results, _ = await run_recall_pipeline(
                query=q, heart=heart, brain=brain, settings=settings, limit=10,
                trace=None,
            )
            if _fingerprint(off_results) != _fingerprint(off2_results):
                print("  SKIP  byte-identity — corpus changed between untraced "
                      "runs; the comparison is not valid this run")
            else:
                check(
                    "results identical with tracing on vs off",
                    _fingerprint(off_results) == _fingerprint(on_results),
                    f"off={len(off_results)} on={len(on_results)}",
                )
            check(
                "stats identical",
                (off_stats.n_heart_results, off_stats.n_brain_results,
                 off_stats.n_graph_expanded, off_stats.attempted_legs)
                == (on_stats.n_heart_results, on_stats.n_brain_results,
                    on_stats.n_graph_expanded, on_stats.attempted_legs),
            )

            d = tr.to_dict()
            counts = d["disposition_counts"]
            print(f"    legs={[l['name'] for l in d['legs']]}")
            print(f"    candidates={d['n_candidates']} rendered={d['n_rendered']} "
                  f"expansions={d['n_expansions']}")
            print(f"    dispositions={counts}")

            check("at least one leg recorded", len(d["legs"]) > 0)
            # A leg that ran and produced rows must not report 0. The
            # attempted-legs rollup used to overwrite the keyed/exemplar
            # counts (which are known only at assembly) with a default 0.
            legs_by_name = {leg["name"]: leg for leg in d["legs"]}
            producing = {c["entry_leg"] for c in (d["candidates"] or [])}
            for leg_name in producing:
                leg = legs_by_name.get(leg_name)
                if leg is None:
                    continue
                check(
                    f"leg '{leg_name}' reports a non-zero count (it produced rows)",
                    leg["n_returned"] > 0,
                    f"n_returned={leg['n_returned']}",
                )
            check("no unaccounted candidates", UNACCOUNTED not in counts, str(counts))
            check(
                "rendered count matches returned results",
                d["n_rendered"] == len(on_results),
                f"trace={d['n_rendered']} pipeline={len(on_results)}",
            )
            check(
                "dispositions sum to candidates",
                sum(counts.values()) == d["n_candidates"],
            )
            any_expansion = any_expansion or d["n_expansions"] > 0
            any_candidates = any_candidates or d["n_candidates"] > 0

        print("\nACROSS ALL QUERIES")
        check("candidates captured somewhere", any_candidates)
        check("graph expansion captured somewhere", any_expansion,
              "no expansion edges seen — check graph_recall_enabled")

        print(f"\n{'ALL CHECKS PASSED' if not failures else 'FAILURES: ' + str(sorted(set(failures)))}")
        return 1 if failures else 0
    finally:
        await database.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
