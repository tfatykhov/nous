"""An automatic review never replaces a review somebody made.

``DecisionReviewer`` reads the list of unreviewed decisions first and writes its
verdicts later, one decision at a time, with the signal checks (an HTTP call for
the pull-request signal) in between. A review by the agent or a person that
commits in that window used to be overwritten: ``('success', 'agent')`` became
``('failure', 'auto')``, and that label went into calibration.

The tests run the production reviewer (``sweep`` or the ``session_ended``
handler) and the production ``Brain.review`` on real rows under a fresh
``agent_id``. Both commit through their own sessions, so the rows are committed
for real and the fixture deletes them again.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text, update

from nous.brain.brain import Brain
from nous.brain.schemas import ReasonInput, RecordInput
from nous.config import Settings
from nous.events import Event as BusEvent
from nous.events import EventBus
from nous.handlers.decision_reviewer import DecisionReviewer, ReviewResult
from nous.storage.models import Decision, Event, GraphEdge

WAIT = 10.0
REVIEWER_LOG = "nous.handlers.decision_reviewer"


class _World:
    """A Brain, an EventBus and a DecisionReviewer wired the way main.py wires them."""

    def __init__(self, db):
        self.db = db
        self.agent_id = f"fix-l-{uuid4().hex[:8]}"
        self.settings = Settings().model_copy(update={"agent_id": self.agent_id, "github_token": ""})
        self.bus = EventBus()
        self.brain = Brain(database=db, settings=self.settings)
        self.brain._bus = self.bus
        self.reviewer = DecisionReviewer(self.brain, self.settings, self.bus)
        self._seen: list[tuple[str, str | None]] = []
        self.bus.on("decision_reviewed", self._saw)

    async def _saw(self, event: BusEvent) -> None:
        self._seen.append((event.data["outcome"], event.data["reviewer"]))

    async def record(self, confidence: float, session_id: str | None = None):
        """A decision. Stated confidence below 0.4 is what the reviewer's
        low-confidence signal grades as a failure."""
        return await self.brain.record(
            RecordInput(
                description="Roll the release out behind a blue-green switch",
                confidence=confidence,
                category="process",
                stakes="medium",
                context="Two deploy strategies were on the table for the billing service",
                pattern="Prefer reversible rollouts",
                tags=["deploy"],
                reasons=[ReasonInput(type="analysis", text="Rollback is one switch flip")],
                session_id=session_id,
            )
        )

    def session_ended(self, session_id: str) -> BusEvent:
        return BusEvent(
            type="session_ended", agent_id=self.agent_id, data={"session_id": session_id}, session_id=session_id
        )

    async def row(self, decision_id) -> tuple[str | None, str | None]:
        """(outcome, reviewer) as the decision row holds them now."""
        async with self.db.session() as s:
            return tuple(
                (await s.execute(select(Decision.outcome, Decision.reviewer).where(Decision.id == decision_id))).one()
            )

    async def audit(self, decision_id) -> list[tuple[str, str | None]]:
        """(outcome, reviewer) of every decision_reviewed audit row of the decision."""
        async with self.db.session() as s:
            rows = await s.execute(
                select(Event.data)
                .where(Event.agent_id == self.agent_id, Event.event_type == "decision_reviewed")
                .order_by(Event.created_at, Event.id)
            )
        return [(d["outcome"], d["reviewer"]) for (d,) in rows if d["decision_id"] == str(decision_id)]

    async def events(self) -> list[tuple[str, str | None]]:
        """(outcome, reviewer) of every decision_reviewed event the bus delivered."""
        await self.bus.start()
        await self.bus.stop()  # stop() dispatches what is queued
        return list(self._seen)

    async def close(self) -> None:
        async with self.db.session() as s:
            for model in (GraphEdge, Event, Decision):
                await s.execute(delete(model).where(model.agent_id == self.agent_id))
            await s.commit()
        await self.brain.close()


@pytest_asyncio.fixture
async def world(db):
    w = _World(db)
    yield w
    await w.close()


def _errors(caplog) -> list[str]:
    """What the reviewer logged as an error."""
    return [r.getMessage() for r in caplog.records if r.name == REVIEWER_LOG and r.levelno >= logging.ERROR]


class _ReviewedMeanwhile:
    """A signal check takes time (the pull-request signal is an HTTP call). While
    this one is awaited, the agent's own review of the decision commits. The
    check itself has no verdict; the low-confidence signal after it has."""

    def __init__(self, brain: Brain, only: set | None = None):
        self._brain = brain
        self._only = only

    async def check(self, decision):
        if self._only is None or decision.id in self._only:
            await self._brain.review(decision.id, outcome="success", result="rolled out cleanly", reviewer="agent")
        return None


# ---------------------------------------------------------------------------
# The window between the reviewer's list and its write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["sweep", "session_ended"])
async def test_a_review_made_while_the_signals_are_checked_is_not_replaced(world, entry):
    """The reviewer has listed the decision as unreviewed. Before it writes, the
    agent reviews it. The agent's review stands: the row, one audit row, one
    event, and the sweep does not report the decision as reviewed by it."""
    decision = await world.record(confidence=0.3, session_id="s-1")
    world.reviewer._signals.insert(0, _ReviewedMeanwhile(world.brain))

    graded = []
    if entry == "sweep":
        graded = await world.reviewer.sweep()
    else:
        await world.reviewer.handle(world.session_ended("s-1"))

    assert await world.row(decision.id) == ("success", "agent")
    assert graded == []
    assert await world.audit(decision.id) == [("success", "agent")]
    assert await world.events() == [("success", "agent")]


@pytest.mark.asyncio
async def test_the_sweep_goes_on_after_a_decision_that_was_reviewed_meanwhile(world):
    """A decision that somebody reviewed meanwhile is skipped, not an error: the
    next decision in the same pass still gets its automatic review."""
    first = await world.record(confidence=0.3)
    second = await world.record(confidence=0.3)
    world.reviewer._signals.insert(0, _ReviewedMeanwhile(world.brain, only={first.id}))

    graded = await world.reviewer.sweep()

    assert await world.row(first.id) == ("success", "agent")
    assert await world.row(second.id) == ("failure", "auto")
    assert [g.result for g in graded] == ["failure"]
    assert sorted(await world.events()) == [("failure", "auto"), ("success", "agent")]


@pytest.mark.asyncio
async def test_the_session_ended_handler_skips_a_reviewed_decision_without_an_error(world, caplog):
    """A decision reviewed meanwhile is a skip in the handler as well: the next
    decision of the session is still reviewed in the same loop, and nothing is
    logged as an error."""
    first = await world.record(confidence=0.3, session_id="s-1")
    second = await world.record(confidence=0.3, session_id="s-1")
    world.reviewer._signals.insert(0, _ReviewedMeanwhile(world.brain, only={first.id}))
    left_for_the_sweep = []
    sweep = world.reviewer.sweep

    async def sweep_after_a_look(*args, **kwargs):
        left_for_the_sweep.append(await world.row(second.id))
        return await sweep(*args, **kwargs)

    world.reviewer.sweep = sweep_after_a_look
    with caplog.at_level(logging.ERROR, logger=REVIEWER_LOG):
        await world.reviewer.handle(world.session_ended("s-1"))

    assert left_for_the_sweep == [("failure", "auto")]  # the session's own loop got to the second decision
    assert await world.row(first.id) == ("success", "agent")
    assert _errors(caplog) == []


class _AnotherPassMeanwhile:
    """The reviewer runs from two places: a periodic sweep and the end of every
    session. While this check is awaited, the other pass reviews the decision."""

    def __init__(self, other: DecisionReviewer):
        self._other = other
        self._ran = False

    async def check(self, decision):
        if not self._ran:
            self._ran = True
            await self._other.sweep()
        return None


@pytest.mark.asyncio
async def test_two_passes_of_the_reviewer_write_one_review(world):
    """The pass that arrives second finds the decision reviewed by the first and
    writes nothing: one audit row and one event, not two."""
    decision = await world.record(confidence=0.3)
    other = DecisionReviewer(world.brain, world.settings, EventBus())
    world.reviewer._signals.insert(0, _AnotherPassMeanwhile(other))

    graded = await world.reviewer.sweep()

    assert await world.audit(decision.id) == [("failure", "auto")]
    assert await world.events() == [("failure", "auto")]
    assert await world.row(decision.id) == ("failure", "auto")
    assert graded == []


class _DeletedMeanwhile:
    """While this check is awaited, the decision is deleted."""

    def __init__(self, brain: Brain, only: set):
        self._brain = brain
        self._only = only

    async def check(self, decision):
        if decision.id in self._only:
            await self._brain.delete(decision.id)
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["sweep", "session_ended"])
async def test_a_decision_deleted_while_the_signals_are_checked_is_skipped(world, entry, caplog):
    """The list is stale in this way too: the decision is gone when the reviewer
    comes to write. That is a skip like a review made meanwhile, not an error,
    and the same pass goes on to the next decision."""
    first = await world.record(confidence=0.3, session_id="s-1")
    second = await world.record(confidence=0.3, session_id="s-1")
    world.reviewer._signals.insert(0, _DeletedMeanwhile(world.brain, only={first.id}))

    with caplog.at_level(logging.ERROR, logger=REVIEWER_LOG):
        if entry == "sweep":
            graded = await world.reviewer.sweep()
            assert [g.result for g in graded] == ["failure"]
        else:
            await world.reviewer.handle(world.session_ended("s-1"))

    assert await world.brain.get(first.id) is None
    assert await world.row(second.id) == ("failure", "auto")
    assert _errors(caplog) == []


class _HeldAtCommit:
    """The real database, except that the next session it hands out stops just
    before its commit until ``release`` is set. A review made through it has
    sent its UPDATE (the row is locked) and its transaction is still open. With
    ``fails`` the commit then fails instead, and the review is rolled back."""

    def __init__(self, db, fails: bool = False):
        self._db = db
        self._fails = fails
        self.at_commit = asyncio.Event()
        self.release = asyncio.Event()

    @asynccontextmanager
    async def session(self):
        async with self._db.session() as session:
            commit = session.commit

            async def held_commit() -> None:
                self.at_commit.set()
                await self.release.wait()
                if self._fails:
                    raise ConnectionError("the connection was lost at the commit")
                await commit()

            session.commit = held_commit
            yield session


async def _waits_for_the_row(db) -> None:
    """Return once some statement on brain.decisions is waiting for a row lock."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT
    while loop.time() < deadline:
        async with db.session() as s:
            waiting = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                        "AND query ILIKE '%decisions%'"
                    )
                )
            ).scalar_one()
        if waiting:
            return
        await asyncio.sleep(0.02)
    pytest.fail(f"no statement on brain.decisions waited for the row (watched for {WAIT:.0f}s)")


