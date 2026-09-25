# Harness Dashboard Visibility — Design (v1)

**Status:** draft for review · **Branch:** `feat/harness-dashboard-visibility` off `main` `fe429ab`
**Design canvas:** https://claude.ai/artifact/BviXvTTbqRyfA7Cz1kkKYc (Overview strip, DAG tab, Ledger tab, Harness tab, shared sidebar)

## 1. Problem

Harness Phases 1–3 (#643–#649) shipped four capabilities that the dashboard barely shows:

| Capability | Today on the dashboard |
|---|---|
| Approval steps (`approval` node, `awaiting_input`) | DAG tab shows the status as an uncoloured string; no question, deadline, answer, card link, attempts or held reason. The companion is the only real surface. |
| Durable execution ledger + idempotent sends (`nous_system.execution_ledger`) | Invisible. The Ledger tab reads the in-memory F026 session ledgers (`runner._ledgers`), which die with the session. `unknown` sends — the one state that needs a person — are never shown. |
| `warn`-mode rules (`harness_unoffered_tool_call`, `harness_context_policy_violation`) | Raw badges in the Activity feed's last-100 events; no counts. Deciding `warn → enforce` needs SQL. |
| Claim checks (`f026_claim_verification`, ~2.4k/30 d on prod) | Same raw feed. |

## 2. Scope

Read-only. No endpoint in this PR mutates state.

1. **DAG tab** — a "Waiting on you" section; a "Now" column per active DAG (waiting on you · approved, waiting for a free slot · running X); approval detail in the node view (question, what the card shows, options + default, asked, deadline, answer + who/when, card link, earlier attempts); `awaiting_input` gets a colour; a failed DAG that *stopped at an approval* reads "stopped by you", not red "failed".
2. **Ledger tab** rebuilt on the durable table: mode banner (all harness switches), an attention callout for `unknown` sends, 24 h/7 d/30 d stats, filters (window, context, status, effect, search), a row table with an expandable detail (key, holder, context ids, key args as stored).
3. **Harness tab** (new route `harness`): one card per rule (offered-tool rule, context policy, claim checks) — mode, total in the window, breakdown, a verdict derived from the numbers; warnings per day; top patterns; the switch order.
4. **Overview** — a "Needs your attention" strip (questions waiting, sends in doubt, harness warnings) and the Execution integrity section re-pointed at the durable ledger.
5. **Nav** — `Harness` item; count badges on DAG Orchestrator (questions waiting) and Execution (unknown sends).
6. **Readability** — `--muted` `#6b6b8a` → `#8e8eab`. Measured contrast on `--surface #12121a`: 3.6:1 → 5.9:1 (WCAG AA needs 4.5:1 for body text). One token, applied app-wide.

**Non-goals:** answering approvals from the dashboard (the companion owns the answer and its audit); a "release key" button (release stays an operator SQL action — the callout shows the statement); event retention (roadmap P2.9); new indexes (existing `idx_events_type` / `idx_events_created` and the ledger's `(agent_id, created_at)` cover these queries).

## 3. Endpoints

All agent-scoped, `GET`, JSON, errors as `{"error": …}` with 4xx/5xx like the siblings. Window parameter: `window` ∈ `24h|7d|30d` (default `24h` ledger, `7d` harness); anything else → 400.

### 3.1 `GET /dashboard/dag` (extended)

Per node, for `node_type == "approval"` only, an `approval` object:

```json
{"question": "<instructions>", "options": [{"id","label","outcome"}], "default_option": "hold",
 "default_label": "Don't send", "asked_at": "<started_at>", "deadline": "<answer_deadline>",
 "answer": "send"|null, "answer_label": "Send it"|null, "answer_source": "companion"|"deadline"|null,
 "answered_by": "tim@…"|null, "answered_at": "…"|null, "card_url": "<base>/companion#/s/<id>"|null,
 "attempts": [<answer_history entries>]}
```

`answered_by` is null for `unattributed` and for the deadline actor (the source already says it). `card_url` uses `dag.approval.card_link(surface_id, NOUS_A2UI_PUBLIC_BASE_URL)` — the same helper `dag_manage` uses.

Per active DAG: `held_reason` = `orchestrator.held_reason(dag_id)` (in-memory, last tick) or null, and `waiting` = count of `awaiting_input` approval nodes. Top level: `waiting_on_you` = every `awaiting_input` approval node in a live DAG, ordered by deadline ascending: `{dag_id, dag_name, node_id, node_name, question, deadline, default_label, card_url, reviewing: [names of its context_flow predecessors]}`. `stats.waiting_count`. Recent DAGs gain `stopped_at_approval: bool` — `dag.approval.stopped_at_approval(nodes)`, the one predicate behind the F087 wording.

`create_app` gains `dag_orchestrator: Any | None = None` (main.py passes it); absent → `held_reason` null.

### 3.2 `GET /dashboard/execution` (new — the durable ledger)

Query: `window`, `context`, `status`, `effect`, `q` (substring over tool name, idempotency key, external ref, and key-arg values; ≤100 chars), `limit` (1–200, default 50), `before` (ISO created_at cursor).

```json
{"modes": {"persist": true, "retention_days": 90, "offered_set": "warn", "context_policy": "warn",
           "claim_verification": "enforce"|"off", "action_gating": "off"|"warn"|…},
 "stats": {"calls", "external_sends", "duplicates_stopped", "blocked", "unknown", "errors", "pending"},
 "attention": [<row>…],            // every `unknown` row, newest first, cap 20, window-independent
 "rows": [<row>…], "has_more": bool}
```

Row: `id, created_at, completed_at, tool_name, context_kind, side_effect_type, status, refusal_code, result_summary, key_args, idempotency_key, external_ref, session_id, parent_session_id, subtask_id, dag_id, dag_name, dag_node_id, node_name, turn`. `refusal_code` parses `refused by <code>` against `ledger_store.REFUSAL_CODES`; `duplicates_stopped` counts `blocked` rows with code `duplicate`, `blocked` counts the others. `key_args` / `result_summary` are returned **as stored** — the store already reduced free text to sha256+length and never kept output (Phase 1b), so the endpoint adds no new exposure. `dag_name` / `node_name` via LEFT JOIN, agent-scoped.

`/dashboard/ledger` (in-memory sessions) stays for API compatibility; the tab stops using it.

### 3.3 `GET /dashboard/harness` (new)

Query: `window` (default `7d`). Reads `nous_system.events` for the three event types in the window.

```json
{"window": "7d",
 "offered_set": {"mode", "total", "by_context": [{"key","count"}], "by_tool": [...]},
 "context_policy": {"mode", "total", "by_violation": [...], "by_context": [...]},
 "claims": {"mode", "events", "total_claims", "by_evidence": {"exact","plausible","none"}},
 "daily": [{"date": "YYYY-MM-DD", "offered_set", "context_policy", "claims_none"}],
 "patterns": [{"rule", "context", "tool", "violation", "count", "last_seen", "latest_session"}]}
```

`patterns`: top 20 groups by count (offered-set: violation = `not offered`; claims: one group per `(kind)` with `evidence == none`, `tool` = null, a ≤120-char claim snippet as stored in the event). `daily` covers every day of the window (zero-filled, UTC). Verdicts are computed in the UI from these numbers (§4.3), never stored.

### 3.4 `GET /dashboard/attention` (new, cheap)

`{"questions_waiting", "next_deadline", "next_dag", "next_node", "sends_unknown", "latest_unknown": {tool_name, target, created_at}|null, "harness_warnings_7d", "offered_set_warnings_7d"}` — three COUNT-style queries. Polled by the Overview strip and by the nav badges (60 s).

## 4. UI

Svelte views reuse the existing primitives (`StatGrid`, `DataTable`, `StaleBadge`, `FilterBar`, `usePoll`); tokens as in `app.css`. The canvas is the reference for layout and copy.

### 4.1 Colours added

`awaiting_input` / approval / "stopped by you" = `#a78bfa` (the decision colour, already a token); `unknown` = `#f472b6`; `pending` = `#22d3ee`. Every status pairs colour with a text label (colour is never the only signal).

### 4.2 Copy rules

Never "human", never "unattributed" (Phase 3 wording rules). A deadline default reads "no answer by <time>; default '<label>' applied". Relative times beside absolute UTC.

### 4.3 Harness verdicts (derived, UI-only)

- mode `enforce` → "Enforcing".
- mode `warn`/`shadow`, total 0 in the window → "Nothing would have been refused in <window>".
- mode `warn`/`shadow`, total > 0 → "Enforce would have refused N calls in <window> — most from <top context> via <top tool>."
- claims: "N claims had no evidence and got a correction" (enforce) / "…would have got a correction" (warn).

The switch-order note is static: context policy first, then the offered-tool rule (an offered-set refusal under `enforce` never reaches the policy's evidence — the documented constraint in CLAUDE.md).

## 5. Tests

- Python (`tests/test_dashboard_harness.py`): each endpoint on SQLite — approval object fields incl. the unattributed/deadline actor rule and card_url; `waiting_on_you` order; `held_reason` threaded; `stopped_at_approval`; ledger filters, stats (duplicate vs other blocked), attention = unknown rows, cursor paging, bad window → 400, agent scoping (a second agent's rows never appear); harness aggregation incl. zero-filled days and top patterns; attention counts.
- Frontend (vitest): verdict function, status-colour map covers every `DAGNodeStatus`, formatters; a render test per new view with a fixture payload (no placeholder rendered for present data).

## 6. Risks

- `events` has no retention (118 MB on prod); the 30 d harness query filters by `event_type` first (indexed), so its cost tracks the three types' volume (~2.4k claims/30 d), not the table.
- `held_reason` is per process and per last tick — shown as a hint, labelled as such in the tooltip.
- `--muted` lift changes every muted label app-wide; intended (visibility), called out in the PR.
