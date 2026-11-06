"""CLI: ``python -m nous_eval.halumem {run,score}``.

    run    HaluMem users -> one Nous agent each (``halumem-<uuid8>-<tag>``) on the
           scratch DB, through the production write path (``--sleep`` adds a sleep
           cycle between days); writes the adapter output HaluMem scores, one file
           per user under ``<out-dir>/results/nous-<version>/tmp/``, merged into
           ``nous_eval_results.jsonl`` next to it. A user whose file exists is
           skipped, so a run resumes; run.json (settings and the corpus's sha256)
           must match to resume. Files are written whole or not at all. A user that
           stopped part-way is run again under a fresh agent id (``-a2``, ...); the
           partial agent is left in the scratch DB.
    score  HaluMem's own ``evaluation.py`` (from ``--halumem-dir``, unchanged) on that
           file, rebuilt first from every per-user file (parallel runs each merge only
           what they saw), with its judge model; results land in the same directory. It caches
           each user's judgements in ``tmp2/``: ``run`` deletes a user's cache when it
           rewrites that user. HaluMem's aggregator divides by the number of
           interference, update and question records, so a ``--limit-sessions`` run
           without all three fails in ``score`` (the per-user records in ``tmp2/`` are
           still written).

Answers use HaluMem's answer prompt (PROMPT_MEMZERO) and memory template
(TEMPLATE_MEM0), read from the HaluMem checkout without running it. Several users
can run in parallel as separate processes with disjoint ``--users``. The out-dir
must be outside version control, and the database's name must start with
nous_eval, nous_test or beam unless ``--i-know-this-is-scratch`` is given.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from nous_eval.beam import arms, harness, scoring
from nous_eval.beam import run as beam_run
from nous_eval.halumem import corpus, runner

logger = logging.getLogger("nous_eval.halumem")

FRAME = "nous"  # evaluation.py reads results/<frame>-<version>/<frame>_eval_results.jsonl
ANSWER_PROMPT_NAME = "PROMPT_MEMZERO"
MEMORY_TEMPLATE_NAME = "TEMPLATE_MEM0"
# HaluMem's eval/.env-example: the judge (and the reference adapters' answer model).
JUDGE_ENV = {
    "OPENAI_MAX_TOKENS": "16384",
    "OPENAI_TEMPERATURE": "0.0",
    "OPENAI_TIMEOUT": "300",
    "RETRY_TIMES": "3",
    "WAIT_TIME_LOWER": "10",
    "WAIT_TIME_UPPER": "30",
}
RESUME_KEYS = (
    "arm",
    "agent_tag",
    "sleep",
    "answer_model",
    "override_names",
    "settings",
    "eval_overrides",
    "data",
    "data_sha256",
    "limit_sessions",
    "halumem_revision",
    "update_top_k",
    "recall_deep_limit",
    "owner_named",
)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m nous_eval.halumem", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("--data", type=Path, required=True, help="HaluMem-Medium.jsonl (or -Long)")
    p_run.add_argument("--users", default="0", help="comma-separated 0-based user indices, or 'all'")
    p_run.add_argument("--limit-sessions", type=int, help="only the first N sessions per user (smoke runs)")
    p_run.add_argument("--db-url", default=os.environ.get("NOUS_EVAL_SCRATCH_DB_URL"))
    p_run.add_argument("--agent-tag", required=True, help="agent id is halumem-<uuid8>-<tag>")
    p_run.add_argument("--arm", default="base", help="label recorded with the run")
    p_run.add_argument("--flags-file", type=Path, help="dotenv file; its arm settings (beam arms.ARM_SETTINGS)")
    p_run.add_argument("--set", action="append", default=[], metavar="NOUS_NAME=VALUE")
    p_run.add_argument("--sleep", action="store_true", help="a sleep cycle between days")
    p_run.add_argument("--answer-model", default="gpt-4o", help="OpenAI model answering the questions")
    p_run.add_argument("--i-know-this-is-scratch", action="store_true")
    p_sc = sub.add_parser("score")
    p_sc.add_argument("--judge-model", default="gpt-4o")
    for p in (p_run, p_sc):
        p.add_argument("--halumem-dir", type=Path, required=True, help="a checkout of github.com/MemTensor/HaluMem")
        p.add_argument("--version", default="default", help="names the run: results/nous-<version>/")
        p.add_argument("--out-dir", type=Path, required=True)
    return parser.parse_args(argv)


class UsageCounter:
    """The run's Anthropic client, totalling the token usage each response reports, per model."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.by_model: dict[str, dict[str, int]] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _add(self, payload: dict[str, Any], response: Any) -> Any:
        totals = self.by_model.setdefault(str(payload.get("model")), {"calls": 0})
        totals["calls"] += 1
        for key, value in (getattr(response, "usage", None) or {}).items():
            if isinstance(value, int):
                totals[key] = totals.get(key, 0) + value
        return response

    async def call(self, payload: dict[str, Any]) -> Any:
        return self._add(payload, await self._inner.call(payload))

    async def call_streaming_aggregated(self, payload: dict[str, Any]) -> Any:
        return self._add(payload, await self._inner.call_streaming_aggregated(payload))

    def take(self) -> dict[str, dict[str, int]]:
        """The totals so far, then start again from zero."""
        out, self.by_model = self.by_model, {}
        return out