async def _sweep_while_a_review_is_open(world, decision, review_fails: bool = False):
    """Two transactions on one row. The agent's review has written the row and is
    held just before its commit, so the sweep still lists the decision as
    unreviewed, checks it and goes to write. Once its write waits for the row,
    the agent's review commits, or fails at its commit and is rolled back.
    Returns the two finished tasks: the agent's review and the sweep."""
    held = _HeldAtCommit(world.db, fails=review_fails)
    agent = Brain(database=held, settings=world.settings)
    agent._bus = world.bus
    tasks = [
        asyncio.create_task(agent.review(decision.id, outcome="success", result="rolled out cleanly", reviewer="agent"))
    ]
    try:
        await asyncio.wait_for(held.at_commit.wait(), WAIT)
        tasks.append(asyncio.create_task(world.reviewer.sweep()))
        await _waits_for_the_row(world.db)
    finally:
        held.release.set()
        # Both have ended before the fixture deletes the rows, also when a wait above failed.
        _, stuck = await asyncio.wait(tasks, timeout=WAIT)
        for task in stuck:
            task.cancel()
    reviewing, sweeping = tasks
    return reviewing, sweeping


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_review_that_commits_while_the_automatic_one_waits_for_the_row_is_not_replaced(world):
    """Everything the sweep has read says "unreviewed": the agent's review is not
    committed yet. The sweep's write waits for the row, the review commits, and
    the write goes ahead against a row that is now reviewed. Only a condition the
    database evaluates in the write itself can refuse it; a check on anything read
    earlier cannot. (Postgres lane: the SQLite lane runs every session on one
    shared connection, so a second transaction cannot exist there.)"""
    decision = await world.record(confidence=0.3)

    reviewing, sweeping = await _sweep_while_a_review_is_open(world, decision)

    assert reviewing.exception() is None
    assert await world.row(decision.id) == ("success", "agent")
    assert sweeping.result() == []
    assert await world.audit(decision.id) == [("success", "agent")]
    assert await world.events() == [("success", "agent")]


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_review_that_is_rolled_back_while_the_automatic_one_waits_lets_it_be_written(world):
    """Parity pin, the other ending: the review the sweep waited for fails at its
    commit and is rolled back. The decision is unreviewed after all, and the
    automatic review is written, once. A locked row is waited for; it is not
    taken for a reviewed one."""
    decision = await world.record(confidence=0.3)

    reviewing, sweeping = await _sweep_while_a_review_is_open(world, decision, review_fails=True)

    assert isinstance(reviewing.exception(), ConnectionError)
    assert await world.row(decision.id) == ("failure", "auto")
    assert [g.result for g in sweeping.result()] == ["failure"]
    assert await world.audit(decision.id) == [("failure", "auto")]
    assert await world.events() == [("failure", "auto")]


