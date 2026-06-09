# REST / MCP / Dashboard Code-Only Audit — 2026-06-09

Scope: `nous/api/rest.py` (2619 lines), `nous/api/mcp.py` (356), `nous/api/dashboard_queries.py` (2036), `nous/api/models.py` (97). Call paths into `runner.py`, `tools.py`, `heart/*`, `brain/*` traced but **not** reported (other agents own them).

Reachability tags use `nous/config.py` defaults + `.env.prod-snapshot` (133 lines, agent `nous-default`, single-agent prod).

---

## A. How it actually works

`create_app()` (rest.py:63) is a closure factory: every route handler is a nested `async def` over the injected component set (`runner`, `brain`, `heart`, `cognitive`, `database`, `settings`, plus optional `identity_manager`, `bus`, `sleep_handler`, `rubric_manager`, `rubric_evolver`, `heartbeat_runner`, `session_monitor`, `context_logger`). Optional components arrive as `_LazyProxy` (main.py:925) that raises `RuntimeError` until lifespan populates them; handlers probe with `try: _ = proxy.attr` and return 503 when unresolved.

Routes are a flat `Route(...)` list (rest.py:2487-2589). Ordering is deliberate: `/decisions/unreviewed` before `/decisions/{id}`, `/dashboard/admission/rejected` before `/dashboard/admission`, and the static `Mount("/dashboard", ...)` (html=True, no-cache wrapper) appended **last** as the catch-all. MCP is mounted separately at `/mcp` in `main.py:918` (lazy ASGI, stateless StreamableHTTP, single shared transport) only when `settings.mcp_enabled` (default True, prod=true).

Persistence model: most handlers open `async with database.session()` per request; dashboard handlers delegate to `dashboard_queries.py` functions that take `(session, agent_id)` and never manage their own session. SSE `/chat/stream` races `aiter.__anext__()` against `settings.sse_ping_interval` (default 15s, prod unset→15) via `asyncio.wait` (no cancel-on-timeout), emits `: keepalive` comments, and `finally`-closes the underlying generator on disconnect.

`docker-compose.yml:5-6` publishes the agent as `"${NOUS_PORT:-8000}:8000"` — host-side bind defaults to **all interfaces** (contrast `nous-eval-db` which is explicitly `127.0.0.1:5433`). `settings.host` default is `0.0.0.0`. There is **no authentication middleware anywhere** in `create_app`.

### Route inventory (REST)

