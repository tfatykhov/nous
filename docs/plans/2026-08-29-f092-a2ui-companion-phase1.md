# F092 — A2UI Companion App, Phase 0+1 spine (implementation plan)

**Source spec:** `F091 — A2UI Companion App for Nous`, rev 4 (accepted). Renamed **F092** in-repo:
F091 is already taken by Retrieval Telemetry. Spec decisions (Q1–Q6) are treated as fixed inputs.
**Branch:** `feat/f092-a2ui-companion` off `origin/main`.
**Protocol:** A2UI v1.0 (Candidate), vendored at upstream commit `d9086fb73fb5ab535780b6af47a7440096d5785f` (2026-08-28).

## 1. Scope of THIS PR

End-to-end vertical slice ("the spine"), per spec §14 Phase 0 + the core of Phase 1:

**In:**
- Vendored v1.0 schemas + basic catalog + upstream examples (done: `nous/a2ui/catalogs/`, `tests/fixtures/a2ui/examples/`).
- Migration: `nous_system.a2ui_surfaces`, `a2ui_outbox`, `a2ui_actions` (spec §6, adjusted: real schema name is `nous_system`, + `agent_id` columns per house rule).
- `nous/a2ui/` package: `validator.py`, `dsl.py`, `service.py` (SurfaceService: create/update/resolve/expire, dedup_key, outbox append, in-process broadcast hub), `transport.py` (SSE generator), `actions.py` (handler registry, allowlist, nonce, rate limit, censor gate, audit), `builders/` (action_review, approval, heartbeat_findings).
- REST routes (registered before Mounts): `GET /a2ui/stream`, `GET /a2ui/surfaces`, `GET /a2ui/surfaces/{id}`, `POST /a2ui/action`, `GET /a2ui/catalog/{catalog_id}`, `GET /companion` (redirect into the built entry).
- Agent tool `push_surface` (template enum + params + dedup_key + notify).
- Telegram deep-link one-liner for priority >= 1 (reuses existing send path; silently skipped when Telegram unconfigured).
- `nous-core` catalog v1 (minimal): `ApprovalPanel`, `ActionReviewCard`, `StatTile`, `KeyValueTable` — authored to §8.3 constraints, linted by a CI test.
- Svelte companion app: second HTML entry `companion.html` + `src/companion/` (transport, stores, pointer, Renderer, catalog adapters for the basic subset incl. full input set + the 4 nous-core components, functions, action dispatch).
- Tests: builder→schema validation, validator structural checks, action security (allowlist/nonce/rate/censor), conformance fixtures (envelope validation of vendored examples), Python route tests; vitest: pointer/scope resolution, renderer walker, formatString, two-way binding, checks-disable, action payload.

