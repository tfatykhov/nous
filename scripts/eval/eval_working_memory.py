"""Working memory selection quality eval (cognitive-layer plan item 4).

Tests the working-memory loading step in `pre_turn`: which prior items
get surfaced into the LLM context for the current turn?

Production behavior (`cognitive/layer.py` post_turn writes WM,
pre_turn reads):
- Items scored >= 0.7 get loaded
- Max 10 items per turn
- Score is recall_score from the prior recall pipeline

This eval seeds synthetic WM rows under a test agent, then asks
pre_turn to load them and measures relevance via Sonnet judge.

Note: requires the eval-DB stack and Anthropic credentials. Will hit
rate limits if prod traffic is competing for OAT quota.

Usage:
    NOUS_EVAL_DB_NAME=nous_eval_scratch \
      uv run python scripts/eval/eval_working_memory.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text

from nous.api.anthropic_client import create_client
from nous.config import Settings
from nous.heart.working_memory import WorkingMemoryManager
from nous.storage.database import Database


_DEFAULT_MODEL = "claude-sonnet-4-6"
_TEST_AGENT_ID = "wm-eval-agent"


@dataclass
class WMScenario:
    """One synthetic session with seeded WM items + a current message.

    Ground truth is per-item: which items are RELEVANT to the current
    message? The judge produces this verdict. We compare to what the
    WM manager loaded.
    """
    name: str
    current_message: str
    seeded_items: list[dict] = field(default_factory=list)
    notes: str = ""


SCENARIOS: list[WMScenario] = [
    WMScenario(
        "decision_continuity",
        current_message="What did we decide about the cache layer?",
        seeded_items=[
            {"content": "Decided to use Postgres LISTEN/NOTIFY over Kafka",
             "score": 0.92, "tags": ["decision", "infra"]},
            {"content": "Redis cache uses port 6380 in staging",
             "score": 0.88, "tags": ["config", "cache"]},
            {"content": "Tim's flight UA2408 departs at 5:45 PM",
             "score": 0.71, "tags": ["personal"]},
            {"content": "FOMC meeting priced one cut for September",
             "score": 0.75, "tags": ["finance"]},
        ],
        notes="cache + decision items relevant; flight/FOMC are noise",
    ),
    WMScenario(
        "fresh_question_no_relevant",
        current_message="What's the weather in Tokyo today?",
        seeded_items=[
            {"content": "User prefers no emojis in responses",
             "score": 0.85, "tags": ["preference"]},
            {"content": "Project deadline moved from June to July 15",
             "score": 0.82, "tags": ["schedule"]},
        ],
        notes="None of the seeded items are relevant; ideal load = 0 or just preferences",
    ),
    WMScenario(
        "low_score_filtered",
        current_message="Update me on Acme Corp.",
        seeded_items=[
            {"content": "Acme Corp CEO is Sarah Chen",
             "score": 0.95, "tags": ["acme", "person"]},
            {"content": "Acme Corp uses SAP for ERP",
             "score": 0.91, "tags": ["acme", "tech"]},
            {"content": "Acme Corp annual revenue $250M",
             "score": 0.65, "tags": ["acme", "finance"]},
            {"content": "Acme Corp office is in Cleveland",
             "score": 0.55, "tags": ["acme", "location"]},
        ],
        notes="0.65 and 0.55 should be filtered (below 0.7 floor)",
    ),
]


_JUDGE_PROMPT = """A working-memory system loaded items into context for a user's current message. You will judge whether each item is RELEVANT or NOT to that message.

Current user message: {current_message}

Items below were CANDIDATES for loading. For each, judge relevance: would surfacing this item help answer the current message accurately?

CANDIDATES:
{candidates}

