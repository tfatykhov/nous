"""F111 PR B1: the stage-2 classifier and its scheduler (plan §3, §9). A stubbed LLM client; no real model call
except the opt-in live test (``-m live_llm`` with NOUS_LIVE_LLM=1 and an Anthropic key), which CI skips."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nous.brain.commitments import Commitment
from nous.cognitive.promise_classifier import (
    PROMPT_VERSION,
    PURPOSE,
    ClassifierScheduler,
)
from nous.observability.model_calls import MAIN_LOOP_PURPOSES, PURPOSES, ModelCallRecorder

FIXTURE = Path(__file__).parent / "fixtures" / "promise_stage2.yaml"
SENTENCE = "I'll deploy it when CI is green."


def _settings(**over: Any) -> SimpleNamespace:
    base = {
        "promise_gate_classifier_enabled": True,
        "promise_gate_classifier_model": "claude-haiku-5-5",
        "promise_gate_classifier_timeout_seconds": 10,
        "promise_gate_classifier_max_concurrency": 2,
    }
    return SimpleNamespace(**{**base, **over})


def _row(text: str = SENTENCE, *, detected_by: str = "gate", evidence: dict | None = None) -> Commitment:
    now = datetime.now(UTC)
    return Commitment(
        id=uuid.uuid4(),
        short_id=42,
        status="open",
        kind="action",
        what=text,
        promise_text=text,
        due_at=now + timedelta(hours=1),
        grace_seconds=900,
        consent="none",
        source_kind="chat_stream",
        detected_by=detected_by,
        session_id="s1",
        turn_id="t1",
        artifact_kind=None,
        artifact_id=None,
        close_reason=None,
        created_at=now,
        evidence=evidence if evidence is not None else {"due_parsed": False},
    )


class StubClient:
    """An LLM client that answers with a ``classify_promise`` tool call (or sleeps, or raises)."""

    supports_call_context = True

    def __init__(self, answer: dict | None = None, *, delay: float = 0.0, error: Exception | None = None) -> None:
        self.answer = answer if answer is not None else {"verdict": "promise", "reason": "a deferred deploy"}
        self.delay = delay
        self.error = error
        self.payloads: list[dict] = []
        self.contexts: list[Any] = []
        self.active = 0
        self.max_active = 0

    async def call(self, payload: dict, *, call_context: Any = None) -> Any:
        self.payloads.append(payload)
        self.contexts.append(call_context)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.error is not None:
                raise self.error
            return SimpleNamespace(stop_reason="tool_use", content=[{"type": "tool_use", "input": self.answer}])
        finally:
            self.active -= 1


class Store:
    """``set_stage2`` in memory (optionally failing)."""

    def __init__(self, fail: bool = False) -> None:
        self.docs: dict[uuid.UUID, list[dict]] = {}
        self.fail = fail

    async def set_stage2(self, row_id: uuid.UUID, doc: dict) -> bool:
        if self.fail:
            raise RuntimeError("database down")
        self.docs.setdefault(row_id, []).append(doc)
        return True


def _sched(client: StubClient, store: Store | None = None, **over: Any) -> tuple[ClassifierScheduler, Store]:
    store = store or Store()
    return ClassifierScheduler(store, _settings(**over), client), store


# ---- 1. verdicts ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("label", ["promise", "not_promise"])
async def test_each_label_is_stored_with_its_metadata(label):
    sched, store = _sched(StubClient({"verdict": label, "reason": "because"}))
    row = _row()
    doc = await sched.classify_now(row)
    assert store.docs[row.id] == [doc]
    assert doc["verdict"] == label
    assert doc["model"] == "claude-haiku-5-5" and doc["prompt_version"] == PROMPT_VERSION == "s2-v1"
    assert isinstance(doc["latency_ms"], int) and doc["latency_ms"] >= 0
    assert doc["attempts"] == 1 and doc["reason"] == "because"
    datetime.fromisoformat(doc["at"])


@pytest.mark.parametrize("answer", [{"verdict": "maybe", "reason": "x"}, {"verdict": None, "reason": "x"}, {}])
async def test_an_unknown_or_missing_label_is_an_error(answer, caplog):
    sched, store = _sched(StubClient(answer))
    row = _row()
    with caplog.at_level(logging.WARNING):
        doc = await sched.classify_now(row)
    assert doc["verdict"] == "error"
    assert store.docs[row.id][0]["verdict"] == "error"
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


# Codex on 391e7849: a label of the wrong type made `label in VERDICTS` raise TypeError outside the guarded block,
# so no `error` was stored and a watcher retry aborted the run.
WRONG_TYPES = [[], {"x": 1}, 1, None]


@pytest.mark.parametrize("label", WRONG_TYPES, ids=["list", "dict", "int", "none"])
async def test_a_label_of_the_wrong_type_is_an_error_in_the_watcher_retry(label, caplog):
    sched, store = _sched(StubClient({"verdict": label, "reason": "x"}))
    row = _row()
    with caplog.at_level(logging.WARNING):
        doc = await sched.classify_now(row)
    assert doc["verdict"] == "error" and store.docs[row.id][0]["verdict"] == "error"
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


@pytest.mark.parametrize("label", WRONG_TYPES, ids=["list", "dict", "int", "none"])
async def test_a_label_of_the_wrong_type_is_an_error_after_the_turn(label):
    sched, store = _sched(StubClient({"verdict": label, "reason": "x"}))
    row = _row()
    task = sched.submit(row)
    assert task is not None
    doc = await task  # the task does not fail
    assert doc["verdict"] == "error" and store.docs[row.id] == [doc]


async def test_a_second_attempt_counts_two():
    sched, _ = _sched(StubClient({"verdict": "promise", "reason": "r"}))
    doc = await sched.classify_now(_row(evidence={"stage2": {"verdict": "error", "attempts": 1}}))
    assert doc["attempts"] == 2


# ---- 2/3. the model, the helper and the purpose -----------------------------------------------------------------


async def test_the_model_setting_and_the_purpose_reach_the_call():
    client = StubClient()
    sched, _ = _sched(client, promise_gate_classifier_model="claude-sonnet-5-5")
    await sched.classify_now(_row())
    [payload] = client.payloads
    assert payload["model"] == "claude-sonnet-5-5"
    assert payload["tools"][0]["name"] == "classify_promise"
    assert payload["tool_choice"] == {"type": "tool", "name": "classify_promise"}
    assert payload["max_tokens"] == 300 and "temperature" not in payload
    assert client.contexts[0].purpose == PURPOSE == "promise_gate_classifier"


async def test_the_call_goes_through_the_background_helper(monkeypatch):
    import nous.cognitive.promise_classifier as pc

    seen: list[dict] = []

    async def fake_helper(client, model, system_prompt, user_message, tool_name, tool_description, schema, **kw):
        seen.append({"model": model, "tool_name": tool_name, "user_message": user_message, **kw})
        return {"verdict": "not_promise", "reason": "policy"}

    monkeypatch.setattr(pc, "call_background_llm_structured", fake_helper)
    sched, _ = _sched(StubClient())
    doc = await sched.classify_now(_row())
    assert doc["verdict"] == "not_promise"
    assert seen == [
        {
            "model": "claude-haiku-5-5",
            "tool_name": "classify_promise",
            "user_message": SENTENCE,
            "max_tokens": 300,
            "purpose": "promise_gate_classifier",
        }
    ]


def test_the_purpose_is_labelled_and_stays_out_of_the_main_loop_views():
    assert PURPOSE in PURPOSES and PURPOSE not in MAIN_LOOP_PURPOSES
    recorder = ModelCallRecorder(writer=None, database=None, agent_id="a", settings=None)
    assert recorder._label(PURPOSE) == PURPOSE
    assert recorder.stats.unlabelled == 0


# ---- 4. fail-open -----------------------------------------------------------------------------------------------


async def test_a_timeout_stores_error_with_one_warning(caplog):
    sched, store = _sched(StubClient(delay=5), promise_gate_classifier_timeout_seconds=0.05)
    row = _row()
    with caplog.at_level(logging.WARNING):
        doc = await sched.classify_now(row)
    assert doc["verdict"] == "error" and store.docs[row.id][0]["verdict"] == "error"
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


async def test_an_exception_stores_error_with_one_warning(caplog):
    sched, store = _sched(StubClient(error=RuntimeError("HTTP 529 overloaded")))
    row = _row()
    with caplog.at_level(logging.WARNING):
        doc = await sched.classify_now(row)
    assert doc["verdict"] == "error" and store.docs[row.id][0]["verdict"] == "error"
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


async def test_a_failing_stage2_write_is_swallowed_with_a_warning(caplog):
    sched, _ = _sched(StubClient(), Store(fail=True))
    with caplog.at_level(logging.WARNING):
        doc = await sched.classify_now(_row())
    assert doc["verdict"] == "promise"
    assert any("stage 2" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


# ---- 5. the turn never waits ------------------------------------------------------------------------------------


async def test_a_slow_classifier_does_not_hold_the_gate():
    from nous.cognitive.promise_gate import PromiseGate
    from tests.test_promise_gate import FakeStore

    sched, _ = _sched(StubClient(delay=5))
    settings = SimpleNamespace(
        promise_gate_enabled=True,
        promise_gate_mode="shadow",
        promise_gate_default_due_minutes=60,
        promise_gate_grace_seconds=900,
    )
    gate = PromiseGate(FakeStore(), settings, classifier=sched)
    started = time.perf_counter()
    outcome = await gate.run(
        "I'll relaunch the chain at 18:15 UTC unless you object.",
        [],
        path="stream",
        session_id="s1",
        turn_id="t1",
        context_kind="interactive",
    )
    assert time.perf_counter() - started < 1.0
    assert outcome.unbacked == 1 and sched.pending == 1  # handed over, not awaited
    await sched.close(timeout=0.05)
    assert sched.pending == 0


# ---- 6. only the stored sentence --------------------------------------------------------------------------------


async def test_only_the_stored_sentence_is_sent():
    client = StubClient()
    sched, _ = _sched(client)
    row = replace(_row("I'll rerun the eval tonight."), what="something else entirely")
    await sched.classify_now(row)
    [payload] = client.payloads
    assert payload["messages"] == [{"role": "user", "content": [{"type": "text", "text": row.promise_text}]}]
    system = " ".join(block["text"] for block in payload["system"])
    assert "something else entirely" not in system


# ---- 7. off, and commit rows ------------------------------------------------------------------------------------


async def test_off_submits_nothing():
    client = StubClient()
    sched, _ = _sched(client, promise_gate_classifier_enabled=False)
    assert sched.submit(_row()) is None
    assert sched.pending == 0 and client.payloads == []


async def test_a_commit_row_is_never_submitted():
    client = StubClient()
    sched, _ = _sched(client)
    assert sched.submit(_row(detected_by="tool")) is None
    assert sched.pending == 0 and client.payloads == []


# ---- 8. concurrency and dedupe ----------------------------------------------------------------------------------


async def test_at_most_two_calls_run_at_once():
    client = StubClient(delay=0.05)
    sched, store = _sched(client)
    tasks = [sched.submit(_row(f"I'll rerun job number {i} tonight.")) for i in range(6)]
    assert all(t is not None for t in tasks)
    await asyncio.gather(*tasks)
    assert client.max_active == 2 and len(client.payloads) == 6 and len(store.docs) == 6


async def test_the_same_row_twice_is_one_call():
    client = StubClient(delay=0.05)
    sched, _ = _sched(client)
    row = _row()
    first = sched.submit(row)
    assert sched.submit(row) is None
    await first
    assert len(client.payloads) == 1


@pytest.mark.parametrize("verdict", ["promise", "not_promise"])
async def test_a_row_with_a_verdict_is_not_classified_again(verdict):
    client = StubClient()
    sched, _ = _sched(client)
    assert sched.submit(_row(evidence={"stage2": {"verdict": verdict, "attempts": 1}})) is None
    assert client.payloads == []


# ---- 9. drain ---------------------------------------------------------------------------------------------------


async def test_close_drains_then_cancels_and_a_cancelled_call_stores_nothing():
    quick, slow = StubClient(delay=0.01), StubClient(delay=5)
    sched_quick, store_quick = _sched(quick)
    sched_quick.submit(_row())
    await sched_quick.close(timeout=1.0)
    assert len(store_quick.docs) == 1 and sched_quick.pending == 0

    sched_slow, store_slow = _sched(slow)
    task = sched_slow.submit(_row())
    started = time.perf_counter()
    await sched_slow.close(timeout=0.05)
    assert time.perf_counter() - started < 1.0
    assert task.cancelled() and sched_slow.pending == 0 and store_slow.docs == {}
    assert sched_slow.submit(_row()) is None  # closed: nothing new starts


# ---- 10. the reason is cleaned ----------------------------------------------------------------------------------


async def test_a_secret_in_the_reason_is_cleaned_and_cut():
    key = "sk-ant-api03-" + "A" * 80
    sched, store = _sched(StubClient({"verdict": "promise", "reason": f"echoing {key}\x1b[31m " + "x" * 400}))
    row = _row()
    doc = await sched.classify_now(row)
    assert key not in doc["reason"] and "sk-ant-api03-AAAA" not in doc["reason"]
    assert "\x1b" not in doc["reason"] and len(doc["reason"]) <= 200
    assert store.docs[row.id][0]["reason"] == doc["reason"]


# ---- 11. fixtures and the live run ------------------------------------------------------------------------------


def _cases() -> list[dict]:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))["cases"]


def test_every_fixture_sentence_has_a_label_and_a_reason():
    cases = _cases()
    assert len(cases) >= 12
    for case in cases:
        assert case["label"] in ("promise", "not_promise"), case
        assert case["text"].strip() and case["reason"].strip(), case
    texts = {c["text"]: c["label"] for c in cases}
    assert texts["The market will be closed by then."] == "not_promise"  # plan K
    assert texts["The PR will be merged by then."] == "promise"
    c1 = [t for t in texts if t.startswith("Nothing follows up on these yet, so I'll close each row myself")]
    assert len(c1) == 1 and texts[c1[0]] == "not_promise"  # review I2: the sentence prod stored for C-1


@pytest.mark.live_llm
@pytest.mark.skipif(
    os.environ.get("NOUS_LIVE_LLM") != "1"
    or not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")),
    reason="opt-in: NOUS_LIVE_LLM=1 and an Anthropic key",
)
async def test_live_haiku_on_the_fixtures():
    from nous.api.anthropic_client import create_client
    from nous.config import Settings

    settings = Settings()
    client = create_client(settings)
    await client.start()
    try:
        sched, _ = _sched(client)
        results = [(c, await sched.classify_now(_row(c["text"]))) for c in _cases()]
    finally:
        await client.close()
    wrong = [(c["text"], c["label"], d["verdict"]) for c, d in results if d["verdict"] != c["label"]]
    assert len(wrong) <= len(results) // 5, wrong
