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
| Session | `session.py` | Headless Chromium, fresh profile, viewport 1280x800 (fits the image limits, so screenshots are never rescaled and model coordinates are viewport pixels 1:1), `--disable-dev-shm-usage` (`/dev/shm` is 64M). Validates the allowlist, starts the egress proxy before the browser and closes it after. Tab registry (`tab-1`, `tab-2`, ...) feeds `browser_state` blocks. Teardown is bounded (below). |
| Egress proxy | `egress.py` | The network boundary: an asyncio HTTP proxy on `127.0.0.1` (a free port), one per task. See Safety boundary. |
| Launcher | `chromium-as-user.sh` | Playwright's `executable_path` when Nous runs as root: runs the real Chromium as the `nous-browser` user. See Safety boundary. |
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

**One at a time.** One browser task runs at a time per process (a module-level semaphore). A second call
waits within its own time budget, then returns `busy` (a tool error). The wait, and resolving the allowlist,
come out of the same budget, so the task still ends before `NOUS_TOOL_TIMEOUT`.

**Teardown.** Closing the context, the browser and Playwright, then the proxy, each gets 5 s. If a step hangs
or is cancelled again (a second cancel), the Chromium process group is killed with SIGKILL: the session tags
its Chromium with an inert `--nous-browser-session=<random>` switch and finds it in `/proc` (Linux; elsewhere
Playwright's own cleanup is all there is). A cancel is re-raised once everything is closed.

**Artifacts.** Each run writes to `<workspace>/browser-task/<run-id>/`. When a task starts, run directories
older than 7 days are deleted, and all but the newest 49, so the new run makes at most 50. Only directories
named like a run id are touched.

## Safety boundary

1. **No raw web content in the main loop.** The outer result is `BrowserTaskResult.render()`. Its summary is
   the inner model's own text, with control characters and markup tags stripped and a 4,000-character cap. It is labelled as
   derived from untrusted web content, to be treated as data and not as instructions. Screenshots go to disk
   for the owner and are never sent to the main loop. The final URL has its query string, fragment and
   userinfo stripped.
2. **Network boundary: public hosts only, enforced by an egress proxy.** The main agent chooses
   `allowed_domains` and `start_url`, and it can be prompt-injected, so the allowlist itself is untrusted.
   - *Entries* (and `start_url`'s host) must be a public IP address in canonical form (`is_global`, not
     multicast) or a public DNS name: at least two labels, the last one starting with a letter, and not
     `localhost`, `*.localhost`, `*.internal` (so `host.docker.internal`), `*.local`, `*.lan` or
     `*.home.arpa`. A name whose last label is a number is refused because a browser reads it as an IPv4
     address (`0xa9.0xfe.0xa9.0xfe`, `0251.0376.0251.0376`, `2852039166`, `127.1`). A bare suffix such as
     `com` or a container name such as `nous-postgres` is a single label and refused. One bad entry refuses
     the task.
   - *At task start*, before the browser launches, every name is resolved once. A name that does not resolve,
     or resolves to any non-public address (`*.nip.io` tricks), refuses the task with a clear error.
   - *The egress proxy* (`egress.py`) is the real boundary. Chromium is launched with
     `--proxy-server=http://127.0.0.1:<port>`; Chromium otherwise sends loopback hosts around a proxy, so
     `--proxy-bypass-list=<-loopback>` comes last on its command line. For each `CONNECT host:port` (https,
     wss) or absolute plain-http request the proxy checks the host against the allowlist, resolves it once,
     refuses if **any** answer is not a public unicast address (IPv4-mapped IPv6 unwrapped first: loopback,
     RFC 1918, CGNAT, link-local, reserved, documentation, multicast), and connects to an address from that
     answer itself, never to the name again, so a DNS rebind cannot move a checked connection. A refusal
     answers 403 and counts toward `blocked_requests`; the log names the host and the reason, never the path
     or query. Other requests answer 405. A plain-http connection carries one request, then closes. This
     catches what routing cannot: redirect hops (a 302 or an `<img>` redirected to another host), rebinding,
     and `fetch` from page script.
   - *Channels that bypass a proxy.* `--force-webrtc-ip-handling-policy=disable_non_proxied_udp` stops WebRTC
     (STUN, TURN, data channels) from sending UDP around the proxy; with it, Chromium 156 gathers no ICE
     candidates and sends no datagrams (a test sends STUN at a local UDP port). With an HTTP proxy Chromium
     needs no DNS of its own, so `--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1` fails every
     lookup but the proxy's address, which stops DNS prefetch from leaking names: Chromium 156 has no
     `--dns-prefetch-disable` switch, and `NetworkPrediction` is a preference, not a feature that
     `--disable-features` can turn off (a second `--disable-features` would also replace the list Playwright
     passes). Preconnects open proxied connections, so the proxy checks them. A WebTransport (QUIC) attempt at
     a local UDP port sends no datagrams either (same test).
   - *Playwright routing* is the second layer: every request the context makes is routed through the
     allowlist (`context.route("**/*")`); a host that is not an entry or a subdomain of one, or a scheme other
     than http/https, is aborted. Service workers are blocked because they bypass routing. WebSockets and
     downloads are refused. `navigate` refuses non-http(s) schemes and off-list hosts before loading anything.
   - *Page and frame URLs* are checked before and after every member call, including one that failed: a
     navigation an earlier call started (a delayed script, a slow redirect) may leave the allowlist after
     that call's check. A child frame that is off the list is blanked; a tab that is off it is blanked (closed
     if that fails) and the call fails without running. The final screenshot blanks such a tab first.
