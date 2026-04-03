# F034 Heartbeat — Implementation Plan

**Date:** 2026-04-03
**Decision:** `55adf0b7`
**Review Decision:** `c59bb1ae` (3-agent review: arch + impl + devil)

## Review Findings Summary

High convergence across all 3 reviewers on critical issues:
1. **Runner API mismatch** — Spec uses nonexistent `create_session()`/`process_turn()`. Actual: `run_turn(session_id, message)`
2. **`telegram_bot.send_push()` nonexistent** — Use direct httpx POST like `SubtaskWorkerPool._notify_telegram()`
3. **Brain/Heart method names fabricated** — Must remap to actual APIs
4. **Event constructor missing `agent_id`** — Required field
5. **Shutdown integration missing** — Need `heartbeat_runner.stop()` in `shutdown_components()`
6. **Config `tuple[int,int]` won't parse** — Use separate int fields

## Scope Decision

**Single delivery** (not phased). Skip:
- DriveCheck — `GoogleDriveIntegration` doesn't exist
- CalendarCheck — No Google Calendar API integration
- Adaptive scheduling — Phase 4 concern
- Procedure-backed checks — Phase 3 concern (F012 integration)

## API Remapping

| Spec Calls | Actual API | Action |
|---|---|---|
| `brain.get_pending_reviews(older_than_days=7)` | `brain.get_unreviewed(max_age_days=7)` | Use existing |
| `heart.get_censors()` | `heart.censors.list_active()` | Use existing |
| `heart.count_stale_facts(older_than_days=30)` | Does not exist | Add to FactManager |
| `heart.procedures.get_low_effectiveness(threshold)` | Does not exist | Add to ProcedureManager |
| `heart.schedules.get_overdue()` | `heart.schedules.get_due(now)` | Use existing |
| `heart.facts.search(query, category, limit)` | Exists with correct signature | Use existing |
| `telegram_bot.send_push()` | Direct httpx POST to TG API | Implement helper |
| `runner.create_session()` / `process_turn()` | `runner.run_turn(session_id, message)` | Redesign |
| `Event(type=..., data=...)` | `Event(type=..., agent_id=..., data=...)` | Add agent_id |
| `NousConfig` | `Settings` | Use correct class name |

## Files to Create

| File | Purpose |
|---|---|
| `nous/heartbeat/__init__.py` | Package exports |
| `nous/heartbeat/schemas.py` | Finding, CheckResult, HeartbeatResult dataclasses |
| `nous/heartbeat/registry.py` | CheckRegistry + BaseCheck ABC |
| `nous/heartbeat/checks.py` | HealthCheck, SelfInitiatedCheck, EmailCheck |
| `nous/heartbeat/runner.py` | HeartbeatRunner main loop |
| `tests/test_heartbeat.py` | Full test suite |

## Files to Modify

| File | Changes |
|---|---|
| `nous/config.py` | Add all `heartbeat_*` config fields |
| `nous/main.py` | Wire HeartbeatRunner in create_components + shutdown_components |
| `nous/api/rest.py` | Add heartbeat status/trigger/config/check endpoints |
| `nous/heart/facts.py` | Add `count_stale(older_than_days)` method |
| `nous/heart/procedures.py` | Add `get_low_effectiveness(threshold)` method |

## Build Order

1. **Schemas** — `schemas.py` (Finding, CheckResult, HeartbeatResult)
2. **Registry** — `registry.py` (BaseCheck ABC, CheckRegistry)
3. **Config** — Add heartbeat fields to Settings
4. **Runner** — `runner.py` (HeartbeatRunner with tick loop, budget, quiet hours, triage, Telegram)
5. **Checks** — `checks.py` (HealthCheck, SelfInitiatedCheck, EmailCheck)
6. **Heart additions** — count_stale_facts, get_low_effectiveness
7. **Main.py wiring** — create_components + shutdown_components
8. **REST endpoints** — Status, trigger, config, check trigger/reset
9. **Tests** — Full coverage

## Implementation Team

- **python-engineer**: Steps 1-8 (all code)
- **test-engineer**: Step 9 (tests, in parallel after step 6)
- **code-reviewer**: Review after implementation complete
