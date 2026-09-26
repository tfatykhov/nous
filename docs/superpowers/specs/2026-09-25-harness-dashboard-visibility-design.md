# Harness Dashboard Visibility — Design (v2)

**Status:** v2 after 3-agent review (architecture, UX/mobile/a11y, devil's advocate — all APPROVE WITH REVISIONS; every finding below verified against code) · **Branch:** `feat/harness-dashboard-visibility` off `main` `fe429ab`
**Design canvas:** https://claude.ai/artifact/BviXvTTbqRyfA7Cz1kkKYc

## 0.0 v2.2 — the verify-by-execution fold

A reviewer probed the implementation (b85e148) against snapshots; these amend everything below.

- **A gap means the record cannot vouch for the day — and nothing more.** The offered-tool rule and the context policy write an event ONLY when they flag a call, so their first flag is not when they started: a quiet day before it may be clean or may predate the rule (prod is deploying the rules now, so a zero there would draw a week of "clean" days before the code existed — false evidence for enforce). Such a day stays `null`; the v2.1 caption "not recording yet" gave it a cause the data cannot show and is replaced. A rule that is `off` now also leaves its quiet days `null`; a day with a count keeps it whatever the mode is now. Claim checks write every turn, so their first event does mark when recording began. Modes are not stored per day: a rule switched off today blanks last week's quiet days too — the trade for never inventing a clean day. **Follow-up:** a startup `harness_modes` event (one row per boot, the four modes) would give every day its real mode and make both directions exact.
- **One in-doubt count.** `harness_dashboard.in_doubt(session, agent_id, limit)` returns `(COUNT, newest limit rows)`; `/execution` returns it as `attention_total` beside the capped `attention`, and `/attention` uses the same call with `limit=1` (it loaded every held row before — tombstones are never deleted, so that only grew). The Ledger headline, the "N hold a send" note and the nav badge all show the total; the callout adds "Showing the newest 20 of N" when capped.
- The Overview never reports "no sends in doubt" or zero calls when ledger persistence is off — it says the ledger is not recording.
- `blocked` (DAG) moves `#dc2626` → `#f25c5c`: 3.55:1 on its own badge tint failed AA at 11px. `status.test.ts` now checks every status colour at 4.5:1 against its tint over `--surface`.
- Ledger "Load older" reports a failed page ("Could not load older rows — try again.") and resumes polling when no older rows are on screen; loaded older rows stay put and the button retries.
- Top-pattern rows are keyed by `(rule, mode, context, tool, violation)` — the backend's own grouping — not by index, so an open disclosure stays with its row when a poll re-sorts the list.
- **Claims** count from the first claim event EVER when no pre-2c event is in the window (every turn writes one, so a quiet day had no turns); only with pre-2c events in view do they start at the first post-2c one.
- **Tombstone** = a keyed `success`/`unknown` row with empty `key_args` — not also a NULL summary: the Ledger's own release statements write the summary back, and a confirmed tombstone must still read "details removed by retention".
- The node sheet says "Card being delivered…", and shows the countdown, only for a step actually `awaiting_input`; an approval not yet reached reads "not asked yet", an ended one "not answered (<status>)".
- **A verdict counts only its own mode** (codex round 1). Every event carries the mode it ran under: the warn verdict counts `by_mode.warn` and its "Most:" pattern comes from warn patterns; the enforce verdict counts `by_mode.enforce`; events under another mode are reported beside it ("Also in 7 d: 3 refused under enforce"), never folded in. Claims gain `none_by_mode`, so "a correction was queued" describes only claims recorded under enforce. Charts and totals stay mode-agnostic — a flag is a flag whether the call ran or was refused.
- **Persistence off reads no events at all**: `/dashboard/harness` skips both event queries, so no record written before the switch can reach a page that says nothing below was measured (patterns, bars, first-flag dates included).
- **Ledger persistence off holds nothing** (codex round 2): with `NOUS_EXECUTION_LEDGER_PERSIST_ENABLED=false`, `main.py` installs no `LedgerStore`, so no retry is refused. `in_doubt()` takes `ledger_persisted` and returns `(0, [])` then — one gate for `/execution` and `/attention` alike. Old keyed `unknown` rows stay in the ledger table as history, labelled "holds nothing while persistence is off"; they hold again only if persistence is switched back on. The Harness view also gates its patterns and bars on `events_persisted` client-side, beside the server-side skip.
- **`evidence_since` only when it is true** (codex round 3): the first post-2c claim event IN the window is when evidence levels began only if pre-2c events are in view too — the switch happened inside the window. Otherwise it began earlier, the field is `null`, and the card says "Every check in this window recorded evidence levels" instead of a start date that moves with the window. (An all-time query would give the exact date but scans every claim event on each poll; coverage is what the card needs.)
- **Nothing on screen outlives the poll that superseded it** (codex round 4). `makePollStore.refresh()` during an in-flight fetch aborts it (its answer is for the old inputs and is never committed) and runs one more fetch when it settles; several refreshes share that re-run, `stop()` drops it, and timer ticks keep their old behaviour. This fixes a Ledger filter or search typed mid-request (and the Harness window selector) showing an older query's rows until the next poll. The DAG node sheet keeps only the node id and derives the node from each poll, closing when the node is gone — a copy went on reading `awaiting_input` after the step was answered.

## 0.1 v2.1 — the v2 re-review fold (devil APPROVE; architecture and UX APPROVE WITH REVISIONS)

Implemented as specified below; these points amend §3-§4 where they differ.

- **Search** is an OR of per-column `lower(coalesce(col,'')) LIKE :q ESCAPE` — never a `||` concatenation (NULL on every unkeyed row, and matches across field boundaries).
- **Unmeasured days are `null`, not 0** (refined in §0.0): a quiet day before a flag-only rule's first flag or while it is off, before claim evidence levels began, and everywhere when persistence is off. The chart breaks its line there; each rule line has its own dash (the three colours are close in brightness).
- **Claims** gain `by_mode`; legacy vs new is decided by KEY PRESENCE of `claims` (post-2c events always carry it, even `[]`). Copy: "a correction was queued for the next turn" — one-turn sessions end before it is delivered.
- **Zero-flag verdicts**: "No flags recorded yet" (no event ever) or "No flags in <window>" + "First flag <date>" — `first_event_at` is the first flag, not a deploy time.
- **`stopped_by`** is `companion | deadline | mixed | null`, with `stops: [{node_name, answer_source, answer_label}]` so a decline on one branch is never hidden by a default on another.
- **`reviewing`** lists the outputs under review, not an earlier approval's name (its answer still shows in `card_summary`).
- **`held_by`** carries the holder's `session_id` and `turn`; the label is "Key currently held by" (a lookup made now cannot prove the refusal's cause).
- **`sends`** = `idempotency.is_keyed_tool`. The Ledger's "N hold a send" note counts `attention_total` (window-independent, the badge's own COUNT — §0.0).
- **Release statements append** with `concat_ws(' · ', result_summary, …)` (never erase the stored note); each Copy button sits under its own statement; outcomes read "retries are answered 'already sent'" / "the next retry sends". A tombstone with no provider ref reads "cannot be verified any more — record it either way".
- **"Card being delivered…"** when an approval is parked but neither linked nor errored yet. The default line reads "the DAG stops here". A past deadline reads "the default applies on the next tick".
- **`/dashboard/attention`** gains `refused_7d` per rule; the Overview harness line reads each rule's mode (enforce → refused, off → is off, warn → flagged, the calls still ran) over "last 7 days".
- Harness "not measured" gates the chart, its hidden table and the patterns message too.

