"""Unit tests for the _LazyProxy component proxy in nous.main."""

import pytest

from nous.main import _lazy_component


class _NoLen:
    """Stand-in for a component (e.g. SessionTimeoutMonitor) that is not sized."""

    def get_stats(self) -> dict:
        return {"ok": True}


def test_truthiness_when_initialized_does_not_call_len():
    """`if proxy` must not fall back to __len__ on a component with no len()."""
    components = {"session_monitor": _NoLen()}
    proxy = _lazy_component(components, "session_monitor")

    # Regression: previously raised TypeError ('object has no len()') because
    # __bool__ was absent and Python fell back to __len__.
    assert bool(proxy) is True
    assert proxy and hasattr(proxy, "get_stats")
    assert proxy.get_stats() == {"ok": True}


def test_truthiness_false_before_initialization():
    """A proxy for an uninitialized component is falsy (does not raise)."""
    components: dict = {"session_monitor": None}
    proxy = _lazy_component(components, "session_monitor")

    assert bool(proxy) is False
    assert not proxy


def test_len_still_forwards_for_sized_components():
    """Explicit len() still forwards to a sized underlying component."""
    components = {"bus": [1, 2, 3]}
    proxy = _lazy_component(components, "bus")

    assert len(proxy) == 3