@pytest.mark.asyncio
async def test_a_review_that_cannot_be_written_takes_its_claim_back(world):
    """The claim and the review are one transaction. If the review cannot be
    written after the row was claimed, the claim goes with it: the decision is
    unreviewed again and the next pass lists it."""
    decision = await world.record(confidence=0.3)

    async def persist_fails(_session, _capture):
        raise RuntimeError("the snapshot store is down")

    with pytest.raises(RuntimeError):
        await world.brain.review(
            decision.id,
            outcome="failure",
            result="Low confidence (0.30) indicates uncertain/failed decision",
            reviewer="auto",
            capture={"persist": persist_fails},
            only_if_unreviewed=True,
        )

    assert (await world.brain.get(decision.id)).reviewed_at is None
    assert [d.id for d in await world.brain.get_unreviewed()] == [decision.id]
    assert await world.audit(decision.id) == []
    assert await world.events() == []


# ---------------------------------------------------------------------------
# What must not change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unreviewed_decision_still_gets_its_automatic_review_and_no_other_row_is_touched(world):
    """Parity pin: the reviewer still grades what nobody has reviewed, with one
    audit row and one event, and leaves a decision it has no verdict on alone."""
    doubtful = await world.record(confidence=0.3)
    confident = await world.record(confidence=0.9)

    graded = await world.reviewer.sweep()

    assert [g.result for g in graded] == ["failure"]
    assert await world.row(doubtful.id) == ("failure", "auto")
    assert (await world.brain.get(doubtful.id)).outcome_result == graded[0].explanation
    assert await world.audit(doubtful.id) == [("failure", "auto")]
    assert await world.events() == [("failure", "auto")]
    assert (await world.brain.get(confident.id)).reviewed_at is None


