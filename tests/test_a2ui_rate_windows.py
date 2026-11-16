"""The action limiter keeps a window per caller and a ceiling for the process (``ActionRouter._rate_ok``). No database:
the limiter is called directly, on a clock the test moves."""

from __future__ import annotations

from types import SimpleNamespace

from nous.a2ui.actions import RATE_CEILING_FACTOR, ActionRouter


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _router(limit: int) -> tuple[ActionRouter, _Clock]:
    router = ActionRouter(None, SimpleNamespace(agent_id="rate-test", a2ui_action_rate_per_minute=limit), None)
    clock = _Clock()
    router._clock = clock
    return router, clock


async def _take(router: ActionRouter, caller: str, times: int) -> list[bool]:
    return [await router._rate_ok(caller) for _ in range(times)]


async def test_an_anonymous_flood_leaves_the_owner_and_a_device_their_own_windows() -> None:
    router, _clock = _router(limit=3)
    assert await _take(router, "anonymous", 10) == [True] * 3 + [False] * 7
    assert await _take(router, "owner", 3) == [True] * 3
    assert await _take(router, "device:one", 3) == [True] * 3
    assert await router._rate_ok("owner") is False  # the owner's own window is full now, not the flood's doing


async def test_two_devices_have_separate_windows() -> None:
    router, _clock = _router(limit=2)
    assert await _take(router, "device:one", 3) == [True, True, False]
    assert await _take(router, "device:two", 3) == [True, True, False]


async def test_the_global_ceiling_is_four_times_the_per_caller_limit() -> None:
    assert RATE_CEILING_FACTOR == 4
    router, _clock = _router(limit=2)
    accepted = [ok for n in range(6) for ok in await _take(router, f"device:{n}", 2)]
    assert accepted == [True] * 8 + [False] * 4
    assert len(router._recent) == 8


async def test_a_refused_call_takes_no_slot_and_an_accepted_one_takes_one_in_each_window() -> None:
    router, _clock = _router(limit=1)
    assert await router._rate_ok("owner") is True
    assert await router._rate_ok("owner") is False
    assert len(router._windows["owner"]) == 1
    assert len(router._recent) == 1


async def test_the_window_slides() -> None:
    router, clock = _router(limit=1)
    assert await router._rate_ok("owner") is True
    clock.now += 59.9
    assert await router._rate_ok("owner") is False
    clock.now += 0.2
    assert await router._rate_ok("owner") is True


async def test_idle_windows_are_dropped() -> None:
    router, clock = _router(limit=3)
    await _take(router, "anonymous", 2)
    await _take(router, "device:one", 1)
    clock.now += 61
    assert await router._rate_ok("owner") is True
    assert set(router._windows) == {"owner"}
    assert len(router._recent) == 1


async def test_forget_caller_drops_the_window_and_leaves_the_others() -> None:
    router, _clock = _router(limit=1)
    await router._rate_ok("device:one")
    await router._rate_ok("owner")
    router.forget_caller("device:one")
    router.forget_caller("device:never-seen")
    assert set(router._windows) == {"owner"}
    assert await router._rate_ok("device:one") is True  # a re-paired device starts with an empty window
