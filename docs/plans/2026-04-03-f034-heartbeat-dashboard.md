# F034 Heartbeat Dashboard — Implementation Plan

**Date:** 2026-04-03
**Decision:** `40d1aef8`
**Review Decision:** `708bb2fa` (3-agent review: ui + api + devil)

## Review Findings Summary

- **UI spec** (95581c82): Status banner, 5 stat cards, 2 charts (budget doughnut + findings stacked bar), check table, findings timeline, cognitive sessions log, 30s auto-refresh
- **API spec** (76276096): Hybrid in-memory + DB query, query function in dashboard_queries.py
- **Devil P1s** (383e56d3): Findings not persisted individually, cognitive session data not emitted, event JSONB lacks granularity, platform tag not persisted

## Pre-Requisite: Runner Event Enrichment

**Must fix BEFORE dashboard can show historical data.**

### Change 1: Enrich `heartbeat_tick` event (runner.py)
Add per-finding details to the event data:
```python
"findings": [{"source": f.source, "summary": f.summary, "urgency": f.urgency, "check_name": f.check_name} for f in all_findings],
"by_source": dict(Counter(f.source for f in all_findings)),
"by_urgency": dict(Counter(f.urgency for f in all_findings)),
```

### Change 2: Emit `heartbeat_triage` event (runner.py)
After cognitive triage completes, emit:
```python
Event(type="heartbeat_triage", agent_id=..., data={
    "session_id": session_id,
    "findings_count": len(findings),
    "tokens_used": result.tokens_used,
    "response_summary": result.response[:200],
})
```

## Build Order

### Step 1: Runner event enrichment (`nous/heartbeat/runner.py`)
- Enrich heartbeat_tick event data with findings array + by_source/by_urgency
- Emit heartbeat_triage event after cognitive session
- Add `@property is_running` and `@property is_quiet` to HeartbeatRunner

### Step 2: Dashboard query function (`nous/api/dashboard_queries.py`)
Add `get_heartbeat_dashboard_data(session, agent_id, hours=24)`:
- Query `heartbeat_tick` events from events table (last N hours, limit 100)
- Query `heartbeat_triage` events for cognitive session history
- Aggregate findings by source/urgency from JSONB
- Return dict with recent_ticks, findings, cognitive_sessions, findings_by_day, totals

### Step 3: REST endpoint (`nous/api/rest.py`)
Add `dashboard_heartbeat(request)`:
- Guard: heartbeat_runner availability
- Read `?hours=` param (default 24, cap 168)
- Merge in-memory state (checks, budget, status) with DB history
- Compute quiet_hours.currently_quiet from settings
- Return JSONResponse

Wire route: `Route("/dashboard/heartbeat", dashboard_heartbeat)` after `/dashboard/ledger`
Wire heartbeat_runner LazyProxy in build_app

### Step 4: Frontend panel (`static/dashboard/js/heartbeat.js`)
`Dashboard.registerView('heartbeat', ...)` with:
- Status banner (active/disabled, quiet hours pill, budget pill)
- 5 stat cards: Total Runs, Findings (24h), Cognitive Sessions, Checks Active, Circuit Breakers
- Token Budget doughnut chart (used/remaining, center text, color shift at 80%/100%)
- Findings by Urgency 7-day stacked bar (high=red, normal=yellow, low=muted)
- Check status table (dot indicator, name, interval, next due, circuit breaker)
- Findings timeline (24h, urgency-colored dots, source badge)
- Cognitive sessions log (session_id, timestamp, findings count, tokens)
- 30s auto-refresh (copy ledger.js pattern: AbortController + in-flight guard)

### Step 5: Nav + HTML (`static/dashboard/index.html`)
- Add nav link after Activity, before Graph Health: "Heartbeat" with heart SVG icon
- Add `<div id="view-heartbeat" class="view"></div>`
- Add `<script src="js/heartbeat.js"></script>`

### Step 6: CSS (`static/dashboard/css/dashboard.css`)
- New variable: `--heartbeat-color: #22d3ee` (cyan/teal)
- Status banner styles (~15 lines, mirror ledger banner)
- Check table row styles (~20 lines)
- Urgency-colored timeline dots (~10 lines)
- Budget gauge center text (~10 lines)

### Step 7: Tests
- Test runner event enrichment (heartbeat_tick includes findings array)
- Test heartbeat_triage event emission
- Test dashboard query function aggregation
- Test REST endpoint response shape
