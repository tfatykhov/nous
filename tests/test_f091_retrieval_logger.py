"""F091: RetrievalLogger — sampling, ring buffer, and write-task retention."""

from __future__ import annotations

import asyncio

import pytest

from nous.observability.retrieval_logger import RetrievalLogger, get_active, set_active
from nous.observability.retrieval_trace import NullTrace, RetrievalTrace


def _logger(**kw) -> RetrievalLogger:
    kw.setdefault("agent_id", "nous-test")
    return RetrievalLogger(**kw)


def test_disabled_logger_hands_back_the_null_trace():
    rl = _logger(enabled=False)
    tr = rl.start(query="q", path="pipeline")
    assert isinstance(tr, NullTrace)


def test_enabled_logger_hands_back_a_real_trace_carrying_identity():
    rl = _logger(candidate_sample_rate=1.0)
    tr = rl.start(query="q", path="context", session_id="s1", turn_number=3,
                  trace_id="abc123")
    assert isinstance(tr, RetrievalTrace)
    assert (tr.path, tr.session_id, tr.turn_number, tr.trace_id) == (
        "context", "s1", 3, "abc123",
    )
    assert tr.agent_id == "nous-test"


def test_sample_rate_zero_still_records_legs_and_expansions():
    """Sampling governs COST (the candidate array), not visibility — an
    unsampled retrieval must still answer 'which legs fired'."""
    rl = _logger(candidate_sample_rate=0.0)
    tr = rl.start(query="q", path="pipeline")
    tr.leg("heart_primary", n_returned=4)
    tr.add("id-1", "fact", "heart_primary", score=0.9)

    d = tr.to_dict()
    assert d["candidates"] is None
    assert d["legs"][0]["n_returned"] == 4


def test_sample_rate_one_captures_candidates():
    rl = _logger(candidate_sample_rate=1.0)
    tr = rl.start(query="q", path="pipeline")
    tr.add("id-1", "fact", "heart_primary", score=0.9)
    assert len(tr.to_dict()["candidates"]) == 1


def test_sample_rate_is_clamped_to_unit_interval():
    assert _logger(candidate_sample_rate=5.0)._sample_rate == 1.0
    assert _logger(candidate_sample_rate=-2.0)._sample_rate == 0.0


def test_commit_is_a_noop_for_nulltrace():
    written: list = []
    rl = _logger(enabled=False, db_writer=lambda p: written.append(p))
    rl.commit(rl.start(query="q", path="pipeline"))
    assert written == []
    assert rl.get_recent() == []


def test_commit_populates_the_ring_and_is_readable_by_id():
    rl = _logger(candidate_sample_rate=1.0)
    tr = rl.start(query="find me", path="pipeline")
    tr.finalize([])
    rl.commit(tr)

    recent = rl.get_recent()
    assert len(recent) == 1
    assert recent[0]["query"] == "find me"
    assert rl.get(tr.id)["id"] == tr.id


def test_ring_evicts_oldest_and_prunes_its_index():
    rl = _logger(ring_size=3)
    ids = []
    for i in range(6):
        tr = rl.start(query=f"q{i}", path="pipeline")
        ids.append(tr.id)
        rl.commit(tr)

    recent = rl.get_recent()
    assert len(recent) == 3
    # Newest first.
    assert [e["query"] for e in recent] == ["q5", "q4", "q3"]
    # Index must not retain evicted entries — that was the ContextLogger leak.
    assert len(rl._by_id) <= len(rl._entries) + 10


def test_get_recent_filters_by_path():
    rl = _logger()
    for path in ("pipeline", "context", "pipeline"):
        rl.commit(rl.start(query="q", path=path))

    assert len(rl.get_recent(path="pipeline")) == 2
    assert len(rl.get_recent(path="context")) == 1


def test_commit_snapshots_so_later_mutation_cannot_leak_into_the_write():
    """The background writer must never see state the caller added after
    commit — hence to_dict() is called synchronously in commit()."""
    rl = _logger(candidate_sample_rate=1.0)
    tr = rl.start(query="q", path="pipeline")
    tr.add("id-1", "fact", "heart_primary", score=0.5)
    rl.commit(tr)

    tr.add("id-2", "fact", "heart_primary", score=0.4)

    assert rl.get_recent()[0]["n_candidates"] == 1


def test_commit_without_an_event_loop_does_not_raise_or_warn():
    """Sync context (tests, scripts) must not blow up or leave an un-awaited
    coroutine behind."""
    async def _writer(_payload):
        return None

    rl = _logger(db_writer=_writer)
    rl.commit(rl.start(query="q", path="pipeline"))  # no running loop here
    assert len(rl.get_recent()) == 1