@pytest.mark.asyncio
async def test_a_review_by_the_agent_or_a_person_still_replaces_any_review(world):
    """Parity pin: only the automatic reviewer is held to "still unreviewed". A
    re-grade by the agent or a person replaces an automatic review and an earlier
    review of their own, one decision at a time or as a batch, as before."""
    decision = await world.record(confidence=0.3)
    await world.reviewer.sweep()
    assert await world.row(decision.id) == ("failure", "auto")

    await world.brain.review(decision.id, outcome="success", result="it held in production", reviewer="agent")
    assert await world.row(decision.id) == ("success", "agent")

    await world.brain.review(decision.id, outcome="partial", result="one rollback was needed", reviewer="a2ui")
    assert await world.row(decision.id) == ("partial", "a2ui")

    batch = [{"decision_id": str(decision.id), "outcome": "failure", "result": "rolled back for good a week later"}]
    assert [item["ok"] for item in await world.brain.review_many(batch, reviewer="agent")] == [True]
    assert await world.row(decision.id) == ("failure", "agent")
    assert sorted(await world.audit(decision.id)) == [
        ("failure", "agent"),
        ("failure", "auto"),
        ("partial", "a2ui"),
        ("success", "agent"),
    ]


class _Says:
    """A signal with a fixed verdict."""

    def __init__(self, verdict: str):
        self._verdict = verdict

    async def check(self, decision):
        return ReviewResult(result=self._verdict, explanation="said so", confidence=1.0, signal_type="fixed")


