"""Fact-extraction coverage audit (2026-06-14).

For a sample of prod episodes that have BOTH F067 verbatim chunks and extracted
facts, ask Sonnet which salient/queryable facts in the transcript are CAPTURED
by an extracted fact vs MISSED, and classify each by type. Aggregates a coverage
rate + a droppage-by-type histogram.

    PROD_PW=... PYTHONPATH=. uv run python scripts/diag/coverage_audit.py [N]

Cost: 1 Sonnet call per episode (~$0.02). N=20 ~ $0.40.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections import Counter

import asyncpg

from nous.api.anthropic_client import create_client
from nous.config import Settings

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROD = dict(host=os.environ.get("PROD_HOST", "192.168.1.141"), port=5432,
            user="nous", password=os.environ["PROD_PW"], database="nous")
AGENT = "nous-default"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 20

_TYPES = ["dated_event", "preference", "numeric_config", "decision_rationale",
          "entity_relationship", "procedure_howto", "status_state", "other"]

_PROMPT = """You audit a memory system's fact-extraction COVERAGE.

Below is a conversation TRANSCRIPT and the FACTS the system extracted from it.
Identify the salient, queryable pieces of information in the TRANSCRIPT that a
user might later ask about (names, dates, decisions, preferences, numbers/IDs/
versions/settings, who-did-what, how-to steps, current status). For EACH such
item, decide whether an extracted FACT captures it.

Return ONLY a JSON object:
{
  "items": [
    {"info": "<one-line summary of the salient info>",
     "status": "CAPTURED" | "MISSED",
     "type": "dated_event|preference|numeric_config|decision_rationale|entity_relationship|procedure_howto|status_state|other"}
  ]
}
Be strict: status=CAPTURED only if an extracted fact actually contains that
information (paraphrase ok). Ignore pure chit-chat with no queryable content.
No prose outside the JSON.

=== TRANSCRIPT ===
{transcript}

=== EXTRACTED FACTS ({nfacts}) ===
{facts}
"""


def _parse(content: list) -> list[dict]:
    parts = []
    for b in content:
        t = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
        if t == "text":
            parts.append(b.get("text") if isinstance(b, dict) else getattr(b, "text", ""))
    raw = "".join(parts).strip()
    raw = re.sub(r"^```(?:json)?\s*\n?", "", raw, flags=re.I)
    raw = re.sub(r"\n?```\s*$", "", raw)
    raw = re.sub(r",(\s*[}\]])", r"\1", raw)
    obj = json.loads(raw)
    return obj.get("items", [])


async def main() -> None:
    settings = Settings()
    client = create_client(settings)
    await client.start()
    conn = await asyncpg.connect(**PROD)
    try:
        # codex P2: sample ALL chunked episodes, NOT only those with facts —
        # zero-fact episodes are the worst coverage failures and excluding them
        # biases coverage upward. The fact query below returns "(none)" for them.
        eps = await conn.fetch(
            """
            SELECT e.id FROM heart.episodes e
            WHERE e.agent_id = $1
              AND EXISTS (SELECT 1 FROM heart.episode_chunks c WHERE c.episode_id = e.id
                          AND c.source_kind IS DISTINCT FROM 'document')
            ORDER BY random() LIMIT $2
            """, AGENT, N)

        captured = 0
        missed = 0
        miss_types: Counter = Counter()
        cap_types: Counter = Counter()
        per_ep = []
        miss_examples = []

        for row in eps:
            ep_id = row["id"]
            chunks = await conn.fetch(
                "SELECT content FROM heart.episode_chunks WHERE episode_id = $1 "
                "AND source_kind IS DISTINCT FROM 'document' ORDER BY chunk_index", ep_id)
            transcript = "\n".join(c["content"] for c in chunks if c["content"])[:14000]
            facts = await conn.fetch(
                "SELECT content FROM heart.facts WHERE source_episode_id = $1", ep_id)
            fact_text = "\n".join(f"- {f['content']}" for f in facts) or "(none)"
            if not transcript.strip():
                continue

            prompt = _PROMPT.replace("{transcript}", transcript).replace(
                "{facts}", fact_text).replace("{nfacts}", str(len(facts)))
            try:
                resp = await client.call({
                    "model": "claude-sonnet-4-6",
                    "system": [
                        {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude.", "cache_control": {"type": "ephemeral"}},
                        {"type": "text", "text": "You are a strict coverage auditor. Return JSON only."},
                    ],
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 4096,
                    "temperature": 0.0,
                })
                items = _parse(resp.content)
            except Exception as e:  # noqa: BLE001
                print(f"  ep {str(ep_id)[:8]}: judge failed ({str(e)[:60]})")
                continue

            ec = sum(1 for it in items if it.get("status") == "CAPTURED")
            em = sum(1 for it in items if it.get("status") == "MISSED")
            captured += ec
            missed += em
            for it in items:
                typ = it.get("type", "other")
                if it.get("status") == "MISSED":
                    miss_types[typ] += 1
                    if len(miss_examples) < 20:
                        miss_examples.append((typ, it.get("info", "")))
                elif it.get("status") == "CAPTURED":
                    cap_types[typ] += 1
            per_ep.append((str(ep_id)[:8], len(facts), ec, em))

        total = captured + missed
        print(f"\n=== Coverage audit: {len(per_ep)} episodes, {total} salient items ===")
        print(f"CAPTURED {captured}  MISSED {missed}  "
              f"COVERAGE = {captured/total if total else 0:.2f}\n")
        print("Miss rate by type (missed / (missed+captured)):")
        for t in _TYPES:
            m, c = miss_types.get(t, 0), cap_types.get(t, 0)
            tot = m + c
            if tot:
                print(f"  {t:<20} miss {m:>3}/{tot:<3} = {m/tot:.2f}")
        print("\nSample MISSED items:")
        for typ, info in miss_examples:
            print(f"  [{typ}] {info[:110]}")
        print("\nPer-episode (id, nfacts, captured, missed):")
        for e in per_ep:
            print(f"  {e[0]}  facts={e[1]:>2}  cap={e[2]:>2}  miss={e[3]:>2}")
    finally:
        await conn.close()
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
