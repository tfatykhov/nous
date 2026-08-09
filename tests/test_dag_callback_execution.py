from __future__ import annotations

from nous.dag import orchestrator as orch_mod


def test_subtask_backed_covers_subtask_and_callback():
    """The seven gates that used to hardcode 'subtask' now share one set.

    F087 review found the same fix applied to some sites and not others,
    repeatedly. A single constant makes the set impossible to drift.
    """
    assert orch_mod._SUBTASK_BACKED == frozenset({"subtask", "callback"})


def test_no_bare_subtask_type_comparisons_remain():
    """Guard against a future site re-hardcoding the literal.

    Six of the seven original gates now share _SUBTASK_BACKED. The seventh —
    the F064.2 concurrency-cap gate in _dispatch_ready_nodes — legitimately
    keeps the bare literal: it runs at dispatch time, before a subtask_id
    exists, so folding callbacks into _SUBTASK_BACKED there is a real
    behaviour change (cap-gating callbacks that would otherwise complete
    instantly), not the no-op it is everywhere else. See the comment at that
    site. This pins the count at exactly one and confirms it's that survivor,
    so a future site can't re-hardcode the literal unnoticed.
    """
    import inspect

    src = inspect.getsource(orch_mod)
    total = src.count('node_type == "subtask"') + src.count('node_type != "subtask"')
    assert total == 1, (
        "expected exactly one bare node_type/'subtask' comparison (the "
        f"deliberate _dispatch_ready_nodes carve-out), found {total}"
    )

    dispatch_src = inspect.getsource(orch_mod.DAGOrchestrator._dispatch_ready_nodes)
    assert 'node_type != "subtask"' in dispatch_src
