# F048 — Background Streaming + TCP Keep-Alive — Implementation Plan (v2, post-review)

**Feature:** [F048-background-streaming-keepalive.md](../../features/F048-background-streaming-keepalive.md)
**Primary decision:** `2801808c`
**Review-consolidation decision:** `b9577d97`
**Date:** 2026-04-19
**Status:** Revised after 3-agent review (architecture, python, quality). All P0s and P1-1..P1-7 accepted. Ready for implementation.

---

## Summary

One PR, ~9 code files changed, ~4 test files added/touched. No DB migration. Gates live behind `NOUS_API_BACKGROUND_STREAMING_ENABLED` and `NOUS_API_SOCKET_KEEPALIVE_ENABLED` for instant rollback.

- **Mechanism A — TCP keep-alive** on `httpx.AsyncHTTPTransport` for both `HttpxAnthropicClient` and (customized) `SdkAnthropicClient`.
- **Mechanism B — Background streaming aggregation** via new `AnthropicClient.call_streaming_aggregated()` method, selected by `AgentRunner.run_turn(is_background=True)` which threads through `_tool_loop` to every `_call_api` call.

---

## Review findings incorporated

**P0s (all fixed in this revision):**
1. `is_background` must thread through `_tool_loop` (not just `_call_api`). `_tool_loop` at `runner.py:1101` has its own signature and calls `_call_api` at lines 1155, 1301, 1333.
2. `SdkAnthropicClient.call_streaming_aggregated` must `kwargs.pop("stream", None)` before `messages.stream(**kwargs)` — `_payload_to_kwargs` sets `stream=True` which would TypeError on `.stream()`.
3. Two missing background call sites: `heartbeat/dynamic.py:123` (DynamicCheck._run_check) and `api/tools.py:1185` (inline subtask via `spawn_task(await_result=True)`). Both confirmed by grep.
4. Pre-existing bug at `runner.py:249` — censor-blocked branch returns `(response_text, usage)` 2-tuple; callers unpack 3. Fix in this PR.
5. Docstring/comment must explicitly document that `call_streaming_aggregated` is the approved exception to the "Do NOT use `messages.stream()`" warning in `SdkAnthropicClient.stream()`.

**P1s (all incorporated):**
1. `HttpxAnthropicClient.stream()` gains optional `timeout: httpx.Timeout | None = None` kwarg so the aggregator's 600s read timeout is actually honored.
2. `NOUS_SUBTASK_DEFAULT_TIMEOUT` bumped 120 → 600 and `NOUS_SUBTASK_MAX_TIMEOUT` 600 → 3600 so the outer `asyncio.wait_for` does not cancel the inner streaming path before it completes.
3. Aggregator usage-merge test asserts `cache_read_input_tokens` and `cache_creation_input_tokens` survive from `message_start`.
4. Env vars renamed: `KEEPIDLE` → `KEEPALIVE_IDLE`, `KEEPINTVL` → `KEEPALIVE_INTERVAL`, `KEEPCNT` → `KEEPALIVE_COUNT` to match codebase full-word convention.
5. Platform-fallback log when tunables unavailable → `logger.warning` (not debug).
6. CLAUDE.md feature row added as "In-progress" during PR; flipped to "Shipped" only after merge (post-merge doc commit).
7. Aggregator retry semantics documented as explicitly weaker than `call()` — justification: the outer heartbeat/subtask retry layer owns whole-turn retries already.

**P2s accepted:** unit test must assert HTTP/2 is active on the transport post-keep-alive swap; SDK `kwargs["timeout"] = float(api_background_timeout_read)` explicit before `messages.stream()`.

**Rejected:** free-function aggregator (P2 architect suggestion) — Protocol method keeps symmetry; SDK path genuinely needs `self._client`.

---

## Phases (all in one PR; subagent dispatch order below)

### Phase 1 — Config
**File:** `nous/config.py`

Add, near `api_timeout_connect` / `api_timeout_read` (line ~145):
```python
api_background_streaming_enabled: bool = Field(
    default=True, validation_alias="NOUS_API_BACKGROUND_STREAMING_ENABLED",
)
api_background_timeout_read: int = Field(
    default=600, validation_alias="NOUS_API_BACKGROUND_TIMEOUT_READ",
)
api_socket_keepalive_enabled: bool = Field(
    default=True, validation_alias="NOUS_API_SOCKET_KEEPALIVE_ENABLED",
)
api_socket_keepalive_idle: int = Field(
    default=30, validation_alias="NOUS_API_SOCKET_KEEPALIVE_IDLE",
)
api_socket_keepalive_interval: int = Field(
    default=10, validation_alias="NOUS_API_SOCKET_KEEPALIVE_INTERVAL",
)
api_socket_keepalive_count: int = Field(
    default=3, validation_alias="NOUS_API_SOCKET_KEEPALIVE_COUNT",
)
```

