"""Harness Phase 3: the pure texts every surface renders from (spec §3.4-§3.12)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from nous.dag import approval as ap

SPEC = {
    "options": [
        {"id": "send", "label": "Send it", "outcome": "proceed"},
        {"id": "hold", "label": "Don't send", "outcome": "stop"},
    ],
    "default_option": "hold",
    "recommended_option": None,
    "answer_timeout_seconds": 86400,
}
AT = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _node(**kw):
    base = dict(
        name="approve", node_type="approval", status="awaiting_input", approval_spec=SPEC,
        answer=None, answer_source=None, answered_by=None, answered_at=None,
        answer_deadline=AT, result=None, error=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_dedup_key_round_trips_and_rejects_foreign_keys():
    nid = uuid.uuid4()
    assert ap.node_id_from_dedup_key(ap.approval_dedup_key(nid)) == nid
    assert ap.node_id_from_dedup_key("dag:" + str(nid)) is None
    assert ap.node_id_from_dedup_key("dag-approval:not-a-uuid") is None
    assert ap.node_id_from_dedup_key(None) is None


def test_summary_leads_with_the_whole_question_and_marks_each_cut():
    question = "Q" * 3000
    out = ap.build_card_summary(question, [("draft", "d" * 5000), ("notes", "short")])
    assert out.startswith(question)
    assert "[truncated, 5000 chars]" in out
    assert "From 'notes':\nshort" in out
    assert ap.build_card_summary("Send it?", []) == "Send it?"


def test_card_texts():
    assert ap.risk_line(AT, "Don't send") == (
        "If nobody answers by 2026-09-25 12:00 UTC, 'Don't send' applies."
    )
    assert ap.button_label("Send it", "proceed") == "Send it — continues"
    assert ap.button_label("Don't send", "stop") == "Don't send — stops here"
    ping = ap.notify_text("dag · approve", "Line one?\nline two", AT, "Don't send")
    assert ping.splitlines() == [
        "dag · approve", "Line one?", "No answer by 2026-09-25 12:00 UTC → 'Don't send'.",
    ]
    assert len(ap.notify_text("t", "x" * 500, AT, "d").splitlines()[1]) == 200


def test_answer_values_never_name_an_unattributed_actor():
    tap = ap.answer_values(SPEC, "send", source="companion", actor="unattributed", at=AT, deadline=AT)
    assert tap == {
        "status": "completed", "error": None,
        "result": "Answered in the companion: 'Send it' (send) at 2026-09-25 12:00 UTC",
    }
    named = ap.answer_values(SPEC, "hold", source="companion", actor="alice@example.com", at=AT, deadline=AT)
    assert named["status"] == "failed"
    assert named["error"].endswith("at 2026-09-25 12:00 UTC by alice@example.com")
    assert named["error"].startswith("declined in the companion: 'Don't send' (hold)")
    late = ap.answer_values(SPEC, "hold", source="deadline", actor=ap.DEADLINE_ACTOR, at=AT, deadline=AT)
    assert late == {
        "status": "failed",
        "error": "no answer by 2026-09-25 12:00 UTC; default 'Don't send' (hold) applied",
    }


def test_refusal_messages():
    closed = ap.AnswerResult(outcome="closed", option_label="Send it", answered_at=AT, answer_source="companion")
    assert ap.refusal_message(closed, "hold") == "already answered 'Send it' at 2026-09-25 12:00 UTC"
    defaulted = ap.AnswerResult(outcome="closed", option_label="Don't send", answered_at=AT, answer_source="deadline")
    assert "no answer by the deadline" in ap.refusal_message(defaulted, "send")
    cancelled = ap.AnswerResult(outcome="closed", node_status="cancelled")
    assert ap.refusal_message(cancelled, "send") == "this DAG step was cancelled"
    assert ap.refusal_message(ap.AnswerResult(outcome="not_open"), "x").endswith("on a new card")
    assert ap.refusal_message(ap.AnswerResult(outcome="dag_ended"), "x") == "this DAG has already ended"
    assert "out of date" in ap.refusal_message(ap.AnswerResult(outcome="stray_card"), "x")
    assert "'x'" in ap.refusal_message(ap.AnswerResult(outcome="invalid_option"), "x")
    assert ap.refusal_message(ap.AnswerResult(outcome="not_linked"), "x") == "this DAG step no longer exists"


def test_stopped_at_approval_and_its_summary():
    stop = _node(status="failed", answer="hold", answer_source="deadline")
    blocked = SimpleNamespace(name="send", node_type="subtask", status="blocked", answer_source=None)
    assert ap.stopped_at_approval([stop, blocked])
    assert ap.stopped_summary([stop, blocked]) == "Stopped at approval 'approve': 'Don't send'; 1 step not run"
    crashed = SimpleNamespace(name="x", node_type="subtask", status="failed", answer_source=None)
    assert not ap.stopped_at_approval([stop, crashed])
    assert not ap.stopped_at_approval([blocked])


def test_approval_lines():
    assert ap.approval_line(_node(status="completed", answer="send", answer_source="companion", answered_at=AT)) == (
        "approve: approved — 'Send it' in the companion at 2026-09-25 12:00 UTC"
    )
    assert ap.approval_line(_node(status="failed", answer="hold", answer_source="deadline")) == (
        "approve: no answer by 2026-09-25 12:00 UTC; default 'Don't send' applied"
    )
    assert ap.approval_line(_node(status="cancelled")) == "approve: not answered (cancelled)"
    assert "waiting for an answer until" in ap.approval_line(_node())


def test_history_entry_and_retry_refusal():
    assert ap.history_entry(_node()) is None
    entry = ap.history_entry(
        _node(answer="hold", answer_source="deadline", answered_by="system:deadline", answered_at=AT)
    )
    assert entry == {
        "answer": "hold", "label": "Don't send", "outcome": "stop", "answer_source": "deadline",
        "answered_by": "system:deadline", "answered_at": "2026-09-25T12:00:00+00:00",
    }
    assert "dag_monitor" in ap.declined_retry_refusal("approve")
    assert ap.card_link("card-1", "https://n.example/") == "https://n.example/companion#/s/card-1"


def test_card_shown_chars_matches_what_the_summary_shows():
    results = [("draft", "d" * 5000), ("notes", "short")]
    shown = ap.card_shown_chars("Send it?", results)
    summary = ap.build_card_summary("Send it?", results)
    assert shown[1] == len("short")
    assert 0 < shown[0] < 5000
    assert "d" * shown[0] + "\n[truncated, 5000 chars]" in summary
    assert "d" * (shown[0] + 1) not in summary
    assert ap.card_shown_chars("q", []) == []


def _n(name, node_type="subtask", result=None):
    return SimpleNamespace(id=uuid.uuid4(), name=name, node_type=node_type, result=result)


def _e(src, dst, edge_type="context_flow"):
    return SimpleNamespace(from_node_id=src.id, to_node_id=dst.id, edge_type=edge_type)


def test_context_results_walks_through_chained_approvals_once():
    """One pure walk shared by the orchestrator (card + acting node) and the
    dashboard (card_summary, reviewing) — a diamond yields the draft once."""
    draft = _n("draft", result="DRAFT")
    a1 = _n("a1", "approval", result="Answered: Send it")
    a2 = _n("a2", "approval")
    send = _n("send")
    nodes = [draft, a1, a2, send]
    edges = [_e(draft, a1), _e(draft, a2), _e(a1, a2), _e(a2, send), _e(draft, send, "dependency")]

    assert ap.context_results(a2, nodes, edges) == [("draft", "DRAFT"), ("a1", "Answered: Send it")]
    assert ap.context_results(a1, nodes, edges) == [("draft", "DRAFT")]
    assert ap.context_results(draft, nodes, edges) == []
