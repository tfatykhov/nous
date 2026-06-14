"""Confirm the F031 MERGE 91%-missing-content failure is max_tokens=300 truncation.

Pulls real prod pairs that were downgraded MERGE->KEEP_BOTH for missing content,
re-runs the resolution LLM call at 300 (repro) vs 800 tokens, and reports whether
merged_content appears only at the higher budget.

    PROD_PW=... NOUS_BACKGROUND_MODEL=claude-sonnet-4-6 \\
    PYTHONPATH=. uv run python scripts/diag/probe_merge_truncation.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import asyncpg

from nous.api.anthropic_client import create_client
from nous.config import Settings
from nous.handlers import call_background_llm_structured
from nous.handlers.sleep_handler import (
    _CONTRADICTION_RESOLUTION_PROMPT,
    _CONTRADICTION_RESOLUTION_SCHEMA,
)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
PROD = dict(host="192.168.1.141", port=5432, user="nous",
            password=os.environ["PROD_PW"], database="nous")
AGENT = "nous-default"


async def _run(client, model, prompt, max_tokens):
    r = await call_background_llm_structured(
        client=client, model=model,
        system_prompt="You are a memory management system resolving contradictory facts.",
        user_message=prompt, tool_name="resolve_contradiction",
        tool_description="Resolve a contradiction between two facts in memory.",
        output_schema=_CONTRADICTION_RESOLUTION_SCHEMA, max_tokens=max_tokens,
    )
    if not r:
        return ("(no result)", 0, 0)
    return (str(r.get("action", "?")),
            len(str(r.get("reason", "") or "")),
            len(str(r.get("merged_content", "") or "").strip()))


async def main() -> None:
    s = Settings()
    client = create_client(s)
    await client.start()
    conn = await asyncpg.connect(**PROD)
    try:
        evs = await conn.fetch(
            "SELECT data FROM nous_system.events WHERE agent_id=$1 "
            "AND event_type='f031_contradiction_resolution' "
            "ORDER BY random() LIMIT 40", AGENT)
        pairs = []
        for e in evs:
            d = e["data"]; d = json.loads(d) if isinstance(d, str) else d
            if d.get("raw_action") == "MERGE" and d.get("downgraded_due_to_missing_content"):
                pairs.append((d["fact1_id"], d["fact2_id"]))
            if len(pairs) >= 6:
                break

        print(f"model={s.background_model}  testing {len(pairs)} missing-content MERGE pairs\n")
        print(f"{'pair':<5}{'@300 action':<14}{'reason':>7}{'merged':>8}   {'@800 action':<14}{'reason':>7}{'merged':>8}")
        fixed = 0
        for i, (f1, f2) in enumerate(pairs, 1):
            rows = await conn.fetch(
                "SELECT id, content, learned_at FROM heart.facts WHERE id = ANY($1::uuid[])",
                [f1, f2])
            by = {str(r["id"]): r for r in rows}
            a, b = by.get(f1), by.get(f2)
            if not a or not b:
                print(f"{i:<5}(a fact is gone — skip)"); continue
            prompt = _CONTRADICTION_RESOLUTION_PROMPT.format(
                date_a=str(a["learned_at"])[:10], date_b=str(b["learned_at"])[:10],
                content_a=a["content"][:500], content_b=b["content"][:500])
            act3, rl3, mc3 = await _run(client, s.background_model, prompt, 300)
            act8, rl8, mc8 = await _run(client, s.background_model, prompt, 800)
            if mc3 == 0 and mc8 > 0 and act8 == "MERGE":
                fixed += 1
            print(f"{i:<5}{act3:<14}{rl3:>7}{mc3:>8}   {act8:<14}{rl8:>7}{mc8:>8}")
        print(f"\npairs where 800-tokens recovered merged_content (300 empty -> 800 filled): {fixed}/{len(pairs)}")
    finally:
        await conn.close()
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
