# Changelog

All notable changes to Nous. Release notes for each version also live on the [GitHub releases page](https://github.com/tfatykhov/nous/releases).

# v1.4.0 — The Goal-Driven Agent

**37 commits, 37 pull requests, 419 files changed since v1.3.0** (November 14 → November 23, 2026)

v1.3.0 let Nous reach past its own tools. v1.4.0 lets Nous **work toward something over days**: the owner sets a goal, approves it, and Nous works on it in sessions of its own under limits the owner chose, while the owner watches from a Goals screen. Beside that, an outside judge grades Nous's decisions and finished task reports in shadow, the owner can set the limits of background work from the dashboard, and the routes that change things ask for proof that the owner is the caller.

---

## ✨ Highlights

- **Goals (F114).** A goal has a charter, a plan, a journal and limits. Once the owner approves it, a goal driver starts background sessions toward it, one task after another, and records what each session did. Ships dark.
- **The Goals screen.** A list of goals beside one pane: create a goal from a preset or custom limits, approve, pause, resume, stop, run a session now, add a note, change the budget or deadline. A panel says what the goal is doing now, and the sections fold.
- **Goals the owner can shape.** One session a day at a set time of day, session settings changed on an existing goal, outward tools allowed for one goal, and one goal at a time that may run any command until an end time. Each is its own setting, all off.
- **Limits and tools (F115).** The owner sets eleven limits of background work and switches a tool off for background work from the dashboard, with a record of each change. Ships dark.
- **An outside judge (F118).** Decisions Nous resolves, and the reports of finished background tasks, are graded later by a separate judge with fixed instructions, in shadow: nothing Nous does changes. A second judge, TypeSafe's Jev model, can run beside it, and the Autonomy screen shows both judges' numbers and a card for the second judge's settings.
- **What a finished job costs (F117).** Tokens per finished task, workflow step and goal session, and how many had an outside check; plus a status line that tells a background turn how many rounds and tool calls it has used.
- **Owner proof on routes that change things.** Routes and companion actions that change state or start work require the owner's secret or the login proxy's secret; chat and the inbound MCP server check for it under one setting that starts in warn mode.
- **Companion devices.** A phone pairs with a code the owner opens on the Integrations screen, and its device token counts as proof for the companion's actions.
- **An About screen** with the version, the build and what Nous runs on.

---

## 🎯 F114 — Goals *(off by default)*

### The store and the driver
- **Goals as rows:** a goal, its sessions (at most one open at a time) and an append-only journal that names who made each change. Limits are columns: Small, Medium and Large presets or custom values (#930, migration 113)
- **The goal driver** ticks every `NOUS_GOALS_TICK_SECONDS`: it journals each goal's channel, acts on the owner's stop and cancel, closes finished sessions, ends a chain whose task ran past its limit, pauses a goal on a stop condition (deadline, total budget, a stall of sessions with no plan step closed, errors in a row) and starts the sessions that are due (#932, #943)
- **A start is held** by the day's token budget, the day's session count, quiet hours, the day's job launches, the open sessions across goals (`NOUS_GOALS_MAX_ACTIVE_SESSIONS`) and free places in the task queue (`NOUS_GOALS_QUEUE_HEADROOM`). The owner's run-now skips the wake, the day's count and quiet hours, and nothing else (#932, #943)
- **Each session is a chain of tasks.** Its first task carries the charter, the plan and the journal, quoted as data and capped at 12,000 characters; the goal's own tools `goal_note` and `goal_plan` write the journal and the plan. A plan step closes only when a completed task or workflow of the session is cited (#946)
- **The owner's chains keep their room:** `NOUS_GOALS_RESERVED_SLOTS` continuation slots are never taken by a goal's chain, and a goal session's reports go to the goal's journal instead of the owner's chat (#946)
- **A host command,** `python -m nous.goals.seed`, creates, lists, shows and changes goals (#930)

### The Goals screen and its routes
- **Routes** under `/goals`: list, options, one goal, create, and one route per owner action (approve, pause, resume, confirm done, stop, cancel, run now, budget, deadline, note). A refusal by the store is a 409 with the store's sentence (#936)
- **The screen:** state in words and colour, spend against budget, deadline, the charter, the plan with its evidence, sessions and the journal. Actions that spend or end something show one sentence and wait for Yes (#936)
- **Follow-ups:** a goal that is gone is dropped and said once, the read-failure line says which read failed, and the bare address shows the goal last shown in the tab (#937)
- **What the goal is doing now** *(`NOUS_GOAL_ACTIVITY_ENABLED`)*: `GET /goals/{ref}/activity` and a panel with the current task or workflow by name, what it waits for, when the last tool call ended, counts of calls and failures, coding jobs started, the newest calls and links to the Work screen. It carries tool names, times, counts and outcomes, never a call's input, result or error text. The goal's sections fold (#959)
- **On a phone and a wide screen:** the activity panel, the goal forms and the costs panel were reworked to read and fit at both widths (#961)

### Pacing and session settings *(`NOUS_GOALS_PACING_ENABLED`)*
- **A goal that works once a day,** from a time of day in `NOUS_GOALS_TIMEZONE`: at most one timer session per local day, not before that time. A day a hold kept from starting before local midnight is owed and carried for one day at most (#957, migration 120)
- **Change how it works:** `POST /goals/{ref}/sessions` changes sessions per day, the shortest gap, job launches per day, quiet hours, the stall limit and the once-a-day time on an existing goal, with one journal row. The change applies from the next start (#957)

### Tools and commands for one goal
- **Outward tools for one goal** *(`NOUS_GOAL_TOOL_GRANTS_ENABLED`)*: the owner allows one goal's background sessions to use `send_file`, `send_email`, `browser_task` or a tool of a connected MCP server without asking, when the goal is created or later. Never the shell or Python. A grant skips only the class rule for that goal's own chain; every other check still applies (#949, migration 117)
- **A goal that may run any command** *(`NOUS_GOAL_COMMANDS_ENABLED`, up to `NOUS_GOAL_COMMANDS_MAX_HOURS`, 24 by default)*: chosen only on the create form, with a typed sentence and a number of hours, counted from approval. One goal at a time. It ends at the end time, on Stop, cancel, confirming done, or a pause the driver makes. It needs goals, the autonomy settings, the tool guard and per-goal tools on, and `NOUS_OWNER_ENTRY_MODE=enforce` with both owner digests set (#950, migration 118)

### The default goal prompt
- **How a goal's work runs:** a short built-in text, read in the first task of each goal session and in every follow-up turn, says how a goal session works: a task runs, then a follow-up turn reads its result and decides what runs next; only a follow-up turn starts tasks or workflows; a plan step is closed by citing a completed task or workflow of the session; the limits are shown to the follow-up turn (#967)
- **The owner can change it:** a card "Default goal prompt" on the Identity screen shows the text in use, whether it is the built-in one, and offers Save and Reset to the default, each asking first. At most 1,500 characters; a change applies from the next first task and follow-up turn with no restart. Routes `GET`, `PUT` and `DELETE /goals/prompt`. No migration and no setting (#967)

---

## 🎚️ F115 — Limits and Tools *(off by default)*

- **Eleven limits of background work** on a new Limits and tools screen: rounds of tool use per turn, tool calls per background task, a chain's decisions, follow-up tasks, levels, tokens and decisions in a row without progress, a chain's lifetime, the time to answer a question or proposal, and the time limits of a task. Each is a whole number inside a range. A change is checked, written with a record, and applied in place with no restart (`NOUS_AUTONOMY_SETTINGS_ENABLED`) (#947, migration 116)
- **Host ceilings and locks:** `NOUS_AUTONOMY_CEILINGS` caps a limit, and a ceiling wins over the owner's value. `NOUS_AUTONOMY_LOCKED_TOOLS` keeps a tool off whatever the owner sets (#947)
- **The tool guard** (`NOUS_TOOL_GUARD_ENABLED`, needs the settings flag): a tool the owner sets to Not allowed is not offered to background work, and a call to it is refused with the code `tool_guard`. The guard applies to background work only. If the saved settings cannot be read at start, background work is held from every tool until they can (#947)
- **Routes** under `/autonomy`, behind the owner guard (#947)

---

## ⚖️ F118 — The Outside Judge *(shadow, off by default)*

### The first judge
- **Decisions:** when an interactive turn resolves a decision as success, partial or failure, a judge with fixed instructions reads the decision's own fields and the calls, ledger rows and tasks of the session that recorded it, redacted and bounded, and gives `success`, `partial`, `failure` or `cannot_tell` with a confidence. The verdict is stored beside the agent's own grade in `nous_system.judge_verdicts`; no grade, calibration, strategy card or reply changes (`NOUS_OUTSIDE_JUDGE_MODE=shadow`, `NOUS_OUTSIDE_JUDGE_MODEL`, `NOUS_OUTSIDE_JUDGE_DAILY_CALLS`, default 100) (#958, migration 121)
- **Task reports** *(`NOUS_OUTSIDE_JUDGE_REPORTS_ENABLED`)*: the report of a background task that finished as completed is judged as `supported`, `not_supported` or `cannot_tell`, from what the task did up to its completion. Reports use at most half the daily limit. The finish checks' verdict is kept on the row for comparison and is not shown to the judge (#963)
- **An hourly pass** queues what a restart lost, and decisions graded by the owner, the review route or an automatic check, which are judged without that grade. At most five a pass, and nothing past half the daily limit (#963)
- **Reply limit:** the built-in judge's reply may be up to 1,500 tokens (#964)

### The second judge, Jev
- **Beside the first** *(`NOUS_OUTSIDE_JUDGE_SECOND=jev`, with `TYPESAFE_API_KEY` and `NOUS_OUTSIDE_JUDGE_JEV_MODEL`, default `jev-1.14.0`)*: the same redacted, bounded record is also sent to TypeSafe at `api.typesafe.ai`, which answers with a verdict and a confidence. Each judge has its own rows and its own daily limit, and a failure of the second leaves the first and the agent as they were (#960)
- **Settings on the Autonomy screen** *(`NOUS_OUTSIDE_JUDGE_SETTINGS_ENABLED`)*: a card "A second opinion on decisions" switches the second judge on and off, sets its model and daily limit, and saves, replaces, removes and checks its API key, sealed with the MCP credentials' key file. Each change applies at once. A change that starts the sending, or raises the limit while it sends, needs the owner's yes, asked with a sentence that says what is sent to which company (#962, migration 122)
- **The hourly pass for decisions:** with the reports setting on, the second judge also runs the hourly pass over graded decisions, at most five a pass, at its own name, version and daily limit. The confirmation sentence says so. A request the second judge has sent when it is replaced or Nous stops now ends with its row, and `docker-compose.yml` gives the `nous` service 30 seconds to stop. Jev's instruction version moves from `jq-2` to `jq-3`. No migration and no new setting (#968)

### The panel
- **"The outside judge"** on the Autonomy screen, read-only, above the second judge's card: decisions judged, agreement with outcomes, the share it could not tell, today's calls against the limit, a verdict bar, agreement by who graded, where the judge disagreed (each decision named by its own text), task reports and the hourly pass, the second judge and where the two judges differed, and the cost (#965)

---

## 📉 F117, F113 — Finish Checks, Loop Numbers and a Status Line *(off by default)*

- **Two fixed finish checks** *(shadow, `NOUS_FINISH_CHECKS_MODE`)*: when a background task finishes with a validated report, one check looks for a file the report says was written and that does not exist, and one for a task whose every writing or sending call failed while the report names no failure. The verdict (`clean`, `flagged` or `not_checked`) is recorded as labels only in the task row's metadata. Nothing else changes (#954)
- **What a finished job costs** *(`NOUS_LOOP_NUMBERS_ENABLED`)*: `GET /dashboard/loop-numbers` and a panel on the Autonomy screen count, for tasks, workflow steps and goal sessions, the finished units, the tokens of everything that ended over the finished ones (cached input is not counted), and the share with no outside confirmation (#956)
- **A status line for background turns** *(`NOUS_STATUS_LINE_ENABLED`)*: each model call of a task, workflow step, scheduled run or continuation turn carries one line saying the round, the tool calls used of the limit, what happens after the last round and the task's changes so far. It exists in the outgoing request only, never in the saved conversation, and each line once sent is sent again unchanged on later calls (#956)

---

## 💓 F116 — Stale Pending-Action Findings *(off by default)*

- **The record:** migration 119 adds `heart.pending_resolutions`, one row per fact or episode that a filter, the `learn_fact` tool or the owner closed, with the reason (#953)
- **Three filters for the heartbeat's self-initiated check,** each `off`, `shadow` or `on`. Kind (`NOUS_HEARTBEAT_PENDING_KIND_MODE`) leaves out rule, preference and person facts and the heartbeat's own triage notes. Age (`NOUS_HEARTBEAT_PENDING_AGE_MODE`) hides old status lines, old facts and ongoing episodes and facts that read as a question, with `NOUS_HEARTBEAT_PENDING_SNAPSHOT_TTL_HOURS`, `..._FACT_MAX_AGE_HOURS` and `..._EPISODE_MAX_AGE_HOURS`. Supersession (`NOUS_HEARTBEAT_PENDING_SUPERSEDE_MODE`) hides superseded or inactive facts. In shadow the counts by reason are recorded and every finding still surfaces. An error keeps the item (#955)

---

## 🛡️ Owner Proof

- **Owner decisions need the owner's proof:** intention decisions, heartbeat configuration, search weights, undo restores, and the companion's approve, reject, answer, cancel and approval-choice actions require the owner's secret or the login proxy's secret (which must come with a JSON content type). The API holds only the SHA-256 of each secret (#934)
- **More routes:** decision review, fact, profile card, censor and identity writes, subtask cancel, schedule delete and run, heartbeat operations and the sleep trigger, rubric changes and the reads of stored prompt, response, message and tool-result text require the same proof. A test checks that every route of the app declares its check (#944)
- **Chat and the inbound MCP server** *(`NOUS_OWNER_ENTRY_MODE`: `off`, `warn` or `enforce`, default `warn`)*: `warn` logs and counts a call with no proof, by the class of its session, with one Telegram line a day; `enforce` requires the owner's or the proxy's secret on the chat routes, and `/mcp` takes the owner's secret or its own key (`NOUS_MCP_SERVER_SECRET_SHA256`). `POST /schedules` and the companion verb `app.act` require proof in every mode. The Telegram bot sends the owner's secret; `python -m nous.ping SESSION_ID MESSAGE` posts one chat message from the bot's container (#935)
- **Logs:** the HTTP client's request lines are redacted in the log record (#935)
- **Fetches of a URL the model supplies** go through one checked path: they connect only to public internet addresses that were checked, check every redirect hop, bound the body and use one deadline. `web_fetch`, `learn_skill`, the browser egress proxy and the MCP client use the same rule (#938)

---

## 📱 Companion Devices

- **Pairing:** the owner opens an 8-character code (valid five minutes), the device redeems it once and receives a token that the server keeps only as a SHA-256. A paired device's token counts as proof for the companion's card actions and for nothing else; revoking takes effect on the next call (#940, migration 115)
- **The Android app** pairs from its Settings, seals the token with a Keystore key and sends it only to the server it paired with, only over https (#941)
- **A Devices section** on the Integrations screen shows a code once with a countdown, lists devices with when each was paired and last seen, and revokes a device after asking (#945)
- **First push registrations** of one installation made at the same moment no longer fail, and two new installations at the cap are counted correctly (#948)

---

## 🔌 MCP Client and Shell

- **Sign-in to a server that answers without credentials** and publishes its resource metadata at the well-known address: the client discovers its authorization server as it does after a 401. A server with no credential says "No key stored and not signed in." where sign-in is offered (#951)
- **The lineage shell names its rule:** a refused command says which rule refused it, and the policy-violation event gains a `reason_code`. More read-only forms are accepted: combined short git flags, bare `git remote`, `git branch` list forms, `git ls-tree`, `git ls-files`, `git -C` the repository top, and `cut` (#931)

---

## 🖥️ Dashboard

- **About:** a new screen at the end of the System group with the version, the build (commit and build time, when the deployment sets `NOUS_BUILD_COMMIT` and `NOUS_BUILD_TIME`), how long Nous has been running, the agent's name and configured models, the Python, PostgreSQL and pgvector versions, the database schema (the highest migration applied), and links to the release notes and the source. Route `GET /dashboard/about` (#966)
- Goals, Limits and tools, the Autonomy screen's judge and costs panels, and the Devices section are described above

---

## 🧪 Tests

- The decode-slot tests wait until every slot is back, not until one is free (#942)
- The classification time bound allows for a slow shared runner (#939)

---

## 📊 By the Numbers

| | v1.3.0 | v1.4.0 |
|---|---|---|
| Migrations (highest number) | 112 | 122 |
| Pull requests since the previous release | 17 | 37 |

---

## Upgrade Notes

- **Migrations 113 and 115 to 122** run at startup (number 114 is not used).
- **Dependencies:** `tzdata>=2024.1` is added, for the time zones of goal pacing. Rebuild the image, or reinstall.
- **Behaviour that changes without a flag:**
  - the routes listed under Owner Proof require the owner's or the proxy's secret. With neither `NOUS_OWNER_ROUTE_SECRET_SHA256` nor `NOUS_MCP_CLIENT_UI_SECRET_SHA256` set to a digest, they refuse every call (403) and the companion's owner actions answer `OWNER_PROOF_REQUIRED`. The login proxy adds its secret, so the dashboard sends nothing new; the README says what the dashboard needs without the proxy (#934, #944);
  - `NOUS_OWNER_ENTRY_MODE` defaults to `warn`: calls to chat and `/mcp` with no proof are logged and counted, and one Telegram line a day reports the counts. Set `enforce` to refuse them; the Telegram bot already sends its secret (#935);
  - `POST /schedules` and the companion verb `app.act` need the owner's or the proxy's secret in every mode (#935);
  - from the Android companion, the five owner actions are refused until the phone is paired (#934, #941);
  - a fetch of a URL the model supplies is refused unless the host resolves only to public addresses (#938);
  - the HTTP client's request lines are redacted in the log (#935);
  - the lineage shell's refusal text names a rule; the dashboard has About, Goals and Limits and tools entries and a Devices section on the Integrations screen; the Identity screen's goal prompt card appears only with goals on.
- **To use goals:** set `NOUS_GOALS_ENABLED` (it also needs continuation, intentions, the result inbox and subtasks). Per-goal tools, commands, pacing and the activity panel each have their own setting; per-goal tools also need the autonomy settings and the tool guard; `NOUS_GOAL_COMMANDS_ENABLED` is forced off unless goals, the autonomy settings, the tool guard and per-goal tools are on and `NOUS_OWNER_ENTRY_MODE` is `enforce` with both owner digests set. Pacing needs `NOUS_GOALS_TIMEZONE`.
- **To use the outside judge:** set `NOUS_OUTSIDE_JUDGE_MODE=shadow`. The second judge also needs `NOUS_OUTSIDE_JUDGE_SECOND=jev` and `TYPESAFE_API_KEY`, or `NOUS_OUTSIDE_JUDGE_SETTINGS_ENABLED` and the dashboard card; **turning it on sends a redacted copy of each decision to TypeSafe**.
- **Everything marked off** stays off until its setting is changed. This release's `docker-compose.yml` lists the new settings with their defaults, so each can be set from `.env`; a deployment with its own compose file needs a line for each. It also gives the `nous` service a 30 second stop time (#968).
- `pyproject.toml` and `nous.__version__` now read `1.4.0`.

---

# v1.3.0 — The Connected Agent

**17 commits, 17 pull requests, 115 files changed since v1.2.0** (November 12 → November 14, 2026)

v1.2.0 made Nous's work legible. v1.3.0 lets Nous **reach past its own tools**: it can use the tools of external MCP servers that the owner adds and allows, from a new Integrations screen, with stored keys, sign-in and a switch for images. Beside that, two ways for text the user never said to be stored as theirs are closed, and Nous starts to record which sessions read content from outside.

---

## ✨ Highlights

- **An MCP client (F112).** The tools of an external MCP server appear to the agent as `mcp__<server>__<tool>`. The owner adds a server, sees every tool before anything is stored, and allows tools one by one. Ships dark.
- **The Integrations screen.** A list of servers beside one pane: add a server, check it, allow or refuse each tool, switch it off, refresh its tools, remove it.
- **Credentials for a server.** An API key or other headers, stored encrypted; or OAuth sign-in with PKCE, by dynamic registration or a client ID metadata document.
- **Images from a server reach the model.** A switch per server, off: a screenshot from a desktop-control server arrives as an image, decoded and rebuilt from its pixels first.
- **Only the user's words count as the user's.** Compaction no longer feeds a model-written summary to the fact extractor as the user, and the inbound `nous_teach` tool can no longer name a source that admission trusts.
- **Outside-content tagging, in shadow.** Nous records per session whether it read content from outside (web, documents, network reads, spawned work, MCP tools) and logs how many facts would be tagged. Off by default.
- **A check of subtask reports, in shadow (F113).** A background subtask's validated report is compared with what the subtask did, and the result is recorded as an event.
- **Consolidation as a sleep journal.** A chart of the last 30 sleeps, one sentence per sleep, and a report that opens in place.

---

## 🔌 F112 — MCP Client *(off by default)*

### Core
- **Tools of external servers** as `mcp__<server>__<tool>`, over streamable HTTP, registered at boot from stored rows. With `NOUS_MCP_CLIENT_ENABLED` off, nothing of the client is imported and no tool is registered (#913, migration 109)
- **Where they may run:** interactive turns and approved actions; never through Nous's own inbound MCP server; in background work (subtasks, DAG nodes, schedules, heartbeats, continuations) only for a server the owner marked for it. A name the client has not registered is refused everywhere (#913)
- **Treated as outside:** every `mcp__` tool counts as an external side effect and as outside content (#913)

### Adding and changing servers
- **Routes** under `/integrations/mcp`: list, probe (connects once, shows what would be registered, writes nothing), create (the approval), change, refresh and remove. A change applies without a restart (#915)
- **Refresh is conservative:** a new tool arrives not allowed, a vanished one stops being allowed, and a changed definition keeps its permission and is reported (#915)
- **Behind the login proxy:** every route but one needs a secret header whose SHA-256 is in `NOUS_MCP_CLIENT_UI_SECRET_SHA256`; unset, they answer 503. The exception is the client ID metadata document, which a provider fetches with no credential (#915, #923)
- **Host-side ceilings,** both optional: `NOUS_MCP_CLIENT_PINNED_SERVERS` limits which servers and tools can register, `NOUS_MCP_CLIENT_BACKGROUND_SERVERS` which servers background work may use (#915)

### Credentials
- **Stored API keys and headers,** sealed with AES-256-GCM under a key file on the host (`NOUS_MCP_CLIENT_KEY_FILE`). A sealed row is bound to the agent, the server's name, its address and the kind of credential. A stored value is never returned by a route or written to an audit event. A server that answers 401 or 403 is shown as refused, with no retry loop (#917, migration 110)
- **OAuth sign-in:** authorization code with PKCE (S256), discovery from the server's 401, a dynamically registered client or the owner's client id. Tokens live in the same sealed row. A failed refresh marks the server as needing sign-in; a tool call never starts a login (#919)
- **Client ID metadata document:** for providers that take one instead of registration. `NOUS_MCP_CLIENT_ID_METADATA_URL`, empty by default. When Nous serves the document, the proxy must let that one path through with no sign-in (#923, #924)

### Reach
- **An `http://` address** only for a host named in `NOUS_MCP_CLIENT_ALLOW_PRIVATE_HOSTS`. Sign-in stays https only (#925, migration 111)
- **Images:** for a server whose images switch is on, the PNG and JPEG images of a tool result go to the model after its text. Each is decoded, scaled to at most 1568 pixels a side and encoded again from its pixels alone. Off for every server until the owner turns it on (#928, migration 112)

### The Integrations screen
- **Add a server:** name and address, "Check server" lists every tool as it would be registered or why it would be skipped, each with an allow box that starts empty (#916)
- **Per server:** switch off and on, allow in background work, refresh tools, remove, a key or sign-in. **Per tool:** allow or don't allow (#916, #917, #919)
- **A list of servers beside one pane,** one line per tool that opens to its description and schema, with a search field and an allowed filter from nine tools up (#926)

---

## 🛡️ Memory Integrity

- **Compaction:** the summary of an earlier compaction is a user-role message that the model wrote. It reached the fact extractor as `User:`; it is now left out. A tool-result block, should one ever reach the extractor, is labelled as tool output (#910)
- **`nous_teach`:** the caller's `source` is kept as a description behind an `mcp:` prefix, so it can never equal a source that admission stores without scoring. A taught fact that admission rejects is now answered with "Fact not stored" (#911)
- **Outside-content tagging** *(shadow, off by default)*: tools are classified by whether they read outside content, the session's list is recorded at dispatch and carried on the compaction, session-end and summary events, and the extractors log how many facts would be tagged. Nothing stored changes. `NOUS_OUTSIDE_CONTENT_TAGGING_MODE` (#912)

---

## ✅ F113 — Subtask Report Check *(shadow: an event only)*

- Once a background subtask's validated report is persisted, the claim verifier checks its summary and findings against the session's ledger and emits one `subtask_report_claim_check` event. No score, outcome, retry or prompt changes. It has no setting of its own: it runs where report checking does (`NOUS_CLAIM_VERIFICATION_REPORTS_ENABLED`, on by default) (#914)

---

## 🖥️ Dashboard and Companion

- **Consolidation is a sleep journal:** a chart of the newest 30 sleeps (facts, graph links or duration), one sentence per sleep under its day, and a report in plain words that opens in place (#922)
- **Companion:** the list of live surfaces moves from a row of chips to a drawer on a phone and a sidebar on a wide screen, and the top bar stays in place (#909)

---

## 📚 Docs

- The quickstart lists the four containers compose starts, `nous-scorer` among them (#921)
- The order for turning on the client ID metadata document (#924)

---

## 📊 By the Numbers

| | v1.2.0 | v1.3.0 |
|---|---|---|
| Migrations | 108 | 112 |
| Pull requests since the previous release | 112 | 17 |

---

## Upgrade Notes

- **Migrations 109 to 112** run at startup.
- **Dependencies:** the `runtime` extra now needs `mcp>=1.30`, `cryptography>=42` and `Pillow`. Rebuild the image, or reinstall with the `runtime` extra.
- **Behaviour that changes without a flag:**
  - from a session's second compaction on, the fact extractor no longer reads the previous summary (#910);
  - a fact taught through `nous_teach` with a source such as `user_stated` is scored like any other `mcp` fact (#911);
  - a taught fact that admission rejects is answered with "Fact not stored" instead of an id (#911);
  - a `subtask_report_claim_check` event is emitted for each validated background subtask report, on a default install (#914);
  - the dashboard has an Integrations entry; with the MCP client off, the screen says so;
  - the Consolidation screen and the companion shell look different.
- **To use the MCP client:** set `NOUS_MCP_CLIENT_ENABLED`, and `NOUS_MCP_CLIENT_UI_SECRET_SHA256` with a proxy that adds the secret header to the requests it has authenticated. Stored keys and sign-in also need `NOUS_MCP_CLIENT_KEY_FILE`, a file mounted read-only into the container. Sign-in also needs `NOUS_A2UI_PUBLIC_BASE_URL` set to an https address: it redirects to `<that address>/integrations/mcp/oauth/callback`. See `docs/reference/environment-variables.md`.
- **Everything marked off** stays off until its setting is changed. The repository's `docker-compose.yml` lists the container's environment explicitly and passes none of the new settings, so each one needs a compose line as well as a `.env` line.
- `pyproject.toml` and `nous.__version__` now read `1.3.0`.

---

# v1.2.0 — The Legible Agent

**112 commits, 112 pull requests, 776 files changed since v1.1.0** (October 22 → November 12, 2026)

v1.1.0 made Nous accountable for the work it does on its own. v1.2.0 makes that work **legible**: the owner can see what Nous did and why, turn by turn and call by call; memory serves fewer wrong rows and says where each one came from; and every dashboard screen has one calm look, with memory drawn as the connected web it is.

---

## ✨ Highlights

- **The agent work view (F108).** Every tool call is recorded with the reason the model gave for it, every model call with what it was for and what it cost, and one record per turn. Two new screens read them: Work (sessions and a session's trace) and Model calls.
- **Tool notes (F108).** Nous detects the tool mistakes it repeats, reviews them into short capped notes during sleep, and reports guard patterns to the owner. Ships dark.
- **A memory pass, end to end.** Three waves of fixes and flags on the read and write paths: recall stops re-serving, mis-ordering and dropping rows; writes fail closed on uncertain evidence; facts can carry when they were said and the span they hold true for; a cross-encoder can order what is served.
- **Memory write health.** A daily rollup, six rules and a latched hard-delete detector, shown on the dashboard and the companion with an acknowledge action.
- **Frame continuity (F110).** The turn log records why each frame was chosen. A short reply can keep the frame of the turn it answers, within a session and across the session roll. The new behaviour ships dark.
- **An obligation ledger (F111).** Commitments Nous makes are recorded, and a promise gate watches replies for them in shadow mode. The stage-2 classifier and the overdue watcher ship dark.
- **`db_query`.** A read-only database tool on its own Postgres login, so Nous can answer questions about its own state without write access.
- **One look, and a graph that looks like memory.** All 18 dashboard screens moved to a light chart-paper look. The Graph screen draws memory as one connected, brain-like mass in three dimensions.

---

## 🔎 F108 — Agent Work View

### Recording *(flag-gated)*
- **Tool calls:** every call with the model's reason, including refused, gated and failed calls, and one record per turn (#870)
- **Model calls:** every call with its purpose and cost (#871); full prompt and response text kept 7 days (#872)
- **One recorder:** the model-call table is the single recorder, the context endpoints read it, and the 15 recording bugs found on the way have regressions (#873)
- **Retention:** turns, tool calls and model-call numbers 30 days; model-call text and a call's full result 7 days (#882)

### Screens
- **Work:** a sessions list and a session trace (#875, #878). A continued session sorts first, shows its latest message, and search covers every turn (#885)
- **Model calls,** with a Cache tab and Patterns (#877, #879)
- **Navigation** regrouped into six groups; memory health in tabs, a Changes tab, and Activity split (#880)

### Tool notes *(off by default)*
- Repeated tool mistakes are detected, reviewed into capped notes, and guard patterns go to the owner (#881)
- A Tool notes procedure with keep-or-retire (#884); the review sees how each offered tool is called (#887)

---

## 🧠 Memory

### Retrieval
- **Recall selection** stops re-serving, mis-ordering and dropping rows (#833); recall failures reach the retrieval trace instead of looking like empty results (#827)
- **Own conversation left out:** `recall_deep` and the pre-turn context no longer return the conversation they are called from (#854, #857)
- **HNSW horizon:** hybrid vector legs widen it so filtered searches return the rows they ask for (#824)
- **Pre-turn context** keeps fresh decisions, bounds fact pinning and traces why items were dropped (#829); it can search on the user's words or the prompt's task instead of a keyword bag *(flags, off)* (#834)
- **Precision controls** for `recall_deep`: a leg cut, a top-k with type floors and a section order *(off)* (#861)
- **Cross-encoder order-only:** the cross-encoder can order Heart's served rows without selecting them *(off)* (#865)
- **Spreading activation v2 (F105)** and **associative edges v2 (F106):** flagged changes, all off, with a replay harness (#808, #809, #841)

### Time
- **Facts record when they were said and the span they hold true for** *(off)* (#839)
- **Facts are read by their date span and shown in date order:** date leg, resolver and rendering *(flags off)* (#848)
- **Supersession can follow event time:** overlapping spans resolve by start date, disjoint spans coexist *(off)* (#847)
- **Merged facts** can show the dated facts they were built from (#843, migration 098)

### Write path
- **Fail closed:** fact writes and merges no longer supersede on uncertain evidence (#831)
- **Profile facts are learned again** (#832), and a bounded **profile card** is written by a sleep phase from cited profile facts *(off)* (#836, #840)
- **Deactivation records why** on the row, and the stale scan keeps person and preference facts (#828)
- **Leaked tool-call markup** is stripped on the write paths, with a script to clean stored rows (#855)
- **Admission confidence** can score grounding as ROUGE-L precision *(off)* (#867)
- **Embeddings** fail fast on read paths within a budget; write-side failures are logged and recoverable (#826)
- **Episodes** stay per session (#825); sleep consolidation remembers refusals and a sleep run starts once (#835)

### Write health
- A daily rollup, six rules and a latched hard-delete detector (#858), on the dashboard and the companion with an acknowledge action (#859)

### Graph
- Densifier edges follow their relation's direction and edges to missing rows are pruned (#846)
- A deliberated decision is linked again on its final description (#851)

---

## 🧭 F110 — Frame Continuity

- The task frame matches write, draft, spec, send, deliver, add and edit (#889, migration 105)
- The turn log records why the frame was chosen, the critic's frame and whether the turn failed (#890, migration 106)
- Frame selection and the critic see the message the user is replying to (#891)
- The frame carries over short replies within a session, for at most three turns *(off)* (#892)
- A new session's first turn sees the channel's last reply *(off)* (#893)
- The `db_query` reader reads the turn log's numbers, never its text (#894)

---

## 🤝 F111 — Obligation Ledger

- An obligation ledger, a commit tool and a promise gate in shadow mode (#897, migration 107)
- An overdue watcher and a stage-2 classifier, both off, with detector fixes (#906, migration 108)

---

## 🛠️ Tools and Runtime

- **`db_query`:** a read-only database tool on its own Postgres login (#816); it logs the tables a statement reads (#868)
- **Tool descriptions** say when to use each tool; old names resolve through aliases; `recall_recent` and `resolve_decision` merge into `recall_deep` and `resolve_decisions` (#822, #823)
- **SmartCompress:** cuts the model can undo, line order kept, every cut marked, exception lines and the real last line kept (#818 to #821)
- **Lineage shell:** owner-listed write dirs, read programs and refused-command heads (#817)
- **DAGs:** wait nodes and typed waits (#813); the PR check can wait for the Claude Code job that opens the PR (#799)
- **Continuation turns** that wait for the owner: the stall gate skips asks (#814)
- **F103:** a flag-only prompt-injection and secret screen for `web_fetch` and `browser_task` (#805)
- **F107 guards:** a run-level guard for scheduled script jobs, shadow mode and a Guards screen, guards for heartbeat dynamic checks (#810 to #812)

---

## ⏱️ Scheduling
- **F101: schedule concurrency and catch-up policies** (inspired by Paperclip routines). Each recurring schedule has a `concurrency_policy`: `coalesce_if_active` (the default), `skip_if_active` or `always_enqueue`. It also has a `catch_up_policy`: `skip_missed` (the default) or `enqueue_missed_with_cap`. Behaviour changes with the defaults: a fire that comes due while the previous run is still active is now coalesced and recorded, where before it was deferred and fired late. The previous run can be the run's subtask or a DAG it created, and a run that is only *pending* counts too, so it now uses up the slot. A `max_fires` schedule counts only the runs that started. `always_enqueue` cannot be combined with `continuation_turns`. Stale runs never block a schedule. `schedule_task` takes the policies and `list_tasks` shows them. `POST /schedules/{id}/run` starts a manual run, which obeys the policy. Migration 091 adds the columns and `execution_dags.schedule_id`. The new behaviour sits behind `NOUS_SCHEDULE_POLICIES_ENABLED` (default on); set it to false for the old behaviour.

## 📈 Learning From Experience
- **Procedure versioning and create-time dedupe (F102).** Every procedure body write is a new version in `heart.procedures.version` and `heart.procedure_versions` (migration 092; existing rows backfilled as version 1). `update_body`, `append_notes` and the new `restore` take an optional `expected_version` and refuse a stale write with `ProcedureConflictError`. `store()` replays an `idempotency_key`, and with `NOUS_PROCEDURE_DEDUPE_ENABLED` (default on) merges an auto-learned procedure into a same-named or near-duplicate (cosine ≥ 0.85) auto-learned one instead of inserting a duplicate. A one-off dry-run report, `scripts/diag/procedure_dedupe_report.py`, lists the existing duplicate clusters. Adopted from paperclipai/paperclip's runner-created skills.

---

## 🖥️ Dashboard and Companion

- **One look: chart paper.** All 18 dashboard screens have a new look: a light chart-paper ground, white rounded cards, an Atlantic-blue sidebar, Archivo and Red Hat Mono, frame colours with their names in plain words, and no symbol icons. The dark look and its switch are gone; a stored choice or a `?look=` link is ignored. The companion's default theme matches it, and its old theme stays as `nous-dark`. (#898 to #904)
- **Graph in three dimensions.** `GET /dashboard/graph` chooses memories instead of links: it grows outward from the newest memories along their strongest links and returns every link among them, with chunks left out unless asked for. The screen draws them as one brain-like mass; a tapped memory sends activation along its links. The 3D library loads only on that screen, and Cytoscape is removed. `limit` now counts memories (1 to 3000), and the response's stats changed. (#886, #907)
- **Video in the companion:** workspace files, https links and YouTube or Vimeo embeds (#896)

---

## 📏 Measurement and Evaluation

- A served-precision eval library for `recall_deep` and the pre-turn entry points (#856)
- A HaluMem harness that replays through the production write path (#866)
- A tracked BEAM harness that ingests through the production write path (#842)

---

## 🔧 Fixes

- The critic's stable prompt goes in the cached system block (#883)
- `db_query` formats a result off the event loop (#876)
- The memory write-health check gets 120 seconds for its cold first run (#862)
- A fact's novelty is 1.0 when nothing comparable exists (#838)
- The pre-turn recency resolver tags a chain of three or more facts the same in any order (#849)
- Settled task scores read a window that ends 14 days ago (#798)
- Claim verifier hardening (#796, #802); the merge gate reads inline Codex P1 comments on the current head (#795)

---

## 📊 By the Numbers

| | v1.1.0 | v1.2.0 |
|---|---|---|
| Migrations | 090 | 108 |
| Pull requests since the previous release | 129 | 112 |

---

## Upgrade Notes

- **Migrations 091 to 108** run at startup.
- **Database roles:** `db_query` reads through its own Postgres login, which `scripts/ops/db_roles.sql` creates; the tool is offered only when `NOUS_READER_DB_PASSWORD` is set. See `docs/reference/database-roles.md` before running the script.
- **Behaviour that changes without a flag:**
  - schedule policies (`NOUS_SCHEDULE_POLICIES_ENABLED`, default on) coalesce a fire that comes due while the previous run is active;
  - procedure dedupe (`NOUS_PROCEDURE_DEDUPE_ENABLED`, default on);
  - `recall_recent` and `resolve_decision` are aliases of `recall_deep` and `resolve_decisions`;
  - `GET /dashboard/graph`: `limit` counts memories, and the stats fields changed (see `docs/reference/rest-api.md`).
- **Everything marked off** stays off until its flag is set. With an explicit `environment:` list in `docker-compose.yml`, a new setting needs a compose line as well as a `.env` line.
- **Dashboard:** the dark look is gone. The bundle is rebuilt by `docker compose build`.
- `pyproject.toml` and `nous.__version__` now read `1.2.0`.

---

# v1.1.0 — The Accountable Agent

**129 commits, 129 pull requests, 685 files changed since v1.0.0** (September 5 → October 22, 2026)

v0.1.0 was "The Thinking Agent", v0.2.0 "The Remembering Agent", and v1.0.0 the stable version, with the architecture complete and running in production. v1.1.0 makes Nous **accountable for the work it does on its own**. When it says it did something, that claim is checked against a durable record of the calls that really ran. A send reaches its recipient at most once, and a risky step can wait for the owner's answer. Its background work is scored by a separate, deterministic scorer it cannot write to. It records why it started each piece of work and follows up when the results arrive, while outward actions go to the owner as proposals. It learns lessons from its recurring jobs, and when a job keeps failing it checks the job, tries a fix and keeps the fix only if later runs fail less.

---

## ✨ Highlights

- **A harness for unattended work.** A durable execution ledger records side-effecting calls. Every tool's risk class is declared once, sends are idempotent, and claims are verified against the calls whose arguments show the effect. Plans can park on an owner approval and resume when it arrives, and an undo journal snapshots the before-state of what the agent changes.
- **Intentions and continuation (F099).** Every spawn records why it was started. When the results come back, a continuation turn follows up, and an outward action becomes a proposal the owner approves from Telegram, the companion or REST. Ships dark.
- **Scored by a separate scorer.** Each finished background subtask gets one deterministic score from a separate scoring job, built to run under its own database role so the agent can read its scores but not change them. Two things count as verification: a pull request Nous opened that the owner merged, and weekly owner spot-checks, which earn credit for agent-written checks of each kind. The dashboard's new Autonomy view shows the scoreboard.
- **Procedure learning that has to earn it.** A lesson gate makes every new procedure cite its evidence and refuses a lesson an existing procedure already teaches. Recurring background jobs become a new source of lessons, and a job that keeps failing is checked and fixed, with the fix judged on later runs.
- **A browser of its own (F100).** `browser_task` runs a separate agent loop that drives headless Chromium. The loop runs behind an egress proxy and a kernel-level network filter, and the caller gets back only a bounded summary. Ships dark.
- **Android companion (F097).** A native Android beta with server push. Companion assets now survive a rebuild.
- **Strategy cards.** Lessons distilled from resolved decisions, found by their closeness to the query and held to the same floor as procedures. Ships dark.
- **Faults surface instead of hiding.** A fault detector for faculties that fail silently, and an event-loop watchdog that dumps every stack and exits on a stall. Background loops now survive cancellations nobody sent them.

---

## 🧾 Harness: Honest Execution

### Harness Phases 0–3
- **Phase 0:** an SMTP timeout, and `send_email` classed as external
- **Phase 1a:** an `ExecutionContext` per turn; tools called outside the offered set are measured, with warn or enforce modes
- **Phase 1b:** a durable execution ledger for side-effecting tool calls (migration 074)
- **Phase 2a:** tool classes declared once (`none` < `write` < `external` < `irreversible`, plus spawns), with a per-context policy at the dispatch choke point (warn by default)
- **Phase 2b:** idempotent sends, so a logical send reaches its recipient at most once (migration 075)
- **Phase 2c:** evidence-aware claim verification: a claim counts as backed when any capable call's arguments show the effect
- **Phase 2.8:** a compensation registry that can undo a successful call, and a proceed-default for approvals (migration 080; off by default)
- **Phase 3:** park-and-resume, where approval nodes wait durably on a person's answer (migration 076; off by default)

### Ledger, Undo and Visibility
- **Harness tab** on the dashboard: approvals and the durable ledger
- **Ledger detail** *(opt-in)*: each call's redacted command and output, shown on the Ledger page (migration 086). An error row keeps a redacted 300-character excerpt.
- **Passive undo journal** (on by default): before-state snapshots of the files, schedules and checks the agent changes, with an owner-run restore
- `write_file` keeps the file's owner, bounds its path lock and snapshots only what can be reverted; `read_file` pages contiguously and holds its bounds

---

## 🔁 F099 — Intentions and Continuation

Every background spawn carries its reason and its result, and the agent can follow up on its own work under the owner's control. All of it lands dark behind `NOUS_INTENTIONS_ENABLED` and `NOUS_CONTINUATION_ENABLED`.
- **Phase 0:** carry the reason (the Plan decision on subtasks, a DAG's original request) and carry the result (callbacks and DAG check nodes get the check's findings)
- **Phase 1:** intentions recorded, one row per spawn (migration 083)
- **Phase 2:** an enforcement substrate that gives internal-only lineages a narrowed tool set. Then a continuation store (claim, gate, fenced commit, lease, TTL, repair) and a continuation runner (turn, loop, owner push, wake pass). Then proposals and owner actions: `propose_action`, deterministic approval, Telegram buttons and REST. Then the owner's cancel, with no root that can hang (migrations 084, 085).
- **Phase 3:** proposal, question and active-intention cards on the companion; an Intentions dashboard with tree and metrics (`GET /dashboard/intentions`); continuation decisions graded from lineage signals for calibration
- **Lineage work:** a lineage shell with an allowlisted environment that reads only inside its workspace, and build and test commands (git reads, pytest, ruff, make)
- **Gate hardening:** approved-action status is reported truthfully; heartbeat triage may not act outward; subtask reports and continuation notes are checked against their tool log; a masked-failure heuristic unwraps `-c`/`eval` strings
- Quiet hours do not hold an owner push while the owner is chatting

### F098 — Result Inbox
Subtask and DAG results are routed to the conversation by a stable channel through one inbox table, claimed exactly once at pre-turn, and a result-memory writer keeps what they found (migrations 081, 082). Phases A and C land dark; Phase B was superseded by F099.

---

## 📏 Scoring and the Autonomy View

- **Task scores:** one deterministic score per finished background subtask, with count-only roll-ups for DAG runs and intention chains, in a `scoring` schema of its own (migration 087). They are written only by `python -m nous.observability.task_score_job`, which the new `nous-scorer` compose service runs every 15 minutes.
- **Scorer heartbeat** (migration 088), so a scorer that stopped is visible
- **Separate database roles** for the application, the scorer and the administrator (`scripts/ops/db_roles.sql`, [database roles reference](docs/reference/database-roles.md)), plus an owner gate on the scoring code in CI
- **Scorer v2–v5:**
  - a subtask's own node check can take a "done" away;
  - a node reset archives the attempt it discards, and the scorer reads it (migration 089);
  - a pull request Nous opened and the owner merged counts as verified, while DAG check nodes no longer do;
  - weekly owner spot-checks earn credit for agent-written checks of each kind (migration 090)
- **Autonomy view** on the dashboard: the autonomy inventory and scoreboard (`GET /dashboard/autonomy`), and a **Check and fix** panel listing the jobs being checked, judged, kept or stopped
- Heartbeat triage turns are local-only and may not spawn or re-arm work

---

## 📈 Learning From Experience

### F012.1 — Evidence-Gated Procedure Learning
A lesson gate in front of every new procedure: a cue, a lesson and evidence citing the inputs it came from, with `NO_ENTRY` preferred over an invented lesson. A lesson an existing procedure already teaches is not stored again, and a cluster needs two cited sources.

### Lessons From Recurring Jobs
- **Job lessons** *(opt-in, `NOUS_JOB_LESSONS_ENABLED`)*: bad runs of a recurring background job are contrasted with its good runs, and the lesson goes through the same gate. A lesson is kept only if later runs fail less.
- **Check and fix** *(opt-in, `NOUS_JOB_FIXES_ENABLED`)*: when 3 of a recurring job's latest 5 scored runs went wrong, a check run diagnoses it and may change the job's instructions. The change is judged on the runs that follow and is kept, or restored and reported to the owner.
- Procedures fixed recently get a grace period before scoring can skip them

### Strategy Cards (Reasoning Maps, Layer 1)
Lessons distilled from resolved decisions (migration 079), found by closeness to the query, held to the procedure floor, never blocking or replacing a skill of the same name. Off by default (`NOUS_STRATEGY_CARDS_ENABLED`).

### Decisions and Calibration
- Background turns may resolve decisions, but only as noise or superseded
- One `decision_reviewed` audit row per review; an automatic review never replaces a review
- **F058:** the stale confidence calibration factor retired (0.7627 → 1.0), with calibration provenance (migration 073) and an era split in the eval

---

## 🌐 F100 — Browser Task

`browser_task` is a separate agent loop on its own pinned model that drives headless Chromium (Playwright) and returns only a bounded text summary. Network access goes through an egress proxy that checks the allowlist and every resolved address, backed by a kernel-level filter. The image now ships Playwright and Chromium, and the agent runs under an init process. Off by default (`NOUS_BROWSER_TASK_ENABLED`).

---

## 📱 Companion

### F097 — Native Android Companion
An Android beta of the companion with server push, plus a toggle for the Telegram card ping now that push works.
- Agent-hosted companion images survive a rebuild
- Buttons look and respond like buttons; findings-card buttons give visible feedback, fully triaged cards close themselves, and there is a close button
- The companion goes live, and resets its backoff, only when the stream opens

---

## 🛡️ Reliability and Operations

- **Fault detector** *(opt-in)*: flags stalled sleep phases, error streaks, collapsing change ratios and failing retrieval canaries (migration 078)
- **Event-loop watchdog** (on by default): dumps every stack and exits on a stall
- `run_python`: the deadline tracer can no longer wedge the process, and a timed-out call leaves no process of its script behind
- The DAG orchestrator tick is decoupled from the heartbeat loop and survives a failed or cancelled tick; a shell command that timed out is really stopped
- Five background loops and the four maintenance loops survive a cancellation nobody sent them
- `NOUS_DB_LOCK_TIMEOUT_SECONDS` bounds a lock wait (off by default)
- The bot token and the model API key are kept out of the logs
- Background calls cache only the prefix every call shares; structured background calls work on models that reject a forced tool choice
- **Claude Haiku 5.5** at every Haiku call site, and the Haiku defaults move to it
- **Build:** ruff and make in the image, a persistent Claude Code config directory with pre-installed plugins, pytest-asyncio for lineage test runs, and a ruleset on the default branch

---

## 🔧 Fixes

- **Subtasks and DAGs:**
  - an inline subtask is run by its caller only;
  - a node whose launch could not be recorded is launched again;
  - a subtask cancelled mid-run keeps its token usage;
  - a terminal check node leaves no state behind;
  - `dag_create` accepts fix-node DAGs and reports every violation at once;
  - a final report with string list fields or a blank confidence no longer fails;
  - subtask confidence is fail-open
- **Tools:**
  - `bash` waits 120 s by default in background turns that can afford it, and a timeout says what to retry with;
  - a refusal from `write_file` names the allowed directories;
  - Telegram notifications say when they are truncated
- **Heartbeat:**
  - known false positives close at ingest with an expiry;
  - an expired question is recorded as unanswered, not as consent;
  - drift checks are residualized against sleep prunes;
  - the low-effectiveness procedure check needs enough scored activations;
  - promise scans skip subtask results and closed episodes
- **Memory:**
  - tier-1 facts are never cluster-consolidation members;
  - a greeting followed by a request is planned as the request;
  - keyword-leg hits are counted per leg, with an any-word fallback behind a flag (off after an A/B)

---

## 📊 By the Numbers

- **Python:** 231 modules, ~134,500 lines in `nous/` (+ ~15,600 in `nous_eval/`)
- **Tests:** 545 files, ~229,000 lines, 10,578 test functions
- **Dashboard + companion:** 143 Svelte/TS files, ~26,100 lines (+ ~7,700 test lines)
- **Surface:**
  - ~116 REST routes
  - 35 agent tools (new: `browser_task`)
  - 58 tables in four schemas (`brain`, `heart`, `nous_system`, `scoring`)
  - migrations through 090
- **Since v1.0.0:** 129 commits, 129 PRs, 685 files changed (+212,431 / −6,888)

---

## Upgrade Notes

- Migrations apply automatically on startup (073 → 090).
- `docker-compose.yml` adds a `nous-scorer` service that runs the scoring job every 15 minutes. Without `NOUS_SCORER_DB_USER` / `NOUS_SCORER_DB_PASSWORD` it uses the application's login. To keep the agent from writing its own scores, run `scripts/ops/db_roles.sql` once and set the role logins; see [database roles](docs/reference/database-roles.md).
- The image now ships Playwright with headless Chromium, ruff and make, and the agent runs under an init process (`init: true`).
- Haiku call sites default to `claude-haiku-5-5`.
- **Dark by default:**
  - `NOUS_INTENTIONS_ENABLED`, `NOUS_CONTINUATION_ENABLED`
  - `NOUS_RESULT_INBOX_ENABLED`, `NOUS_RESULT_MEMORY_ENABLED`
  - `NOUS_STRATEGY_CARDS_ENABLED`
  - `NOUS_FAULT_DETECTOR_ENABLED`
  - `NOUS_DAG_APPROVAL_NODES_ENABLED`, `NOUS_COMPENSATION_ENABLED`
  - `NOUS_EXECUTION_LEDGER_DETAIL_ENABLED`
  - `NOUS_BROWSER_TASK_ENABLED`
  - `NOUS_JOB_LESSONS_ENABLED`, `NOUS_JOB_FIXES_ENABLED`
  - `NOUS_CHECK_AUDIT_ENABLED`
- **On by default:** the undo journal, the event-loop watchdog, and the tool policy in `warn` mode.
- Flag defaults and rationale now live in [docs/reference/environment-variables.md](docs/reference/environment-variables.md) (moved out of `CLAUDE.md`).
- Deploy: `docker compose build && docker compose up -d`.
- `pyproject.toml` and `nous.__version__` now read `1.1.0`.

---

# v1.0.0 — Stable Version

**704 commits, 384 pull requests, 1,234 files changed since v0.2.0** (March 19 → September 5, 2026)

v0.1.0 was "The Thinking Agent" — decisions, deliberation, cognitive framing. v0.2.0 was "The Remembering Agent" — what to remember, how to recall it, how to forget. v1.0.0 is the **stable version**: the architecture is complete and running in production. Nous runs unattended — a heartbeat that watches and acts, durable multi-step DAGs that survive restarts, background subtasks and schedules — and shows its work in a companion app whose interfaces it composes itself. Underneath, memory stopped being assumed and started being **measured**: a retrieval evaluation harness, per-retrieval telemetry that attributes every dropped candidate to the gate that dropped it, and an audit trail for every sleep cycle.

---

## ✨ Key Features

- **Persistent memory** — episodic, semantic, procedural and working memory in one Postgres + pgvector database; verbatim episode chunks next to summaries; admission control before a fact is kept (F023); supersession and principled forgetting (F027); document and multimodal ingestion — PDFs, images, text files (F024, F069).
- **Decision intelligence** — every decision recorded with confidence and reasoning, reviewed for outcome, calibrated (Brier score, temperature scaling — F058), and guarded by CEL guardrails; `resolve_decision` closes the loop from inside a conversation.
- **The cognitive loop** — frame selection, intent-driven retrieval, token-budgeted context assembly, post-turn self-monitoring, end-of-session reflection and a nightly sleep cycle that consolidates, links and prunes — with an audit trail per cycle (F035.6).
- **Hybrid retrieval** — full-text + vector search fused by Reciprocal Rank Fusion (F025), graph-augmented recall with spreading activation (F022), MMR diversity (F030), optional cross-encoder reranking (F042) and query expansion (F050), all measured on a retrieval evaluation harness (F051).
- **Knowledge graph** — facts, decisions, episodes, procedures and chunks linked across types, densified during sleep (F040), with contradictions surfaced at recall time.
- **Proactive heartbeat** — scheduled checks (health, email, Drive, prompt-defined dynamic checks), a finding lifecycle with escalation and digests, self-tuning parameters, Telegram delivery (F034).
- **Orchestration** — background subtasks, cron and one-shot schedules, and multi-node DAGs with durable at-least-once result delivery, a wall-clock reaper and token accounting (F038, F087).
- **Skills and procedures** — `learn_skill` from URLs, files or inline markdown; K-line procedure learning from repeated successful decisions (F012); full skill bodies delivered at recall (F079, F081).
- **Execution integrity** — an execution ledger, action gating, claim verification (F026) and censor middleware that can run read-only checks before an action proceeds (F031, F078).
- **Companion app** — the agent renders structured, interactive UI instead of prose: approval gates, findings cards, and self-composed micro-apps with charts, metric cards, scorecards and tables that refresh live (F092–F096).
- **Interfaces** — REST API (~100 routes), MCP server, and a streaming Telegram bot with attachments and file delivery; multi-tier web search (Tavily → Exa → Brave, F033).
- **Observability** — a Svelte 5 dashboard: memory graph, decisions and calibration, retrieval telemetry with drop attribution (F091), consolidation audit, prompt-cache efficiency, execution ledger, heartbeat, DAGs, identity and user-profile editing.
- **Operations** — one `docker compose up`; 72 migrations applied automatically on startup; every behavior-changing capability behind a documented flag, shipped land-dark and flipped deliberately; prompt-cache optimization at ~88% hit rate in production (F036); 6,344 tests.

---

## 🖥️ Companion App & Micro-Apps

### F092 — A2UI Companion (Phases 0–4)
The agent renders structured, interactive UI instead of prose. A2UI v1.0 vendored; surfaces persisted with an outbox delta log and pushed over resumable SSE; a Svelte 5 renderer at `/companion`; every user action passes allowlist → nonce → CSRF → rate limit → censor gate → durable audit.
- **Phase 2:** `callAgentFunction` RPC, decision sweep, memory graph and DAG monitor surfaces
- **Phase 3 (F092.1):** ephemeral micro-apps — `compose_surface` builds a read-only, navigable app for any intent, with refine options and live refresh
- **Phase 4:** PWA install, multi-app switcher, per-app deep links, close-all
- **F092.2:** agent-bound actions — a tap becomes a background agent turn that recomposes the app in place (land-dark)
- **F095:** agent-authored data sources — the agent writes the script that produces an app's data, so any domain is live with no new code or env var (land-dark)
- **Activity indicator (#637):** live header stamp with elapsed time, progress rail, pressed control, section dimming; completion keyed to server-issued outbox revisions, never timestamps

### F093 + F094 — Design System & Visualization Vocabulary
Five closed themes, `Repeat` templates, section layouts, and hand-rolled SVG `Sparkline` / `LineChart` / `BarChart` with a `to_series` generalizer, gap-preserving downsampling and normative zero-basing for bars.

### F096 — Report Vocabulary
`MetricCard`, `ScoreCard`, `DeltaList`, `DataTable`, `ChipRow`; section captions, card grids, header notes, sparkline trendlines and focus windows — a "how are things going" report now renders as a live app instead of emailed HTML.

---

## 🫀 Proactive Autonomy

### F034 — Heartbeat (Phases 1–6)
A tick loop that runs checks (health, email, Google Drive, self-initiated), triages findings through a cognitive session, and pushes to Telegram.
- **F034.1** finding lifecycle — fingerprint dedup, state machine, escalation, daily digest, outcome signals
- **F034.2** intelligent checks — embedding search, LLM email classification, drive significance
- **F034.3** self-tuning — outcome-driven parameter adjustment with cross-cycle rollback and pinned params
- **F034.5** dynamic checks — prompt-driven checks created and managed in conversation
- **F034.6** `on_complete` callbacks with three-layer failure handling

### F038 — Unified DAG Orchestration
`DAGStore` + `DAGOrchestrator` + DAG tools + dashboard tab; node completion checks (F038.1); configurable node timeouts (F046); stall detection, per-frame concurrency caps, workspace safety, schedule continuation and a work-queue adapter (F064.x).

### F087 — DAG Durability Spine
Reaching terminal and being delivered are separate transitions: at-least-once result delivery across restarts (bus, agent-authored summary, Telegram), a wall-clock reaper for orphaned nodes, live token accounting, fail-loud wiring when the clock is not running.

### F090 — Callback Execution & Finished-DAG Visibility
Callback nodes execute as real subtasks (flag-gated); `dag_manage action=recent`; Phase-2 gate signals on the dashboard.

### Subtasks, Schedules & Sessions
Subtask hardening — schema, contract, dashboard (#421); **F049** session and memory lifecycle hygiene; **F048** background streaming with TCP keep-alive so long generations no longer drop.

---

## 🧠 Memory — Write Path

### F023 — Memory Admission Control
Five-dimension scoring gate (utility, confidence, novelty, recency, type prior) before a fact enters long-term memory, with shadow mode, persisted per-dimension scores and an admission dashboard.

### F027 — Supersession Detection & Principled Forgetting
Contradiction classification at learn time, supersession lineage, and a sleep-cycle contradiction-resolution phase using structured outputs.

### F084 / F085 / F086 — Write-Path Adjudication, Keyed Facts, Exemplars
- **F084:** dense documents route through enumerative extraction into atomic keyed facts; store-time key-conflict supersession
- **F085:** bidirectional entity indexing + canonical keys, a keyed retrieval leg, bounded iterative composition (R3v2)
- **F086:** parse-only exemplar extraction and a kNN exemplar leg for classification-shaped queries

All three land dark; acceptance is measured in an external harness before any flag flips.

### More Capture, Less Loss
- **F067** episode chunks — verbatim transcript chunks stored alongside lossy summaries, with parent-episode recall
- **F069** `ingest_document` — chunk and persist whole documents
- **F024** inbound multimodal attachments (Telegram + REST): images, PDFs (pypdf with a Claude transcription fallback), text files
- **F075** temporal extraction — date-anchored facts and an event-date recency resolver
- **F039** correction learning (MemAlign-inspired) — user corrections become stored principles
- **F047** actionability classification at learn time
- **S2** extraction input hardening — delimiters, a DATA/INSTRUCTION boundary, prompt-echo filter
- Memory-fidelity constants: transcript, lessons and summary bounds moved from hardcoded truncations to configurable, lossless capture

---

## 🔍 Memory — Retrieval

- **F025** Reciprocal Rank Fusion for hybrid search · **F030** MMR diversity re-ranking · **F042 / F043** cross-encoder reranking in recall and in sleep-cycle graph backfill · **F045** CE-aware cosine thresholds
- **F050** multi-query expansion via Haiku (dark-launch) · **F080** coherent ranking over a knowledge-only recall pool + §14 graph-primary procedure selection · **F079 / F081** procedure delivery with full skill bodies
- **F083** follow-up association — cross-session deictic references ("did that fix work?") resolved from memory
- **F070 / F070.1** chunk graph consolidation · **F071** cross-context dedup · R2 hybrid chunk search over `search_tsv` (land-dark)
- Spreading activation seeded by heart facts (fires on decision-less corpora), bounded activation scores, depth-1 parity option (C-S), Path-A cross-type expansion
- **F044** tinyHippo-Lite — synaptic tagging & capture over graph edges (telemetry-only)
- Pre-turn context: reliable fact injection (render depth, fact pin, supersession lineage, recall backstop), user-profile core/intent split, per-line identity dedup, demotion of superseded and noise decisions
- `recall_deep()` inside `run_python` now runs the real retrieval pipeline (it was a facts-only search)

---

## 🕸️ Graph

- **F040** graph densification — orphan backfill, reverse linking, per-relation thresholds, cluster discovery, density dashboard
- **F053** episode-graph erasure fixed (7,739 edges restored); co-mention / shared-entity associative linking; experiential co-occurrence edges; decision autolink backfill; orphan-edge sleep cleanup
- Edge-precision and orphan audits: the graph is measured rather than assumed — retrieval depends on edge existence, not edge weight

---

## 📏 Measurement & Evaluation

### F051 — Retrieval Evaluation Harness
Local-first evaluation on a separate Postgres image, per-source qrels, paired A/B with an F050 gate, persisted run history and a regression CLI; LongMemEval, LoCoMo and BEAM adapters; multi-turn replay; handler evals for summaries, dedup, admission and graph backfill.

### F091 — Retrieval Telemetry
Every candidate on all three retrieval paths (`recall_deep`, the per-turn context build, `run_python`) ends with a terminal disposition and the gate that assigned it; graph expansion is captured as seed → edge → neighbor; a retrieval dashboard shows drop attribution per retrieval.

### Also
- **F035.6** consolidation audit — a reviewable changelog of every sleep cycle
- **F058** confidence calibration (temperature scaling on decision write) and a `resolve_decision` tool that closes the calibration loop
- Cognitive-layer eval suite; a sleep-cycle health monitor that found three silently broken phases

---

## 🧭 Cognition & Context

- **F026** Execution Integrity — execution ledger, action gating, claim verification, change-aware duplicate detection
- **F024** Critic Agent Phase 0 (smart frame selector) · **F024-3b** self-modifying rubrics
- **F031** censor middleware with action payloads · **F078 / F078.1** censor system and a guarded `send_email` with a hot-reloadable allowlist
- **F036** prompt cache optimization — 3-tier system prompt, single breakpoint, stable tool set (prod cache hit rate ~88%)
- **F038** memory quality & context loading fixes; epistemic gate and recency resolver (flags OFF)
- Tool-input validation and salvage; every tool error path now flags `is_error`

---

## 🛠️ Runtime & Integrations

- **F033** multi-tier search routing (Tavily → Exa → Brave)
- Google Drive via service account, OAuth2 with auto-refresh, `send_file` to Telegram
- Structured-output compaction (fact ledger as a tool array), a compaction hallucination guard, restart-resilient active episodes
- Docker: non-root runner, GitHub CLI in the image; OS and Python packages exposed to the agent's environment section

---

## 📊 Dashboard

Dashboard v1 (vanilla JS, six views) shipped in March and was retired in June for **Dashboard v2** — a Svelte 5 SPA: graph (Cytoscape, centrality, cluster colouring, node detail), decisions, memory browser, calibration, activity, health, observability (event bus, causal traces, drift, context viewer), prompt cache, admission, execution ledger, heartbeat, DAG, density, retrieval, consolidation, and identity / user-profile editing. Mobile layout overhauled.

---

## 📊 By the Numbers

- **Python:** 171 modules, ~90,700 lines in `nous/` (+ ~15,000 in `nous_eval/`)
- **Tests:** 325 files, ~138,000 lines, 6,344 test functions
- **Dashboard + companion:** 135 Svelte/TS files, ~23,600 lines (+ ~6,300 test lines)
- **Surface:** ~100 REST routes, 34 agent tools, 37 ORM tables, migrations through 072
- **Since v0.2.0:** 704 commits, 384 PRs, 1,234 files changed (+423,892 / −4,143)

---

## Upgrade Notes

- Migrations apply automatically on startup (022 → 072).
- Every behavior-changing capability ships behind a flag; defaults are documented in `CLAUDE.md`. Land-dark features (F050, F084–F086, F092.2, F095, …) stay off until flipped deliberately.
- Deploy: `docker compose build && docker compose up -d` — rebuilds the dashboard bundle and restarts the agent.
- `pyproject.toml` and `nous.__version__` now read `1.0.0`.

---

## v0.2.0 — The Remembering Agent (2026-03-17)

Memory quality: graph-augmented recall (F022), K-Line learning (F012), skill discovery v2 (F011), context pruning and quality gates (F016/F017), tool output intelligence (F020), programmatic tool calling. Full notes: https://github.com/tfatykhov/nous/releases/tag/v0.2.0

## v0.1.0 — The Thinking Agent (2026-03-02)

Core architecture: Brain, Heart, Cognitive Layer, runtime (REST + MCP + Telegram), context engine, event bus. Full notes: https://github.com/tfatykhov/nous/releases/tag/v0.1.0