## 0. What v2 changed (review fold)

| Finding | v2 |
|---|---|
| `unknown` is not "send in doubt": any cancelled / timed-out / swept side-effecting call closes `unknown` (`runner.py:77-84,1852,2395`; `ledger_store.py:380-384`), and only KEYED rows hold anything | Attention = `status='unknown' AND idempotency_key IS NOT NULL`; other unknown rows stay in the table as "outcome unknown — nothing held" (§3.2) |
| Release SQL `SET status='error'` on a send that DID arrive frees the key → the next retry duplicates | Two guarded statements: arrived → `'success'` (hold stays, badge clears); nobody got it → `'error'`. Both `AND status='unknown'`. "Releasing re-sends nothing." (§4.4) |
| The callout named a cause ("connection dropped") nothing stores; `unknown` also covers a partial SMTP recipient refusal and Telegram 5xx/timeouts | Neutral headline + `result_summary` verbatim + "if no recipient got it" (§4.4) |
| "Already sent" is only the answer when the holder is `success`; a pending/unknown holder gets a refusal | "Currently held by" (holder as read now); no "answer to the agent" (§3.2) |
| "Stopped by you" also fires on a deadline default (`is_answered_approval` = `bool(answer_source)`, `approval.py:187-196`) and dropped delivery's `status=='failed'` guard | `stopped_by: companion|deadline|null` under `dag.status=='failed'`; neutral copy (§3.1, §4.2) |
| Harness "nothing refused" is fabricated when persistence is off or a rule is `off`; Phases 1a/2a are not on prod yet | `events_persisted`, per-rule `mode`, `first_event_at`; "Not measured" / "Not checking" verdicts (§3.3, §4.3) |
| One warn-mode call writes BOTH events (`runner.py:283-335`); unoffered ⇒ undeclared for heartbeat contexts | Never sum across rules: per-rule counts, one line per rule, Overview names each rule (§3.3, §4.3) |
| Each event carries its own `mode`; a total after a flip mixes warn with enforce | Totals grouped by event `mode` (§3.3) |
| Prod claim events predate 2c (`violation_count`, no `claims[]`) → by_evidence 0/0/0 | Evidence levels from new-shape events only, plus a `legacy` bucket and the date evidence starts (§3.3) |
| "Duplicates stopped" merges three cases | Stat "Repeat sends refused"; the row detail says which (holder status) (§3.2) |
| Mockup data the store never keeps (hashed bash/code/spawn text, "exit 1" on run_python, key format, attempt numbers, re-ask line, context on claims) | Canvas and fixtures show only stored shapes (§4.5) |
| Missing fields: holder, card summary, undelivered card | `held_by`, `card_summary` (hoisted walk), `card_error` (§3.1, §3.2) |
| `create_app` gets a lazy proxy (never None; raises when DAGs disabled); `_held` keyed by UUID vs SQLite str rows | try/except `RuntimeError`, `UUID(str(id))` (§3.1) |
| Paging on `created_at` alone; `text()` JSON-as-str on SQLite; naive timestamps | `(created_at, id)` keyset, typed ORM selects, `as_utc().isoformat()` (§3.2) |
| Keyboard/SR can't reach row detail or node detail | DataTable disclosure button; "Details" opens the node sheet; accessible node list under the graph (§4.6) |
| Contrast: white on accent 3.99, `#ef4444` pill 4.26, the muted lift is ~5 token copies + ~20 literals, active nav 3.96 | §4.1 |
| Fixed-width SVG chart; stacked series | `Chart.svelte` lines + visually-hidden table (§4.3) |
| Mobile collapse, filters (fake `heartbeat` kind, clearable window, duplicate aria-labels), badges hidden from SR / closed drawer, bare local times | §4.6 |

