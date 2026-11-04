"""CLI: ``python -m nous_eval.beam {ingest,answer,score}``.

    ingest  BEAM conversations -> one Nous agent each (``beam-<bucket>-<id>-<tag>``)
            on the scratch DB, through the production write path; ``--sleep`` adds
            a sleep cycle after each batch (an ingest arm: give it its own tag).
    answer  each probing question -> context (``--mode preturn|recall_deep``) ->
            answer model; appends ``answers.jsonl`` and writes ``run.json``.
    score   ``answers.jsonl`` -> BEAM judge -> ``scores.jsonl``, ``summary.json``
            and BEAM-shaped ``results/<bucket>/<id>/result.json``.

One out-dir per (arm, mode). ``answer`` and ``score`` resume: rows already
written are skipped. The out-dir must be outside version control.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from nous_eval.beam import arms, corpus, harness, scoring

logger = logging.getLogger("nous_eval.beam")


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--chats-dir", type=Path, default=Path("tools/beam/chats"), help="upstream BEAM chats/ directory")
    p.add_argument("--bucket", required=True, choices=["100K", "500K", "1M", "10M"])
    p.add_argument("--conv", default="1", help="comma-separated conversation ids, or 'all'")
    p.add_argument(
        "--db-url",
        default=os.environ.get("NOUS_EVAL_SCRATCH_DB_URL"),
        help="scratch DB (default: $NOUS_EVAL_SCRATCH_DB_URL)",
    )
    p.add_argument("--agent-tag", required=True, help="names the ingest: agent id is beam-<bucket>-<id>-<tag>")
    p.add_argument("--arm", default="base", help="label recorded with the run")
    p.add_argument("--flags-file", type=Path, help="dotenv file whose NOUS_* flags form the base config")
    p.add_argument("--set", action="append", default=[], metavar="NOUS_NAME=VALUE", help="arm override (repeatable)")
    p.add_argument("--out-dir", type=Path, required=True)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m nous_eval.beam", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_ing = sub.add_parser("ingest")
    _common(p_ing)
    p_ing.add_argument("--sleep", action="store_true", help="run a sleep cycle after each batch")
    p_ing.add_argument("--limit-sessions", type=int, help="ingest only the first N sessions (smoke runs)")
    p_ans = sub.add_parser("answer")
    _common(p_ans)
    p_ans.add_argument("--mode", required=True, choices=sorted(harness.MODES))
    p_ans.add_argument("--answer-model", help="default: the model Settings resolves")
    p_ans.add_argument("--limit-questions", type=int, help="answer only the first N questions per conversation")
    p_sc = sub.add_parser("score")
    p_sc.add_argument("--out-dir", type=Path, required=True)
    p_sc.add_argument("--judge-model", default="gpt-4.1-mini")
    return parser.parse_args(argv)


def resolve_overrides(args: argparse.Namespace) -> tuple[dict[str, str], list[str]]:
    """Flags file first, ``--set`` on top. Returns (overrides, flags-file names unknown to this code)."""
    base, skipped = arms.load_flags_file(args.flags_file) if args.flags_file else ({}, [])
    return {**base, **arms.parse_overrides(args.set)}, skipped


def agent_id(bucket: str, conv_id: str, tag: str) -> str:
    return f"beam-{bucket.lower()}-{conv_id}-{tag}"


def _conv_ids(args: argparse.Namespace) -> list[str]:
    if args.conv == "all":
        return corpus.list_conversations(args.chats_dir, args.bucket)
    return [c.strip() for c in args.conv.split(",") if c.strip()]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def _row_key(row: dict[str, Any]) -> tuple[str, str, int]:
    return row["conversation"], row["ability"], row["index"]


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def _require_db(args: argparse.Namespace) -> str:
    if not args.db_url:
        raise SystemExit("--db-url (or NOUS_EVAL_SCRATCH_DB_URL) is required")
    return args.db_url


async def cmd_ingest(
    args: argparse.Namespace, *, llm: Any = None, embeddings: Any = None, prepare: bool = True
) -> list[dict[str, Any]]:
    overrides, skipped = resolve_overrides(args)
    db_url = _require_db(args)
    out: list[dict[str, Any]] = []
    for i, cid in enumerate(_conv_ids(args)):
        conv = corpus.load_conversation(args.chats_dir, args.bucket, cid)
        settings = arms.build_settings(db_url, agent_id(args.bucket, cid, args.agent_tag), overrides)
        if prepare and i == 0:
            await harness.prepare_db(settings)
        async with harness.open_stack(settings, llm=llm, embeddings=embeddings, sleep=args.sleep) as stack:
            stats = await harness.ingest_conversation(stack, conv, limit_sessions=args.limit_sessions)
        record = {
            "conversation": conv.key,
            "agent_id": settings.agent_id,
            "sleep": args.sleep,
            "arm": args.arm,
            "overrides": overrides,
            "flags_file_unknown_names": skipped,
            "settings": arms.settings_snapshot(settings, overrides),
            "git": arms.code_revision(),
            **stats,
        }
        _write_json(args.out_dir / f"ingest-{args.bucket}-{cid}-{args.agent_tag}.json", record)
        logger.info("ingested %s into %s: %s", conv.key, settings.agent_id, stats["rows"])
        out.append(record)
    return out


async def cmd_answer(args: argparse.Namespace, *, llm: Any = None, embeddings: Any = None) -> int:
    overrides, skipped = resolve_overrides(args)
    db_url = _require_db(args)
    answers_path = args.out_dir / "answers.jsonl"
    run_path = args.out_dir / "run.json"
    run_id = {
        "mode": args.mode,
        "arm": args.arm,
        "overrides": overrides,
        "agent_tag": args.agent_tag,
        "bucket": args.bucket,
    }
    if run_path.exists():
        prior = json.loads(run_path.read_text(encoding="utf-8"))
        if {k: prior.get(k) for k in run_id} != run_id:
            raise SystemExit(
                f"{args.out_dir} holds a different run (mode/arm/overrides/tag/bucket); use a new --out-dir"
            )
    done = {_row_key(r) for r in _read_jsonl(answers_path)}
    written = 0
    for cid in _conv_ids(args):
        conv = corpus.load_conversation(args.chats_dir, args.bucket, cid)
        settings = arms.build_settings(db_url, agent_id(args.bucket, cid, args.agent_tag), overrides)
        model = args.answer_model or settings.model
        if not run_path.exists():
            snapshot = arms.settings_snapshot(settings, overrides)
            _write_json(
                run_path,
                {
                    **run_id,
                    "answer_model": model,
                    "chats_dir": str(args.chats_dir),
                    "flags_file_unknown_names": skipped,
                    "settings": snapshot,
                    "git": arms.code_revision(),
                },
            )
        questions = conv.questions[: args.limit_questions] if args.limit_questions else conv.questions
        async with harness.open_stack(settings, llm=llm, embeddings=embeddings) as stack:
            if not (await harness.count_rows(stack))["episodes"]:
                raise SystemExit(f"agent {settings.agent_id} has no episodes; run ingest first")
            for q in questions:
                row: dict[str, Any] = {"conversation": conv.key, "ability": q.ability, "index": q.index}
                if _row_key(row) in done:
                    continue
                t0 = time.monotonic()
                row.update(
                    {"question": q.question, "rubric": list(q.rubric), "mode": args.mode, "agent_id": settings.agent_id}
                )
                try:
                    context, meta = await harness.MODES[args.mode](stack, q.question)
                    answer, stop_reason = await harness.generate_answer(stack.llm, model, context, q.question)
                    row.update(
                        {
                            "answer": answer,
                            "stop_reason": stop_reason,
                            "context": context,
                            "context_chars": len(context),
                            "retrieval": meta,
                            "error": None,
                        }
                    )
                except Exception as e:  # recorded, not fatal: one bad question must not lose a long run
                    logger.exception("answer failed for %s %s[%d]", conv.key, q.ability, q.index)
                    row.update({"answer": None, "error": f"{type(e).__name__}: {e}"})
                row["latency_s"] = round(time.monotonic() - t0, 2)
                _append_jsonl(answers_path, row)
                written += 1
        logger.info("answered %s (%s)", conv.key, args.mode)
    return written


async def cmd_score(args: argparse.Namespace, *, judge: scoring.Judge | None = None) -> dict[str, Any]:
    from nous.config import Settings

    answers = _read_jsonl(args.out_dir / "answers.jsonl")
    if not answers:
        raise SystemExit(f"no answers.jsonl in {args.out_dir}")
    scores_path = args.out_dir / "scores.jsonl"
    done = {_row_key(r) for r in _read_jsonl(scores_path)}
    owned = judge is None
    if owned:
        judge = scoring.OpenAIJudge(Settings().openai_api_key, model=args.judge_model)
    try:
        for row in answers:
            if _row_key(row) in done:
                continue
            scored = {k: row[k] for k in ("conversation", "ability", "index", "question", "answer", "error")}
            if row.get("error") is None:
                try:
                    scored["score"] = await scoring.score_answer(judge, row["ability"], row["rubric"], row["answer"])
                except Exception as e:  # recorded and left out of the means, as an unparseable judge reply
                    logger.exception("judge failed for %s %s[%d]", row["conversation"], row["ability"], row["index"])
                    scored["error"] = f"judge {type(e).__name__}: {e}"
            _append_jsonl(scores_path, scored)
    finally:
        if owned:
            await judge.close()

    rows = _read_jsonl(scores_path)
    run = json.loads((args.out_dir / "run.json").read_text(encoding="utf-8"))
    summary = {
        "run": run,
        "judge_model": getattr(judge, "model", args.judge_model),
        "n_answers": len(answers),
        "n_errors": sum(1 for r in rows if r.get("error")),
        **scoring.aggregate(rows),
    }
    _write_json(args.out_dir / "summary.json", summary)
    by_conv: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for r in sorted(answers, key=lambda r: (r["conversation"], r["ability"], r["index"])):
        by_conv.setdefault(r["conversation"], {}).setdefault(r["ability"], []).append(
            {"question": r["question"], "llm_response": r.get("answer") or ""}
        )
    for conv_key, data in by_conv.items():
        _write_json(args.out_dir / "results" / conv_key / "result.json", data)
    return summary


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args(argv)
    arms.check_out_dir(args.out_dir)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    if args.cmd == "ingest":
        asyncio.run(cmd_ingest(args))
    elif args.cmd == "answer":
        asyncio.run(cmd_answer(args))
    else:
        summary = asyncio.run(cmd_score(args))
        print(json.dumps({k: summary[k] for k in ("overall", "abilities", "event_ordering", "n_errors")}, indent=2))
    return 0
