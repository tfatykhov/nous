"""S2 replay probe: feed a stored transcript through the episode summarizer,
hardening flag OFF vs ON, and count prompt-echo vs content candidate facts.

Validates the 2026-07-02 S2 fix (extraction_input_hardening_enabled) against
a real transcript — independent of any eval harness. The original evidence
transcript is nous_mab episode dc0d8f50 (agent mab-eval-prod_memory-8f18622a):
274k chars of "Please remember the following information (part N/9)" bulk
ingest, which pre-fix produced 11/11 verbatim summarizer-prompt-echo facts.

Usage (from repo root, .env supplies the LLM credentials):
    uv run python scripts/diag/replay_s2_contamination.py --transcript t.txt \
        [--chunks 4]

Result on the evidence transcript (2026-07-02, claude-sonnet-4-6):
    baseline(OFF): 8 facts  = 7 echo / 1 content
    hardened(ON): 81 facts  = 0 echo / 81 content
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


def classify(facts: list) -> tuple[int, int]:
    """(echoes, content) via the verbatim-shingle classifier."""
    from nous.handlers import _is_prompt_echo, _normalize_for_echo
    from nous.handlers.episode_summarizer import _ECHO_GUARD_TEMPLATES

    norm = [_normalize_for_echo(t) for t in _ECHO_GUARD_TEMPLATES]
    echoes = sum(
        1 for f in facts
        if isinstance(f, dict) and _is_prompt_echo(f.get("content", ""), norm)
    )
    return echoes, len(facts) - echoes


async def run_arm(settings, client, transcript: str, flag: bool, n_chunks: int) -> list[dict]:
    from nous.handlers.episode_summarizer import EpisodeSummarizer

    s = EpisodeSummarizer.__new__(EpisodeSummarizer)
    s._settings = settings.model_copy(update={
        "temporal_extraction_enabled": True,
        "extraction_coverage_broadened": True,
        "episode_open_threads": False,
        "episode_summary_max_tokens": 0,
        "extraction_input_hardening_enabled": flag,
    })
    s._llm = client

    chunks = s._chunk_transcript(transcript, max_chars=settings.transcript_max_chars)
    print(f"  transcript={len(transcript)} chars -> {len(chunks)} chunks; running first {n_chunks}")
    out = []
    for i, chunk in enumerate(chunks[:n_chunks]):
        result = await s._summarize_single(chunk, "", started_at=None)
        facts = (result or {}).get("candidate_facts", [])
        out.append({
            "chunk": i, "chunk_chars": len(chunk),
            "parse_ok": result is not None,
            "title": (result or {}).get("title"), "facts": facts,
        })
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcript", required=True, help="path to a raw transcript file")
    ap.add_argument("--chunks", type=int, default=4, help="chunks per arm")
    args = ap.parse_args()
    transcript = Path(args.transcript).read_text(encoding="utf-8")

    from nous.api.anthropic_client import create_client
    from nous.config import Settings

    settings = Settings()
    print(f"background_model={settings.background_model}")
    client = create_client(settings)
    await client.start()
    try:
        report = {}
        for flag in (False, True):
            arm = "hardened(ON)" if flag else "baseline(OFF)"
            print(f"\n=== ARM {arm} ===")
            results = await run_arm(settings, client, transcript, flag, args.chunks)
            all_facts = [f for r in results for f in r["facts"]]
            echoes, content = classify(all_facts)
            report[arm] = {"facts_total": len(all_facts),
                           "verbatim_echoes": echoes, "non_echo": content}
            for r in results:
                e, c = classify(r["facts"])
                print(f"  chunk {r['chunk']} ({r['chunk_chars']} chars, parse_ok={r['parse_ok']}): "
                      f"title={r['title']!r}, {len(r['facts'])} facts ({e} echo / {c} content)")
        print("\n=== SUMMARY ===")
        print(json.dumps(report, indent=2))
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
