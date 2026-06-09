# Heartbeat Subsystem Audit (F034.x) — 2026-06-09

Code-only deep audit of `nous/heartbeat/` (runner, registry, checks, dynamic, finding_store, tuner, work_queue, schemas) plus the wiring in `nous/main.py`, the REST surface in `nous/api/rest.py`, and the work-queue handoff in `nous/heart/work_queue.py`. Reachability verdicts are evaluated against `nous/config.py` defaults **and** `.env.prod-snapshot`.

Verdict legend: **LIVE** = reachable with prod config; **LATENT** = reachable only via manual/non-default trigger; **INERT** = code exists but disabled/unwired in prod; **DEAD** = unreachable from any path.

---

## 1. How it actually works

**Construction (main.py:614-748).** When `heartbeat_enabled` (default true, prod true), main.py builds an `EscalationConfig` from settings (main.py:627-632), an in-memory `FindingStore`, and a `CheckRegistry`. It registers:

| Check | Registered when | Prod status |
|---|---|---|
| `HealthCheck` (interval 3600s) | always, permanent | **LIVE** |
| `SelfInitiatedCheck` (1800s) | always, permanent, with embeddings | **LIVE** |
| `EmailCheck` (180s, urgent_override) | `heartbeat_email_enabled` + `email_user` (main.py:642) | **LIVE** (prod `NOUS_HEARTBEAT_EMAIL_ENABLED=true`, creds set) — but constructed **without** `llm_callable` (HB-4) |
| `DriveCheck` (600s) | `heartbeat_drive_enabled` + `GOOGLE_SERVICE_ACCOUNT_JSON` (main.py:645) | **INERT** (no service-account JSON in prod env) |
| `BehaviorDriftCheck` (3600s) | `drift_detection_enabled` (default true) + bus (main.py:649-657) | **LIVE** (but its findings are dropped — HB-1) |
| `DynamicCheckLoader` + DB-backed dynamic checks | always | **LIVE** |
| `WorkQueueCheck` (300s) | `work_queue_enabled` + DAG enabled (main.py:723-748) | **LIVE** (prod `NOUS_WORK_QUEUE_ENABLED=true`, `file_jsonl`) |

**Tick loop (runner.py:147-187).** Sleeps `heartbeat_tick_interval` (30s default; not overridden in prod), resets the daily budget on local-date change, then runs `_tick()` — or `_tick(urgent_only=True)` during quiet hours (prod 03:00–12:00 UTC; only `EmailCheck` has `urgent_override`). The same loop then ticks the DAG orchestrator, periodically re-syncs dynamic checks (every 60 ticks ≈ 30 min), sends the daily digest at hardcoded UTC hour 9, and prunes/sweeps the FindingStore every 24h. Everything — checks, cognitive triage, callbacks dispatch, DAG ticks — runs **sequentially in this one task**.

**Per tick (runner.py:189-331).** Due checks (interval elapsed, circuit breaker closed: `consecutive_failures < 3`, registry.py:33-43) run one at a time under `asyncio.wait_for(check.run(), timeout=check.timeout)`. Findings from results with `has_updates=True` are stamped with `check_name`, fingerprinted (`sha256(check:source:digits→N(summary))`, schemas.py:33-37), auto-resolve bookkeeping runs (ACKNOWLEDGED findings absent from 2 consecutive successful runs get resolved), then `_triage` routes each finding through `FindingStore.ingest` → TRIAGE / SUPPRESS / ESCALATE. High-urgency findings go straight to Telegram; `needs_action` findings go to a cognitive session (`run_turn` on a dedicated forked AgentRunner with an isolated API client) if the daily token budget (prod 500K) has headroom. Usage from `run_turn` is the full tool-loop aggregate (api/runner.py:1499-1614), so budget accounting per session is correct.

**Dynamic checks (dynamic.py).** DB rows (`nous_system.dynamic_checks`, UNIQUE(agent_id, name)) become `DynamicCheck` instances whose `run()` sends a JSON-instruction prompt through `run_turn` with a tool allowlist (`ALLOWED_TOOLS`, dynamic.py:32-35) and parses the response with the tolerant `parse_llm_json`. A check can self-disable via `heartbeat_check_manage` mid-run; the runner then fires its `on_complete` callback as a background task with 3-layer failure handling (retry → Telegram → warning Finding).

