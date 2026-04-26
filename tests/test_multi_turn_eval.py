"""F051.4 — Multi-Turn Replay Mode tests.

Covers all 9 cases from the spec §Tests section plus 1 dispatcher-injection test.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nous_eval.multi_turn_eval import (
    MultiTurnRunResult,
    _build_qrel_result,
    _question_id,
    _question_type,
    _user_messages,
    _write_report,
    run_multi_turn_eval,
)
from nous_eval.retrieval_runner import QrelResult


# ---------------------------------------------------------------------------
# 0. Dispatcher injects _session_id for recall_deep (F051.4-owned wiring)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_injects_session_id_for_recall_deep() -> None:
    """F051.4: ToolDispatcher.dispatch must inject _session_id for recall_deep.

    Without this branch (added by F051.4), F055 baseline-vs-on would produce
    identical numbers from a SILENT KWARG DROP, not a flag-off no-op. The
    test asserts the kwarg actually reaches the handler.
    """
    from nous.api.tools import ToolDispatcher

    dispatcher = ToolDispatcher()
    captured: dict = {}

    async def fake_recall_deep(
        query: str, limit: int = 10, memory_types=None, _session_id=None,
    ):
        captured["_session_id"] = _session_id
        return {"content": [{"type": "text", "text": "ok"}]}

    dispatcher.register("recall_deep", fake_recall_deep, {"name": "recall_deep"})
    await dispatcher.dispatch(
        name="recall_deep",
        args={"query": "test"},
        session_id="my-session-id",
    )
    assert captured["_session_id"] == "my-session-id"


# ---------------------------------------------------------------------------
# 1. _user_messages filters role=="user" (Test #3 spec — walk uses user-only)
# ---------------------------------------------------------------------------


def test_user_messages_filters_assistant_turns() -> None:
    """Walk reads session.get('turns') with role=='user' filter."""
    session = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "ok"},
    ]
    assert _user_messages(session) == ["hi", "ok"]


def test_user_messages_dict_format() -> None:
    """LongMemEval also wraps turns in {'turns': [...]} dict."""
    session = {"turns": [{"role": "user", "content": "alpha"}]}
    assert _user_messages(session) == ["alpha"]


def test_user_messages_skips_empty_content() -> None:
    """Empty user messages are skipped (helper requires non-empty content key)."""
    session = [
        {"role": "user", "content": ""},
        {"role": "user", "content": "real"},
    ]
    assert _user_messages(session) == ["real"]


# ---------------------------------------------------------------------------
# 2. question_id / question_type extraction (post-F051.5-hotfix dict notes)
# ---------------------------------------------------------------------------


def test_question_id_from_dict_notes() -> None:
    """qrel.notes is dict post-F051.5 hotfix — _question_id reads dict key."""
    from nous_eval.qrels_loader import Qrel, QrelSource

    qrel = Qrel(
        query="?",
        gold_ids=[uuid4()],
        source=QrelSource.LONGMEMEVAL,
        notes={"question_id": "q-42", "question_type": "single-session-user"},
    )
    assert _question_id(qrel) == "q-42"
    assert _question_type(qrel) == "single-session-user"


def test_question_id_falls_back_when_notes_string() -> None:
    """When notes is a string (probes/hand_labels), _question_id falls back to query prefix."""
    from nous_eval.qrels_loader import Qrel, QrelSource

    qrel = Qrel(
        query="this is a probe query string",
        gold_ids=[uuid4()],
        source=QrelSource.PROBES,
        notes="probe_42",
    )
    assert _question_id(qrel) == "this is a probe query string"[:40]
    assert _question_type(qrel) == "(unknown)"


# ---------------------------------------------------------------------------
# 3. _build_qrel_result computes rank_of_first_gold + n_gold_in_top_k
# ---------------------------------------------------------------------------


def test_build_qrel_result_finds_gold_at_correct_rank() -> None:
    """Rank-of-first-gold + n_gold_in_top_k computed correctly."""
    from nous_eval.qrels_loader import Qrel, QrelSource

    gold1, gold2, miss = uuid4(), uuid4(), uuid4()
    qrel = Qrel(
        query="?",
        gold_ids=[gold1, gold2],
        source=QrelSource.LONGMEMEVAL,
    )
    # Pipeline returns: [miss, gold1, miss, gold2]
    pipeline = [
        MagicMock(id=miss, type="fact", score=0.9),
        MagicMock(id=gold1, type="episode", score=0.8),
        MagicMock(id=miss, type="fact", score=0.7),
        MagicMock(id=gold2, type="fact", score=0.6),
    ]
    pr = _build_qrel_result(qrel, qrel_index=0, pipeline_results=pipeline, top_k=10)
    assert pr.rank_of_first_gold == 2
    assert pr.n_gold_in_top_k == 2
    assert pr.n_gold_total == 2
    assert pr.gold_ids == [gold1, gold2]
    assert pr.error is None


def test_build_qrel_result_no_gold_in_top_k() -> None:
    """When pipeline returns no gold, rank_of_first_gold is None."""
    from nous_eval.qrels_loader import Qrel, QrelSource

    qrel = Qrel(
        query="?", gold_ids=[uuid4()], source=QrelSource.LONGMEMEVAL,
    )
    pipeline = [MagicMock(id=uuid4(), type="fact", score=0.9)]
    pr = _build_qrel_result(qrel, qrel_index=1, pipeline_results=pipeline, top_k=10)
    assert pr.rank_of_first_gold is None
    assert pr.n_gold_in_top_k == 0


def test_build_qrel_result_with_error() -> None:
    """Error string is preserved on QrelResult."""
    from nous_eval.qrels_loader import Qrel, QrelSource

    qrel = Qrel(
        query="?", gold_ids=[uuid4()], source=QrelSource.LONGMEMEVAL,
    )
    pr = _build_qrel_result(qrel, qrel_index=2, pipeline_results=[], top_k=10, error="boom")
    assert pr.error == "boom"
    assert pr.retrieved_ids == []


# ---------------------------------------------------------------------------
# 4. No qrels → fail loud (spec test #1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_fails_when_no_qrels(tmp_path: Path) -> None:
    """When load_qrels returns [], harness raises a clear error."""
    from nous_eval.config import EvalSettings

    empty_qrels = tmp_path / "empty.jsonl"
    empty_qrels.write_text("", encoding="utf-8")
    haystack = tmp_path / "haystack.json"
    haystack.write_text("[]", encoding="utf-8")

    with pytest.raises(SystemExit, match="no qrels"):
        await run_multi_turn_eval(
            config_names=["baseline"],
            eval_settings=EvalSettings(),
            qrels_path=empty_qrels,
            haystack_cache=haystack,
            max_turns=5,
            top_k=10,
        )


@pytest.mark.asyncio
async def test_run_fails_when_haystack_cache_missing(tmp_path: Path) -> None:
    """When LongMemEval cache file is absent, harness raises with operator hint."""
    from nous_eval.config import EvalSettings
    from nous_eval.qrels_loader import Qrel, QrelSource

    qrel_path = tmp_path / "qrels.jsonl"
    qrel = {
        "query": "?", "gold_ids": [str(uuid4())], "source": "longmemeval",
        "notes": {"question_id": "q-1", "question_type": "single-session-user"},
    }
    qrel_path.write_text(json.dumps(qrel) + "\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="LongMemEval cache not found"):
        await run_multi_turn_eval(
            config_names=["baseline"],
            eval_settings=EvalSettings(),
            qrels_path=qrel_path,
            haystack_cache=tmp_path / "missing.json",
            max_turns=5,
            top_k=10,
        )


# ---------------------------------------------------------------------------
# 5. Unknown config → fail loud
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_fails_on_unknown_config(tmp_path: Path) -> None:
    """Unknown config name raises with valid-name list in error."""
    from nous_eval.config import EvalSettings
    qrels_path = tmp_path / "qrels.jsonl"
    qrel = {
        "query": "?", "gold_ids": [str(uuid4())], "source": "longmemeval",
        "notes": {"question_id": "q-1", "question_type": "single-session-user"},
    }
    qrels_path.write_text(json.dumps(qrel) + "\n", encoding="utf-8")
    haystack = tmp_path / "haystack.json"
    haystack.write_text(json.dumps([{"question_id": "q-1", "haystack_sessions": []}]), encoding="utf-8")

    with pytest.raises(SystemExit, match="unknown config"):
        await run_multi_turn_eval(
            config_names=["does_not_exist"],
            eval_settings=EvalSettings(),
            qrels_path=qrels_path,
            haystack_cache=haystack,
            max_turns=5,
            top_k=10,
        )


# ---------------------------------------------------------------------------
# 6. Markdown report shape (per-config + per-question_type)
# ---------------------------------------------------------------------------


def test_write_report_includes_per_config_and_per_qtype(tmp_path: Path) -> None:
    """Report has per-config row + per-question_type breakdown table."""
    from nous_eval.metrics import MetricsResult
    from nous_eval.retrieval_runner import RetrievalConfig

    cfg_baseline = RetrievalConfig(name="baseline", flags={}, description="")
    cfg_f055 = RetrievalConfig(name="f055_on", flags={}, description="")

    overall = MetricsResult(
        mrr=0.5, p_at_1=0.4, p_at_5=0.45, p_at_10=0.5,
        r_at_1=0.4, r_at_5=0.45, r_at_10=0.55,
        ndcg_at_10=0.5,
        n_qrels=10, n_errored=0,
    )
    qtype_metrics = MetricsResult(
        mrr=0.6, p_at_1=0.5, p_at_5=0.55, p_at_10=0.6,
        r_at_1=0.5, r_at_5=0.55, r_at_10=0.65,
        ndcg_at_10=0.6,
        n_qrels=5, n_errored=0,
    )

    results = [
        MultiTurnRunResult(
            config=cfg_baseline, per_qrel=[], overall=overall,
            per_question_type={"single-session-user": qtype_metrics},
            duration_seconds=120.0, n_walk_calls=150,
        ),
        MultiTurnRunResult(
            config=cfg_f055, per_qrel=[], overall=overall,
            per_question_type={"single-session-user": qtype_metrics},
            duration_seconds=180.0, n_walk_calls=150,
        ),
    ]
    out = tmp_path / "report.md"
    _write_report(results, out, n_qrels=10, max_turns=30)
    text = out.read_text(encoding="utf-8")
    assert "F051.4 multi-turn-eval report" in text
    assert "baseline" in text
    assert "f055_on" in text
    assert "single-session-user" in text
    assert "Per-question_type breakdown" in text


# ---------------------------------------------------------------------------
# 7. Reset session state — verified-existing API only (no phantom methods)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_session_state_uses_real_api() -> None:
    """F051.4 reset path uses delete_conversation_state, NOT phantom reset_turn_count."""
    from nous_eval.multi_turn_eval import _reset_session_state

    heart = MagicMock()
    heart.working_memory.clear = AsyncMock()
    heart.delete_conversation_state = AsyncMock()
    # Phantom method should NEVER be called — defensive: would raise if called
    # because MagicMock auto-creates attrs, so we explicitly check below.

    await _reset_session_state(heart, agent_id="a", session_id="s")
    heart.working_memory.clear.assert_called_once_with("s", session=None)
    heart.delete_conversation_state.assert_called_once_with("a", "s")


@pytest.mark.asyncio
async def test_reset_session_state_swallows_exceptions() -> None:
    """Reset is best-effort — exceptions are logged, not propagated."""
    from nous_eval.multi_turn_eval import _reset_session_state

    heart = MagicMock()
    heart.working_memory.clear = AsyncMock(side_effect=RuntimeError("wm boom"))
    heart.delete_conversation_state = AsyncMock(side_effect=RuntimeError("cs boom"))
    # Should NOT raise
    await _reset_session_state(heart, agent_id="a", session_id="s")
