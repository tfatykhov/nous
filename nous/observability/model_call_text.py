"""F108: the model-call text — what a model call sent and got back, kept as blocks addressed by their hash.

A call's system blocks, its messages, its tool list and its response are each one block. A block's body is redacted
(a JSON block's strings first, then its serialised text: ``prepare_block``), its base64 attachments replaced by a
marker, and cut at ``NOUS_MODEL_CALL_TEXT_BLOCK_MAX_CHARS``; it is stored once per
agent under the sha256 of that body (``nous_system.model_text_block``), so a block that repeats (the system prompt,
the tool list, the earlier messages of a session) is stored once. Each call lists its blocks in order
(``nous_system.model_call_block_ref``). Nothing outside the system blocks, the messages, the tools and the response is
ever stored: no header, no auth, no other payload key.

The text write is chained from the call's numbers write (``ModelCallRecorder``): it is submitted once the
``model_call_log`` row is committed, as a write of its own through the same ``BoundedWriter``, with its own pending
cap (``TEXT_MAX_PENDING``), so a text failure never touches the numbers row. All the work happens in that write:
the scope's exit only copies containers (lists and dicts, never a string). A block over ``BIG_BLOCK_CHARS`` is hashed
and redacted in a thread, but CPython's regex engine holds the GIL, so that thread shares the CPU with the event loop
rather than freeing it: redacting a big block still stalls the loop for about as long. What keeps the steady state
cheap is the LRU, keyed by the sha256 of the raw text: a block sent again (the earlier messages, the system prompt,
the tools) is not redacted again, so each call redacts only its new blocks. Freeing the loop for regex work would
take a process pool or a cheaper redactor (a follow-up).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from collections import OrderedDict
from typing import Any

from sqlalchemy import text

from nous.observability.recording import BoundedWriter, RecorderStats

logger = logging.getLogger(__name__)

# At most this many text writes pending in the shared writer; past it a new one is dropped and counted.
TEXT_MAX_PENDING = 50
# A block longer than this is hashed and redacted in a thread. The regex work holds the GIL, so the thread shares the
# CPU with the event loop rather than freeing it (see the module docstring).
BIG_BLOCK_CHARS = 20_000
# The recently prepared blocks kept, keyed by the sha256 of their raw text: at most this many, and at most this many
# characters of body in all (a long session re-sends all its earlier messages on every call).
LRU_MAX_BLOCKS = 1024
LRU_MAX_CHARS = 4_000_000
# The stream events that carry the response's content (``response_from_events``).
CONTENT_EVENTS = frozenset(
    {
        "text_block_start",
        "text_delta",
        "tool_start",
        "tool_input_delta",
        "thinking_start",
        "thinking_delta",
        "redacted_thinking",
    }
)

# A base64 attachment over 1 KB: an image or document block's ``source.data`` in a block's JSON, or a data URL.
_BASE64_FIELD = re.compile(r'("data"\s*:\s*")([A-Za-z0-9+/]{1025,}={0,2})(")')
_BASE64_VALUE = re.compile(r"[A-Za-z0-9+/]{1025,}={0,2}")
_DATA_URL = re.compile(r"(data:[\w.+-]+/[\w.+-]+;base64,)([A-Za-z0-9+/]{1025,}={0,2})")
# How a character UTF-8 cannot encode (a lone surrogate) is handled, by the body and by the LRU key alike.
_UNENCODABLE = "replace"

# The touch on conflict locks the block's row and makes it "used now", so the sweep's DELETE of an unreferenced old
# block waits for this transaction and then no longer matches it: a ref is never inserted for a just-deleted block.
_UPSERT_BLOCK = text(
    "INSERT INTO nous_system.model_text_block (agent_id, hash, body, size_chars) "
    "VALUES (:agent_id, :hash, :body, :size_chars) "
    "ON CONFLICT (agent_id, hash) DO UPDATE SET created_at = now()"
)
_INSERT_REF = text(
    "INSERT INTO nous_system.model_call_block_ref (call_id, agent_id, part, ord, block_hash) "
    "VALUES (:call_id, :agent_id, :part, :ord, :block_hash)"
)
_LOAD = text(
    "SELECT r.part, b.body FROM nous_system.model_call_block_ref r "
    "JOIN nous_system.model_text_block b ON b.agent_id = r.agent_id AND b.hash = r.block_hash "
    "WHERE r.call_id = :call_id AND r.agent_id = :agent_id ORDER BY r.part, r.ord"
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _without_cache_control(block: Any) -> Any:
    """``block`` without its ``cache_control`` key: a caching marker, not content, and the runner moves it from call to
    call, so a block that keeps it would hash differently in the next call."""
    if isinstance(block, dict) and "cache_control" in block:
        return {k: v for k, v in block.items() if k != "cache_control"}
    return block


def _message(message: Any) -> Any:
    """A message as stored: its content blocks without ``cache_control``, and a user message whose content is one
    plain text block as that text (the runner sends the last user message's string content as one text block, to mark
    it for the cache; in the next call the same message goes as the string)."""
    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
        return message
    content = [_without_cache_control(block) for block in message["content"]]
    only = content[0] if len(content) == 1 and isinstance(content[0], dict) else {}
    if message.get("role") == "user" and only.keys() == {"type", "text"} and only["type"] == "text":
        if isinstance(only["text"], str):
            return {**message, "content": only["text"]}
    return {**message, "content": content}


def split_payload(payload: dict[str, Any], response_content: list[dict] | None) -> list[tuple[str, int, str, bool]]:
    """The blocks of one call as ``(part, ord, text, is_json)``, in order: one per system block (a text block as its
    text), one per message (JSON), one for the tool list (JSON) and one for the response's content (JSON) when there
    is one. ``is_json`` is False only for a text system block. Only ``system``, ``messages`` and ``tools`` are read
    from ``payload``."""
    parts: list[tuple[str, int, str, bool]] = []
    system = payload.get("system")
    if isinstance(system, str):
        if system:
            parts.append(("system", 0, system, False))
    elif isinstance(system, list):
        for i, block in enumerate(system):
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(("system", i, block["text"], False))
            else:
                parts.append(("system", i, _json(_without_cache_control(block)), True))
    for i, message in enumerate(payload.get("messages") or []):
        parts.append(("messages", i, _json(_message(message)), True))
    tools = payload.get("tools")
    if tools:
        parts.append(("tools", 0, _json([_without_cache_control(tool) for tool in tools]), True))
    if response_content is not None:
        parts.append(("response", 0, _json(response_content), True))
    return parts


def _base64_marker(data: str) -> str:
    return f"[base64 {len(data.rstrip('=')) * 3 // 4} bytes]"


def _without_base64(value: Any) -> Any:
    """``value`` (a parsed JSON block) with its base64 attachments over 1 KB replaced by the marker: a ``data`` value
    that is all base64 (an image or document ``source.data``), and every data URL in a string."""
    if isinstance(value, str):
        return _DATA_URL.sub(lambda m: m.group(1) + _base64_marker(m.group(2)), value)
    if isinstance(value, dict):
        return {
            k: _base64_marker(v)
            if k == "data" and isinstance(v, str) and _BASE64_VALUE.fullmatch(v)
            else _without_base64(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_without_base64(v) for v in value]
    return value


def prepare_block(raw: str, max_chars: int, is_json: bool = False) -> tuple[str, str]:
    """``(hash, body)`` of one block: base64 attachments over 1 KB replaced by ``[base64 N bytes]``, redacted
    (``redact_output``), cut at ``max_chars`` with a ``[truncated N chars]`` marker (N: the characters past the cap),
    and the sha256 hex of that final body. Over the cap only the head and a scan pad are redacted, and the pad is
    dropped after, so a secret the cut splits is not kept in part (as ``detail_text`` does). A lone surrogate (bash
    output decoded with ``surrogateescape``), which UTF-8 and Postgres cannot hold, becomes ``?`` first, so the body
    is always storable.

    A JSON block (``is_json``) is parsed back into a structure of its own (nothing the runner holds is read here,
    which may be a thread), its base64 replaced, and every string in it redacted and bounded by ``max_chars``
    (``redact_structure``: a string over the cap keeps its head and tail around a middle-cut marker) BEFORE it is
    serialised again: in the JSON text, a string that holds JSON has its quotes escaped and the patterns cannot see
    its secrets. The serialised text is then redacted as a plain block is, which masks a credential-named key of the
    block itself."""
    # Imported here: nous.cognitive imports nous.observability, whose package imports the model-call recorder.
    from nous.cognitive.ledger_store import redact_output, redact_structure, redaction_scan_pad

    raw = raw.encode("utf-8", _UNENCODABLE).decode("utf-8")
    if is_json:
        clean = redact_structure(_without_base64(json.loads(raw)), max_chars)
        raw = _json(clean).encode("utf-8", _UNENCODABLE).decode("utf-8")
    raw = _BASE64_FIELD.sub(lambda m: m.group(1) + _base64_marker(m.group(2)) + m.group(3), raw)
    raw = _DATA_URL.sub(lambda m: m.group(1) + _base64_marker(m.group(2)), raw)
    if len(raw) <= max_chars:
        body = redact_output(raw)
    else:
        pad = redaction_scan_pad()
        head = redact_output(raw[: max_chars + pad])
        body = f"{head[: max(0, min(max_chars, len(head) - pad))]}\n[truncated {len(raw) - max_chars} chars]"
    return hashlib.sha256(body.encode("utf-8")).hexdigest(), body


def response_from_events(events: list[Any]) -> list[dict[str, Any]]:
    """A streamed response's content blocks, rebuilt from its content events (``CONTENT_EVENTS``): text, tool calls
    (the input parsed, or kept as its raw text when it is not JSON) and thinking."""
    blocks: dict[int, dict[str, Any]] = {}
    pieces: dict[int, list[str]] = {}
    last_text: int | None = None  # a text delta carries no index: it belongs to the text block opened last
    for event in events:
        kind, index = event.type, event.block_index
        if kind == "text_block_start":
            blocks[index], pieces[index], last_text = {"type": "text"}, [], index
        elif kind == "tool_start":
            blocks[index], pieces[index] = {"type": "tool_use", "id": event.tool_id, "name": event.tool_name}, []
        elif kind == "thinking_start":
            blocks[index], pieces[index] = {"type": "thinking"}, []
        elif kind == "redacted_thinking":
            blocks[index] = {"type": "redacted_thinking", "data": event.text}
        elif kind == "text_delta":
            if last_text is not None:
                pieces[last_text].append(event.text)
        elif kind in ("tool_input_delta", "thinking_delta") and index in pieces:
            pieces[index].append(event.text)
    for index, block in blocks.items():
        joined = "".join(pieces.get(index, []))
        if block["type"] == "text":
            block["text"] = joined
        elif block["type"] == "thinking":
            block["thinking"] = joined
        elif block["type"] == "tool_use":
            try:
                block["input"] = json.loads(joined) if joined else {}
            except json.JSONDecodeError:
                block["input"] = joined
    return [blocks[index] for index in sorted(blocks)]


class TextStore:
    """Writes and reads the model-call text blocks through the shared ``BoundedWriter`` (built in main.py).

    Enabled when ``NOUS_MODEL_CALL_TEXT_ENABLED`` and ``NOUS_MODEL_CALL_LOG_ENABLED`` are both on: the text hangs off
    the numbers row, so with the numbers off it logs one WARNING when built and writes nothing."""

    def __init__(self, writer: BoundedWriter, database: Any, agent_id: str, settings: Any) -> None:
        self._writer = writer
        self._database = database
        self._agent_id = agent_id
        self._max_chars = settings.model_call_text_block_max_chars
        self._stats = RecorderStats()
        self._lru: OrderedDict[tuple[bool, bytes], tuple[str, str]] = OrderedDict()
        self._lru_chars = 0
        self.enabled = bool(settings.model_call_text_enabled and settings.model_call_log_enabled)
        if settings.model_call_text_enabled and not settings.model_call_log_enabled:
            logger.warning(
                "F108: NOUS_MODEL_CALL_TEXT_ENABLED is on but NOUS_MODEL_CALL_LOG_ENABLED is off: the text hangs off "
                "the model-call row, so no model-call text is recorded"
            )

    @property
    def stats(self) -> RecorderStats:
        """The text writes' own counters (the shared writer's ``stats`` are the totals of every recorder)."""
        return self._stats

    def submit(
        self,
        call_id: uuid.UUID,
        sent: dict[str, Any],
        response_content: list[dict] | None,
        stream_events: list[Any] | None,
    ) -> None:
        """Schedule the text of call ``call_id`` (its row is committed): ``sent`` holds the call's ``system``,
        ``messages`` and ``tools`` as sent; the response is ``response_content`` when the call returned one, else it is
        rebuilt from ``stream_events``. Everything is serialised, redacted and written in the writer's task. Never
        raises."""
        if not self.enabled:
            return

        async def write() -> None:
            response = response_content
            if response is None and stream_events:
                response = response_from_events(stream_events)
            await self.store(call_id, split_payload(sent, response))

        try:
            self._writer.submit(write, stats=self._stats, kind="text", max_pending=TEXT_MAX_PENDING)
        except Exception as exc:
            self._stats.dropped_error += 1
            logger.warning("F108: model-call text not written (%s)", type(exc).__name__)

    async def store(self, call_id: uuid.UUID, parts: list[tuple[str, int, str, bool]]) -> None:
        """Store ``parts`` (``split_payload``) as the blocks of call ``call_id``: upsert each block (a block already
        stored is touched, so it counts as used now), then insert the call's refs, in one transaction."""
        refs: list[dict[str, Any]] = []
        blocks: dict[str, str] = {}
        for part, ord_, raw, is_json in parts:
            digest, body = await self._prepare(raw, is_json)
            blocks[digest] = body
            refs.append(
                {"call_id": call_id, "agent_id": self._agent_id, "part": part, "ord": ord_, "block_hash": digest}
            )
        if not refs:
            return
        # One row per hash (a call may send the same block twice), in hash order: concurrent stores that share blocks
        # lock their rows in the same order, so they wait for each other and never deadlock.
        rows = [
            {"agent_id": self._agent_id, "hash": digest, "body": body, "size_chars": len(body)}
            for digest, body in sorted(blocks.items())
        ]
        async with self._database.session() as session:
            await session.execute(_UPSERT_BLOCK, rows)
            await session.execute(_INSERT_REF, refs)
            await session.commit()

    async def load(self, call_id: uuid.UUID) -> dict[str, list[str]]:
        """The text of call ``call_id``: each part's block bodies, in order (a part the call did not have is absent)."""
        async with self._database.session() as session:
            rows = (await session.execute(_LOAD, {"call_id": call_id, "agent_id": self._agent_id})).all()
        loaded: dict[str, list[str]] = {}
        for part, body in rows:
            loaded.setdefault(part, []).append(body)
        return loaded

    async def _prepare(self, raw: str, is_json: bool = False) -> tuple[str, str]:
        """``prepare_block`` of ``raw``, from the LRU when the same raw text (of the same kind, plain or JSON) was
        prepared recently. A block over
        ``BIG_BLOCK_CHARS`` is hashed and prepared in a thread, which shares the CPU with the loop rather than freeing
        it (the regex work holds the GIL); the LRU is what spares a block sent again. The LRU is only touched here, on
        the event loop. Two writes may miss the same block at once: the second insert replaces the first and is
        counted once."""
        big = len(raw) > BIG_BLOCK_CHARS
        digest = await asyncio.to_thread(_raw_key, raw) if big else _raw_key(raw)
        key = (is_json, digest)
        cached = self._lru.get(key)
        if cached is not None:
            self._lru.move_to_end(key)
            return cached
        if big:
            prepared = await asyncio.to_thread(prepare_block, raw, self._max_chars, is_json)
        else:
            prepared = prepare_block(raw, self._max_chars, is_json)
        if key in self._lru:
            self._lru_chars -= len(self._lru.pop(key)[1])
        self._lru[key] = prepared
        self._lru_chars += len(prepared[1])
        while self._lru and (len(self._lru) > LRU_MAX_BLOCKS or self._lru_chars > LRU_MAX_CHARS):
            _, (_, body) = self._lru.popitem(last=False)
            self._lru_chars -= len(body)
        return prepared


def _raw_key(raw: str) -> bytes:
    return hashlib.sha256(raw.encode("utf-8", _UNENCODABLE)).digest()