**Out (follow-up PRs, per spec's own phasing):** one-store conversation + Telegram echo (§5.4), `POST /a2ui/message`, `callAgentFunction`/`callRendererFunction` round-trips, `compose_surface` LLM path, MemoryGraph/DagGraph/DecisionSweep/DagMonitor surfaces, PWA/Web Push, oauth2-proxy concerns (deployment-level).

Feature flag: `NOUS_A2UI_ENABLED` (default **true** — additive surface, no behavior change to existing paths; the SSE endpoint and tool registration are gated on it).

## 2. Data model (migration `0NN_a2ui.sql`)

Spec §6 with two adjustments: schema = `nous_system` (spec wrote `system`), and `agent_id TEXT NOT NULL` on all three tables (house rule: every new table is agent-scoped).

- `a2ui_surfaces(surface_id PK, agent_id, origin, kind, catalog_id, status live|resolved|expired|deleted, priority 0..2, title, components JSONB, data_model JSONB, allowed_actions TEXT[], dedup_key, nonce, session_id, trace_id, created_at, updated_at, expires_at, resolved_at)`
  - `components`/`data_model` = authoritative current state (snapshot hydration).
  - Index `(status, priority DESC, created_at DESC)`; partial index on `(dedup_key) WHERE status='live'`; partial on `(expires_at) WHERE status='live'`.
- `a2ui_outbox(seq BIGSERIAL PK, agent_id, surface_id FK CASCADE, envelope JSONB, created_at)` — delta log; SSE `id:` = `seq`.
- `a2ui_actions(id UUID PK, agent_id, surface_id, action_name, source_component_id, context JSONB, data_model JSONB, status pending|dispatched|completed|rejected, rejection_reason, ledger_entry_id, created_at, completed_at)`.

ORM models appended to `nous/storage/models.py` following house conventions.

## 3. Backend design

### 3.1 validator.py
- `jsonschema` (already a core dep) with a `referencing.Registry` that maps the spec's
  `catalog.json` placeholder ref to the **active catalog** (basic or nous-core), and
  `common_types.json` etc. to the vendored files. Envelope validation = `agent_to_renderer.json`.
- Structural checks past schema: exactly one `root` per surface after applying updates; no dangling
  child refs (warning-level: protocol allows placeholders, our *builders* must not emit them);
  no cycles; every `action.event.name` ∈ surface `allowed_actions`.
- Error shape mirrors the spec's standard: `{code: VALIDATION_FAILED, surfaceId, path, message}`.

### 3.2 dsl.py + builders/
- Thin typed helpers (`Surface`, `Column`, `Row`, `Text`, `List`, `Button`, `TextField`, …,
  `ApprovalPanel`, `ActionReviewCard`) that accumulate flat component dicts; `Surface.envelopes()`
  emits a single v1.0 `createSurface` with inline `components` + `dataModel`.
- Every builder's output goes through `validator.validate_surface()` in unit tests → invalid A2UI
  fails CI, not production (spec §9.1).
- Builders in scope: `approval_gate` (spec §9.1 verbatim shape), `action_review` (Appendix A2 shape,
  incl. `compensation.revertible` driving Revert button presence), `heartbeat_findings`
  (List of finding rows + ack/resolve/dismiss buttons).

### 3.3 service.py — SurfaceService
- `push(surface: BuiltSurface, *, dedup_key, notify) -> surface_id`:
  - dedup: `live` surface with same `dedup_key` → update-in-place (updateComponents +
    updateDataModel envelopes) instead of create (spec §7 rule 2).
  - persists authoritative state + appends outbox rows in one transaction, then broadcasts
    envelopes to the in-process hub.
  - priority >= 1 → Telegram one-liner with deep link (best-effort, never blocks).
- `update_data(surface_id, path, value)`, `resolve(surface_id)`, `expire_sweep()`:
  resolve/expire emit the teardown (`deleteSurface`) envelope; sweep is invoked opportunistically
  (on stream connect + on push) — no new background loop in this PR.
- Broadcast hub: `asyncio.Queue` per connected stream + outbox catch-up poll (2s) inside the SSE
  generator so cross-process writers (diag scripts) still surface without LISTEN/NOTIFY machinery.

### 3.4 transport.py — SSE
- `GET /a2ui/stream?since=SEQ` (also honors `Last-Event-ID` header). Replays outbox rows
  `seq > since` for surfaces still `live`, then live-tails hub + catch-up poll.
- **Reconnect protocol is hydration-first (zombie-surface fix):** a live-filtered replay would
  drop the `deleteSurface` teardown for a surface that resolved while the client was offline,
  leaving it rendered forever. So the CLIENT, on every connect AND reconnect: (1) fetch
  `GET /a2ui/surfaces?status=live` and drop any local surface not in the index, (2) hydrate
  missing ones via snapshot, (3) open the stream from the latest seq the index reports.
  Cold start and resume are the same code path; replay exists only to close the tiny gap
  between index fetch and stream open. Test: disconnect → resolve → reconnect.
- Gap > `a2ui_outbox_replay_window` (default 500) or compacted surfaces → emit
  `event: control  data: {"type":"resync"}` → client redoes the hydration-first sequence.
- Keepalive: reuse the `chat_stream` `asyncio.wait` race pattern verbatim (spec finding 3.3).
- Event framing: `id: <seq>\nevent: a2ui\ndata: <envelope JSON>`.

### 3.5 actions.py — ActionRouter
- `@action("name", mutating=..., irreversible=..., compensation=...)` registry.
- `POST /a2ui/action` pipeline: surface exists & `live` → name ∈ `allowed_actions` → nonce match
  (`context.surfaceNonce` vs stored) → rate limit (30/min in-process sliding window) → censor gate
  for mutating handlers (existing censor check API) → dispatch → write `a2ui_actions` + F032 ledger
  entry → handler may update/resolve the surface. Rejections return the spec error shape and are
  audited with `status='rejected'`.
- Handlers in scope: `approval.choose`, `approval.defer`, `review.acknowledge`,
  `review.course_correct` (writes resolve_decision outcome against trace_id), `review.make_rule`
  (v1: records a decision/fact stub), `heartbeat.acknowledge|resolve|dismiss` (delegate to existing
  finding lifecycle functions).

### 3.6 Tool + wiring
- `push_surface` tool (template enum: approval_gate | action_review | heartbeat_findings; params;
  dedup_key; notify) registered per house pattern; frame access like other action tools.
- `main.py`: construct SurfaceService + ActionRouter alongside existing components; hand to
  `create_rest_app`.
- Config: `NOUS_A2UI_ENABLED`, `NOUS_A2UI_OUTBOX_REPLAY_WINDOW=500`,
  `NOUS_A2UI_ACTION_RATE_PER_MINUTE=30`, `NOUS_A2UI_SURFACE_TTL_HOURS=720` (30d), documented in
  CLAUDE.md env table.

## 4. Frontend design (`dashboard-app/src/companion/`)

Entry: **option (b)** — `companion.html` second Rollup input in the existing Vite build
(`base: /dashboard/v2/` unchanged), chrome-free page, own `companion.ts` mount. Backend adds
`Route("/companion", redirect → /dashboard/v2/companion.html)`; fragment deep links
(`#/s/{surface_id}`) survive the redirect client-side.

Modules (spec §12.1, trimmed to this PR's scope):
- `transport.ts` — `EventSource` client, `?since=` resume from last seen seq, `control:resync`
  → full re-hydrate from `GET /a2ui/surfaces` + `GET /a2ui/surfaces/{id}`; POST helper for actions.
- `store.svelte.ts` — surfaces map as Svelte 5 runes state: per-surface `{components: Map<id,comp>,
  dataModel, status, meta}`; applies the 4 envelope types with updateDataModel upsert/delete
  semantics (null deletes; no path = replace).
- `pointer.ts` — RFC 6901 resolution + scope stack (absolute `/…`, relative inside list templates,
  `@index` valid only in collection scope).
- `Renderer.svelte` — recursive adjacency-list walker: buffer-until-root, placeholder for dangling
  refs, ChildList array vs template expansion, weight → flex-grow.
- `catalog/` — registry `component name → Svelte component`; basic subset: Text, Image, Icon, Row,
  Column, List, Card, Tabs, Divider, Modal, Button, CheckBox, TextField, DateTimeInput,
  ChoicePicker, Slider (Video/AudioPlayer out of scope, spec §8.1); nous-core: ApprovalPanel,
  ActionReviewCard, StatTile, KeyValueTable.
- `functions.ts` — required, regex, length, numeric, email, formatString (`${...}` incl. nested +
  relative paths), formatNumber, formatCurrency, formatDate, pluralize, and/or/not, openUrl,
  `@index(offset)`. checks → ValidationResult; failing checks disable Buttons and show messages
  on inputs.
- Two-way binding local-only; action dispatch resolves context bindings, stamps ISO timestamp,
  POSTs `{version, action:{name, surfaceId, sourceComponentId, timestamp, context}}`.
- Text markdown: minimal safe subset (headings, bold, italic, inline code, links w/ noopener),
  rendered via Svelte elements — **no `{@html}` on agent-authored content**.
- Styling: scoped `<style>` blocks against Nous CSS vars (`--surface`, `--border`, `--accent`, …),
  dark theme, matching house idiom; shadcn primitives NOT used.
- App shell `Companion.svelte`: surface feed sorted by (priority DESC, created_at DESC), focused
  view for `#/s/{id}`, connection status pill.

Tests (vitest, colocated, explicit imports, jsdom): pointer/scope, store envelope application,
formatString, walker (buffer-until-root, template expansion, placeholders), checks-disable,
action payload assembly, conformance smoke over a handful of vendored example fixtures.

## 3.9 Backend review revisions (rev-arch, APPROVE WITH REVISIONS — all folded in)

**P1 (probe-proven; probes to be folded into tests as regressions):**
- **`\p{…}` crash:** `common_types.json` `Extensions.patternProperties` uses `\p{XID_Start}` —
  Python `re` raises `PatternError` on it, and every Nous surface carries `metadata.extensions`.
  `validator.py` rewrites that one pattern at schema LOAD time to `^[^\W\d][\w]*$` (vendored file
  stays byte-identical). Test: accepts `com_nous_nonce`/`a2ui_x`, rejects `com-nous-nonce`/`1abc`.
- **Mixed catalogs:** the envelope's `catalog.json#/$defs/anyComponent` `$ref` is static — a
  basic+nous-core surface (the flagship Action Review shape) fails against either catalog alone.
  `validator.py` builds a MERGED catalog (union anyComponent/anyFunction oneOf lists + components/
  functions maps, assert no key collisions) registered at the `catalog.json` placeholder URI.
  Registry mappings needed: `…/v1_0/common_types.json` → vendored, `…/v1_0/catalog.json` → merged.
- **Outbox visibility race:** BIGSERIAL seq is allocated at INSERT, visible at COMMIT — a
  watermark poller can skip a not-yet-committed lower seq forever. Fixes: (a) SSE generator
  subscribes to the hub BEFORE the replay query, drops hub items `seq <= max_replayed`;
  (b) client dedupes by seq (replay/live legitimately overlap); (c) poll/replay watermark only
  advances over rows `created_at <= now() - interval '2 seconds'`.

**P2 (all adopted):**
- Ratchet asserts `==` not `<=`: `push_surface` handler LIVES IN `nous/a2ui/tools.py` and is
  registered from there — the only `nous/api/tools.py` edit is the `_session_id` dispatch branch
  (adds no returns). Baseline stays 43.
- `stable_tool_set_enabled` (default true) exposes any registered tool in all non-initiation
  frames — blanket exposure is INTENDED for push_surface; FRAME_TOOLS entries still added for the
  flag-off path.
- `create_app` new kwargs default `None`; when None the /a2ui routes 503 (matching the heartbeat
  routes' unavailable pattern — registration always happens, behavior is defined). Components dict
  gains `surface_service`/`action_router` keys before `_lazy_component` references.
- Hub queues: bounded (maxsize 256), `put_nowait`; `QueueFull` → drop subscriber, its stream sends
  `control: resync`; unregister in generator `finally` (F087 precedent: drop-on-full by design).
- Multiplexed keepalive: hold hub-get + poll-timer as long-lived pending tasks across iterations
  (`asyncio.wait` on the set); cancel-and-await both in `finally` (rest.py:243-253 template).
- Censor gate: match target = surface title + action name + serialized context/data model, AND a
  push-time check on builder text (where the risky prose lives). Side effects on real attempts OK.
- Catalog route: short names — `/a2ui/catalog/{name}`, name ∈ {basic, nous-core}; the renderer
  maps catalogId → local fetch path; served `catalogId`/`$id` must equal what surfaces declare.
- Nonce: `secrets.token_urlsafe(16)`; never logged; omitted from the LIST endpoint only (detail +
  stream must carry it); `POST /a2ui/action` requires `Content-Type: application/json` (the real
  CSRF control — no CORS middleware exists and Request.json() never checks it). Channel decision:
  `context.surfaceNonce` (Appendix A form), documented deviation from §10.6 prose.
- Migration 071: FULL-LINE `--` comments only (trailing-comment `;` bug passes CI's psql but
  breaks the startup splitter); every index named + `IF NOT EXISTS`; `CHECK (priority BETWEEN 0
  AND 2)`; verify locally via the Python migrator against a fresh DB, not just psql.
- Vite: `build.rollupOptions.input` lists BOTH `index.html` and `companion.html` (setting input
  replaces the default).
- Rate limit key: `agent_id` (single-user deployment; effectively global) — stated explicitly.
- `expire_sweep` writes `no_objection` evidence (a `brain.decisions` review row via existing API)
  BEFORE flipping to `expired`, per spec §6.2; outbox rows for non-live surfaces deleted after
  24h inside the same sweep.

**P3:** no context-manager protocol on SurfaceService (LazyProxy forwards `__getattr__` only);
deep-link fragment-across-redirect is accepted (works in current browsers; single-user app).

## 4.1 Frontend review revisions (rev-ui, APPROVE WITH REVISIONS — all folded in)

Binding decisions from the review, treated as part of the plan:

- **Store shapes:** per-surface components as plain `Record<string, Component>` (NOT `Map` —
  `$state` doesn't proxy Maps). Store is a class instance exported once from `store.svelte.ts`
  (`export const surfaces = new SurfaceStore()`), `$state` fields mutated via `this.*`; no
  `$effect` outside components.
- **Walker:** recursion via self-import (`import Self from './Renderer.svelte'`; `svelte:self` is
  deprecated). Thread `depth` + ancestor-ID `Set`; cycle or depth>64 renders the dangling-ref
  placeholder. Server adds post-merge cycle check too, but the renderer is the last line.
- **Two-way binding:** Svelte 5.9+ function bindings
  `bind:value={() => getPointer(...), (v) => setPointer(...)}` — no local mirror + `$effect` sync.
  TextField `number` variant round-trips as string (value is DynamicString).
- **Checks engine:** condition may evaluate to boolean OR ValidationResult; `toBool()` inside
  and/or/not (`args.values` array); result reader accepts both, falls back to `CheckRule.message`.
  ValidationResult local shape: `{valid, message?}` (no schema exists upstream — deliberate local
  choice). Check messages render inline on the input (catalog instructions forbid separate error
  Text components).
- **formatString:** recursive-descent scanner (quote-aware, depth-aware — fixture 32 has a comma
  inside a quoted arg and nested `${}`), `\${` escape, absolute + relative paths.
  **formatDate:** CLDR token mapper (~30 LOC: yyyy MM MMMM d dd EEE EEEE HH mm ss). Both tested
  against fixture 32. `@index` offset is DynamicNumber; errors outside collection scope.
- **Transport seam:** `EventSource` never constructed at module scope; `createTransport({streamFactory,
  fetchImpl})` injection so vitest (jsdom has no EventSource) drives a fake synchronously.
  **Server precedence: `Last-Event-ID` header WINS over `?since=`** (browser auto-reconnect reuses
  the original URL) — Python test for this.
- **Catalog registry aliasing:** basic adapters registered under BOTH the upstream
  `a2ui.org/.../basic/catalog.json` id and the Nous-served basic id — fixtures use the former.
- **Markdown-lite (~150 LOC):** block: heading|paragraph|list|code; inline: text|strong|em|code|link;
  tiny AST rendered recursively, no `{@html}`; href scheme allowlist (http/https/mailto,
  else plain text) + `rel="noopener noreferrer"`; same allowlist gates `openUrl` (+ user-gesture
  only). Note in code: catalog prose says no links, official fixture 35 uses one — fixture wins.
- **Action POST errors:** non-2xx paints inline on the originating component; surface stays
  interactive; never silent.
- **Routes:** register `/companion` AND `/companion/` (mirror the /dashboard redirect pair);
  deep link form standardized as `/companion#/s/{id}`. Companion hash router is ~25 own lines
  (`{view:'feed'} | {view:'surface', id}`) with the `initialized` guard copied from lib/router;
  NOT a reuse of the dashboard ROUTES union. `decodeURIComponent` the id.
- **Adapters (no bits-ui anywhere):** Modal → native `<dialog>`/`showModal()`; Tabs → hand-rolled
  tablist ARIA; ChoicePicker → fieldset + native radio/checkbox (chips = CSS; filterable = one
  input; value ALWAYS a string array, even mutuallyExclusive); Slider → native range with
  `step=(max-min)/steps`; DateTimeInput → native date/time/datetime-local by enableDate/enableTime
  (document UTC-vs-local choice); TextField → 4 variants only (no `date` — spec drift, catalog
  is ground truth).
- **Data model semantics:** `path` omitted OR `"/"` = whole-model replace; null deletes; pointer
  writes create intermediates (numeric token → array, else object — matched on the Python side);
  unescape order `~1` then `~0` (test).
- **`weight` → flex-grow only under Row/Column.** Modal open state renderer-local.
- **CSS:** extract the `:root` Nous token block to a shared `tokens.css` imported by both entries
  (companion does NOT import app.css or the vendor script tags — nothing in scope needs
  chart/d3/cytoscape).
- **Pre-existing red test:** `src/lib/router.test.ts` asserts 17 routes, router has 18 (F091 added
  `retrieval` without updating). One-line fix included in this PR so gate 2 is attributable.

## 4.5 Build order — walking skeleton first

Phase-0's kill criterion (walker + binding working fast, else fall back to web_core v0.9.1) only
functions if built in this order:

1. **Skeleton:** migration 071 + minimal SurfaceService (push + snapshot + outbox) + SSE endpoint +
   minimal renderer (Text/Column/Button, pointer binding) → render ONE hardcoded surface in Chrome,
   action click round-trips. Boot `nous.main` locally as part of this step, not at the end.
2. **Fan-out (subagents):** catalog adapters, remaining builders, ActionRouter hardening,
   functions.ts, tests — only after the skeleton is proven.

Notes locked by advisor review: `push_surface`'s success return bumps `_UNFLAGGED_BASELINE`
(44) with a comment — do not contort the handler; the demo script loads real `Settings` so its
`agent_id` matches the server's (env mismatch = silent empty browser); PR body must call out the
`NOUS_A2UI_ENABLED=true` default rationale and the deferred Conversation surface deviation.

## 5. Verification gates

1. `uv run pytest tests/test_a2ui_*.py` green locally (real Postgres via docker compose).
2. `cd dashboard-app && npm run check && npm run test` green (CI does not run these — local gate).
3. Local browser E2E: `docker compose up -d postgres` → run Nous → `scripts/diag/a2ui_push_demo.py`
   pushes all three builder surfaces → verify in Chrome: render, live update, action round-trip
   (click approve → server handler → surface resolves → teardown animation), SSE resume after
   reload, deep link.
4. Full pytest suite locally for regressions; push; CI green; codex clean.

## 6. Backend recon findings folded in (binding facts)

- **Migration number: `071_a2ui.sql`.** Schema is `nous_system` (never `system`; DB connect
  hard-fails otherwise). Migrator constraints: no `$$` bodies, no `/* */`, no `;` inside `--`
  comments; idempotent `CREATE TABLE IF NOT EXISTS`. CI applies migrations via raw psql, startup
  uses the stricter Python splitter — test both paths locally.
- **Service wiring:** `create_app` handlers are closures over kwargs; add `surface_service=` +
  `action_router=` kwargs; `main.py` builds them in the lifespan component builder and passes
  `_lazy_component(components, "surface_service")` (proxies resolve at first request). Follow the
  DAG block (`main.py:944-968`) as the wiring template.
- **ORM:** hand-written models in `storage/models.py` matching the migration (`Mapped`/
  `mapped_column`, `__table_args__` ending `{"schema": "nous_system"}`, `agent_id` on every table).
- **Tool registration:** inline-style `register_a2ui_tools(dispatcher, surface_service)` per the
  `register_heartbeat_tools` pattern; `_tool_error()` on every failure path.
  ⚠ AST ratchet in `tests/test_tool_arg_salvage.py` (`_UNFLAGGED_BASELINE = 43`): any new
  unflagged return in `tools.py` fails CI — classify or bump the baseline deliberately with a
  comment. `push_surface` needs an explicit `session_id` injection branch in `dispatch()`
  (hardcoded per-tool allowlist at `tools.py:438-479`, `_session_id` convention) and entries in
  `FRAME_TOOLS` (runner.py:108-116).
- **Censors:** `heart.check_censors(text)` — has side effects by design (activation counts).
  Gate mutating A2UI actions the way `spawn_task` gates (tools.py:2921-2960): abort/refuse →
  reject the action; steer → proceed. A real activation on a real action attempt is a legitimate
  trigger, so side effects are correct here.
- **Ledger (spec §10.5 deviation):** F032's ExecutionLedger is in-memory + session-scoped —
  companion actions have no session and would evaporate on restart. `nous_system.a2ui_actions`
  IS the durable audit record; `ledger_entry_id` stays nullable/reserved. Documented deviation.
- **Telegram:** no shared helper exists (3 inline copies in the repo); A2UI gets its own small
  `_send_telegram` following `heartbeat/runner.py:775-795` (plaintext, best-effort, no parse_mode),
  with the deep link `{base}/companion#/s/{surface_id}`.
- **Auth (spec §10.7 note):** the entire REST surface is unauthenticated by existing design;
  deployment fronts it with oauth2-proxy. A2UI endpoints follow the same posture — no in-app auth
  in this PR. Nonce + allowlist + rate limit are still enforced.
- **SSE resume is net-new:** no `id:`/`Last-Event-ID` anywhere today; we add the first.
  Keepalive: copy the `asyncio.wait` race verbatim from `rest.py:198-228`; response headers
  `Cache-Control: no-cache` + `X-Accel-Buffering: no`.
- **Route ordering:** a2ui JSON routes go in the `routes` list with the other Routes (before the
  static Mounts); `/a2ui/surfaces/{surface_id}` before `/a2ui/surfaces`? Not needed — different
  path shapes, but keep detail-before-list convention anyway. `/companion` redirect Route is
  registered next to the `/dashboard` redirects, gated on the dist dir existing.
- **Fire-and-forget writes:** if any write must not block (Telegram notify, outbox broadcast),
  use the `_schedule_bg` + strong-ref `_pending_tasks` set pattern (retrieval_logger.py:119-147).
- **Heartbeat findings:** store methods are sync (`acknowledge/resolve/dismiss(fp) -> bool`,
  `record_outcome(fp, OutcomeSignal.POSITIVE)` on resolve) — A2UI heartbeat handlers delegate to
  these, matching rest.py:2402-2437 semantics.
- **CI reality:** pytest is the only real gate (ruff is advisory; dashboard never builds in CI).
  Local gates before push: full pytest + `npm run check` + `npm run test` + Docker-stage-equivalent
  `npm run build`.
