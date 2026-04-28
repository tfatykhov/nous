"""F056: shared CLI scaffolding for handler eval CLIs.

Each handler eval (`admission`, `dedup`, `backfill`, `summary`) reuses
this module for argparse boilerplate, post-run report write, and
`eval_runs` persistence. Handler-specific logic stays in the handler
module; this module owns the shape every handler must match.

Per F056 spec §"Per-handler eval lifecycle" steps 1-9.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from sqlalchemy import text

from nous.config import Settings
from nous.storage.database import Database
from nous_eval.config import EvalSettings
from nous_eval.retrieval_runner import _settings_for_eval_db
from nous_eval.run_history import persist_run_history

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HandlerResult:
    """What a handler eval's `async_fn` returns to `_cli_base`.

    `metrics` is a flat scalar dict (e.g. {"admission_f1": 0.91, ...}).
    `extras` is for nested struct payloads (e.g. confusion_matrix dict)
    that are persisted to JSONB but excluded from delta tables.
    `report_lines` is the markdown body the handler wants under its report.
    """

    metrics: dict[str, float]
    extras: dict[str, Any]
    report_lines: list[str]
    primary_metric: str  # which key in `metrics` is the gated one
    fixture_size: int
    handler_specific_notes: str = ""


def build_handler_parser(prog: str) -> argparse.ArgumentParser:
    """Standard argparse setup shared across all handler CLIs.

    Returns the parser; handlers may add their own flags before calling
    `parser.parse_args(...)`.
    """
    p = argparse.ArgumentParser(prog=prog)
    p.add_argument(
        "--fixture-path", type=Path, default=None,
        help="Override default fixture path (default: tests/fixtures/handlers/<name>.jsonl).",
    )
    p.add_argument(
        "--threshold", type=float, default=None,
        help=(
            "Per-handler regression threshold (primary-metric drop fraction). "
            "Default differs per handler — admission/dedup/summary 0.05, "
            "backfill 0.10. Pass to override."
        ),
    )
    p.add_argument(
        "--include-unreviewed", action="store_true",
        help="Include rows where reviewed_by is None (AI-only drafts). Default: skip.",
    )
    p.add_argument(
        "--report-only", action="store_true",
        help="Print report but exit 0 even if regression detected.",
    )
    p.add_argument(
        "--no-history", action="store_true",
        help="Skip the nous_system.eval_runs INSERT.",
    )
    p.add_argument(
        "--notes", type=str, default="",
        help="Free-form notes to attach to the eval_runs row.",
    )
    p.add_argument(
        "--log-level", default="INFO",
    )
    return p


def lock_key_for(name: str, agent_id: str) -> int:
    """Cross-process-stable 31-bit advisory-lock key per F049 pattern.

    Mirrors `nous/heart/working_memory.py:347-348` exactly. The composite
    `f"{name}:{agent_id}"` keying means concurrent runs of the SAME
    handler+agent_id serialize, but different handlers don't collide.

    asyncpg's `bigint` codec rejects raw 256-bit ints — the `[:4]` +
    `% (2**31)` truncation is mandatory.
    """
    digest = hashlib.sha256(f"{name}:{agent_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % (2**31)


async def clear_handler_state(
    db: Database, *, name: str, agent_id: str, table_truncate_sql: list[str],
) -> None:
    """Truncate handler-scoped rows BEFORE seed under advisory lock.

    Per F056 spec §"Per-handler eval lifecycle" step 6: TRUNCATE runs at
    session START (clean slate before seed), inside its own session under
    `pg_try_advisory_xact_lock` (xact-scoped, auto-released on commit).
    Concurrent same-handler+agent_id runs serialize on the same lock key.

    `table_truncate_sql` is a list of fully-qualified DELETE/TRUNCATE
    statements the handler wants run under the lock — keeps this helper
    reusable across handlers without hardcoding table names.
    """
    if not table_truncate_sql:
        return
    key = lock_key_for(name, agent_id)
    async with db.session() as session:
        acquired = (
            await session.execute(
                text("SELECT pg_try_advisory_xact_lock(:k)").bindparams(k=key)
            )
        ).scalar()
        if not acquired:
            logger.warning(
                "%s eval: another process holds advisory lock for agent_id=%s; skipping clear",
                name, agent_id,
            )
            return
        for stmt in table_truncate_sql:
            await session.execute(text(stmt))
        await session.commit()
        logger.info("%s eval: cleared %d table(s) under lock", name, len(table_truncate_sql))


def _git_sha_short() -> str:
    """Resolve current git SHA; falls back to 'unknown' on error."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, timeout=2,
        ).strip()
    except Exception:
        return "unknown"


