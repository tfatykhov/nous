"""CLI: ``python -m nous_eval.served {recall,preturn,compare,cost} ...`` (see README.md here)."""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from nous_eval.served import inputs as inp
from nous_eval.served import pin
from nous_eval.served.judge import (
    AGREEMENT_MODEL,
    PRIMARY_MODEL,
    CostLedger,
    LabelCache,
    cache_path,
    judge_units,
    make_llm_client,
    sample_units,
    units_for_rows,
)
from nous_eval.served.stats import SEED

logger = logging.getLogger("nous_eval.served")
CREDENTIAL_NAMES = ("OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
_TAG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def _code_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--code", help="the code to measure: a directory, or a commit (gets a detached worktree)")
    p.add_argument("--worktree-dir", type=Path, help="where --code commits are checked out (default: next to the repo)")
    p.add_argument("--allow-dirty", action="store_true", help="measure uncommitted changes (recorded)")
    p.add_argument("--pinned-root", help=argparse.SUPPRESS)  # set by the re-exec
    p.add_argument("--env-file", type=Path, help="dotenv file; only OPENAI_API_KEY / ANTHROPIC_* are read, by name")


def _db_args(p: argparse.ArgumentParser) -> None:
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--db-template", help="clone this database for the run (CREATE DATABASE ... TEMPLATE)")
    g.add_argument("--db-name", help="run on this scratch database directly (nous_eval*, nous_test*, beam*)")
    p.add_argument("--keep-db", action="store_true", help="keep the clone after the run")
    p.add_argument("--db-host", default="127.0.0.1")
    p.add_argument("--db-port", type=int, default=5433)
    p.add_argument("--db-user", default="nous")
    p.add_argument("--db-password", default=None, help="default: EvalSettings' (the local eval container's)")
    p.add_argument("--agent", default="nous-default")
    p.add_argument("--no-migrate", action="store_true", help="do not apply pending migrations to the database")
    p.add_argument(
        "--i-know-this-is-scratch",
        action="store_true",
        help="--db-name may be a database whose name is not nous_eval*/nous_test*/beam*",
    )


def _settings_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--base",
        type=Path,
        action="append",
        default=[],
        help="base layer, in order: a .json settings snapshot or a dotenv flags file (repeatable)",
    )
    p.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="NOUS_NAME=VALUE",
        help="the arm's override (repeatable; one variable per arm)",
    )
    p.add_argument("--arm", default="base", help="the arm's name, recorded")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m nous_eval.served", description=__doc__)
    sub = ap.add_subparsers(dest="entry", required=True)
    entries: dict[str, Any] = {}
    for entry in ("recall", "preturn"):
        e = entries[entry] = sub.add_parser(entry).add_subparsers(dest="cmd", required=True)
        r = e.add_parser("run", help="one arm over the inputs; writes <data>/runs/<entry>/<tag>/")
        r.add_argument("--data-dir", type=Path, required=True)
        r.add_argument("--tag", required=True)
        if entry == "recall":
            r.add_argument("--queries", type=Path, help="recall_deep queries JSONL (set prod)")
        else:
            r.add_argument("--prompts", type=Path, help="prod_templated_prompts.json (set prod)")
        r.add_argument("--gold", type=Path, action="append", default=[], help="gold JSONL (repeatable)")
        r.add_argument(
            "--as-of",
            default=datetime.date.today().isoformat(),
            help="date relative dates resolve against when an input has no logged date",
        )
        r.add_argument("--n", type=int, default=0, help="smoke: a fixed-seed sample of n inputs")
        r.add_argument("--warm", type=int, default=10, help="warm-up inputs, results discarded")
        _settings_args(r)
        _db_args(r)
        _code_args(r)
        j = e.add_parser("judge", help="label a run's rendered items into the shared cache")
        j.add_argument("run_dir", type=Path)
        j.add_argument("--model", default=PRIMARY_MODEL)
        j.add_argument("--sample", type=int, default=0, help="judge only this many queries (fixed seed)")
        j.add_argument("--sets", default="prod", help="comma list of input sets to judge")
        j.add_argument("--concurrency", type=int, default=4)
        j.add_argument("--env-file", type=Path)
        s = e.add_parser("score", help="metrics of one run -> scores.json")
        s.add_argument("run_dir", type=Path)
        s.add_argument("--alt-judge", default=AGREEMENT_MODEL, help="agreement check against this judge's labels")
        s.add_argument("--keys", type=Path, help="restrict to the input keys in this file (one per line)")
    q = entries["recall"].add_parser("queries", help="distinct recall_deep queries from a copy's retrieval_log")
    q.add_argument("--data-dir", type=Path, required=True)
    q.add_argument("--out", type=Path, required=True)
    q.add_argument("--days", type=int, default=30)
    q.add_argument("--tag", default="queries")
    g = entries["preturn"].add_parser("gold", help="LLM-written gold questions for sampled memories of one type")
    g.add_argument("--data-dir", type=Path, required=True)
    g.add_argument("--type", required=True, choices=inp.GOLD_TYPES)
    g.add_argument("--n", type=int, default=120, help="first stratum (facts: other; episodes: untagged)")
    g.add_argument("--n-second", type=int, default=0, help="second stratum (facts: tier-1; episodes: tagged)")
    g.add_argument("--out", type=Path, required=True)
    g.add_argument("--tag", default="gold")
    for p in (q, g):
        _settings_args(p)
        _db_args(p)
        _code_args(p)
    c = sub.add_parser("compare", help="paired deltas and the win condition for two runs")
    c.add_argument("run_a", type=Path, help="control")
    c.add_argument("run_b", type=Path, help="treatment")
    c.add_argument(
        "--gold-loss-tolerance",
        type=float,
        required=True,
        help="largest gold-in-served / gold-in-context loss (fraction) still acceptable",
    )
    c.add_argument("--top10-loss-tolerance", type=float, default=0.0)
    c.add_argument("--max-p95-regression", type=float, default=0.20)
    c.add_argument("--keys", type=Path)
    c.add_argument("--out", type=Path, help="also write the full comparison as JSON")
    k = sub.add_parser("cost", help="LLM spend recorded in a data dir")
    k.add_argument("--data-dir", type=Path, required=True)
    k.add_argument("--run", default=None)
    return ap.parse_args(argv)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _load_credentials(path: Path | None) -> None:
    """Copy ONLY the credential names from a dotenv file into the environment (never printed)."""
    if path is None or not path.exists():
        return
    from dotenv import dotenv_values

    for name, value in dotenv_values(path).items():
        if name in CREDENTIAL_NAMES and value and not os.environ.get(name):
            os.environ[name] = value


