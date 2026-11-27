# F121 — Detached tool calls (Tasks inside the harness)

Status: 📝 Spec, not built. Owner decision pending on §9.
Date: 2026-11-27. Repo at `d5767085`.
Origin: chat 2026-11-27. MCP Tasks (protocol 2026-07-28) lets a *client* run a long tool call
in the background, but Nous calls the Anthropic API directly and the API has no Tasks support.
The model never sees the protocol. The useful idea is still sound, so Nous builds it into its
own harness: a tool call that runs long is moved to the background, and its result wakes Nous later.

Claims are marked **[verified: file:line]**, **[measured]** (live DB, 30 days to 2026-11-27)
or **[assumption]**.

## 1. Problem

In a chat turn, every tool call blocks the turn until it returns or is cancelled.

* The dispatcher wraps each chat call in `asyncio.wait_for(..., timeout=tool_timeout)` and
  cancels it on timeout. The result is "Tool 'x' timed out after Ns", marked as outcome unknown
  **[verified: nous/api/runner.py:6348-6420, `_dispatch_with_keepalive`]**.
* `tool_timeout` defaults to 120 s **[verified: nous/config.py:1331]**. bash allows up to 300 s
  **[verified: nous/api/builtin_tools.py:39]**. With the default, a chat bash call that asks for
  300 s is cut at 120 s. (Production chat bash calls have run to 300 s **[measured]**, so the
  deployed value is higher. Either way one ceiling is hidden behind another.)
* When bash hits its own limit, the error tells the model to do it by hand: `nohup ... &` and poll
  **[verified: builtin_tools.py:248-254]**. That is a do-it-yourself background task with no
  wake-up, no record and no cancel.
* Today the model has to guess up front that something is slow and choose `spawn_task` or a DAG.
  When it guesses wrong, Tim waits while the turn is blocked.

Measured, interactive (chat) context, last 30 days **[measured, nous_system.tool_call_log]**:

