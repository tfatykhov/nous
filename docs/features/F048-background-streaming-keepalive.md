# F048 — Background Streaming + TCP Keep-Alive for Long Anthropic Calls

**Status:** ✅ Shipped
**Owner:** nous core
**Scope:** Subtask workers and heartbeat cognitive triage / on_complete callbacks + DynamicCheck._run_check + inline `spawn_task(await_result)`. Foreground `/chat/stream` is out of scope (already streams).

---

## Problem

Anthropic support reports subtask and heartbeat runs hitting read timeouts and retry storms. Anthropic's own guidance:

> Avoid setting a large `max_tokens` value without using the streaming Messages API or the Message Batches API. Some networks may drop idle connections after a variable period of time, which can cause the request to fail or timeout without receiving a response from Anthropic. If you are building a direct API integration, setting a TCP socket keep-alive can reduce the impact. The SDKs validate that your non-streaming Messages API requests are not expected to exceed a 10-minute timeout and also set a socket option for TCP keep-alive.

Today in Nous:

1. `SubtaskWorkerPool._execute_subtask` → `AgentRunner.run_turn()` → `HttpxAnthropicClient.call()` / `SdkAnthropicClient.call()` — **non-streaming POST**. (`nous/handlers/subtask_worker.py:144`)
2. `HeartbeatRunner._cognitive_triage` and `_execute_callback` → same non-streaming path. (`nous/heartbeat/runner.py:492`, `:572`)
3. `HttpxAnthropicClient.start()` builds `httpx.AsyncClient` with **no `socket_options`** — no TCP keep-alive. (`nous/api/anthropic_client.py:300-306`)
4. `SdkAnthropicClient.start()` passes a custom `httpx.AsyncClient(http2=True, limits=...)` to `AsyncAnthropic`, which **overrides the SDK's default transport** and therefore silently disables the keep-alive the SDK normally provides. (`nous/api/anthropic_client.py:539-545`)
5. Both client timeouts default to `api_timeout_read=120s` — fine for foreground but too tight for background turns that emit ~4K tokens over a congested link.

### Why this matters now

- Heartbeat ticks run every 30s. A single stalled triage blocks its budgeted window and looks like a silent failure from Telegram's side.
- Subtasks are fire-and-forget; a retry storm silently burns the daily token budget then fails the task at the outer `asyncio.wait_for` timeout, not at the API.
- Both paths pay full input-token cost for every retry, wrecking cache hit rate (F036).

---

## Goals

1. Background LLM turns (subtask, heartbeat triage, heartbeat on_complete callback) **survive multi-minute Anthropic responses without idle-socket drops.**
2. Direct `httpx` integration sets **TCP keep-alive** (SO_KEEPALIVE + platform-appropriate KEEPIDLE/INTVL/CNT) so that intermediaries don't close warm connections.
3. The SDK-backed path actually **uses the SDK's own keep-alive defaults** instead of accidentally overriding them.
4. Callers of `AgentRunner.run_turn()` do **not** need to learn a new API. One additional kwarg (`is_background=True`) at two call sites.
5. A kill-switch env var (`NOUS_API_BACKGROUND_STREAMING_ENABLED`) allows instant rollback.

## Non-goals

- Reworking `/chat/stream` or `/chat` (non-streaming foreground). F048 is background-only.
- Adopting the Message Batches API. Batches add minutes of latency; heartbeat and interactive subtasks need sub-minute response start.
- Changing tool-loop semantics, censor behavior, or cognitive pre/post-turn hooks.
- Changing `subtask_default_timeout` / `subtask_max_timeout` — F046 already handles the outer wrapper.

---

## Design

### Two mechanisms, one feature

**Mechanism A — TCP keep-alive for direct httpx client.**
`HttpxAnthropicClient.start()` constructs its `httpx.AsyncClient` with an `httpx.AsyncHTTPTransport` whose `socket_options` list sets, at minimum, `SO_KEEPALIVE=1`, and where available `TCP_KEEPIDLE=30`, `TCP_KEEPINTVL=10`, `TCP_KEEPCNT=3` (Linux) or `TCP_KEEPALIVE=30` (macOS). Windows sets only `SO_KEEPALIVE`; the OS tunes the rest. This runs for **all** httpx requests (foreground and background) since keep-alive is pure-win at the socket layer.

`SdkAnthropicClient.start()` is modified to **stop overriding** the SDK's default transport unless HTTP/2 is strictly required. The SDK already sets keep-alive correctly. If HTTP/2 is needed, the custom `httpx.AsyncClient` is built with the same keep-alive `socket_options` as HttpxAnthropicClient.

**Mechanism B — Streaming under the hood for background turns.**

A new method on the `AnthropicClient` protocol:
```python
async def call_streaming_aggregated(self, payload: dict[str, Any]) -> ApiResponse
```

Runs the request in streaming mode but aggregates all chunks into the same `ApiResponse` shape that `call()` returns. From the runner's and caller's perspective, it is indistinguishable from `call()` — just with continuous byte flow that defeats idle timeouts.

- **SdkAnthropicClient** implements it via `async with self._client.messages.stream(**kwargs) as s: message = await s.get_final_message()` — the exact pattern Anthropic docs recommend.
- **HttpxAnthropicClient** reuses the existing `stream()` generator, consuming `StreamEvent`s and rebuilding:
  - `content` = list of `{"type": "text", "text": ...}` and `{"type": "tool_use", "id": ..., "name": ..., "input": ...}` blocks stitched from `content_block_start` / `content_block_delta` / `content_block_stop` events.
  - `stop_reason` = value from the final `message_delta` event (emitted as `StreamEvent.type == "done"`).
  - `usage` = merged `input_tokens` / `cache_*` from `message_start` with `output_tokens` from the final `done` event.

