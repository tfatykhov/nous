"""Tests for the docs-vs-memory bucket aggregation in
scripts/eval/eval_context_packing.py.

The eval has two kinds of scenarios:
  - bucket="memory": gold info is something the agent would naturally
    memorize (events, decisions, architectural facts). These drive the
    headline sufficiency metric.
  - bucket="docs": gold info lives in CLAUDE.md / source code (env-var
    defaults, internal classnames). These are tracked as a
    known-limitation aside so they don't depress the headline metric.

These tests guard the aggregation logic and the SCENARIOS bucketing.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_eval_module():
    """Load scripts/eval/eval_context_packing.py as a module without
    needing the script to be on PYTHONPATH (it lives outside the
    nous_eval package on purpose — it's an operator-run script).
    """
    repo_root = Path(__file__).resolve().parent.parent
    script_path = repo_root / "scripts" / "eval" / "eval_context_packing.py"
    spec = importlib.util.spec_from_file_location(
        "_eval_context_packing", str(script_path),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["_eval_context_packing"] = module
    spec.loader.exec_module(module)
    return module


eval_mod = _load_eval_module()
aggregate_by_bucket = eval_mod.aggregate_by_bucket
SCENARIOS = eval_mod.SCENARIOS


def test_aggregate_separates_memory_from_docs():
    """Memory and docs buckets must be reported independently — a
    failure in a docs scenario must not pull down the headline metric."""
    results = [
        {"name": "a", "bucket": "memory", "sufficient": True},
        {"name": "b", "bucket": "memory", "sufficient": True},
        {"name": "c", "bucket": "memory", "sufficient": False},
        {"name": "d", "bucket": "docs", "sufficient": False},
        {"name": "e", "bucket": "docs", "sufficient": False},
    ]
    agg = aggregate_by_bucket(results)
    assert agg["memory_total"] == 3
    assert agg["memory_ok"] == 2
    assert agg["memory_rate"] == pytest.approx(2 / 3)
    assert agg["docs_total"] == 2
    assert agg["docs_ok"] == 0
    assert agg["docs_rate"] == 0.0


def test_aggregate_handles_no_docs_scenarios():
    """When zero docs scenarios are present, docs_rate is 0.0 and
    docs_total is 0 — must not divide-by-zero."""
    results = [
        {"name": "a", "bucket": "memory", "sufficient": True},
    ]
    agg = aggregate_by_bucket(results)
    assert agg["memory_rate"] == 1.0
    assert agg["docs_total"] == 0
    assert agg["docs_rate"] == 0.0


def test_aggregate_handles_no_memory_scenarios():
    """Symmetric guard: zero memory scenarios → memory_rate 0.0 not crash."""
    results = [
        {"name": "a", "bucket": "docs", "sufficient": True},
    ]
    agg = aggregate_by_bucket(results)
    assert agg["memory_total"] == 0
    assert agg["memory_rate"] == 0.0
    assert agg["docs_rate"] == 1.0


def test_aggregate_defaults_missing_bucket_to_memory():
    """Result rows without a bucket field default to memory — preserves
    backwards-compat with any pre-existing JSON reports a downstream
    tool might still feed in."""
    results = [
        {"name": "a", "sufficient": True},  # no bucket field
        {"name": "b", "bucket": "memory", "sufficient": False},
    ]
    agg = aggregate_by_bucket(results)
    assert agg["memory_total"] == 2
    assert agg["memory_ok"] == 1


def test_scenarios_correctly_bucketed():
    """The 8 hand-curated scenarios must match the corpus-probe audit:
    subtask_workers and skill_management are docs-only (gold expects
    information that never appears in memory); the other 6 are memory.

    If you add or rename a scenario, update this assertion deliberately
    — the bucket choice is a load-bearing decision about what the eval
    is actually measuring.
    """
    by_name = {sc.name: sc.bucket for sc in SCENARIOS}
    assert by_name == {
        "telegram_email": "memory",
        "heartbeat_overview": "memory",
        "skill_management": "docs",
        "subtask_workers": "docs",
        "rubric_evolution": "memory",
        "procedure_learning": "memory",
        "graph_densification": "memory",
        "cognitive_loop": "memory",
    }
