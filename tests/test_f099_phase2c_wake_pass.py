"""F099 Phase 2c-2: the reconciler's continuation pass repairs and wakes; it never runs a turn."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from f099_support import CONT, ON, env_factory, finish, intention_of, make_subtask  # noqa: F401

import nous.heart.result_reconciler as reconciler_module
from nous.config import Settings
from nous.heart.result_reconciler import ContinuationWakePass, build_reconciler


def _names(reconciler) -> list[str]:
    return [p.name for p in reconciler._passes]


async def test_the_pass_repairs_then_wakes_the_runner(monkeypatch):
    order = []

    async def repair(*args, **kwargs):
        order.append("repair")
        return 3

    monkeypatch.setattr(reconciler_module, "repair_missing_results", repair)
    settings = Settings(_env_file=None, **CONT)
    wake = MagicMock(side_effect=lambda: order.append("wake"))
    assert await ContinuationWakePass(object(), object(), settings, wake).run(limit=50) == 3
    assert order == ["repair", "wake"]

    order.clear()

    async def repaired_nothing(*args, **kwargs):
        return 0

    monkeypatch.setattr(reconciler_module, "repair_missing_results", repaired_nothing)
    assert await ContinuationWakePass(object(), object(), settings, wake).run(limit=50) == 0
    assert order == []  # nothing repaired: nothing to wake the runner for


@pytest.mark.postgres_only
async def test_the_pass_repairs_a_lost_result_and_wakes(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    st = await make_subtask(env, policy="continue")
    await finish(env, st)  # the worker hook's write was lost
    wake = MagicMock()
    assert await ContinuationWakePass(env.db, env.heart.result_inbox, env.settings, wake).run(limit=50) == 1
    assert (await intention_of(env, "subtask", st.id)).state == "result_ready"
    wake.assert_called_once_with()


async def test_with_continuation_off_the_pass_touches_nothing_and_wakes_nobody():  # PIN
    class NoDatabase:
        def session(self):
            raise AssertionError("the pass touched the database with continuation off")

    wake = MagicMock()
    settings = Settings(_env_file=None, **ON)
    assert await ContinuationWakePass(NoDatabase(), object(), settings, wake).run(limit=50) == 0
    wake.assert_not_called()


async def test_a_failing_repair_does_not_stop_the_other_passes(monkeypatch):
    """Carry-over 6: the reconciler isolates each pass, so repair (which runs in this pass, not the runner's
    sweep: C13) cannot stop the inbox, DAG or intentions passes."""
    from nous.heart.result_reconciler import TerminalSubtaskReconciler

    async def boom(*args, **kwargs):
        raise RuntimeError("repair is down")

    monkeypatch.setattr(reconciler_module, "repair_missing_results", boom)

    class Other:
        name = "other"

        async def run(self, *, limit):
            return 7

    settings = Settings(_env_file=None, **CONT)
    wake = MagicMock()
    reconciler = TerminalSubtaskReconciler([ContinuationWakePass(object(), object(), settings, wake), Other()])
    assert await reconciler.run_once() == {"other": 7}  # the failing pass is logged and left out
    wake.assert_not_called()


def test_the_pass_is_registered_only_with_the_flag_on_and_a_wake():
    on, off = Settings(_env_file=None, **CONT), Settings(_env_file=None, **ON)
    wake = MagicMock()
    assert _names(build_reconciler(MagicMock(), MagicMock(), on, continuation_wake=wake)) == [
        "inbox",
        "dag",
        "intentions",
        "continuation",
    ]
    assert "continuation" not in _names(build_reconciler(MagicMock(), MagicMock(), off, continuation_wake=wake))  # PIN
    assert "continuation" not in _names(build_reconciler(MagicMock(), MagicMock(), on))  # no runner, no pass


def test_the_prod_flag_set_registers_exactly_the_passes_it_did_before():  # PIN
    reconciler = build_reconciler(
        MagicMock(), MagicMock(), Settings(_env_file=None, **ON), continuation_wake=MagicMock()
    )
    assert _names(reconciler) == ["inbox", "dag", "intentions"]