* bash: 1,187 calls, 106 over 25 s (9 %), 31 over 60 s, 27 timeouts, p95 30.6 s, max 300 s.
* run_python: 35 calls, 15 over 25 s, p95 46 s (its own 90 s deadline).
* recall_deep: 132 calls, 9 over 25 s, max 57 s.
* win-mcp tools: timeouts cluster at exactly 30 s (the MCP server's own limit, not ours).

So the payoff in chat is about 3–4 long calls a day plus about one timeout a day. That's real but
modest, so v1 is kept small (§3).

## 2. Goal and non-goals

Goal: in a chat turn, an eligible tool call that is still running after `promote_after` seconds
keeps running in the background. The model gets an immediate placeholder result with a task id.
The turn can go on or end. The real result comes back through the F099 result inbox and wakes a
continuation turn, the same way subtask results do now.

Non-goals (v1):

* Background contexts (scheduled, dag_node, subtask, heartbeat_check, continuation). No human is
  waiting, and they have their own time budgets. Their timeouts are a separate problem (§10).
* MCP Tasks protocol support, either as client or server. See the F121 decision record. Exposing
  DAGs to outside clients as MCP Tasks is a separate idea ("option B").
* Changing any tool's schema. The harness decides, and the model is not asked.
* Raising bash's 300 s cap. That's a follow-up once detaching is proven (§10).

## 3. Which calls can be detached

A call is eligible only if **all** of these hold:

1. Context is `interactive` (a chat turn through `_dispatch_with_keepalive`).
2. The tool is on the allowlist `detach_tools` (default: `bash`, `browser_task`, `web_fetch`).
   Opt-in later: `run_python`, `recall_deep`, and `mcp__<server>__*` per server.
3. Its tool class is not `spawns` (already async), not `irreversible`, and not `external` sends
   **[verified: nous/api/tool_classes.py]**. `send_email`, `send_file` and Telegram sends are never
   detached, because the turn has to know whether a send happened before it reports to Tim.
4. The call wasn't held for owner approval (ActionGate / owner_actions) and doesn't carry an
   idempotency send key. Those flows assume an inline result.
5. Concurrency is under limits: at most `detach_max_per_session` (3) and `detach_max_global` (8)
   detached calls running. Over the limit, the call waits inline as today.
6. `continuation_enabled` and `result_inbox_enabled` are on. Without a wake path, a detached
   result would be lost, so the feature refuses to turn on (startup check, logged once).

bash is eligible whether it writes or not. Detaching doesn't change what the command does, only
whether the turn waits. The risk is the model acting before the effect lands, and the placeholder
text (§4.2) plus the claim verifier (§5) cover that.

## 4. Mechanism

### 4.1 Promotion

In `_dispatch_with_keepalive`, the keepalive loop already wakes every `keepalive_interval` (10 s).
Add one check: once elapsed ≥ `promote_after` (default 25 s) and the call is eligible:

1. Don't cancel the dispatch task. Move ownership of it to a process-wide `DetachedCallRegistry`
   (new, `nous/api/detached_calls.py`), which holds a strong reference and a done-callback.
2. Write a tracking row (§4.3) in its own committed transaction **before** yielding the
   placeholder. If the write fails, don't detach; keep waiting inline as today.
3. Yield `DispatchOutcome(placeholder_text, is_error=False, timed_out=False)` plus a new field
   `detached_id`. The `finally` block must not cancel a detached task. Today it cancels anything
   not done **[verified: runner.py:6413-6419]**, so this is the one line that needs care.
4. The registry wraps the task in `asyncio.wait_for(..., detach_max_seconds)` (default 900 s,
   counted from the original start) and swaps the inline `tool_timeout` for that.

The ledger entry for the call closes as `detached`, a new status: not success, not failure.

### 4.2 Placeholder result (what the model sees)

```
[detached] bash is still running after 25s and has moved to the background as task <id>.
Its result will arrive in a later turn. It has NOT finished: don't claim its effect, don't
re-run it, and don't depend on its output in this turn. Use cancel_task <id> to stop it.
```

### 4.3 Tracking row: reuse `heart.subtasks` (recommended)

Insert a `heart.subtasks` row with `status='running'`, `worker_id='detached:<process id>'`,
`task` = tool name plus its `reason` argument and a short input preview, and a marker
`metadata.kind='detached_call'` with the session, turn, call index and ledger entry id.

Why: the result inbox, intentions and wake routing only accept `source_kind` in
`('subtask','dag',…)` **[verified: nous/storage/models.py:461, 1355, 1431]**. A subtask row works
with all of them as they are, plus `list_tasks`, `cancel_task`, the work view and the reconciler.
Workers claim only `pending` rows, so a `running` row stamped with a worker id is never picked up
**[assumption: confirm in the claim query]**. The alternative, a new table and `source_kind`,
needs a migration on three CHECK constraints and new code in every consumer.

An intention is created with the row:

* `wake_policy = continue`. Tim asked for something in chat, so Nous must finish the answer.
* `intent` is generated: `Detached <tool> from chat turn <n> (session <s>). Why it ran: <reason>.
  Finish answering Tim's request with this result.` The tool's own `reason` argument is the best
  context Nous has here, and every tool call already carries one.
* The channel is the chat's channel, so the continuation replies in the same Telegram chat.

### 4.4 Completion

The registry's done-callback (scheduled on the loop, never blocking it):

* Takes the result text through the same post-processing as an inline result (SmartCompress,
  outside-content tagging, log redaction, `tool_call_log` row with the real `duration_ms`). One
  shared function, not a copy.
* Marks the subtask `completed`, or `failed` if `is_error`, and calls the existing
  `record_subtask_result` **[verified: nous/heart/result_inbox.py:624]**. F099 handles the rest:
  debounce, wake, continuation turn.
* On `detach_max_seconds`: cancels, which for bash kills the process group
  **[verified: builtin_tools.py:239, 256-260]**, and files "timed out after N s, outcome unknown".

### 4.5 Cancel

`cancel_task <id>` on a `detached_call` row calls `registry.cancel(id)` and the task gets
`CancelledError`. bash kills its process group, so the stop is real. For other tools a cancel
may leave a worker thread running (the same caveat as today's timeout, runner.py:6404-6407).
The cancel result says "cancel requested; outcome unknown" for those tools and "stopped" only
for bash.

### 4.6 Restart

The task lives in process memory, so a restart kills it. On startup, the reconciler finds
`detached_call` rows still `running` whose `worker_id` names a different process. It marks them
`failed` with "lost in restart, outcome unknown: check before re-running" and files that to the
inbox, so Nous is woken and doesn't assume success. No retry, because the call may have had
effects.

## 5. Honesty rules (hard)

* The claim verifier and finish checks treat a `detached` ledger entry as **pending**. A reply
  claiming its effect ("done", "created", "installed") gets flagged the same way an unverified
  claim does today.
* The final chat reply of a turn that detached something should name it: "still running in the
  background (task <id>); I'll report when it finishes." The task row and intention already
  exist, so this promise is backed (it satisfies the deferred-promise censor).
* The continuation turn sees the inbox result as data, not instructions (F099 framing, as it is).

## 6. Config

* `detach_mode: Literal["off","shadow","on"] = "off"`. In `shadow`, nothing is detached; each call
  that would have been detached is logged to `nous_system.events` (`detach_would_promote`) with
  the tool and its final duration.
* `detach_promote_after_seconds: int = 25`, ge 10, le `tool_timeout - 10`.
* `detach_max_seconds: int = 900`.
* `detach_tools: list[str] = ["bash","browser_task","web_fetch"]`.
* `detach_max_per_session: int = 3`, `detach_max_global: int = 8`.

Runtime switch through the owner config table like F120, so it can be changed without a restart
**[assumption: same `nous_system.config` pattern as F120]**.

## 7. Tests (merge gate)

1. Fake tool sleeping 40 s, `promote_after=1`: the turn gets the placeholder within ~1–2 s, the
   tool keeps running, the subtask row ends `completed`, and an inbox row exists with the real
   output.
2. The `finally` block doesn't cancel a detached task (regression for runner.py:6413).
3. Tracking-row write fails: no detach, inline behaviour as today.
4. Ineligible tools (`send_email`, `spawn_task`, an approval-held call) are never detached.
5. Over the concurrency cap: waits inline.
6. `cancel_task` on a detached bash call kills its process group (no child left in `ps`).
7. `detach_max_seconds` reached: cancelled, filed as outcome unknown.
8. Restart reconcile: an orphaned running row becomes `failed` with "lost in restart", and an
   inbox row is written.
9. Claim verifier flags "done" for a detached entry in the same turn.
10. `detach_mode=off`: byte-identical behaviour (golden test on the dispatch path).
11. `shadow`: nothing detached, events written.

## 8. Rollout

1. PR 1: registry, tracking row, completion and reconcile, `shadow` mode, tests 1–5, 8, 10, 11.
   Shipped dark (`off`).
2. Owner sets `shadow` for a week and reads the would-detach count (expect about 3–4 a day from §1).
3. PR 2: `on` path, cancel, claim-verifier rule, placeholder text, tests 6, 7, 9.
4. Owner sets `on`. Watch for duplicate re-runs (the model re-runs a call it was told is
   running) and for orphaned rows.

Size: about 1.5–2.5 days of Claude Code job time across both PRs **[assumption]**.

## 9. Open decisions (for Tim)

1. **Tracking row:** reuse `heart.subtasks` (recommended) or a new table plus `source_kind`?
2. **Threshold:** 25 s (recommended; it catches about 9 % of chat bash calls, while a lower value would detach calls that would have finished seconds later)?
3. **Wake policy:** `continue` (recommended; Nous finishes the answer) or `report` (Tim just
   gets the raw result)?
4. **Model hint:** let the model ask "don't detach this" (for example, when the next step needs
   the output)? Recommended: not in v1. Measure re-run and confusion rates in shadow/on first.

## 10. Later (not v1)

* Background contexts: same registry, but the parent is a subtask/DAG node and its own stall
  timer must count a detached call as activity.
* Raise bash's 300 s cap for detached calls only, which retires the "nohup and poll" hint.
* An `await_task(id, max_wait)` tool so a turn can choose to wait a little longer before ending.
* Option B (separate): expose DAGs and subtasks to outside clients as MCP Tasks.
