# REST Endpoints

Documented routes served by `nous/api/rest.py`, which is the full list. Part of the [Nous development guide](../../CLAUDE.md).

| Method | Path | Description |
|--------|------|-------------|
| POST | `/chat` | Send message, get response |
| POST | `/chat/stream` | SSE streaming chat |
| DELETE | `/chat/{session_id}` | End conversation |
| GET | `/status` | Agent status + memory stats + calibration |
| GET | `/decisions` | List recent decisions |
| GET | `/decisions/unreviewed` | Unreviewed decisions |
| POST | `/decisions/{id}/review` | Review a decision |
| GET | `/decisions/{id}` | Decision detail |
| GET | `/episodes` | List recent episodes |
| GET | `/facts?q=query` | Search facts |
| PUT | `/facts/{fact_id}` | Edit a Tier-1 fact via supersession (new versioned fact, re-embedded; reports merged_into_existing on dedup-swallow) |
| DELETE | `/facts/{fact_id}` | Deactivate (soft-delete) a Tier-1 fact |
| GET | `/profile/facts` | Tier-1 user-profile facts (preference/person/rule), prompt-order; `?core=true` filters to the curated core set |
| POST | `/facts/{fact_id}/core` | Toggle the `profile_core` curation tag on a Tier-1 fact (`{"core": true|false}`) |
| GET | `/censors` | Active censors |
| PUT | `/censors/{id}` | Update censor fields (trigger_action, action_instruction, unblock_pattern) |
| GET | `/procedures` | List procedures |
| GET | `/frames` | Available cognitive frames |
| GET | `/calibration` | Calibration report |
| GET | `/identity` | Get agent identity |
| PUT | `/identity/{section}` | Update identity section |
| POST | `/reinitiate` | Re-run initiation protocol |
| GET | `/health` | Health check |
| POST | `/sleep/trigger` | Trigger sleep cycle |
| GET | `/subtasks` | List subtasks |
| GET | `/subtasks/{id}` | Subtask detail |
| DELETE | `/subtasks/{id}` | Cancel a subtask |
| GET | `/schedules` | List schedules |
| POST | `/schedules` | Create a schedule |
| DELETE | `/schedules/{id}` | Deactivate a schedule |
| GET | `/admin/search-weights` | Get search weights |
| POST | `/admin/search-weights` | Set search weights |
| GET | `/rubric` | Current rubric |
| GET | `/rubric/history` | Rubric version history |
| GET | `/rubric/signals` | Outcome signals |
| GET | `/rubric/proposals` | List dimension proposals |
| POST | `/rubric/propose-dimension` | Propose a new dimension |
| POST | `/rubric/proposals/{id}/approve` | Approve a proposal |
| POST | `/rubric/rollback` | Rollback rubric version |
| POST | `/rubric/evolve` | Trigger rubric evolution |
| GET | `/dashboard/graph` | Graph visualization data |
| GET | `/dashboard/calibration` | Calibration dashboard data |
| GET | `/dashboard/activity` | Activity dashboard data |
| GET | `/dashboard/health` | Health dashboard data |
| GET | `/dashboard/rubric` | Rubric dashboard data |
| GET | `/dashboard/admission` | Admission control dashboard |
| GET | `/dashboard/admission/rejected` | Rejected admission entries |
| GET | `/dashboard/ledger` | Execution ledger dashboard data |
| GET | `/dashboard/heartbeat` | Heartbeat dashboard data |
| GET | `/dashboard/subtasks` | Subtask dashboard data (`hours`, default 24, max 168). With `NOUS_RESULT_INBOX_ENABLED`, also `result_inbox: {7d, 30d}`: per source kind (`subtask`, `dag`) and `intention_report` (F099: owner-facing rows) the rows `created` and `delivered`, `delivery_rate`, `latency_p50_s`, `latency_p95_s` (F098) |
| GET | `/dashboard/density` | Graph density dashboard data (F040) |
| GET | `/dashboard/retrieval` | F091 recent retrievals + window-level disposition/leg rollup |
| GET | `/dashboard/retrieval/{entry_id}` | F091 one retrieval's candidates (grouped by disposition) + graph-expansion edges |
| GET | `/dashboard/consolidation` | F035.6 recent consolidation cycles (sleep audit diff) |
| GET | `/dashboard/consolidation/{cycle_id}` | F035.6 one cycle's per-action diffs |
| GET | `/dashboard/execution` | Harness dashboard: the durable execution ledger (`window`, `context`, `status`, `effect`, `q`, `limit`, `before` keyset); `attention` = keyed sends with an unknown outcome |
| GET | `/dashboard/harness` | Harness dashboard: per-rule warn/enforce evidence (offered-tool rule, context policy, claim checks), grouped by each event's mode, never summed across rules |
| GET | `/dashboard/attention` | Harness dashboard: questions waiting + sends in doubt (same predicates as the DAG and Ledger tabs) for the nav badges and the Overview strip |
| GET | `/heartbeat/status` | Heartbeat status, checks, budget, and DAG tick liveness: `last_dag_tick` (last tick that succeeded) and `dag_tick_pending_since` (start of the tick in flight, null when none) |
| POST | `/heartbeat/trigger` | Force immediate heartbeat tick |
| PUT | `/heartbeat/config` | Update heartbeat intervals/budget at runtime |
| POST | `/heartbeat/check/{name}/trigger` | Force a specific check to run |
| POST | `/heartbeat/check/{name}/reset` | Reset circuit breaker for a failed check |
| GET | `/heartbeat/findings` | All tracked findings with state/age |
| POST | `/heartbeat/findings/{fingerprint}/acknowledge` | Acknowledge a finding |
| POST | `/heartbeat/findings/{fingerprint}/resolve` | Resolve a finding |
| POST | `/heartbeat/findings/{fingerprint}/dismiss` | Dismiss a finding (strong negative) |
| PUT | `/heartbeat/escalation-policy` | Update escalation thresholds |
| GET | `/heartbeat/tuning-report` | Latest tuning report |
| POST | `/heartbeat/tune` | Force a tuning pass |
| GET | `/heartbeat/checks/dynamic` | List all dynamic checks |
| POST | `/heartbeat/checks/dynamic` | Create a new dynamic check |
| PATCH | `/heartbeat/checks/dynamic/{name}` | Update a dynamic check |
| DELETE | `/heartbeat/checks/dynamic/{name}` | Delete a dynamic check |
| POST | `/heartbeat/checks/dynamic/{name}/trigger` | Force-run a dynamic check |
| GET | `/a2ui/stream` | F092: SSE surface envelopes (id = outbox seq; `Last-Event-ID` wins over `?since=`) |
| GET | `/a2ui/surfaces` | F092: live-surface index for hydration (never includes nonces) |
| GET | `/a2ui/surfaces/{surface_id}` | F092: full snapshot as a single `createSurface` envelope |
| POST | `/a2ui/action` | F092: renderer→agent user action (allowlist/nonce/rate/censor gated, audited) |
| GET | `/a2ui/catalog/{name}` | F092: serve a vendored catalog by short name (basic, nous-core) |
| GET | `/companion` | F092: redirect to the built companion entry (fragment deep links survive) |
| GET | `/companion/a/{surface_id}` | F092.1 Phase 4: shareable path-form per-app deep link — redirects into the hash router (`#/a/<id>`, alias of `#/s/`) |
| GET | `/a2ui/push/config` | F097: the four PUBLIC Firebase client values, or `{enabled:false, reason}`. Always 200 — "not configured" is an answer the app renders, not an outage |
| PUT | `/a2ui/push/tokens` | F097: upsert this install's FCM token (`installation_id`, `fcm_token`, `name`, `app_version`, `notifications_enabled`) |
| DELETE | `/a2ui/push/tokens/{installation_id}` | F097: the only way to deregister |
| GET | `/a2ui/push/installations` | F097: operator list — name, version, notification flag, last error. Never the token |
| POST | `/a2ui/push/test` | F097: send one test push and REPORT the failure (the ordinary send path swallows it) |