**Tuner (tuner.py).** Reads outcome signals from the FindingStore per check, relaxes (>60% negative) or tightens (>80% positive) one tunable param per check per pass, with snapshot-based cross-cycle rollback. Only invoked from `POST /heartbeat/tune` (rest.py:2092). Nothing schedules it (HB-3).

**Work queue (work_queue.py + heart/work_queue.py).** `WorkQueueCheck` polls the `FileJsonlAdapter`, claims each new item via `INSERT … ON CONFLICT DO NOTHING RETURNING` on `nous_system.work_queue_items` (race-safe), creates a single-subtask DAG (`frame_type="research"`), and links it via `mark_dispatched`. Terminal items cancel their DAG before `mark_terminal`. A reconciler re-dispatches rows claimed >5 min ago that never got linked. Per-tick admission cap = `work_queue_max_dags_per_tick` (5); terminal handling is exempt from the cap.

---

## 2. Findings register

### P1

---

**HB-1 — BehaviorDriftCheck findings are silently dropped (drift alerting is a no-op)**
- Severity: P1 · Reachability: **LIVE**
- Where: `nous/heartbeat/checks.py:957` vs `nous/heartbeat/runner.py:244`
- Description: `BehaviorDriftCheck.run` returns `CheckResult(findings=findings)` without setting `has_updates`. `CheckResult.has_updates` defaults to `False` (schemas.py:44), and `_tick` only collects findings when `if result.has_updates:` (runner.py:244). Every other check sets `has_updates=bool(findings)` (checks.py:126, 467, 600, 849); drift is the lone omission.
- Evidence: drift anomalies (including `severity == "alert"` with `urgency="high"`, checks.py:946-952) never reach `all_findings`, so they are never triaged, never Telegram-alerted, never tracked in the FindingStore. The check still persists snapshots to `nous_system.behavior_snapshots`, so the DB side looks healthy while the alerting half of F035.3 is dead. Registered in prod (main.py:649-657; `drift_detection_enabled` default true, event bus on).
- Fix: `return CheckResult(has_updates=bool(findings), findings=findings)`.

---

