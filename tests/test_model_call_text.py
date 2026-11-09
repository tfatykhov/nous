"""F108 Phase 1 (recording), PR 3: the model-call text store, migration 102.

A model call's system blocks, messages, tool list and response are stored as blocks addressed by the sha256 of their
redacted body (``nous_system.model_text_block``), and each call lists its blocks in order
(``nous_system.model_call_block_ref``). The row tests write real Postgres under their own agent id and delete their
rows afterwards (a call's refs go with its ``model_call_log`` row: ``ON DELETE CASCADE``). The rest need no database.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import random
import re
import threading
import time
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from nous.api.anthropic_client import StreamEvent
from nous.api.models import ApiResponse
from nous.cognitive import ledger_store
from nous.observability import model_call_text
from nous.observability.model_call_text import TEXT_MAX_PENDING, TextStore, prepare_block, split_payload
from nous.observability.model_calls import CallContext, ModelCallRecorder
from nous.observability.recording import BoundedWriter

# The column names scripts/ops/db_roles.sql refuses to grant the reader unless excluded (its section 5d regex).
CREDENTIAL_NAME = (
    r"(^|_)(token|secret|password|passwd|credential|credentials)(_|$)|(api|private|secret|access|auth)_?key|fcm"
)
# Tokens the ledger's redaction masks (an sk- key needs 20+ characters after the prefix), split so no source line is
# itself a credential shape.
_SECRET_A = "sk-" + "ant-api03-" + "AbCdEfGhIjKlMnOpQrStUv"
_SECRET_B = "sk-" + "ant-api03-" + "ZyXwVuTsRqPoNmLkJiHgFe"
_OAT = "sk-" + "ant-oat01-" + "QwErTyUiOpAsDfGhJkLzXc"
_USAGE = {"input_tokens": 3, "output_tokens": 5}
_SYSTEM = [
    {"type": "text", "text": "You are a careful assistant.", "cache_control": {"type": "ephemeral"}},
    {"type": "text", "text": "## Relevant Facts\n- the sky is blue", "cache_control": {"type": "ephemeral"}},
]
_TOOLS = [
    {"name": "recall_deep", "description": "search memory", "input_schema": {"type": "object"}},
    {
        "name": "bash",
        "description": "run a command",
        "input_schema": {"type": "object"},
        "cache_control": {"type": "ephemeral"},
    },
]


def _agent() -> str:
    return f"test-awv-mct-{uuid.uuid4().hex[:12]}"


def _settings(*, text_on: bool = True, numbers_on: bool = True, max_chars: int = 200_000) -> SimpleNamespace:
    return SimpleNamespace(
        model_call_log_enabled=numbers_on,
        model_call_text_enabled=text_on,
        model_call_text_block_max_chars=max_chars,
    )


def _stack(db, agent: str, **settings) -> tuple[BoundedWriter, TextStore, ModelCallRecorder]:
    writer = BoundedWriter("test")
    store = TextStore(writer, db, agent, _settings(**settings))
    return writer, store, ModelCallRecorder(writer, db, agent, SimpleNamespace(), text_store=store)


def _payload(messages: list[dict], **extra) -> dict:
    return {
        "model": "claude-test-model",
        "max_tokens": 10,
        "system": _SYSTEM,
        "tools": _TOOLS,
        "messages": messages,
        **extra,
    }


def _record(recorder: ModelCallRecorder, payload: dict, session: str, response: list[dict] | None) -> None:
    with recorder.scope(payload, CallContext(purpose="chat", session_id=session), "call") as scope:
        if response is not None:
            scope.finish(ApiResponse(content=response, stop_reason="end_turn", usage=dict(_USAGE)))


def _text_reply(words: str) -> list[dict]:
    return [{"type": "text", "text": words}]


async def _call_id(db, agent: str, session: str) -> uuid.UUID:
    async with db.engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT id FROM nous_system.model_call_log WHERE agent_id = :a AND session_id = :s"),
                {"a": agent, "s": session},
            )
        ).scalar_one()


async def _blocks(db, agent: str) -> dict[str, dict]:
    async with db.engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT * FROM nous_system.model_text_block WHERE agent_id = :a"), {"a": agent})
        ).mappings()
        return {r["hash"]: dict(r) for r in rows}


async def _refs(db, call_id: uuid.UUID) -> list[tuple[str, int, str]]:
    async with db.engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT part, ord, block_hash FROM nous_system.model_call_block_ref WHERE call_id = :c "
                "ORDER BY part, ord"
            ),
            {"c": call_id},
        )
        return [tuple(r) for r in rows.all()]


async def _delete(db, agent: str) -> None:
    async with db.engine.begin() as conn:
        # the refs go with their calls (ON DELETE CASCADE), then nothing references the blocks
        await conn.execute(text("DELETE FROM nous_system.model_call_log WHERE agent_id = :a"), {"a": agent})
        await conn.execute(text("DELETE FROM nous_system.model_text_block WHERE agent_id = :a"), {"a": agent})


# ---------------------------------------------------------------------------------------------------------------------
# Splitting and preparing (pure)
# ---------------------------------------------------------------------------------------------------------------------


def test_split_payload_gives_one_block_per_system_block_message_tools_and_response():
    messages = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": _text_reply("hi")}]
    parts = split_payload(_payload(messages), _text_reply("done"))
    assert [(part, ord_) for part, ord_, _, _ in parts] == [
        ("system", 0),
        ("system", 1),
        ("messages", 0),
        ("messages", 1),
        ("tools", 0),
        ("response", 0),
    ]
    assert parts[0][2] == "You are a careful assistant."  # a text system block is stored as its text
    assert json.loads(parts[2][2]) == messages[0]
    assert json.loads(parts[3][2]) == messages[1]
    assert [t["name"] for t in json.loads(parts[4][2])] == ["recall_deep", "bash"]
    assert json.loads(parts[5][2]) == _text_reply("done")
    # cache_control is a caching marker, not content: never part of a block
    assert not [body for _, _, body, _ in parts if "cache_control" in body]
    # a text system block is plain text; every other block is JSON, redacted as a structure before it is serialised
    assert [is_json for *_, is_json in parts] == [False, False, True, True, True, True]
    # no response, no response block; a string system prompt is one block
    plain = split_payload({"system": "be brief", "messages": messages}, None)
    assert plain[0] == ("system", 0, "be brief", False)
    assert [part for part, *_ in plain] == ["system", "messages", "messages"]


def test_block_over_cap_is_truncated_with_marker():
    raw = "word " * 100  # 500 characters
    digest, body = prepare_block(raw, 100)
    assert body.startswith("word word")
    assert re.search(r"\[truncated \d+ chars\]$", body), body
    assert len(body) <= 100 + len("\n[truncated 400 chars]")
    assert digest == hashlib.sha256(body.encode("utf-8")).hexdigest()
    # under the cap: no marker
    assert "truncated" not in prepare_block("short", 100)[1]


def test_a_secret_cut_by_the_cap_is_not_kept_in_part():
    raw = "a " * 45 + _SECRET_A + " b" * 400  # the secret starts at character 90, the cap cuts it at 100
    _, body = prepare_block(raw, 100)
    assert "sk-ant" not in body and "AbCdEf" not in body, body
    assert body.startswith("a a a") and body.endswith(f"[truncated {len(raw) - 100} chars]")


def test_the_scan_pad_is_dropped_so_a_secret_cut_by_the_window_is_not_kept_in_part():
    """Over the cap only the cap plus a scan pad is redacted: a secret the window's edge cuts matches nothing there,
    and masking the secrets before it shrinks the text, so without dropping the pad its raw start would slide into
    the kept part."""
    pad = ledger_store.redaction_scan_pad()
    masked = (_SECRET_A + " ") * 20  # each mask is shorter than its secret: the redacted window shrinks
    cap = len(masked) + 10
    edge = cap + pad  # where the redacted window ends
    filler = "c " * ((edge - 6 - len(masked)) // 2) + "c" * ((edge - 6 - len(masked)) % 2)
    raw = masked + filler + _SECRET_B + " d" * 200  # the window holds the first 6 characters of the second secret
    assert raw[edge - 6 : edge] == "sk-ant"
    _, body = prepare_block(raw, cap)
    assert "sk-ant" not in body and "ZyXwVu" not in body and "AbCdEf" not in body, body


# A block over the cap at 200,000 characters. Prod's scan pad is 3148 (the longest own secret); dropping it from the
# REDACTED head lost text the block had room for, ~9k raw characters of mask-dense text, and all of a block that is
# one base64 run.
_PROSE = "the quick brown fox jumps over the lazy dog\n"
_HASHES = "".join(hashlib.sha1(str(i).encode()).hexdigest() + "\n" for i in range(8000))  # 328,000 characters


@pytest.fixture(params=[None, 3148], ids=["default_pad", "prod_pad"])
def scan_pad(request, monkeypatch) -> int:
    if request.param is not None:
        monkeypatch.setattr(ledger_store, "redaction_scan_pad", lambda: request.param)
    return ledger_store.redaction_scan_pad()


def _head(body: str) -> str:
    head, marker, _ = body.rpartition("\n[truncated ")
    assert marker, body[-80:]
    return head


@pytest.mark.parametrize("size", [201_000, 260_000])
def test_a_block_a_little_over_the_cap_keeps_the_whole_cap(scan_pad, size):
    raw = (_PROSE * (size // len(_PROSE) + 1))[:size]
    _, body = prepare_block(raw, 200_000)
    assert _head(body) == raw[:200_000]
    assert body.endswith(f"\n[truncated {size - 200_000} chars]")


@pytest.mark.parametrize("size", [205_000, 300_000])
def test_a_mask_dense_block_keeps_all_its_cap_settles(scan_pad, size):
    raw = _HASHES[:size]
    _, body = prepare_block(raw, 200_000)
    head = _head(body)
    # at least the redaction of the first 200,000 raw characters, less the hash the cap splits
    assert len(head) >= len(ledger_store.redact_output(raw[:200_000])) - 40
    assert ledger_store.redact_output(raw).startswith(head)
    assert re.search(r"[0-9a-f]{4}", head) is None


@pytest.mark.parametrize("size", [202_000, 300_000])
def test_a_block_that_is_one_base64_run_keeps_its_prefix_and_mask(scan_pad, size):
    blob = base64.b64encode(random.Random(3).randbytes(size)).decode()[:size]
    _, body = prepare_block("Here is the file:\n" + blob, 200_000)
    assert _head(body) == "Here is the file:\n[REDACTED_BLOB]"


@pytest.mark.parametrize("size", ["within_two_pads", "past_two_pads"])
def test_a_block_never_shows_a_quoted_value_the_base_window_masked(scan_pad, size):
    """Before #874 a block over the cap was redacted over its first max_chars + pad characters. A quoted value whose
    closing quote lies anywhere in that window was masked then, so it must be masked now."""
    cap = 1000
    head = 'password: "'
    for quote_at in range(300, cap + 2 * scan_pad + 400, 97):
        value = head + ("sensitive words " * 1000)[: quote_at - len(head)] + '"; '
        raw = value + "ordinary prose " * ((cap + 2 * scan_pad + (10 if size == "within_two_pads" else 3000)) // 15)
        if size == "within_two_pads":
            raw = raw[: cap + 2 * scan_pad]
        if "sensitive" in ledger_store.redact_output(raw[: cap + scan_pad]):
            continue  # the base left it raw too (its quote was past the base window)
        _, body = prepare_block(raw, cap)
        assert "sensitive" not in body, (quote_at, len(raw), body[:80])


def test_a_periodic_own_secret_in_a_block_leaves_no_fragment(monkeypatch, scan_pad):
    """A self-overlapping secret run on past a whole copy: str.replace left the overhang raw, within the cap or over
    it."""
    password = "s3cr3t! " * 8
    monkeypatch.setenv("NOUS_TEST_FAKE_TOKEN", password)
    ledger_store._own_secrets.cache_clear()
    try:
        group = password * 3 + password[:24] + " "
        for raw in (group, "x " + group * 1000):  # within the 200,000 cap, and 217,002 characters, over it
            _, body = prepare_block(raw, 200_000)
            assert not any(password[i : i + 6] in body for i in range(len(password) - 5)), body[:200]
    finally:
        monkeypatch.delenv("NOUS_TEST_FAKE_TOKEN")
        ledger_store._own_secrets.cache_clear()


def test_base64_attachment_replaced():
    data = base64.b64encode(bytes(range(256)) * 12).decode()  # 3,072 bytes, 4,096 characters
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}
    link = f"see data:image/png;base64,{data} here"
    message = {"role": "user", "content": [image, {"type": "text", "text": link}]}
    small = base64.b64encode(b"tiny").decode()
    small_message = {"role": "user", "content": [{"type": "text", "text": f"data:image/png;base64,{small}"}]}
    parts = split_payload({"messages": [message, small_message]}, None)
    _, body = prepare_block(parts[0][2], 200_000, parts[0][3])
    assert data not in body and data[:200] not in body
    assert body.count("[base64 3072 bytes]") == 2, body
    assert json.loads(body)["content"][0]["source"]["data"] == "[base64 3072 bytes]"  # still JSON
    assert small in prepare_block(parts[1][2], 200_000, parts[1][3])[1]  # 1 KB or less is kept


_NESTED = '{"password":"hunter2"}'  # JSON inside a string: serialised, its quotes are escaped


def _json_secret_call(password: str = "hunter2") -> tuple[dict, list[dict]]:
    """A call whose message text, tool input, tool result and response all hold a password inside a JSON string,
    and whose response's tool input is a structured ``{"password": ...}``."""
    nested = _NESTED.replace("hunter2", password)
    messages = [
        {"role": "user", "content": f"send {nested} to the API"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "bash", "input": {"command": f"curl -d '{nested}' x"}}
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": f"echo {nested}"}]},
    ]
    response = [
        {"type": "text", "text": f"I sent {nested}"},
        {"type": "tool_use", "id": "t2", "name": "http_post", "input": {"password": password}},
    ]
    return _payload(messages), response


