# F100: Browser Task (Phase 1: in-process browser use)

**Status:** Phase 1 merged dark (`NOUS_BROWSER_TASK_ENABLED=false`). Phase 2 is future work.
**Code:** `nous/browser/` · **Tests:** `tests/test_browser_task.py`

## What it is

A tool `browser_task(goal, allowed_domains, start_url=None, max_steps=30)` for the main agent. It runs a
**separate agent loop** on Anthropic's browser use toolset (`browser_toolset_20260801`, GA, no beta header),
driven by the Playwright 1.64 + Chromium already in the image (`/opt/ms-playwright`). The inner loop sees
screenshots, accessibility trees and page text. The main agent gets back only text: a status, a bounded
summary written by the inner model, the final URL without its query string, and the path of a saved final
screenshot.

Use it for tasks *inside* web pages (forms, JavaScript-built pages, multi-step navigation). For reading a page
or finding sources, `web_fetch` and `web_search` are cheaper.

## Design

| Part | File | Notes |
|------|------|-------|
| Session | `session.py` | Headless Chromium, fresh profile, viewport 1280x800 (fits the image limits, so screenshots are never rescaled and model coordinates are viewport pixels 1:1), `--disable-dev-shm-usage` (`/dev/shm` is 64M). Tab registry (`tab-1`, `tab-2`, ...) feeds `browser_state` blocks. |
| Executor | `executor.py` | All 27 default toolset members mapped to Playwright. Element refs come from Playwright's AI snapshot (`page.aria_snapshot(mode="ai")`, `[ref=e5]` shown to the model as `[ref_5]`, resolved through `aria-ref=e5`); an unknown ref is a "stale or not found" error. `zoom` uses CDP `Page.captureScreenshot` with a clip scale. `javascript_exec`, `file_upload`, `read_console` and `read_network` stay disabled and answer with an error. |
| Loop | `loop.py` | Batches run in order with the documented halt rule; every result echoes `toolset_name: "browser"`; tab-management results are exactly one `browser_state` block. The assistant content is appended verbatim (thinking blocks included). |
| Tool | `tools.py` | Registers `browser_task` when the flag is on (`main.py`). |

**Model pinning.** The inner loop always uses `NOUS_BROWSER_TASK_MODEL` (default `claude-sonnet-5-5`), never
the caller's model. The background model (default `claude-sonnet-4-6`, which the heartbeat falls back to) and the
critic as production runs it (`claude-haiku-4-5`) don't support the toolset. The setting is checked against `SUPPORTED_MODELS` (`claude-fable-5`, `-5-1`, `claude-mythos-5`,
`-5-1`, `claude-opus-4-8`, `claude-opus-5`, `-5-5`, `claude-sonnet-5`, `-5-5`, `claude-haiku-5-5`). A bad value
is logged at startup, and every call fails at once with a message that names the setting. The task never
launches a browser or calls the API with an unsupported model.

**Transport.** The loop has its own small `POST /v1/messages` caller, using the same auth rules as
`nous/api/anthropic_client.py`: an API key, or a Bearer token plus the OAuth betas for OAT, with the
Claude Code preamble at system block 0. It does not use the shared client, because the shared client's SDK
backend drops `toolset_name` from `tool_use` blocks and drops `context_management`, and the API would reject
the replayed history.

**Context and cost.** Old tool results are cleared server-side by context editing (`clear_tool_uses_20250919`,
beta `context-management-2025-06-27`): the trigger is 30k input tokens, it keeps the last 3 tool uses, and it
clears at least 5k tokens at a time. Screenshots are never pruned on the client: on the 5.5 models, editing an
earlier turn invalidates the thinking that came after it. The toolset entry carries `cache_control`, which
caches the roughly 6.6k-token definition. The system prompt has a breakpoint, and a top-level automatic
breakpoint covers the newest turn. System and tools are identical on every call. Effort is `medium`, and
thinking is left at the model default (adaptive).

**Budget.** The limits are `NOUS_BROWSER_TASK_MAX_STEPS` model turns, which the `max_steps` argument can only
lower, and `NOUS_BROWSER_TASK_TIMEOUT_SECONDS`. The timeout is clamped below `NOUS_TOOL_TIMEOUT` (default
120 s) with 10 s left to close the browser, because the runner cancels any tool at that point and would
discard the result. After a timeout the final screenshot is skipped. A failure while closing the browser is
logged and never changes a finished task's outcome. Only `end_turn` counts as completed; another stop reason
(such as `max_tokens`) is an error, and a completed task's summary is the final turn's text only.