3. **Confirmation guard, two layers.** The task stops with `needs_confirmation` and does not take the step:
   - *Model layer.* The inner model must call the custom `request_confirmation` tool before logging in,
     entering credentials, paying, purchasing, booking, accepting terms, sending or posting messages,
     subscribing or deleting anything.
   - *Executor layer.* This check runs before each call, because one turn can carry several. It stops:
     - a click, drag, drop, mouse release or checkbox on an element whose label matches a narrow list of
       consequential words. The label is the element's own text plus its associated `<label>`s (`el.labels`),
       `aria-labelledby` targets, `aria-label`, title and alt, and, for a ref target, the computed accessible name
       from Playwright's AI snapshot;
     - any click, Enter, Space (or typed space) or tap inside a `<form>` (or a form-associated control) that
       contains a password or payment field, whatever the control's label or type, so `type=button` with
       `requestSubmit()`, `div role=button` and label-less buttons are covered;
     - typing or `form_input` into a password field or a payment field. A payment field has `autocomplete=cc-*`,
       or a name, id, aria-label, placeholder or label that names a card, CVC/CVV/CSC, expiry, security code,
       IBAN, routing or account number, or is `inputmode=numeric` with a `maxlength` of 12 to 19;
     - a drag or mouse release onto such a target: the drop target is inspected before pressing and again
       before releasing, and `left_mouse_up` inspects what is under the pointer;
     - every key that activates the focused control (Enter, Space), checked chord by chord, so `Tab Enter`, a
       newline inside `type` and `hold_key` are covered too.

     Coordinate targets and focus are resolved through open shadow roots and same-origin frames; a cross-origin
     frame cannot be inspected, so acting inside one stops the task. **Fail closed:** when the target of a
     guarded action cannot be inspected at all, the action does not run and the task ends with
     `needs_confirmation` and the fixed reason "could not inspect the target". A text field's typed value is
     not part of its label, so a search for "send money" does not trip it. The reason names only the matched
     guard word or that fixed reason, never the page's text. A trip ends the task. It is never returned to the
     model, so the model can't work around it.
   - *Select changes and URLs.* Choosing a `<select>` option (`form_input`) is guarded like a click: the
     chosen option's text and the select's own labels are matched against the same vocabulary, and a select in a
     form with a password or payment field stops the task, before the change event can fire. Navigation is
     guarded too: `start_url` (before the browser starts) and every model `navigate` have their path and query
     searched for `unsubscribe`, `delete`, `remove`, `cancel`, `confirm`, `approve`, `verify`, `magic`,
     `logout`, `pay`, `payment`, `checkout`, `purchase`, `subscribe`, `transfer`; a query parameter named
     `token`, `code` or `key`; and `login`/`signin` beside a token-like parameter. It is a blunt heuristic
     (`/blog/how-to-cancel-a-subscription` trips it; the fragment is not looked at; back, forward and reload
     are not checked). The reason names only the matched word, never the URL.
   - No credentials are ever passed to the inner loop. A re-run of the same goal stops at the same step. Once
     the owner has confirmed, carrying the step out is Phase 2 work, or done by hand.
