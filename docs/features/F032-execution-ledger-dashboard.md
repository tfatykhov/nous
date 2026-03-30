# F032: Execution Ledger Dashboard

**Status:** Reviewed
**Date:** 2026-03-30
**Depends on:** F026 (Execution Integrity — deployed)
**Blocks:** None (additive, UI-focused)
**Review:** 3-agent review (architect + data specialist + devil's advocate), 6 P1s fixed

---

## Problem Statement

The execution ledger (F026) tracks every tool call within a session — what was executed, whether it was allowed or blocked, side-effect classifications, and claim verification status. However, this data is currently only visible as a **summary section on the Overview tab**: a few stat cards and a table showing session-level totals.

Operators cannot:
- See **individual actions** within a session (tool name, arguments, status, timing)
- Filter actions by status (allowed vs. blocked vs. error)
- Understand the **timeline** of tool execution within a turn
- Inspect **why** an action was blocked (result summary from the gate)
- See side-effect classifications at a glance (read/write/external/irreversible)

### Why a separate tab (not enhanced Overview)

- Action timeline + filtering complexity would bloat `overview.js` past 500 lines
- `/status` endpoint serves Telegram bot and MCP tools — per-action data would bloat their responses
- Consistent with pattern: every major subsystem has a dedicated tab (decisions, graph, admission, rubric)

### Why now

- F026 is fully deployed and generating data in every session
- The Overview tab integrity section is too compressed — operators need drill-down
- Debugging blocked actions requires reading server logs, not the dashboard

---

## Solution: Dedicated Execution Ledger Tab

Add a new "Execution" dashboard tab with two levels of detail:

1. **Session list** — all active sessions with summary stats and status indicators
2. **Session detail** — expandable per-session view showing every action with full metadata

### Data source

The execution ledger is **in-memory and session-scoped** (no database persistence). Data is served from `runner._ledgers` via a new REST endpoint. This means:
- Data is available only for active sessions
- When a session ends, its ledger is lost
- This is acceptable for an operational monitoring dashboard

### New REST endpoint

**GET /dashboard/ledger?action_limit=50**

Query parameters:
- `action_limit` (int, default 50, max 200) — max actions returned per session

Returns detailed action data for all active sessions:

```json
{
  "enabled": {
    "ledger": true,
    "claim_verification": true,
    "action_gating": true
  },
  "modes": {
    "claim_verification": "enforce",
    "action_gating": "enforce"
  },
  "sessions": [
    {
      "session_id": "abc-123",
      "current_turn": 5,
      "total_actions": 23,
      "success_actions": 20,
      "blocked_actions": 1,
      "error_actions": 2,
      "timeout_actions": 0,
      "summary": "12 searches, 5 file writes, 3 bash",
      "actions": [
        {
          "turn": 1,
          "tool_name": "recall_deep",
          "key_args": {"query": "authentication patterns"},
          "status": "success",
          "timestamp": "2026-03-30T14:22:01.000000+00:00",
          "result_summary": "Found 5 matching facts...",
          "side_effect_type": "none"
        },
        {
          "turn": 3,
          "tool_name": "write_file",
          "key_args": {"path": "/tmp/auth.py"},
          "status": "blocked",
          "timestamp": "2026-03-30T14:23:15.000000+00:00",
          "result_summary": "Action gating: duplicate write to same path",
          "side_effect_type": "write"
        }
      ],
      "actions_truncated": false
    }
  ]
}
```

### Implementation notes (from review)

1. **Snapshot before iteration**: `list(runner._ledgers.items())` and `list(ledger.actions)` at handler entry — prevents `RuntimeError` from concurrent mutation
2. **Datetime serialization**: Call `a.timestamp.isoformat()` explicitly — `JSONResponse` cannot serialize `datetime` objects
3. **Single-pass status counts**: Use `collections.Counter` over `a.status for a in actions` to compute all counts in one pass
4. **Sensitive data redaction**: For `bash` tool `key_args`, redact patterns matching `[A-Z_]+=\S+` (env vars) and `Bearer \S+` (tokens) before serialization
5. **Route ordering**: Register before `Mount("/dashboard", ...)` static mount to avoid shadowing
6. **Shared config helper**: Extract `_build_integrity_config(settings)` callable from both `/status` and `/dashboard/ledger`
7. **`escapeHtml` required**: All `key_args` values, `result_summary`, session IDs must be escaped in JS — `result_summary` contains raw tool output
8. **No `pending_corrections`**: This field is consumed by `.pop()` at turn start and will nearly always read as 0 — removed from per-session response
9. **`current_turn` property**: Add `@property current_turn` to `ExecutionLedger` to avoid accessing private `_current_turn`

### UI Design

#### Session List View (default)
- **Stat cards row**: Active Sessions, Total Actions (all sessions), Blocked Actions, Error Actions, Claim Verification mode, Action Gating mode
- **Session cards**: One card per active session showing:
  - Session ID (truncated with copy-on-click)
  - Current turn number
  - Action counts by status (success/blocked/error/timeout)
  - One-line summary
  - "View Details" expand button
- **Empty state**: "No active sessions" when no ledgers exist
- **Auto-refresh**: 15s polling via `setInterval` when tab is active, cleared on tab switch

#### Session Detail View (expanded)
When a session card is expanded:
- **Action timeline**: Grouped by turn number
  - Each action shows: tool name, key args, status badge, side-effect badge, timestamp
  - Status badges: green (success), red (blocked), yellow (error), grey (timeout)
  - Side-effect badges: none (no badge), write (blue), external (orange), irreversible (red)
- **Filter bar**: Filter by status (all/success/blocked/error/timeout) and by side-effect type
- **Action count summary**: "Showing 15 of 23 actions" when filtered

#### Overview tab update
Keep existing `buildIntegritySection()` as summary. Add "View Details >" link that navigates to `#/execution`.

### Known limitations
- `result_summary` is truncated to 100 chars at record time in `ExecutionLedger` — may cut off gating rationale for blocked actions. Enhancement tracked separately.
- No historical data — ledger is cleared when session ends

---

## Files Changed

| File | Change |
|------|--------|
| `nous/cognitive/execution_ledger.py` | Add `@property current_turn`, add `redact_key_args()` helper |
| `nous/api/rest.py` | Add `GET /dashboard/ledger` endpoint, extract `_build_integrity_config()` |
| `static/dashboard/index.html` | Add nav link + view div for "Execution" tab |
| `static/dashboard/js/ledger.js` | New JS module for the tab |
| `static/dashboard/css/dashboard.css` | Add ledger-specific styles (badges, filters, turn groups) |
| `static/dashboard/js/overview.js` | Add "View Details >" link to integrity section |
| `tests/test_rest_ledger.py` | Tests for new endpoint |

---

## Testing

1. **Endpoint test**: Mock runner with ledgers, verify JSON structure and all fields
2. **Empty state test**: No ledgers returns empty sessions array
3. **Serialization test**: Verify datetime is ISO string, all `ExecutedAction` fields included
4. **Status counts**: Verify success/blocked/error/timeout counts are correct
5. **Action limit**: Verify `action_limit` param truncates and sets `actions_truncated: true`
6. **Sensitive data redaction**: Verify bash commands with env vars/Bearer tokens are redacted
7. **Snapshot safety**: Verify response builds from snapshot, not live references