@pytest.mark.asyncio
async def test_write_task_is_retained_until_it_completes():
    """asyncio keeps only a weak ref to tasks — a discarded one can be GC'd
    mid-flight, silently dropping the INSERT. Same bug class already fixed in
    context_logger.py:345."""
    written: list = []

    async def _writer(payload):
        await asyncio.sleep(0)
        written.append(payload)

    rl = _logger(db_writer=_writer, candidate_sample_rate=1.0)
    tr = rl.start(query="q", path="pipeline")
    tr.add("id-1", "fact", "heart_primary", score=0.9)
    tr.finalize([])
    rl.commit(tr)

    assert len(rl._pending_tasks) == 1  # retained while in flight
    await asyncio.gather(*list(rl._pending_tasks))

    assert len(written) == 1
    assert written[0]["n_candidates"] == 1
    assert rl._pending_tasks == set()  # discarded on completion


@pytest.mark.asyncio
async def test_writer_failure_does_not_break_the_caller():
    async def _boom(_payload):
        raise RuntimeError("db down")

    rl = _logger(db_writer=_boom)
    tr = rl.start(query="q", path="pipeline")
    rl.commit(tr)

    results = await asyncio.gather(*list(rl._pending_tasks), return_exceptions=True)
    assert isinstance(results[0], RuntimeError)
    # The in-memory ring is unaffected by a failed persist.
    assert len(rl.get_recent()) == 1


def test_active_registry_round_trips_and_clears():
    original = get_active()
    try:
        rl = _logger()
        set_active(rl)
        assert get_active() is rl
        set_active(None)
        assert get_active() is None
    finally:
        set_active(original)


@pytest.mark.asyncio
async def test_drain_awaits_in_flight_writes():
    """Fire-and-forget writes are cancelled by loop teardown or wake against a
    closed pool at shutdown, and the writer swallows its own errors — so the
    loss is silent unless shutdown drains first."""
    written: list = []

    async def _slow_writer(payload):
        await asyncio.sleep(0.05)
        written.append(payload)

    rl = _logger(db_writer=_slow_writer)
    rl.commit(rl.start(query="q", path="pipeline"))
    assert written == []  # still in flight

    await rl.drain()

    assert len(written) == 1


@pytest.mark.asyncio
async def test_drain_is_a_noop_with_nothing_pending():
    rl = _logger()
    await rl.drain()  # must not raise


@pytest.mark.asyncio
async def test_drain_is_bounded_by_its_timeout():
    """A wedged write must not hold shutdown open forever."""
    async def _never(payload):
        await asyncio.sleep(30)

    rl = _logger(db_writer=_never)
    rl.commit(rl.start(query="q", path="pipeline"))

    await asyncio.wait_for(rl.drain(timeout=0.05), timeout=2.0)

    for t in list(rl._pending_tasks):
        t.cancel()


def test_retrieval_paths_are_registered():
    """Every `path=` literal handed to `.start()` must be in RETRIEVAL_PATHS.

    `/dashboard/retrieval` validates its `path` filter against that tuple, so a
    producer emitting a value the API rejects writes rows nobody can filter to
    — the precise failure this telemetry exists to catch. It is not
    hypothetical: in-script `recall_deep` retrievals were unobservable until
    2026-08-25, and the first fix attempt could have shipped a fourth path with
    the validator still hardcoded to two.

    AST, not a line regex: a regex reports clean over a call split across lines,
    which is worse than no guard because it stops anyone looking.
    """
    import ast
    from pathlib import Path

    from nous.observability.retrieval_logger import RETRIEVAL_PATHS

    root = Path(__file__).resolve().parents[1] / "nous"
    found: dict[str, str] = {}

    for py in root.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "start"):
                continue
            for kw in node.keywords:
                if kw.arg == "path" and isinstance(kw.value, ast.Constant):
                    if isinstance(kw.value.value, str):
                        found[kw.value.value] = f"{py.name}:{node.lineno}"

    assert found, "no instrumented .start(path=...) call sites found — scan broke"
    unregistered = {p: loc for p, loc in found.items() if p not in RETRIEVAL_PATHS}
    assert not unregistered, (
        f"path(s) emitted but not in RETRIEVAL_PATHS: {unregistered}. "
        "Add them there, or /dashboard/retrieval will 400 on the filter."
    )
    # Both directions: a stale entry means the dashboard advertises a filter
    # that can only ever return nothing.
    assert set(RETRIEVAL_PATHS) == set(found), (
        f"RETRIEVAL_PATHS has entries no call site emits: "
        f"{set(RETRIEVAL_PATHS) - set(found)}"
    )