@pytest.mark.asyncio
async def test_a_verdict_that_brain_review_rejects_still_stops_the_pass(world):
    """Parity pin: the reviewer skips a decision that was reviewed or deleted
    meanwhile, and nothing else. Whatever else Brain.review refuses is raised as
    before (here a verdict that is not an outcome)."""
    await world.record(confidence=0.9)
    world.reviewer._signals.insert(0, _Says("not-an-outcome"))

    with pytest.raises(ValueError):
        await world.reviewer.sweep()


@pytest.mark.asyncio
async def test_a_missing_decision_is_the_same_error_as_before_for_every_other_caller(world):
    """Parity pin: only the automatic reviewer treats a missing decision as a
    skip. For everybody else it is the ValueError with the text it always had
    (the REST route answers 404 on it, the tools report its text)."""
    missing = uuid4()

    with pytest.raises(ValueError) as error:
        await world.brain.review(missing, outcome="success", result="rolled out cleanly", reviewer="agent")

    assert str(error.value) == f"Decision {missing} not found"


# ---------------------------------------------------------------------------
# Inside a caller's session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_callers_session_that_read_the_row_before_the_review_is_refused_too(world):
    """Brain.review joins the caller's session when it is given one, and that
    session may hold its own copy of the row from before the review. The
    condition is tested in the database, not on that copy: the review is refused
    the way Brain.review refuses anything else (a ValueError), nothing is
    written, the copy is left as it was and the caller's transaction is usable."""
    from nous.brain.brain import DecisionAlreadyReviewed

    decision = await world.record(confidence=0.3)
    async with world.db.session() as session:
        copy = await session.get(Decision, decision.id)
        await world.brain.review(decision.id, outcome="success", result="rolled out cleanly", reviewer="agent")

        with pytest.raises(DecisionAlreadyReviewed) as refused:
            await world.brain.review(
                decision.id,
                outcome="failure",
                result="Low confidence (0.30) indicates uncertain/failed decision",
                reviewer="auto",
                session=session,
                only_if_unreviewed=True,
            )
        assert isinstance(refused.value, ValueError)
        assert (copy.outcome, copy.reviewed_at) == ("pending", None)
        await session.commit()

    assert await world.row(decision.id) == ("success", "agent")
    assert await world.audit(decision.id) == [("success", "agent")]


@pytest.mark.asyncio
async def test_a_review_made_again_through_a_session_that_still_shows_it_is_written_whole(world):
    """The other stale copy. The caller's session read the row while it carried a
    review; that review was then taken back (what a revert does), so the row is
    unreviewed and the same review may be written once more. The session's copy
    still shows every field of it. The write must not be a diff against that
    copy, or nothing but the timestamp would reach the row."""
    decision = await world.record(confidence=0.3)
    successor = await world.record(confidence=0.9)
    review = {
        "outcome": "superseded",
        "result": "replaced by the canary rollout",
        "reviewer": "agent",
        "superseded_by": successor.id,
    }
    await world.brain.review(decision.id, **review)

    async with world.db.session() as session:
        copy = await session.get(Decision, decision.id)
        async with world.db.session() as revert:
            await revert.execute(
                update(Decision)
                .where(Decision.id == decision.id)
                .values(outcome="pending", outcome_result=None, reviewed_at=None, reviewer=None, superseded_by=None)
            )
            await revert.commit()
        assert copy.outcome == "superseded"  # the session's copy still shows the review

        await world.brain.review(decision.id, session=session, only_if_unreviewed=True, **review)
        await session.commit()

    written = await world.brain.get(decision.id)
    assert (written.outcome, written.outcome_result, written.reviewer, written.superseded_by) == (
        "superseded",
        "replaced by the canary rollout",
        "agent",
        successor.id,
    )
