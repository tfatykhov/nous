"""F051 Phase 1 finish: shared eval-run persistence to the eval DB.

Originally `_persist_run_history` lived inside `nous_eval/retrieval.py` and
targeted the **main** Nous DB. In practice evals run on dev machines without
the full Nous stack up, so the silent ``main DB unreachable`` warning meant
0 rows accumulated for weeks. This module:

1. Targets the **eval DB** (self-contained, runs whenever the eval harness runs).
2. Is reusable from any harness CLI (retrieval, multi_turn_eval, future
   per-handler evals from F056).
3. Wraps writes in `asyncio.wait_for` per F051's silent-failure guard.

Schema: `nous_system.eval_runs` (sql/migrations/037_eval_runs.sql).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nous.config import Settings
    from nous_eval.config import EvalSettings

logger = logging.getLogger(__name__)


async def persist_run_history(
    eval_settings: "EvalSettings",
    main_settings: "Settings",
    *,
    git_sha: str,
    fixture_version: str,
    configs_payload: list[dict[str, Any]],
    metrics_payload: dict[str, Any],
    qrel_counts: dict[str, int],
    report_path: str,
    notes: str,
    timeout_s: float | None = None,
) -> bool:
    """Insert one row into `nous_system.eval_runs` on the **eval** DB.

    Returns True on successful insert, False on any failure (best-effort —
    callers should not depend on persistence to surface signal).

    `timeout_s`: defaults to `eval_settings.run_history_insert_timeout_s`.
    """
    if not eval_settings.run_history_enabled:
        logger.debug("F051: run_history disabled (run_history_enabled=False)")
        return False

    deadline = timeout_s if timeout_s is not None else eval_settings.run_history_insert_timeout_s
    try:
        await asyncio.wait_for(
            _do_insert(
                eval_settings=eval_settings,
                main_settings=main_settings,
                git_sha=git_sha,
                fixture_version=fixture_version,
                configs_payload=configs_payload,
                metrics_payload=metrics_payload,
                qrel_counts=qrel_counts,
                report_path=report_path,
                notes=notes,
            ),
            timeout=deadline,
        )
        return True
    except asyncio.TimeoutError:
        logger.warning("F051: run_history_persist_timed_out (%.1fs)", deadline)
        return False
    except Exception:
        logger.exception("F051: run_history_persist_failed")
        return False


async def _do_insert(
    eval_settings: "EvalSettings",
    main_settings: "Settings",
    *,
    git_sha: str,
    fixture_version: str,
    configs_payload: list[dict[str, Any]],
    metrics_payload: dict[str, Any],
    qrel_counts: dict[str, int],
    report_path: str,
    notes: str,
) -> None:
    from sqlalchemy import text

    from nous.storage.database import Database
    from nous_eval.retrieval_runner import _settings_for_eval_db

    db_settings = _settings_for_eval_db(eval_settings, main_settings)
    db = Database(db_settings)
    try:
        await db.connect()
    except Exception as exc:
        logger.warning("F051: eval DB unreachable for run_history: %s", exc)
        await db.engine.dispose()
        return

    try:
        async with db.session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO nous_system.eval_runs (
                        agent_id, git_sha, fixture_version, configs,
                        metrics, qrel_counts, report_path, notes
                    )
                    VALUES (
                        :agent_id, :git_sha, :fixture_version,
                        CAST(:configs AS JSONB),
                        CAST(:metrics AS JSONB),
                        CAST(:qrel_counts AS JSONB),
                        :report_path, :notes
                    )
                    """
                ),
                {
                    "agent_id": eval_settings.agent_id,
                    "git_sha": git_sha,
                    "fixture_version": fixture_version,
                    "configs": json.dumps(configs_payload),
                    "metrics": json.dumps(metrics_payload),
                    "qrel_counts": json.dumps(qrel_counts),
                    "report_path": report_path,
                    "notes": notes,
                },
            )
            await session.commit()
            logger.info("F051: run_history_persisted (eval DB)")
    finally:
        await db.engine.dispose()
