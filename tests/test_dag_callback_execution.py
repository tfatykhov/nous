from __future__ import annotations

from nous.dag import orchestrator as orch_mod


def test_subtask_backed_covers_subtask_and_callback():
    """The seven gates that used to hardcode 'subtask' now share one set.

    F087 review found the same fix applied to some sites and not others,
    repeatedly. A single constant makes the set impossible to drift.
    """
    assert orch_mod._SUBTASK_BACKED == frozenset({"subtask", "callback"})


def test_no_bare_subtask_type_comparisons_remain():
    """Guard against a future site re-hardcoding the literal."""
    import inspect

    src = inspect.getsource(orch_mod)
    assert 'node_type == "subtask"' not in src
    assert 'node_type != "subtask"' not in src