**HB-2 — Startup suppression permanently captures first-run findings; SUPPRESSED has no exit to triage**
- Severity: P1 · Reachability: **LIVE**
- Where: `nous/heartbeat/finding_store.py:51-62` (capture), `:64-89` (no SUPPRESSED→NEW transition), `:311-313` (window), `nous/heartbeat/registry.py:39-40` (first-run due immediately)
- Description: For 5 minutes after process start, every new finding is stored with `state=SUPPRESSED` (finding_store.py:53-62). There is no code path that moves a SUPPRESSED finding back to NEW/TRIAGE: on every subsequent ingest of the same fingerprint the store updates `last_seen`, bumps `seen_count`, and returns SUPPRESS again (finding_store.py:64-89) until the *time-based escalation* threshold fires (24h for normal, 72h for low).
- Evidence: all permanent checks have `last_run=None` at boot, so `is_due` is true on the **first tick (~30s after start)** — deterministically inside the 5-minute window. Therefore the steady-state recurring findings (e.g. HealthCheck's "N decisions pending review", whose fingerprint is stable because digits are normalized, schemas.py:35) are captured as SUPPRESSED on every restart, skip cognitive triage entirely, and only surface 24–72h later as **escalated-to-high Telegram alerts** (runner.py:394-396). If the process restarts more often than the escalation threshold, these findings never surface at all (the in-memory store resets `first_seen` on every restart). `heartbeat_suppression_ttl_hours` (config.py:822) was presumably meant to bound this and is consumed nowhere.
- Fix: when the startup window has passed, transition SUPPRESSED findings ingested during the window to NEW (return TRIAGE on next sighting), or stamp suppressed-at and expire suppression after `heartbeat_suppression_ttl_hours`.

---

### P2

---

**HB-3 — Scheduled self-tuning never implemented; four tuning settings are severed wires**
- Severity: P2 · Reachability: **LATENT** (manual REST only; prod `NOUS_HEARTBEAT_TUNING_ENABLED=true` expects it to run)
- Where: `nous/heartbeat/runner.py:78-79` (tuner + `_last_tune` created, never used in the loop), `nous/api/rest.py:2084-2092` (only call site of `tuner.tune`), `nous/config.py:826-829`
- Description: `HeartbeatRunner._loop` never calls `tuner.tune`. `heartbeat_tuning_interval_hours` (weekly), `heartbeat_tuning_min_samples`, `heartbeat_tuning_learning_rate`, and `heartbeat_tuning_rollback_threshold` are consumed by no code — `HeartbeatTuner` hardcodes `MIN_SAMPLES=10`, `LEARNING_RATE=0.1` (tuner.py:39-41) and a rollback trigger of `neg_rate > 0.8` (tuner.py:169), which doesn't even match the documented semantics of the setting ("negative rate **increase** of 0.2").
- Evidence: grep over the tree shows `heartbeat_tuning_enabled` read only by rest.py (gate on the manual endpoint + dashboards); the other four fields have zero readers. Operator turned tuning on in prod 2026-x believing F034.3's "weekly pass" runs; it does not.
- Fix: add an interval gate in `_loop` analogous to `_maybe_prune_and_sweep`, and thread the four settings into `HeartbeatTuner`.

---

**HB-4 — EmailCheck LLM classification tier is unreachable (never wired)**
- Severity: P2 · Reachability: tier **DEAD** on a **LIVE** check
- Where: `nous/main.py:643` (`registry.register(EmailCheck(settings))`), `nous/heartbeat/checks.py:543-555, 615`
- Description: `EmailCheck.__init__` accepts `llm_callable` and `budget_check` for F034.2's tiered classification (reputation → LLM → keywords), but main.py constructs it with neither. `self._llm_callable` is always `None`, so the Tier-1 branch (checks.py:615) never executes; every email is classified by the 4-keyword heuristic (checks.py:694-701), and the sender-reputation cache (checks.py:628-666) learns from keyword output only.
- Evidence: only construction site in the tree is main.py:643. CLAUDE.md/F034.2 advertise "LLM email classification"; in prod (email check live, 180s interval) it has never run.
- Fix: pass a Haiku-backed callable + `heartbeat_runner._has_budget` at wiring time (the constructor was designed for exactly this).

---

**HB-5 — Dynamic-check execution is not gated on the daily token budget**
- Severity: P2 · Reachability: **LIVE**
- Where: `nous/heartbeat/runner.py:214-225` (checks run unconditionally; tokens added post-hoc), `:744-746` (`_has_budget` checked only for triage at :450 and callbacks at :276/:562)
- Description: `_tick` runs every due check regardless of `_tokens_used_today`. Dynamic checks each open a full `run_turn` cognitive session (dynamic.py:123-131); their tokens are *counted* after the fact (runner.py:224-225) but exhaustion never stops the next check from running. The budget therefore only suppresses triage and callbacks — the largest spenders (agentic dynamic checks with tools, prod model Sonnet, up to 10 checks) bypass it entirely and can overrun the 500K/day cap without bound.
- Evidence: contrast with `_triage` (runner.py:450-457) and callback dispatch (:276-285), which both check `_has_budget()` first. There is no analogous gate in the `for check in due_checks` loop. Additionally, tokens consumed by a check that *times out* are never counted at all (the usage dict is lost when `wait_for` cancels, runner.py:251-261), so the counter also under-reports.
- Fix: skip (or defer) `DynamicCheck` instances when `not self._has_budget()`, mirroring the triage gate.

---

**HB-6 — Cognitive triage and check execution block the entire heartbeat loop (including DAG orchestration) with no outer timeout**
- Severity: P2 · Reachability: **LIVE**
- Where: `nous/heartbeat/runner.py:147-187` (single sequential loop), `:476-533` (`_cognitive_triage` — no `asyncio.wait_for`)
- Description: `_tick` → `_triage` → `_cognitive_triage` runs `run_turn` inline with no timeout. Prod runs `NOUS_MAX_TURNS=600` and `NOUS_TOOL_TIMEOUT=2000`; a single pathological triage session can stall the loop for tens of minutes. While stalled: no checks run (email check misses its 180s cadence), the **DAG orchestrator does not tick** (runner.py:161-165 — DAG advancement is piggybacked on this same loop), dynamic-check sync stops, and the daily digest can be skipped if the stall spans UTC hour 9 (`now.hour == 9` check at :646 evaluates after the stall).
- Evidence: the only timeouts in the path are per-API-call read timeouts inside the runner; there is no bound on the tool loop for `platform="heartbeat"` sessions. F048 keep-alives make long generations *more* likely to run to completion, not less.
- Fix: wrap `_cognitive_triage` in `asyncio.wait_for` with a settings-derived cap, and/or move DAG orchestrator ticking to its own task.

---

**HB-7 — Work-queue dispatch is not cancellation-safe: check timeout between `dag_store.create` and `mark_dispatched` yields a duplicate DAG**
- Severity: P2 · Reachability: **LIVE**
- Where: `nous/heartbeat/work_queue.py:293-355` (`_claim_and_dispatch`), `:357-403` (`_reconcile_orphan` — re-**creates**), `nous/heartbeat/runner.py:216-219` (`wait_for(check.run(), timeout=30)`)
- Description: `WorkQueueCheck` inherits `timeout=30` (registry.py:23). `asyncio.wait_for` *cancels* `run()` on timeout. If cancellation lands after `dag = await self._dag_store.create(request)` but before `mark_dispatched`, the cleanup branch does **not** run — `except Exception` (work_queue.py:322) does not catch `CancelledError` — so the orphan DAG is never cancelled. Five minutes later `list_undispatched` returns the row and `_reconcile_orphan` calls `self._dag_store.create(request)` **again** (work_queue.py:372), producing a second DAG executing the same work item. The comment at work_queue.py:303-307 claims "the reconciler will retry the link (not re-create)" — the code contradicts it: `_reconcile_orphan` has no path that re-links an existing DAG.
- Evidence: prod has work_queue LIVE with a default request factory that spawns subtasks (side-effectful agent work). DAG creation is multiple DB roundtrips; under DB load with up to 5 dispatches + reconciler inside one 30s window, mid-flight cancellation is realistic. The same gap applies to ordinary exceptions thrown by `mark_dispatched` *when the compensating `cancel_dag` also fails* (acknowledged "we just leak the DAG" at :328-331 — but the next reconciler pass then duplicates it, which the comment misses).
- Fix: persist the created `dag_id` on the row *before* the DAG becomes runnable, or have the reconciler first look for an existing non-terminal DAG named `work_queue:{safe_ext}` and re-link instead of re-creating; shield the create+link section from cancellation (`asyncio.shield`) or catch `BaseException` for cleanup.

---

**HB-8 — Circuit breaker never auto-recovers and opening it is invisible**
- Severity: P2 · Reachability: **LIVE**
- Where: `nous/heartbeat/registry.py:36-38` (`is_due` returns False forever once `consecutive_failures >= 3`), `:50-58` (open = log.warning only)
- Description: Three consecutive failures permanently disable a check until someone calls `POST /heartbeat/check/{name}/reset` or restarts the process. There is no half-open/retry-after-cooldown state, and the only signal that a breaker opened is a log line — no Finding, no Telegram, no event-bus emission.
- Evidence: `EmailCheck` raises on any IMAP failure (checks.py:572-575) and runs every 180s, so a ~9-minute Gmail/IMAP blip silently kills email monitoring (the subsystem's flagship prod feature) indefinitely. `WorkQueueCheck` similarly re-raises adapter/DB failures by design (work_queue.py:233-239) — three DB hiccups and work ingestion stops forever. The `/heartbeat/checks/dynamic` listing and dashboard expose `circuit_breaker_open`, but only if someone looks.
- Fix: add a cooldown-based half-open (e.g. retry once after `interval * 2^failures`, capped), and emit a Finding/Telegram when a breaker opens.

---

**HB-9 — Escalation skips the documented ladder: low findings jump straight to high (immediate Telegram)**
- Severity: P2 · Reachability: **LIVE**
- Where: `nous/heartbeat/runner.py:393-396` (`f.urgency = "high"` for every ESCALATE), `nous/heartbeat/finding_store.py:291-309`, `nous/heartbeat/schemas.py:92-98`
- Description: `EscalationConfig` defines a low→normal (72h) and normal→high (24h) ladder, and the settings/REST surface advertise it (`low_to_normal_hours`). But `_triage` upgrades **every** ESCALATE action to `"high"`, so a low-urgency finding that ages 72h goes directly to an urgent Telegram page rather than entering the normal-triage pool. The store also marks `escalated=True` on the original tracked finding (finding_store.py:85-87), so low/normal findings escalate exactly once — there is no second-stage normal→high progression at all.
- Evidence: `low_to_normal_hours` is used only as a time gate (finding_store.py:299-300); nothing ever produces `urgency="normal"` from an escalation. Combined with HB-2 this means restart-suppressed low findings reappear *only* as urgent pages.
- Fix: map the escalation step to the next rung (`low→normal` re-enters triage; `normal→high` pages), keeping high re-alert as is.

---

### P3

---

**HB-10 — Digest hour hardcoded; `heartbeat_digest_hour_utc` is a severed setting**
- P3 · LIVE (coincidence: prod uses default 9) · `runner.py:646` (`if now.hour == 9`), `config.py:821`. Changing the env var does nothing. Fix: read the setting.

**HB-11 — `heartbeat_suppression_ttl_hours` consumed nowhere**
- P3 · DEAD setting · `config.py:822`; `FindingStore._startup_suppression_seconds` hardcoded 300 (finding_store.py:35). Companion to HB-2.

**HB-12 — Tuner step math has a units bug**
- P3 · LATENT · `tuner.py:134-137`: `step = param.step * LEARNING_RATE * param_range` multiplies the param's natural step by the *range*, then clamps to 10% of range. For `stale_fact_days` (step 5, range 83) the raw step is 41.5 → always clamped to 8.3; for `similarity_threshold` (step 0.02, range 0.3) the step is 0.0006 — 1000× smaller than its declared step. Adjustments are either "always max" or "effectively zero" depending on units. Fix: `step = param.step` or `LEARNING_RATE * param_range`, not both.

**HB-13 — Tuner relax/tighten direction is wrong for count/lookback params**
- P3 · LATENT · `tuner.py:139-142` assumes increase = less sensitive ("relax = increase threshold"). For `max_findings_per_run`, `max_pending_items`, `lookback_days`, `cross_reference_lookback_hours` (checks.py:55, 175, 174, 781), increasing produces *more* findings — i.e. the tuner would respond to noise by amplifying it. Fix: per-param direction metadata on `TunableParam`.

**HB-14 — Rollback evaluation window includes pre-adjustment outcomes**
- P3 · LATENT · `tuner.py:163`: `_check_and_rollback` uses `get_outcomes_for_check` with the default 30-day window, despite the docstring claiming "outcomes accumulated AFTER the previous adjustment". A check with chronically bad outcomes gets rolled back regardless of whether the adjustment helped. Fix: pass `since_seconds` = time since `_last_tune`.

**HB-15 — Outcome-signal economy makes the tuner effectively inert**
- P3 · LATENT · Emitters: REST resolve → POSITIVE (rest.py:2004), REST dismiss → STRONG_NEGATIVE (finding_store.py:125), 72h sweep → WEAK_NEGATIVE (finding_store.py:235). `NEGATIVE`, `STRONG_POSITIVE`, `NEUTRAL` are never emitted anywhere (grep-verified). But `_negative_rate` counts only STRONG_NEGATIVE+NEGATIVE and `_positive_rate` only STRONG_POSITIVE+POSITIVE (tuner.py:186-204) — so WEAK_NEGATIVE (the *dominant* signal, generated automatically) dilutes both rates toward zero and the >60%/>80% triggers essentially require the operator to manually dismiss/resolve via REST. Auto-resolve (runner.py:367) records **no** outcome at all. Fix: count WEAK_NEGATIVE at fractional weight; emit POSITIVE on auto-resolve-after-action or NEUTRAL on auto-resolve.

**HB-16 — "Embedding search" never uses the embeddings; threshold compares the wrong score space**
- P3 · LIVE · `checks.py:178-189` embeds the 5 `PENDING_PROTOTYPES` into `_prototype_cache`, which is then used **only as a truthiness gate** (:196-198); the actual matching is `heart.facts.search(proto_text)` — text/hybrid search with the prototype *string* as query. The cached vectors are never compared to anything. Worse, the fallback branch `is_pending = score >= threshold` (:247) compares a hybrid/RRF score (≪0.1 typical — cf. memory-retrieval whitepaper) against `similarity_threshold=0.75`, so it is constant-false; unclassified facts are detected solely by substring patterns. Fix: either do real cosine scoring against the prototypes or delete the cache and rename.

**HB-17 — `lookback_days` tunable read but never applied**
- P3 · LIVE (no-op) · `checks.py:202, 328`: fetched in `_embedding_search` and `_temporal_scan` but no query uses it — fact searches have no recency filter. The tuner could "adjust" it forever with zero effect. Fix: thread into `facts.search` or remove the param.

**HB-18 — Layer-3 callback failure Finding is write-only: never triaged, never expires**
- P3 · LIVE (when callbacks fail) · `runner.py:613-628`: the warning Finding is `ingest()`ed directly; the returned action is ignored, no triage/Telegram/acknowledge happens (Layer 2's Telegram is the only notification), and the entry sits in state NEW forever — escalation requires re-ingest of the same fingerprint (never happens for one-shot callbacks), auto-resolve requires ACKNOWLEDGED, and `prune()` removes only RESOLVED (finding_store.py:240-250). It is visible only via `GET /heartbeat/findings`. Fix: route it through `_triage([failure_finding])` instead.

**HB-19 — `heartbeat_triage` event lineage points at the *previous* tick**
- P3 · LIVE · `runner.py:298-303` runs `_triage` (which reads `getattr(self, "_current_tick_event", None)` at :511) **before** the tick event is created and assigned at :307-329. The triage event's `trace_id`/`caused_by` therefore reference the prior tick's event (or None on the first findings-bearing tick). F035 causal traces for heartbeat are systematically off-by-one. Fix: build/emit the tick event before triage.

**HB-20 — Fire-and-forget `create_task` without reference retention**
- P3 · LIVE · `runner.py:277-280, 863-866`: callback tasks are not stored; per asyncio docs the loop holds only a weak reference, so a mid-flight GC can drop a running callback. Fix: keep a `set` of tasks with `add_done_callback(discard)`.

**HB-21 — REST `trigger_tick`/`trigger_check` race the background loop**
- P3 · LIVE · `runner.py:835-846`: a forced tick runs concurrently with `_loop`'s `_tick`; both see the same checks as due (`last_run` updates only after completion), so the same check can run twice concurrently (double IMAP fetch → duplicate findings pre-dedup; double work-queue poll → benign thanks to DB claim, but doubled DAG-create attempts). Fix: an `asyncio.Lock` around `_tick`.

**HB-22 — Telegram delivery failure is swallowed; one-shot urgent alerts are lost permanently**
- P3 · LIVE · `runner.py:705-725`: any exception → `logger.warning`, no retry, no fallback. The findings were already `acknowledge()`d in `_triage` (:402) before the send, and low/normal escalations fire only once (`escalated=True`), so a transient Telegram outage permanently eats those pages; only high-urgency re-alert (12h) retries. Fix: don't mark escalated until send succeeds, or add a bounded retry.

**HB-23 — IMAP fetch marks messages `\Seen` (no `BODY.PEEK`)**
- P3 · LIVE · `checks.py:716`: `fetch(mid, "(BODY[HEADER.FIELDS (...)])")` implicitly sets `\Seen` on the message. The agent silently marks inbox mail as read on every poll; if the account is ever a shared/human inbox this destroys unread state, and it makes the `UNSEEN` search + `_seen_ids` cache double-dedup (the cache is mostly redundant). Fix: `BODY.PEEK[HEADER.FIELDS (...)]`.

**HB-24 — Cron dynamic checks: immediate first fire, `MIN_INTERVAL` bypass, unbounded timeout**
- P3 · LIVE · `dynamic.py:85-88`: with no `last_run`, the croniter anchor is 2000-01-01 so any cron check fires on the first tick after registration regardless of schedule. `create_check` skips the 300s minimum when `cron_expr` is set (:357), so `* * * * *` (every minute) is accepted. Neither `create_check` nor `manage_check("update")` bounds `timeout_seconds` (:347, :503), so a huge value lets one check monopolize the (single-threaded) tick loop, and a non-positive value makes it instantly time out into the circuit breaker. Fix: anchor first cron fire at registration time; enforce a cron floor; clamp timeout to e.g. [5, heartbeat_default_check_timeout*4].

**HB-25 — Terminal work-queue items are re-processed every tick forever**
- P3 · LIVE · `work_queue.py:405-456`: `_handle_terminal` never checks `existing.terminal_state` — as long as the terminal line stays in the JSONL file (operators don't truncate it), every 300s tick re-calls `cancel_dag` (idempotent early-return at orchestrator.py:130-132) and re-runs `mark_terminal`, and re-appends a "Cancelled DAG …" finding (deduped downstream). If the DAG row was deleted, `cancel_dag` raises `ValueError` (orchestrator.py:128) → an `urgency="high"` finding loops every tick. Fix: short-circuit when `existing.terminal_state is not None`.

**HB-26 — FileJsonlAdapter body-hash IDs collide for identical bodies**
- P3 · LIVE · `work_queue.py:119-125`: when `external_id` is omitted, it is derived from `sha256(body)`. Two distinct items with identical bodies (e.g. templated "run the nightly report" entries) collapse to one row; the second is never dispatched, silently. Fix: include title/line-number in the hash or require external_id.

**HB-27 — Work-queue ingestion and all dynamic checks pause 9h/day in prod quiet hours**
- P3 · LIVE · `runner.py:154-158`: quiet hours (prod 03–12 UTC) restrict ticks to `urgent_override` checks — only EmailCheck qualifies. `WorkQueueCheck` and every dynamic check (unless created `urgent=True`) stop polling for 9 hours; queued work items wait until 12:00 UTC. Probably unintended for a machine-facing ingestion path. Fix: set `urgent_override=True` on WorkQueueCheck (it pages nothing by itself) or exempt non-notifying checks from quiet hours.

**HB-28 — Budget resets on *local* midnight; everything else is UTC**
- P3 · LIVE · `runner.py:748-754` uses `date.today()` (local tz) while quiet hours/digest/escalations use UTC. In a non-UTC container the 500K budget window is offset from the digest/quiet windows. Fix: `datetime.now(UTC).date()`.

**HB-29 — DriveCheck loses the modification window on failure; timestamp has no timezone**
- P3 · **INERT** in prod (no service-account JSON) · `checks.py:796-801`: `_last_check_time` is advanced *before* the fetch; if `list_files` raises, the next run's cutoff is the failed run's start, so files modified in the window preceding the failure are permanently skipped. The Drive query also formats the cutoff without `Z`/offset. Fix: advance the watermark only on success; use RFC3339 with `Z`.

**HB-30 — Renaming a dynamic check in the DB orphans the old registry entry**
- P3 · LATENT (rename impossible via API — `name` not in `allowed_fields`, dynamic.py:494-498 — but possible via direct DB edit) · `sync()` keys unregistration on removed *ids* (:227-232); a same-id name change registers the new name and overwrites `_id_to_name`, leaving the old-name `DynamicCheck` registered and running forever. Fix: detect name changes per id during sync.

**HB-31 — Mid-run sync replacement can double-run a dynamic check**
- P3 · LATENT · `dynamic.py:269-277`: if a check's signature changed while an instance is mid-`run()`, sync registers a fresh instance copying the *pre-run* `last_run`; the in-flight instance's eventual `mark_success` lands on the orphaned object, so the new instance is immediately due again → back-to-back duplicate run. Narrow window (sync every 60 ticks), no corruption. Fix: copy state at completion or look up the live instance in `mark_*`.

---

### INFO

- **HB-I1** — Quiet hours documented as "user timezone" (CLAUDE.md, config comments) but computed in UTC (`runner.py:733`). Prod values (3–12) appear pre-compensated; document it.
- **HB-I2** — Tokens consumed by a check that times out are never added to the budget counter (usage lost on cancellation, `runner.py:251-261`); daily spend is under-reported.
- **HB-I3** — Findings are `acknowledge()`d in `_triage` (runner.py:402) *before* Telegram/cognitive triage occur; a triage crash or budget-skip still leaves them ACKNOWLEDGED (later swept WEAK_NEGATIVE) — "acknowledged" does not mean "seen".
- **HB-I4** — `BehaviorDriftCheck.run` swallows all exceptions (`checks.py:955-957`), so its circuit breaker can never trip and persistent DB failures are invisible (debug-level logs in `_store_snapshot`/`_load_baseline`).
- **HB-I5** — Email header parsing is line-naive: RFC2822 folded headers are truncated to their first line and MIME-encoded-word subjects (`=?UTF-8?...?=`) are not decoded (`checks.py:724-731`); only the last 5 unseen messages are examined per poll (`:714`).
- **HB-I6** — `heartbeat_default_check_timeout` (prod 120) applies only to *dynamic* checks (dynamic loader default, main.py:666); built-ins hardcode `timeout` 30/20/30 (checks.py:44, 157, 537, 764) and ignore the setting.
- **HB-I7** — `dynamic.py:242, 382` and `rest.py:2028` reach into private attributes (`registry._permanent`, `store._escalation`) across module boundaries.
- **HB-I8** — Every already-dispatched JSONL item costs one INSERT-conflict roundtrip per item per tick (`work_queue.py:308`, heart/work_queue.py:59-83); fine at small scale, quadratic-ish as the file grows since dispatched lines are never removed.
- **HB-I9** — Prod path suspicion: `NOUS_WORK_QUEUE_FILE_JSONL_PATH=/tmp/nous_workspace/task_queue/tasks.jsonl` (underscore) vs `NOUS_WORKSPACE_DIR=/tmp/nous-workspace` (hyphen). If the underscore directory is wrong, the adapter logs "path does not exist" every 300s and ingests nothing (`work_queue.py:92-96`) — worth a one-line prod check.
- **HB-I10** — `PUT /heartbeat/escalation-policy` and `PUT /heartbeat/config` mutate in-memory state only; changes are lost on restart (no persistence).
- **HB-I11** — `Finding.needs_action` from dynamic-check LLM output is taken as-is (`dynamic.py:176`) — a truthy string like `"false"` becomes `True`. `parse_llm_json` is otherwise robustly fenced.

---

## 3. Dead-code inventory

| Item | Location | Note |
|---|---|---|
| `HeartbeatRunner._last_tune` | runner.py:79 | assigned `None`, never read/written again (the tuner keeps its own) |
| `SelfInitiatedCheck._prototype_cache` vectors | checks.py:171, 178-189 | embedded, cached, never compared (HB-16) |
| `lookback_days` tunable | checks.py:174, 202, 328 | read into locals, never used in any query (HB-17) |
| `EmailCheck` params `sender_reputation_weight`, `llm_classification_budget` | checks.py:560-563 | declared (and tuner-adjustable) but never read |
| `DriveCheck` param `cross_reference_lookback_hours` | checks.py:781 | declared, never read (`_contextualize` has no lookback) |
| `DEFAULT_FOLDER_MAP` | checks.py:753 | defined, zero references |
| Settings: `heartbeat_digest_hour_utc`, `heartbeat_suppression_ttl_hours`, `heartbeat_tuning_interval_hours`, `heartbeat_tuning_min_samples`, `heartbeat_tuning_learning_rate`, `heartbeat_tuning_rollback_threshold` | config.py:821-829 | no readers (HB-3/10/11) |
| `EmailCheck` LLM tier (`_llm_classify`, `llm_callable` plumbing) | checks.py:543-555, 668-691 | unreachable in any wiring (HB-4) |
| `OutcomeSignal.NEGATIVE`, `STRONG_POSITIVE`, `NEUTRAL` | schemas.py:80-88 | never emitted anywhere (HB-15) |
| `_OBSERVATION_PATTERNS` re-export | checks.py:138 | declared back-compat shim; no external importers found in-tree |
| `GithubIssuesAdapter`, `LinearAdapter` | work_queue.py:137-158 | declared v2 stubs (intentional) |
| `WorkQueueAdapter.get_state` mention | work_queue.py:56-58 | docstring promises an optional method that exists nowhere |

---

## 4. Improvement opportunities

1. **Persist the FindingStore.** Everything F034.1 tracks (states, outcomes, escalation timers, reputation in EmailCheck, `_seen_ids`) is in-memory and resets on every deploy — which is the root enabler of HB-2 and makes outcome history (HB-15) too sparse for the tuner to ever act. A small `nous_system.heartbeat_findings` table would fix the restart pathology, give the tuner real sample sizes, and let the digest survive restarts.
2. **Decouple the loop.** One task currently serializes checks, triage, callbacks budget-decisions, DAG orchestration, digest, and prune (HB-6). Run checks with `asyncio.gather` under per-check timeouts, and give the DAG orchestrator its own ticker.
3. **Make budget a first-class gate** (HB-5): one `_charge(tokens)`/`_reserve()` helper used by triage, callbacks, *and* dynamic checks, counting timed-out sessions (HB-I2), reset on UTC midnight (HB-28).
4. **Close the outcome loop end-to-end** (HB-15): auto-resolve → NEUTRAL/POSITIVE, sweep → counted negative, cognitive triage result ("acted" vs "ignored") → signal. Then schedule the tuner (HB-3) — today F034.3 is advertising, not behavior.
5. **Half-open circuit breakers + breaker-open Findings** (HB-8) — the email check is the prod feature most likely to die silently today.
6. **Work-queue hardening**: raise `WorkQueueCheck.timeout`, make dispatch cancellation-safe (HB-7), short-circuit recorded terminals (HB-25), and emit a startup Finding if the configured JSONL path doesn't exist (HB-I9) instead of a 300s log loop.
