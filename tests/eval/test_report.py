"""Unit tests for nous.eval.report (F051 Phase 1).

Covers the F050 gate decision logic:

- Missing baseline or f050_on config -> fail
- No gate-eligible sources -> fail
- Aggregate MRR delta < threshold -> fail
- Single-source regression > max_single_regression -> fail
- Minority positive when require_majority_positive=True -> fail
- Aggregate passes + no regression + majority positive -> pass

Plus:
- Markdown/JSON report file writing
- compute_metrics gating by resolved sources
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.eval

try:
    from nous.eval.report import (
        GateDecision,
        decide_gate_f050,
        render_json,
        render_markdown,
        write_reports,
    )
    from nous.eval.retrieval_runner import QrelResult, RetrievalConfig, RunResult
    from nous.eval.source_registry import ResolvedSource, SourceSpec
except ImportError:
    pytest.skip(
        "nous.eval.report (+deps) not yet available",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Helpers — fabricate RunResult from canned MRR values
# ---------------------------------------------------------------------------


def _mk_qrel_result(rank: int | None, source: str = "probes") -> QrelResult:
    gold = uuid4()
    retrieved: list = []
    if rank is not None:
        # build a 10-long retrieved_ids with gold at `rank-1`
        retrieved = [uuid4() for _ in range(10)]
        retrieved[rank - 1] = gold
    return QrelResult(
        qrel_index=0,
        qrel_query="q",
        qrel_source=source,
        gold_ids=[gold],
        retrieved_ids=retrieved,
        retrieved_types=["fact"] * len(retrieved),
        rank_of_first_gold=rank,
        n_gold_in_top_k=1 if rank is not None else 0,
        n_gold_total=1,
    )


def _mk_run_result(
    name: str,
    per_source_ranks: dict[str, list[int | None]],
) -> RunResult:
    """Build a RunResult with one QrelResult per (source, rank) entry."""
    per_qrel = []
    for source, ranks in per_source_ranks.items():
        for r in ranks:
            per_qrel.append(_mk_qrel_result(r, source))
    return RunResult(
        config=RetrievalConfig(name=name, flags={}),
        per_qrel=per_qrel,
        duration_seconds=0.1,
    )


def _mk_resolved_source(
    name: str, gate_eligible: bool = True, available: bool = True
) -> ResolvedSource:
    """Build a minimal ResolvedSource for gate-decision tests."""
    spec = SourceSpec(
        name=name,
        path=f"/tmp/{name}.jsonl",
        enabled_by_default=True,
        gate_eligible=gate_eligible,
        requires_fixtures_dir=False,
        description="test",
    )
    return ResolvedSource(
        spec=spec,
        resolved_path=Path(f"/tmp/{name}.jsonl"),
        available=available,
        gate_eligible_effective=gate_eligible,
        include_unreviewed=False,
        _skip_reason=None,
    )


# ---------------------------------------------------------------------------
# Gate decision — failure modes
# ---------------------------------------------------------------------------


def test_gate_f050_fail_when_baseline_missing() -> None:
    runs = [_mk_run_result("f050_on", {"probes": [1, 1, 1]})]
    sources = [_mk_resolved_source("probes")]
    d = decide_gate_f050(runs, sources)
    assert d.passed is False
    assert "baseline" in d.reason.lower()


def test_gate_f050_fail_when_f050_on_missing() -> None:
    runs = [_mk_run_result("baseline", {"probes": [1, 1, 1]})]
    sources = [_mk_resolved_source("probes")]
    d = decide_gate_f050(runs, sources)
    assert d.passed is False
    assert "f050_on" in d.reason.lower() or "missing" in d.reason.lower()


def test_gate_f050_fail_when_no_gate_eligible_sources() -> None:
    runs = [
        _mk_run_result("baseline", {"probes": [1]}),
        _mk_run_result("f050_on", {"probes": [1]}),
    ]
    sources = [_mk_resolved_source("probes", gate_eligible=False)]
    d = decide_gate_f050(runs, sources)
    assert d.passed is False


def test_gate_f050_fail_when_aggregate_below_threshold() -> None:
    """baseline MRR = 1.0, f050_on MRR = 1.0 -> delta = 0% < 7%."""
    runs = [
        _mk_run_result("baseline", {"probes": [1, 1, 1]}),
        _mk_run_result("f050_on", {"probes": [1, 1, 1]}),
    ]
    sources = [_mk_resolved_source("probes")]
    d = decide_gate_f050(runs, sources, threshold=0.07)
    assert d.passed is False
    assert "aggregate" in d.reason.lower() or "7" in d.reason


def test_gate_f050_fail_when_single_source_regresses() -> None:
    """Aggregate positive but one source regresses >3%.

    baseline: lme MRR ~0.3 (rank 5), probes MRR 1.0 (rank 1).
    f050_on:  lme MRR 1.0 (rank 1), probes MRR ~0.9 (rank 1.1 avg).

    Aggregate delta is positive (big jump on lme), but probes regresses ~-10%.
    """
    runs = [
        _mk_run_result(
            "baseline",
            {
                "longmemeval": [10, 10, 10, 10],  # MRR 0.1
                "probes": [1, 1, 1, 1],  # MRR 1.0
            },
        ),
        _mk_run_result(
            "f050_on",
            {
                "longmemeval": [1, 1, 1, 1],  # MRR 1.0 (huge boost)
                "probes": [2, 2, 2, 2],  # MRR 0.5 (50% regression)
            },
        ),
    ]
    sources = [
        _mk_resolved_source("longmemeval"),
        _mk_resolved_source("probes"),
    ]
    d = decide_gate_f050(runs, sources, threshold=0.07, max_single_regression=0.03)
    assert d.passed is False
    assert (
        "regression" in d.reason.lower()
        or "probes" in d.reason.lower()
        or "single" in d.reason.lower()
    )


def test_gate_f050_fail_when_minority_sources_positive() -> None:
    """With 3 sources, only 1 positive -> minority, fail under majority rule."""
    # src_a: big jump (so aggregate passes)
    # src_b, src_c: flat (neither positive)
    runs = [
        _mk_run_result(
            "baseline",
            {"src_a": [10, 10, 10], "src_b": [1, 1, 1], "src_c": [1, 1, 1]},
        ),
        _mk_run_result(
            "f050_on",
            {"src_a": [1, 1, 1], "src_b": [1, 1, 1], "src_c": [1, 1, 1]},
        ),
    ]
    sources = [
        _mk_resolved_source("src_a"),
        _mk_resolved_source("src_b"),
        _mk_resolved_source("src_c"),
    ]
    d = decide_gate_f050(
        runs,
        sources,
        threshold=0.07,
        max_single_regression=0.5,  # loose to isolate majority check
        require_majority_positive=True,
    )
    assert d.passed is False
    assert (
        "majority" in d.reason.lower()
        or "minority" in d.reason.lower()
        or "positive" in d.reason.lower()
    )


# ---------------------------------------------------------------------------
# Gate decision — happy path
# ---------------------------------------------------------------------------


def test_gate_f050_pass_when_aggregate_and_majority_and_no_regression() -> None:
    runs = [
        _mk_run_result(
            "baseline",
            {
                "longmemeval": [10, 10, 10, 10],
                "probes": [10, 10, 10, 10],
            },
        ),
        _mk_run_result(
            "f050_on",
            {
                "longmemeval": [1, 1, 1, 2],  # MRR ~ 0.875, big jump
                "probes": [1, 1, 2, 2],  # MRR = 0.75, big jump
            },
        ),
    ]
    sources = [
        _mk_resolved_source("longmemeval"),
        _mk_resolved_source("probes"),
    ]
    d = decide_gate_f050(runs, sources, threshold=0.07, max_single_regression=0.03)
    assert d.passed is True, f"gate failed unexpectedly: {d.reason}"


# ---------------------------------------------------------------------------
# write_report — file writing smoke test
# ---------------------------------------------------------------------------


def test_render_markdown_contains_git_sha_and_version(tmp_path: Path) -> None:
    runs = [
        _mk_run_result("baseline", {"probes": [1, 1, 1]}),
        _mk_run_result("f050_on", {"probes": [1, 1, 1]}),
    ]
    sources = [_mk_resolved_source("probes")]
    md = render_markdown(
        run_results=runs,
        resolved_sources=sources,
        git_sha="deadbeef",
        fixture_version="v2026-Q2",
    )
    assert "F051" in md or "retrieval eval" in md.lower()
    assert "deadbeef" in md
    assert "v2026-Q2" in md


def test_write_reports_creates_md_and_json(tmp_path: Path) -> None:
    runs = [_mk_run_result("baseline", {"probes": [1]})]
    sources = [_mk_resolved_source("probes")]
    md = render_markdown(
        run_results=runs,
        resolved_sources=sources,
        git_sha="abc123",
        fixture_version="v1",
    )
    js = render_json(
        run_results=runs,
        resolved_sources=sources,
        git_sha="abc123",
        fixture_version="v1",
    )
    md_path, json_path = write_reports(
        report_dir=tmp_path,
        md_content=md,
        json_content=js,
        config_names=[r.config.name for r in runs],
    )
    assert md_path.exists()
    assert json_path.exists()
    assert md_path.suffix == ".md"
    assert json_path.suffix == ".json"