Also bump:
```python
subtask_default_timeout: int = Field(
    default=600, validation_alias="NOUS_SUBTASK_DEFAULT_TIMEOUT",   # was 120
)
subtask_max_timeout: int = Field(
    default=3600, validation_alias="NOUS_SUBTASK_MAX_TIMEOUT",      # was 600
)
```

Update the CLAUDE.md env var table in Phase 8 accordingly.

**Verify:** `uv run python -c "from nous.config import Settings; s=Settings(); assert s.api_background_timeout_read==600 and s.subtask_default_timeout==600"`

---

### Phase 2 — Socket-options helper
**File:** `nous/api/anthropic_client.py`

Add a module-level helper:
```python
def _build_socket_options(settings: Settings) -> list[tuple[int, int, int]] | None:
    if not settings.api_socket_keepalive_enabled:
        return None

    import socket, sys

    opts: list[tuple[int, int, int]] = [
        (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
    ]

    keepidle_added = False
    if hasattr(socket, "TCP_KEEPIDLE"):
        opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, settings.api_socket_keepalive_idle))
        keepidle_added = True
    elif hasattr(socket, "TCP_KEEPALIVE"):  # macOS
        opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, settings.api_socket_keepalive_idle))
        keepidle_added = True

    if hasattr(socket, "TCP_KEEPINTVL"):
        opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, settings.api_socket_keepalive_interval))
    if hasattr(socket, "TCP_KEEPCNT"):
        opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPCNT, settings.api_socket_keepalive_count))

    if not keepidle_added:
        logger.warning(
            "TCP keep-alive tunables not available on this platform (os=%s) — "
            "only SO_KEEPALIVE is set; idle probe timing relies on OS defaults",
            sys.platform,
        )

    return opts
```

**Verify:** unit test feeds different `hasattr` matrices and asserts the right options are produced.

---

### Phase 3 — Keep-alive on HttpxAnthropicClient
**File:** `nous/api/anthropic_client.py::HttpxAnthropicClient.start`

Replace the `httpx.AsyncClient(...)` construction (~lines 300-306) with:
```python
sock_opts = _build_socket_options(settings)
transport = httpx.AsyncHTTPTransport(
    http2=True,
    limits=limits,
    socket_options=sock_opts,
)
self._http = httpx.AsyncClient(
    base_url=settings.api_base_url,
    headers=headers,
    timeout=timeout,
    transport=transport,
)
```

Delete the redundant `http2=True`/`limits=limits` kwargs from `AsyncClient` — they'd be silently ignored when `transport=` is set.

Startup log:
```python
logger.info("httpx client initialized (auth: %s, http2: true, keepalive: %s)",
            auth_type, bool(sock_opts))
```

**Verify:** unit test `tests/test_api_keepalive.py::test_httpx_client_sets_keepalive_socket_options` constructs a `HttpxAnthropicClient` with `api_socket_keepalive_enabled=True` and asserts the transport's `_pool._socket_options` contains `SO_KEEPALIVE`. Separate test with flag `False` asserts `_socket_options is None`.

---

### Phase 4 — Keep-alive on SdkAnthropicClient
**File:** `nous/api/anthropic_client.py::SdkAnthropicClient.start` (~lines 539-545)

```python
sock_opts = _build_socket_options(settings)
transport = httpx.AsyncHTTPTransport(
    http2=True,
    limits=httpx.Limits(max_connections=5, max_keepalive_connections=2),
    socket_options=sock_opts,
)
kwargs["http_client"] = httpx.AsyncClient(transport=transport)
```

Drop the redundant `http2=True` / `limits=...` on the `AsyncClient` — transport owns them.

Startup log includes `keepalive: <bool>`.

**Verify:** analogous unit test against SDK client path.

---

### Phase 5 — `call_streaming_aggregated` on both clients + Protocol
**File:** `nous/api/anthropic_client.py`

Extend the Protocol:
```python
async def call_streaming_aggregated(self, payload: dict[str, Any]) -> ApiResponse: ...
```

