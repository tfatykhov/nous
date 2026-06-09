# Agent Tools Subsystem — Code-Only Audit (2026-06-09)

Scope: `nous/api/tools.py`, `builtin_tools.py`, `web_tools.py`, `search_providers.py`,
`search_router.py`, `email_tools.py`, `telegram_tools.py`, `subtask_tools.py`.
Reachability verdicts checked against `nous/config.py` defaults and
`.env.prod-snapshot`. Boundaries (`runner.py`, `rest.py`, `mcp.py`,
`retrieval_pipeline.py`, `heart/*`, `brain/*`) read only to trace wiring.

---

## (a) How it actually works

**Dispatcher.** `ToolDispatcher` (tools.py:40) holds `name → handler` and
`name → schema` maps. `register()` clears an F036 per-frame schema cache.
`dispatch(name, args, session_id)` (tools.py:61) injects an infrastructure
`_session_id`/`session_id` kwarg for a *hard-coded allowlist* of tool names
(spawn_task, cache_retrieve, run_python, ingest_document, recall_deep), calls
`handler(**args)`, and extracts `result["content"][0]["text"]`. Every handler is
expected to catch its own exceptions and return an MCP dict; the dispatcher's
outer `except` only catches programming errors and returns `("Tool error: …",
True)`. `available_tools(frame_id)` filters by `FRAME_TOOLS` (runner.py:92) with
a `"*"` wildcard for the `task` frame.

**Timeout.** Per-tool timeout (`NOUS_TOOL_TIMEOUT`, prod 2000s) is enforced only
in the *streaming* path via `_dispatch_with_keepalive` (runner.py:2444, wraps
`dispatch` in `asyncio.wait_for`). The *non-streaming* `_tool_loop`
(runner.py:1728) calls `dispatch` directly with no `wait_for` — background
subtasks/heartbeat are bounded only by their outer subtask `wait_for` and each
tool's internal timeout.

**Memory tools** (`create_nous_tools`, tools.py:433): record_decision, learn_fact,
recall_deep (thin wrapper over `run_recall_pipeline` + `_format_pipeline_text`),
create_censor, recall_recent, learn_skill, get_procedure, recall_hubs,
ingest_document. **Subtask tools** (`create_subtask_tools`, tools.py:1892):
spawn_task (with F078 censor gate at creation, F061 hardened inline executor,
F062 payload schema), schedule_task, list_tasks, cancel_task, spawn_sync.
**run_python** (tools.py:2918): restricted-`__builtins__` `exec` in a single
ThreadPoolExecutor thread, memory wrappers scheduled back onto the main loop via
`run_coroutine_threadsafe`. **builtin_tools**: bash (cwd=workspace, unrestricted
command), read_file/write_file (`_validate_path` → `is_relative_to(workspace)`).
**web_tools**: web_search (SearchRouter, always wired in main.py:494) and
web_fetch (SSRF check + manual redirect re-check). **search_router**: keyword
classify → Tavily/Brave for factual, Exa/Tavily/Brave for research, availability
filtered, cascading fallback. **email/telegram**: guarded send_email (allowlist +
secret scan + rate limit) and send_file (Telegram upload).

---

## (b) Findings register

### P2

**AT-1 — `schedule_task` has no censor gate (the autonomous-exfil path is unguarded).**
Severity P2 · LIVE (`NOUS_SCHEDULE_ENABLED=true` in prod).
`tools.py:2268-2346`. `spawn_task` explicitly treats its creation-time
`heart.check_censors(task)` call as "the ONLY censor enforcement a subtask gets
(the exfil path)" (tools.py:1986-2034). Scheduled tasks are equally
non-interactive background executions, but `schedule_task` performs **no**
censor check at all before `heart.schedules.create(...)`. An `abort`/`refuse`
censor that would reject a `spawn_task` is silently bypassed by routing the same
instruction through `schedule_task` (e.g. `every="in 1 minute"`).
Evidence: no `check_censors` call anywhere in the `schedule_task` body; compare
to spawn_task's gate. Fix: run the same F078 censor gate (reject on
abort/refuse, inject steer directives) in `schedule_task` before create, and
re-evaluate at fire time in the scheduler.

