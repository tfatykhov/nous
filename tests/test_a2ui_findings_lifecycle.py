"""Regression tests for heartbeat_findings lifecycle ordering (P2-1, P2-2).

These tests exercise the tools.py push_surface path with fake services and
the real FindingStore (no DB needed). They are NOT postgres_only so they run
in every CI tier and catch regressions without a live database.

P2-1: fingerprints must be registered in the finding store BEFORE the surface
      is broadcast to connected clients (pre_broadcast hook).
P2-2: re-pushing a resolved finding must transition RESOLVED -> NEW and
      increment reopen_count (always route through ingest, even for known fps).
"""

from __future__ import annotations

from typing import Any

from nous.a2ui.tools import register_a2ui_tools
from nous.api.tools import ToolDispatcher
from nous.heartbeat.finding_store import FindingStore
from nous.heartbeat.schemas import Finding

# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class _FakeFindingStore:
    """Minimal store that tracks ingest calls and fingerprints."""

    def __init__(self, fingerprints: list[str] | None = None) -> None:
        self._fps: list[str] = list(fingerprints or [])
        self.ingested: list[Finding] = []

    def to_list(self) -> list[dict[str, Any]]:
        return [{"fingerprint": fp, "summary": "x", "state": "new"} for fp in self._fps]

    def ingest(self, finding: Finding) -> str:
        self.ingested.append(finding)
        fp = finding.fingerprint()
        if fp not in self._fps:
            self._fps.append(fp)
        return "TRIAGE"

    def get_tracked(self, fingerprint: str) -> Any | None:
        return None

    # Compensation API — mirrors the real FindingStore contract so this fake
    # cannot pass a test the real store would fail.
    def snapshot(self, fingerprints: Any) -> dict[str, bool]:
        return {fp: fp in self._fps for fp in fingerprints}

    def restore(self, snapshot: dict[str, bool]) -> None:
        for fp, was_tracked in snapshot.items():
            if not was_tracked and fp in self._fps:
                self._fps.remove(fp)


class _FakeHeartbeatRunner:
    def __init__(self, store: Any) -> None:
        self.finding_store = store


class _PreBroadcastService:
    """Fake push_built that calls pre_broadcast before returning.

    Mirrors the real push_built contract: after the DB flush, pre_broadcast
    fires (registering findings), then the commit lands, then broadcast fires.
    A service that ignores pre_broadcast (old _PushBuiltService) would mask
    P2-1 ordering bugs in unit tests.
    """

    def __init__(self) -> None:
        self.pushed: list[Any] = []

    async def push_built(self, built: Any, *, pre_broadcast: Any = None, **kwargs: Any) -> str:
        if pre_broadcast is not None:
            pre_broadcast()
        self.pushed.append(built)
        return "surface-123"


def _make_dispatcher(store: Any) -> tuple[ToolDispatcher, _PreBroadcastService]:
    dispatcher = ToolDispatcher()
    service = _PreBroadcastService()
    runner = _FakeHeartbeatRunner(store) if store is not None else None
    register_a2ui_tools(dispatcher, service, heartbeat_runner=runner)
    return dispatcher, service


async def _push(dispatcher: ToolDispatcher, message: str) -> tuple[Any, bool]:
    return await dispatcher.dispatch(
        "push_surface",
        {"template": "heartbeat_findings", "params": {"findings": [{"message": message}]}},
    )


# ---------------------------------------------------------------------------
# P2-1: fingerprint registered before broadcast fires
# ---------------------------------------------------------------------------


async def test_p2_1_fingerprint_registered_before_broadcast() -> None:
    """P2-1: the fingerprint must be in the store when push_built 'broadcasts'.

    push_built calls pre_broadcast before returning (the real implementation
    calls it inside the open transaction, before commit+broadcast). This test
    captures store state at that moment and asserts the fingerprint was already
    registered — a client receiving the broadcast can immediately act on it.
    """
    store = _FakeFindingStore()
    store_state_at_broadcast: list[list[dict[str, Any]]] = []

    class _OrderingService:
        def __init__(self) -> None:
            self.pushed: list[Any] = []

        async def push_built(self, built: Any, *, pre_broadcast: Any = None, **kwargs: Any) -> str:
            if pre_broadcast is not None:
                pre_broadcast()
            # Snapshot store state at "broadcast" time
            store_state_at_broadcast.append(list(store.to_list()))
            self.pushed.append(built)
            return "surface-123"

    dispatcher = ToolDispatcher()
    service = _OrderingService()
    register_a2ui_tools(dispatcher, service, heartbeat_runner=_FakeHeartbeatRunner(store))

    content, is_error = await dispatcher.dispatch(
        "push_surface",
        {"template": "heartbeat_findings", "params": {"findings": [{"message": "disk 91%"}]}},
    )

    assert not is_error, content
    assert store_state_at_broadcast, "push_built must have been called"
    fps_at_broadcast = {f["fingerprint"] for f in store_state_at_broadcast[0]}
    assert fps_at_broadcast, "finding must be registered by the time broadcast fires"
    # The rendered fingerprint matches what was registered
    assert len(service.pushed) == 1
    dm = service.pushed[0].data_model or {}
    rendered_fps = set(dm.get("findings", {}).keys())
    assert rendered_fps == fps_at_broadcast, f"rendered {rendered_fps} != store at broadcast {fps_at_broadcast}"