def result_dir(args: argparse.Namespace) -> Path:
    return args.out_dir / "results" / f"{FRAME}-{args.version}"


def agent_id(user_uuid: str, tag: str) -> str:
    return f"halumem-{user_uuid[:8]}-{tag}"


def next_agent_id(base: str, used: set[str]) -> str:
    """``base``, or the first ``base-a<n>`` (n >= 2) with no episodes yet."""
    if base not in used:
        return base
    n = 2
    while f"{base}-a{n}" in used:
        n += 1
    return f"{base}-a{n}"


async def fresh_agent_id(settings: Any, base: str) -> str:
    """An agent id for ``base`` that holds no episodes (a user that stopped part-way left some)."""
    from sqlalchemy import text as sa_text

    from nous.storage.database import Database

    db = Database(settings)
    await db.connect()
    try:
        async with db.session() as s:
            used = set(
                (
                    await s.execute(
                        sa_text("SELECT DISTINCT agent_id FROM heart.episodes WHERE agent_id = :b OR agent_id LIKE :p"),
                        {"b": base, "p": f"{base}-a%"},
                    )
                ).scalars()
            )
    finally:
        await db.disconnect()
    chosen = next_agent_id(base, used)
    if chosen != base:
        logger.warning("%s already has episodes from a run that stopped part-way; using %s", base, chosen)
    return chosen


def select_users(users: list[dict[str, Any]], spec: str) -> list[dict[str, Any]]:
    if spec == "all":
        return users
    picked = [int(x) for x in spec.split(",") if x.strip()]
    bad = [i for i in picked if not 0 <= i < len(users)]
    if bad:
        raise SystemExit(f"--users {bad}: the data has {len(users)} users")
    return [users[i] for i in picked]


def checkout_revision(halumem_dir: Path) -> str:
    out = subprocess.run(["git", "-C", str(halumem_dir), "rev-parse", "HEAD"], capture_output=True, text=True)
    return out.stdout.strip() or "unknown"


