# F107: Scheduled Run Guard

**Status:** PR open. `NOUS_SCHEDULE_GUARDS_ENABLED` (default `true`); nothing is guarded until a schedule's metadata has a `guard` that names an entry of `NOUS_SCHEDULE_GUARD_COMMANDS` (default empty).

## Problem

Some recurring schedules run a model turn whose only job is to run one idempotent script and report what it printed. The hourly ledger sweep (schedule `83ff767a`) had 286 runs in 14 days (2026-10-12 to 2026-10-26). They cost $47, which was 8.8% of logged model spend, and about 98% of them produced nothing. Each run cold-writes a ~32k-token cache prefix just to learn that the script printed nothing.

## Design

A deterministic guard runs first. The model runs only when the guard cannot certify that there is nothing to do.

- **Config, two parts:**
  - The commands: `NOUS_SCHEDULE_GUARD_COMMANDS`, a JSON map of name to shell command in the deployment's env, e.g. `{"ledger_sweep": "bash /tmp/nous-workspace/claude-jobs/ledger_sweep.sh"}`. Empty, invalid JSON or a non-object gives no commands (a WARNING at startup, never a boot failure), and an entry whose name or command is not a non-empty string is dropped.
  - The schedule: `heart.schedules.metadata.guard = {"name": "ledger_sweep", "skip_when": "exit0_quiet", "timeout_seconds": 60}`. The timeout defaults to 60 s and is clamped to 1 to 300 s. The guard is opt-in per schedule and needs no migration.
- **Trust boundary:** the schedule row is **not** owner-only. No agent tool or REST endpoint writes schedule metadata, but the agent has general shell and database access, so it could write the row. The row therefore only *names* a command; what runs comes only from the allowlist, which lives in the env, outside the agent's reach. A `command` key in the row is ignored, and a name that is not in the allowlist means no guard: the turn runs as usual, with a WARNING naming the schedule. The worker reads the guard from the schedule row at run time (a schedule of this agent only), never from the run's own metadata or task text.
- **Where it runs:** `SubtaskWorkerPool._process_subtask` (`nous/handlers/subtask_worker.py`), before the run's turn and outside its timeout.
  - Fire bookkeeping is unchanged: `fire_count`, `next_fire_at`, the F101 policies, the run's intention and the subtask row are created exactly as before.
  - The guard applies only to a plain fire of the schedule: the run's text must equal `schedule.task`, and the run must not be a check-and-fix run.
- **Command:** an asyncio subprocess (`/bin/sh -c`) with stdin closed. It runs in its own session, and its whole process group is killed on timeout or cancellation.
- **Skip (`exit0_quiet`):** exit 0 with whitespace-only stdout **and** stderr. stderr counts because a script that ends in `; true` reports a failure only there; known chatter belongs in the script (`2>/dev/null`), not in the guard.
  - The row is completed with result `skipped_by_guard: <command> exit=0 stdout=empty stderr=empty (<ms> ms)` and `final_outcome=completed`.
  - It records `attempts`/tokens/tool calls of 0 and `report_jsonb = {"guard_skipped": true, "guard": {...}}`.
  - It emits a persisted `subtask_guard_skipped` event.
  - The run's intention is closed quietly, as the inbox writer would close it.
  - No model call, no inbox row, no result memory, no Telegram push and no wake.
- **Fall through (fail open):** a non-zero exit, non-empty stdout or stderr (all of it is checked, not just the first 2 KB), a timeout, an unknown `skip_when`, an error starting or reading the command, a failed schedule read, or a failed write of the skip.
  - The normal turn runs.
  - When the command ran, the guard's receipt is appended to the task text in memory; the row is not changed.
  - A malformed guard is ignored with a WARNING naming the schedule, and a guard that errors, times out or has an unknown `skip_when` is logged at WARNING.
  - The receipt holds the exit code or timeout, the duration, and the first 2 KB of stdout and stderr, fenced as command output.
  - So the model does not need to run the script again.

## Pilot

After merge, add the command to the deployment's env and restart. `docker-compose.yml` passes `NOUS_*` variables one by one and is a code-owner path, so the owner adds the pass-through line `- NOUS_SCHEDULE_GUARD_COMMANDS=${NOUS_SCHEDULE_GUARD_COMMANDS:-}` to the `nous` service there (this PR does not), and sets the value in the host `.env`:

```
NOUS_SCHEDULE_GUARD_COMMANDS={"ledger_sweep": "bash /tmp/nous-workspace/claude-jobs/ledger_sweep.sh"}
```

Then set the guard on the ledger sweep:

```sql
UPDATE heart.schedules
SET metadata = metadata || '{"guard": {"name": "ledger_sweep", "skip_when": "exit0_quiet", "timeout_seconds": 60}}'::jsonb
WHERE id = '83ff767a-1077-4f68-91e9-89fed1e62a55';
```

The script ends with `; true`, so its exit code is always 0. It prints a line only when it logged commits, so empty stdout is the signal, and a failure shows up on stderr, which sends the run to the model.

## Rollback

- Remove the guard from one schedule: `UPDATE heart.schedules SET metadata = metadata - 'guard' WHERE id = ...`.
- Turn guards off everywhere: `NOUS_SCHEDULE_GUARDS_ENABLED=false`, or empty `NOUS_SCHEDULE_GUARD_COMMANDS`.

## Heartbeat model calls in `context_log` (same PR)

Heartbeat checks, triage and callbacks run on a fork of the main `AgentRunner` (`HeartbeatRunner.start`). `fork()` deliberately did not share the `ContextLogger`, so those calls wrote no `nous_system.context_log` rows, although about 3,900 dynamic-check runs happen in 14 days.

- The fork now shares the logger, and `set_context_logger` propagates it to existing forks. `NOUS_CONTEXT_LOG_FORKS_ENABLED=false` restores the old behaviour.
- The logger was withheld from forks after the April 2026 heartbeat triage hang (`0d1d45e3`, "async DB writer contends with triage DB sessions"). Back then the fire-and-forget writes held no strong reference and had no bound. A plausible mechanism is a write task that was garbage-collected mid-flight and left its pooled connection checked out. Every write now has a strong reference (since #499), a 30 s timeout and a 4-write concurrency cap, so these writes cannot exhaust the pool.
- After deploy, verify two things: heartbeat rows appear in `context_log`, and triage duration is unchanged.
- Rows carry the check's session id `dynamic-check-<name>-<hex8>`.
- A logger exception never fails a turn: both `update_response` call sites are now guarded, as `log` already was.
- Each entry's response UPDATE now runs after its INSERT. Before, the UPDATE could run first, match no row, and leave the response columns NULL.
