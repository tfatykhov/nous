"""Frame selection accuracy eval (cognitive-layer plan item 1).

Tests `nous/cognitive/frames.py::FrameEngine.select` — pattern matching
against the 6 default frames seeded in sql/seed.sql.

Seeds frames under `frames-eval-agent` in the eval DB so we don't depend
on whatever frames already exist for nous-default / nous-prod-snapshot.
Cleans up afterward.

Usage:
    NOUS_EVAL_DB_NAME=nous_eval_scratch \
      uv run python scripts/eval/eval_frame_selection.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from nous.cognitive.frames import FrameEngine
from nous.config import Settings
from nous.storage.database import Database


_TEST_AGENT_ID = "frames-eval-agent"


@dataclass
class FrameScenario:
    name: str
    input_text: str
    expected_frame: str
    notes: str = ""


# Hand-curated 30 messages covering 6 frames.
SCENARIOS: list[FrameScenario] = [
    # decision (5)
    FrameScenario("dec_should_we", "Should we use Postgres or MySQL?", "decision"),
    FrameScenario("dec_choose", "Help me choose between two architectures.", "decision"),
    FrameScenario("dec_decide", "I need to decide on the cache layer.", "decision"),
    FrameScenario("dec_compare", "Compare Redis and Memcached for our use.", "decision"),
    FrameScenario("dec_tradeoff", "What's the trade-off here?", "decision"),
    # task (5)
    FrameScenario("task_build", "Build me a deploy script for staging.", "task"),
    FrameScenario("task_fix", "Fix the failing CI pipeline.", "task"),
    FrameScenario("task_implement", "Implement the new auth handler.", "task"),
    FrameScenario("task_deploy", "Deploy this branch to production.", "task"),
    FrameScenario("task_run", "Run the migration on the eval DB.", "task"),
    # question (5)
    FrameScenario("q_what", "What is the difference between TCP and UDP?", "question"),
    FrameScenario("q_how", "How does the encryption layer work?",
                  # "how" + "work"; "how" is in question, "work" not in any
                  "question"),
    FrameScenario("q_why", "Why did the build fail this morning?",
                  # "fail" matches debug; "why" matches question; counts equal,
                  # but priority debug (5) > question (3) → debug wins.
                  "debug",
                  "expects debug due to 'fail' priority tiebreak"),
    FrameScenario("q_explain", "Explain how kubernetes pods are scheduled.",
                  "question"),
    FrameScenario("q_tell_me", "Tell me about the new feature.", "question"),
    # debug (5)
    FrameScenario("dbg_error", "I'm getting an error when starting the server.",
                  "debug"),
    FrameScenario("dbg_bug", "There's a bug in the dedup pipeline.", "debug"),
    FrameScenario("dbg_broken", "The compaction step is broken.", "debug"),
    FrameScenario("dbg_failing", "The tests are failing intermittently.", "debug"),
    FrameScenario("dbg_crash", "Server crash on startup.", "debug"),
    # conversation (5)
    FrameScenario("conv_hi", "Hi Nous, how's it going?",
                  # 'hi' (1 hit), 'how are you' (substring) — "how's it going" is
                  # close but not exact. Just 'hi' fires → conversation.
                  "conversation"),
    FrameScenario("conv_hello", "Hello there.", "conversation"),
    FrameScenario("conv_thanks", "Thanks for the help.", "conversation"),
    FrameScenario("conv_how_are_you", "How are you doing today?", "conversation"),
    FrameScenario("conv_just_chat", "Hi just wanted to chat.", "conversation"),
    # creative (5)
    FrameScenario("cre_imagine", "Imagine a world without electricity.", "creative"),
    FrameScenario("cre_brainstorm", "Brainstorm names for the new product.",
                  "creative"),
    FrameScenario("cre_what_if", "What if we redesigned from scratch?",
                  "creative"),
    FrameScenario("cre_design", "Help me design the new dashboard layout.",
                  "creative"),
    FrameScenario("cre_explore", "Explore alternative architectures.", "creative"),
]


async def _seed_frames(db: Database) -> None:
    """Insert 6 default frames under the test agent_id."""
    async with db.session() as session:
        from sqlalchemy import text
        # FK chain: frames.agent_id -> agents.id. Ensure agent row exists.
        await session.execute(text(
            "INSERT INTO nous_system.agents (id, name) VALUES (:aid, :name) "
            "ON CONFLICT (id) DO NOTHING"
        ), {"aid": _TEST_AGENT_ID, "name": "Frames eval test agent"})
        await session.execute(text(
            "DELETE FROM nous_system.frames WHERE agent_id = :aid"
        ), {"aid": _TEST_AGENT_ID})
        # Mirror sql/seed.sql lines 27-33
        rows = [
            ("task", "Task Execution", "Focused on completing a specific task",
             ["build", "fix", "create", "implement", "deploy", "run", "execute",
              "check", "show", "list", "install", "update", "delete", "move", "copy"],
             "tooling", "medium"),
            ("question", "Question Answering", "Answering questions",
             ["what", "how", "why", "explain", "tell me"], "process", "low"),
            ("decision", "Decision Making", "Evaluating options",
             ["should", "choose", "decide", "compare", "trade-off"],
             "architecture", "high"),
            ("creative", "Creative", "Brainstorming, ideation",
             ["imagine", "brainstorm", "what if", "design", "explore"],
             "architecture", "low"),
            ("conversation", "Conversation", "Casual interaction",
             ["hello", "hi", "thanks", "how are you"], "process", "low"),
            ("debug", "Debug", "Investigating problems",
             ["error", "bug", "broken", "failing", "crash", "wrong"],
             "tooling", "medium"),
        ]
        for fid, name, desc, patterns, cat, stakes in rows:
            await session.execute(text(
                "INSERT INTO nous_system.frames (id, agent_id, name, "
                "description, activation_patterns, default_category, "
                "default_stakes) VALUES (:id, :aid, :name, :desc, :pat, "
                ":cat, :stakes)"
            ), {
                "id": fid, "aid": _TEST_AGENT_ID, "name": name,
                "desc": desc, "pat": patterns, "cat": cat, "stakes": stakes,
            })
        await session.commit()


async def _cleanup_frames(db: Database) -> None:
    async with db.session() as session:
        from sqlalchemy import text
        await session.execute(text(
            "DELETE FROM nous_system.frames WHERE agent_id = :aid"
        ), {"aid": _TEST_AGENT_ID})
        await session.commit()


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, default=Path("reports/eval_frame_selection.md"))
    p.add_argument("--out-json", type=Path, default=Path("reports/eval_frame_selection.json"))
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
    db = Database(settings)
    await db.connect()

    try:
        await _seed_frames(db)
        engine = FrameEngine(db, settings)

        results = []
        confusion: dict[tuple[str, str], int] = defaultdict(int)
        for sc in SCENARIOS:
            sel = await engine.select(_TEST_AGENT_ID, sc.input_text)
            actual = sel.frame_id
            passed = actual == sc.expected_frame
            confusion[(sc.expected_frame, actual)] += 1
            results.append({
                "name": sc.name, "input": sc.input_text,
                "expected": sc.expected_frame, "actual": actual,
                "match_method": sel.match_method, "confidence": sel.confidence,
                "passed": passed, "notes": sc.notes,
            })

        n_passed = sum(1 for r in results if r["passed"])
        accuracy = n_passed / len(results)

        per_frame: dict[str, dict] = {}
        for f in ["decision", "task", "question", "debug", "conversation", "creative"]:
            n = sum(1 for r in results if r["expected"] == f)
            correct = sum(1 for r in results if r["expected"] == f and r["passed"])
            per_frame[f] = {"n": n, "correct": correct,
                            "acc": correct / n if n else 0}

        print()
        print("=" * 76)
        print(f"FRAME SELECTION EVAL — {len(SCENARIOS)} scenarios")
        print("=" * 76)
        print(f"\n  Overall: {n_passed}/{len(results)} ({100*accuracy:.1f}%)\n")
        print(f"  {'frame':<14}{'n':>5}{'correct':>10}{'acc':>9}")
        for f, d in per_frame.items():
            print(f"  {f:<14}{d['n']:>5}{d['correct']:>10}{d['acc']:>9.0%}")

        print(f"\nFailures:")
        for r in results:
            if not r["passed"]:
                print(f"  [{r['expected']:<13} -> {r['actual']:<13}] "
                      f"\"{r['input'][:50]}\"")

        labels = ["decision", "task", "question", "debug",
                  "conversation", "creative"]
        print(f"\nConfusion (rows=expected, cols=actual):")
        print(f"  {'':<14}" + "".join(f"{l:>14}" for l in labels))
        for exp in labels:
            row = f"  {exp:<14}"
            for act in labels:
                row += f"{confusion.get((exp, act), 0):>14d}"
            print(row)
        print("=" * 76)

        # Persist
        args.out.parent.mkdir(parents=True, exist_ok=True)
        md = [
            f"# Frame selection eval — {len(SCENARIOS)} scenarios",
            "",
            f"- accuracy: **{n_passed}/{len(results)} ({100*accuracy:.1f}%)**",
            "- SUT: `nous.cognitive.frames.FrameEngine.select`",
            "- 6 default frames seeded under `frames-eval-agent`.",
            "",
            "## Per-frame",
            "",
            "| frame | n | correct | accuracy |",
            "|---|---:|---:|---:|",
        ]
        for f, d in per_frame.items():
            md.append(f"| {f} | {d['n']} | {d['correct']} | {d['acc']:.0%} |")
        md.extend(["", "## Failures", "",
                   "| expected | actual | input |",
                   "|---|---|---|"])
        any_fail = False
        for r in results:
            if not r["passed"]:
                any_fail = True
                md.append(f"| {r['expected']} | {r['actual']} | "
                          f"`{r['input'][:80]}` |")
        if not any_fail:
            md.append("_None — all scenarios passed._")
        md.extend(["", "## Confusion matrix", "",
                   "| expected \\ actual | " + " | ".join(labels) + " |",
                   "|---" * (len(labels) + 1) + "|"])
        for exp in labels:
            md.append("| " + exp + " | " + " | ".join(
                str(confusion.get((exp, act), 0)) for act in labels
            ) + " |")
        args.out.write_text("\n".join(md), encoding="utf-8")
        args.out_json.write_text(json.dumps({
            "n": len(results), "passed": n_passed, "accuracy": accuracy,
            "per_frame": per_frame,
            "confusion": {f"{e}->{a}": c for (e, a), c in confusion.items()},
            "results": results,
        }, indent=2), encoding="utf-8")
        print(f"\nWrote: {args.out}")
        print(f"Wrote: {args.out_json}")
    finally:
        await _cleanup_frames(db)
        await db.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