def test_json_inside_a_string_is_masked_in_every_block():
    payload, response = _json_secret_call()
    bodies = [prepare_block(text_, 200_000, is_json)[1] for _, _, text_, is_json in split_payload(payload, response)]

    assert not [body for body in bodies if "hunter2" in body], bodies
    # the response's {"password": ...}: the string alone carries no key, the serialised pass masks it
    assert '"password": [REDACTED]' in bodies[-1], bodies[-1]


def test_base64_is_replaced_before_the_strings_are_redacted():
    """A string over the cap is cut in its middle when it is redacted: base64 cut there would no longer be
    recognised, so it is replaced first."""
    data = base64.b64encode(bytes(range(256)) * 12).decode()  # 4,096 characters
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}
    message = {"role": "user", "content": [image, {"type": "text", "text": f"the login is {_NESTED}"}]}
    [(_, _, text_, is_json)] = split_payload({"messages": [message]}, None)

    _, body = prepare_block(text_, 3000, is_json)

    assert "[base64 3072 bytes]" in body and data[:100] not in body and data[-100:] not in body, body
    assert "hunter2" not in body


def test_a_string_over_the_cap_inside_a_block_is_cut_in_its_middle_then_the_block_at_the_cap():
    text_ = "head " + "x" * 5000 + " tail"
    [(_, _, raw, is_json)] = split_payload({"messages": [{"role": "user", "content": text_}]}, None)

    _, body = prepare_block(raw, 1000, is_json)

    # the string is redacted over a head and a tail window around a marker, then the block is cut at the cap
    assert body.startswith('{"content": "head xxx') and "[middle cut]" in body, body
    assert re.search(r"\[truncated \d+ chars\]$", body), body


