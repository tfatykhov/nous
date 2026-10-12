"""F099 Phase 3d: grade_arrivals on real rows. One fixture test per row of the signal table, then the
protections (never replace a grade, repair noise, idempotence, no record, first-page pinning, a race)."""

from __future__ import annotations

import pytest
from f099_calibration_support import arrival, brain_of, close_root, make_decision, outcome_of, proposal
from f099_support import CONT, env_factory, make_child, make_root  # noqa: F401
from sqlalchemy import delete

from nous.brain import Brain
from nous.brain.intention_calibration import INTENTION_REVIEWER, grade_arrivals
from nous.storage.models import Decision

pytestmark = pytest.mark.postgres_only
FLAG = {**CONT, "intention_calibration_enabled": True}


async def _grade(env, *, limit: int = 100, brain: Brain | None = None) -> int:
    async with env.db.session() as s:
        return await grade_arrivals(s, env.agent, brain=brain or brain_of(env), settings=env.settings, limit=limit)


async def _ended(env, *arrivals_of, **closing):
    """A root with the given arrivals ``(n, kwargs)``, then ended as ``closing`` says (see close_root; no
    keyword = closed with no marker). Returns ``(root, [arrival, ...])``."""
    root = await make_root(env)
    made = [await arrival(env, root, n, **kw) for n, kw in arrivals_of]
    await close_root(env, root, **closing)
    return root, made


async def _state(env, arr):
    return await outcome_of(env, arr.decision_id)


# ---- one test per row of the table ----------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["executed", "failed"])
async def test_row_1_an_approved_proposal_grades_its_arrival_success(env_factory, state):  # noqa: F811
    env = await env_factory(**FLAG)
    root = await make_root(env)  # the root stays OPEN: the proposal alone is the signal (S3)
    first = await arrival(env, root, 1)
    second = await arrival(env, root, 2)
    await proposal(env, root, first, state=state)
    assert await _grade(env) == 1
    got = await _state(env, first)
    assert (got.outcome, got.reviewer) == ("success", INTENTION_REVIEWER)
    assert got.result.startswith("owner approved proposal ") and state in got.result
    assert (await _state(env, second)).outcome == "pending"  # a proposal grades its own arrival only


async def test_row_2_a_rejected_proposal_grades_failure(env_factory):  # noqa: F811
    env = await env_factory(**FLAG)
    root = await make_root(env)
    arr = await arrival(env, root, 1)
    await proposal(env, root, arr, state="rejected", decided_by="telegram:42")
    assert await _grade(env) == 1
    assert (await _state(env, arr)).outcome == "failure"


async def test_rows_1_2_expired_cancelled_and_system_rejected_proposals_grade_nothing(env_factory):  # noqa: F811
    env = await env_factory(**FLAG)
    root = await make_root(env)
    a1, a2, a3 = [await arrival(env, root, n) for n in (1, 2, 3)]
    await proposal(env, root, a1, state="expired", decided_by="system")
    await proposal(env, root, a2, state="cancelled", decided_by="system")
    await proposal(env, root, a3, state="rejected", decided_by="system")
    assert await _grade(env) == 0
    assert {(await _state(env, a)).outcome for a in (a1, a2, a3)} == {"pending"}


async def test_row_3_a_cancel_grades_every_candidate_partial(env_factory):  # noqa: F811
    env = await env_factory(**FLAG)
    _root, (a1, a2) = await _ended(env, (1, {}), (2, {}), cancelled=True)
    assert await _grade(env) == 2
    for arr in (a1, a2):
        got = await _state(env, arr)
        assert (got.outcome, got.reviewer, got.result) == (
            "partial",
            INTENTION_REVIEWER,
            "chain cancelled after 2 arrivals",
        )


async def test_an_approved_arrival_beats_the_cancel_that_followed(env_factory):  # noqa: F811
    env = await env_factory(**FLAG)
    root, (a1, a2) = await _ended(env, (1, {}), (2, {}), cancelled=True)
    await proposal(env, root, a1, state="executed")
    assert await _grade(env) == 2
    assert (await _state(env, a1)).outcome == "success"
    assert (await _state(env, a2)).outcome == "partial"


@pytest.mark.parametrize(("child_expired", "cause"), [(True, "ttl"), (False, "hanging")])
async def test_row_4_an_expired_root_fails_and_names_how_it_ended(env_factory, child_expired, cause):  # noqa: F811
    env = await env_factory(**FLAG)
    _root, (a1,) = await _ended(env, (1, {"decision": "ask"}), expired=True, child_expired=child_expired)
    assert await _grade(env) == 1
    got = await _state(env, a1)
    assert (got.outcome, got.result) == ("failure", f"chain ended by {cause} after 1 arrival")


@pytest.mark.parametrize(
    "gate", ["budget_turns", "budget_tokens", "budget_stall", "limit_depth", "limit_spawns", "past_deadline"]
)
async def test_row_5_a_bound_fails_the_chain_and_names_the_gate(env_factory, gate):  # noqa: F811
    env = await env_factory(**FLAG)
    _root, (a1, _gate) = await _ended(env, (1, {}), (2, {"linked": False, "decision": "report", "gate_reason": gate}))
    assert await _grade(env) == 1  # the gate arrival has no record: only a1 is a candidate
    got = await _state(env, a1)
    assert (got.outcome, got.result) == ("failure", f"chain ended by {gate} after 2 arrivals")


