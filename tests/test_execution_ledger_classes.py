"""Harness Phase 0: side-effect classes for tools registered after F026.

classify_side_effect defaults any unlisted tool to "write", so send_email
(registered after F026) was classed a local write — never "external" — and
the F078 refuse denylist, built from these sets, never stripped it.
"""

from nous.cognitive.execution_ledger import (
    EXTERNAL_TOOLS,
    READ_TOOLS,
    WRITE_TOOLS,
    ExecutionLedger,
    classify_side_effect,
)


def test_send_email_is_external():
    assert classify_side_effect("send_email", {"to": "a@b.c"}) == "external"


def test_pure_decision_and_graph_reads_are_reads():
    assert classify_side_effect("recall_hubs", {}) == "none"
    assert classify_side_effect("list_decisions", {}) == "none"


def test_f078_refuse_denylist_now_includes_send_email():
    """runner.py builds the refuse denylist as WRITE | EXTERNAL | IRREVERSIBLE | {bash}."""
    assert "send_email" in (WRITE_TOOLS | EXTERNAL_TOOLS)


def test_sets_are_disjoint():
    assert not (READ_TOOLS & WRITE_TOOLS)
    assert not (READ_TOOLS & EXTERNAL_TOOLS)
    assert not (WRITE_TOOLS & EXTERNAL_TOOLS)


def test_send_email_ledger_keys_on_recipient_and_subject_not_body():
    """The session ledger (and ActionGate's duplicate check) used the 5-arg
    fallback for send_email, which captured body[:80]."""
    ledger = ExecutionLedger(session_id="s")
    action = ledger.record(
        "send_email",
        {"to": "a@b.c", "subject": "Premarket", "body": "secret numbers", "cc": "d@e.f"},
        "Email sent",
        "success",
    )
    assert action.key_args == {"to": "a@b.c", "cc": "d@e.f", "subject": "Premarket"}
    assert action.side_effect_type == "external"