async def test_p2_1_censor_rejected_push_registers_nothing() -> None:
    """P2-1 guard: if push_built raises, pre_broadcast is never called.

    Censor rejection happens before the pre_broadcast hook fires, so a blocked
    push must leave the finding store completely unchanged. Guards the P1 fix
    (ingest must not expose rejected prose via GET /heartbeat/findings).
    """
    store = _FakeFindingStore()

    class _CensorBlockedService:
        async def push_built(self, built: Any, *, pre_broadcast: Any = None, **kwargs: Any) -> str:
            # Never calls pre_broadcast — censor gate fires before commit
            raise PermissionError("censor blocked")

    dispatcher = ToolDispatcher()
    register_a2ui_tools(dispatcher, _CensorBlockedService(), heartbeat_runner=_FakeHeartbeatRunner(store))

    _, is_error = await dispatcher.dispatch(
        "push_surface",
        {"template": "heartbeat_findings", "params": {"findings": [{"message": "disk 91%"}]}},
    )

    assert is_error
    assert not store.ingested, "censor-blocked push must not register any findings"


# ---------------------------------------------------------------------------
# P2-2: resolved finding reopens on re-push
# ---------------------------------------------------------------------------


async def test_p2_2_resolved_finding_reopens_on_repush() -> None:
    """P2-2: re-pushing a resolved finding must call ingest and transition
    RESOLVED -> NEW with reopen_count incremented.

    Before the fix, 'fingerprint already known' caused the second push to skip
    ingest entirely, leaving the finding in RESOLVED state and reopen_count=0
    while the surface showed new buttons for a finding the store thought was done.
    """
    store = FindingStore()
    # Bypass startup suppression (5-min window) so ingest uses the real
    # lifecycle state machine from the first call, not the blanket SUPPRESS path
    # that short-circuits all state transitions during startup.
    store._startup_suppression_seconds = 0

    class _RunnerWithRealStore:
        def __init__(self) -> None:
            self.finding_store = store

    dispatcher = ToolDispatcher()
    service = _PreBroadcastService()
    register_a2ui_tools(dispatcher, service, heartbeat_runner=_RunnerWithRealStore())

    # First push: registers the finding as NEW
    _, is_error = await _push(dispatcher, "disk 91% full")
    assert not is_error

    findings = store.to_list()
    assert len(findings) == 1
    fp = findings[0]["fingerprint"]
    assert findings[0]["state"] == "new"
    assert findings[0]["reopen_count"] == 0

    # Simulate user resolving the finding
    assert store.resolve(fp), "resolve must succeed"
    assert store.to_list()[0]["state"] == "resolved"

    # Re-push the identical message: must reopen, not silently skip ingest
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
    """A caller-supplied fingerprint took a fast path that skipped ingest.

    The card then rendered live Acknowledge/Resolve/Dismiss buttons over a
    finding the store still considered RESOLVED: no RESOLVED -> NEW
    transition, no reopen_count, so flapping/outcome bookkeeping disagreed
    with what the user saw. Adopting the fingerprint must not mean skipping
    the lifecycle.
    """
    store = FindingStore()
    store._startup_suppression_seconds = 0
    store.ingest(Finding(source="disk", summary="disk 91% full", urgency="normal", check_name="disk-check"))
    fp = store.to_list()[0]["fingerprint"]
    assert store.resolve(fp)
    assert store.to_list()[0]["state"] == "resolved"

    dispatcher = ToolDispatcher()
    service = _PreBroadcastService()
    register_a2ui_tools(dispatcher, service, heartbeat_runner=_FakeHeartbeatRunner(store))

    content, is_error = await dispatcher.dispatch(
        "push_surface",
        {
            "template": "heartbeat_findings",
            "params": {"findings": [{"fingerprint": fp, "message": "disk 91% full", "check": "disk-check"}]},
        },
    )
    assert not is_error, content

    rows = store.to_list()
    assert len(rows) == 1, "adopting a real fingerprint must not fork a second record"
    assert rows[0]["fingerprint"] == fp, "the supplied fingerprint must be adopted unchanged"
    assert rows[0]["state"] == "new", f"supplied-fp re-push must reopen, got {rows[0]['state']!r}"
    assert rows[0]["reopen_count"] == 1, f"reopen_count must be 1, got {rows[0]['reopen_count']}"
    # And the card renders the same fingerprint the store now tracks as open.
    dm = service.pushed[0].data_model or {}
    assert set(dm.get("findings", {}).keys()) == {fp}