async def test_row_5_does_not_end_a_root_that_still_has_an_open_branch(env_factory):  # noqa: F811
    env = await env_factory(**FLAG)
    root = await make_root(env)
    a1 = await arrival(env, root, 1)
    await arrival(env, root, 2, linked=False, decision="report", gate_reason="budget_turns")
    await make_child(env, root)  # an open child
    await close_root(env, root)  # closes the root row only: the child stays pending, so the lineage is NOT closed
    assert await _grade(env) == 0
    assert (await _state(env, a1)).outcome == "pending"


async def test_row_6_plan_resolved_elsewhere_is_partial(env_factory):  # noqa: F811
    env = await env_factory(**FLAG)
    _root, (a1, _gate) = await _ended(
        env, (1, {}), (2, {"linked": False, "decision": "drop", "gate_reason": "plan_resolved"})
    )
    assert await _grade(env) == 1
    got = await _state(env, a1)
    assert (got.outcome, got.result) == ("partial", "plan resolved elsewhere after 2 arrivals")


@pytest.mark.parametrize("outcome", ["fallback_report", "failed_report"])
async def test_row_7_a_chain_that_could_not_report_fails(env_factory, outcome):  # noqa: F811
    env = await env_factory(**FLAG)
    _root, (a1, _last) = await _ended(env, (1, {}), (2, {"linked": False, "decision": "report", "outcome": outcome}))
    assert await _grade(env) == 1  # the fallback and the failed report have no record (R7)
    got = await _state(env, a1)
    assert (got.outcome, got.result) == ("failure", f"chain could not report ({outcome}) after 2 arrivals")


async def test_row_8_a_report_grades_every_candidate_success(env_factory):  # noqa: F811
    env = await env_factory(**FLAG)
    _root, (a1, a2) = await _ended(env, (1, {}), (2, {"decision": "report"}))
    assert await _grade(env) == 2
    for arr in (a1, a2):
        got = await _state(env, arr)
        assert (got.outcome, got.result) == ("success", "chain reported after 2 arrivals")


async def test_row_9_a_drop_grades_every_candidate_partial_the_drop_included(env_factory):  # noqa: F811
    env = await env_factory(**FLAG)
    _root, (a1, a2) = await _ended(env, (1, {}), (2, {"decision": "drop"}))
    assert await _grade(env) == 2
    for arr in (a1, a2):
        got = await _state(env, arr)
        assert (got.outcome, got.result) == ("partial", "chain dropped after 2 arrivals")


async def test_no_signal_an_open_root_stays_pending(env_factory):  # noqa: F811
    env = await env_factory(**FLAG)
    root = await make_root(env)
    a1 = await arrival(env, root, 1)
    assert await _grade(env) == 0
    got = await _state(env, a1)
    assert (got.outcome, got.reviewed_at) == ("pending", None)


async def test_no_signal_a_closed_root_whose_last_arrival_is_a_continue_stays_pending(env_factory):  # noqa: F811
    """The hanging shape, before end_hanging_roots has marked it."""
    env = await env_factory(**FLAG)
    _root, (a1,) = await _ended(env, (1, {"decision": "continue"}))
    assert await _grade(env) == 0
    assert (await _state(env, a1)).outcome == "pending"


# ---- protections ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "reviewer"),
    [("failure", "agent"), ("success", "auto"), ("success", "a2ui"), ("partial", "agent")],
)
async def test_a_graded_decision_is_never_replaced(env_factory, outcome, reviewer):  # noqa: F811
    """The prod `auto`/`success` (19) and `a2ui`/`success` (1) rows, and an owner failure, survive a chain end.
    Two guards keep them: `_candidates` leaves a graded row out, so it never takes a slot of the page (limit=1
    here: the graded a1 sorts first), and `preserve_graded=True` refuses the write (pinned by the race test
    below). Mutation: add `success` to `REGRADED_OUTCOMES` and the two `success` cases fail (a1 takes the only
    slot, its write is refused, the pass writes nothing); preserve_graded alone would still keep a1 as it is."""
    env = await env_factory(**FLAG)
    _root, (a1, a2) = await _ended(
        env, (1, {"decision_kw": {"outcome": outcome, "reviewer": reviewer}}), (2, {"decision": "report"})
    )
    assert await _grade(env, limit=1) == 1  # only a2, on a page of one
    got = await _state(env, a1)
    assert (got.outcome, got.reviewer) == (outcome, reviewer)
    assert (await _state(env, a2)).outcome == "success"