def test_a_lone_surrogate_is_replaced_so_the_body_is_storable():
    digest, body = prepare_block("bytes \udcff here", 1000)
    assert body == "bytes ? here"
    assert digest == hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert model_call_text._raw_key("bytes \udcff here") == model_call_text._raw_key("bytes \udcfe here")


async def test_a_plain_block_never_lends_its_body_to_a_json_block_with_the_same_text():
    """A text system block can hold exactly the JSON of a message. Plain, its escaped secret is out of the patterns'
    reach; as JSON its strings are redacted first. The LRU keys on the kind too, so the JSON block never gets the
    plain block's body."""
    store = TextStore(BoundedWriter("test"), None, "a", _settings())
    [(_, _, raw, is_json)] = split_payload({"messages": [{"role": "user", "content": _NESTED}]}, None)

    await store._prepare(raw, False)
    _, body = await store._prepare(raw, is_json)

    assert is_json and "hunter2" not in body, body


async def test_two_concurrent_misses_on_one_block_are_counted_once(monkeypatch):
    store = TextStore(BoundedWriter("test"), None, "a", _settings())
    real = model_call_text.prepare_block

    def slow(raw, max_chars, is_json=False):
        time.sleep(0.2)  # both writes are past the LRU lookup before either inserts
        return real(raw, max_chars, is_json)

    monkeypatch.setattr(model_call_text, "prepare_block", slow)
    big = "big " * 6000
    first, second = await asyncio.wait_for(asyncio.gather(store._prepare(big), store._prepare(big)), 10)
    assert first == second and len(store._lru) == 1
    assert store._lru_chars == sum(len(body) for _, body in store._lru.values())


