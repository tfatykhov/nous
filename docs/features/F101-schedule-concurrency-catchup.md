# F101: Schedule Concurrency and Catch-up Policies

**Status:** implemented on `feature/f101-schedule-policies`, behind `NOUS_SCHEDULE_POLICIES_ENABLED` (default on).
**Code:** `nous/handlers/task_scheduler.py`, `nous/heart/schedules.py`, migration `sql/migrations/091_schedule_policies.sql`
**Tests:** `tests/test_f101_schedule_policies.py`
**Inspiration:** the `concurrencyPolicy` / `catchUpPolicy` of [Paperclip routines](https://github.com/paperclipai/paperclip) (`docs/api/routines.md`, `server/src/services/routines.ts`). The policy names and defaults are Paperclip's.

## Problem

`TaskScheduler` fires a due schedule by creating a subtask (`create_fire`, `metadata.schedule_id`). Two cases had no policy:

1. **A fire while the previous run is still active.** A scheduled turn that calls `dag_create` starts a DAG that keeps running after the turn's subtask has finished. Nothing linked that DAG back to its schedule (the F099 intention lineage does, but intentions are off by default), so the next fire of a daily DAG job started a second DAG beside the first.
2. **Fires missed during downtime.** Nothing defined what happens.

## Behaviour before F101 (verified in the code)

These are the flag-off rules, and they are unchanged with the flag off:

- **Overlap: deferred.** `_has_active_subtask_for_schedule` (F064.5) looks for a pending or running subtask with the schedule's `metadata.schedule_id`. If it finds one, the fire is skipped **without advancing** `next_fire_at`. The schedule stays due, is checked again every `NOUS_SCHEDULE_CHECK_INTERVAL` seconds, and fires late once the subtask ends. The check sees subtasks only, not DAGs. It fails open on a database error, and a subtask whose worker died while it was `running` holds the schedule until the next restart (`reclaim_stale` then sets it back to pending).
- **Missed slots: collapsed.** `get_due` returns every schedule with `next_fire_at <= now`, and `advance(now)` computes the next slot from the time of the check: for cron the next cron time after now, for an interval `now + interval_seconds`. Any number of slots missed during downtime therefore produce **one** run on recovery. The next slot is then the next future one.

## Policies

Both are columns of `heart.schedules` (migration 091, with CHECK constraints). Existing rows get the defaults.

### `concurrency_policy`

| Value | A fire while a run is active |
|-------|------------------------------|
| `coalesce_if_active` (default) | starts nothing, is recorded as `coalesced` and linked to the active run, and the slot is consumed |
| `skip_if_active` | the same, recorded as `skipped` |
| `always_enqueue` | starts another run |

`always_enqueue` cannot be combined with `continuation_turns > 0` (F064.5): `ScheduleManager.create` refuses it with a `ValueError` (no agent tool or REST route sets `continuation_turns` today, so the check guards every way in). The runs of one continuation cycle share a session (`metadata.session_id`), and the runner serializes a session's turns on its lock. Two runs started side by side would therefore queue there while each worker's timeout keeps counting, so the second run could use up its timeout waiting.

`coalesced` and `skipped` differ only in what is recorded, as in Paperclip. "Coalesced" means the active run serves this slot. "Skipped" means the slot was dropped.

### `catch_up_policy`

| Value | A check that finds missed slots |
|-------|---------------------------------|
| `skip_missed` (default) | one run; the next slot is the next future one (the pre-F101 behaviour) |
| `enqueue_missed_with_cap` | one run per missed slot, oldest first, up to `catch_up_cap` (per schedule, 1 to 25; NULL means `NOUS_SCHEDULE_CATCH_UP_CAP`, default 3) |

**How catch-up and concurrency combine.** As in Paperclip, every catch-up run passes the concurrency gate. Under the default `coalesce_if_active`, the first run starts and the later missed slots are coalesced into it, each one recorded. Several catch-up runs therefore only start with `always_enqueue`. The `schedule_task` tool description says this.

**Counting missed slots.** The scheduler walks the schedule's cron or interval from `next_fire_at`, at most `cap` steps. A slot counts as missed only when it is more than one check interval old, so a check that is only late cannot turn a short interval into a double fire. Such a passed slot is not used up: the schedule then advances from the last slot it counted, so `next_fire_at` is that passed slot, which is due at the next check and goes through the concurrency gate as usual. When the cap is reached, the schedule advances from now and the remaining slots are dropped, a passed slot inside the last check interval included. A schedule whose slots are closer together than `NOUS_SCHEDULE_CHECK_INTERVAL` therefore gets one run per slot at each check (up to the cap), and its `next_fire_at` can lie up to one check interval in the past. `max_fires` bounds the batch, and `fire_count` never passes it.

**`max_fires` counts runs started.** A fire that is coalesced or skipped does not raise `fire_count`. A `max_fires` schedule whose fires keep coalescing therefore stays active until that many runs have actually started.

### Scope

- The policies apply to **recurring** schedules. A one-shot schedule keeps the pre-F101 path.
- The job-fix watcher's diagnostic run (`nous/handlers/job_fixes.py`) does not pass the gate. It is started on purpose for a job that keeps failing, and suppressing it would hide what it diagnoses. Its subtask carries `metadata.schedule_id`, so it does count as an active run for the schedule's own fires.
- Heartbeat checks are not schedules and are out of scope.

## The active-run check

`find_active_run(heart, settings, schedule_id, now)` returns the oldest active run, or None:

| Run | Active while |
|-----|--------------|
| subtask with `metadata.schedule_id` = the schedule, `pending` | `created_at` is younger than `NOUS_SCHEDULE_ACTIVE_RUN_MAX_AGE_SECONDS` |
| the same, `running` | `started_at` (or `created_at`) + its timeout (`timeout_seconds`, or `NOUS_SUBTASK_DEFAULT_TIMEOUT` for 0) + 300 s grace is still in the future |
| DAG with `execution_dags.schedule_id` = the schedule, `pending`/`running` | `created_at` is younger than `NOUS_SCHEDULE_ACTIVE_RUN_MAX_AGE_SECONDS` |

**Stale runs never block a schedule for good.** A worker enforces a subtask's timeout, so a row still `running` past it plus the grace belongs to a worker that died. A DAG has no timeout of its own, so a DAG, or a pending subtask in a wedged queue, stops counting after the max age (default 12 hours). The default is under a day, so a stuck DAG costs a daily job at most one slot. An hourly job can lose up to 12 slots to it, and each lost slot is recorded.

**A parked DAG counts.** A DAG that waits on an owner's answer (an approval node) is still `running`, so it is the schedule's active run, even though `MAX_ACTIVE_DAGS` does not count it (`parked_clause()`). This is on purpose. A second run would usually ask the owner the same question, and while a schedule is held this way each held fire is recorded with the DAG's id. The max age bounds the wait.

**The DAG link.** Migration 091 adds `nous_system.execution_dags.schedule_id`. The `ExecutionContext` of a scheduled turn already carries `schedule_id` (from the subtask's metadata). `ToolDispatcher` injects it into `dag_create` as the hidden `_schedule_id`, and `DAGStore.create` stores it. The dispatcher drops every `_`-prefixed argument the model sends, so a model cannot forge or remove the link. A DAG launched by another DAG's node is not linked, but its parent DAG is already the active run.

**Errors.** A database error in the check propagates. The fire is logged ("Failed to fire schedule"), not advanced, and retried on the next check. Failing open would start the double run this feature exists to prevent, and the run's own insert would most likely fail on the same database anyway.

## Firing (flag on)

For each due recurring schedule, `_fire_with_policies`:

1. Takes the schedule's **fire lock**, `pg_try_advisory_xact_lock(hashtextextended('schedule_fire:<hex>', 0))`, in a session of its own. If another fire holds it, nothing happens and the schedule is retried on the next check. A try-lock is used because a waiter would hold a pooled connection.
2. Reads the schedule again under the lock and stops if it is no longer active or due.
3. Works out the number of runs (above). For each run it calls the concurrency gate and then either records the suppressed fire or starts the run. Starting a run uses the F064.5 continuation bookkeeping, and the row is re-read between runs so that a continuation cycle carries forward.
4. Advances the schedule once: `next_fire_at` is the next slot after now (under `enqueue_missed_with_cap`, after the last counted slot when a passed slot lies inside the last check interval; see above), `fire_count` grows by the runs started, and `last_fired_at` is set only if a run started. The slot advances even when every run was coalesced or skipped.

**Failures.** An error before the first run starts (queue full, closed container, database) propagates as before F101: the schedule is not advanced and is retried. An error after a run has started ends the catch-up there, and so does a cancellation. The runs already started are counted and the schedule advances, so the next check cannot start the same slots again.

## Manual run

`POST /schedules/{id}/run` (REST) calls `run_schedule_now`. It takes the same fire lock, reads the schedule again under it (404 if a fire in between used up its last `max_fires` run), and goes through the same concurrency gate, so a manual run obeys the policy (Paperclip: "Concurrency policy still applies"). It leaves `next_fire_at`, `fire_count` and the continuation cycle alone.

| Response | When |
|----------|------|
| 202 `{"status": "started", "run_id", "schedule_id"}` | a run was started |
| 200 `{"status": "coalesced"/"skipped", "run_id", "schedule_id"}` | the policy held it back; `run_id` is the active run |
| 404 | no active recurring schedule of this agent with that id (a one-shot schedule's own fire takes no fire lock, so it has no manual run) |
| 409 | the flag is off, another fire holds the lock, the queue is full, or the schedule's container intention is closed |
| 400 | malformed id |

No agent tool starts a manual run.

## Observability

A suppressed fire:

- logs `Schedule <id8> <schedule|manual> fire <coalesced|skipped>: <subtask|dag> <id> is still active` at INFO;
- emits a `schedule_fire_suppressed` event on the event bus, which the bus persists to the events table, with `data = {schedule_id, outcome, policy, trigger, active_run: {kind, id}}`. With `NOUS_EVENT_BUS_ENABLED` off, only the log line is written.

`list_tasks` shows each schedule's policies, for example `(next: 2026-10-25 08:00 UTC; concurrency: coalesce_if_active, catch-up: skip_missed)`.

## Settings

| Setting | Default | Meaning |
|---------|---------|---------|
| `NOUS_SCHEDULE_POLICIES_ENABLED` | `true` | Off: the scheduler ignores both policy columns and keeps the pre-F101 path (defer without advancing, subtasks only). `POST /schedules/{id}/run` answers 409. The columns are still written. |
| `NOUS_SCHEDULE_CATCH_UP_CAP` | `3` | Cap for `enqueue_missed_with_cap` when a schedule has no `catch_up_cap` (1 to 25) |
| `NOUS_SCHEDULE_ACTIVE_RUN_MAX_AGE_SECONDS` | `43200` | How long a pending subtask or a live DAG can count as a schedule's active run |

**Why the flag is on by default.** The defaults (`coalesce_if_active`, `skip_missed`) change existing behaviour in one place: a fire that overlaps an active run is now dropped and recorded, where before it was deferred and fired late once the run ended. That is the intended fix. A late duplicate of a daily DAG job is exactly the double run this feature removes, and catch-up is unchanged. The new active-run check is also stricter than the old one. It sees DAGs, a running subtask stops blocking after its timeout, and a database error no longer fails open. To roll back, set `NOUS_SCHEDULE_POLICIES_ENABLED=false`. The migration is additive (nullable or defaulted columns plus one partial index), so the code can be rolled back without reverting it.

## Tool and API surface

- `schedule_task` takes optional `concurrency_policy`, `catch_up_policy` and `catch_up_cap`, which are enums and an integer in the schema. They are validated by `ScheduleManager.create`, which raises `ValueError` and stores nothing. An invalid value comes back as a tool error that names the bad parameter.
- `list_tasks` shows the policies.
- REST: `POST /schedules/{id}/run` (above). `GET /schedules` and `POST /schedules` are unchanged, so a schedule created over REST gets the defaults.

## Known gaps

- A scheduled turn that calls `spawn_task` starts a subtask without `metadata.schedule_id`. That child does not count as an active run. Only DAGs are linked.
- A DAG has no reaper of its own. The max-age rule hides a stuck DAG from the scheduler but does not fail it.