def _check_data_dir(path: Path) -> Path:
    from nous_eval.beam.arms import check_out_dir

    path = path.resolve()
    path.mkdir(parents=True, exist_ok=True)
    check_out_dir(path)
    if (path / ".env").exists():  # the search leaves build their own Settings() from the cwd's .env
        raise SystemExit(f"{path} holds a .env; the data dir must not (it is the working directory of a run)")
    return path


def _strip_code_arg(argv: list[str]) -> list[str]:
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
            continue
        if a == "--code":
            skip = True
            continue
        if a.startswith("--code="):
            continue
        out.append(a)
    return out


def _maybe_reexec(args: argparse.Namespace, argv: list[str]) -> int | None:
    """With --code, run this command again inside the pinned root; None when already there."""
    if not getattr(args, "code", None) or args.pinned_root:
        return None
    repo = pin.imported_roots()[0]
    root = pin.resolve_code_root(
        args.code, repo=repo, worktree_base=args.worktree_dir or repo.parent / "nous-served-wt"
    )
    data_dir = _check_data_dir(args.data_dir)
    print(f"pinned code: {root}")
    return pin.reexec(root, _strip_code_arg(argv), cwd=data_dir)


def _db_conn(args: argparse.Namespace) -> dict[str, Any]:
    pw = args.db_password
    if pw is None:
        from nous_eval.config import EvalSettings

        pw = EvalSettings(_env_file=None).db_password
    return {"db_host": args.db_host, "db_port": args.db_port, "db_user": args.db_user, "db_password": pw}


def _quiet() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logger.setLevel(logging.INFO)
    for noisy in ("httpx", "nous", "nous_eval.beam", "nous_eval.retrieval_runner"):
        logging.getLogger(noisy).setLevel(logging.ERROR)