def write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` so a reader, or a resume after a crash, never sees a partial file."""
    part = path.with_name(path.name + ".part")
    part.write_text(text, encoding="utf-8")
    os.replace(part, path)


def data_fingerprint(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def merge_results(directory: Path, order: list[str]) -> int:
    """Rewrite the merged JSONL from the per-user files, in data order; returns the user count."""
    files = {p.stem: p for p in (directory / "tmp").glob("*.json")}
    lines = [files[u].read_text(encoding="utf-8").strip() for u in order if u in files]
    write_atomic(directory / f"{FRAME}_eval_results.jsonl", "".join(x + "\n" for x in lines))
    return len(lines)


def _check_resume(run_path: Path, run_info: dict[str, Any]) -> None:
    if not run_path.exists():
        beam_run._write_json(run_path, run_info)
        return
    prior = json.loads(run_path.read_text(encoding="utf-8"))
    differences = [k for k in RESUME_KEYS if prior.get(k) != run_info.get(k)]
    if differences:
        raise SystemExit(f"{run_path.parent} holds a run under other settings ({', '.join(differences)})")


async def cmd_run(
    args: argparse.Namespace,
    *,
    llm: Any = None,
    embeddings: Any = None,
    answer: runner.Answer | None = None,
    prepare: bool = True,
) -> list[str]:
    """Run the selected users; returns the uuids written by this call."""
    overrides, ignored = beam_run.resolve_overrides(args)
    db_url = beam_run._require_db(args)
    eval_dir = args.halumem_dir / "eval"
    answer_prompt = corpus.load_string_constant(eval_dir / "prompts.py", ANSWER_PROMPT_NAME)
    memory_template = corpus.load_string_constant(eval_dir / "eval_memzero.py", MEMORY_TEMPLATE_NAME)
    all_users = corpus.load_users(args.data)
    users = select_users(all_users, args.users)
    out = result_dir(args)
    (out / "tmp").mkdir(parents=True, exist_ok=True)
    first = arms.build_settings(db_url, agent_id(users[0]["uuid"], args.agent_tag), overrides)
    run_info = beam_run._json(
        {
            "arm": args.arm,
            "agent_tag": args.agent_tag,
            "sleep": args.sleep,
            "answer_model": args.answer_model,
            "data": args.data.name,
            "data_sha256": data_fingerprint(args.data),
            "data_users": [u["uuid"] for u in all_users],
            "limit_sessions": args.limit_sessions,
            "halumem_revision": checkout_revision(args.halumem_dir),
            "answer_prompt": ANSWER_PROMPT_NAME,
            "memory_template": MEMORY_TEMPLATE_NAME,
            "update_top_k": runner.UPDATE_TOP_K,
            "owner_named": "scored memories: 'the user' -> persona name (runner.name_owner)",
            "recall_deep_limit": harness.RECALL_DEEP_LIMIT,
            **beam_run.run_settings_record(first, overrides, ignored),
        }
    )
    _check_resume(out / "run.json", run_info)
    judge = scoring.OpenAIJudge(first.openai_api_key, model=args.answer_model) if answer is None else None

    async def openai_answer(prompt: str) -> str:
        return await judge([{"role": "user", "content": prompt}])

    owns_llm = llm is None
    if owns_llm:
        from nous.api.anthropic_client import create_client

        llm = create_client(first)
        await llm.start()
    counter = UsageCounter(llm)
    written: list[str] = []
    try:
        for user in users:
            path = out / "tmp" / f"{user['uuid']}.json"
            if path.exists():
                logger.info("skipping %s: %s exists", user["uuid"], path.name)
                continue
            settings = arms.build_settings(db_url, agent_id(user["uuid"], args.agent_tag), overrides)
            if prepare:
                await harness.prepare_db(settings)
                prepare = False
            fresh = await fresh_agent_id(settings, settings.agent_id)
            if fresh != settings.agent_id:
                settings = arms.build_settings(db_url, fresh, overrides)
            counter.take()
            async with harness.open_stack(
                settings, llm=counter, embeddings=embeddings, sleep=args.sleep, pinned=beam_run.pinned_fields(overrides)
            ) as stack:
                beam_run.record_runtime_config(out / "run.json", stack.runtime_config)
                result = await runner.run_user(
                    stack,
                    user,
                    answer=answer or openai_answer,
                    answer_prompt=answer_prompt,
                    memory_template=memory_template,
                    sleep=args.sleep,
                    limit_sessions=args.limit_sessions,
                )
                result["nous_run"]["rows"] = await harness.count_rows(stack)
            result["nous_run"]["anthropic_usage"] = counter.take()
            write_atomic(path, json.dumps(result, ensure_ascii=False, default=str))
            (out / "tmp2" / f"{user['uuid']}.json").unlink(missing_ok=True)  # evaluation.py's judgement cache
            written.append(user["uuid"])
            logger.info("user %s done: %s", user["uuid"], result["nous_run"])
    finally:
        if judge is not None:
            await judge.close()
        if owns_llm:
            await llm.close()
        merge_results(out, [u["uuid"] for u in all_users])
    return written


def judge_command(eval_dir: Path, version: str, n_users: int) -> list[str]:
    """HaluMem's evaluation.main for frame ``nous`` (its CLI accepts only its own frames)."""
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import evaluation; "
        "evaluation.main(sys.argv[2], sys.argv[3], user_num=int(sys.argv[4]))"
    )
    deps = ["--with", "openai", "--with", "tenacity", "--with", "python-dotenv", "--with", "tqdm"]
    # -X utf8: evaluation.py prints emoji, which a Windows console code page cannot encode.
    python = ["python", "-I", "-X", "utf8", "-c", code]
    return ["uv", "run", "--no-project", *deps, *python, str(eval_dir), FRAME, version, str(n_users)]


def cmd_score(args: argparse.Namespace) -> Path:
    from nous.config import Settings

    out = result_dir(args)
    if not (out / "run.json").exists():
        raise SystemExit(f"no {out / 'run.json'}; run first")
    # Rebuilt from the per-user files: parallel runs each merge what they saw, and the last
    # merge written can predate another process's user.
    info = json.loads((out / "run.json").read_text(encoding="utf-8"))
    n_users = merge_results(out, info.get("data_users") or sorted(p.stem for p in (out / "tmp").glob("*.json")))
    key = Settings().openai_api_key
    if not key:
        raise SystemExit("OPENAI_API_KEY is required for the HaluMem judge")
    env = {**os.environ, **JUDGE_ENV, "OPENAI_MODEL": args.judge_model, "OPENAI_API_KEY": key}
    env.pop("OPENAI_BASE_URL", None)
    cmd = judge_command((args.halumem_dir / "eval").resolve(), args.version, n_users)
    subprocess.run(cmd, cwd=args.out_dir, env=env, check=True)
    return out / f"{FRAME}_eval_stat_result.json"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args(argv)
    arms.check_out_dir(args.out_dir)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    if args.cmd == "run":
        asyncio.run(cmd_run(args))
    else:
        stats = json.loads(cmd_score(args).read_text(encoding="utf-8"))
        print(json.dumps({k: v for k, v in stats["overall_score"].items()}, indent=2))
    return 0