## 1. Problem

Harness Phases 1–3 (#643–#649) shipped data the dashboard barely shows: approval steps (DAG tab shows a bare uncoloured status); the durable execution ledger (the Ledger tab reads in-memory F026 session ledgers that die with the session); `warn`-mode rule events and claim checks (raw badges in the Activity feed). Deciding `warn → enforce` needs SQL.

## 2. Scope

Read-only — no endpoint mutates state; answering stays in the companion (its audit), key release stays an operator SQL action.

1. **DAG tab** — "Waiting on you"; a "Now" column; approval detail in the node sheet; `awaiting_input` colour + shape; a failed DAG that stopped at an approval reads "stopped at approval".
2. **Ledger tab** rebuilt on `nous_system.execution_ledger`.
3. **Harness tab** (new route `harness`).
4. **Overview** — "Needs your attention" (questions, keyed sends in doubt) + a quiet harness line; Execution integrity re-pointed at the durable ledger.
5. **Nav** — `Harness` item; badges for questions waiting and keyed sends in doubt (also on the mobile hamburger).
6. **Readability** — muted text `#6b6b8a` → `#8e8eab` everywhere it is a text colour (§4.1).

Non-goals: dashboard answering; a release button; event retention (P2.9); new indexes; changing `success_rate` (a stopped DAG still counts as failed — noted in the tooltip).

## 3. Endpoints

All agent-scoped `GET`, JSON; `{"error": …}` with 4xx/5xx. `window` ∈ `24h|7d|30d` (anything else → 400). Typed ORM selects (not `text()`), timestamps emitted `as_utc(ts).isoformat()`.

### 3.1 `GET /dashboard/dag` (extended)

**Approval object** on every `node_type == "approval"` node:

```json
{"question", "options": [{"id","label","outcome"}], "default_option", "default_label",
 "asked_at", "deadline", "answer", "answer_label", "answer_source": "companion"|"deadline"|null,
 "answered_by": "<email>"|null, "answered_at", "card_url"|null, "card_error"|null,
 "card_summary": "<question + inputs, exactly as the card shows them>",
 "reviewing": ["draft-report"], "attempts": [{"answer","label","outcome","answer_source","answered_by","answered_at"}]}
```

- `answered_by` is null for `unattributed` and for `system:deadline` — in `attempts` too (history entries store them raw, `approval.py:243-256`).
- `card_url` = `approval.card_link(surface_id, a2ui_public_base_url)`; `card_error` = `node.error` when `surface_id IS NULL` (e.g. "approval card not delivered yet: …", `orchestrator.py:2970-2996`).
- `card_summary` / `reviewing` come from ONE pure function hoisted into `approval.py` — `context_results(node, nodes, edges)`, the walk that goes THROUGH approval predecessors (today `orchestrator._context_results`, which then calls it). `card_summary = build_card_summary(question, context_results(...))`, computed from full node results (the node query's 200-char cut is display-only).

**Per active DAG:** `waiting` (count of `awaiting_input` approval nodes) and `held_reason` = `orchestrator.held_reason(UUID(str(id)))` — `create_app(dag_orchestrator=...)` receives main.py's lazy proxy; the call is wrapped in `try/except RuntimeError` (DAGs disabled) like `heartbeat_runner`. Per process, last tick — labelled a hint.

**Top level:** `waiting_on_you` (derived from the active-DAG loop — no extra query), every approval node `status='awaiting_input'` in a DAG with `status IN LIVE_DAG_STATUSES`, ordered by deadline: `{dag_id, dag_name, node_id, node_name, question, deadline, default_label, card_url, card_error, reviewing}`. `stats.waiting_count` = its length.

**Recent DAGs:** `stopped_by: "companion"|"deadline"|null` — non-null only when `status == "failed"` and `stopped_at_approval(nodes)`; `"companion"` if every stopping approval was answered in the companion, else `"deadline"`. One batched `(dag_id, status, node_type, answer_source)` fetch for the 20 recent DAGs.

### 3.2 `GET /dashboard/execution` (new)

Query: `window` (default `24h`), `context` (a `context_kind` from `execution_context.ContextKind`), `status`, `effect`, `q` (≤100 chars; `lower(tool_name || idempotency_key || external_ref || cast(key_args as text)) LIKE %q%` with autoescape — matches key names too, documented), `limit` 1–200 (default 50), `before` = `<iso>,<id>` keyset.

```json
{"modes": {"persist", "retention_days", "offered_set", "context_policy", "claim_verification", "action_gating", "events_persisted"},
 "stats": {"calls", "sends", "external", "repeat_sends_refused", "blocked", "unknown", "unknown_keyed", "errors", "pending"},
 "attention": [<row>…], "attention_total": n, "rows": [<row>…], "next_before": "<iso>,<id>"|null}
```

- `stats` follow `window` only (not the other filters). `sends` = `tool_name IN ('send_email','send_file')`; `external` = `side_effect_type IN ('external','irreversible')`; `repeat_sends_refused` = blocked with code `duplicate`; `blocked` = other refusal codes; `unknown_keyed` ⊂ `unknown`.
- `attention` — window-independent, cap 20, newest first: `status='unknown' AND idempotency_key IS NOT NULL`. The same predicate is the nav badge and the Overview card (§3.4).
- Row: `id, created_at, completed_at, dispatched_at, tool_name, context_kind, side_effect_type, status, refusal_code, result_summary, key_args, idempotency_key, external_ref, session_id, parent_session_id, subtask_id, dag_id, dag_name, dag_node_id, node_name, turn, held_by`. `refusal_code` parses `refused by <code>` against `REFUSAL_CODES`. `held_by` (keyed rows only, batched per page) = the row currently holding the same `(agent_id, tool_name, idempotency_key)` in `KEY_HOLDING_STATUSES`: `{id, status, created_at, external_ref}` or null. Joins scope `d.agent_id = l.agent_id AND n.dag_id = d.id`.
- `key_args` / `result_summary` returned as stored (free text is already sha256+length; output never stored; keys are `{scope}:{sha256[:16]}`, recipients live in `key_args.to/cc` by design). A retention tombstone (`key_args = {}` on a keyed row) is flagged `tombstone: true`.

`/dashboard/ledger` stays for API compatibility; the tab stops using it.

### 3.3 `GET /dashboard/harness` (new)

Query: `window` (default `7d`). One fetch of the three event types in the window (`idx_events_type`), aggregated in Python (UTC buckets).

```json
{"window", "events_persisted": bool,
 "rules": {
   "offered_set":    {"mode", "first_event_at", "by_mode": {"warn": n, "enforce": n}, "by_context": [...], "by_tool": [...]},
   "context_policy": {"mode", "first_event_at", "by_mode": {...}, "by_violation": [...], "by_context": [...]},
   "claims":         {"mode", "first_event_at", "evidence_since", "by_evidence": {"exact","plausible","none"},
                      "turns_with_claims", "legacy": {"events", "violations"}}},
 "daily": [{"date", "offered_set", "context_policy", "claims_none"}],
 "patterns": [{"rule", "mode", "context"|null, "tool"|null, "violation", "count", "last_seen", "latest_session", "snippet"|null}]}
```

- `mode` = current Settings; `by_mode` groups by each event's own `data.mode`.
- Claims: `by_evidence` counts claims in events that carry `claims[]` (post-2c); `evidence_since` = the first such event; older events (`violation_count`, no `claims[]`) go to `legacy`. Claim patterns: `rule=claim`, `context=null` (the event has none), `violation = "no evidence"`, `snippet` = the stored ≤120-char text.
- `daily` covers every day of the window; a day the record cannot vouch for is `null` (§0.0).

### 3.4 `GET /dashboard/attention` (new, cheap; nav badge + Overview)

`{"questions_waiting", "next": {dag_name, node_name, deadline, default_label}|null, "sends_in_doubt", "latest_in_doubt": {tool_name, recipients, created_at, dag_name, tombstone}|null, "ledger_persisted", "harness": {"events_persisted", "offered_set": {"mode","warn_7d"}, "context_policy": {"mode","warn_7d"}}}`. Same predicates as §3.1 `waiting_on_you` and §3.2 `attention`. Views that already hold fresher counts push them into the shared store the nav reads.

## 4. UI

Existing primitives (`StatGrid`, `DataTable`, `StaleBadge`, `FilterBar`, `BottomSheet`, `Chart.svelte`, `usePoll`); canvas is the layout reference.

### 4.1 Tokens and contrast

- `--muted` → `#8e8eab` (5.86:1 on surface, 6.21 on bg), and its copies: `--muted-token`/`--muted-foreground` HSL, `@theme --color-nous-muted`, and every literal `#6b6b8a` used as TEXT in `dashboard-app/src` (swept; companion.css untouched — own themes).
- New tokens: `--waiting #a78bfa` (approval, awaiting_input, stopped at approval), `--unknown #f472b6`, `--pending #22d3ee`. Harness series use colours outside that set: offered-tool `#60a5fa`, context policy `#fb923c`, claims without evidence `#2dd4bf`.
- Primary button: `#0a0a0f` on `#7c6af7` (4.95:1) — also FilterBar's active pill. Error pill uses `--red #f87171`. Active nav text `#a99df9` (6.69).
- One status→colour map in `lib/status.ts`, used by DagView and `Dag.svelte` (today two maps).

### 4.2 Copy

Never "human", never "unattributed". Stopped DAG badge: "stopped at approval"; detail: "declined — '<label>' in the companion at <t>" / "no answer by <t>; default '<label>' applied" (`approval_line` wording). Times: absolute UTC + relative ("Sep 25 14:31 UTC · 2h ago").

### 4.3 Harness verdicts (UI-only, from the numbers)

- `events_persisted` false → "Not measured — event persistence is off (NOUS_F026_PERSISTENCE_ENABLED)".
- rule `mode == off` → "Not checking".
- `enforce` → "Enforcing — refused N in <window>".
- `warn`, no events since `first_event_at` (or none at all) → "Nothing flagged since <first_event_at | window start>" — never "nothing would be refused".
- `warn`, N > 0 → "Enforce would refuse calls like these — N in <window>. Most: <context> · <tool> · <violation>" (from that rule's top pattern row).
- Claims: "N claims had no evidence and got a correction" (enforce) / "…would have got" (warn); legacy events shown as "N older checks, evidence levels not recorded".
- Never sum across rules. Chart: "Flags per day", one line per rule (`Chart.svelte`), plus a visually-hidden table.
- Switch order: context policy first, then the offered-tool rule (static note). Violation codes glossed (title + legend).

### 4.4 The sends-in-doubt callout

Headline "N send(s) ended without confirming delivery". Per row: tool, recipients (`key_args.to/cc`), DAG/node, time, provider ref, `result_summary` verbatim. Copy: "Check whether the recipients got it." Two statements, full id, guarded:

- Got it → `UPDATE nous_system.execution_ledger SET status='success', result_summary='confirmed delivered by operator' WHERE id='<id>' AND status='unknown';` (the hold stays; retries stay suppressed)
- Nobody got it → `… SET status='error', result_summary='released by operator' WHERE id='<id>' AND status='unknown';` (frees the key; re-sends nothing by itself — retry the node)

A partial refusal warning: "If only some recipients got it, keep it as delivered." Copy uses the Clipboard API with a select-all + `aria-live` fallback (plain-http LAN host). Tombstones render "recipients no longer stored".

### 4.5 Canvas / fixture honesty

Only stored shapes: bash/run_python/spawn text as `sha256… (N chars)`; exit codes only on bash; keys `scope:hash16`; no attempt numbers or re-ask lines; claim patterns without a context.

### 4.6 Interaction, mobile, accessibility

- DataTable gains a disclosure `<button aria-expanded aria-controls>` in the first cell (Ledger, Recent DAGs, Top patterns). "Waiting on you" rows: "Answer in companion" (link; hidden with `card_error` shown when null) + "Details" (opens the node sheet). Under the graph, an accessible list of nodes (buttons) opens the same sheet; `Dag.svelte` draws approval nodes as a diamond, status still also in text.
- Filters: Window = required pills (not clearable); Context / Status / Effect = `<select>`s built from the real enums; `FilterBar` gains `label` + `required` props.
- Paging: "Load older" pauses polling ("Paused — viewing older rows · Back to latest").
- Below 768px: "Waiting on you" stacks (buttons full-width, 44px); stat rows via StatGrid (+ optional `note`/`tone`); rule cards `auto-fit minmax(280px,1fr)`; callout stacks; detail grids `auto-fill minmax(220px,1fr)`; Ledger/patterns use DataTable card mode with a compact card (tool + status / target / time · context); 44px targets.
- Nav: badge count in the link's accessible name ("DAG Orchestrator, 2 questions waiting"); a dot on the mobile hamburger from the same store. Overview cards `aria-labelledby` their title; only non-zero cards render; all-clear = no questions and no sends in doubt (harness is a secondary line, never part of all-clear).

## 5. Tests

- Python (`tests/test_dashboard_harness.py`, SQLite): approval object (null actor rule incl. attempts, card_url/card_error, card_summary through a chained approval), `waiting_on_you` predicate/order, held_reason via proxy (+ RuntimeError path, UUID coercion), `stopped_by` companion/deadline/guarded; execution: keyset paging with tied timestamps, filters, stats definitions, attention predicate (unkeyed unknown excluded), `held_by`, tombstone flag, agent scoping, bad window → 400; harness: `events_persisted` false, per-event-mode grouping, no cross-rule sums, legacy claim events, zero-filled days, top patterns; attention counts = tab predicates.
- Frontend (vitest): verdict function (every branch), status map covers every `DAGNodeStatus` and is the one Dag.svelte uses, time formatter, DataTable disclosure is keyboard-operable, render tests per view from fixtures that obey §4.5.

## 6. Risks

- `events` has no retention (118 MB on prod); the harness fetch is type-filtered and window-bounded. Nothing is cached server-side: the only 60 s is the client's attention poll. `/harness` at `30d` decodes every claim event in the window (~2.4k today, 0.5–2 KB each) and its all-time `MIN(created_at) GROUP BY event_type` reads through `idx_events_type`; there is no `(event_type, created_at)` index. Fine at today's volume, grows with it. **Follow-up:** that index (a migration) or event retention, whichever comes first.
- `held_reason` is per process / last tick — a hint.
- The muted lift changes every muted label app-wide (intended).