class _Prepared:
    """Settings, code record and database of a command that opens a stack."""

    def __init__(self, args: argparse.Namespace, entry: str, tag: str) -> None:
        from nous_eval.served import db as dbm
        from nous_eval.served import settings as st

        if not _TAG_RE.match(tag):
            raise SystemExit(f"--tag {tag!r}: letters, digits, . _ - only (max 40)")
        self.code = pin.code_info()
        pin.check_code(self.code, expected_root=args.pinned_root, allow_dirty=args.allow_dirty)
        _load_credentials(args.env_file)
        self.data_dir = _check_data_dir(args.data_dir)
        os.chdir(self.data_dir)
        self.conn = _db_conn(args)
        self.template = args.db_template
        if self.template:
            self.db_name = dbm.clone_name(f"{entry}_{tag}")
        else:
            self.db_name = (
                dbm.check_ident(args.db_name) if args.i_know_this_is_scratch else dbm.check_scratch(args.db_name)
            )
        self.keep = bool(args.keep_db) or not self.template
        self.resolved = st.resolve(
            args.base,
            args.set,
            agent_id=args.agent,
            db={**self.conn, "db_name": self.db_name, "db_pool_size": 10, "db_max_overflow": 5},
        )

    async def __aenter__(self) -> _Prepared:
        from nous_eval.served import db as dbm

        if self.template:
            await dbm.create_clone(self.conn, self.template, self.db_name)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        from nous_eval.served import db as dbm

        if not self.keep:
            await dbm.drop_clone(self.conn, self.db_name)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