4. **Fresh, isolated browser, as an unprivileged user.** Each call launches its own Chromium with an empty
   profile and closes it at the end. Artifacts are written to `/tmp/nous-workspace/browser-task/<run-id>/`
   (`step-NNN.png`, `final.png`). The container runs as root and holds the database and API credentials, so
   Chromium does not: when Nous runs as root, Playwright launches `nous/browser/chromium-as-user.sh` as the
   browser executable. The wrapper chowns the fresh profile directory to `nous-browser` (a system user the
   Dockerfile creates: no sudo, no login shell, no groups but its own; the existing `claude-runner` user has
   passwordless sudo, so it would not help) and `exec`s `setpriv --reuid=nous-browser --regid=nous-browser
   --clear-groups --reset-env --no-new-privs` into the same headless shell Playwright would run. `exec` keeps
   the pid (so Playwright's process-group kill still works) and the CDP pipe on fds 3 and 4.
   `--reset-env` leaves Chromium only `HOME`, `PATH`, `SHELL`, `USER` and `LOGNAME`, never the agent's
   API keys or database credentials. Verified in the image: every Chromium process (browser, zygotes, GPU,
   network utility, renderer) runs as uid `nous-browser` with no capabilities and `NoNewPrivs: 1`; an
   `ANTHROPIC_API_KEY` set on the container is absent from the browser's environment.
   - *Sandbox: off.* Chromium's own sandbox needs user namespaces, and Docker's default seccomp profile
     refuses them to every process in the container, root included (`unshare -U` fails with EPERM), so
     Chromium cannot start with it ("Chromium sandboxing failed!"), with or without the namespace layer. It
     keeps `--no-sandbox` and runs as `nous-browser`. Turning it on would need a seccomp profile that allows
     user namespaces in prod's compose file.
   - *Fallback.* When Nous is not root (local development, tests) or the user, the wrapper or `setpriv` is
     missing, Chromium is launched directly as the current user, and a WARNING is logged once.
5. **Policy class.** `browser_task` is `external` in `tool_classes.py`. Who may call it follows the context policy table (`tool_policy.py`), the same as `send_email`: interactive and MCP turns, subtasks, DAG nodes, scheduled tasks and agent actions may; heartbeat triage, background-model calls and `internal_only` continuation turns (local levels only) may not. It is stripped by refuse-tier censors, and the execution ledger records it. Under `NOUS_STABLE_TOOL_SET_ENABLED` (the default) every non-initiation frame is offered the tool, so `FRAME_TOOLS["conversation"]` is nominal and the context policy, not the frame, decides.

Known gaps for Phase 1:
- **Chromium's sandbox is off** (Docker's default seccomp profile refuses user namespaces; see 4), so a
  renderer exploit runs code as `nous-browser` directly.