Add to `HttpxAnthropicClient` — also add optional `timeout=` kwarg to `stream()`:
```python
async def stream(self, payload, *, timeout: httpx.Timeout | None = None): ...
    # existing body; pass timeout=timeout to self._http.stream(...) when set

async def call_streaming_aggregated(self, payload: dict[str, Any]) -> ApiResponse:
    bg_timeout = httpx.Timeout(
        connect=self._settings.api_timeout_connect,
        read=self._settings.api_background_timeout_read,
        write=10.0, pool=10.0,
    )
    blocks: dict[int, dict[str, Any]] = {}
    tool_input_fragments: dict[int, list[str]] = {}
    stop_reason: str | None = None
    usage: dict[str, int] = {}

    async for event in self.stream(payload, timeout=bg_timeout):
        if event.type == "message_start" and event.usage:
            usage.update(event.usage)  # captures input_tokens + cache_*_tokens
        elif event.type == "text_block_start":
            blocks[event.block_index] = {"type": "text", "text": ""}
        elif event.type == "tool_start":
            blocks[event.block_index] = {
                "type": "tool_use",
                "id": event.tool_id,
                "name": event.tool_name,
                "input": {},
            }
            tool_input_fragments[event.block_index] = []
        elif event.type == "text_delta":
            # text_delta events in our parser do not carry block_index reliably;
            # they modify the last text block opened. Track last_text_index.
            # (implementation detail — see existing stream_chat flow)
            ...
        elif event.type == "tool_input_delta":
            tool_input_fragments.setdefault(event.block_index, []).append(event.text)
        elif event.type == "block_stop":
            frags = tool_input_fragments.pop(event.block_index, None)
            if frags is not None and event.block_index in blocks:
                try:
                    blocks[event.block_index]["input"] = json.loads("".join(frags)) if frags else {}
                except json.JSONDecodeError:
                    blocks[event.block_index]["input"] = {}
        elif event.type == "done":
            stop_reason = event.stop_reason
            if event.usage:
                # Merge output_tokens without clobbering cache_* fields from message_start
                for k, v in event.usage.items():
                    if k in ("cache_read_input_tokens", "cache_creation_input_tokens"):
                        if not usage.get(k):
                            usage[k] = v
                    else:
                        usage[k] = v
        elif event.type == "error":
            raise RuntimeError(f"Anthropic streaming error: {event.text}")

    ordered_content = [blocks[i] for i in sorted(blocks)]
    return ApiResponse(
        content=ordered_content,
        stop_reason=stop_reason or "end_turn",
        usage=usage or None,
    )
```

Add to `SdkAnthropicClient`:
```python
async def call_streaming_aggregated(self, payload: dict[str, Any]) -> ApiResponse:
    """Background-path aggregated streaming call.

    NOTE: Uses messages.stream() + get_final_message() — this is the SDK-
    recommended pattern for aggregating a full Message from a streaming
    request. The existing SdkAnthropicClient.stream() method deliberately
    uses messages.create(stream=True) for RAW event iteration; that warning
    does NOT apply here because we only consume the final aggregated Message,
    not raw events.
    """
    if not self._client:
        raise RuntimeError("SDK client not initialized -- call start() first")

    kwargs = self._payload_to_kwargs(payload)
    kwargs.pop("stream", None)  # messages.stream() does not accept stream=
    kwargs["timeout"] = float(self._settings.api_background_timeout_read)

    try:
        async with self._client.messages.stream(**kwargs) as s:
            message = await s.get_final_message()
    except Exception as e:
        self._log_sdk_error(e)
        raise RuntimeError(f"Anthropic SDK streaming error: {e}") from e

    return ApiResponse(
        content=self._message_to_content(message),
        stop_reason=message.stop_reason,
        usage={
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
            "cache_creation_input_tokens": getattr(message.usage, "cache_creation_input_tokens", 0),
            "cache_read_input_tokens": getattr(message.usage, "cache_read_input_tokens", 0),
        } if message.usage else None,
    )
```

Update the docstring on `SdkAnthropicClient.stream()` to cross-reference `call_streaming_aggregated` as the approved exception.

**Tests:** `tests/test_api_streaming_aggregated.py`
- `test_httpx_aggregator_reconstructs_text_and_tool_blocks`
- `test_httpx_aggregator_merges_usage_preserves_cache_tokens` — message_start has cache_read=100; message_delta has only output_tokens; aggregated usage has cache_read=100.
- `test_httpx_aggregator_raises_on_error_event`
- `test_sdk_aggregator_pops_stream_kwarg` — `_payload_to_kwargs` returns `{..., "stream": True}`; after aggregator runs, the mock's `.stream()` was called without a `stream=` kwarg.
- `test_sdk_aggregator_matches_call` — same mocked message in `call()` and `call_streaming_aggregated()` produce identical `ApiResponse`.

---

### Phase 6 — Thread `is_background` through runner
**File:** `nous/api/runner.py`

