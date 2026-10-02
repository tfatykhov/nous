"""Post-merge review P2-9: one ``decision_reviewed`` audit row per review.

#651 added a post-commit bus emit for every review. The bus persister writes
every bus event to ``nous_system.events``, and ``Brain._review`` already writes
its own row inside the review transaction, so each review produced two rows
with both strategy-card flags off.
"""

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.brain import Brain
from nous.brain.schemas import ReasonInput, RecordInput
from nous.events import Event as BusEvent
from nous.events import EventBus
from nous.storage.models import Event


class _RollbackDatabase:
    """Database stand-in whose sessions all join one outer transaction.

    ``Brain.review`` only emits on the bus when it owns its session, so the
    test cannot hand it the rollback-isolated ``session`` fixture. On Postgres
    every ``commit()`` here releases a SAVEPOINT and the fixture rolls the lot
    back. On the SQLite lane the rows stay in the in-memory database, under
    this test's own ``agent_id``.
    """

    def __init__(self, conn):
        self._conn = conn

    @asynccontextmanager
    async def session(self):
        async with AsyncSession(bind=self._conn, expire_on_commit=False, join_transaction_mode="create_savepoint") as s:
            yield s


async def _drain(bus: EventBus) -> None:
    """Dispatch queued events one at a time (the bus loop is not started here,
    so nothing else touches the shared connection)."""
    while not bus._queue.empty():
        await bus._dispatch(bus._queue.get_nowait())


@pytest_asyncio.fixture
async def wired_brain(db, settings):
    """A real Brain on a real EventBus with the persister main.py installs."""
    async with db.engine.connect() as conn:
        trans = await conn.begin()
        brain = Brain(
            database=_RollbackDatabase(conn),
            settings=settings.model_copy(update={"agent_id": f"fix-d-{uuid4().hex[:8]}"}),
        )
        bus = EventBus()

        async def persist_to_db(event: BusEvent) -> None:
            await brain.emit_event(
                event.type,
                {**event.data},
                session_id=event.session_id,
                event_id=event.event_id,
                trace_id=event.trace_id,
                caused_by=event.caused_by,
            )

        bus.set_db_persister(persist_to_db)
        brain._bus = bus
        try:
            yield brain, bus
        finally:
            await brain.close()
            await trans.rollback()


async def _count(brain: Brain, event_type: str) -> int:
    async with brain.db.session() as s:
        result = await s.execute(
            select(func.count())
            .select_from(Event)
            .where(Event.agent_id == brain.agent_id, Event.event_type == event_type)
        )
        return result.scalar_one()


def _decision() -> RecordInput:
    return RecordInput(
        description="Use cursor pagination for the list endpoint",
        confidence=0.8,
        category="architecture",
        stakes="low",
        context="Offsets drift while rows are being inserted",
        pattern="Prefer stable cursors over offsets",
        tags=["api", "pagination"],
        reasons=[ReasonInput(type="analysis", text="Offsets skip rows under concurrent writes")],
    )


async def test_review_writes_one_decision_reviewed_row(wired_brain):
    brain, bus = wired_brain
    detail = await brain.record(_decision())
    await _drain(bus)

    await brain.review(detail.id, "success", result="No skipped rows in a week")
    # The row is the one _review wrote in the review transaction: it is there
    # before the bus dispatches anything.
    assert await _count(brain, "decision_reviewed") == 1
    await _drain(bus)

    assert await _count(brain, "decision_reviewed") == 1


async def test_review_without_a_bus_writes_one_row(wired_brain):
    """Parity pin (green before and after the fix): the audit row does not
    depend on the bus."""
    brain, bus = wired_brain
    detail = await brain.record(_decision())
    await _drain(bus)
    brain._bus = None

    await brain.review(detail.id, "success")

    assert await _count(brain, "decision_reviewed") == 1


async def test_unmarked_bus_event_is_still_persisted(wired_brain):
    """Control (green before and after the fix): the persister in this fixture
    really writes rows, so a count of 1 above is the marker's doing."""
    brain, bus = wired_brain
    await bus.emit(BusEvent(type="fix_d_control", agent_id=brain.agent_id))
    await _drain(bus)

    assert await _count(brain, "fix_d_control") == 1


async def test_review_many_writes_one_row_per_reviewed_decision(wired_brain):
    brain, bus = wired_brain
    first = await brain.record(_decision())
    second = await brain.record(_decision())
    await _drain(bus)

    results = await brain.review_many(
        [
            {"decision_id": str(first.id), "outcome": "success"},
            {"decision_id": str(second.id), "outcome": "failure"},
        ],
        reviewer="test",
    )
    await _drain(bus)

    assert [r["ok"] for r in results] == [True, True]
    assert await _count(brain, "decision_reviewed") == 2


async def test_review_still_reaches_bus_handlers(wired_brain):
    """The strategy-card distiller subscribes to this event: skipping the
    duplicate row must not skip delivery."""
    brain, bus = wired_brain
    seen: list[BusEvent] = []

    async def handler(event: BusEvent) -> None:
        seen.append(event)

    bus.on("decision_reviewed", handler)
    detail = await brain.record(_decision())
    await brain.review(detail.id, "partial", reviewer="tim")
    await _drain(bus)

    assert [(e.data["decision_id"], e.data["outcome"], e.data["reviewer"]) for e in seen] == [
        (str(detail.id), "partial", "tim")
    ]
