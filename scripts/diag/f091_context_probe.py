"""F091 Path-B probe: does ContextEngine.build produce a usable trace?

Path B runs on EVERY turn and fills the system prompt, but until F091 its only
telemetry was a bullet-count regex over rendered prose. This runs the REAL
``ContextEngine.build`` against the local nous DB with tracing on and off, and
asserts the built prompt is unchanged while the trace captures the per-leg
drops — in particular the ``max_k`` cut inside ``_apply_relevance_filter``,
which is the largest silent drop on this path.

Usage:
  DB_HOST=localhost uv run python -m scripts.diag.f091_context_probe
"""

from __future__ import annotations

import asyncio
import sys

from nous.brain.brain import Brain
from nous.brain.embeddings import EmbeddingProvider
from nous.cognitive.context import ContextEngine
from nous.cognitive.schemas import FrameSelection
from nous.config import Settings
from nous.heart.heart import Heart
from nous.observability.retrieval_logger import RetrievalLogger, set_active
from nous.observability.retrieval_trace import UNACCOUNTED
from nous.storage.database import Database

QUERIES = [
    "what are my preferences for running tests",
    "remind me what we decided about retrieval",
    "how does graph expansion work here",
]

FRAME = FrameSelection(
    frame_id="conversation", frame_name="Conversation",
    description="probe", confidence=1.0, match_method="default",
)


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
            print("NOTE: no OPENAI_API_KEY — keyword-only retrieval")
        brain = Brain(database, settings, embedding_provider=embeddings)
        heart = Heart(database, settings, embedding_provider=embeddings)
        engine = ContextEngine(brain, heart, settings, identity_prompt="Probe agent.")

        any_candidates = False
        any_drops = False

        for i, q in enumerate(QUERIES):
            print(f"\nQUERY {q!r}")

            # Tracing OFF
            set_active(None)
            off = await engine.build(
                agent_id=settings.agent_id, session_id=f"probe-off-{i}",
                input_text=q, frame=FRAME,
            )

            # Tracing ON (sample everything so the probe is deterministic)
            rl = RetrievalLogger(candidate_sample_rate=1.0, agent_id=settings.agent_id)
            set_active(rl)
            on = await engine.build(
                agent_id=settings.agent_id, session_id=f"probe-on-{i}",
                input_text=q, frame=FRAME,
            )
            set_active(None)

            check(
                "system prompt identical with tracing on vs off",
                off.system_prompt == on.system_prompt,
                f"off={len(off.system_prompt)}ch on={len(on.system_prompt)}ch",
            )
            check(
                "recalled ids identical",
                off.recalled_ids == on.recalled_ids,
                f"off={ {k: len(v) for k, v in off.recalled_ids.items()} }",
            )

            entries = rl.get_recent()
            check("a trace was committed", len(entries) == 1, str(len(entries)))
            if not entries:
                continue
            d = entries[0]
            counts = d["disposition_counts"]
            n_rendered_expected = sum(len(v) for v in on.recalled_ids.values())

            print(f"    path={d['path']} legs={[l['name'] for l in d['legs']]}")
            print(f"    candidates={d['n_candidates']} rendered={d['n_rendered']} "
                  f"(recalled_ids={n_rendered_expected})")
            print(f"    dispositions={counts}")
            for leg in d["legs"]:
                print(f"      leg {leg['name']}: returned={leg['n_returned']} "
                      f"scores=[{leg['score_min']}, {leg['score_max']}]")

            check("path is 'context'", d["path"] == "context", d["path"])
            check("no unaccounted candidates", UNACCOUNTED not in counts, str(counts))
            check(
                "dispositions sum to candidates",
                sum(counts.values()) == d["n_candidates"],
                f"{sum(counts.values())} vs {d['n_candidates']}",
            )
            check(
                "rendered matches what actually reached the prompt",
                d["n_rendered"] == n_rendered_expected,
                f"trace={d['n_rendered']} recalled_ids={n_rendered_expected}",
            )

            any_candidates = any_candidates or d["n_candidates"] > 0
            any_drops = any_drops or any(
                k != "rendered" for k in counts
            )

        print("\nACROSS ALL QUERIES")
        check("candidates captured on the context path", any_candidates)
        check(
            "at least one drop attributed to a named gate",
            any_drops,
            "no drops seen — expected some at relevance_filter/diversity",
        )

        print(f"\n{'ALL CHECKS PASSED' if not failures else 'FAILURES: ' + str(sorted(set(failures)))}")
        return 1 if failures else 0
    finally:
        set_active(None)
        await database.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
