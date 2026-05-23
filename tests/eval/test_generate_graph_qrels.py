"""Unit tests for nous_eval.generate_graph_qrels (LLM-free portions)."""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

pytestmark = pytest.mark.eval

try:
    from nous_eval.generate_graph_qrels import (
        EdgeCandidate,
        GeneratedQrel,
        _build_query_gen_prompt,
        _qrel_to_jsonl,
    )
    from nous_eval.qrels_loader import QrelSource
except ImportError:
    pytest.skip("nous_eval.generate_graph_qrels not yet available", allow_module_level=True)


def _mk_candidate(**overrides) -> EdgeCandidate:
    base = {
        "source_id": uuid4(),
        "target_id": uuid4(),
        "source_type": "fact",
        "target_type": "decision",
        "relation": "evidence_for",
        "weight": 0.85,
        "source_content": "We measured 12k requests per second on uvicorn.",
        "target_content": "Adopted FastAPI for the new high-throughput API.",
    }
    base.update(overrides)
    return EdgeCandidate(**base)


class TestPromptBuilder:
    def test_includes_both_contents(self) -> None:
        c = _mk_candidate()
        prompt = _build_query_gen_prompt(c)
        assert c.source_content in prompt
        assert c.target_content in prompt

    def test_carries_relation(self) -> None:
        c = _mk_candidate(relation="related_to")
        prompt = _build_query_gen_prompt(c)
        assert "related_to" in prompt

    def test_instructs_no_target_mention(self) -> None:
        c = _mk_candidate()
        prompt = _build_query_gen_prompt(c)
        # Must explicitly tell the model not to leak TARGET vocabulary
        # — otherwise the produced query is findable via vector search
        # and the qrel fails validation.
        assert "does NOT mention TARGET" in prompt or "do not include any phrasing from TARGET" in prompt.lower() or "lean on SOURCE" in prompt.lower() or "TARGET's vocabulary" in prompt


class TestJsonlEmitter:
    def test_round_trip_preserves_schema(self) -> None:
        target = uuid4()
        bridge = uuid4()
        qrel = GeneratedQrel(
            query="How do we serve high-throughput HTTP requests?",
            gold_id=target,
            gold_type="decision",
            source_id=bridge,
            relation="evidence_for",
            rationale="performance evidence → adoption decision",
        )
        line = _qrel_to_jsonl(qrel)
        loaded = json.loads(line)
        assert loaded["query"] == qrel.query
        assert loaded["gold_ids"] == [str(target)]
        assert loaded["memory_types"] == ["decision"]
        assert loaded["source"] == QrelSource.GRAPH_TARGETED.value
        assert loaded["reviewed_by"] == "auto"
        assert loaded["notes"]["bridge_via"] == str(bridge)
        assert loaded["notes"]["edge_relation"] == "evidence_for"

    def test_source_tag_matches_enum(self) -> None:
        """Codex-style guard: if QrelSource.GRAPH_TARGETED ever drifts
        from the string "graph_targeted", qrels won't load."""
        assert QrelSource.GRAPH_TARGETED.value == "graph_targeted"
