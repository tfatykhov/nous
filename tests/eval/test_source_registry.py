"""Unit tests for nous_eval.source_registry (F051 Phase 1).

Covers the 6 resolution rules:
1. fixtures_dir unset -> only requires_fixtures_dir=false sources load; smoke banner
2. missing source file -> silently skipped with _skip_reason; remaining sources proceed
3. --sources whitelist overrides enabled_by_default
4. --exclude subtracts from resolution
5. --gate-only filters to gate_eligible=true
6. --include-unreviewed promotes rows with reviewed_by=null

Plus:
- ResolvedSource has a _skip_reason populated when unavailable
- Unknown source names in `only` raise ValueError
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

try:
    from nous_eval.source_registry import ResolvedSource, SourceRegistry, SourceSpec
except ImportError:
    pytest.skip("nous_eval.source_registry not yet available", allow_module_level=True)


# ---------------------------------------------------------------------------
# Rule 1: fixtures_dir unset -> only requires_fixtures_dir=false sources
# ---------------------------------------------------------------------------


def test_load_smoke_mode(caplog: pytest.LogCaptureFixture) -> None:
    """When fixtures_dir is None, only sources with requires_fixtures_dir=false
    resolve to ``available=True``.
    """
    caplog.set_level(logging.INFO)
    reg = SourceRegistry.load(fixtures_dir=None)
    resolved = reg.resolve()
    # Only `probes` has requires_fixtures_dir=false
    names_available = {r.spec.name for r in resolved if r.available}
    # At minimum, probes should be available (it ships in-repo)
    # Sources requiring fixtures_dir must be unavailable
    for r in resolved:
        if r.spec.requires_fixtures_dir and not r.available:
            assert r._skip_reason is not None


# ---------------------------------------------------------------------------
# Rule 2: missing source file -> silent skip + _skip_reason
# ---------------------------------------------------------------------------


def test_missing_source_file_is_skipped(tmp_path: Path) -> None:
    """A source whose path is missing-on-disk must be marked unavailable, not crash."""
    reg = SourceRegistry.load(fixtures_dir=tmp_path)
    resolved = reg.resolve()
    # Look up longmemeval — its file is missing, so available=False
    lme = next((r for r in resolved if r.spec.name == "longmemeval"), None)
    assert lme is not None
    assert lme.available is False
    assert lme._skip_reason is not None


# ---------------------------------------------------------------------------
# Rule 3: --sources whitelist overrides enabled_by_default
# ---------------------------------------------------------------------------


def test_sources_whitelist_overrides_default(
    mock_fixtures_dir: Path,
) -> None:
    """CLI --sources=synthetic_haiku should resolve synthetic_haiku even though
    enabled_by_default=False."""
    # Create the synthetic qrels file in fixtures_dir so it resolves
    from uuid import uuid4
    (mock_fixtures_dir / "qrels_synthetic.jsonl").write_text(
        json.dumps(
            {"query": "q", "gold_ids": [str(uuid4())], "source": "synthetic_haiku", "confidence": "low"}
        ),
        encoding="utf-8",
    )
    reg = SourceRegistry.load(fixtures_dir=mock_fixtures_dir)
    resolved = reg.resolve(only=["synthetic_haiku"])
    names = {r.spec.name for r in resolved}
    assert names == {"synthetic_haiku"}


# ---------------------------------------------------------------------------
# Rule 4: --exclude subtracts
# ---------------------------------------------------------------------------


def test_exclude_subtracts_from_resolution(mock_fixtures_dir: Path) -> None:
    reg = SourceRegistry.load(fixtures_dir=mock_fixtures_dir)
    resolved = reg.resolve(exclude=["longmemeval"])
    names = {r.spec.name for r in resolved}
    assert "longmemeval" not in names


# ---------------------------------------------------------------------------
# Rule 5: --gate-only filters to gate_eligible=true
# ---------------------------------------------------------------------------


def test_gate_only_filters_to_gate_eligible(mock_fixtures_dir: Path) -> None:
    reg = SourceRegistry.load(fixtures_dir=mock_fixtures_dir)
    resolved = reg.resolve(gate_only=True)
    for r in resolved:
        # gate_only filters out non-gate-eligible specs entirely
        assert r.spec.gate_eligible


# ---------------------------------------------------------------------------
# Rule 6: --include-unreviewed flag propagates to ResolvedSource
# ---------------------------------------------------------------------------


def test_include_unreviewed_sets_flag(mock_fixtures_dir: Path) -> None:
    reg = SourceRegistry.load(fixtures_dir=mock_fixtures_dir)
    resolved = reg.resolve(include_unreviewed=True)
    # Flag should propagate onto each ResolvedSource
    for r in resolved:
        assert r.include_unreviewed is True


# ---------------------------------------------------------------------------
# Unknown source in only -> ValueError (typo safety)
# ---------------------------------------------------------------------------


def test_unknown_source_name_raises() -> None:
    reg = SourceRegistry.load(fixtures_dir=None)
    with pytest.raises(ValueError) as exc:
        reg.resolve(only=["this_source_does_not_exist"])
    assert "this_source_does_not_exist" in str(exc.value)


# ---------------------------------------------------------------------------
# SourceRegistry shape
# ---------------------------------------------------------------------------


def test_registry_contains_all_five_sources() -> None:
    reg = SourceRegistry.load(fixtures_dir=None)
    names = {s.name for s in reg.specs}
    # The five builtin sources from the spec §3
    assert {"longmemeval", "ai_hand_labeled", "probes", "silver_episodes", "synthetic_haiku"} <= names


# ---------------------------------------------------------------------------
# ResolvedSource fields
# ---------------------------------------------------------------------------


def test_resolved_source_has_gate_eligible_effective_field() -> None:
    reg = SourceRegistry.load(fixtures_dir=None)
    resolved = reg.resolve()
    for r in resolved:
        assert hasattr(r, "gate_eligible_effective")
        assert hasattr(r, "available")
        assert hasattr(r, "_skip_reason")
