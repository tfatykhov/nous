"""F056 PR #3: unit tests for the backfill handler eval CLI."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from nous_eval.density_eval import DensitySnapshot
from nous_eval.handlers._jsonl import load_jsonl
from nous_eval.handlers._models import BackfillEntity
from nous_eval.handlers.backfill import (
    _AGENT_ID,
    _LLM_JUDGE_SAMPLE_N,
    _LLM_JUDGE_SEED,
    _sample_edges,
    _settings_with_backfill_overrides,
    compute_edge_precision,
    compute_orphan_resolution_rate,
    filter_entities,
)


# ---------------------------------------------------------------------------
# BackfillEntity schema (F056 §C)
# ---------------------------------------------------------------------------


_VALID_CONTENT = "This is a long enough fact content for the F038 character floor."  # 65 chars


class TestBackfillEntitySchema:
    def test_minimal_valid_row(self):
        e = BackfillEntity(
            row_id="b1", entity_type="fact", content=_VALID_CONTENT,
        )
        assert e.entity_type == "fact"
        assert e.is_orphan_intent is True  # default

    def test_all_entity_types_valid(self):
        for t in ("fact", "decision", "episode", "procedure"):
            BackfillEntity(row_id="b1", entity_type=t, content=_VALID_CONTENT)

    def test_invalid_entity_type_rejected(self):
        with pytest.raises(ValidationError):
            BackfillEntity(row_id="b1", entity_type="unknown", content=_VALID_CONTENT)

    def test_short_content_rejected(self):
        # F038-1.2 floor enforced at schema level.
        with pytest.raises(ValidationError):
            BackfillEntity(row_id="b1", entity_type="fact", content="too short")


# ---------------------------------------------------------------------------
# Real fixture loads cleanly
# ---------------------------------------------------------------------------


class TestRealFixtureLoads:
    def test_full_fixture_validates(self):
        path = Path("tests/fixtures/handlers/backfill_corpus.jsonl")
        if not path.exists():
            pytest.skip(f"fixture not present at {path}")
        rows = load_jsonl(path, BackfillEntity)
        assert len(rows) == 100
        # PR #3 v1 limitation: facts-only.
        assert all(r.entity_type == "fact" for r in rows)
        # All must be reviewed_by="tim" per spec §C
        unreviewed = [r.row_id for r in rows if not r.reviewed_by]
        assert unreviewed == [], f"unreviewed: {unreviewed}"
        # Roughly 30% orphan-intent per spec
        orphan_count = sum(1 for r in rows if r.is_orphan_intent)
        assert 25 <= orphan_count <= 40, f"orphan_count={orphan_count} out of expected 25-40 range"


# ---------------------------------------------------------------------------
# filter_entities
# ---------------------------------------------------------------------------


class TestFilterEntities:
    def test_default_skips_unreviewed(self):
        es = [
            BackfillEntity(row_id="b1", entity_type="fact", content=_VALID_CONTENT, reviewed_by="tim"),
            BackfillEntity(row_id="b2", entity_type="fact", content=_VALID_CONTENT, reviewed_by=None),
        ]
        filtered = filter_entities(es, include_unreviewed=False)
        assert len(filtered) == 1
        assert filtered[0].row_id == "b1"

    def test_include_unreviewed_keeps_all(self):
        es = [
            BackfillEntity(row_id="b1", entity_type="fact", content=_VALID_CONTENT, reviewed_by="tim"),
            BackfillEntity(row_id="b2", entity_type="fact", content=_VALID_CONTENT, reviewed_by=None),
        ]
        assert len(filter_entities(es, include_unreviewed=True)) == 2


# ---------------------------------------------------------------------------
# _sample_edges deterministic ordering (F056 spec §"Determinism")
# ---------------------------------------------------------------------------


class TestSampleEdges:
    def _edge(self, src: str, tgt: str, rel: str) -> dict:
        return {"source_id": src, "target_id": tgt, "relation": rel}

    def test_sample_is_reproducible_across_calls(self):
        # Same input + same seed → byte-identical sample. This is the key
        # determinism guarantee that prevents flaky regression flagging.
        edges = [self._edge(f"s{i:02d}", f"t{i:02d}", "related") for i in range(100)]
        s1 = _sample_edges(edges, n=20, seed=42)
        s2 = _sample_edges(edges, n=20, seed=42)
        assert s1 == s2

    def test_sample_invariant_to_input_order(self):
        # asyncpg row order is undefined; spec mandates sort BEFORE sample.
        # Same edges in different orders MUST produce same sample.
        edges_a = [self._edge(f"s{i:02d}", f"t{i:02d}", "related") for i in range(50)]
        edges_b = list(reversed(edges_a))
        s_a = _sample_edges(edges_a, n=10, seed=42)
        s_b = _sample_edges(edges_b, n=10, seed=42)
        assert s_a == s_b

    def test_sample_caps_at_input_size(self):
        # If only 5 edges available, sample at most 5 (not crash).
        edges = [self._edge(f"s{i}", f"t{i}", "rel") for i in range(5)]
        sample = _sample_edges(edges, n=20, seed=42)
        assert len(sample) == 5

    def test_default_constants(self):
        # Defaults must match spec values (sample_n=20, seed=42).
        assert _LLM_JUDGE_SAMPLE_N == 20
        assert _LLM_JUDGE_SEED == 42


# ---------------------------------------------------------------------------
# compute_edge_precision
# ---------------------------------------------------------------------------


class TestComputeEdgePrecision:
    def test_all_true(self):
        precision, counts = compute_edge_precision(["true"] * 10)
        assert precision == 1.0
        assert counts == {"true": 10, "false": 0, "borderline": 0}

    def test_half_true_half_false(self):
        precision, _ = compute_edge_precision(["true"] * 5 + ["false"] * 5)
        assert precision == 0.5

    def test_borderline_excluded_from_denominator(self):
        # 5 true + 5 borderline + 0 false → precision = 5/(5+0) = 1.0
        precision, counts = compute_edge_precision(["true"] * 5 + ["borderline"] * 5)
        assert precision == 1.0
        assert counts == {"true": 5, "false": 0, "borderline": 5}

    def test_all_borderline_returns_zero(self):
        # Spec: 0.0 when denominator is 0 (no decisive verdicts).
        precision, _ = compute_edge_precision(["borderline"] * 5)
        assert precision == 0.0


# ---------------------------------------------------------------------------
# compute_orphan_resolution_rate
# ---------------------------------------------------------------------------


class TestComputeOrphanResolutionRate:
    def test_resolved_half(self):
        pre = DensitySnapshot(edge_count_total=0, orphan_count_per_type={"fact": 100})
        post = DensitySnapshot(edge_count_total=50, orphan_count_per_type={"fact": 50})
        assert compute_orphan_resolution_rate(pre, post) == 0.5

    def test_no_pre_orphans_returns_zero(self):
        # Avoid 0/0 — return 0.0 when pre had no orphans.
        pre = DensitySnapshot(edge_count_total=0, orphan_count_per_type={"fact": 0})
        post = DensitySnapshot(edge_count_total=0, orphan_count_per_type={"fact": 0})
        assert compute_orphan_resolution_rate(pre, post) == 0.0

    def test_negative_clamped_to_zero(self):
        # Defensive: if post somehow has more orphans than pre (shouldn't
        # happen in practice), clamp to 0 rather than report a negative
        # resolution rate.
        pre = DensitySnapshot(edge_count_total=0, orphan_count_per_type={"fact": 50})
        post = DensitySnapshot(edge_count_total=0, orphan_count_per_type={"fact": 100})
        assert compute_orphan_resolution_rate(pre, post) == 0.0


# ---------------------------------------------------------------------------
# Settings overrides (F056 §C — graph_backfill_enabled gate)
# ---------------------------------------------------------------------------


class TestSettingsOverrides:
    def test_graph_backfill_enabled_forced_true(self):
        # Per F056 spec §C: graph_backfill_enabled is gated at 4 sites in
        # graph_densifier.py; eval MUST set True or run_backfill_cycle
        # short-circuits and reports 0 edges every run.
        from nous.config import Settings
        base = Settings()
        overridden = _settings_with_backfill_overrides(base)
        assert overridden.graph_backfill_enabled is True

    def test_agent_id_set_to_handler_scope(self):
        from nous.config import Settings
        base = Settings()
        overridden = _settings_with_backfill_overrides(base)
        assert overridden.agent_id == _AGENT_ID