## Safety boundary

1. **No raw web content in the main loop.** The outer result is `BrowserTaskResult.render()`. Its summary is
   the inner model's own text, with control characters and markup tags stripped and a 4,000-character cap. It is labelled as
   derived from untrusted web content, to be treated as data and not as instructions. Screenshots go to disk
   for the owner and are never sent to the main loop. The final URL has its query string, fragment and
   userinfo stripped.
2. **Domain allowlist at the Playwright layer.** Every request the browser context makes is routed through the
   allowlist (`context.route("**/*")`). A host that is not an entry or a subdomain of an entry, or a scheme
   other than http/https, is aborted. Entries must be hosts: a bare suffix such as `com` is refused. Service workers are blocked because they bypass routing. WebSockets and
   downloads are refused. Playwright does not route the redirects of a request it already allowed, so after
   every action, including one that failed, the executor checks each tab's and each frame's URL. A child frame
   that ended up off the list is blanked; a tab that did is blanked (closed if that fails) and the action fails. `navigate` refuses non-http(s) schemes and off-list hosts before loading anything. Loopback
   and private addresses are reachable only if listed literally (e.g. `127.0.0.1`); link-local (cloud
   metadata) and unspecified addresses are refused even then.
3. **Confirmation guard, two layers.** The task stops with `needs_confirmation` and does not take the step:
   - *Model layer.* The inner model must call the custom `request_confirmation` tool before logging in,
     entering credentials, paying, purchasing, booking, accepting terms, sending or posting messages,
     subscribing or deleting anything.
   - *Executor layer.* This check runs before each call, because one turn can carry several. It covers a click,
     drag or checkbox on an element whose label matches a narrow list of consequential words; submitting a
     form that has password or payment fields; typing or `form_input` into a password or `cc-*` field; and
     every key that activates the focused control (Enter, Space), checked chord by chord, so `Tab Enter`, a
     newline inside `type` and `hold_key` are covered too. Coordinate targets and focus are resolved through
     open shadow roots and same-origin frames; a cross-origin frame cannot be inspected, so acting inside one
     stops the task. A text field's typed value is not part of its label, so a search for "send money" does not
     trip it. The reason names only the matched word from the guard's own list, never the page's label text.
     A trip ends the task. It is never returned to the model, so the model can't work around it.
   - No credentials are ever passed to the inner loop. A re-run of the same goal stops at the same step. Once
     the owner has confirmed, carrying the step out is Phase 2 work, or done by hand.
4. **Fresh, isolated browser.** Each call launches its own Chromium with an empty profile and closes it at the
   end. Artifacts are written to `/tmp/nous-workspace/browser-task/<run-id>/` (`step-NNN.png`, `final.png`).
5. **Policy class.** `browser_task` is `external` in `tool_classes.py`. It is outside every `internal_only`
   (continuation) turn and the heartbeat outward floor, it is stripped by refuse-tier censors, and the
   execution ledger records it.

Known gaps for Phase 1: an allowed host whose DNS resolves to a private address is not detected; a
sub-resource redirect from an allowed host to another host is not caught; and the executor guard is
label-based, so a consequential button with a neutral label ("Continue") relies on the model layer.

## Settings

| Variable | Default | Meaning |
|----------|---------|---------|
| `NOUS_BROWSER_TASK_ENABLED` | `false` | Register the tool. |
| `NOUS_BROWSER_TASK_MODEL` | `claude-sonnet-5-5` | Inner model; must be in `SUPPORTED_MODELS`. |
| `NOUS_BROWSER_TASK_MAX_STEPS` | `30` | Cap on model turns per call. |
| `NOUS_BROWSER_TASK_TIMEOUT_SECONDS` | `110` | Per-call wall clock, clamped to `NOUS_TOOL_TIMEOUT - 10`. |

## Phase 2 (future work)

- Move the browser into a **separate desktop sidecar container** (its own network policy and egress
  allowlist at the network layer, no access to the Nous database or workspace), driven over a narrow RPC. The
  executor interface (`execute(name, input) -> blocks`) is the seam.
- Enforce the allowlist at the network layer as well (an egress proxy), which closes the DNS and redirect
  gaps above.
- A resumable confirmation flow: the owner approves the pending step (A2UI approval gate) and the task
  continues from saved state instead of stopping.
- Optionally the computer use toolset for whole-desktop tasks.