def test_text_requires_numbers_flag(caplog):
    with caplog.at_level(logging.WARNING, logger="nous.observability.model_call_text"):
        store = TextStore(BoundedWriter("test"), None, "a", _settings(text_on=True, numbers_on=False))
    assert not store.enabled
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "NOUS_MODEL_CALL_LOG_ENABLED" in warnings[0].getMessage()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="nous.observability.model_call_text"):
        assert not TextStore(BoundedWriter("test"), None, "a", _settings(text_on=False)).enabled
        assert TextStore(BoundedWriter("test"), None, "a", _settings()).enabled
    assert not caplog.records  # off, or on with the numbers: nothing to warn about


def test_text_writes_go_to_the_writer_with_their_own_cap():
    class _Writer:
        def __init__(self) -> None:
            self.kwargs: list[dict] = []

        def submit(self, make_write, stats=None, **kwargs) -> bool:
            self.kwargs.append(kwargs)
            return True

    writer = _Writer()
    store = TextStore(writer, None, "a", _settings())
    store.submit(uuid.uuid4(), {"messages": []}, None, None)
    assert TEXT_MAX_PENDING == 50
    assert writer.kwargs == [{"kind": "text", "max_pending": 50}]


async def test_a_kind_over_its_cap_is_dropped_without_touching_the_others():
    writer = BoundedWriter("test", concurrency=1, max_pending=3)
    gate = asyncio.Event()
    done: list[str] = []

    def write(name: str):
        async def run() -> None:
            await gate.wait()
            done.append(name)

        return run

    assert writer.submit(write("t1"), kind="text", max_pending=2)
    assert writer.submit(write("t2"), kind="text", max_pending=2)
    assert not writer.submit(write("t3"), kind="text", max_pending=2)  # over the text cap: dropped
    # the text writes pending do not count against the others' cap of 3
    assert writer.submit(write("n1")) and writer.submit(write("n2")) and writer.submit(write("n3"))
    assert not writer.submit(write("n4"))
    assert writer.stats.dropped_backpressure == 2
    gate.set()
    await asyncio.wait_for(writer.drain(5.0), 10)
    assert sorted(done) == ["n1", "n2", "n3", "t1", "t2"]
    assert writer.stats.written == 5
    # once drained, both kinds take writes again
    assert writer.submit(write("t4"), kind="text", max_pending=2) and writer.submit(write("n5"))
    await asyncio.wait_for(writer.drain(5.0), 10)