**AT-2 — REFUTED ON VERIFICATION (2026-06-09, main-session re-check). The F067
summarizer DOES take the same advisory lock.**
~~Severity P2~~ → downgraded to RESOLVED/NO-ISSUE.
Original claim: `ingest_document` (`tools.py:1240-1321`) allocates `chunk_index`
via `MAX()+1` under `pg_advisory_xact_lock("ingest_document:{episode}")` while
the F067 summarizer writes the same `(episode_id, chunk_index)` space without
the lock. Verification shows `handlers/episode_summarizer.py:259-292` ("Audit
E1 (2026-06-09)" comment, shipped in PR #495) acquires the **identical lock
key** `ingest_document:{episode_id}` and likewise allocates from
`MAX(chunk_index)+1` with a per-kind existence check for idempotency. The two
writers serialize correctly at HEAD; no chunk loss path remains.

**AT-3 — `spawn_sync` is omitted from the dispatcher's `_session_id` injection list → `parent_session_id` always lost.**
Severity P2 · LATENT (`subtask_payload_schema_enabled=false` default and unset in
prod, so `spawn_sync` is not registered today; becomes LIVE the moment the flag
flips).
`tools.py:72-89` (injection allowlist) vs `tools.py:2447-2485` (spawn_sync reads
`_session_id`). The dispatcher injects `_session_id` only for spawn_task,
run_python, ingest_document, recall_deep (and `session_id` for cache_retrieve).
`spawn_sync` declares `_session_id: str | None = None` and forwards it to
`spawn_task(..., _session_id=_session_id)` specifically to preserve caller
session linkage (Codex round-14 comment, tools.py:2461-2466), but because
dispatch never injects it, `spawn_sync` invoked by the model always runs with
`_session_id=None`. The subtask row gets `parent_session_id=None`, defeating the
cognitive-layer delivery sweep (`get_undelivered`) the comment is at pains to
protect. Fix: add `spawn_sync` to the dispatch injection allowlist (or generalize
the allowlist to any handler that declares a `_session_id` kwarg).

### P3

**AT-4 — `run_python` is not a real sandbox, and a non-terminating script leaks a thread.**
Severity P3 · LIVE (`programmatic_tools_enabled=true`), but escape is
non-escalating because every frame exposing `run_python` also exposes `bash`.
`tools.py:2879-3026`. Restricting `__builtins__` to `SAFE_BUILTINS` does not
sandbox CPython — attribute traversal (`().__class__.__mro__[1].__subclasses__()`)
reaches `subprocess.Popen`/`os` without `__import__` or `open`. Treat
`run_python` as arbitrary code execution (acceptable only because `bash`
coexists in conversation/question/task/debug frames). Separately, the timeout is
enforced via `asyncio.wait_for(loop.run_in_executor(...))` and on timeout the
`finally` calls `executor.shutdown(wait=False, cancel_futures=True)` — which
**cannot** interrupt a thread already executing `exec`. A `while True: pass`
returns a timeout error to the model but leaks a live CPU-bound thread for the
process lifetime. Fix: document the non-sandbox reality; consider a subprocess
with a hard kill for true isolation, or at least cap concurrent run_python
threads.

**AT-5 — `web_fetch` SSRF check is TOCTOU / DNS-rebinding-vulnerable.**
Severity P3 · LATENT.
`web_tools.py:52-83, 229-263`. `_is_url_safe` resolves the hostname with
`socket.getaddrinfo` and checks the returned IPs, then `httpx.get(current_url)`
performs its **own** independent DNS resolution. A hostname that resolves to a
public IP during the check and a private IP (e.g. `169.254.169.254`) during the
fetch bypasses the guard. Redirect hops are re-checked the same (vulnerable) way.
Fix: pin the validated IP into the connection (custom transport / resolver), or
resolve once and connect by IP with `Host` header.

**AT-6 — `send_file` reads an arbitrary filesystem path and accepts an arbitrary `chat_id`.**
Severity P3 · LIVE (`NOUS_TELEGRAM_BOT_TOKEN` set in prod); risk bounded by
Telegram delivery rules.
`telegram_tools.py:67-145`. No path validation: `file_path` may be any path the
process can read (e.g. `/app/.env`), and `chat_id` is model-controllable. Unlike
`send_email` (allowlist-guarded), `send_file` has no recipient guard. Exfil to an
attacker-controlled chat generally fails (the bot must be a member of the target
chat), and the default chat is the operator's, so practical risk is modest — but
arbitrary-file read + upload is an unguarded data-egress channel. Fix:
`_validate_path` the file against the workspace and/or restrict `chat_id` to the
configured operator chat.

**AT-7 — `learn_fact`'s `source` parameter is advertised but silently ignored.**
Severity P3 · LIVE.
`tools.py:521-560`, schema `tools.py:1435`. The handler signature and JSON schema
both expose `source` ("Where this fact came from"), but the body hard-codes
`source="user_direct"` (for the F023/F038 +0.15 admission bonus), discarding the
caller's value entirely. The schema therefore lies; any provenance the model
supplies is dropped. (`run_python._learn_fact`, tools.py:2959, also hard-codes
`user_direct` but doesn't expose the param, so it's only misleading here.) Fix:
remove `source` from the schema, or honor it while still applying the bonus only
for genuine direct calls.

**AT-8 — `bash_tool` timeout kills only the shell, orphaning child processes.**
Severity P3 · LIVE.
`builtin_tools.py:76-93`. On timeout, `proc.kill()` signals only the immediate
shell, not its process group. A `bash -c "sleep 999 & …"`-style command leaves
orphaned children running after the timeout returns. Fix: start the subprocess in
a new session (`start_new_session=True`) and `os.killpg` on timeout.

**AT-9 — web-search rate limiter is not async-safe and charges quota for failed searches.**
Severity P3 · LIVE.
`web_tools.py:86-108, 131-136`. `_rate_limit` is a module-global dict mutated
without a lock — concurrent `web_search` calls race the read/increment.
`_check_rate_limit` also increments **before** the provider call, so timed-out or
all-providers-failed searches still consume a daily slot. Fix: guard with an
`asyncio.Lock`; only count successful searches (or decrement on failure).

**AT-10 — `NOUS_TOOL_TIMEOUT` is not enforced on the non-streaming dispatch path.**
Severity P3 · LIVE (fix lives in `runner.py`, a boundary file — recorded here
because the symptom is per-tool timeout).
`runner.py:1728` calls `self._dispatcher.dispatch(...)` with no `wait_for`,
whereas the streaming path (`_dispatch_with_keepalive`, runner.py:2456) wraps it
in `wait_for(timeout=tool_timeout)`. Background subtasks and heartbeat checks use
the non-streaming loop, so a tool with no internal timeout (e.g. `recall_deep`
stalled on a DB lock) is bounded only by the much larger outer subtask timeout
(prod `NOUS_SUBTASK_MAX_TIMEOUT=5000`), tying up a worker for up to ~83 min. Fix
(runner owner): wrap the non-streaming dispatch in the same `wait_for`.

---

## (c) Dead-code inventory

- **`_web_search` direct-Brave fallback branch (web_tools.py:156-205).** DEAD in
  production: `register_web_tools` is always called with a non-None `SearchRouter`
  (main.py:494), so `_router is not None` is always true and the
  `if not _settings.brave_search_api_key …` fallback is unreachable. Kept only for
  the no-router call form, which production never uses.
- **`_session_id` plumbing in `recall_deep` for F055** is inert unless
  `NOUS_RESIDUAL_ACTIVATION_ENABLED` AND a `_residual_activator` is wired; prod
  sets the flag true, so this is LIVE in prod, INERT in the eval harness — not
  dead, noted for completeness.

## (d) Improvement opportunities / resolved observations

- **`_validate_path` (builtin_tools.py:31-44) correctly uses `is_relative_to` on
  resolved paths** — the historical `startswith` prefix-collision bug
  (`/workspace` vs `/workspace-evil`) is already fixed, and `.resolve()` collapses
  symlinks before the check. No action needed.
- **`bash` is intentionally unrestricted** (arbitrary command, only cwd pinned to
  workspace; read_file/write_file are sandboxed but bash is not). By design for an
  agent, but worth a one-line note that the workspace sandbox is advisory, not a
  security boundary, once `bash` is in the frame.
- **Dispatcher `_session_id` injection is an ad-hoc per-name allowlist**
  (tools.py:73-89). Generalize to "inject for any handler whose signature declares
  `_session_id`" to prevent recurrence of AT-3 as new session-aware tools are added.
- **`web_fetch` `max_chars` has no `minimum`** in `_WEB_FETCH_SCHEMA`
  (web_tools.py:345); a negative value slices `text[:negative]`. Harmless but add
  `"minimum": 1`.
- **send_email secret scan does not cover attachment contents** (documented,
  email_tools.py:116-135) — acceptable given the recipient allowlist, but note it.