# ---------------------------------------------------------------------------
# Round 3 P2-B: a push that fails AFTER the hook ran must not orphan findings
# ---------------------------------------------------------------------------


async def test_round3_commit_failure_after_hook_leaves_no_orphan_finding() -> None:
    """The store is in-memory and cannot roll back with the DB transaction.

    pre_broadcast runs before commit (so a client never gets dead buttons); a
    commit that then fails must not leave the finding registered and visible
    at GET /heartbeat/findings for a surface that was never published.
    """
    store = FindingStore()
    store._startup_suppression_seconds = 0

    class _CommitFailsService:
        async def push_built(self, built: Any, *, pre_broadcast: Any = None, pre_broadcast_rollback: Any = None, **kwargs: Any) -> str:
            if pre_broadcast is not None:
                pre_broadcast()
            # commit blows up (disconnect / serialization failure): the real
            # service compensates, then re-raises.
            if pre_broadcast_rollback is not None:
                pre_broadcast_rollback()
            raise RuntimeError("commit failed")

    dispatcher = ToolDispatcher()
    register_a2ui_tools(dispatcher, _CommitFailsService(), heartbeat_runner=_FakeHeartbeatRunner(store))

    _, is_error = await dispatcher.dispatch(
        "push_surface",
        {"template": "heartbeat_findings", "params": {"findings": [{"message": "disk 91% full"}]}},
    )

    assert is_error, "a failed commit must surface as a tool error"
    assert store.to_list() == [], "a rolled-back push must leave no orphaned finding"


async def test_round3_orphan_is_compensated_even_if_service_skips_rollback() -> None:
    """Belt-and-braces: the tool compensates whatever layer dropped the ball.

    A service that raises after running the hook without calling the rollback
    hook must still not leave an orphan — push_surface's own except path
    restores the snapshot, and double-restore is a no-op.
    """
    store = FindingStore()
    store._startup_suppression_seconds = 0

    class _RaisesWithoutRollback:
        async def push_built(self, built: Any, *, pre_broadcast: Any = None, **kwargs: Any) -> str:
            if pre_broadcast is not None:
                pre_broadcast()
            raise RuntimeError("boom after hook")

    dispatcher = ToolDispatcher()
    register_a2ui_tools(dispatcher, _RaisesWithoutRollback(), heartbeat_runner=_FakeHeartbeatRunner(store))

    _, is_error = await dispatcher.dispatch(
        "push_surface",
        {"template": "heartbeat_findings", "params": {"findings": [{"message": "disk 91% full"}]}},
    )
    assert is_error
    assert store.to_list() == [], "tool-level compensation must remove the orphan"


async def test_round3_compensation_restores_prior_state_not_just_deletes() -> None:
    """Compensation must restore the PRIOR record, not delete a pre-existing one.

    A finding that was already tracked (and resolved) before the failed push
    must be left exactly as it was — deleting it would destroy real lifecycle
    history that predates the push.
    """
    store = FindingStore()
    store._startup_suppression_seconds = 0
    store.ingest(Finding(source="disk", summary="disk 91% full", urgency="normal", check_name="disk-check"))
    fp = store.to_list()[0]["fingerprint"]
    store.resolve(fp)
    before = store.to_list()[0]

    class _CommitFailsService:
        async def push_built(self, built: Any, *, pre_broadcast: Any = None, pre_broadcast_rollback: Any = None, **kwargs: Any) -> str:
            if pre_broadcast is not None:
                pre_broadcast()
            if pre_broadcast_rollback is not None:
                pre_broadcast_rollback()
            raise RuntimeError("commit failed")

    dispatcher = ToolDispatcher()
    register_a2ui_tools(dispatcher, _CommitFailsService(), heartbeat_runner=_FakeHeartbeatRunner(store))

    _, is_error = await dispatcher.dispatch(
        "push_surface",
        {
            "template": "heartbeat_findings",
            "params": {"findings": [{"fingerprint": fp, "message": "disk 91% full", "check": "disk-check"}]},
        },
    )
    assert is_error
    after = store.to_list()[0]
    assert after["state"] == "resolved" == before["state"], "prior state must survive compensation"
    assert after["reopen_count"] == before["reopen_count"], "compensation must undo the reopen bump"
