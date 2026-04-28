"""F056 PR #1: unit tests for the admission handler eval CLI."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from nous_eval.handlers._jsonl import load_jsonl
from nous_eval.handlers._models import AdmissionRow
from nous.heart.schemas import FactRejected
from nous_eval.handlers._cli_base import _DeleteSpec, _DELETE_ALLOWLIST, clear_handler_state
from nous_eval.handlers.admission import (
    _AGENT_ID,
    _classify_outcome,
    _confusion_increment,
    _settings_with_admission_overrides,
    compute_f1,
    filter_rows,
)


# ---------------------------------------------------------------------------
# AdmissionRow schema (F056 §A)
# ---------------------------------------------------------------------------


class TestAdmissionRowSchema:
    def test_minimal_valid_row(self):
        row = AdmissionRow(row_id="r1", content="x", label="admit")
        assert row.row_id == "r1"
        assert row.label == "admit"
        assert row.reviewed_by is None

    def test_all_fields_valid_row(self, mini_admission_rows):
        # Every mini fixture row must validate.
        for raw in mini_admission_rows:
            AdmissionRow.model_validate(raw)

    def test_label_must_be_admit_or_reject(self):
        with pytest.raises(ValidationError):
            AdmissionRow(row_id="r1", content="x", label="maybe")

    def test_empty_content_rejected(self):
        with pytest.raises(ValidationError):
            AdmissionRow(row_id="r1", content="", label="admit")

    def test_empty_row_id_rejected(self):
        with pytest.raises(ValidationError):
            AdmissionRow(row_id="", content="x", label="admit")


# ---------------------------------------------------------------------------
# _jsonl loader (F056 §"_jsonl raises-on-error policy")
# ---------------------------------------------------------------------------


class TestLoadJsonl:
    def test_load_valid_file(self, tmp_path: Path, mini_admission_rows: list[dict]):
        path = tmp_path / "rows.jsonl"
        path.write_text(
            "\n".join(json.dumps(r) for r in mini_admission_rows),
            encoding="utf-8",
        )
        rows = load_jsonl(path, AdmissionRow)
        assert len(rows) == 5
        assert all(isinstance(r, AdmissionRow) for r in rows)
        assert rows[0].row_id == "mini_a01"

    def test_raises_on_missing_file(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            load_jsonl(tmp_path / "nope.jsonl", AdmissionRow)

    def test_raises_on_validation_error_no_silent_skip(self, tmp_path: Path):
        # Per F056 spec: corpus integrity is hard precondition. NOT skip-with-warn.
        path = tmp_path / "bad.jsonl"
        path.write_text(
            json.dumps({"row_id": "r1", "content": "x", "label": "INVALID"}),
            encoding="utf-8",
        )
        with pytest.raises(ValidationError):
            load_jsonl(path, AdmissionRow)

    def test_skips_blank_and_comment_lines(self, tmp_path: Path):
        path = tmp_path / "rows.jsonl"
        path.write_text(
            "# comment\n\n"
            + json.dumps({"row_id": "r1", "content": "x", "label": "admit"}) + "\n"
            + "\n"
            + "# another comment\n",
            encoding="utf-8",
        )
        rows = load_jsonl(path, AdmissionRow)
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# filter_rows (reviewed_by gating)
# ---------------------------------------------------------------------------


class TestFilterRows:
    def test_default_skips_unreviewed(self, mini_admission_rows: list[dict]):
        rows = [AdmissionRow.model_validate(r) for r in mini_admission_rows]
        # Add an AI-only row
        rows.append(AdmissionRow(
            row_id="ai_only", content="ai-drafted fact", label="admit", reviewed_by=None,
        ))
        filtered = filter_rows(rows, include_unreviewed=False)
        assert len(filtered) == 5  # all 5 mini rows have reviewed_by="tim"
        assert all(r.reviewed_by for r in filtered)

    def test_include_unreviewed_keeps_all(self, mini_admission_rows: list[dict]):
        rows = [AdmissionRow.model_validate(r) for r in mini_admission_rows]
        rows.append(AdmissionRow(
            row_id="ai_only", content="ai-drafted fact", label="admit", reviewed_by=None,
        ))
        filtered = filter_rows(rows, include_unreviewed=True)
        assert len(filtered) == 6


# ---------------------------------------------------------------------------
# compute_f1
# ---------------------------------------------------------------------------


class TestComputeF1:
    def test_perfect_classification(self):
        precision, recall, f1 = compute_f1(tp=25, fp=0, fn=0)
        assert precision == 1.0
        assert recall == 1.0
        assert f1 == 1.0

    def test_all_wrong(self):
        precision, recall, f1 = compute_f1(tp=0, fp=10, fn=10)
        assert precision == 0.0
        assert recall == 0.0
        assert f1 == 0.0

    def test_balanced_50_50(self):
        # tp=10, fp=10, fn=10: precision=0.5, recall=0.5, f1=0.5
        precision, recall, f1 = compute_f1(tp=10, fp=10, fn=10)
        assert precision == 0.5
        assert recall == 0.5
        assert f1 == 0.5

    def test_zero_division_safe(self):
        # No predictions at all: should return zeros, not raise.
        precision, recall, f1 = compute_f1(tp=0, fp=0, fn=0)
        assert (precision, recall, f1) == (0.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# Settings overrides (F056 §A — the admission_shadow_mode=False fix)
# ---------------------------------------------------------------------------


class TestSettingsOverrides:
    def test_admission_shadow_mode_forced_false(self):
        # Per F056 spec: production default is True (admits everything).
        # Eval MUST override or F1 collapses to label-balance baseline.
        from nous.config import Settings
        base = Settings()
        overridden = _settings_with_admission_overrides(base)
        assert overridden.admission_shadow_mode is False
        assert overridden.admission_control_enabled is True

    def test_agent_id_set_to_handler_scope(self):
        from nous.config import Settings
        base = Settings()
        overridden = _settings_with_admission_overrides(base)
        assert overridden.agent_id == _AGENT_ID


# ---------------------------------------------------------------------------
# _classify_outcome (extracted for unit test per architect P2)
# ---------------------------------------------------------------------------


class TestClassifyOutcome:
    def test_fact_rejected_returns_reject(self):
        rejected = FactRejected(
            content="x", composite_score=0.1, threshold=0.5,
            scores={}, explanation="below threshold",
        )
        assert _classify_outcome("admit", rejected) == "reject"
        assert _classify_outcome("reject", rejected) == "reject"

    def test_non_fact_rejected_returns_admit(self):
        # Anything that's not a FactRejected instance counts as admit (the
        # production code path returns FactDetail on success). Using a sentinel
        # object keeps this a true unit test — no need to construct FactDetail.
        admitted = object()
        assert _classify_outcome("admit", admitted) == "admit"
        assert _classify_outcome("reject", admitted) == "admit"


class TestConfusionIncrement:
    def test_tp(self):
        cm = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
        _confusion_increment(cm, "admit", "admit")
        assert cm == {"tp": 1, "fp": 0, "tn": 0, "fn": 0}

    def test_tn(self):
        cm = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
        _confusion_increment(cm, "reject", "reject")
        assert cm == {"tp": 0, "fp": 0, "tn": 1, "fn": 0}

    def test_fp(self):
        cm = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
        _confusion_increment(cm, "reject", "admit")
        assert cm == {"tp": 0, "fp": 1, "tn": 0, "fn": 0}

    def test_fn(self):
        cm = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
        _confusion_increment(cm, "admit", "reject")
        assert cm == {"tp": 0, "fp": 0, "tn": 0, "fn": 1}


# ---------------------------------------------------------------------------
# clear_handler_state safety (allowlist + lock contention)
# ---------------------------------------------------------------------------


class TestClearHandlerStateSafety:
    def test_unknown_table_raises_value_error(self):
        # Allowlist guard prevents future PRs from running TRUNCATE on any
        # arbitrary table name passed in via DeleteSpec.
        import asyncio

        async def go():
            with pytest.raises(ValueError, match="not in DELETE allowlist"):
                await clear_handler_state(
                    db=None,  # not reached — validation runs first
                    name="test",
                    agent_id="x",
                    deletes=[_DeleteSpec(schema_table="public.evil", agent_id="x")],
                )
        asyncio.run(go())

    def test_allowlist_includes_handler_targets(self):
        # All 4 F056 handlers must be able to TRUNCATE their target tables.
        assert "heart.facts" in _DELETE_ALLOWLIST  # admission
        assert "heart.episodes" in _DELETE_ALLOWLIST  # summary
        assert "brain.graph_edges" in _DELETE_ALLOWLIST  # backfill