- **A renderer escape is contained by a kernel rule, which needs `NET_ADMIN`.** Code running as
  `nous-browser` shares the container's network namespace, so `nous/browser/firewall.py` installs, before each
  launch, an iptables owner-match rule: `OUTPUT -m owner --uid-owner nous-browser -j NOUS_BROWSER`, and in that
  chain `ACCEPT -o lo -d 127.0.0.1 -p tcp --dport <NOUS_BROWSER_TASK_PROXY_PORT>` then `REJECT` (IPv6: `REJECT`
  only). The REST API, the database and the LAN are unreachable for that user. The proxy listens on that fixed
  port (safe under the one-task-at-a-time slot). The chain is rebuilt atomically with `iptables-restore`, and
  the effect is probed once per process as `nous-browser` (a decoy local port must refuse, the proxy port
  must accept). **It fails closed**: no `iptables`, no `cap_add: [NET_ADMIN]`, a command error or a failed
  probe makes the task refuse to start, with an error naming `cap_add: [NET_ADMIN]`
  (`NOUS_BROWSER_TASK_REQUIRE_EGRESS_FIREWALL=false` is the escape hatch). The step is skipped on the
  non-wrapper path (development, tests). At the start and end of every session all processes owned by
  `nous-browser` are killed, whether or not they carry the session marker.
  Remaining: unix-domain sockets (including abstract ones) and other non-IP channels are not filtered; the
  rule does not stop code that escapes to root; with the firewall switched off the proxy is again only
  Chromium's own configuration.
- `nous-browser` can read world-readable files in the container (the application code, and the workspace if
  its mode allows).
- Ports are not checked: an allowed host is reachable on any port Chromium allows.
- A plain-http redirect to an off-list host renders the proxy's 403 text at that URL until the URL check
  blanks the tab; nothing reaches the host.
- The executor guard still cannot see a consequential control that sits outside any form and carries a neutral
  label and accessible name (a `div` with an `onclick` that posts), nor look inside a cross-origin frame (it
  refuses to act there); those rely on the model layer.
- **Screenshots are isolated by convention only.** The outer agent receives `screenshot_path` and has
  `read_file`, so it can read the image. Nothing technical stops it; the rule is that it does not.

## Settings

| Variable | Default | Meaning |
|----------|---------|---------|
| `NOUS_BROWSER_TASK_ENABLED` | `false` | Register the tool. |
| `NOUS_BROWSER_TASK_MODEL` | `claude-sonnet-5-5` | Inner model; must be in `SUPPORTED_MODELS`. |
| `NOUS_BROWSER_TASK_MAX_STEPS` | `30` | Cap on model turns per call. |
| `NOUS_BROWSER_TASK_TIMEOUT_SECONDS` | `110` | Per-call wall clock, clamped to `NOUS_TOOL_TIMEOUT - 10`. |
| `NOUS_BROWSER_TASK_PROXY_PORT` | `18181` | Fixed loopback port of the egress proxy when Chromium runs as `nous-browser` (> 1024). |
| `NOUS_BROWSER_TASK_REQUIRE_EGRESS_FIREWALL` | `true` | Refuse to start the browser unless the kernel egress filter is installed and verified. |

**Enabling in prod.** Prod's compose file passes the environment to the container through an explicit
`environment:` list, so setting these variables in `.env` alone does nothing: enabling the tool needs
`NOUS_BROWSER_TASK_*` lines in that list as well. The repo `docker-compose.yml` now carries all six with real defaults (`NOUS_BROWSER_TASK_ENABLED=${NOUS_BROWSER_TASK_ENABLED:-false}`, model `claude-sonnet-5-5`, max steps `30`, timeout `110`); **prod's hand-edited compose needs the same six lines, plus `cap_add: [NET_ADMIN]` on the `nous` service**. Never use an empty default (`${VAR:-}`): the bool and numeric settings reject an empty string.

## Phase 2 (future work)

- Move the browser into a **separate desktop sidecar container** (its own network policy and egress
  allowlist at the network layer, no access to the Nous database or workspace), driven over a narrow RPC. The
  executor interface (`execute(name, input) -> blocks`) is the seam.
- Enforce the egress boundary in the kernel (its own network namespace or firewall rules), so code that
  escapes Chromium cannot open sockets around the proxy.
- A resumable confirmation flow: the owner approves the pending step (A2UI approval gate) and the task
  continues from saved state instead of stopping.
- Optionally the computer use toolset for whole-desktop tasks.
