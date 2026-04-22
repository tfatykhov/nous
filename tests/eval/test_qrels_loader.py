"""Unit tests for nous_eval.qrels_loader (F051 Phase 1).

Covers:
- Loading a valid JSONL file -> list[Qrel]
- Empty file -> empty list
- Missing file -> empty list (caller decides if fatal)
- Comment lines (#-prefixed) are skipped
- Malformed JSON line -> ValueError mentioning the line number
- Schema violation (missing field, bad UUID) -> ValueError with line number
- review_filter_enabled drops rows where reviewed_by is None
- memory_types Literal includes "decision" (post-refactor)
- source_override re-stamps every row's source field
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest

pytestmark = pytest.mark.eval

try:
    from nous_eval.qrels_loader import Qrel, QrelSource, load_qrels
except ImportError:
    pytest.skip("nous_eval.qrels_loader not yet available", allow_module_level=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_load_valid_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "qrels.jsonl"
    g = str(uuid4())
    _write_jsonl(
        path,
        [
            {
                "query": "what is F049?",
                "gold_ids": [g],
                "source": "probes",
                "confidence": "high",
                "reasoning_type": "specific_lookup",
            }
        ],
    )
    qrels = load_qrels(path)
    assert len(qrels) == 1
    q = qrels[0]
    assert q.query == "what is F049?"
    assert q.gold_ids == [UUID(g)]
    assert q.source.value == "probes"
    assert q.reasoning_type == "specific_lookup"


def test_load_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    assert load_qrels(path) == []


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    """Missing file returns empty list — caller decides whether to error."""
    qrels = load_qrels(tmp_path / "does_not_exist.jsonl")
    assert qrels == []


def test_comment_lines_are_skipped(tmp_path: Path) -> None:
    """`#`-prefixed lines (provenance headers) are silently skipped."""
    path = tmp_path / "with_comments.jsonl"
    path.write_text(
        "\n".join(
            [
                "# F051 fixture header",
                "# captured: 2026-04-20",
                json.dumps(
                    {
                        "query": "ok",
                        "gold_ids": [str(uuid4())],
                        "source": "probes",
                        "confidence": "high",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    qrels = load_qrels(path)
    assert len(qrels) == 1


# ---------------------------------------------------------------------------
# Error paths — must surface line numbers
# ---------------------------------------------------------------------------


def test_malformed_line_raises_with_line_number(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "query": "ok",
                        "gold_ids": [str(uuid4())],
                        "source": "probes",
                        "confidence": "high",
                    }
                ),
                "this_is_not_json{{{",
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as exc_info:
        load_qrels(path)
    msg = str(exc_info.value)
    # Should mention line 2 specifically
    assert ":2" in msg or "line 2" in msg.lower() or "2:" in msg


def test_missing_required_field_raises_with_line_number(tmp_path: Path) -> None:
    path = tmp_path / "missing.jsonl"
    # Row 1 is OK, row 2 is missing `source`
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "query": "ok",
                        "gold_ids": [str(uuid4())],
                        "source": "probes",
                        "confidence": "high",
                    }
                ),
                json.dumps({"query": "no source", "gold_ids": [str(uuid4())], "confidence": "high"}),
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as exc_info:
        load_qrels(path)
    msg = str(exc_info.value).lower()
    assert "2" in msg


def test_bad_uuid_in_gold_ids_raises(tmp_path: Path) -> None:
    path = tmp_path / "baduuid.jsonl"
    _write_jsonl(
        path,
        [
            {
                "query": "x",
                "gold_ids": ["not-a-uuid"],
                "source": "probes",
                "confidence": "high",
            }
        ],
    )
    with pytest.raises(ValueError):
        load_qrels(path)


# ---------------------------------------------------------------------------
# review_filter_enabled — drops unreviewed rows
# ---------------------------------------------------------------------------


def test_review_filter_excludes_unreviewed(tmp_path: Path) -> None:
    path = tmp_path / "aih.jsonl"
    _write_jsonl(
        path,
        [
            {
                "query": "reviewed",
                "gold_ids": [str(uuid4())],
                "source": "ai_hand_labeled",
                "confidence": "high",
                "reviewed_by": "tim",
            },
            {
                "query": "unreviewed",
                "gold_ids": [str(uuid4())],
                "source": "ai_hand_labeled",
                "confidence": "medium",
                "reviewed_by": None,
            },
        ],
    )
    qrels = load_qrels(path, review_filter_enabled=True)
    assert len(qrels) == 1
    assert qrels[0].query == "reviewed"


def test_no_review_filter_keeps_all(tmp_path: Path) -> None:
    path = tmp_path / "aih.jsonl"
    _write_jsonl(
        path,
        [
            {
                "query": "a",
                "gold_ids": [str(uuid4())],
                "source": "ai_hand_labeled",
                "confidence": "high",
                "reviewed_by": None,
            },
            {
                "query": "b",
                "gold_ids": [str(uuid4())],
                "source": "ai_hand_labeled",
                "confidence": "high",
                "reviewed_by": "tim",
            },
        ],
    )
    qrels = load_qrels(path)
    assert len(qrels) == 2


# ---------------------------------------------------------------------------
# memory_types now includes "decision" (post-refactor)
# ---------------------------------------------------------------------------


def test_memory_types_allows_decision(tmp_path: Path) -> None:
    path = tmp_path / "q.jsonl"
    _write_jsonl(
        path,
        [
            {
                "query": "a",
                "gold_ids": [str(uuid4())],
                "source": "probes",
                "confidence": "high",
                "memory_types": ["decision", "fact"],
            }
        ],
    )
    qrels = load_qrels(path)
    types = qrels[0].memory_types or []
    # Stored as Literal strings or enums; accept either
    type_strs = [t.value if hasattr(t, "value") else t for t in types]
    assert "decision" in type_strs


# ---------------------------------------------------------------------------
# source_override re-tags every row
# ---------------------------------------------------------------------------


def test_source_override_restamps_rows(tmp_path: Path) -> None:
    path = tmp_path / "q.jsonl"
    _write_jsonl(
        path,
        [
            {
                "query": "x",
                "gold_ids": [str(uuid4())],
                "source": "probes",
                "confidence": "high",
            }
        ],
    )
    qrels = load_qrels(path, source_override=QrelSource.SILVER_EPISODES)
    assert qrels[0].source == QrelSource.SILVER_EPISODES
