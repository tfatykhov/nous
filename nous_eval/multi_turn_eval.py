"""F051.4 multi-turn replay harness mode.

For each LongMemEval qrel, walks the haystack as if it were a real
conversation (each user-message → ``recall_deep`` via
``ToolDispatcher.dispatch`` with a stable ``session_id``), then runs
the gold question as the final turn via ``run_recall_pipeline``
directly (bypasses dispatcher to get structured results suitable for
``compute_metrics``).

F055 (Cross-Turn Residual Activation) reads ``_session_id`` injected by
the dispatcher to bias recall toward recently-surfaced items. F051.4
itself ships the dispatcher injection + ``recall_deep`` kwarg; until
F055 lands, the kwarg is silently ignored (fail-open).

Per-question_type breakdown in the markdown report exploits LongMemEval's
6 reasoning categories as a built-in ablation matrix:
  - single-session-* → F055 should lift recall (intra-session warm context)
  - multi-session    → F055 should NOT lift (cross-session out of scope)
  - temporal-reasoning, knowledge-update → mixed diagnostics
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from nous.api.retrieval_pipeline import run_recall_pipeline
from nous.config import Settings
from nous.storage.database import Database
from nous_eval.config import EvalSettings
from nous_eval.metrics import MetricsResult, compute_metrics
from nous_eval.qrels_loader import Qrel, QrelSource, load_qrels
from nous_eval.retrieval import _DEFAULT_CONFIGS, RetrievalConfig
from nous_eval.retrieval_runner import (
    QrelResult,
    RuntimeConfig,
    _apply_config_flags,
    _build_brain_for_eval,
    _build_heart_for_eval,
    _settings_for_eval_db,
)

logger = logging.getLogger(__name__)


_LME_AGENT_ID = "nous-lme-corpus"  # F051.5 ingest agent_id
_DEFAULT_LME_CACHE = Path.home() / ".cache" / "nous-eval" / "longmemeval" / "longmemeval_s.json"


# ---------------------------------------------------------------------------
# Public data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MultiTurnRunResult:
    """Outcome of one config under one multi-turn run.

    ``per_question_type`` is the F051.4 value-add over F051: arithmetic-mean
    aggregation per LongMemEval question_type so we can see F055's effect
    partitioned by category.
    """

    config: RetrievalConfig
    per_qrel: list[QrelResult]
    overall: MetricsResult
    per_question_type: dict[str, MetricsResult] = field(default_factory=dict)
    duration_seconds: float = 0.0
    n_walk_calls: int = 0  # total dispatcher invocations across all qrels


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user_messages(session: list | dict) -> list[str]:
    """Extract user-only messages from a LongMemEval session.

    LongMemEval sessions are either a list of {role, content} turns OR a
    dict with a ``turns`` key. Filter to ``role == "user"`` so we walk
    only the operator's hypothetical inputs (assistant turns are agent
    output, not recall queries).
    """
    turns = session if isinstance(session, list) else (session.get("turns") or [])
    return [
        str(t.get("content", "")).strip()
        for t in turns
        if isinstance(t, dict) and t.get("role") == "user" and t.get("content")
    ]


def _question_type(qrel: Qrel) -> str:
    """Pull question_type from qrel.notes (dict, post-F051.5-hotfix)."""
    if isinstance(qrel.notes, dict):
        return str(qrel.notes.get("question_type") or "(unknown)")
    return "(unknown)"


def _question_id(qrel: Qrel) -> str:
    """Pull question_id from qrel.notes (used as session_id for F055 state)."""
    if isinstance(qrel.notes, dict):
        return str(qrel.notes.get("question_id") or qrel.query[:40])
    return qrel.query[:40]


def _build_qrel_result(
    qrel: Qrel,
    qrel_index: int,
    pipeline_results: list,
    top_k: int,
    error: str | None = None,
) -> QrelResult:
    """Build a QrelResult from pipeline output suitable for compute_metrics."""
    retrieved_ids = [r.id for r in pipeline_results] if pipeline_results else []
    retrieved_types = [r.type for r in pipeline_results] if pipeline_results else []
    gold_set = {str(g) for g in qrel.gold_ids}
    rank_of_first_gold: int | None = None
    n_gold_in_top_k = 0
    for rank, rid in enumerate(retrieved_ids[:top_k], start=1):
        if str(rid) in gold_set:
            if rank_of_first_gold is None:
                rank_of_first_gold = rank
            n_gold_in_top_k += 1
    return QrelResult(
        qrel_index=qrel_index,
        qrel_query=qrel.query,
        qrel_source=qrel.source.value,
        retrieved_ids=retrieved_ids[:top_k],
        retrieved_types=retrieved_types[:top_k],
        rank_of_first_gold=rank_of_first_gold,
        n_gold_in_top_k=n_gold_in_top_k,
        n_gold_total=len(qrel.gold_ids),
        error=error,
        gold_ids=list(qrel.gold_ids),
    )


async def _reset_session_state(heart, agent_id: str, session_id: str) -> None:
    """F051.4: per-qrel reset. WM clear + ConversationState delete.

    Uses the verified-existing API. There is NO ``reset_turn_count`` method;
    ``delete_conversation_state`` drops the row entirely → next access sees
    a fresh state with ``turn_count=0``.
    """
    try:
        await heart.working_memory.clear(session_id, session=None)
    except Exception:
        logger.exception("F051.4: working_memory.clear raised for %s", session_id)
    try:
        await heart.delete_conversation_state(agent_id, session_id)
    except Exception:
        logger.exception(
            "F051.4: delete_conversation_state raised for agent=%s session=%s",
            agent_id, session_id,
        )


def _load_lme_haystack_index(cache_path: Path) -> dict[str, dict[str, Any]]:
    """Load LongMemEval cache + index by question_id for haystack lookup."""
    if not cache_path.exists():
        raise SystemExit(
            f"F051.4: LongMemEval cache not found at {cache_path}. "
            f"Run `python -m nous_eval.ingest_longmemeval --n 20` first."
        )
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit(f"F051.4: unexpected LongMemEval JSON shape at {cache_path}")
    return {entry["question_id"]: entry for entry in data if "question_id" in entry}


# ---------------------------------------------------------------------------
# Per-config runner
# ---------------------------------------------------------------------------


async def _run_one_config(
    config: RetrievalConfig,
    main_settings_template: Settings,
    eval_settings: EvalSettings,
    qrels: list[Qrel],
    haystack_index: dict[str, dict[str, Any]],
    max_turns: int,
    top_k: int,
) -> MultiTurnRunResult:
    """Run one config across all qrels — walk haystack + score gold question.

    Pattern mirrors :mod:`nous_eval.retrieval_runner.run_matrix:160-220`:
    RuntimeConfig.reset → Settings overlay → eval-DB scoping → per-qrel loop.
    """
    RuntimeConfig.reset()
    logger.info("F051.4: running multi-turn config=%s", config.name)
    overridden = _apply_config_flags(main_settings_template, config)
    eval_scoped = _settings_for_eval_db(eval_settings, overridden)

    # F051.4 pre-condition: agent_id must match F051.5 ingest agent_id or
    # Heart.recall sees zero LongMemEval episodes.
    eval_scoped = eval_scoped.model_copy(update={"agent_id": _LME_AGENT_ID})

    t0 = time.monotonic()
    eval_db = Database(eval_scoped)
    n_walk_calls = 0
    per_qrel: list[QrelResult] = []
    try:
        await eval_db.connect()
        # ToolDispatcher import is lazy — F051.4 only needs it for walk turns.
        from nous.api.tools import ToolDispatcher

        async with _build_heart_for_eval(eval_db, eval_scoped) as heart:
            brain = _build_brain_for_eval(eval_db, eval_scoped, heart._embeddings)
            dispatcher = ToolDispatcher()

            # Inline a minimal recall_deep wrapper for the dispatcher. We
            # don't call available_tools() because that drags in prod-only
            # deps (anthropic client, tool registry, bus). The wrapper
            # mirrors the production recall_deep closure: takes _session_id
            # via dispatcher injection (F051.4 added the branch) and
            # delegates to run_recall_pipeline. F055 reads _session_id off
            # the args dict to compute residual_activations (when shipped).
            async def _eval_recall_deep(
                query: str,
                limit: int = 10,
                memory_types: list[str] | None = None,
                _session_id: str | None = None,
            ) -> dict[str, Any]:
                _ = _session_id  # consumed by F055 when implemented
                try:
                    results, _stats = await run_recall_pipeline(
                        query=query,
                        heart=heart,
                        brain=brain,
                        settings=eval_scoped,
                        limit=limit,
                        memory_types=memory_types,
                    )
                except Exception as exc:
                    return {"content": [{"type": "text", "text": f"recall error: {exc}"}]}
                lines = [f"{r.id} ({r.type}, score={r.score:.3f})" for r in results]
                return {"content": [{"type": "text", "text": "\n".join(lines) or "(no results)"}]}

            dispatcher.register(
                "recall_deep", _eval_recall_deep,
                {"name": "recall_deep", "description": "F051.4 eval wrapper", "input_schema": {"type": "object"}},
            )

            for qrel_index, qrel in enumerate(qrels):
                qid = _question_id(qrel)
                session_id = qid
                qtype = _question_type(qrel)

                await _reset_session_state(heart, eval_scoped.agent_id, session_id)

                # Walk: load haystack from cache, take user-only turns.
                haystack_entry = haystack_index.get(qid)
                if haystack_entry is None:
                    logger.warning("F051.4: qid=%s not in haystack cache", qid)
                    pr = _build_qrel_result(qrel, qrel_index, [], top_k, error="haystack_missing")
                    per_qrel.append(pr)
                    continue

                walk_msgs: list[str] = []
                for session in haystack_entry.get("haystack_sessions", []) or []:
                    walk_msgs.extend(_user_messages(session))
                walk_msgs = walk_msgs[:max_turns]

                # Walk turns drive dispatcher (side-effects only — discard text).
                for user_msg in walk_msgs:
                    try:
                        _text, is_error = await dispatcher.dispatch(
                            name="recall_deep",
                            args={"query": user_msg, "limit": top_k},
                            session_id=session_id,
                        )
                        if is_error:
                            logger.warning("F051.4: dispatcher returned is_error=True for qid=%s", qid)
                        n_walk_calls += 1
                    except Exception:
                        logger.exception(
                            "F051.4: dispatcher raised mid-walk qid=%s msg=%r",
                            qid, user_msg[:50],
                        )

                # Final gold-question turn: bypass dispatcher for structured results.
                try:
                    pipeline_results, _stats = await run_recall_pipeline(
                        query=qrel.query,
                        heart=heart,
                        brain=brain,
                        settings=eval_scoped,
                        limit=top_k,
                        memory_types=qrel.memory_types,
                    )
                    pr = _build_qrel_result(qrel, qrel_index, pipeline_results, top_k)
                except Exception as exc:
                    logger.exception("F051.4: gold-question pipeline raised qid=%s", qid)
                    pr = _build_qrel_result(
                        qrel, qrel_index, [], top_k,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                per_qrel.append(pr)
                logger.debug(
                    "F051.4: qid=%s qtype=%s walk=%d rank_first_gold=%s",
                    qid, qtype, len(walk_msgs), pr.rank_of_first_gold,
                )
    finally:
        await eval_db.engine.dispose()

    overall = compute_metrics(per_qrel, top_k=top_k)

    # Per-question_type aggregation — arithmetic mean over qrels per type.
    per_qtype: dict[str, MetricsResult] = {}
    type_buckets: dict[str, list[QrelResult]] = {}
    for pr, qrel in zip(per_qrel, qrels):
        type_buckets.setdefault(_question_type(qrel), []).append(pr)
    for qtype, bucket in type_buckets.items():
        per_qtype[qtype] = compute_metrics(bucket, top_k=top_k)

    duration = time.monotonic() - t0
    return MultiTurnRunResult(
        config=config,
        per_qrel=per_qrel,
        overall=overall,
        per_question_type=per_qtype,
        duration_seconds=duration,
        n_walk_calls=n_walk_calls,
    )


# ---------------------------------------------------------------------------
# Top-level orchestration + reporting
# ---------------------------------------------------------------------------


_LOW_CONFIDENCE_N = 5  # per-qtype rows with n < this get a "low-confidence" suffix


def _format_pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def _write_report(
    results: list[MultiTurnRunResult],
    out_path: Path,
    n_qrels: int,
    max_turns: int,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("# F051.4 multi-turn-eval report")
    lines.append("")
    lines.append(f"_Generated: {datetime.now(tz=UTC):%Y-%m-%d %H:%M:%S} UTC_")
    lines.append(f"_qrels: {n_qrels}, max_turns_per_haystack: {max_turns}_")
    lines.append("")
    lines.append("| config | n_qrels | overall_MRR | overall_recall@10 | walk_calls | wall_s |")
    lines.append("|--------|---------|-------------|-------------------|------------|--------|")
    for r in results:
        lines.append(
            f"| {r.config.name} | {len(r.per_qrel)} | {r.overall.mrr:.3f} | "
            f"{r.overall.r_at_10:.3f} | {r.n_walk_calls} | {r.duration_seconds:.1f} |"
        )
    lines.append("")
    lines.append("## Per-question_type breakdown")
    lines.append("")
    # Build table rows: one row per question_type, columns = configs.
    all_qtypes = sorted({qt for r in results for qt in r.per_question_type})
    if results and all_qtypes:
        baseline = next((r for r in results if r.config.name == "baseline"), None)
        header = ["question_type", "n"] + [r.config.name + "_MRR" for r in results]
        if baseline is not None and len(results) > 1:
            header.append("Δ_vs_baseline")
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join(["---"] * len(header)) + "|")
        for qtype in all_qtypes:
            n = next(
                (r.per_question_type[qtype].n_qrels for r in results if qtype in r.per_question_type),
                0,
            )
            row = [qtype, str(n)]
            for r in results:
                m = r.per_question_type.get(qtype)
                row.append(f"{m.mrr:.3f}" if m else "—")
            if baseline is not None and len(results) > 1:
                base_mrr = baseline.per_question_type.get(qtype)
                other = next(
                    (r for r in results if r.config.name != "baseline" and qtype in r.per_question_type),
                    None,
                )
                if base_mrr is not None and other is not None and base_mrr.mrr > 0:
                    delta = (other.per_question_type[qtype].mrr - base_mrr.mrr) / base_mrr.mrr
                    suffix = "  ⚠ low-confidence" if n < _LOW_CONFIDENCE_N else ""
                    row.append(_format_pct(delta) + suffix)
                else:
                    row.append("—")
            lines.append("| " + " | ".join(row) + " |")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def run_multi_turn_eval(
    config_names: list[str],
    eval_settings: EvalSettings,
    qrels_path: Path,
    haystack_cache: Path,
    max_turns: int,
    top_k: int,
) -> Path:
    """Driver: load qrels, run each config, write report. Returns report path."""
    qrels = load_qrels(qrels_path, source_override=QrelSource.LONGMEMEVAL)
    if not qrels:
        raise SystemExit(
            f"F051.4: no qrels at {qrels_path}. "
            f"Run `python -m nous_eval.ingest_longmemeval --n 20` first."
        )
    haystack_index = _load_lme_haystack_index(haystack_cache)

    main_settings_template = Settings()
    results: list[MultiTurnRunResult] = []
    for name in config_names:
        if name not in _DEFAULT_CONFIGS:
            raise SystemExit(
                f"F051.4: unknown config {name!r}. Valid: {sorted(_DEFAULT_CONFIGS)}"
            )
        cfg = _DEFAULT_CONFIGS[name]
        result = await _run_one_config(
            cfg, main_settings_template, eval_settings,
            qrels, haystack_index, max_turns, top_k,
        )
        results.append(result)

    out_path = (
        Path(eval_settings.report_dir)
        / f"multi-turn-eval-{datetime.now(tz=UTC):%Y%m%d-%H%M%S}.md"
    )
    _write_report(results, out_path, len(qrels), max_turns)
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_qrels_path() -> Path:
    """Match F051.5's default: NOUS_EVAL_FIXTURES_DIR/qrels_longmemeval.jsonl."""
    import os

    base = Path(os.environ.get("NOUS_EVAL_FIXTURES_DIR", "tests/fixtures"))
    return base / "qrels_longmemeval.jsonl"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m nous_eval.multi_turn_eval")
    p.add_argument(
        "--configs", default="baseline,f055_on",
        help="Comma-separated config names. F055 ablations: f055_on, "
             "f055_seed_only, f055_boost_only, f055_geom_decay_0.3, "
             "f055_power_alpha_0.5 (after F055 ships).",
    )
    p.add_argument(
        "--qrels-path", type=Path, default=None,
        help="Path to LongMemEval qrels JSONL (default: F051.5 output).",
    )
    p.add_argument(
        "--haystack-cache", type=Path, default=_DEFAULT_LME_CACHE,
        help="Path to cached LongMemEval JSON (default: ~/.cache/nous-eval/...).",
    )
    p.add_argument(
        "--max-turns-per-haystack", type=int, default=30,
        help="Cap on per-qrel walk turns (default 30; cost guardrail).",
    )
    p.add_argument(
        "--top-k", type=int, default=10,
        help="Retrieval top-K for scoring (default 10).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    ns = _parse_args(argv)
    eval_settings = EvalSettings()
    qrels_path = ns.qrels_path or _default_qrels_path()
    out = asyncio.run(run_multi_turn_eval(
        config_names=ns.configs.split(","),
        eval_settings=eval_settings,
        qrels_path=qrels_path,
        haystack_cache=ns.haystack_cache,
        max_turns=ns.max_turns_per_haystack,
        top_k=ns.top_k,
    ))
    print(f"Wrote: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
