# Nous Complete Code Audit — Master Synthesis (2026-06-09)

Code-only audit of every major subsystem, driven by actual source at HEAD `2f6a193`
(post PR #495). Eleven subsystem reviews were produced by parallel agents; every P1
below was **personally re-verified against the source** by the synthesizing session
(file:line evidence inline). Reachability is judged against `nous/config.py` defaults
**and** the prod overlay `.env.prod-snapshot`.

Per-subsystem detail lives in the sibling docs:
`brain-`, `cognitive-layer-`, `runtime-core-`, `agent-tools-`, `handlers-`,
`rest-mcp-dashboard-`, `heart-`, `heartbeat-`, `dag-`, `storage-config-`,
`skills-identity-observability-audit-2026-06-09.md`.

---

## 1. The three dominant defect patterns

Almost every confirmed P1 is an instance of one of three recurring structural bugs.
This is the most important takeaway: the bugs are not random, they are **the same
three mistakes repeated at sites the earlier targeted audits/PRs did not reach.**

### Pattern A — RRF *rank* score compared to a *similarity* threshold
`heart` hybrid search returns RRF-fused scores that encode **rank**, not closeness:
the top hit scores ≈0.95 for *any* query (documented at `heart/procedures.py:502-506`).
PR #495 fixed this at the fact-dedup sites. It survives at:
- **HD-1 (P1)** `procedure_learner.py:561` — `_is_duplicate` thresholds the RRF score `>0.85` → top procedure always "duplicate" → **all** auto-procedure creation suppressed (the prod "K-line = 0").
- **HD-2 (P1)** `sleep_handler.py:777` — UPDATES-prefix supersession compares RRF score to `0.80` → can deactivate an unrelated fact.
- **CL-3 (P2)** `context.py:1027-1044` — usage boost sorts by RRF-derived factor, destroying relevance order.
- **CL-15 / HT-3 (P2)** WM floor `0.7` and procedure floor `0.40` and censor keyword score hardcoded `1.0` are all compared against RRF facts in the cross-type merge.

**Fix shape:** use a raw-cosine probe (`find_similar_*`) with a cosine-calibrated
threshold wherever a *closeness* decision is made; never threshold the RRF rank score.

### Pattern B — F036 cache-split ignores the flat `system_prompt`
When `cache_split_system_prompt` is on (prod default), `runner._build_system_prompt`
reads only `turn_context.sections_by_tier` and **ignores the flat `system_prompt`
string** (`runner.py:1916-1952`). F078 fixed censors by writing to *both*
(`layer.py:874-893`). Two producers still write only the ignored flat string:
- **CL-1 (P1)** `layer.py:642-672` — subtask results appended to the flat string **and rows marked `delivered` first** → the parent agent permanently never sees subtask outcomes.
- **CL-2 (P2)** `layer.py:906-918` — F065 hub-shift notice, same discarded string; snapshot still persisted so the shift is consumed unseen.

**Fix shape:** route these into `sections_by_tier["dynamic"]` (as F078 does), and only
mark subtask rows delivered once the content is actually placed in a read surface.

### Pattern C — severed self-improvement / telemetry wires (built, flag-ON, no consumer)
Features are fully built and enabled in prod but nothing reads their output:
- **BR-7** `brain/guardrails.py` — `Brain.check`/GuardrailEngine has zero runtime callers.
- **HB-3** scheduled heartbeat tuning never invoked (`tuner.tune` is REST-only) despite `NOUS_HEARTBEAT_TUNING_ENABLED=true`.
- **HB-4** EmailCheck built without `llm_callable` → F034.2 LLM classification tier dead.
- **DG-5** DAG LLM fix dispatch: `llm_client` never passed; `NOUS_DAG_FIX_LLM_DISPATCH_ENABLED=true` is inert.
- **DG-3** DAG token budget: `update_dag_tokens` has zero callers, `partial` status unreachable.
- **CL-6 (P2)** operator budget overrides (`facts=3000/decisions=2500`) silently REPLACED back to 500/500 every conversation-frame turn.
- **HD-9** rubric loop: `session_ended` carries no `summary` → `self_improvement_scores` always NULL (known RA-R1, still live).
- **OB-1/OB-3** `nous_system.context_log` grows unbounded (no retention consumer) and its token/latency columns are never written.

---

## 2. Confirmed P1 register (personally verified)

| ID | File:line | Bug | Verdict |
|----|-----------|-----|---------|
| **CL-1** | `cognitive/layer.py:663` vs `api/runner.py:1916-1952` | Subtask results to flat prompt the split path ignores; rows marked delivered → permanent loss | LIVE |
| **HD-1** | `handlers/procedure_learner.py:561` | RRF rank vs 0.85 → all auto-procedure creation suppressed (K-line=0) | LIVE |
| **HD-2** | `handlers/sleep_handler.py:777` | RRF rank vs 0.80 → can supersede/deactivate an unrelated fact | LIVE |
| **BR-1** | `brain/brain.py:1316-1342` | fact/episode/chunk neighbor queries lack `active` filter (only procedures got F080) → superseded facts resurface in recall | LIVE |
| **HT-1** | `heart/search.py:191,241` + `heart/episodes.py:235,519-527` | episode hybrid search filters `active=true AND outcome!='abandoned'` → excludes both closed and ongoing episodes → episode semantic recall structurally dead | LIVE |
| **HT-2** | `heart/facts.py:473-478` vs `608-613,719-731` | F075 date-distinct dupe not added to `exclude_ids`; `_find_contradiction` re-collides and supersedes the older dated event the bypass preserved | LIVE |
| **HB-1** | `heartbeat/checks.py:957` vs `runner.py:244` | BehaviorDriftCheck returns findings without `has_updates=True` → all drift alerts silently dropped | LIVE |
| **DG-1** | `dag/orchestrator.py:1379-1397` | DAG finishing via skip-and-continue success path finalized as `failed` | LIVE |
| **RM-1** | `docker-compose.yml:5-6,116-117` + `config.py:299` | REST+MCP+dashboard zero-auth on 0.0.0.0; Postgres also published with default password | LIVE |
| **ST-1** | `docker-compose.yml:70` + `config.py:195` | `NOUS_CONTEXT_BUDGET_OVERRIDES=${...:-}` passes empty string to JSON dict field → fresh `docker compose up` without host `.env` crash-loops | LIVE (clean-install) |
| **SK-1** | `api/tools.py:975-986` | `learn_skill` dedup misses superseded rows → re-learn resurrects archived duplicate (reopens F081 loop on agent path) | LIVE |

(Rubric P1 chain RA-R1..R3 from the 2026-06-09 rubric audit remains live at HEAD; not
re-listed here.)

## 3. High-value P2 register (selected; full lists in subsystem docs)

- **CL-4** `claim_verifier.py:88-98` — claim grounding checks tool *name only*, ignoring status/args → any `bash` "verifies" "I deployed"; enforce-mode net is hollow.
- **CL-5** `critic.py:377-380` + `layer.py:1209` — `record_decision` without confidence stores `None`; `None < 0.4` → TypeError in post_turn on the non-streaming (REST/subtask) path.
- **HD-4** `decision_reviewer.py:51-135` — auto-review corrupts Brain calibration three ways (error-keyword in description → fail; any existing referenced file → success; hardcoded episode "success").
- **HD-5** `time_parser.py:151-159` + `heart/schedules.py:152` — timezone token captured then discarded → "daily 9am EST" fires 9am UTC.
- **HT-4** `heart/subtasks.py:143-203` — `complete()`/`fail()` unconditional UPDATE with no precondition → stale worker overwrites a re-run.
- **RT-1** `anthropic_client.py:1167` + `runner.py:1196` — streamed `input_tokens` double-counted; `None` injection → TypeError kills a successful streamed turn.
- **RT-3** `config.py:452-460` + `compaction.py:593` — `tool_metadata_degrade_after`/`tool_hard_clear_after` severed (prod 60/90 ignored; hardcoded profile used).
- **RT-8/RT-9** telegram overflow duplicates messages; sequential bot loop blocks all chats on one hung turn.
- **HB-5..HB-9** dynamic-check budget not gated; triage no timeout (stalls tick + DAG); work-queue cancel → duplicate DAG; circuit breaker never auto-recovers; every escalation forced `urgency=high`.
- **DG-2/DG-4/DG-6/DG-7** can't cancel RUNNING subtasks; `_MAX_PENDING=5` permanently fails nodes instead of deferring; work-queue DAGs never `start_dag`; cancel/retry not serialized with tick.
- **RM-3/RM-4** MCP constant session ids collide concurrent callers; unbounded/negative `limit` on list endpoints.
- **ID-2** `identity/manager.py:92-125` — concurrent `update_section` → two `is_current=true` → `MultipleResultsFound` permanently bricks that section.
- **OB-2** 5 of 11 drift metrics never populated → constant zero → skipped forever.

## 4. Dead code (headline items)
`heart/content_date_extractor.py` (324 lines, zero consumers) · `brain` calibration
snapshot (`generate_calibration_snapshot`/`CalibrationSnapshot` never written or read) ·
`Brain.link` unused · `ContextEngine.expand`/`refresh_needed`/`_dedup_decisions` zero
callers · `ExecutionLedger.has_blocked_actions_this_turn` zero callers · `web_tools`
direct-Brave fallback unreachable · 9 dead config fields (`quality_block_threshold`,
`agent_description`, `subtask_bootstrap_timeout`, …).

## 5. Recommended fix order
1. **Security (RM-1)** — bind ports to 127.0.0.1, rotate DB password. Deploy/compose change.
2. **Pattern A cluster (HD-1, HD-2)** — swap RRF probe → raw cosine. Unblocks procedure learning, stops wrong supersession.
3. **BR-1 / HT-1 (active-filter correctness)** — restore episode + neighbor retrievability.
4. **HB-1** — one-line `has_updates=True`; restores drift alerting.
5. **CL-1 + CL-2** — route into `sections_by_tier`; stop silent subtask/hub-shift loss.
6. **DG-1** — correct skip-and-continue terminal status.
7. **ST-1** — guard empty-string JSON env default for clean installs.
8. Telemetry/loop re-wiring (Pattern C) — larger, schedule as follow-up features.
