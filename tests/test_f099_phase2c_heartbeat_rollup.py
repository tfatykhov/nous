"""F099 Phase 2c-2: the callback's tokens reach the DAG, and a cancelled roll-up is not a failed run."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

from f099_support import ON
from test_f099_phase2c_check_tokens import STAMP, _check, _hb

from nous.heartbeat.dynamic import DynamicCheck


def _callback_check(name: str, *, stamp=STAMP) -> DynamicCheck:
    return DynamicCheck(
        check_id=f"{name}-id",
        name=name,
        prompt="Watch",
        tools=["web_search"],
        interval=300,
        on_complete_prompt="Tell the user what the check found",
        on_complete_tools=["web_search"],
        intention=stamp,
    )


async def test_a_callback_adds_its_tokens_to_its_dag():
    check = _callback_check("lineage_cb")
    hb, loader = _hb(check, 0)
    await hb._execute_callback(check, [])  # the triage double answers with 1 + 1 tokens
    hb.dag_orchestrator.add_check_tokens.assert_awaited_once_with({"dag_node_id": "node-1"}, 2)


async def test_a_callback_adds_nothing_with_continuation_off():  # PIN (prod's flags)
    check = _callback_check("prod_cb")
    hb, loader = _hb(check, 0, flags=ON)
    await hb._execute_callback(check, [])
    loader.check_metadata.assert_not_awaited()
    hb.dag_orchestrator.add_check_tokens.assert_not_awaited()


async def test_a_callback_of_a_check_with_no_lineage_adds_nothing():  # PIN
    check = _callback_check("plain_cb", stamp=None)
    hb, loader = _hb(check, 0)
    await hb._execute_callback(check, [])
    loader.check_metadata.assert_not_awaited()


async def _cancelled_from_within(*args, **kwargs):
    victim = asyncio.get_running_loop().create_future()
    victim.cancel()
    await victim  # a CancelledError nobody sent to this task


async def test_a_cancel_from_within_the_roll_up_leaves_the_run_a_success(caplog):
    check = _check("rolling")
    check.mark_failure = MagicMock()
    hb, loader = _hb(check, 120)
    hb.dag_orchestrator.add_check_tokens = _cancelled_from_within
    ended = []
    real_end_run = hb._registry.end_run

    def spy(name, succeeded, **kwargs):
        ended.append(succeeded)
        return real_end_run(name, succeeded, **kwargs)

    hb._registry.end_run = spy
    with caplog.at_level(logging.ERROR, logger="nous.heartbeat.runner"):
        await hb._tick()
    check.mark_failure.assert_not_called()  # the run succeeded: its stats and findings were written first
    assert ended == [True]  # the registry outcome, as trigger_check records it
    loader.update_run_stats.assert_awaited_once()  # one write, the success; no failure write after it
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == [
        "Heartbeat check 'rolling': the roll-up of its tokens into its DAG was cancelled from within — the run itself "
        "succeeded; the increment may or may not have landed and is not written again"
    ]