async def test_a_background_noise_label_is_repaired_once_the_chain_ends(env_factory):  # noqa: F811
    """The 28 prod rows: `agent-background`/`noise`. Overwritten (S5: an owner `noise` too)."""
    env = await env_factory(**FLAG)
    noisy = {"outcome": "noise", "reviewer": "agent-background"}
    _root, (a1,) = await _ended(env, (1, {"decision": "report", "decision_kw": noisy}))
    assert await _grade(env) == 1
    got = await _state(env, a1)
    assert (got.outcome, got.reviewer) == ("success", INTENTION_REVIEWER)


async def test_a_noise_row_on_a_chain_that_is_still_running_is_left_alone(env_factory):  # noqa: F811
    env = await env_factory(**FLAG)
    root = await make_root(env)
    a1 = await arrival(env, root, 1, decision_kw={"outcome": "noise", "reviewer": "agent-background"})
    assert await _grade(env) == 0
    assert (await _state(env, a1)).reviewer == "agent-background"


async def test_a_superseded_decision_is_left_alone(env_factory):  # noqa: F811
    """C3: superseded is lineage, not pollution; a review would clear superseded_by.
    Mutation: add 'superseded' to REGRADED_OUTCOMES."""
    env = await env_factory(**FLAG)
    _root, (a1,) = await _ended(env, (1, {"decision": "report", "decision_kw": {"outcome": "superseded"}}))
    assert await _grade(env) == 0
    assert (await _state(env, a1)).outcome == "superseded"


async def test_a_second_pass_writes_nothing(env_factory):  # noqa: F811
    env = await env_factory(**FLAG)
    await _ended(env, (1, {}), (2, {"decision": "report"}))
    assert await _grade(env) == 2
    assert await _grade(env) == 0


async def test_an_arrival_without_a_record_is_untouched_and_not_a_candidate(env_factory):  # noqa: F811
    env = await env_factory(**FLAG)
    await _ended(env, (1, {"linked": False, "decision": "report"}))
    assert await _grade(env) == 0


async def test_a_decision_that_is_not_an_arrivals_is_never_graded(env_factory):  # noqa: F811
    """Calibration isolation: the predicate is the arrival link, not the session."""
    env = await env_factory(**FLAG)
    root, _ = await _ended(env, (1, {"decision": "report"}))
    ordinary = await make_decision(env, session_id=f"intent-{root.id}", description="Chose the Friday slot")
    await _grade(env)
    got = await outcome_of(env, ordinary)
    assert (got.outcome, got.reviewer) == ("pending", None)


async def test_the_flag_gates_the_grader_itself(env_factory):  # noqa: F811
    env = await env_factory(**CONT)  # calibration off
    await _ended(env, (1, {"decision": "report"}))
    assert await _grade(env) == 0


# ---- S3: signal presence is in the WHERE ------------------------------------------------------------------------


async def test_open_roots_cannot_pin_the_first_page(env_factory):  # noqa: F811
    """With limit=1 the OLDEST ungraded arrival belongs to a root that is still running. Without the signal
    predicate it would take the one slot every pass and the ended root behind it would never be graded.
    Mutation: delete the `or_(...)` signal-presence clause from `_candidates`."""
    env = await env_factory(**FLAG)
    running = await make_root(env)
    stuck = await arrival(env, running, 1)  # sorts first, no signal
    _root, (ended,) = await _ended(env, (5, {"decision": "report"}))  # sorts later, ended by a report
    assert await _grade(env, limit=1) == 1
    assert (await _state(env, ended)).outcome == "success"
    assert (await _state(env, stuck)).outcome == "pending"


# ---- a review that lands between the read and the write -----------------------------------------------------------


async def test_a_grade_that_lands_between_the_read_and_the_write_stands(env_factory):  # noqa: F811
    """The owner re-grades one arrival after the candidate read. preserve_graded refuses; the pass goes on."""
    env = await env_factory(**FLAG)
    _root, (a1, a2) = await _ended(env, (1, {}), (2, {"decision": "report"}))

    class Racing(Brain):
        raced = False

        async def review(self, decision_id, *args, **kwargs):
            if not Racing.raced and decision_id == a1.decision_id:
                Racing.raced = True
                await Brain.review(self, decision_id, outcome="failure", reviewer="agent")
            return await super().review(decision_id, *args, **kwargs)

    assert await _grade(env, brain=Racing(database=env.db, settings=env.settings)) == 1  # only a2 was written
    got = await _state(env, a1)
    assert (got.outcome, got.reviewer) == ("failure", "agent")
    assert (await _state(env, a2)).outcome == "success"


async def test_a_deleted_decision_does_not_abort_the_pass(env_factory):  # noqa: F811
    env = await env_factory(**FLAG)
    _root, (a1, a2) = await _ended(env, (1, {}), (2, {"decision": "report"}))

    class Vanishing(Brain):
        async def review(self, decision_id, *args, **kwargs):
            if decision_id == a1.decision_id:
                async with self.db.session() as s:
                    await s.execute(delete(Decision).where(Decision.id == a1.decision_id))
                    await s.commit()
            return await super().review(decision_id, *args, **kwargs)

    assert await _grade(env, brain=Vanishing(database=env.db, settings=env.settings)) == 1
    assert (await _state(env, a2)).outcome == "success"
