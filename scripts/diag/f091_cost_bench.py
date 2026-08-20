"""F091 cost benchmark: what does the collector actually cost per turn?

The sample rate was shipped at 0.1 as an ESTIMATE. This measures the real
overhead of candidate capture on both retrieval paths so the rate can be set
from data instead.

Reports median and p90 wall-clock per build/recall across three arms:
  off      — tracing disabled entirely (NULL_TRACE)
  on_0.0   — trace created, candidate capture NOT sampled (header/legs only)
  on_1.0   — trace created, full per-candidate capture

The gap between on_0.0 and on_1.0 is the cost the sample rate governs; the gap
between off and on_0.0 is what every turn pays unconditionally.

Usage:
  DB_HOST=localhost uv run python -m scripts.diag.f091_cost_bench
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time

from nous.api.retrieval_pipeline import run_recall_pipeline
from nous.brain.brain import Brain
from nous.brain.embeddings import EmbeddingProvider
from nous.cognitive.context import ContextEngine
from nous.cognitive.schemas import FrameSelection
from nous.config import Settings
from nous.heart.heart import Heart
from nous.observability.retrieval_logger import RetrievalLogger, set_active
from nous.storage.database import Database

QUERIES = [
    "what are my preferences for running tests",
    "remind me what we decided about retrieval",
    "how does graph expansion work here",
    "user preferences",
]
REPS = 5

FRAME = FrameSelection(
    frame_id="conversation", frame_name="Conversation",
    description="bench", confidence=1.0, match_method="default",
)


def _summarize(label: str, samples: list[float]) -> tuple[float, float]:
    med = statistics.median(samples)
    p90 = sorted(samples)[max(0, int(len(samples) * 0.9) - 1)]
    print(f"    {label:<10} median {med*1000:8.1f} ms   p90 {p90*1000:8.1f} ms   n={len(samples)}")
    return med, p90


async def main() -> int:
    settings = Settings()
    database = Database(settings)
    await database.connect()
    try:
        emb = (
            EmbeddingProvider(
                api_key=settings.openai_api_key, model=settings.embedding_model,
                dimensions=settings.embedding_dimensions,
                cache_size=settings.embedding_cache_size,
            )
            if getattr(settings, "openai_api_key", None) else None
        )
        brain = Brain(database, settings, embedding_provider=emb)
        heart = Heart(database, settings, embedding_provider=emb)
        engine = ContextEngine(brain, heart, settings, identity_prompt="Bench.")

        # ---- Path B: ContextEngine.build (runs on EVERY turn) -------------
        print("\nPATH B — ContextEngine.build (every turn)")
        arms: dict[str, list[float]] = {"off": [], "on_0.0": [], "on_1.0": []}
        for rep in range(REPS):
            for qi, q in enumerate(QUERIES):
                for arm in ("off", "on_0.0", "on_1.0"):
                    if arm == "off":
                        set_active(None)
                    else:
                        set_active(RetrievalLogger(
                            candidate_sample_rate=0.0 if arm == "on_0.0" else 1.0,
                            agent_id=settings.agent_id,
                        ))
                    t0 = time.perf_counter()
                    await engine.build(
                        agent_id=settings.agent_id,
                        session_id=f"bench-{arm}-{rep}-{qi}",
                        input_text=q, frame=FRAME,
                    )
                    arms[arm].append(time.perf_counter() - t0)
        set_active(None)
        b_off, _ = _summarize("off", arms["off"])
        b_hdr, _ = _summarize("on_0.0", arms["on_0.0"])
        b_full, _ = _summarize("on_1.0", arms["on_1.0"])

        # ---- Path A: run_recall_pipeline (per recall_deep call) -----------
        print("\nPATH A — run_recall_pipeline (per recall_deep call)")
        parms: dict[str, list[float]] = {"off": [], "on_0.0": [], "on_1.0": []}
        for rep in range(REPS):
            for q in QUERIES:
                for arm in ("off", "on_0.0", "on_1.0"):
                    tr = None
                    if arm != "off":
                        rl = RetrievalLogger(
                            candidate_sample_rate=0.0 if arm == "on_0.0" else 1.0,
                            agent_id=settings.agent_id,
                        )
                        tr = rl.start(query=q, path="pipeline")
                    t0 = time.perf_counter()
                    await run_recall_pipeline(
                        query=q, heart=heart, brain=brain, settings=settings,
                        limit=10, trace=tr,
                    )
                    parms[arm].append(time.perf_counter() - t0)
        a_off, _ = _summarize("off", parms["off"])
        a_hdr, _ = _summarize("on_0.0", parms["on_0.0"])
        a_full, _ = _summarize("on_1.0", parms["on_1.0"])

        def pct(base: float, other: float) -> str:
            if base <= 0:
                return "n/a"
            return f"{(other - base) / base * 100:+.1f}%"

        print("\nOVERHEAD vs untraced (median)")
        print(f"  Path B  header+legs only : {pct(b_off, b_hdr)}   "
              f"({(b_hdr-b_off)*1000:+.2f} ms)  <- paid on EVERY turn")
        print(f"  Path B  full candidates  : {pct(b_off, b_full)}   "
              f"({(b_full-b_off)*1000:+.2f} ms)  <- what the sample rate governs")
        print(f"  Path A  header+legs only : {pct(a_off, a_hdr)}   "
              f"({(a_hdr-a_off)*1000:+.2f} ms)")
        print(f"  Path A  full candidates  : {pct(a_off, a_full)}   "
              f"({(a_full-a_off)*1000:+.2f} ms)")
        print("\nNOTE: retrieval is DB-bound, so these medians carry real query "
              "variance. Treat a result under ~1 ms / ~1% as 'in the noise' "
              "rather than as a precise measurement.")
        return 0
    finally:
        set_active(None)
        await database.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