async def cmd_run(args: argparse.Namespace, *, embeddings: Any = None, llm: Any = None) -> int:
    from nous_eval.served import db as dbm
    from nous_eval.served import preturn, recall
    from nous_eval.served.stack import ce_environment, open_stack

    entry = args.entry
    run_dir = args.data_dir.resolve() / "runs" / entry / args.tag
    if (run_dir / "rows.jsonl").exists():
        raise SystemExit(f"{run_dir} has rows; pick another --tag")
    prep = _Prepared(args, entry, args.tag)
    if entry == "recall":
        items = inp.recall_items(
            args.queries.resolve() if args.queries else None, [g.resolve() for g in args.gold], args.as_of
        )
    else:
        items = inp.preturn_items(
            args.prompts.resolve() if args.prompts else None, [g.resolve() for g in args.gold], args.as_of
        )
    items = inp.sample(items, args.n, SEED)
    if not items:
        raise SystemExit("no inputs: pass --queries/--prompts and/or --gold")
    run_dir.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {
        "entry": entry,
        "tag": args.tag,
        "arm": args.arm,
        "data_dir": str(prep.data_dir),
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "code": prep.code,
        "python": sys.version.split()[0],
        "settings": prep.resolved.record(),
        "arm_overrides": {k: str(v) for k, v in prep.resolved.arm_overrides.items()},
        "base_files": prep.resolved.base_files,
        "ignored_base_names": {k: len(v) for k, v in prep.resolved.ignored.items()},
        "db_template": prep.template,
        "db_name": prep.db_name,
        "agent": args.agent,
        "inputs_sha256": inp.inputs_sha256(items),
        "n_inputs": {s: sum(1 for it in items if it["set"] == s) for s in ("prod", "gold")},
        "judge_model": PRIMARY_MODEL,
        "as_of": args.as_of,
        "warm": args.warm,
        "smoke_n": args.n,
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=1, default=str), encoding="utf-8")
    print(
        f"{entry} run {args.tag}: {len(items)} inputs {meta['n_inputs']}, code {prep.code['head'][:8]} "
        f"dirty={prep.code['dirty_files']}, db {prep.db_name}"
    )
    mod = recall if entry == "recall" else preturn
    async with prep:
        async with open_stack(
            prep.resolved.settings,
            pinned=sorted(prep.resolved.arm_overrides),
            data_dir=prep.data_dir,
            as_of=inp.as_of_map(items),
            snippet_chars=mod.SNIPPET_CHARS,
            embeddings=embeddings,
            llm=llm,
            migrate=not args.no_migrate,
        ) as stack:
            meta["gold_validity"] = await inp.mark_gold_validity(stack.db, args.agent, items)
            meta["runtime_config"] = stack.runtime
            meta["leaf_start"] = stack.leaf_start
            w0 = await dbm.table_write_counts(stack.db)
            t0 = time.monotonic()
            meta["run"] = await mod.run(stack, items, run_dir / "rows.jsonl", warm=args.warm)
            meta["run"]["seconds"] = round(time.monotonic() - t0, 1)
            await asyncio.sleep(1.0)  # pg_stat counters are flushed asynchronously
            meta["table_writes"] = dbm.write_diff(await dbm.table_write_counts(stack.db), w0)
            meta["leaf_seen"] = {k: dict(v) for k, v in stack.leaf_seen.items()}
            meta["ce_env"] = ce_environment(stack.settings)
            if stack.date_parser is not None:
                meta["date_store_entries"] = len(stack.date_parser.store)
    meta["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=1, default=str), encoding="utf-8")
    print(
        f"done: {meta['run']}; table writes during the run: {sum(meta['table_writes'].values())} rows "
        f"in {len(meta['table_writes'])} tables -> {run_dir}"
    )
    return 0 if not meta["run"]["errors"] else 3


async def cmd_judge(args: argparse.Namespace, *, client: Any = None) -> int:
    from nous_eval.served.score import load_run

    meta, rows = load_run(args.run_dir)
    sets = set(args.sets.split(","))
    units = units_for_rows(meta["entry"], [r for r in rows if r.get("set") in sets])
    units = sample_units(units, args.sample, SEED + 1)
    data_dir = Path(meta["data_dir"])
    cache = LabelCache(cache_path(data_dir, args.model))
    ledger = CostLedger(data_dir / "cost_ledger.jsonl")
    owns = client is None
    if owns:
        _load_credentials(args.env_file)
        client = await make_llm_client()
    try:
        counts = await judge_units(
            meta["entry"],
            units,
            model=args.model,
            cache=cache,
            client=client,
            ledger=ledger,
            run=meta["tag"],
            concurrency=args.concurrency,
        )
    finally:
        if owns:
            await client.close()
    spend = ledger.summary(run=meta["tag"])["usd_total"]
    print(f"judge {args.model}: {len(units)} queries, {counts}; run spend {spend} USD")
    return 0 if not counts.get("failed_calls") else 3


def _keys(path: Path | None) -> set[str] | None:
    return None if path is None else {x.strip() for x in path.read_text(encoding="utf-8").splitlines() if x.strip()}


def cmd_score(args: argparse.Namespace) -> int:
    from nous_eval.served.score import summarize

    out = summarize(args.run_dir, alt_model=args.alt_judge or None, keys=_keys(args.keys))
    name = "scores.json" if args.keys is None else f"scores_{args.keys.stem}.json"
    (args.run_dir / name).write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    print(json.dumps(_compact(out), indent=1, default=str))
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    from nous_eval.served.compare import compare

    rep = compare(
        args.run_a,
        args.run_b,
        gold_loss_tolerance=args.gold_loss_tolerance,
        top10_loss_tolerance=args.top10_loss_tolerance,
        max_p95_regression=args.max_p95_regression,
        keys=_keys(args.keys),
    )
    if args.out:
        args.out.write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    print(render_compare(rep))
    return 0


def render_compare(rep: dict[str, Any]) -> str:
    """The comparison as text: differences, deltas with CIs, the win condition."""

    def f(d: dict, nd: int = 3) -> str:
        return f"{d['a']:.{nd}f} -> {d['b']:.{nd}f}  {d['delta']:+.{nd}f} [{d['ci'][0]:+.{nd}f}, {d['ci'][1]:+.{nd}f}]"

    diff = rep["differences"]
    lines = [
        f"{rep['entry']}: {rep['a']} (A) vs {rep['b']} (B); {rep['n_shared']} shared inputs, "
        f"{rep['n_judged_both']} judged in both",
        f"differences ({diff['n_variables']}):",
    ]
    for n, v in diff["settings"].items():
        lines.append(f"  setting {n}: {v['a']!r} -> {v['b']!r}")
    for k, v in diff.items():
        if k not in ("settings", "n_variables"):
            lines.append(f"  {k}: {str(v['a'])[:60]} -> {str(v['b'])[:60]}")
    m = rep["prod"]
    for k, v in m.items():
        if isinstance(v, dict) and "delta" in v:
            lines.append(f"  {k:18s} {f(v, 1 if k in ('tokens', 'tokens_memory', 'n', 'rel') else 3)}")
    for k, v in m.get("latency_ms", {}).items():
        lines.append(f"  latency {k:10s} {f(v, 0)}")
    for group in ("per_leg", "by_type", "by_band"):
        for k, v in (m.get(group) or {}).items():
            lines.append(f"  {group}.{k:16s} {f(v)}")
    for t, g in rep["gold"].items():
        for k, v in g.items():
            if isinstance(v, dict):
                lines.append(f"  gold {t:8s} {k:6s} (n={g['n']}) {f(v)}")
    w = rep["win"]
    lines.append(f"win condition: {'PASS' if w['win'] else 'FAIL'}; one variable: {w['one_variable']}")
    for k, c in w["checks"].items():
        lines.append(f"  {k}: {c['pass']}  {json.dumps({x: y for x, y in c.items() if x != 'pass'}, default=str)}")
    return "\n".join(lines)


async def cmd_queries(args: argparse.Namespace) -> int:
    from nous.storage.database import Database
    from nous_eval.served.recall import extract_queries

    prep = _Prepared(args, "queries", args.tag)
    async with prep:
        db = Database(prep.resolved.settings)
        await db.connect()
        try:
            rows = await extract_queries(db, args.agent, args.days)
        finally:
            await db.disconnect()
    args.out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    print(f"{len(rows)} distinct queries -> {args.out}")
    return 0


async def cmd_gold(args: argparse.Namespace, *, embeddings: Any = None, llm: Any = None) -> int:
    from nous_eval.served import gold as goldmod
    from nous_eval.served import preturn
    from nous_eval.served.stack import open_stack

    prep = _Prepared(args, "gold", args.tag)
    ledger = CostLedger(prep.data_dir / "cost_ledger.jsonl")
    async with prep:
        async with open_stack(
            prep.resolved.settings,
            pinned=sorted(prep.resolved.arm_overrides),
            data_dir=prep.data_dir,
            as_of={},
            snippet_chars=preturn.SNIPPET_CHARS,
            embeddings=embeddings,
            llm=llm,
            migrate=not args.no_migrate,
        ) as stack:
            client = llm if llm is not None else await make_llm_client()
            try:
                stats = await goldmod.generate(
                    stack,
                    client,
                    gold_type=args.type,
                    n=args.n,
                    n_second=args.n_second,
                    seed=SEED,
                    out=args.out.resolve(),
                    ledger=ledger,
                    run=args.tag,
                )
            finally:
                if llm is None:
                    await client.close()
    print(f"gold {args.type}: {stats} -> {args.out}; spend {ledger.summary(run=args.tag)['usd_total']} USD")
    return 0


def _compact(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: _compact(v) for k, v in x.items() if k not in ("settings",)}
    if isinstance(x, float):
        return round(x, 4)
    return x


def main(argv: list[str] | None = None, **inject: Any) -> int:
    """Entry point; ``inject`` passes test doubles (embeddings, llm, client) to the commands."""
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(argv)
    _quiet()
    if args.entry == "compare":
        return cmd_compare(args)
    if args.entry == "cost":
        print(json.dumps(CostLedger(args.data_dir / "cost_ledger.jsonl").summary(run=args.run), indent=1))
        return 0
    if args.cmd in ("run", "queries", "gold"):
        code = _maybe_reexec(args, argv)
        if code is not None:
            return code
    if args.cmd == "run":
        return asyncio.run(cmd_run(args, embeddings=inject.get("embeddings"), llm=inject.get("llm")))
    if args.cmd == "judge":
        return asyncio.run(cmd_judge(args, client=inject.get("client")))
    if args.cmd == "score":
        return cmd_score(args)
    if args.cmd == "queries":
        return asyncio.run(cmd_queries(args))
    if args.cmd == "gold":
        return asyncio.run(cmd_gold(args, embeddings=inject.get("embeddings"), llm=inject.get("llm")))
    raise SystemExit(f"unknown command {args.cmd}")