1. `run_turn` (line 191) — add `is_background: bool = False`.
2. `_tool_loop` (line 1101) — add `is_background: bool = False`.
3. `_call_api` (line 616) — add `is_background: bool = False`.
4. In `_call_api` body:
   ```python
   if is_background and self._settings.api_background_streaming_enabled:
       return await self._api.call_streaming_aggregated(payload)
   return await self._api.call(payload)
   ```
5. `run_turn` passes `is_background` to `_tool_loop` (at the call ~line 330).
6. `_tool_loop` passes `is_background` to every `_call_api` call inside it (lines 1155, 1301, 1333).
7. `end_conversation` reflection call at line 400 — stays foreground; DO NOT pass `is_background=True`.
8. **Fix pre-existing bug at line 249:** change `return response_text, usage` → `return response_text, turn_context, usage` so 3-tuple callers don't crash on censor-block.

**Verify:**
- `tests/test_runner_background.py::test_run_turn_background_calls_streaming_aggregated`
- `test_run_turn_background_respects_feature_flag` — flag=False ⇒ `call()` even when `is_background=True`.
- `test_tool_loop_threads_is_background` — stub `_call_api` records calls, assert every tool-loop iteration sees the flag.
- `test_run_turn_censor_blocked_returns_3_tuple` — regression test for the pre-existing bug.

---

### Phase 7 — Wire background call sites
**Files:**
- `nous/handlers/subtask_worker.py:144` — add `is_background=True`
- `nous/heartbeat/runner.py:492` (`_cognitive_triage`) — add `is_background=True`
- `nous/heartbeat/runner.py:572` (`_execute_callback`) — add `is_background=True`
- `nous/heartbeat/dynamic.py:123` (`DynamicCheck._run_check`) — add `is_background=True`
- `nous/api/tools.py:1185` (`spawn_task` inline with await_result) — add `is_background=True`

**Verify:**
- Grep: `rg "is_background=True" nous/` returns exactly 5 hits.
- Integration test in `tests/test_subtasks.py` patches runner's `_api` to a stub with counter; confirms `call_streaming_aggregated` called.
- Similar integration test in `tests/test_heartbeat.py` for triage and callback.
- Similar in `tests/test_heartbeat_dynamic.py` for `_run_check`.
- `tests/test_subtasks.py::test_inline_subtask_uses_background_streaming` for the tools.py path.

---

### Phase 8 — Docs
**Files:**
- `CLAUDE.md`: add F048 row (status: **In-progress** during PR, **Shipped** post-merge); add 6 env var rows (`NOUS_API_BACKGROUND_*` + `NOUS_API_SOCKET_KEEPALIVE_*`); update `NOUS_SUBTASK_DEFAULT_TIMEOUT` from 120 → 600 and `NOUS_SUBTASK_MAX_TIMEOUT` from 600 → 3600.
- `docs/features/INDEX.md`: add F048 entry in the appropriate P1/infra section.
- `docs/features/F048-background-streaming-keepalive.md`: flip Status to **Shipped** after merge; left at Draft/In-progress in this PR.

**Verify:** `rg "F048" CLAUDE.md docs/features/INDEX.md`.

---

## Subagent dispatch order

Implementation runs **sequentially** (not parallel) to avoid `is_background` threading conflicts across files:

1. **Impl-1 (python-pro):** Phases 1-5 on `config.py` + `anthropic_client.py` (all changes to these two files).
2. **Impl-2 (python-pro):** Phases 6-7 on `runner.py` + `subtask_worker.py` + `heartbeat/runner.py` + `heartbeat/dynamic.py` + `api/tools.py`.
3. **Impl-3 (test-master):** All new tests in `tests/test_api_keepalive.py`, `tests/test_api_streaming_aggregated.py`, `tests/test_runner_background.py` + additions to `test_subtasks.py`, `test_heartbeat.py`, `test_heartbeat_dynamic.py`. Per feedback `feedback_test_impl_ordering.md`, tests asserting exact behavior run AFTER impl, not in parallel.
4. **Impl-4 (leader):** Phase 8 docs. Leader (not a subagent) performs the final branch + commit + PR.

Each subagent records its own forge decision at start, streams 10+ thoughts, finalizes with `update_decision`.

---

## Rollback

- `NOUS_API_BACKGROUND_STREAMING_ENABLED=false` → all background paths fall back to `call()`.
- `NOUS_API_SOCKET_KEEPALIVE_ENABLED=false` → transport uses httpx defaults.
- `NOUS_SUBTASK_DEFAULT_TIMEOUT=120` (override) → reverts timeout change for operators who prefer the old behavior.
