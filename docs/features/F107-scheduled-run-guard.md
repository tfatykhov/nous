# F107: Scheduled Run Guard

**Status:** phase 1 merged (#810); phase 2 (shadow mode, Run guards panel) PR open. `NOUS_SCHEDULE_GUARDS_ENABLED` (default `true`); nothing is guarded until a schedule's metadata has a `guard` that names an entry of `NOUS_SCHEDULE_GUARD_COMMANDS` (default empty).

## Problem

Some recurring schedules run a model turn whose only job is to run one idempotent script and report what it printed. The hourly ledger sweep (schedule `83ff767a`) had 286 runs in 14 days (2026-10-12 to 2026-10-26). They cost $47, which was 8.8% of logged model spend, and about 98% of them produced nothing. Each run cold-writes a ~32k-token cache prefix just to learn that the script printed nothing.

## Design

A deterministic guard runs first. The model runs only when the guard cannot certify that there is nothing to do.

- **Config, two parts:**
  - The commands: `NOUS_SCHEDULE_GUARD_COMMANDS`, a JSON map of name to shell command in the deployment's env, e.g. `{"ledger_sweep": "bash /tmp/nous-workspace/claude-jobs/ledger_sweep.sh"}`. Unset or empty gives no commands; so does invalid JSON or a non-object, with a WARNING at startup, never a boot failure. An entry whose name or command is not a non-empty string is dropped, with a WARNING naming it.
  - The schedule: `heart.schedules.metadata.guard = {"name": "ledger_sweep", "mode": "shadow", "skip_when": "exit0_quiet", "timeout_seconds": 60}`. `mode` is `shadow` when absent; see [Phase 2](#phase-2-shadow-mode-and-the-run-guards-panel). The timeout defaults to 60 s and is clamped to 1 to 120 s. The guard is opt-in per schedule and needs no migration.
- **Trust boundary:** the schedule row is **not** owner-only. No agent tool or REST endpoint writes schedule metadata, but the agent has general shell and database access, so it could write the row. The row therefore only *names* a command; the command line comes only from the allowlist, which lives in the env, outside the agent's reach. The allowlist pins the command line, not what a script does: an entry that runs a script the agent can write (anything under its workspace, e.g. `/tmp/nous-workspace`) still lets the agent decide what runs. For the guarantee to hold, point each entry at a script the agent cannot write (owned by another user, read-only, outside every mounted workspace). A `command` key in the row is ignored, and a name that is not in the allowlist means no guard: the turn runs as usual, with a WARNING naming the schedule, the name and the allowlist's entries. The worker reads the guard from the schedule row at run time (a schedule of this agent only), never from the run's own metadata or task text.
- **Where it runs:** `SubtaskWorkerPool._process_subtask` (`nous/handlers/subtask_worker.py`), before the run's turn and outside its timeout.
  - The row is already `running`, so the guard's time comes out of F101's grace: `find_active_run` counts a running row only until `started_at + timeout + 300 s`. The guard timeout is therefore capped at 120 s (plus at most 5 s for the kill), well inside that grace.
  - Fire bookkeeping is unchanged: `fire_count`, `next_fire_at`, the F101 policies, the run's intention and the subtask row are created exactly as before.
  - The guard applies only to a plain fire of the schedule: the run's text must equal `schedule.task`, and the run must not be a check-and-fix run.
  - A manual "run now" (`POST /schedules/{id}/run`, F101) creates a plain fire too, so the guard can skip it as well. To force a model turn, run the task as a one-off subtask, or remove the guard first.
- **Command:** an asyncio subprocess (`/bin/sh -c`) with stdin closed. It runs in its own session, and its whole process group is killed on timeout or cancellation, also when the shell has already exited and a background child of it holds the pipes. The wait after the kill is bounded (`nous.utils.kill_process_group`, 5 s), so a child that left the group (`setsid`) cannot strand the worker.
- **Skip (`exit0_quiet`, `mode: skip` only):** exit 0 with whitespace-only stdout **and** stderr. stderr counts because a script that ends in `; true` reports a failure only there; known chatter belongs in the script (`2>/dev/null`), not in the guard.
  - The row is completed with result `skipped_by_guard: <command> exit=0 stdout=empty stderr=empty (<ms> ms)` and `final_outcome=completed`.
  - It records `attempts`/tokens/tool calls of 0 and `report_jsonb = {"guard_skipped": true, "guard": <record>}`.
  - It emits a persisted `subtask_guard_skipped` event.
  - The run's intention is closed quietly, as the inbox writer would close it.
  - No model call, no inbox row, no result memory, no Telegram push and no wake.
- **Fall through (fail open):** a non-zero exit, non-empty stdout or stderr (all of it is checked, not just the first 2 KB), a timeout, an unknown `skip_when`, an error starting or reading the command, a failed schedule read, or a failed write of the skip.
  - The normal turn runs.
  - When the command ran, the guard's receipt is appended to the task text in memory; the row is not changed.
  - A malformed guard is ignored with a WARNING naming the schedule, and a guard that errors, times out or has an unknown `skip_when` is logged at WARNING.
  - The receipt holds the exit code or timeout, the duration, and the first 2 KB of stdout and stderr, fenced as command output.
  - The receipt's output enters the prompt **unscreened**: it does not pass through F103's injection screen or the tool-result path. It is treated as an owner-authored channel, because only an allowlisted command (owner-written, in the env) can produce it. A guard command must therefore not print content from untrusted sources (web pages, mail, third-party issue text); a script that does belongs in the model turn, behind the normal tool path.
  - So the model does not need to run the script again.
- **A record of every run** (that returns a result; a run cut short by a cancellation leaves only a log line): the guard command runs outside tool classes, the execution ledger and F103, so each run leaves its own durable record: `{name, command, skip_when, timeout_seconds, exit_code, timed_out, error, duration_ms, stdout_bytes, stderr_bytes, stderr_head, skipped}` (phase 2 adds `mode, would_skip, reason`). `stderr_head` is the first 512 bytes of stderr; it and `command` pass through the ledger's error redaction (`redact_error_text`). On a skip it is `report_jsonb.guard`; on a fall-through (including a skip whose row write failed) it is the data of a `subtask_guard_ran` event, which the event bus persists to `nous_system.events` like every other subtask event (best effort, as for those: a full bus queue or a failed insert loses it, with a log line). A name that is not in the allowlist runs nothing and leaves its WARNING and, since phase 2, a persisted `subtask_guard_unresolved` event.

## Pilot

After merge, add the command to the deployment's env and restart. `docker-compose.yml` passes `NOUS_*` variables one by one and is a code-owner path, so the owner adds the pass-through line `- NOUS_SCHEDULE_GUARD_COMMANDS=${NOUS_SCHEDULE_GUARD_COMMANDS:-}` to the `nous` service there (this PR does not), and sets the value in the host `.env`:

```
NOUS_SCHEDULE_GUARD_COMMANDS={"ledger_sweep": "bash /tmp/nous-workspace/claude-jobs/ledger_sweep.sh"}
```

`/tmp/nous-workspace/claude-jobs/ledger_sweep.sh` is under the agent's workspace volume, so the agent can rewrite it. Before relying on the allowlist as a trust boundary, copy the script to an owner-owned, read-only path outside the workspace and point the entry there. The record of every run (below) makes any change visible after the fact either way.

Then set the guard on the ledger sweep, in shadow mode first:

```sql
UPDATE heart.schedules
SET metadata = metadata || '{"guard": {"name": "ledger_sweep", "mode": "shadow", "skip_when": "exit0_quiet", "timeout_seconds": 60}}'::jsonb
WHERE id = '83ff767a-1077-4f68-91e9-89fed1e62a55';
```

Watch the Run guards section of the Autonomy view. Once its would-skip runs have no shadow misses you accept, switch it to skip (`jsonb_set(metadata, '{guard,mode}', '"skip"')`). No code path makes that switch.

The script ends with `; true`, so its exit code is always 0. It prints a line only when it logged commits, so empty stdout is the signal, and a failure shows up on stderr, which sends the run to the model.

## Phase 2: shadow mode and the Run guards panel

No guard is turned on in prod until these exist (owner requirement, PR #810 review).

- **Mode:** `metadata.guard.mode` is `shadow` or `skip`. When absent it is `shadow`, so a newly guarded job never loses a run. Any other value (`"SKIP"`, `""`, `null`) falls through at WARNING: the command still runs and is recorded, the receipt notes the unknown mode, and the record's `reason` is `error`. Only the owner switches a job to `skip`; no code path writes `mode`.
- **Shadow runs:** the command runs and the model **always** runs.
  - The receipt is appended as on a fall-through, so a guard that is itself the job's action is not run again by the model.
  - The `subtask_guard_ran` event carries the record. `guard_skipped` is never set, so for the scorer and the job lessons the run is a real one.
- **The record** (`schedule_guard.record`) gains `mode`, `would_skip` (whether the command certified a no-op, whatever the mode) and `reason` (`output`, `stderr`, `exit`, `timeout` or `error`; null when it certified a no-op in a known mode; `error` also for a skip whose row write failed). Events also carry `schedule_id`.
- **Did the model act?** After the turn of a shadow run whose row is terminal, the worker emits a persisted `subtask_guard_acted` event: `{subtask_id, schedule_id, guard: {name, mode, would_skip}, acted, acted_by, observed, run_status, ledger_persisted}`. It is emitted from `_process_subtask`'s `finally`, after `_record_inbox`, so a timed-out turn is recorded too. A run cut short by a shutdown, with its row still `running`, records nothing. "Acted" is defined once, in `schedule_guard`: `acted_signals` gives `acted_by`, the kinds, in this order, and `acted` the verdict (below):
  - `report`: the report has a non-empty summary;
  - `finding`: the report has findings or next actions, or says the work is incomplete or blocked;
  - `inbox`: the inbox writer wrote a row (`record_subtask_result` returned true);
  - `push`: the Telegram push was sent and Telegram answered 2xx;
  - `ledger`: an execution-ledger entry of the run that was not `blocked`, other than a bash run of the guard's own command (matched on `key_args.command_sha256`, so only the exact command line). The durable ledger holds only side-effecting calls (write, external, irreversible), so none of these is a read. With `NOUS_EXECUTION_LEDGER_PERSIST_ENABLED=false` no entry exists, which `ledger_persisted` says.
  - `inbox` and `push` count only for a run that completed: on a failed run they carry the system's failure notice.
  - **Acted** is `schedule_guard.acted(acted_by)`: a `finding` or a `ledger` write, the things that go "beyond the task's 'do nothing' path" (owner requirement). `report`, `inbox` and `push` stay in `acted_by` as context only. A run that finds nothing still writes a summary (the hardened path's validator fails a completed run whose summary is under `NOUS_SUBTASK_REPORT_MIN_SUMMARY_CHARS`), an inbox row and, on the legacy path, a push, so those three come with every completed run; counting them, every would-skip run was a miss (PR #811 review: 805 of 805 completed scheduled runs over 14 days had a summary). The panel derives `acted` from `acted_by` too, so records written before this rule count under it.
  - **Observed** is `schedule_guard.observed(row)`: the row has a structured report. The legacy path (`subtask_hardening_enabled=False`, the code default) writes only a result text, so a finding there is no signal: such a run records `observed: false` and `acted: null` (unknown, never "did nothing"), unless a `ledger` write shows it acted (`acted: true`). `schedule_guard.acted_verdict` is that rule, used by the worker and the panel; a record from before this field reads as observed (PR #811 review, Codex P2).
  - `ledger` can still over-count: a read-only bash line that `classify_bash_command` cannot parse is classed as a write, so it lands in the durable ledger and counts as acting.
- **Unresolved guards:** a guard that names no allowlisted command (or no name) still runs no command, and now also emits a persisted `subtask_guard_unresolved` event (`{schedule_id, guard: {name, reason: "not_allowlisted"}}`) on each run, so a misconfigured guard has a history.
- **The panel:** GET /dashboard/autonomy has a read-only `guards` block (`nous/api/guard_dashboard.py`, merged by the handler like `job_fixes`), described in [REST API](../reference/rest-api.md). It reads only `heart.schedules`, `heart.subtasks`, `nous_system.events` and Settings, never an in-memory ring. A record from before phase 2 counts as `mode: skip`, with its reason derived from its fields.
  - Every count reads one record per run (subtask id): the row's `report_jsonb.guard` wins over an event, else the newest. A skip whose `complete()` committed and then raised leaves both its row and a `ran` event with reason `error`, and a row reclaimed after a shutdown runs its guard again under the same id.
  - `would_skip` counts shadow runs only. It splits into clean runs (`acted` false), `shadow_misses`, `would_skip_unrecorded` (no acted record) and `would_skip_unobserved` (an acted record with `observed: false` and no ledger write); the view labels such a record "not observed".
  - `shadow_misses` counts the would-skip runs in the window whose `subtask_guard_acted` record says the model acted. A miss is counted only for a run with a would-skip `subtask_guard_ran` record in the window, so `shadow_misses <= would_skip` holds even when the best-effort bus drops a `ran` event or a run straddles the window edge.
  - Each `schedules` row carries its own counts (`runs`, `guard_runs`, `skipped`, `would_skip`, `would_skip_unrecorded`, `would_skip_unobserved`, `fell_through`, `shadow_misses`, `shadow_misses_by_signal`) and `last_runs`, its last 10 records, so the misses of one guard never mix with another's and an hourly job does not push a daily one's records out. The block's top-level counts and `recent` are the totals.
  - The Autonomy view's "Run guards" section lists the guarded jobs, each with its runs, skips, would-skips and shadow misses and, folded under its row, its own last 10 records (`last_runs`), then the totals and the last 10 records over every job. Every text in it (job, command, stderr) is rendered by interpolation, never as markup. Shadow misses above 0 show as an alert.
- No migration: the records are JSONB in `report_jsonb` and `nous_system.events.data`.

## Rollback

- Remove the guard from one schedule: `UPDATE heart.schedules SET metadata = metadata - 'guard' WHERE id = ...`.
- Turn guards off everywhere: `NOUS_SCHEDULE_GUARDS_ENABLED=false`, or empty `NOUS_SCHEDULE_GUARD_COMMANDS`.

## Heartbeat model calls in `context_log` (same PR)

Heartbeat checks, triage and callbacks run on a fork of the main `AgentRunner` (`HeartbeatRunner.start`). `fork()` deliberately did not share the `ContextLogger`, so those calls wrote no `nous_system.context_log` rows, although about 3,900 dynamic-check runs happen in 14 days.

- The fork now shares the logger, and `set_context_logger` propagates it to existing forks. `NOUS_CONTEXT_LOG_FORKS_ENABLED=false` restores the old behaviour.
- The logger was withheld from forks after the April 2026 heartbeat triage hang (`0d1d45e3`, "async DB writer contends with triage DB sessions"). Back then the fire-and-forget writes held no strong reference and had no bound. A plausible mechanism is a write task that was garbage-collected mid-flight and left its pooled connection checked out. Every write now has a strong reference (since #499), a 30 s timeout and a 4-write concurrency cap, so these writes cannot exhaust the pool.
- After deploy, verify two things: heartbeat rows appear in `context_log`, and triage duration is unchanged.
- Rows carry the check's session id `dynamic-check-<name>-<hex8>` and `call_type = "heartbeat"` (every call of a fork; the heartbeat is the only caller of `fork()`).
- The fork's calls share the in-memory ring (200 entries) with chat, at about 280 check rows a day. So `/dashboard/observability` ("last 10 calls"), `/dashboard/cache` and `GET /context/log` without a `session_id` leave `heartbeat` calls out; the cache numbers describe the chat prompt. `GET /context/log?session_id=<a heartbeat session>` still returns that session.
- A logger exception never fails a turn: both `update_response` call sites are now guarded, as `log` already was.
- Each entry's response UPDATE now runs after its INSERT. Before, the UPDATE could run first, match no row, and leave the response columns NULL.
