"""F060.1 quality probe — does fallback-from-summary hallucinate?

Pulls N real stuck-open episodes from the eval DB (nous-prod-snapshot),
runs `summarize_episode(transcript=plain_summary)` on each, and applies
the F059 entity-substring guard to the LLM output. Reports:

  - per-episode: input chars, output chars, suspect entity count + samples
  - aggregate: hallucination rate, average entities introduced, cost

This is the option-2 sample-validation gate for activating F060.1 on
prod. If average suspect rate is low and inspection of suspects shows
no fabrications, F060.1 is safe to enable. If hallucination rate is
material, F060.1 stays off and F060.2 alone handles the cleanup.

Usage:
    NOUS_EVAL_DB_NAME=nous_eval_scratch \
    NOUS_EVAL_AGENT_ID=nous-prod-snapshot \
      uv run python scripts/eval/validate_f060_1_quality.py --n 10
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text as sql_text

from nous.api.compaction import detect_hallucinated_entities
from nous.brain.brain import Brain
from nous.brain.embeddings import EmbeddingProvider
from nous.config import Settings
from nous.handlers.episode_summarizer import EpisodeSummarizer
from nous.heart.heart import Heart
from nous.storage.database import Database
from nous_eval.config import EvalSettings
from nous_eval.retrieval_runner import _settings_for_eval_db


async def _pull_stuck_open(db: Database, agent_id: str, n: int) -> list[tuple[uuid.UUID, str]]:
    """Pull N real stuck-open episodes whose only signal is plain summary."""
    async with db.session() as session:
        rows = await session.execute(sql_text("""
            SELECT id, summary
            FROM heart.episodes
            WHERE agent_id = :agent_id
              AND active = true
              AND structured_summary IS NULL
              AND (transcript IS NULL OR length(transcript) < 50)
              AND summary IS NOT NULL
              AND length(summary) >= 20
            ORDER BY started_at DESC
            LIMIT :n
        """), {"agent_id": agent_id, "n": n})
        return [(r[0], r[1]) for r in rows.all()]


async def _restore_summary(db: Database, ep_id: uuid.UUID) -> None:
    """Reset structured_summary to NULL so the eval is non-mutating."""
    async with db.session() as session:
        await session.execute(sql_text(
            "UPDATE heart.episodes SET structured_summary = NULL WHERE id = :id"
        ), {"id": ep_id})
        await session.commit()


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=10)
    p.add_argument(
        "--out-md",
        type=Path,
        default=Path("reports/eval_f060_1_fallback_quality.md"),
    )
    p.add_argument(
        "--out-json",
        type=Path,
        default=Path("reports/eval_f060_1_fallback_quality.json"),
    )
    args = p.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    logging.basicConfig(level=logging.WARNING)

    eval_settings = EvalSettings()
    main_settings = Settings()
    settings = _settings_for_eval_db(eval_settings, main_settings)

    if not (main_settings.anthropic_api_key or main_settings.anthropic_auth_token):
        print("ERROR: Anthropic creds required.", file=sys.stderr)
        return 2
    if not settings.openai_api_key:
        print("ERROR: OPENAI_API_KEY required.", file=sys.stderr)
        return 2

    agent_id = eval_settings.agent_id  # nous-prod-snapshot

    db = Database(settings)
    await db.connect()

    embedder = EmbeddingProvider(
        api_key=settings.openai_api_key,
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
    )
    from nous.api.anthropic_client import create_client
    api_client = create_client(main_settings)
    await api_client.start()

    heart = Heart(database=db, settings=settings,
                  embedding_provider=embedder, owns_embeddings=False)
    brain = Brain(database=db, settings=settings, embedding_provider=embedder)

    results: list[dict] = []

    try:
        async with heart, brain:
            samples = await _pull_stuck_open(db, agent_id, args.n)
            print(f"\nSampled {len(samples)} real stuck-open episodes from {agent_id}.\n")

            summarizer = EpisodeSummarizer(
                heart=heart, brain=brain, settings=settings, bus=None,
                llm_client=api_client,
            )

            for ep_id, plain_summary in samples:
                print(f"  [{str(ep_id)[:8]}...] input ({len(plain_summary)}c): {plain_summary[:100]!r}")
                try:
                    output = await summarizer.summarize_episode(
                        episode_id=ep_id,
                        transcript=plain_summary,
                        agent_id=agent_id,
                    )
                except Exception as exc:
                    print(f"     LLM error: {exc}")
                    results.append({
                        "episode_id": str(ep_id),
                        "input_chars": len(plain_summary),
                        "input_sample": plain_summary[:200],
                        "error": str(exc),
                    })
                    await _restore_summary(db, ep_id)
                    continue

                if output is None:
                    print("     SKIPPED by summarize_episode")
                    results.append({
                        "episode_id": str(ep_id),
                        "input_chars": len(plain_summary),
                        "input_sample": plain_summary[:200],
                        "skipped": True,
                    })
                    continue

                # Build a flat string from the structured output so the
                # entity guard sees the full surface area the summarizer
                # generated.
                output_text_parts = [
                    output.get("title", ""),
                    output.get("summary", ""),
                    " ".join(output.get("key_points") or []),
                    " ".join(output.get("topics") or []),
                ]
                for f in output.get("candidate_facts") or []:
                    if isinstance(f, dict):
                        output_text_parts.append(f.get("subject", ""))
                        output_text_parts.append(f.get("content", ""))
                output_text = " | ".join(p for p in output_text_parts if p)

                suspects = detect_hallucinated_entities(plain_summary, output_text)
                results.append({
                    "episode_id": str(ep_id),
                    "input_chars": len(plain_summary),
                    "input_sample": plain_summary[:200],
                    "output_title": output.get("title", ""),
                    "output_chars": len(output_text),
                    "candidate_fact_count": len(output.get("candidate_facts") or []),
                    "guard_suspect_count": len(suspects),
                    "guard_suspects": suspects[:10],
                    "output_summary": (output.get("summary") or "")[:400],
                })
                print(
                    f"     output ({len(output_text)}c) — "
                    f"facts={len(output.get('candidate_facts') or [])} "
                    f"suspects={len(suspects)} {(suspects[:3] if suspects else [])}"
                )
                # Restore so we don't mutate the eval-DB snapshot
                await _restore_summary(db, ep_id)
    finally:
        await api_client.close()
        await db.disconnect()

    # Aggregate
    completed = [r for r in results if "guard_suspect_count" in r]
    n_done = len(completed)
    if n_done == 0:
        print("\nNo summaries produced — nothing to evaluate.")
        return 1

    fired = sum(1 for r in completed if r["guard_suspect_count"] > 0)
    above_thresh = sum(1 for r in completed if r["guard_suspect_count"] > 2)
    avg_susp = sum(r["guard_suspect_count"] for r in completed) / n_done
    avg_facts = sum(r["candidate_fact_count"] for r in completed) / n_done

    print(
        f"\n  Aggregate: {n_done} summarized, {fired} fired any suspect, "
        f"{above_thresh} above threshold (>2 suspects)"
    )
    print(f"  Avg suspect entities: {avg_susp:.1f}")
    print(f"  Avg candidate_facts emitted: {avg_facts:.1f}")

    # Write reports
    args.out_md.parent.mkdir(parents=True, exist_ok=True)
    md_lines = [
        f"# F060.1 fallback quality probe — `{agent_id}`",
        "",
        f"- Sampled: **{len(samples)}** real stuck-open episodes (transcript NULL, summary >= 20 chars)",
        f"- Summarized: {n_done}, errors/skipped: {len(results) - n_done}",
        f"- Guard fired (any suspects): **{fired}/{n_done}** ({100*fired/n_done:.0f}%)",
        f"- Above threshold (>2 suspects): **{above_thresh}/{n_done}** ({100*above_thresh/n_done:.0f}%)",
        f"- Avg suspect entities per output: **{avg_susp:.1f}**",
        f"- Avg candidate_facts emitted: **{avg_facts:.1f}**",
        "",
        "| ep_id | input chars | output chars | facts | suspects | suspect samples |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for r in completed:
        susp = r.get("guard_suspects") or []
        sample = ", ".join(susp[:3]) + ("…" if len(susp) > 3 else "")
        md_lines.append(
            f"| {r['episode_id'][:8]}... | {r['input_chars']} | {r['output_chars']} | "
            f"{r['candidate_fact_count']} | {r['guard_suspect_count']} | "
            f"{sample if sample else '—'} |"
        )

    md_lines.extend(["", "## Sample inputs and outputs", ""])
    for r in completed:
        md_lines.append(f"### {r['episode_id'][:8]}...")
        md_lines.append("")
        md_lines.append(f"**Input** ({r['input_chars']} chars):")
        md_lines.append(f"> {r['input_sample']}")
        md_lines.append("")
        md_lines.append(f"**Output title:** {r['output_title']}")
        md_lines.append(f"**Output summary:** {r['output_summary']}")
        if r.get("guard_suspects"):
            md_lines.append("")
            md_lines.append("**Guard suspects:**")
            for s in r["guard_suspects"]:
                md_lines.append(f"- `{s}`")
        md_lines.append("")

    args.out_md.write_text("\n".join(md_lines), encoding="utf-8")
    args.out_json.write_text(
        json.dumps({"agent_id": agent_id, "results": results}, indent=2),
        encoding="utf-8",
    )
    print(f"\nWrote: {args.out_md}")
    print(f"Wrote: {args.out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
