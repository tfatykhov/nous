"""Coverage A/B: re-extract the same transcripts with extraction_coverage_broadened
OFF vs ON (live summarizer _summarize_single), judge coverage of each, and report
the lift + the fact-count delta (the noise/bloat side).

    PROD_PW=... NOUS_BACKGROUND_MODEL=claude-sonnet-4-6 \\
    PYTHONPATH=. uv run python scripts/diag/coverage_ab.py [N]
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg

from nous.api.anthropic_client import create_client
from nous.config import Settings
from nous.handlers.episode_summarizer import EpisodeSummarizer
from scripts.diag.coverage_audit import _PROMPT, _parse

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
PROD = dict(host=os.environ.get("PROD_HOST", "192.168.1.141"), port=5432,
            user="nous", password=os.environ["PROD_PW"], database="nous")
AGENT = "nous-default"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 10


async def _extract(summarizer, transcript: str) -> list[str]:
    s = await summarizer._summarize_single(transcript, "", None)
    if isinstance(s, dict):
        facts = s.get("candidate_facts", [])
    elif isinstance(s, list):
        facts = s  # model returned a bare array (truncation/quirk)
    else:
        facts = []
    out = [f.get("content", "") for f in facts if isinstance(f, dict) and f.get("content")]
    if not isinstance(s, dict):
        print(f"    [warn] _summarize_single returned {type(s).__name__}; {len(out)} facts salvaged")
    return out


async def _judge(client, transcript: str, facts: list[str]) -> tuple[int, int]:
    fact_text = "\n".join(f"- {c}" for c in facts) or "(none)"
    prompt = _PROMPT.replace("{transcript}", transcript).replace(
        "{facts}", fact_text).replace("{nfacts}", str(len(facts)))
    resp = await client.call({
        "model": "claude-sonnet-4-6",
        "system": [
            {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude.", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "You are a strict coverage auditor. Return JSON only."},
        ],
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4096, "temperature": 0.0,
    })
    items = _parse(resp.content)
    cap = sum(1 for it in items if it.get("status") == "CAPTURED")
    miss = sum(1 for it in items if it.get("status") == "MISSED")
    return cap, miss


async def main() -> None:
    base = Settings()
    off = base.model_copy(update={"extraction_coverage_broadened": False})
    on = base.model_copy(update={"extraction_coverage_broadened": True})
    client = create_client(base)
    await client.start()
    s_off = EpisodeSummarizer.__new__(EpisodeSummarizer)
    s_off._llm, s_off._settings = client, off
    s_on = EpisodeSummarizer.__new__(EpisodeSummarizer)
    s_on._llm, s_on._settings = client, on

    conn = await asyncpg.connect(**PROD)
    try:
        eps = await conn.fetch(
            """SELECT e.id FROM heart.episodes e
               WHERE e.agent_id=$1
                 AND EXISTS(SELECT 1 FROM heart.episode_chunks c WHERE c.episode_id=e.id)
               ORDER BY random() LIMIT $2""", AGENT, N)
        co = [0, 0]
        cn = [0, 0]
        nf_off = nf_on = 0
        print(f"model={base.background_model}  episodes={len(eps)}\n")
        for row in eps:
            chunks = await conn.fetch(
                "SELECT content FROM heart.episode_chunks WHERE episode_id=$1 ORDER BY chunk_index",
                row["id"])
            transcript = "\n".join(c["content"] for c in chunks if c["content"])[:14000]
            if len(transcript) < 200:
                continue
            f_off = await _extract(s_off, transcript)
            f_on = await _extract(s_on, transcript)
            o_cap, o_miss = await _judge(client, transcript, f_off)
            n_cap, n_miss = await _judge(client, transcript, f_on)
            co[0] += o_cap; co[1] += o_miss
            cn[0] += n_cap; cn[1] += n_miss
            nf_off += len(f_off); nf_on += len(f_on)
            ot = o_cap + o_miss or 1
            nt = n_cap + n_miss or 1
            print(f"  ep {str(row['id'])[:8]}: OFF {len(f_off)}f cov={o_cap/ot:.2f}  "
                  f"ON {len(f_on)}f cov={n_cap/nt:.2f}")
        to = co[0] + co[1] or 1
        tn = cn[0] + cn[1] or 1
        print(f"\n=== A/B ({len(eps)} episodes) ===")
        print(f"OFF: coverage {co[0]/to:.2f}  ({co[0]}/{to})  avg facts/ep {nf_off/len(eps):.1f}")
        print(f"ON : coverage {cn[0]/tn:.2f}  ({cn[0]}/{tn})  avg facts/ep {nf_on/len(eps):.1f}")
        print(f"lift: {cn[0]/tn - co[0]/to:+.2f} coverage, "
              f"{nf_on/len(eps) - nf_off/len(eps):+.1f} facts/ep")
    finally:
        await conn.close()
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