| Method | Path | Handler | Notes |
|---|---|---|---|
| POST | /chat | chat | |
| POST | /chat/stream | chat_stream | SSE |
| DELETE | /chat/{session_id} | end_chat | |
| GET | /status | status | `?dashboard=true` extends |
| GET | /decisions | list_decisions | |
| GET | /decisions/unreviewed | list_unreviewed | no error guard |
| POST | /decisions/{id}/review | review_decision | no error guard |
| GET | /decisions/{id} | get_decision | |
| GET | /episodes | list_episodes | |
| GET | /facts | search_facts | search + browse |
| GET | /chunks | list_chunks | direct SQL |
| PUT | /censors/{id} | update_censor | |
| GET | /censors | list_censors | |
| GET | /procedures | list_procedures | |
| GET | /frames | list_frames | |
| GET | /calibration | calibration | |
| GET | /identity | get_identity | |
| PUT | /identity/{section} | update_identity_section | mutating, no auth |
| POST | /reinitiate | reinitiate | destructive, no auth |
| GET/DELETE | /subtasks, /subtasks/{id} | list/get/cancel_subtask | |
| GET/POST/DELETE | /schedules, /schedules/{id} | list/create/deactivate | |
| GET | /health | health | |
| GET | /events/stats, /events/recent | events_stats/recent | |
| GET | /events/trace/{trace_id} | events_trace | **no agent_id scope** |
| GET | /events/recent-traces | events_recent_traces | **no agent_id scope** |
| GET | /events/modifications | events_modifications | **no agent_id scope** |
| GET/POST/PUT | /heartbeat/* (status, trigger, config) | heartbeat_* | config mutates Settings |
| POST | /heartbeat/findings/{fp}/{ack,resolve,dismiss} | | |
| GET/PUT | /heartbeat/findings, /escalation-policy | | |
| GET/POST | /heartbeat/tuning-report, /tune | | |
| POST | /heartbeat/check/{name}/{trigger,reset} | | |
| GET/POST/PATCH/DELETE | /heartbeat/checks/dynamic[/{name}[/trigger]] | | |
| POST | /sleep/trigger | trigger_sleep | |
| GET/POST | /admin/search-weights | get/set_search_weights | mutating, no auth |
| POST/GET | /rubric/* (propose, approve, rollback, history, signals, evolve, root) | | mutating, no auth |
| GET | /dashboard/{graph,calibration,activity,health,rubric,admission[/rejected],ledger,heartbeat,observability,cache,dag,density,subtasks} | dashboard_* | |
| GET | /context/log[/{id}[/payload|/sections]], /context/diff | context_log_* | |
| GET | /behavior/{snapshot/latest,trends,anomalies,drift-report} | behavior_* | |
| (mount) | /dashboard/* | static SPA | html=True catch-all, no auth |

### MCP tool surface (`/mcp`)

5 tools: `nous_chat`, `nous_recall`, `nous_status`, `nous_teach`, `nous_decide`. `nous_chat`/`nous_decide` accept an optional/none session id; **`nous_recall` has no session, `nous_decide` hardcodes `"mcp-decision"`, `_handle_chat` defaults `"mcp-session"`** (mcp.py:224, 345). No auth on the MCP mount either.

---

## B. Findings register

### P1

**RM-1 — REST API + dashboard + MCP exposed with zero authentication on a publicly-bound port. (LIVE)**

> Main-session verification addendum (2026-06-09): CONFIRMED, and it compounds —
> `docker-compose.yml:116-117` also publishes the **main Postgres** on an
> all-interface bind (`"${DB_PORT:-5432}:5432"`), and the prod env snapshot
> shows prod still runs the default `DB_PASSWORD=nous_dev_password`. Anyone who
> can reach the host gets not just the unauthenticated REST/MCP surface but the
> database itself with a guessable password. Mitigation order: bind both ports
> to 127.0.0.1 (or an internal network), rotate the DB password, then add an
> auth layer to REST/MCP if remote access is required.

`docker-compose.yml:5-6` `"${NOUS_PORT:-8000}:8000"` binds the host port on all interfaces (no `127.0.0.1:` prefix, unlike the eval DB at line 131); `settings.host` defaults to `0.0.0.0` (config.py:299). `create_app` (rest.py:63-2618) registers **no auth middleware**. Anyone who can reach the port can: read all memory (`/facts`, `/decisions`, `/episodes`, `/chunks`, every `/dashboard/*`, the static SPA), wipe agent identity (`POST /reinitiate`, rest.py:742), rewrite identity sections (`PUT /identity/{section}`), mutate retrieval weights persisted to DB (`POST /admin/search-weights`, rest.py:1042), force sleep cycles, cancel subtasks/schedules, evolve/rollback the rubric, and drive arbitrary LLM turns with tool access via `POST /chat` and the unauthenticated MCP mount (`nous_chat`/`nous_decide` run the full tool loop → `bash`, `write_file`, `web_fetch`, etc.). Evidence: no `Middleware`/`AuthenticationMiddleware` in the `Starlette(**kwargs)` build (rest.py:2615-2618); telegram is the only "trusted" client and it talks plain HTTP to `http://nous:8000`. Fix: bind the published port to `127.0.0.1` in compose (matching the eval-DB pattern) and/or add a shared-secret/Bearer auth middleware gating all non-`/health` routes; at minimum gate the mutating/destructive set and `/mcp`.

### P2

**RM-2 — `/events/recent-traces`, `/events/modifications`, `/events/trace/{trace_id}` query `nous_system.events` with no `agent_id` filter. (LATENT)**
rest.py:2283-2303 (recent-traces) and 2317-2323 (modifications) scan the entire events table across all agents; `events_trace` (rest.py:2249-2254) filters only by `trace_id`. By contrast the in-handler `dashboard_observability` version of the same query *does* scope `AND agent_id = :aid` (rest.py:1425, 1459, 1479) — so the standalone endpoints are inconsistent with the dashboard ones. Latent today (single agent `nous-default`) but a direct cross-agent data leak the moment a second agent shares the DB, which the schema is explicitly designed for ("All tables are agent-scoped for multi-agent readiness"). Fix: add `AND agent_id = :aid` and bind `settings.agent_id`.

**RM-3 — MCP `nous_recall`/`nous_chat`/`nous_decide` share fixed session IDs across all external callers. (LIVE if MCP used)**
mcp.py:224 (`"mcp-session"`), 345 (`"mcp-decision"`). Two external agents (or two concurrent calls from one) calling `nous_chat`/`nous_decide` land in the **same** conversation/working-memory/ledger session, interleaving history and context. `nous_recall` is stateless so safe, but `nous_chat` defaulting to a single constant means multi-client MCP is effectively a shared mutable conversation. prod `NOUS_MCP_ENABLED=true`. Fix: generate a per-connection session id (or require the caller to pass one) instead of a module constant.

**RM-4 — Core list endpoints accept an unbounded, unvalidated-sign `limit`. (LIVE)**
`list_decisions` (rest.py:352), `list_episodes` (406), `search_facts` (438), `list_censors` (561), `list_procedures` (645), `list_subtasks` (804), `list_schedules` (888) parse `int(limit)` with **no upper cap** and **no `>= 0` check** — unlike `dashboard_graph` (min(...,2000)) and `dashboard_admission_rejected` (min(...,200)). `?limit=100000000` forces a giant result set + `model_dump` loop (memory/latency DoS); a **negative** limit reaches Postgres `LIMIT -5`, which errors → swallowed into a generic 500. Fix: clamp to a sane max and reject negatives, mirroring the dashboard handlers.

### P3

**RM-5 — `confidence_min`/`float` parsing sits outside the try/except in `list_decisions`. (LATENT)**
rest.py:361 `confidence_min = float(confidence_min_str)` is between the int-guard try (ends 355) and the brain-call try (starts 369). `?confidence_min=abc` raises an unguarded `ValueError` → generic Starlette 500 instead of a 400. Same shape exists implicitly anywhere a raw `float(...)`/`int(...)` runs outside a guard.

**RM-6 — `list_unreviewed` and `review_decision` have no input guards at all. (LATENT)**
`list_unreviewed` (rest.py:780-794): `int(max_age_days)` / `int(limit)` and the brain call are unwrapped → 500 on non-int params. `review_decision` (757-778): `await request.json()` unwrapped (500 on bad body), `UUID(decision_id)` unwrapped (500 on malformed id), and only `ValueError→404` is caught so any other brain exception is a raw 500. Both should mirror the `try/except ValueError → 400/404` pattern used elsewhere.

**RM-7 — Many observability/behavior endpoints parse `int(limit|hours)` ungaurded. (LATENT)**
`events_recent` (2231), `events_recent_traces` (2280), `events_modifications` (2314), `behavior_trends` (2424), `behavior_anomalies` (2449), `context_log_list` (2339), `get_rubric_history` (1698), `list_unreviewed` (783) all do bare `int(query_params.get(...))` → 500 on garbage input. Low severity (operator-facing) but inconsistent with the guarded list handlers.

**RM-8 — `heartbeat_config` mutates a live `Settings` via `object.__setattr__` with weak validation and no persistence. (LATENT)**
rest.py:1915 writes `heartbeat_quiet_start`/`quiet_end`/`tick_interval`/`daily_token_budget` onto the shared settings object. Validation is only `isinstance(val, int) and val >= 0` — `quiet_start=99` or `tick_interval=0` are accepted. Changes are lost on restart (not persisted), and mutating a process-global settings object that other components read concurrently is a latent consistency hazard. Fix: range-validate (0–23 for hours, ≥1 for intervals) and document the non-persistence.

**RM-9 — Error responses leak raw internal exception strings to clients. (LIVE)**
Pervasive `return JSONResponse({"error": str(e)}, status_code=500)` (e.g. rest.py:132, 231, 347, 384, 401, ...). `str(e)` on SQLAlchemy/asyncpg errors can include SQL fragments, table/column names, and connection detail. Combined with RM-1 (no auth) this is real reconnaissance surface. Fix: log full detail, return a generic message + request id to the client.

**RM-10 — `set_search_weights` swallows DB-persist failures but returns 200. (LIVE)**
rest.py:1068-1072 / 1088-1092: `persist_to_db` failures are caught and logged only; the response still reports success with the new in-memory weight. The runtime value *is* applied (so behavior changes), but the operator believes it was persisted when it was not — silent divergence after restart. Fix: surface a `persisted: false` flag or non-2xx when persistence fails.

**RM-11 — `chat_stream` returns a `JSONResponse` 400 from a handler typed/clients-expecting SSE. (LATENT)**
rest.py:137-143: on bad JSON / missing message the SSE endpoint returns a JSON body, not an `text/event-stream` error event. SSE clients parsing the stream may mishandle the 400. Cosmetic but a contract wrinkle.

### INFO

**RM-12** — `get_admission_data` interpolates `{dim}` into SQL via f-string (dashboard_queries.py:964-969). Safe today: `dim` iterates a hardcoded `["utility","confidence","novelty","recency","type_prior"]` list, never user input. Same for `base_where`/`filter_clause` ("1=1", `active = true`) in `get_density_data` (1689-1695) and the `{table}`/`{cols}` map in `get_graph_data` (181-206) — all hardcoded. Sort/order in `get_admission_rejected` go through an allowlist (1125-1131). No injection reachable, but the f-string-into-`text()` pattern is fragile if a future edit makes any token user-derived.

**RM-13** — `get_unreviewed` fetches all unreviewed rows then slices `[:limit]` in Python (rest.py:786-790) — works but does redundant DB work; push the limit into SQL.

**RM-14** — Dashboard orphan/degree/density queries (`get_graph_data` orphan `NOT EXISTS`, `get_health_data` degree-distribution UNION-ALL, `get_density_data` per-type orphan scans) are full sequential scans joined against `brain.graph_edges` on every dashboard load, repeated per node type (5×). On a large graph these are hot-path table scans; verify indexes on `graph_edges(agent_id, source_id)` / `(agent_id, target_id)` exist, else dashboard latency scales with corpus size.

**RM-15** — `get_activity_data` (dashboard_queries.py:474) and `events_*` return raw `data` JSONB blobs verbatim to the client; event payloads can embed conversation/tool content. Under RM-1 this widens the read surface. Consider field-whitelisting.

**RM-16** — `_handle_teach` (mcp.py:308-336) maps `domain`→`category` for facts with no category validation, and bypasses admission/dedup paths the REST `/facts` write would use; benign but an inconsistency between the two write surfaces.

---

## C. Dead-code / no-consumer inventory

Verified consumers: dashboard JS (`static/dashboard/js/*`) fetches `/status?dashboard=true`, `/decisions`, `/episodes`, `/facts`, `/chunks`, `/procedures`, `/censors`, `/context/log/*`, and every `/dashboard/*`. Telegram bot uses only `/chat`, `/chat/stream`, `/chat/{id}` (DELETE), `/identity`. No test references found for the events/behavior/admin families.

- **No frontend or test consumer found** for: `/events/trace/{trace_id}`, `/events/recent-traces`, `/events/modifications`, `/events/stats`, `/events/recent`, `/behavior/snapshot/latest`, `/behavior/trends`, `/behavior/anomalies`, `/behavior/drift-report`, `/context/diff`, `/admin/search-weights` (GET/POST), `/rubric/*` REST family, `/decisions/unreviewed`, `/decisions/{id}/review`. These are external-API/manual-curl surfaces — not strictly dead (intentional external endpoints) but unexercised by any in-repo client, so field-shape drift here is invisible until an external caller hits it. RM-2/RM-6/RM-7 cluster in exactly this unexercised set.
- `_get_last_bot_message_id` (telegram_bot.py:808) is a `return None` stub (out of scope, noted in passing).
- `events_trace` computes `"duration_ms": None` with a comment "Could compute" (rest.py:2275) — placeholder field always null.

No endpoints mutate state via GET (all writes are POST/PUT/DELETE) — clean.

## D. FE/BE field-shape spot check

Cross-checked dashboard JS field access against handler dicts: `overview.js` (`data.memory/calibration/execution_integrity/dashboard`), `graph.js` (`nodes/edges/stats`), `admission.js` (`summary/score_distribution/dimension_stats/by_source/by_category/daily_trend/bypass_breakdown/config/facts/total`), `health.js` (`daily_edges/degree_distribution/density_history/orphan_trend/total_edges`), `heartbeat.js` (`status/checks/budget/quiet_hours/totals/findings_by_day/findings_timeline/cognitive_sessions/finding_lifecycle/tuning`) — **all present** in the corresponding response builders. No drift detected on the exercised surface. (The unexercised endpoints in section C are where drift would hide.)

## E. Improvement opportunities

1. Add an auth middleware + bind `127.0.0.1` (RM-1) — single highest-value change.
2. Factor a `_parse_int(param, default, *, min, max)` helper and route all list/observability handlers through it (fixes RM-4, RM-5, RM-6, RM-7 uniformly).
3. Add `AND agent_id = :aid` to the three standalone events endpoints (RM-2) to match the dashboard variants.
4. Per-connection MCP session ids (RM-3).
5. Generic client error bodies + structured server logs (RM-9).
6. Confirm/add `graph_edges(agent_id, source_id)` and `(agent_id, target_id)` indexes for dashboard scans (RM-14).
