"""F051 Retrieval Evaluation Harness — public API surface.

The harness measures retrieval quality (MRR, P@K, R@K, nDCG) against a fixed
corpus + qrels set, supporting paired A/B between retrieval configs (e.g.
baseline vs ``f050_on``). It calls :func:`nous.api.retrieval_pipeline.run_recall_pipeline`
directly — the same pipeline that powers the ``recall_deep`` tool — so the
metrics reflect the full production stack (Heart memory + Brain decisions +
graph expansion + spreading activation + contradiction detection).

See ``docs/features/F051-retrieval-eval-harness.md`` for the full spec and
``docs/superpowers/plans/2026-04-20-f051-retrieval-eval-harness.md`` for the
implementation plan.
"""

from __future__ import annotations

from nous_eval.config import EvalSettings
from nous_eval.metrics import Delta, MetricsResult, compute_delta, compute_metrics
from nous_eval.qrels_loader import Qrel, QrelSource, load_qrels
from nous_eval.report import GateDecision, decide_gate_f050, render_json, render_markdown
from nous_eval.retrieval_runner import (
    QrelResult,
    RetrievalConfig,
    RunResult,
    run_matrix,
)
from nous_eval.source_registry import ResolvedSource, SourceRegistry, SourceSpec

__all__ = [
    "EvalSettings",
    "Qrel",
    "QrelSource",
    "load_qrels",
    "SourceRegistry",
    "SourceSpec",
    "ResolvedSource",
    "RetrievalConfig",
    "QrelResult",
    "RunResult",
    "run_matrix",
    "MetricsResult",
    "Delta",
    "compute_metrics",
    "compute_delta",
    "GateDecision",
    "decide_gate_f050",
    "render_markdown",
    "render_json",
]