Respond with strict JSON:
{{
  "verdicts": [
    {{"index": 0, "relevant": true|false, "reason": "<short>"}},
    ...
  ]
}}"""


async def _judge_relevance(
    api_client, model: str, current_message: str, items: list[dict],
) -> list[dict]:
    candidates_block = "\n".join(
        f"{i}. score={it['score']:.2f} content=\"{it['content']}\""
        for i, it in enumerate(items)
    )
    prompt = _JUDGE_PROMPT.format(
        current_message=current_message, candidates=candidates_block,
    )
    payload = {
        "model": model,
        "max_tokens": 800,
        "temperature": 0,
        "system": "",
        "messages": [{"role": "user", "content": prompt}],
    }
    response = await api_client.call(payload)
    text_out = ""
    for block in response.content:
        if isinstance(block, dict) and block.get("type") == "text":
            text_out = block.get("text", "").strip()
            break
    if text_out.startswith("```"):
        text_out = "\n".join(text_out.splitlines()[1:-1])
    try:
        return json.loads(text_out).get("verdicts", [])
    except Exception:
        return [{"index": i, "relevant": False, "reason": "judge parse error"}
                for i in range(len(items))]


async def _seed_wm(db: Database, scenario_idx: int, items: list[dict]) -> str:
    """Seed WM rows under a unique session_id for this scenario."""
    session_id = f"wm-eval-session-{scenario_idx}"
    async with db.session() as session:
        # Ensure agent exists
        await session.execute(text(
            "INSERT INTO nous_system.agents (id, name) VALUES (:aid, :n) "
            "ON CONFLICT (id) DO NOTHING"
        ), {"aid": _TEST_AGENT_ID, "n": "WM eval"})
        # Clean prior runs
        await session.execute(text(
            "DELETE FROM heart.working_memory WHERE session_id = :sid"
        ), {"sid": session_id})
        for item in items:
            await session.execute(text(
                "INSERT INTO heart.working_memory "
                "(id, agent_id, session_id, content, score, tags) "
                "VALUES (:id, :aid, :sid, :content, :score, :tags)"
            ), {
                "id": uuid4(), "aid": _TEST_AGENT_ID, "sid": session_id,
                "content": item["content"], "score": item["score"],
                "tags": item.get("tags", []),
            })
        await session.commit()
    return session_id


async def _load_wm(db: Database, session_id: str, threshold: float = 0.7,
                   limit: int = 10) -> list[dict]:
    """Mirror pre_turn's WM load query."""
    async with db.session() as session:
        result = await session.execute(text(
            "SELECT content, score FROM heart.working_memory "
            "WHERE agent_id = :aid AND session_id = :sid AND score >= :thr "
            "ORDER BY score DESC LIMIT :lim"
        ), {"aid": _TEST_AGENT_ID, "sid": session_id,
            "thr": threshold, "lim": limit})
        return [{"content": r[0], "score": r[1]} for r in result.all()]


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--judge-model", default=_DEFAULT_MODEL)
    p.add_argument("--threshold", type=float, default=0.7)
    p.add_argument("--max-scenarios", type=int, default=3)
    p.add_argument("--out", type=Path, default=Path("reports/eval_working_memory.md"))
    p.add_argument("--out-json", type=Path, default=Path("reports/eval_working_memory.json"))
    p.add_argument("--eval-db-host", default="127.0.0.1")
    p.add_argument("--eval-db-port", type=int, default=5433)
    p.add_argument("--eval-db-user", default="nous")
    p.add_argument("--eval-db-password", default="nous_eval")
    p.add_argument("--eval-db-name", default="nous_eval_scratch")
    args = p.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    logging.basicConfig(level=logging.WARNING)

    settings = Settings().model_copy(update={
        "db_host": args.eval_db_host, "db_port": args.eval_db_port,
        "db_user": args.eval_db_user, "db_password": args.eval_db_password,
        "db_name": args.eval_db_name, "agent_id": _TEST_AGENT_ID,
    })
    if not (settings.anthropic_api_key or settings.anthropic_auth_token):
        print("ERROR: Anthropic creds required for judge.", file=sys.stderr)
        return 2

    db = Database(settings)
    await db.connect()

    api_client = create_client(settings)
    await api_client.start()

    results = []
    try:
        for i, sc in enumerate(SCENARIOS[:args.max_scenarios]):
            session_id = await _seed_wm(db, i, sc.seeded_items)
            loaded = await _load_wm(db, session_id, args.threshold)
            loaded_contents = {it["content"] for it in loaded}

            await asyncio.sleep(2.0)
            verdicts = await _judge_relevance(
                api_client, args.judge_model,
                sc.current_message, sc.seeded_items,
            )
            relevant_set = {
                sc.seeded_items[v["index"]]["content"]
                for v in verdicts if v.get("relevant")
                and 0 <= v.get("index", -1) < len(sc.seeded_items)
            }

            tp = len(loaded_contents & relevant_set)
            fp = len(loaded_contents - relevant_set)
            fn = len(relevant_set - loaded_contents)
            precision = tp / (tp + fp) if (tp + fp) else 0
            recall = tp / (tp + fn) if (tp + fn) else 0

            results.append({
                "name": sc.name,
                "n_seeded": len(sc.seeded_items),
                "n_loaded": len(loaded),
                "n_relevant_per_judge": len(relevant_set),
                "tp": tp, "fp": fp, "fn": fn,
                "precision": precision, "recall": recall,
                "loaded": list(loaded_contents),
                "relevant": list(relevant_set),
                "verdicts": verdicts,
            })
    finally:
        # Cleanup WM rows
        async with db.session() as session:
            await session.execute(text(
                "DELETE FROM heart.working_memory WHERE agent_id = :aid"
            ), {"aid": _TEST_AGENT_ID})
            await session.commit()
        await api_client.close()
        await db.disconnect()

    if results:
        avg_p = sum(r["precision"] for r in results) / len(results)
        avg_r = sum(r["recall"] for r in results) / len(results)
    else:
        avg_p = avg_r = 0

    print()
    print("=" * 76)
    print(f"WORKING MEMORY SELECTION — {len(results)} scenarios")
    print("=" * 76)
    for r in results:
        print(f"\n  {r['name']}: precision={r['precision']:.0%} "
              f"recall={r['recall']:.0%} "
              f"(loaded={r['n_loaded']}, relevant={r['n_relevant_per_judge']})")
    print(f"\n  Average precision: {avg_p:.0%}")
    print(f"  Average recall:    {avg_r:.0%}")
    print("=" * 76)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    md = [
        f"# Working memory selection eval — {len(results)} scenarios",
        "",
        f"- judge: `{args.judge_model}`",
        f"- threshold: {args.threshold}",
        f"- avg precision: **{avg_p:.0%}**, avg recall: **{avg_r:.0%}**",
        "",
        "## Per-scenario",
        "",
        "| name | seeded | loaded | relevant (judge) | TP | FP | FN | P | R |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        md.append(
            f"| {r['name']} | {r['n_seeded']} | {r['n_loaded']} | "
            f"{r['n_relevant_per_judge']} | {r['tp']} | {r['fp']} | "
            f"{r['fn']} | {r['precision']:.0%} | {r['recall']:.0%} |"
        )
    args.out.write_text("\n".join(md), encoding="utf-8")
    args.out_json.write_text(json.dumps({
        "n_scenarios": len(results),
        "avg_precision": avg_p, "avg_recall": avg_r,
        "judge_model": args.judge_model,
        "results": results,
    }, indent=2), encoding="utf-8")
    print(f"\nWrote: {args.out}")
    print(f"Wrote: {args.out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
