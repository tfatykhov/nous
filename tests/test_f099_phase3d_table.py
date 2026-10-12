"""F099 Phase 3d: the signal table, row by row (contract 4.10.3 as amended by R7 and owner answer 4)."""

from __future__ import annotations

import uuid

import pytest

from nous.brain.intention_calibration import (
    INTENTION_REVIEWER,
    Grade,
    LastArrival,
    RootEvidence,
    grade_for,
)

X = uuid.uuid4()
PID = uuid.UUID("12345678-1234-5678-1234-567812345678")


def ev(**kw) -> RootEvidence:
    base = {
        "cancelled": False,
        "expired": False,
        "ttl": False,
        "closed": True,
        "last": LastArrival(n=2, decision="report", outcome="resolved", gate_reason=None),
        "proposals": {},
    }
    return RootEvidence(**{**base, **kw})


def last(decision="report", outcome="resolved", gate=None, n=2) -> LastArrival:
    return LastArrival(n=n, decision=decision, outcome=outcome, gate_reason=gate)


def test_the_reviewer_tag():
    assert INTENTION_REVIEWER == "f099"


@pytest.mark.parametrize("state", ["executed", "failed"])
def test_row_1_an_approved_proposal_is_a_success(state):
    got = grade_for(X, ev(closed=False, last=last("continue"), proposals={X: [(PID, state, "companion:me")]}))
    assert got == Grade("success", f"owner approved proposal 12345678 ({state})")


def test_row_2_a_proposal_the_owner_rejected_is_a_failure():
    got = grade_for(X, ev(closed=False, last=last("ask"), proposals={X: [(PID, "rejected", "telegram:1")]}))
    assert got == Grade("failure", "owner rejected proposal 12345678")


def test_row_2_a_rejection_by_the_system_is_not_a_signal():
    assert grade_for(X, ev(closed=False, last=last("ask"), proposals={X: [(PID, "rejected", "system")]})) is None


def test_the_proposal_rows_are_this_arrivals_only():
    other = uuid.uuid4()
    assert grade_for(X, ev(closed=False, last=last("continue"), proposals={other: [(PID, "executed", "o")]})) is None


def test_rows_1_and_2_in_table_order_an_approval_beats_a_rejection():
    got = grade_for(X, ev(proposals={X: [(PID, "rejected", "o"), (uuid.uuid4(), "executed", "o")]}))
    assert got.outcome == "success"


def test_row_3_a_cancel_is_partial_not_failure():
    got = grade_for(X, ev(cancelled=True, closed=False, last=last("continue", n=3)))
    assert got == Grade("partial", "chain cancelled after 3 arrivals")


def test_a_proposal_beats_the_cancel():
    got = grade_for(X, ev(cancelled=True, closed=False, proposals={X: [(PID, "executed", "o")]}))
    assert got.outcome == "success"


def test_row_4_ttl_and_hanging_name_their_cause():
    assert grade_for(X, ev(expired=True, closed=False, ttl=True, last=last("ask", n=1))) == Grade(
        "failure", "chain ended by ttl after 1 arrival"
    )
    assert grade_for(X, ev(expired=True, closed=False, ttl=False, last=last("continue"))) == Grade(
        "failure", "chain ended by hanging after 2 arrivals"
    )


@pytest.mark.parametrize(
    "gate",
    ["budget_turns", "budget_tokens", "budget_stall", "limit_depth", "limit_spawns", "past_deadline"],
)
def test_row_5_a_bound_ends_the_chain_in_failure_and_names_the_gate(gate):
    got = grade_for(X, ev(last=last("report", gate=gate)))
    assert got == Grade("failure", f"chain ended by {gate} after 2 arrivals")


def test_row_6_plan_resolved_elsewhere_is_partial():
    got = grade_for(X, ev(last=last("drop", gate="plan_resolved")))
    assert got == Grade("partial", "plan resolved elsewhere after 2 arrivals")


@pytest.mark.parametrize("outcome", ["fallback_report", "failed_report"])
@pytest.mark.parametrize("decision", ["report", "drop"])
def test_row_7_a_chain_that_could_not_report_is_a_failure(outcome, decision):
    got = grade_for(X, ev(last=last(decision, outcome=outcome)))
    assert got == Grade("failure", f"chain could not report ({outcome}) after 2 arrivals")


def test_row_8_a_report_is_a_success():
    assert grade_for(X, ev()) == Grade("success", "chain reported after 2 arrivals")


def test_row_9_a_drop_is_partial():
    assert grade_for(X, ev(last=last("drop"))) == Grade("partial", "chain dropped after 2 arrivals")


@pytest.mark.parametrize("decision", ["continue", "revise", "ask"])
def test_a_closed_root_whose_last_arrival_is_not_an_ending_has_no_signal(decision):
    assert grade_for(X, ev(last=last(decision))) is None


@pytest.mark.parametrize("decision", ["report", "drop", "continue"])
def test_an_open_root_without_a_proposal_has_no_signal(decision):
    assert grade_for(X, ev(closed=False, last=last(decision))) is None


def test_a_root_with_no_arrival_has_no_signal():
    assert grade_for(X, ev(last=None)) is None


def test_absence_is_not_a_value_an_expired_or_cancelled_proposal_grades_nothing():
    for state in ("expired", "cancelled", "approved", "executing", "pending"):
        assert grade_for(X, ev(closed=False, last=last("continue"), proposals={X: [(PID, state, "system")]})) is None