# ---------------------------------------------------------------------------------------------------------------------
# Rows in Postgres
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_repeated_system_prompt_is_stored_once(db):
    agent = _agent()
    writer, store, recorder = _stack(db, agent)
    head = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": _text_reply("hi there")}]
    try:
        _record(recorder, _payload([*head, {"role": "user", "content": "first question"}]), "c1", _text_reply("one"))
        _record(recorder, _payload([*head, {"role": "user", "content": "second question"}]), "c2", _text_reply("two"))
        await asyncio.wait_for(writer.drain(5.0), 10)
        blocks = await _blocks(db, agent)
        # shared: 2 system blocks, 2 messages and the tool list; new: one last message and one response per call
        assert len(blocks) == 5 + 2 + 2, sorted(b["body"][:30] for b in blocks.values())
        for session, question, answer in (("c1", "first question", "one"), ("c2", "second question", "two")):
            call_id = await _call_id(db, agent, session)
            refs = await _refs(db, call_id)
            assert [(part, ord_) for part, ord_, _ in refs] == [
                ("messages", 0),
                ("messages", 1),
                ("messages", 2),
                ("response", 0),
                ("system", 0),
                ("system", 1),
                ("tools", 0),
            ]
            loaded = await store.load(call_id)
            assert loaded["system"] == ["You are a careful assistant.", "## Relevant Facts\n- the sky is blue"]
            assert [json.loads(m) for m in loaded["messages"]] == [*head, {"role": "user", "content": question}]
            assert json.loads(loaded["response"][0]) == _text_reply(answer)
            assert [t["name"] for t in json.loads(loaded["tools"][0])] == ["recall_deep", "bash"]
        assert {b["size_chars"] for b in blocks.values()} == {len(b["body"]) for b in blocks.values()}
        assert all(h == hashlib.sha256(b["body"].encode("utf-8")).hexdigest() for h, b in blocks.items())
        assert store.stats.written == 2 and store.stats.dropped_error == 0
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_a_secret_inside_a_json_string_never_reaches_a_block(db):
    agent = _agent()
    writer, store, recorder = _stack(db, agent)
    try:
        for session, password in (("c1", "hunter2"), ("c2", "swordfish")):
            payload, response = _json_secret_call(password)
            _record(recorder, payload, session, response)
        await asyncio.wait_for(writer.drain(5.0), 10)
        bodies = " ".join(b["body"] for b in (await _blocks(db, agent)).values())
        assert "hunter2" not in bodies and "swordfish" not in bodies, bodies
        # two calls that differ only in the masked password store the same blocks
        assert await _refs(db, await _call_id(db, agent, "c1")) == await _refs(db, await _call_id(db, agent, "c2"))
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_refs_preserve_order(db):
    agent = _agent()
    writer, store, recorder = _stack(db, agent)
    messages = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"message number {i} of the conversation"}
        for i in range(12)
    ]
    try:
        _record(recorder, _payload(messages), "c1", _text_reply("ok"))
        await asyncio.wait_for(writer.drain(5.0), 10)
        loaded = await store.load(await _call_id(db, agent, "c1"))
        assert [json.loads(m) for m in loaded["messages"]] == messages
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_secret_masked_before_hash(db):
    agent = _agent()
    writer, store, recorder = _stack(db, agent)
    try:
        for session, secret in (("c1", _SECRET_A), ("c2", _SECRET_B)):
            _record(recorder, _payload([{"role": "user", "content": f"my key is {secret} keep it"}]), session, None)
        await asyncio.wait_for(writer.drain(5.0), 10)
        blocks = await _blocks(db, agent)
        first = await _refs(db, await _call_id(db, agent, "c1"))
        second = await _refs(db, await _call_id(db, agent, "c2"))
        assert first == second  # the same message, once masked: ONE block
        assert len(blocks) == 2 + 1 + 1  # the system blocks, the tool list and the one message
        bodies = " ".join(b["body"] for b in blocks.values())
        assert "AbCdEf" not in bodies and "ZyXwVu" not in bodies and "sk-ant" not in bodies
        assert "my key is [REDACTED" in bodies
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_text_disabled_writes_no_blocks(db, monkeypatch):
    agent = _agent()
    writer, store, recorder = _stack(db, agent, text_on=False)
    handed: list[uuid.UUID] = []
    monkeypatch.setattr(store, "submit", lambda call_id, *rest: handed.append(call_id))
    try:
        _record(recorder, _payload([{"role": "user", "content": "hello"}]), "c1", _text_reply("hi"))
        await asyncio.wait_for(writer.drain(5.0), 10)
        call_id = await _call_id(db, agent, "c1")  # the numbers row is written
        assert await _refs(db, call_id) == [] and await _blocks(db, agent) == {}
        assert store.stats.written == 0 and recorder.stats.written == 1
        assert handed == []  # off: the recorder hands the store nothing, so no scope keeps text
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_text_on_without_numbers_writes_no_text(db, caplog):
    agent = _agent()
    with caplog.at_level(logging.WARNING, logger="nous.observability.model_call_text"):
        writer, store, recorder = _stack(db, agent, numbers_on=False)
    try:
        _record(recorder, _payload([{"role": "user", "content": "hello"}]), "c1", _text_reply("hi"))
        await asyncio.wait_for(writer.drain(5.0), 10)
        assert await _blocks(db, agent) == {}
        assert store.stats.written == 0
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_text_write_failure_keeps_the_numbers_row(db, monkeypatch, caplog):
    agent = _agent()
    writer, store, recorder = _stack(db, agent)
    monkeypatch.setattr(
        model_call_text,
        "_UPSERT_BLOCK",
        text("INSERT INTO nous_system.model_text_block_missing (agent_id) VALUES (:agent_id)"),
    )
    try:
        with caplog.at_level(logging.WARNING):
            _record(recorder, _payload([{"role": "user", "content": "hello"}]), "c1", _text_reply("hi"))
            await asyncio.wait_for(writer.drain(5.0), 10)
        call_id = await _call_id(db, agent, "c1")  # the numbers row stands
        assert await _refs(db, call_id) == [] and await _blocks(db, agent) == {}
        assert recorder.stats.written == 1 and recorder.stats.dropped_error == 0
        assert store.stats.dropped_error == 1 and store.stats.written == 0
        assert writer.stats.dropped_error == 1 and writer.stats.written == 1
        assert any(r.levelno == logging.WARNING and "write failed" in r.getMessage() for r in caplog.records)
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_oat_headers_are_not_in_the_payload_blocks(db, monkeypatch):
    agent = _agent()
    writer, store, recorder = _stack(db, agent)
    payload = _payload(
        [{"role": "user", "content": "hello"}],
        thinking={"type": "enabled", "budget_tokens": 1024},
        metadata={"user_id": "owner-42"},
        extra_headers={"Authorization": f"Bearer {_OAT}", "anthropic-beta": "oauth-2025-04-20"},
        stream=True,
    )
    handed: list[set[str]] = []
    real_submit = store.submit

    def submit(call_id, sent, *rest):
        handed.append(set(sent))
        real_submit(call_id, sent, *rest)

    monkeypatch.setattr(store, "submit", submit)
    try:
        _record(recorder, payload, "c1", _text_reply("hi"))
        await asyncio.wait_for(writer.drain(5.0), 10)
        assert handed == [{"system", "messages", "tools"}]  # the scope keeps no other key, not even until the write
        call_id = await _call_id(db, agent, "c1")
        assert {part for part, _, _ in await _refs(db, call_id)} == {"system", "messages", "tools", "response"}
        bodies = " ".join(b["body"] for b in (await _blocks(db, agent)).values())
        for leaked in (
            "Bearer",
            "oat01",
            "QwErTy",
            "anthropic-beta",
            "owner-42",
            "budget_tokens",
            "max_tokens",
            "claude-test-model",
            "Authorization",
        ):
            assert leaked not in bodies, leaked
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_tool_result_block_keeps_hash_across_calls(db):
    """Shaped like the runner's payloads: the last user message carries cache_control (a string content is sent as one
    text block), and in the next call the same message no longer does."""
    agent = _agent()
    writer, store, recorder = _stack(db, agent)
    ask = {"role": "user", "content": "list the files"}
    use = {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"c": "ls"}}],
    }
    result = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "a.txt b.txt"}]}
    cached_ask = {
        "role": "user",
        "content": [{"type": "text", "text": "list the files", "cache_control": {"type": "ephemeral"}}],
    }
    cached_result = {"role": "user", "content": [{**result["content"][0], "cache_control": {"type": "ephemeral"}}]}
    try:
        _record(recorder, _payload([cached_ask]), "c1", use["content"])
        _record(recorder, _payload([ask, use, cached_result]), "c2", _text_reply("two files"))
        _record(
            recorder,
            _payload(
                [
                    ask,
                    use,
                    result,
                    {"role": "assistant", "content": _text_reply("two files")},
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "thanks", "cache_control": {"type": "ephemeral"}}],
                    },
                ]
            ),
            "c3",
            _text_reply("welcome"),
        )
        await asyncio.wait_for(writer.drain(5.0), 10)
        refs = {
            s: [h for part, _, h in await _refs(db, await _call_id(db, agent, s)) if part == "messages"]
            for s in ("c1", "c2", "c3")
        }
        assert refs["c1"][0] == refs["c2"][0] == refs["c3"][0]  # the question, cached or not
        assert refs["c2"][1:3] == refs["c3"][1:3]  # the tool call and its result, cached or not
        loaded = await store.load(await _call_id(db, agent, "c2"))
        assert json.loads(loaded["messages"][2]) == result
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_stored_messages_are_the_ones_sent(db):
    agent = _agent()
    writer, store, recorder = _stack(db, agent)
    messages = [{"role": "user", "content": "what is in the folder"}]
    try:
        _record(recorder, _payload(messages), "c1", _text_reply("let me look"))
        # the runner appends the response and the tool results to the same list right after the call returns
        messages.append({"role": "assistant", "content": _text_reply("let me look")})
        messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "a"}]})
        await asyncio.wait_for(writer.drain(5.0), 10)
        loaded = await store.load(await _call_id(db, agent, "c1"))
        assert [json.loads(m) for m in loaded["messages"]] == [{"role": "user", "content": "what is in the folder"}]
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_stored_tool_result_is_the_one_sent_when_compaction_clears_it_after(db):
    """The runner's payload holds the runner's own message dicts, and compaction (``prune_tool_results``) rewrites an
    aged tool result's content in place right after the call, before the text write runs."""
    agent = _agent()
    writer, store, recorder = _stack(db, agent)
    result = {"type": "tool_result", "tool_use_id": "toolu_1", "content": "a.txt b.txt c.txt"}
    messages = [
        {"role": "user", "content": "list the files"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {}}]},
        {"role": "user", "content": [result]},
    ]
    payload = _payload(messages)
    try:
        _record(recorder, payload, "c1", _text_reply("three files"))
        payload["messages"][2]["content"][0]["content"] = "[cleared]"  # in place, before the write runs
        payload["messages"][2]["content"].append({"type": "text", "text": "added after"})
        payload["messages"][1]["role"] = "changed"
        await asyncio.wait_for(writer.drain(5.0), 10)
        loaded = await store.load(await _call_id(db, agent, "c1"))
        stored = [json.loads(m) for m in loaded["messages"]]
        assert stored[2] == {"role": "user", "content": [{**result, "content": "a.txt b.txt c.txt"}]}
        assert stored[1]["role"] == "assistant"
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_a_lone_surrogate_never_drops_the_text(db):
    """Bash output decoded with ``surrogateescape`` can carry a lone surrogate, which UTF-8 cannot encode: the block is
    stored with a replacement character instead of failing the whole call's text."""
    agent = _agent()
    writer, store, recorder = _stack(db, agent)
    try:
        _record(recorder, _payload([{"role": "user", "content": "bytes \udcff here"}]), "c1", _text_reply("ok"))
        await asyncio.wait_for(writer.drain(5.0), 10)
        loaded = await store.load(await _call_id(db, agent, "c1"))
        assert json.loads(loaded["messages"][0])["content"] == "bytes ? here"
        assert store.stats.written == 1 and store.stats.dropped_error == 0
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_store_touches_existing_block_created_at(db):
    agent = _agent()
    writer, store, recorder = _stack(db, agent)
    try:
        _record(recorder, _payload([{"role": "user", "content": "hello"}]), "c1", _text_reply("hi"))
        await asyncio.wait_for(writer.drain(5.0), 10)
        async with db.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE nous_system.model_text_block SET created_at = now() - interval '3 days' WHERE agent_id = :a"
                ),
                {"a": agent},
            )
        _record(recorder, _payload([{"role": "user", "content": "hello"}]), "c2", _text_reply("hi again"))
        await asyncio.wait_for(writer.drain(5.0), 10)
        async with db.engine.connect() as conn:
            stale = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM nous_system.model_text_block WHERE agent_id = :a "
                        "AND created_at < now() - interval '1 day'"
                    ),
                    {"a": agent},
                )
            ).scalar_one()
        assert stale == 1  # only the first response, which the second call did not send
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_stream_response_is_stored(db):
    agent = _agent()
    writer, store, recorder = _stack(db, agent)
    events = [
        StreamEvent(type="message_start", usage={"input_tokens": 3}),
        StreamEvent(type="text_block_start", block_index=0),
        StreamEvent(type="text_delta", text="Let me "),
        StreamEvent(type="text_delta", text="check."),
        StreamEvent(type="block_stop", block_index=0),
        StreamEvent(type="tool_start", tool_name="bash", tool_id="toolu_9", block_index=1),
        StreamEvent(type="tool_input_delta", text='{"command": ', block_index=1),
        StreamEvent(type="tool_input_delta", text='"ls"}', block_index=1),
        StreamEvent(type="block_stop", block_index=1),
        StreamEvent(type="done", stop_reason="tool_use", usage={"output_tokens": 9}),
        StreamEvent(type="message_stop"),
    ]
    try:
        with recorder.scope(
            _payload([{"role": "user", "content": "hi"}]), CallContext(purpose="chat", session_id="c1"), "stream"
        ) as scope:
            for event in events:
                scope.observe_stream_event(event)
        await asyncio.wait_for(writer.drain(5.0), 10)
        loaded = await store.load(await _call_id(db, agent, "c1"))
        assert json.loads(loaded["response"][0]) == [
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": "toolu_9", "name": "bash", "input": {"command": "ls"}},
        ]
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_exit_does_no_redaction_or_serialisation(db, monkeypatch):
    agent = _agent()
    writer, store, recorder = _stack(db, agent)
    calls: list[str] = []

    def spy(name, real):
        def wrapper(*args, **kwargs):
            calls.append(name)
            return real(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(ledger_store, "redact_output", spy("redact_output", ledger_store.redact_output))
    monkeypatch.setattr(model_call_text, "split_payload", spy("split_payload", model_call_text.split_payload))
    monkeypatch.setattr(model_call_text, "prepare_block", spy("prepare_block", model_call_text.prepare_block))
    try:
        _record(recorder, _payload([{"role": "user", "content": "hello " * 5000}]), "c1", _text_reply("hi"))
        assert calls == []  # the scope's exit only took references
        await asyncio.wait_for(writer.drain(5.0), 10)
        assert {"split_payload", "prepare_block", "redact_output"} <= set(calls)
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_a_big_block_is_prepared_in_a_thread(db, monkeypatch):
    """A block over ``BIG_BLOCK_CHARS`` is hashed and redacted in a worker thread, the small ones on the loop. This
    proves the thread, not a free loop: the regex work holds the GIL, so the thread shares the CPU with the loop."""
    agent = _agent()
    writer, store, recorder = _stack(db, agent)
    threads: dict[int, bool] = {}
    real = model_call_text.prepare_block

    def spy(raw, max_chars, is_json=False):
        threads[len(raw)] = threading.current_thread() is threading.main_thread()
        return real(raw, max_chars, is_json)

    monkeypatch.setattr(model_call_text, "prepare_block", spy)
    try:
        _record(recorder, _payload([{"role": "user", "content": "big " * 6000}]), "c1", _text_reply("hi"))
        await asyncio.wait_for(writer.drain(5.0), 10)
        on_loop = {size > model_call_text.BIG_BLOCK_CHARS: flag for size, flag in threads.items()}
        assert on_loop == {True: False, False: True}, threads  # the big block in a thread, the small ones not
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_a_repeated_block_is_redacted_once(db, monkeypatch):
    agent = _agent()
    writer, store, recorder = _stack(db, agent)
    redacted: list[str] = []
    real = ledger_store.redact_output

    def spy(raw):
        redacted.append(raw)
        return real(raw)

    monkeypatch.setattr(ledger_store, "redact_output", spy)
    try:
        _record(recorder, _payload([{"role": "user", "content": "hello"}]), "c1", _text_reply("one"))
        await asyncio.wait_for(writer.drain(5.0), 10)
        first = len(redacted)
        assert first == 5  # 2 system blocks, the message, the tool list, the response
        _record(recorder, _payload([{"role": "user", "content": "hello"}]), "c2", _text_reply("two"))
        await asyncio.wait_for(writer.drain(5.0), 10)
        assert len(redacted) - first == 1  # only the new response
        assert len(await _refs(db, await _call_id(db, agent, "c2"))) == 5
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_text_tables_indexes_and_no_credential_named_column(db):
    async with db.engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT tablename, indexdef FROM pg_indexes WHERE schemaname = 'nous_system' "
                    "AND tablename IN ('model_text_block', 'model_call_block_ref')"
                )
            )
        ).all()
        names = (
            (
                await conn.execute(
                    text(
                        "SELECT table_name || '.' || column_name FROM information_schema.columns "
                        "WHERE table_schema = 'nous_system' AND table_name IN ('model_text_block', "
                        "'model_call_block_ref')"
                    )
                )
            )
            .scalars()
            .all()
        )
    indexes: dict[str, set[str]] = {}
    for table, definition in rows:
        indexes.setdefault(table, set()).add(definition.split(" USING btree ", 1)[1])
    assert indexes == {
        "model_text_block": {"(agent_id, hash)"},
        "model_call_block_ref": {"(call_id, part, ord)", "(agent_id, created_at)", "(agent_id, block_hash)"},
    }, indexes
    # db_roles.sql section 5d refuses to grant a credential-looking column (these are not reader tables either)
    assert len(names) == 11 and not [n for n in names if re.search(CREDENTIAL_NAME, n.split(".", 1)[1])], names