`AgentRunner.run_turn()` gains an `is_background: bool = False` kwarg. `_call_api()` receives the same flag and routes to `call_streaming_aggregated()` when the flag is set and `NOUS_API_BACKGROUND_STREAMING_ENABLED` is true. Default false preserves the existing foreground contract.

**Call sites to flip:**
| File | Line | Change |
| --- | --- | --- |
| `nous/handlers/subtask_worker.py` | `_execute_subtask` (~:144) | `run_turn(..., is_background=True)` |
| `nous/heartbeat/runner.py` | `_cognitive_triage` (~:492) | `run_turn(..., is_background=True)` |
| `nous/heartbeat/runner.py` | `_execute_callback` (~:572) | `run_turn(..., is_background=True)` |

### Timeouts

Background streaming still needs a ceiling for the *total* body transfer. `httpx.Timeout(read=...)` in streaming mode is per-chunk, not total, so as long as bytes arrive faster than `read`, the connection is healthy. We explicitly set `read=` to `max(api_timeout_read, NOUS_API_BACKGROUND_TIMEOUT_READ)` where `NOUS_API_BACKGROUND_TIMEOUT_READ` defaults to **600** (10 minutes, matching the SDK's cap). Applied only to background requests by passing `timeout=httpx.Timeout(...)` to that specific `stream()` / `post()` call; the client-wide default is unchanged.

### Config knobs (new)

| Env var | Default | Meaning |
| --- | --- | --- |
| `NOUS_API_BACKGROUND_STREAMING_ENABLED` | `true` | Master switch for Mechanism B |
| `NOUS_API_BACKGROUND_TIMEOUT_READ` | `600` | Per-chunk read timeout for background streamed requests |
| `NOUS_API_SOCKET_KEEPALIVE_ENABLED` | `true` | Master switch for Mechanism A |
| `NOUS_API_SOCKET_KEEPIDLE` | `30` | Idle seconds before first keep-alive probe |
| `NOUS_API_SOCKET_KEEPINTVL` | `10` | Seconds between keep-alive probes |
| `NOUS_API_SOCKET_KEEPCNT` | `3` | Failed probes before connection is dropped |

All unused knobs on Windows get a debug log.

### Backward compatibility

- `call()` / `stream()` signatures unchanged.
- `run_turn()` gains a keyword-only default-false kwarg — all existing callers pass positionally up to `tool_filter` and hit the default.
- Adding `call_streaming_aggregated` to the `AnthropicClient` protocol is backwards-compatible for any out-of-tree client: missing method triggers explicit `AttributeError` at runtime with a log pointing to F048.

---

## Acceptance criteria

1. `httpx.AsyncHTTPTransport` in `HttpxAnthropicClient` has `SO_KEEPALIVE=1` plus Linux keep-alive tunables; asserted in unit test.
2. `SdkAnthropicClient`'s custom httpx client either (a) is removed in favor of the SDK default, or (b) sets the same keep-alive socket options; asserted in unit test.
3. `AgentRunner.run_turn(is_background=True)` routes to `call_streaming_aggregated()`; unit test with stub clients verifies dispatch.
4. `SubtaskWorkerPool._execute_subtask`, `HeartbeatRunner._cognitive_triage`, and `HeartbeatRunner._execute_callback` all pass `is_background=True` — asserted by grep + integration test.
5. `HttpxAnthropicClient.call_streaming_aggregated()` reconstructs a content list with `text` + `tool_use` blocks equal to what a non-streamed `call()` returns; tested with canned SSE event stream.
6. `SdkAnthropicClient.call_streaming_aggregated()` returns `ApiResponse` fields that exactly match `call()` for the same prompt, including cache token counts; tested with mocked `.stream()` context manager.
7. Feature flag `NOUS_API_BACKGROUND_STREAMING_ENABLED=false` falls back to `call()` even when `is_background=True`; asserted by unit test.
8. No behavior change for foreground `/chat`, `/chat/stream`, or any non-background `run_turn()` caller; existing test suite passes without modification.
9. `CLAUDE.md` feature table lists F048 as shipped; `docs/features/INDEX.md` updated; env var table in `CLAUDE.md` gains new knobs.
10. All PR reviews (spec review and implementation review) return no P0 findings.

---

## Rollout

1. Ship behind `NOUS_API_BACKGROUND_STREAMING_ENABLED=true` default. Kill-switch flips to `false` if any regression.
2. Day-1 monitoring: heartbeat tick success rate, subtask completion rate, count of `httpx.TimeoutException` in logs. Expected: retry count → near zero for background turns; success rate → unchanged or higher.
3. No migration needed. No DB schema change.

## Risks

| Risk | Mitigation |
| --- | --- |
| Streaming aggregation loses a usage field | Unit test parity between `call()` and `call_streaming_aggregated()` on identical mocked responses |
| Tool-use blocks aren't reconstructable from SSE events alone | We already do this in `stream_chat`; the aggregator is strictly a subset of that logic |
| Platform-specific socket constants missing on Windows/macOS | Conditional `hasattr(socket, ...)` guards; fall back to `SO_KEEPALIVE` only with debug log |
| SDK version drift breaks `.stream().get_final_message()` | Version-pinned in `pyproject.toml`; upgrade path requires re-running the client tests |
| Feature flag disabled accidentally in prod | Default is `true`; flag is logged at startup |

---

## Related

- Decision `2801808c` (this feature)
- Decision `884108b4` — AnthropicClient protocol extraction
- Decision `533151eb` — Phases A–C of `stream_chat` (source of SSE parsing code we reuse)
- Decision `216b09f9` — HeartbeatRunner dedicated client via `fork()`
- Decision `e7cb8f9a` — `/chat/stream` disconnect fix (foreground; complementary)