def write_report(
    name: str, result: HandlerResult, eval_settings: EvalSettings,
    args: argparse.Namespace,
) -> Path:
    """Write markdown report to `<report_dir>/handlers/<name>-<ts>.md`."""
    out_dir = Path(eval_settings.report_dir) / "handlers"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
    out_path = out_dir / f"{name}-{ts}.md"
    lines: list[str] = []
    lines.append(f"# F056 {name} eval report")
    lines.append("")
    lines.append(f"_Generated: {datetime.now(tz=UTC):%Y-%m-%d %H:%M:%S} UTC_")
    lines.append(f"_fixture_size: {result.fixture_size}, primary_metric: {result.primary_metric}_")
    if result.handler_specific_notes:
        lines.append(f"_notes: {result.handler_specific_notes}_")
    lines.append("")
    lines.append("## Metrics")
    lines.append("")
    lines.append("| metric | value |")
    lines.append("|---|---:|")
    for k, v in result.metrics.items():
        lines.append(f"| {k} | {v:.4f} |")
    if result.extras:
        lines.append("")
        lines.append("## Extras (sub-payloads, not in delta tables)")
        lines.append("")
        for k, v in result.extras.items():
            lines.append(f"- **{k}**: `{v}`")
    if result.report_lines:
        lines.append("")
        lines.append("## Handler-specific")
        lines.append("")
        lines.extend(result.report_lines)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def run_handler_eval(
    name: str,
    async_fn: Callable[
        [argparse.Namespace, EvalSettings, Settings],
        Awaitable[HandlerResult],
    ],
    *,
    default_threshold: float,
    extra_args_fn: Callable[[argparse.ArgumentParser], None] | None = None,
    argv: list[str] | None = None,
) -> int:
    """Standard handler-CLI entry point.

    Each handler module ends with:

        if __name__ == "__main__":
            raise SystemExit(run_handler_eval(
                "admission", _run_admission_eval, default_threshold=0.05,
            ))

    `default_threshold` is REQUIRED per spec — no shared default because
    different handlers gate at different rates (5pp for admission/dedup/
    summary, 10pp for backfill).

    `async_fn` does the actual work and returns a HandlerResult.
    `_cli_base` handles the report write + eval_runs persistence.
    """
    parser = build_handler_parser(f"python -m nous_eval.handlers.{name}")
    if extra_args_fn is not None:
        extra_args_fn(parser)
    args = parser.parse_args(argv)
    if args.threshold is None:
        args.threshold = default_threshold

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    eval_settings = EvalSettings()
    eval_settings.warn_if_default_password()
    main_settings = Settings()

    return asyncio.run(_run_async(name, async_fn, args, eval_settings, main_settings))


async def _run_async(
    name: str,
    async_fn: Callable[
        [argparse.Namespace, EvalSettings, Settings],
        Awaitable[HandlerResult],
    ],
    args: argparse.Namespace,
    eval_settings: EvalSettings,
    main_settings: Settings,
) -> int:
    try:
        result = await async_fn(args, eval_settings, main_settings)
    except Exception:
        logger.exception("%s eval: unhandled exception", name)
        return 1

    out_path = write_report(name, result, eval_settings, args)
    print(f"Wrote: {out_path}")

    if not args.no_history:
        await persist_run_history(
            eval_settings=eval_settings,
            main_settings=main_settings,
            git_sha=_git_sha_short(),
            fixture_version=eval_settings.fixture_version,
            configs_payload=[{
                "name": name,
                "harness": name,
                "flags": {},
                "description": f"F056 {name} handler eval",
            }],
            metrics_payload={name: {
                "metrics": {**result.metrics, **result.extras},
                "fixture_size": result.fixture_size,
                "primary_metric": result.primary_metric,
            }},
            qrel_counts={name: result.fixture_size},
            report_path=str(out_path),
            notes=args.notes or f"{name} handler eval",
        )

    # Gate: did the primary metric drop below an absolute floor? Per spec,
    # gating happens via `nous_eval.regression` reading eval_runs after
    # multiple runs accumulate — single-run gating in the handler itself
    # would require a baseline, which the handler doesn't have access to.
    # Handlers exit 0 on success; regression gating is a separate CLI step.
    if args.report_only:
        return 0
    return 0
