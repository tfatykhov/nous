"""Regression tests for the heartbeat_findings lifecycle contract.

These tests exercise the tools.py push_surface path with fake services and
the REAL FindingStore (in-memory, no DB). They are NOT postgres_only so they
run in every CI tier.

The contract, one invariant per codex round:

P2-1   fingerprints are registered in the store BEFORE the surface is
       broadcast (pre_broadcast hook runs inside the push transaction) and a
       blocked push leaves the store exactly as it was.
P2-2   re-pushing a resolved finding transitions RESOLVED -> NEW and bumps
       reopen_count - a supplied real fingerprint takes the same road.
Round 3  a push that fails AFTER the hook ran is compensated: the prior
       record is restored, never merely deleted.
Round 4  the lifecycle's SUPPRESS verdict is honoured on the card: the store
       is PROBED (ingest -> read state -> restore) before the surface is
       built, only items left NEW render, suppressed ones are reported, an
       all-suppressed push registers nothing, and concurrent pushes are
       serialized so one push's compensation cannot clobber another's.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from nous.a2ui.tools import register_a2ui_tools
from nous.api.tools import ToolDispatcher
from nous.heartbeat.finding_store import FindingStore
from nous.heartbeat.schemas import Finding, FindingState

# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


def _store() -> FindingStore:
    """The real store with the 5-minute startup-suppression window zeroed, so
    ingest uses the lifecycle state machine from the first call."""
    store = FindingStore()
    store._startup_suppression_seconds = 0
    return store


def _seed(store: FindingStore, message: str = "disk 91% full", check: str = "disk-check") -> str:
    finding = Finding(source="disk", summary=message, urgency="normal", check_name=check)
    store.ingest(finding)
    return finding.fingerprint()


class _FakeHeartbeatRunner:
    def __init__(self, store: Any) -> None:
        self.finding_store = store


class _PreBroadcastService:
    """Fake push_built that calls pre_broadcast before returning.

    Mirrors the real push_built contract: after the DB flush, pre_broadcast
    fires (registering findings), then the commit lands, then broadcast fires.
    A service that ignores pre_broadcast would mask ordering bugs.
    """

    def __init__(self) -> None:
        self.pushed: list[Any] = []

    async def push_built(self, built: Any, *, pre_broadcast: Any = None, **kwargs: Any) -> str:
        if pre_broadcast is not None:
            pre_broadcast()
        self.pushed.append(built)
        return "surface-123"


def _make_dispatcher(store: Any, service: Any | None = None) -> tuple[ToolDispatcher, Any]:
    dispatcher = ToolDispatcher()
    service = service if service is not None else _PreBroadcastService()
    runner = _FakeHeartbeatRunner(store) if store is not None else None
    register_a2ui_tools(dispatcher, service, heartbeat_runner=runner)
    return dispatcher, service


async def _push(dispatcher: ToolDispatcher, message: str) -> tuple[Any, bool]:
    return await dispatcher.dispatch(
        "push_surface",
        {"template": "heartbeat_findings", "params": {"findings": [{"message": message}]}},
    )


async def _push_rows(dispatcher: ToolDispatcher, rows: list[dict[str, Any]], **extra: Any) -> tuple[Any, bool]:
    return await dispatcher.dispatch(
        "push_surface",
        {"template": "heartbeat_findings", "params": {"findings": rows}, **extra},
    )


def _rendered(service: Any, index: int = -1) -> set[str]:
    dm = service.pushed[index].data_model or {}
    return set(dm.get("findings", {}).keys())


# ---------------------------------------------------------------------------
# P2-1: fingerprint registered before broadcast fires
# ---------------------------------------------------------------------------


async def test_p2_1_fingerprint_registered_before_broadcast() -> None:
    """The fingerprint must be in the store when push_built 'broadcasts'."""
    store = _store()
    store_state_at_broadcast: list[list[dict[str, Any]]] = []

    class _OrderingService:
        def __init__(self) -> None:
            self.pushed: list[Any] = []

        async def push_built(self, built: Any, *, pre_broadcast: Any = None, **kwargs: Any) -> str:
            if pre_broadcast is not None:
                pre_broadcast()
            store_state_at_broadcast.append(list(store.to_list()))
            self.pushed.append(built)
            return "surface-123"

    dispatcher, service = _make_dispatcher(store, _OrderingService())

    content, is_error = await _push(dispatcher, "disk 91%")

    assert not is_error, content
    assert store_state_at_broadcast, "push_built must have been called"
    fps_at_broadcast = {f["fingerprint"] for f in store_state_at_broadcast[0]}
    assert fps_at_broadcast, "finding must be registered by the time broadcast fires"
    assert _rendered(service) == fps_at_broadcast


async def test_p2_1_censor_rejected_push_registers_nothing() -> None:
    """A censor-blocked push must leave the store exactly as it was: the probe
    restores itself and the hook never runs, so rejected prose cannot reach
    GET /heartbeat/findings."""
    store = _store()

    class _CensorBlockedService:
        async def push_built(self, built: Any, *, pre_broadcast: Any = None, **kwargs: Any) -> str:
            raise PermissionError("censor blocked")

    dispatcher, _ = _make_dispatcher(store, _CensorBlockedService())

    _, is_error = await _push(dispatcher, "disk 91%")

    assert is_error
    assert store.to_list() == [], "censor-blocked push must not register any findings"


# ---------------------------------------------------------------------------
# P2-2: resolved finding reopens on re-push
# ---------------------------------------------------------------------------


async def test_p2_2_resolved_finding_reopens_on_repush() -> None:
    """Re-pushing a resolved finding must transition RESOLVED -> NEW with
    reopen_count incremented, not skip ingest because the fp is 'known'."""
    store = _store()
    dispatcher, _service = _make_dispatcher(store)

    _, is_error = await _push(dispatcher, "disk 91% full")
    assert not is_error

    findings = store.to_list()
    assert len(findings) == 1
    fp = findings[0]["fingerprint"]
    assert findings[0]["state"] == "new"
    assert findings[0]["reopen_count"] == 0

    assert store.resolve(fp), "resolve must succeed"
    assert store.to_list()[0]["state"] == "resolved"

    _, is_error = await _push(dispatcher, "disk 91% full")
    assert not is_error

    findings = store.to_list()
    assert len(findings) == 1, "store must still have exactly one tracked finding"
    assert findings[0]["state"] == "new", f"finding must be reopened to NEW, got {findings[0]['state']!r}"
    assert findings[0]["reopen_count"] == 1, f"reopen_count must be 1, got {findings[0]['reopen_count']}"


# ---------------------------------------------------------------------------
# Round 3 P2-A: a caller-supplied REAL fingerprint must also re-ingest
# ---------------------------------------------------------------------------


async def test_round3_supplied_fingerprint_of_resolved_finding_reopens() -> None:
    """Adopting a real fingerprint must not mean skipping the lifecycle: a
    RESOLVED record reopens (reopen_count, flapping bookkeeping) and the card
    renders the same fingerprint the store now tracks as open."""
    store = _store()
    fp = _seed(store)
    assert store.resolve(fp)
    assert store.to_list()[0]["state"] == "resolved"

    dispatcher, service = _make_dispatcher(store)

    content, is_error = await _push_rows(
        dispatcher, [{"fingerprint": fp, "message": "disk 91% full", "check": "disk-check"}]
    )
    assert not is_error, content

    rows = store.to_list()
    assert len(rows) == 1, "adopting a real fingerprint must not fork a second record"
    assert rows[0]["fingerprint"] == fp, "the supplied fingerprint must be adopted unchanged"
    assert rows[0]["state"] == "new", f"supplied-fp re-push must reopen, got {rows[0]['state']!r}"
    assert rows[0]["reopen_count"] == 1, f"reopen_count must be 1, got {rows[0]['reopen_count']}"
    assert _rendered(service) == {fp}


# ---------------------------------------------------------------------------
# Round 3 P2-B: a push that fails AFTER the hook ran must not orphan findings
# ---------------------------------------------------------------------------


class _CommitFailsService:
    """The hook runs, then the commit blows up (disconnect / serialization
    failure): the real service compensates, then re-raises."""

    async def push_built(
        self,
        built: Any,
        *,
        pre_broadcast: Any = None,
        pre_broadcast_rollback: Any = None,
        **kwargs: Any,
    ) -> str:
        if pre_broadcast is not None:
            pre_broadcast()
        if pre_broadcast_rollback is not None:
            pre_broadcast_rollback()
        raise RuntimeError("commit failed")


async def test_round3_commit_failure_after_hook_leaves_no_orphan_finding() -> None:
    store = _store()
    dispatcher, _ = _make_dispatcher(store, _CommitFailsService())

    _, is_error = await _push(dispatcher, "disk 91% full")

    assert is_error, "a failed commit must surface as a tool error"
    assert store.to_list() == [], "a rolled-back push must leave no orphaned finding"


async def test_round3_orphan_is_compensated_even_if_service_skips_rollback() -> None:
    """Belt-and-braces: push_surface's own except path restores the snapshot
    when a service raises after the hook without calling the rollback hook;
    double-restore is a no-op."""
    store = _store()

    class _RaisesWithoutRollback:
        async def push_built(self, built: Any, *, pre_broadcast: Any = None, **kwargs: Any) -> str:
            if pre_broadcast is not None:
                pre_broadcast()
            raise RuntimeError("boom after hook")

    dispatcher, _ = _make_dispatcher(store, _RaisesWithoutRollback())

    _, is_error = await _push(dispatcher, "disk 91% full")
    assert is_error
    assert store.to_list() == [], "tool-level compensation must remove the orphan"


async def test_round3_compensation_restores_prior_state_not_just_deletes() -> None:
    """A finding already tracked (and resolved) before the failed push must be
    left exactly as it was - deleting it would destroy real lifecycle history."""
    store = _store()
    fp = _seed(store)
    store.resolve(fp)
    before = store.to_list()[0]

    dispatcher, _ = _make_dispatcher(store, _CommitFailsService())

    _, is_error = await _push_rows(
        dispatcher, [{"fingerprint": fp, "message": "disk 91% full", "check": "disk-check"}]
    )
    assert is_error
    after = store.to_list()[0]
    assert after["state"] == "resolved" == before["state"], "prior state must survive compensation"
    assert after["reopen_count"] == before["reopen_count"], "compensation must undo the reopen bump"


# ---------------------------------------------------------------------------
# Round 4: the SUPPRESS verdict is honoured, the probe leaves no trace, and
# concurrent pushes cannot clobber each other's compensation
# ---------------------------------------------------------------------------


async def test_round4_probe_leaves_store_untouched_when_push_is_blocked() -> None:
    """The lifecycle probe reopens a RESOLVED record to learn its verdict and
    restores it synchronously - a blocked push must show no trace of that."""
    store = _store()
    fp = _seed(store)
    store.resolve(fp)
    before = store.to_list()[0]

    class _CensorBlockedService:
        async def push_built(self, built: Any, **kwargs: Any) -> str:
            raise PermissionError("censor blocked")

    dispatcher, _ = _make_dispatcher(store, _CensorBlockedService())

    _, is_error = await _push_rows(dispatcher, [{"fingerprint": fp, "message": "disk 91% full"}])

    assert is_error
    after = store.to_list()[0]
    assert after["state"] == "resolved" == before["state"]
    assert after["reopen_count"] == before["reopen_count"] == 0
    assert after["seen_count"] == before["seen_count"]


async def test_round4_acknowledged_finding_is_not_resurfaced() -> None:
    """An already-acknowledged duplicate is what the heartbeat runner skips on
    SUPPRESS; the card must skip it too, say so, and mutate nothing."""
    store = _store()
    fp = _seed(store)
    assert store.acknowledge(fp)
    before = store.to_list()[0]
    dispatcher, service = _make_dispatcher(store)

    content, is_error = await _push_rows(dispatcher, [{"fingerprint": fp, "message": "disk 91% full"}])

    assert not is_error, content
    payload = json.loads(content)
    assert payload["pushed"] is False
    assert payload["surface_id"] is None
    assert [s["state"] for s in payload["suppressed"]] == ["acknowledged"]
    assert payload["suppressed"][0]["fingerprint"] == fp
    assert not service.pushed, "nothing open for triage => no surface"
    after = store.to_list()[0]
    assert after["state"] == "acknowledged"
    assert after["seen_count"] == before["seen_count"], "a dropped item must not be ingested"


async def test_round4_startup_window_registers_nothing_and_says_so() -> None:
    """During the 5-minute startup window every ingest is SUPPRESS and a new
    fingerprint would be stored as SUPPRESSED - a state that never transitions
    to NEW. Persisting that from an agent push would wedge the item for the
    process lifetime, so the probe restores and the agent is told to retry."""
    store = FindingStore()  # window ACTIVE
    dispatcher, service = _make_dispatcher(store)

    content, is_error = await _push(dispatcher, "disk 91% full")

    assert not is_error, content
    payload = json.loads(content)
    assert payload["pushed"] is False
    assert "startup" in payload["note"]
    assert [s["state"] for s in payload["suppressed"]] == ["suppressed"]
    assert not service.pushed
    assert store.to_list() == [], "nothing may be registered during the window"

    # After the window the very same push registers normally.
    store._startup_suppression_seconds = 0
    content, is_error = await _push(dispatcher, "disk 91% full")
    assert not is_error, content
    assert store.to_list()[0]["state"] == "new"
    assert len(service.pushed) == 1


async def test_round4_flapping_finding_is_routed_away_from_the_card() -> None:
    """The third resolved->reopened flap is routed to the digest by the store
    (ACKNOWLEDGED + SUPPRESS); the card must not keep re-surfacing it."""
    store = _store()
    fp = _seed(store)
    dispatcher, service = _make_dispatcher(store)

    for expected_reopens in (1, 2):
        assert store.resolve(fp)
        content, is_error = await _push_rows(dispatcher, [{"fingerprint": fp, "message": "disk 91% full"}])
        assert not is_error, content
        assert store.to_list()[0]["reopen_count"] == expected_reopens
        assert _rendered(service) == {fp}

    assert store.resolve(fp)
    content, is_error = await _push_rows(dispatcher, [{"fingerprint": fp, "message": "disk 91% full"}])

    assert not is_error, content
    payload = json.loads(content)
    assert payload["pushed"] is False
    assert [s["state"] for s in payload["suppressed"]] == ["acknowledged"]
    assert len(service.pushed) == 2, "the flapping item must not render a third card"


async def test_round4_mixed_push_renders_only_open_items_and_reports_the_rest() -> None:
    store = _store()
    acked = _seed(store, "old thing")
    assert store.acknowledge(acked)
    dispatcher, service = _make_dispatcher(store)

    content, is_error = await _push_rows(
        dispatcher,
        [{"fingerprint": acked, "message": "old thing"}, {"message": "new thing"}],
    )

    assert not is_error, content
    payload = json.loads(content)
    assert payload["surface_id"] == "surface-123"
    assert [s["fingerprint"] for s in payload["suppressed"]] == [acked]
    rendered = _rendered(service)
    assert acked not in rendered
    assert len(rendered) == 1
    (new_fp,) = rendered
    assert store.get_tracked(new_fp).state == FindingState.NEW
    assert store.get_tracked(acked).state == FindingState.ACKNOWLEDGED


async def test_round4_concurrent_pushes_serialize_compensation() -> None:
    """Two pushes with different dedup keys carrying the same fingerprint: the
    first parks in commit and then fails; the second succeeds. Unserialized,
    the first push's restore (snapshot: 'not tracked') would DELETE the record
    the second push just published a live card against. Under the lock the
    second push cannot start until the first has compensated."""
    store = _store()

    class _FirstCommitStalls:
        def __init__(self) -> None:
            self.calls = 0
            self.gate = asyncio.Event()
            self.pushed: list[Any] = []

        async def push_built(
            self,
            built: Any,
            *,
            pre_broadcast: Any = None,
            pre_broadcast_rollback: Any = None,
            **kwargs: Any,
        ) -> str:
            self.calls += 1
            if pre_broadcast is not None:
                pre_broadcast()
            if self.calls == 1:
                await self.gate.wait()
                if pre_broadcast_rollback is not None:
                    pre_broadcast_rollback()
                raise RuntimeError("commit failed")
            self.pushed.append(built)
            return f"surface-{self.calls}"

    dispatcher, service = _make_dispatcher(store, _FirstCommitStalls())

    first = asyncio.create_task(_push_rows(dispatcher, [{"message": "disk 91% full"}], dedup_key="a"))
    while service.calls < 1:
        await asyncio.sleep(0)
    second = asyncio.create_task(_push_rows(dispatcher, [{"message": "disk 91% full"}], dedup_key="b"))
    await asyncio.sleep(0.01)
    assert service.calls == 1, "the second push must wait for the first to finish"
    service.gate.set()

    (_, first_error), (second_content, second_error) = await asyncio.gather(first, second)

    assert first_error, "the stalled push failed at commit"
    assert not second_error, second_content
    assert len(service.pushed) == 1
    rows = store.to_list()
    assert len(rows) == 1, "the live card's finding must still be tracked"
    assert rows[0]["state"] == "new"
    assert _rendered(service) == {rows[0]["fingerprint"]}
