"""Harness Phase 2b: a handler reports what it learned through dispatch()."""

import asyncio

import pytest

from nous.api.call_outcome import CallOutcome, current_outcome
from nous.api.tools import ToolDispatcher

_SCHEMA = {"name": "probe", "description": "d", "input_schema": {"type": "object", "properties": {}}}


def _dispatcher(fail: bool = False):
    d = ToolDispatcher()

    async def probe():
        o = current_outcome()
        if o is not None:
            o.external_ref, o.uncertain = "<m@x>", True
        if fail:
            raise RuntimeError("boom")
        return {"content": [{"type": "text", "text": "ok"}]}

    d.register("probe", probe, _SCHEMA)
    return d


def test_no_dispatch_means_no_outcome():
    assert current_outcome() is None


@pytest.mark.asyncio
async def test_a_handler_reports_through_dispatch():
    outcome = CallOutcome()
    text, is_error = await _dispatcher().dispatch("probe", {}, outcome=outcome)
    assert (text, is_error) == ("ok", False)
    assert (outcome.external_ref, outcome.uncertain) == ("<m@x>", True)
    assert current_outcome() is None


@pytest.mark.asyncio
async def test_a_raising_handler_still_resets_the_channel():
    outcome = CallOutcome()
    _text, is_error = await _dispatcher(fail=True).dispatch("probe", {}, outcome=outcome)
    assert is_error and outcome.external_ref == "<m@x>"
    assert current_outcome() is None


@pytest.mark.asyncio
async def test_dispatch_without_an_outcome_is_unchanged():
    assert await _dispatcher().dispatch("probe", {}) == ("ok", False)


@pytest.mark.asyncio
async def test_dispatch_in_a_task_resumed_elsewhere_does_not_leak_or_crash():
    """The keepalive path runs dispatch in a task and stream_chat is resumed chunk
    by chunk in new tasks (rest.py): nothing may span those boundaries."""
    d = _dispatcher()
    outcome = CallOutcome()

    async def gen():
        task = asyncio.create_task(d.dispatch("probe", {}, outcome=outcome))
        yield "keepalive"
        yield await task

    agen = gen()
    assert await asyncio.create_task(agen.__anext__()) == "keepalive"
    await asyncio.create_task(agen.__anext__())
    assert outcome.external_ref == "<m@x>"
    assert current_outcome() is None
