> In-repo number: **F092** (the spec below self-numbers F091, which is already taken by Retrieval Telemetry).
> Phase-0+1 spine shipped on branch feat/f092-a2ui-companion; deferred per plan: one-store conversation
> unification (spec 5.4), POST /a2ui/message, compose_surface LLM path, MemoryGraph/DagGraph/DecisionSweep
> surfaces, renderer/agent function RPC round-trips, PWA. Implementation plan:
> docs/plans/2026-08-29-f092-a2ui-companion-phase1.md

# F091 — A2UI Companion App for Nous

**Status:** rev 4 — **accepted, ready to build**. All six questions resolved; no blocking input outstanding.
**Author:** Nous
**Date:** 2026-08-29
**Rev 4 changes:** Q1/Q2/Q3/Q6 resolved to recommendation on Tim's instruction (§16 rewritten as a decision log); §6.2 retention split into permanent evidence vs. disposable surface; §12.1 commits the v1.0 LOC number and adds the Phase-0 escape hatch; §16.1 adds revisit triggers. Brain decision `1b6ef89a`.
**Target protocol:** A2UI v1.0 (Candidate), pinned + vendored — **committed (Q3)**
**Related:** F021 Memory Dashboard, F032 Execution Ledger, F034 Heartbeat, F038 DAG Orchestration

---

## 1. Summary

A second interaction surface for Nous where the agent renders **structured, interactive UI** instead of prose. Telegram stays the conversational channel. The companion app is where Nous puts things that are *bad as text and good as an interface*: approval gates, batch triage, live graphs, forms, diffs.

The UI is described declaratively by the agent using the **A2UI protocol** — a JSON wire format for agent-generated interfaces — and rendered by a Svelte client using Nous's existing component library. No code crosses the boundary.

### What this is

- A **push surface**. Nous initiates. Heartbeat finds something at 03:00 → a triage card is waiting when you open the app.
- The **action-review channel** for the autonomy directive. Nous acts, then renders *what it did* as a structured card — reasoning attached, risk annotated, course-correction one tap away — instead of a paragraph in a chat window (§10.4).
- A **workspace for batch operations** that are miserable in chat — decision sweeps, finding triage, fact hygiene.
- A **second window onto the same conversation**. It has a chat input and the full input affordance set — text, choices, checkboxes, sliders, dates, lists — but it writes into the *same* session Telegram writes into (§5.4).

### What this is explicitly NOT

- Not a Telegram replacement — but it *does* have chat input (Q4, decided). That distinction is load-bearing, and it is defended architecturally rather than by omission: there is exactly **one conversation in one store**, with two windows onto it. Telegram stays the narrator and the notification channel; the companion is where interaction becomes structured. See §5.4.
- Not a replacement for the F021 dashboard. The dashboard is a *fixed, human-designed* read-mostly analytics UI. The companion is *agent-composed, ephemeral, action-oriented*. Different lifecycles, different owners.
- Not a general app platform. The catalog is a closed set.

### Why it's worth building

The strongest argument isn't "richer UI." It's this: Tim's standing autonomy directive says Nous acts by default and escalates only for irreversible actions, spend, values calls, and repeated-failure patterns — and that **"escalation must always come with a recommendation and options, never a bare question."** Today that gets flattened into Telegram prose and a yes/no reply. A2UI makes the recommendation-plus-options structure *literal and enforceable*: options as buttons, the recommendation pre-selected, the reasoning inline, the decision recorded with its `trace_id` on click.

The companion app is the missing half of the autonomy loop.

---

## 2. A2UI in one page

Only the parts that matter for this build.

**Wire model.** The agent streams JSON envelopes. Six agent→renderer message types:

| Message | Purpose |
|---|---|
| `createSurface` | Create a surface. v1.0 lets you embed `components` + `dataModel` inline — full UI in one payload. |
| `updateComponents` | Add/replace components in an existing surface. |
| `updateDataModel` | Upsert at a JSON Pointer path. `value: null` deletes. Omit `path` to replace wholesale. |
| `deleteSurface` | Tear down. |
| `callRendererFunction` | Agent→renderer RPC (needs `functionCallId`; renderer MUST always respond). |
| `agentFunctionResponse` | Reply to a renderer-initiated call. |

Four renderer→agent message types: `action`, `callAgentFunction`, `rendererFunctionResponse`, `error`.

**Structure = adjacency list.** Components are a *flat list*; the tree is built from ID references. Exactly one component must have `"id": "root"`. This is what makes it stream-friendly — components arrive in any order, the renderer buffers and fills in the tree progressively. Missing refs render as placeholders, not errors.

**Data = separate.** Components bind to a data model via JSON Pointer (`{"path": "/user/name"}`). Absolute paths start with `/`; inside list templates, bare paths are relative to the current item. Input components are **two-way bound locally** — typing updates the local model and does *not* hit the network. State reaches the agent only when an `action` fires.

**Catalogs.** The component vocabulary is a swappable JSON Schema document. Basic catalog ships `Text, Image, Icon, Video, AudioPlayer, Row, Column, List, Card, Tabs, Divider, Modal, Button, CheckBox, TextField, DateTimeInput, ChoicePicker, Slider`. You define your own to constrain the agent to your design system. Catalogs are **mixable** per-surface. Resolution order is strict: component-level `catalogId` → surface default `catalogId` → **error**. There is deliberately no fallback.

**Safety.** Declarative data, not code. Functions are catalog-declared and called by name. `allowedCallers` (`rendererOnly` | `agentOnly` | `rendererOrAgent`) is enforced *at runtime by the renderer*, read from the active catalog.

---

## 3. Findings that constrain the design

These came out of reading the spec and the repo, and they change the plan.

**3.1 — No renderer supports v1.0. None.**
React, Lit, Angular, and Flutter are ✅ Stable for v0.8 and v0.9.1, and 🚧 *Planned* for v1.0. Community renderers (A2UI-Android, a2ui-react-native, Lynx, AGenUI, Vercel's json-render) are all v0.8/v0.9. **We are writing a renderer regardless of framework choice.**

**3.2 — There is no Svelte renderer at any version.**
`dashboard-app` is Svelte 5 + Tailwind 4 + `bits-ui`. Adopting React or Lit means a second framework, a second build, and a component library we'd have to rebuild anyway to match the design system.

Combined, 3.1 + 3.2 collapse the "which renderer do we adopt" question *for the web*. We write one. Which means the version choice is largely free — so take v1.0 for `callAgentFunction` (lazy graph expansion), single-message instantiation, and mixable catalogs.

One renderer escapes this reasoning and deserves an explicit answer rather than an omission: **Flutter (GenUI SDK)** is ✅ Stable at v0.9.1 and is the only maintained renderer covering Mobile/Desktop/Web from a single codebase. It is treated in §12.0.

**3.3 — Nous already has the hard parts of the transport.**
`nous/api/rest.py:167` `chat_stream` is a working SSE endpoint with a genuinely subtle keepalive: it races the next event against a ping interval using `asyncio.wait` (*not* `wait_for`) so a timeout doesn't cancel the pending task, then emits an SSE comment line to reset the client's read timer. That pattern is directly reusable and would take a day to rediscover.

**3.4 — Several v1 surfaces are already backed by live endpoints.**
Heartbeat findings have `acknowledge` / `resolve` / `dismiss` POST routes. DAG has `dashboard_dag`, retry, cancel. Decisions have list/review/resolve. **The surfaces are thin presentation over an API that already exists.** This is a much smaller build than it looks.

**3.5 — A2UI has no concept of surface durability.**
The spec assumes a live connection: `surfaceId` must be "globally unique for the renderer's lifetime," and there is no persistence story. Nous pushes surfaces from heartbeat ticks and DAG completions that fire when nobody is watching. **Durable surfaces are a Nous-side extension** (§7), not something the protocol gives us.

**3.6 — `@a2ui/web_core` is framework-agnostic and reusable, but only up to v0.9.**
`renderers/web_core` (Apache-2.0, npm `@a2ui/web_core`) is explicitly *"designed to be framework-agnostic, providing the foundation for specific renderer implementations like Angular, React, or Lit."* It ships message processing, Zod schema validation, a reactive `DataModel`/`SurfaceGroupModel` on `@preact/signals-core`, `DataContext` expression + function execution, and the catalog registry. None of that is DOM-bound, and signals interoperate cleanly with Svelte 5 runes.

The catch: its `exports` map publishes `./v0_8`, `./v0_9` and `./v0_9/basic_catalog` — **there is no `v1_0` runtime module**, only v1.0 *schemas* vendored by the build (`copy-spec` already copies `specification/v1_0/json` and `catalogs`). So the reuse is real at v0.9.1 and hypothetical at v1.0. This is a genuine new input to Q3 (§16) — see §12.0.

**This corrects a number I gave in rev 1.** I said we write ~2,320 LOC of renderer "either way." At v0.9.1 that's overstated: web_core absorbs the protocol, validation, data-model and expression layers, leaving ~1,910. The discount is ~410 LOC — real, but smaller than it first looks, because the irreducible cost was always the ~29 component adapters, and no upstream package writes those in *your* design system.

---

## 4. Architecture

```
┌──────────────────────── Nous (Python / Starlette) ─────────────────────────┐
│                                                                             │
│  Producers                    Composition                Transport          │
│  ─────────                    ───────────                ─────────          │
│  heartbeat checks ─┐                                                        │
│  DAG nodes ────────┤     ┌──────────────────┐                               │
│  decision sweeps ──┼───▶ │ SurfaceService   │                               │
│  runner / tools ───┤     │                  │      ┌──────────────────┐     │
│  escalation gate ──┘     │ builders.py      │─────▶│ outbox (Postgres)│     │
│                          │  (templates)     │      └────────┬─────────┘     │
│                          │ compose tool     │               │               │
│                          │  (LLM + repair)  │               ▼               │
│                          │ validator.py     │      GET /a2ui/stream  (SSE)  │
│                          └──────────────────┘               │               │
│                                   ▲                         │               │
│                          ┌────────┴─────────┐               │               │
│                          │ ActionRouter     │◀── POST /a2ui/action          │
│                          │  allowlist       │◀── POST /a2ui/call            │
│                          │  censors         │◀── POST /a2ui/function-response│
│                          │  ledger (F032)   │                               │
│                          └──────────────────┘                               │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │  SSE envelopes ▼   ▲ POST messages
┌──────────────── Companion (Svelte 5 PWA, /companion) ──────────────────────┐
│  transport.ts  → resumable SSE client (?since=seq)                          │
│  store.svelte.ts → surfaces map, data models, JSON-Pointer engine           │
│  Renderer.svelte → adjacency-list walker, scope stack, template expansion   │
│  catalog/       → component registry: A2UI name → Svelte component          │
│  functions.ts   → renderer-side function registry + agent-RPC fallback      │
└─────────────────────────────────────────────────────────────────────────────┘
```

Deployed behind the existing Traefik + oauth2-proxy at `nous.fatykhov.us`. Same auth, no new gateway.

---

## 5. Transport

**Decision recorded:** `a41370bf` — SSE down, POST up. Rejected WebSocket (Traefik/oauth2-proxy upgrade friction, no existing WS in the codebase), A2A and MCP bindings (negotiation machinery whose only consumer is our own first-party client).

### 5.1 Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/a2ui/stream?since={seq}` | SSE. Downstream envelopes. Resumes from `seq`. |
| `GET` | `/a2ui/surfaces?status=live` | Cold-start hydration index. |
| `GET` | `/a2ui/surfaces/{surface_id}` | Full snapshot as a single `createSurface` (v1.0 inline components + dataModel). |
| `POST` | `/a2ui/action` | Renderer→agent `action` — button clicks and **form submits**, the latter carrying the surface data model. |
| `POST` | `/a2ui/message` | **Free-text user turn.** `{text, session_id, client_msg_id}`. Starts an agent turn; output streams back on the same SSE connection. Idempotent on `client_msg_id`. |
| `POST` | `/a2ui/call` | Renderer→agent `callAgentFunction`. |
| `POST` | `/a2ui/function-response` | Reply to `callRendererFunction`. |
| `POST` | `/a2ui/capabilities` | Renderer capability registration (per-connection). |
| `GET` | `/a2ui/catalog/{catalog_id}` | Serve the vendored catalog JSON. |

Registered in the `routes = [...]` list in `nous/api/rest.py`. **Must be listed before any `Mount`** — Starlette matches top-down, same trap the `/dashboard/*` routes already document.

### 5.2 Stream framing

Each SSE event carries one A2UI envelope, with the outbox sequence as the SSE `id` so browsers resume natively via `Last-Event-ID`:

```
id: 4821
event: a2ui
data: {"version":"v1.0","updateDataModel":{"surfaceId":"nous:dag:monitor:7f21a9","path":"/nodes/2/status","value":"completed"}}

: keepalive
```

Reuse the `chat_stream` keepalive pattern verbatim (`asyncio.wait` with a timeout that does not cancel the pending task).

**One stream, four event types.** Because the companion now carries conversation as well as surfaces (Q4), the downstream is a single ordered timeline rather than two parallel channels:

| `event:` | Payload | Meaning |
|---|---|---|
| `a2ui` | A2UI envelope | Surface create / update / delete. |
| `message` | `{role, text, channel, msg_id, ts}` | A conversational turn — **including turns that originated in Telegram**. |
| `token` | `{msg_id, delta}` | Streaming delta for an in-flight assistant turn. |
| `control` | `{type: "resync" \| "notify"}` | Transport control. |

A single `seq` shared across all four is what guarantees a surface and the sentence that introduced it can never render out of order.

### 5.3 Reconnect

1. Client reconnects with `Last-Event-ID: 4821`.
2. Server replays outbox rows where `seq > 4821` **for surfaces still `live`**.
3. If the gap exceeds `a2ui_outbox_replay_window` (default 500 rows) or referenced surfaces were compacted, respond with a `resync` control event; client drops local state and re-hydrates from `GET /a2ui/surfaces`.

Snapshot-over-replay is the failure-safe path. Never try to patch a stale tree.

### 5.4 One conversation, two windows

**Decision recorded:** `4898e56e` — chat input is in, and the split-history risk is engineered out rather than avoided.

The original objection to chat input was never the text box; it was the consequence — two divergent records of what was said, so `recall_deep` returns half a conversation depending on which channel it hits. That is a genuine memory-integrity failure, so it is designed against directly:

1. **One store.** A companion message is persisted through the *same* session/episode path as a Telegram message. No parallel table, no companion-only transcript.
2. **Channel is a tag, not a partition.** Every turn carries `channel ∈ {telegram, companion}`. Consolidation, episode formation, and recall are channel-blind by default; the tag exists for provenance and delivery routing only.
3. **Bidirectional echo.** Turns entered in Telegram are pushed to connected companion clients as `event: message`. The companion transcript is a live view of the *whole* conversation, never a subset. **This is the requirement that actually prevents divergence** — without it, two windows quietly become two memories.
4. **Reply routing.** A text reply returns to the **originating channel only**. If the turn also produced a surface, Telegram gets the one-line pointer from §13 and the surface goes to the companion. No double-delivery.
5. **Ordering.** Both channels share the outbox `seq`, so transcript order is identical everywhere.

**Deliberate non-goal:** the companion does not attempt Telegram's notification reliability, offline queueing, or lock-screen delivery. If those start being asked for, that is the §15 scope-creep signal and the answer is no.

---

## 6. Data model

New tables in the `system` schema (execution/presentation state, not memory).

```sql
CREATE TABLE system.a2ui_surfaces (
    surface_id       TEXT PRIMARY KEY,
    origin           TEXT        NOT NULL,   -- heartbeat|dag|sweep|chat|escalation|manual
    kind             TEXT        NOT NULL,   -- template name, e.g. 'approval_gate'
    catalog_id       TEXT        NOT NULL,
    status           TEXT        NOT NULL DEFAULT 'live',  -- live|resolved|expired|deleted
    priority         SMALLINT    NOT NULL DEFAULT 0,       -- 0 ambient, 1 attention, 2 blocking
    title            TEXT        NOT NULL,
    components       JSONB       NOT NULL,
    data_model       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    allowed_actions  TEXT[]      NOT NULL DEFAULT '{}',
    session_id       UUID,
    trace_id         UUID,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at       TIMESTAMPTZ,
    resolved_at      TIMESTAMPTZ
);
CREATE INDEX ON system.a2ui_surfaces (status, priority DESC, created_at DESC);
CREATE INDEX ON system.a2ui_surfaces (expires_at) WHERE status = 'live';

CREATE TABLE system.a2ui_outbox (
    seq         BIGSERIAL PRIMARY KEY,
    surface_id  TEXT NOT NULL REFERENCES system.a2ui_surfaces(surface_id) ON DELETE CASCADE,
    envelope    JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON system.a2ui_outbox (surface_id, seq);

CREATE TABLE system.a2ui_actions (
    id                   UUID PRIMARY KEY,
    surface_id           TEXT NOT NULL,
    action_name          TEXT NOT NULL,
    source_component_id  TEXT,
    context              JSONB NOT NULL DEFAULT '{}'::jsonb,
    data_model           JSONB,
    status               TEXT NOT NULL DEFAULT 'pending', -- pending|dispatched|completed|rejected
    rejection_reason     TEXT,
    ledger_entry_id      UUID,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at         TIMESTAMPTZ
);
CREATE INDEX ON system.a2ui_actions (surface_id, created_at DESC);
```

`components` and `data_model` are the **authoritative current state**, updated on every mutation. The outbox is the *delta log* for live clients. That split is what makes snapshot-hydration cheap and replay optional.

### 6.1 Surface ID convention

```
nous:{origin}:{kind}:{short_id}
```

Examples: `nous:heartbeat:findings:a3f21c`, `nous:escalation:approval:0f9e21`, `nous:dag:monitor:7f21a9`.

The spec requires global uniqueness for the renderer's lifetime and warns that orchestrators with subagents must manage this. The `origin` segment prevents a DAG node and a heartbeat check from ever colliding; `short_id` is 6 hex chars of a UUID4.

### 6.2 Retention — records are permanent, surfaces are disposable

**Resolved (Q6): split them.** Under advisory review (§10.4) the action-review record *is* the audit trail of what Nous did while unsupervised. A flat 30-day TTL would delete precisely the evidence that makes autonomy accountable. But the A2UI envelope — component tree, data model, layout — is presentation, and reconstructible from the record.

| Tier | What | Where | Retention |
|---|---|---|---|
| **Evidence** | The act itself: `trace_id`, what was executed, verb chosen (`acknowledged` / `course_corrected` / `reverted` / `promoted_to_rule` / `no_objection`), actor, timestamps, resolution note | `brain.decisions` + execution ledger | **Permanent** |
| **Presentation** | A2UI surface envelope: component tree, data model, catalog refs, outbox frames | `heart.a2ui_surfaces`, `heart.a2ui_outbox` | live until `expires_at`; **30 days** after `resolved`/`expired`, then deleted |

Rules that make the split safe:

- The evidence row is written **at resolution time, before** the surface transitions to `resolved` — never derived from the surface afterwards. Surface deletion can therefore never lose a record.
- Silence counts. When a surface expires unactioned it writes `no_objection` evidence *then* expires; "nobody looked" is itself an audit fact under advisory review.
- `surface_id` is stored on the evidence row as a **weak reference**. Post-sweep it dangles by design; the UI renders the evidence row with a "surface expired" note rather than a 404.
- Read-only surfaces with no verb (memory browser, DAG monitor, timeline) produce no evidence row and follow the plain 30-day path.
- Unchanged sweeps: `live` with `expires_at < now()` → `expired` (existing heartbeat); outbox rows for non-`live` surfaces deleted after 24h.

---

## 7. Surface lifecycle (Nous-side extension)

A2UI has no durability model (finding 3.5). Nous adds one.

```
        ┌──────── push_surface() / compose_surface()
        ▼
   ┌─────────┐   updateComponents / updateDataModel   ┌─────────┐
   │  live   │◀──────────────────────────────────────▶│  live   │
   └────┬────┘                                        └─────────┘
        │
        ├── terminal action fired ──▶ resolved  (deleteSurface emitted)
        ├── expires_at reached ─────▶ expired   (deleteSurface emitted)
        └── superseded by newer ────▶ resolved  (dedup, see below)
```

**Rules**

1. **Offline push is normal.** A surface created while no client is connected is simply `live` in the DB. The next connection hydrates it. Nothing is lost, nothing is retried.
2. **Deduplication by fingerprint.** A heartbeat check that fires hourly must not create 24 approval cards. Producers pass a `dedup_key`; if a `live` surface exists with that key, it is **updated in place** rather than recreated. This mirrors the existing heartbeat finding-fingerprint discipline.
3. **`createSurface` is idempotent server-side.** The spec makes re-creating an existing `surfaceId` an error. The `SurfaceService` guarantees uniqueness so that error is unreachable from producer code.
4. **Priority drives delivery, not just sort order:**
   - `0` ambient — appears in the app, no notification.
   - `1` attention — Telegram one-liner with deep link.
   - `2` blocking — Telegram + `callRendererFunction: notify` if a client is connected. Reserved for approval gates on irreversible actions.

---

## 8. Catalogs

Two catalogs, mixed per surface. Both vendored into `nous/a2ui/catalogs/` and served from `/a2ui/catalog/{id}`.

### 8.1 `a2ui-basic` (upstream, unmodified)

Vendored verbatim at a pinned commit. We implement a **subset**: `Text, Row, Column, List, Card, Tabs, Divider, Modal, Button, CheckBox, TextField, ChoicePicker, Slider, DateTimeInput, Icon, Image`. `Video` and `AudioPlayer` are out of scope for v1.

### 8.2 `nous-core` (ours)

`catalogId: "https://nous.fatykhov.us/a2ui/v1.0/nous-core/catalog.json"`

Domain components, each mapping to an existing `bits-ui`/Tailwind component in `dashboard-app/src/lib/ui/`:

| Component | Props (abridged) | Maps to |
|---|---|---|
| `StatTile` | `label`, `value`, `delta`, `intent` | `StatGrid.svelte` |
| `DecisionCard` | `decisionId`, `description`, `confidence`, `stakes`, `category`, `outcome` | `card/` + `badge/` |
| `ConfidenceMeter` | `value` (0–1), `calibrationBand` | new, ~40 LOC |
| `MemoryGraph` | `nodes` (path), `edges` (path), `focusNodeId`, `layout` | `lib/viz/` (D3 already present) |
| `Timeline` | `events` (path), `groupBy` | `lib/viz/` |
| `DiffView` | `before`, `after`, `mode` | new |
| `KeyValueTable` | `rows` (path) | `table/` |
| `EntityChip` | `kind` (fact\|decision\|episode\|procedure), `entityId`, `label` | `badge/` |
| `ApprovalPanel` | `title`, `summary`, `risk`, `recommendation`, `options` (path) | `card/` + `dialog/` |
| `DagGraph` | `nodes` (path), `edges` (path) | `lib/viz/` |
| `MarkdownView` | `content` | existing md renderer |
| `SparkLine` | `series` (path) | `lib/viz/` |
| `LogStream` | `lines` (path), `follow` | new |

**Functions**

*Renderer-side* (`allowedCallers: "rendererOnly"`): `relativeTime`, `confidenceLabel`, `formatTokens`, `copyToClipboard`, `truncateMiddle`.

*Agent-side* — declared in the catalog so the model knows they exist; the renderer has no local registration, so per spec §"Fallback to Agent RPC" it dispatches `callAgentFunction`: `expandGraphNode(nodeId, depth)`, `searchMemory(query, limit)`, `loadDecisionDetail(decisionId)`, `loadEpisodeChunks(episodeId)`.

*Agent→renderer* (`allowedCallers: "agentOnly"`): `notify(title, body)`, `focusSurface(surfaceId)`, `getViewport()`.

### 8.3 Catalog authoring constraints

The v1.0 spec imposes rules a hand-written catalog will violate on the first try. Encode all of these as CI lint checks:

- Child references **must** use `$ref: common_types.json#/$defs/ComponentId`, lists **must** use `ChildList`. A raw `"type": "string"` makes the validator treat a child ID as static text and silently skip link checking.
- Every component needs `component: {"const": "<Key>"}` matching its map key (discriminator dispatch).
- `$defs` may contain **only** `anyComponent` and `anyFunction`. No shared helpers — inline them.
- Names must satisfy UAX #31: `^[\p{XID_Start}_][\p{XID_Continue}]*$`. No `-`, no spaces, no leading digit.
- `Surface` is reserved. Do not define it.
- Only 10 permitted root-level keys.
- Set `protocolVersion: "1.0"` — it defaults to `"0.9"` if omitted.

---

## 9. Composition

**Decision recorded:** `03bfea34` — template-first, LLM as gated escape hatch.

### 9.1 Template builders (default path)

A small typed DSL in `nous/a2ui/dsl.py` that makes invalid trees hard to express, and one builder per surface kind in `nous/a2ui/builders/`.

```python
# nous/a2ui/builders/approval.py
from nous.a2ui.dsl import Surface, Column, Text, List, ApprovalPanel, Button, event

def approval_gate(
    *, title: str, summary: str, risk: str,
    recommendation: str, options: list[Option], trace_id: UUID,
) -> Surface:
    s = Surface(
        kind="approval_gate", origin="escalation",
        catalog_id=NOUS_CORE, priority=2,
        title=title, trace_id=trace_id,
        allowed_actions=["approval.choose", "approval.defer"],
        expires_in=timedelta(hours=24),
    )
    s.data({
        "summary": summary, "risk": risk,
        "recommendation": recommendation,
        "options": [o.as_dict() for o in options],
    })
    s.add(
        Column("root", children=["panel", "opts", "defer"]),
        ApprovalPanel("panel",
            title=title,
            summary={"path": "/summary"},
            risk={"path": "/risk"},
            recommendation={"path": "/recommendation"}),
        List("opts", template="opt_btn", path="/options"),
        Button("opt_btn", child="opt_label",
            variant={"path": "variant"},
            action=event("approval.choose", {
                "optionId": {"path": "id"},
                "traceId": trace_id,
            })),
        Text("opt_label", text={"path": "label"}),
        Button("defer", child="defer_label",
            variant="borderless",
            action=event("approval.defer", {"traceId": trace_id})),
        Text("defer_label", text="Ask me later"),
    )
    return s
```

Every builder gets a unit test that runs the output through the vendored JSON Schema. **A builder that emits invalid A2UI fails CI**, not production.

Exposed to the agent as a cheap tool:

```json
{
  "name": "push_surface",
  "input_schema": {
    "type": "object",
    "properties": {
      "template": {"enum": ["approval_gate", "decision_sweep", "heartbeat_findings",
                             "memory_graph", "dag_monitor", "entity_detail"]},
      "params":   {"type": "object"},
      "dedup_key":{"type": "string"},
      "notify":   {"type": "boolean"}
    },
    "required": ["template", "params"]
  }
}
```

Cost: one small tool call. No catalog in the prompt.

### 9.2 LLM composition (escape hatch)

For genuinely novel UI — a chart answering a one-off numeric question, an ad-hoc comparison table.

```
compose_surface(title, intent, components[], data_model{})
```

Pipeline:

1. Inject the `nous-core` catalog **summary** (component names + one-line descriptions + required props, ~800 tokens), not the full JSON Schema.
2. Model emits the `components` array and `dataModel`.
3. Validate against the vendored schemas → on failure return the spec's standard error format (`code: VALIDATION_FAILED`, `surfaceId`, `path`, `message`) and retry. **Max 2 repairs**, then fall back to a `MarkdownView` surface containing the intended content.
4. Structural checks beyond schema: exactly one `root`; no dangling child refs; no cycles; every `action.event.name` ∈ declared `allowed_actions`.
5. Persist + push.

**Nothing unvalidated ever reaches a client.**

---

## 10. Actions and security

A2UI's safety story ends at "no code crosses the wire." That's necessary, not sufficient — actions come *back* and can trigger real tool calls. The server-side model:

**1. Per-surface allowlist.** Every surface declares `allowed_actions` at creation. `POST /a2ui/action` rejects any `name` not in that array for that `surface_id`. The client is never trusted, and a compromised or replayed payload cannot invoke an action the surface never offered.

**2. Handler registry.** Each action name maps to a handler declaring `mutating: bool` and `irreversible: bool`.

```python
@action("dag.retry_node", mutating=True, irreversible=False)
async def _(ctx: ActionContext) -> ActionResult: ...
```

**3. Censors run before dispatch.** Mutating actions pass through the existing censor pipeline with the action name and context as the match target. Existing censors (the underwater-position family, `rm -rf`) apply unchanged. A censored action returns a `rendererFunctionResponse` error and paints the failure into the surface — it does **not** silently no-op.

**4. Advisory review, not a blocking gate** (Q5, decided; decision `6e74293b`). The companion adds **no new blocking layer**. Nous executes per the standing autonomy directive, then renders an *Action Review* surface describing what it did. Four consequences follow, and each is a requirement rather than a rewording:

- **The verbs change.** Approve/Reject presuppose a pending action. After the fact the vocabulary is **Acknowledge / Course-correct / Revert / Make-it-a-rule.**
- **Revertibility is declared, never assumed.** Every reviewable handler supplies `compensation: {revertible: bool, handler: str | None, window_s: int}`. If nothing can undo it, the card says so plainly and renders no Revert button. A Revert button that silently fails is worse than no button.
- **Silence is consent.** Records auto-archive after `a2ui_review_archive_days` with outcome `no_objection`. There is no expiry-that-abandons, because nothing is pending.
- **Disagreement is calibration data.** Course-correct writes `resolve_decision(outcome=…)` against the originating `trace_id`. Repeated correction of the same pattern surfaces *Make-it-a-rule*, which drafts a standing rule or censor.

**What "advisory" does NOT mean.** It does not repeal the standing autonomy directive. Genuinely irreversible or destructive actions, real spend, credential/security-posture changes, values calls, and repeated-failure patterns still escalate *before* execution, exactly as they do today — they simply render as a surface instead of prose (Appendix A). Advisory describes the *surface*, not the escalation policy. Conflating the two would silently weaken an existing safety boundary.

**5. Full audit.** Every action writes to `system.a2ui_actions` and to the F032 execution ledger with the originating `trace_id`. Approvals are reconstructible after the fact — which is the entire point of routing them here.

**6. Nonce + expiry.** Each surface carries `metadata.extensions.com_nous_nonce`, echoed on action egress (the spec reserves `metadata.extensions` on `UserActionMessage.action` for exactly this). Mismatched or expired nonce → reject. Third-party extension keys must be prefixed with a distinct org identifier, hence `com_nous_`.

**7. Auth.** oauth2-proxy at the edge, unchanged. The SSE endpoint additionally verifies the session on connect — SSE connections are long-lived, so a mid-stream session revocation must terminate the stream.

**8. Rate limit.** 30 actions/minute per session. Beyond that, `429`.

**9. Form submits carry state, so they are re-validated server-side.** A2UI two-way binding is local — keystrokes never touch the network and state ships only on an `action`. A submit therefore arrives with the whole surface data model attached, from a client we do not trust. Re-validate against the surface's declared schema on the server; the client-side `checks` that disabled the button are a UX affordance, not a control.

**10. Free-text input is a conversational turn, not an action.** `POST /a2ui/message` bypasses the action allowlist by design — it starts an ordinary agent turn and inherits the ordinary censor pipeline. It must never be a path to invoking a handler by name.

---

## 11. The v1 surface set

Six surfaces. Four are thin presentation over endpoints that already exist (finding 3.4).

**1. Action Review** — `origin: escalation`, `priority: 2`
The autonomy-directive surface, now post-hoc and advisory (§10.4). What Nous did, why, what it cost, and what can still be undone — reasoning inline, risk annotated, revertibility declared. Buttons: **Acknowledge / Course-correct / Revert / Make-it-a-rule.** Resolves against the originating decision's `trace_id`, feeding calibration. *This is the flagship — build it first.*

**2. Decision Sweep** — `origin: sweep`
Pending decisions as a scrollable list of `DecisionCard`s with inline outcome pickers and a batch-submit. Backs onto `resolve_decisions`. Replaces a genuinely painful chat loop, and it's the surface with the clearest immediate ROI given the periodic-sweep procedure already exists.

**3. Heartbeat Findings** — `origin: heartbeat`, `priority: 1`
Findings grouped by fingerprint; acknowledge / resolve / dismiss buttons wired to the existing `/heartbeat/findings/{fingerprint}/*` routes. Near-zero backend work.

**4. Memory Graph Explorer** — `origin: manual`
`MemoryGraph` seeded with a focus node; tapping a node calls `expandGraphNode` via `callAgentFunction`, agent responds with `agentFunctionResponse`, renderer merges into the data model. **This is the surface that justifies targeting v1.0** — under v0.9.1 there's no renderer→agent RPC and every expansion would need a full round trip through the chat channel.

**5. DAG Monitor** — `origin: dag`, `priority: 1` on failure
Live node status via `updateDataModel` patches at `/nodes/{i}/status`. Retry and cancel actions. Node result drill-down via `Modal`.

**6. Ad-hoc Composed** — `origin: chat`
The `compose_surface` output. Charts, comparison tables, forms.

**7. Conversation** — `origin: chat`, always present
The persistent surface (Q4, decided). Transcript rendered from `event: message` — *including Telegram turns* — with a composer at the bottom. Not a separate app mode: it is **surface zero**, and every other surface stacks above it in the same timeline, so a card always appears in the conversational context that produced it.

**Structured input is native, not an extension.** Verified against the A2UI basic catalog: `TextField` (`shortText` / `longText` / `number` / `obscured` / `date`), `CheckBox`, `ChoicePicker` (`MultipleChoice` in v0.8), `Slider`, `DateTimeInput`, `Button`, `List`, `Modal`, `Tabs` all exist upstream. **Zero catalog work** — the cost is renderer adapters only. That is why "buttons, lists, checkboxes, etc." moves from Phase 3 into Phase 1.

---

## 12. Renderer implementation

### 12.0 Client platform: why Svelte, not Flutter

Flutter is the strongest alternative and the case for it is not weak. `flutter/genui` is maintained by the Flutter team, is ✅ Stable at v0.8 and v0.9.1, and is the **only** maintained renderer spanning Mobile + Desktop + Web from one codebase. A2UI itself is a Google project and the repo ships a first-party Flutter sample (`samples/client/flutter/restaurant_finder`). "One codebase, real iOS/Android app, renderer already written" is a legitimate offer.

It still loses here, for reasons specific to Nous rather than to Flutter:

**Telegram is already the native mobile app.** The reason anyone wants a Flutter build is push notification and pocket-presence. Nous has both, today, in the channel that is explicitly *not* being replaced. The companion's job is rendering surfaces, not reaching you — a Telegram ping with a deep link into `/companion/s/<surface_id>` (§13) covers reach at zero cost. Standing up FCM, APNs and App Store distribution rebuilds a solved problem, and it would split notification state across two delivery channels — the same failure mode §5.4 exists to prevent, re-introduced one layer down.

**Flutter Web is the wrong engine for the surface that matters most.** GenUI on web renders through CanvasKit: a ~2 MB initial payload and a canvas, not a DOM. That costs text selection, browser find-in-page, real CSS, the existing Tailwind theme, and reduces accessibility to a synthesized semantics tree. The heaviest companion work — reading decision traces, expanding graph neighbourhoods, comparing DAG nodes — happens on a large screen. Flutter Web would make the primary surface worse to buy a secondary one.

**The renderer was never the expensive part; integration is.** After Q4, the companion shares the session/episode store, the transcript, and the Telegram echo path (§5.4). Embedded in `dashboard-app` it inherits oauth2-proxy auth, routing, layout, the design system, and the D3 helpers. A Flutter app cannot embed — it is an island needing its own auth flow, deploy, release channel, CI (a ~1 GB Flutter SDK in the build image), and a language with zero existing Nous procedures or skills. That is a second full stack for a single-user application.

**v1.0 neutralises Flutter's main advantage.** GenUI is 🚧 Planned for v1.0 exactly like React, Lit and Angular. If Q3 lands on v1.0, Flutter is not turnkey either — closing the gap would mean forking Dart we do not control, which is strictly worse than writing a mapping layer in a stack we do.

**And per 3.6, the "we write it either way" premise is now partly false — in Svelte's favour.** `@a2ui/web_core` hands us the protocol layer for free at v0.9.1. The build-your-own side got *cheaper*, not more expensive.

| Option | Renderer cost | Mobile | Web quality | Integration | Verdict |
|---|---|---|---|---|---|
| **Svelte in `dashboard-app`** | ~1,910 LOC (v0.9.1, web_core) / ~2,320 (v1.0) | PWA + Telegram deep link | Native DOM, existing theme | Zero — same repo, auth, build | **Chosen** |
| Flutter / GenUI | ~0 at v0.9.1; unknown at v1.0 | True native, own push | CanvasKit: heavy, no DOM/CSS | New stack: Dart, CI, auth, deploy | Deferred |
| React or Lit (upstream renderer) | ~0 at v0.9.1 | PWA | Good | Second framework in the build; components still restyled | Rejected |
| Flutter mobile **+** Svelte web | ~1,910 + upkeep | True native | Native DOM | Two clients, two release paths | Phase 3 only |

**Why this is a cheap decision to get wrong.** The server is client-agnostic on purpose: canonical A2UI over SSE down and HTTP POST up (§5), with no assumption about who renders it. That is the entire premise of the protocol. Adding a Flutter client later means *adding a client*, not rewriting the backend — and the conformance fixtures (§12, below) are the guard that keeps two renderers honest, which hand-written email HTML never had.

**Revisit Flutter when any of these fire:** (a) Telegram deep-link friction proves real in Phase 1 use — measure it, don't assume it; (b) GenUI ships v1.0 Stable; (c) biometric-gated confirmation on irreversible actions becomes a requirement, which a PWA cannot do well; (d) offline or background surface access is wanted. Absent those, the PWA is the cheaper answer to the same need.

### 12.1 Modules


`dashboard-app/src/companion/` — same repo, same build, separate entry point and route namespace (`/companion`). Reuses the existing Tailwind theme, `bits-ui` primitives, and D3 viz helpers.

Per 3.6, the protocol/validation/data-model rows collapse into `@a2ui/web_core` at v0.9.1 and must be written by us at v1.0. **Q3 committed to v1.0, so the operative number is ~2,320** — the ~410 LOC premium is the accepted price of `callAgentFunction` and single-message instantiation. The v0.9.1 column stays in the table because it is the Phase-0 escape hatch (§14): if the walker and binding engine are not working after a day, fall back to web_core at v0.9.1 rather than pushing on.

| Module | Responsibility | Est. LOC |
|---|---|---|
| `transport.ts` | SSE client, `Last-Event-ID` resume, POST helpers, resync | 180 |
| `bridge.svelte.ts` | Adapt web_core `SurfaceGroupModel` + signals → Svelte 5 runes; surfaces map | 140 |
| `pointer.ts` | RFC 6901 pointer resolution + scope stack — *only if self-implementing (v1.0)* | 120 |
| `Renderer.svelte` | Adjacency-list walker, template expansion, `@index`, missing-ref placeholders | 260 |
| `functions.ts` | Renderer-function registry + agent-RPC fallback (`formatString`/expressions from web_core) | 110 |
| `validate.ts` | `checks` → button-disabling UI glue (Zod schema validation from web_core) | 50 |
| `catalog/*.svelte` | ~29 component adapters, **full input set included** (TextField, CheckBox, ChoicePicker, Slider, DateTimeInput) | 900 |
| `Conversation.svelte` | Transcript view, composer, streaming-token assembly, optimistic send + reconcile | 180 |
| `forms.ts` | Data-model diffing, submit payload assembly, client-side `checks` evaluation | 90 |
| **Total** | | **~2,320 committed** (v1.0, self-implemented protocol layer) · ~1,910 fallback (v0.9.1 on web_core) |

### Renderer requirements that are easy to get wrong

- **Buffer until `root` exists.** Components arriving before `root` are stored, not dropped, and produce no visible output.
- **Placeholders, not exceptions,** for dangling child refs and unresolved paths. Progressive rendering depends on this.
- **Two-way binding is local.** Keystrokes update the local model and never hit the network. State ships only on `action`.
- **Reactivity across bindings.** A `TextField` bound to `/x` and a `Text` bound to `/x` must update together in real time.
- **Scope stack for templates.** Inside a list template, `name` is relative (`/employees/0/name`); `/company` stays absolute.
- **`@index` errors outside collection scope.** It is not a global.
- **Always answer `callRendererFunction`** — even for `void` returns. Non-response is a protocol violation.
- **Enforce `allowedCallers` at runtime.** A `rendererOnly` function invoked by the agent returns `INVALID_FUNCTION_CALL`.
- **Type coercion:** numbers/booleans stringify normally; `null`/`undefined` → `""`; objects/arrays → JSON.
- **Accessibility is normative,** not optional. Map `AccessibilityAttributes` to ARIA; explicit `label` overrides inferred visual text.
- **Optimistic send must reconcile, not duplicate.** A composed message renders immediately under a local `client_msg_id`, then is *replaced* when the server echoes it with its real `msg_id`. Dedupe on `client_msg_id`.
- **A resync must not eat the composer.** `control: resync` drops surface state; it must preserve half-typed input and the transcript.

Conformance tests: the spec's worked examples (contact form JSONL, scope resolution, `formatString` nesting) become fixtures. If our renderer disagrees with the spec's stated output, the test fails.

---

## 13. Telegram ↔ Companion handoff

Telegram remains the narrator. The companion is where you act.

- `priority: 1` → one-line Telegram message + deep link: `https://nous.fatykhov.us/companion/#/s/{surface_id}`
- `priority: 2` → same, plus `callRendererFunction: notify` if a client is connected.
- `priority: 0` → no Telegram traffic at all.

When a surface resolves, Nous posts a short confirmation to Telegram so the chat log stays the complete narrative record. **The conversation history remains in one place** — the companion never becomes a second, divergent memory of what happened.

---

## 14. Phasing

**Phase 0 — Spike (~1 day).**
Vendor schemas, wire a JSON Schema validator into CI, render one hardcoded surface end-to-end in Svelte with no persistence. Proves the adjacency-list walker and data binding. *Kill criterion: if the walker + binding engine isn't working in a day, reconsider wrapping the Lit renderer at v0.9.1 instead.*

**Phase 1 — Core + conversation (~1.5 weeks).**
Migrations, `SurfaceService`, outbox, SSE transport with resume, renderer core, basic-catalog subset **including the full input component set**, unified conversation store with channel tagging and Telegram echo (§5.4), the **Conversation** surface, and **Action Review** wired to the escalation path.

**Phase 2 — Catalog + surfaces (~1 week).**
`nous-core` catalog with lint rules, `DecisionCard` / `MemoryGraph` / `DagGraph` / `Timeline`, surfaces 2–6. `callAgentFunction` for graph expansion. Server-side form-submit validation.

**Phase 3 — LLM composition (~3 days).**
`compose_surface`, validate→repair loop, catalog summary prompt, markdown fallback.

**Phase 4 — Polish (~3 days).**
PWA manifest + service worker, Web Push, offline snapshot cache, Telegram deep links.

Phases 1 and 2 are independently useful. If it stops after Phase 1, Action Review plus a working conversation view already justifies the build.

---

## 15. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| v1.0 is a Candidate; breaking changes possible | Medium | Vendor schemas at a pinned commit. Version-isolate the renderer core behind a `protocolVersion` switch. We control both ends — upgrade on our schedule, not theirs. |
| We own ~1,910–2,320 LOC of renderer with limited upstream to lean on | Medium | Implement only the catalog subset in use. Spec examples as conformance fixtures. Renderer is pure-functional over JSON — cheap to test. |
| LLM emits invalid A2UI | Low | Template-first (§9.1) removes it from the hot path. Validate→repair with markdown fallback for the rest. |
| Companion drifts into a Telegram replacement (chat input now present) | **High** | Structural mitigations in §5.4 — one store, one timeline, echo both ways — so drift costs nothing in memory integrity. Behavioural boundary: no notification reliability, no offline queue, no lock-screen delivery. Telegram stays the push channel; the companion is the interaction channel. |
| Scope creep into "replace the dashboard" | **High** | Non-goal in §1. Different lifecycle: dashboard = human-designed persistent analytics; companion = agent-composed ephemeral action surfaces. Enforce at review. |
| Surface spam from heartbeat producers | Medium | Mandatory `dedup_key` for `origin: heartbeat`. Update-in-place. Priority 0 default. |
| Action replay / CSRF | Medium | Server-side allowlist + per-surface nonce + expiry + rate limit. Client never trusted. |
| Second UI to maintain | Medium | Same repo, same build, same component library, same auth. Marginal cost is the renderer, not the app. |

---

## 16. Question log — all resolved

**Nothing here blocks the build.** Q4 and Q5 were decided by Tim; Q1, Q2, Q3 and Q6 were resolved to recommendation on his instruction (2026-08-29) and are folded into the body above. This section is kept as the reasoning trail, not as a to-do. Brain decision: `1b6ef89a-d051-4b5b-a37a-7bf6d10a05c6` (architecture / medium, 0.72).

**Q1. Same app or separate?** — ✅ **RESOLVED: same repo, new entry point.**
`dashboard-app/src/companion/`, served at `/companion`. Inherits the Tailwind theme, `bits-ui` primitives, D3 helpers and the oauth2-proxy session. Q4 strengthened this after the fact: once the companion shares the session store, transcript and Telegram echo, a separate app would mean duplicating auth, build, CI and the component library to serve one user. The renderer was never the expensive part — integration was. *§4, §12.1 already assume this; no spec change needed.*

**Q2. PWA only, or native mobile too?** — ✅ **RESOLVED: PWA only for v1.**
Manifest + service worker + Web Push, shipped in Phase 4. No native shell. The reasoning is §12.0's, one layer down: Telegram already *is* the native mobile app with working push, and a second notification channel would split delivery state the same way two conversation stores would have split memory. A Telegram ping deep-linking to `/companion/s/<id>` buys pocket-presence at zero cost. *No spec change needed.*

**Q3. Target v1.0 or v0.9.1?** — ✅ **RESOLVED: v1.0 (Candidate), pinned + vendored. Confidence held at ~0.7, not raised.**
The core argument stands: no renderer supports v1.0, none exists for Svelte at any version, so we write a renderer either way and "adopt the version with library support" never applied. v1.0 buys `callAgentFunction` (surface #4 depends on it), single-message instantiation, and mixable catalogs; risk is contained because we own both ends of the wire.

I am recording, not burying, the evidence that argues the other way. §3.6 established that `@a2ui/web_core` would hand us protocol, validation, data-model and expressions **for free at v0.9.1 only** — its `exports` map stops at `./v0_9`, with v1.0 present as vendored schemas but no runtime module. That discount is real. It does not flip the call because it is ~410 LOC of ~2,320, the ~29 component adapters (~900 LOC) are version-invariant, and v0.9.1 means either dropping `callAgentFunction` or hand-rolling it non-standard — a private dialect is worse than a pinned candidate.

**This is the weakest of the four resolutions, and it is deliberately the cheapest to reverse:** Phase 0's kill criterion is exactly this fallback, and it fires after ~1 day, not after the build.

**Q4. Chat input in the companion app?** — ✅ **DECIDED (Tim, rev 2): yes** — chat input *and* the full interaction set: buttons, lists, checkboxes, choices, sliders, dates.
This reversed my recommendation, and the design absorbed it rather than merely complying. My objection was to the *consequence* (split conversation history), not the feature, so §5.4 removes the consequence: one session store, channel-tagged turns, bidirectional Telegram↔companion echo, one ordered timeline. Verified upside — every requested affordance already exists in the A2UI basic catalog, so it costs ~+270 renderer LOC and **zero protocol or catalog work**, and it pulled forms from Phase 3 to Phase 1.
**Residual risk still being watched:** "not a Telegram replacement" is now a discipline rather than a structural fact. The tripwire is any request for notification reliability, offline queueing, or lock-screen delivery in the companion — §15 says refuse it, and I will.

**Q5. Do approval gates *block* autonomous execution, or are they advisory?** — ✅ **DECIDED (Tim, rev 2): advisory.**
Rewritten into §10.4 and surface #1. Material consequences: verbs became **Acknowledge / Course-correct / Revert / Make-it-a-rule**; `compensation` metadata is mandatory on every reviewable handler; silence archives as `no_objection`; disagreement writes back as calibration against the originating `trace_id`.
**Boundary restated, because this one is easy to over-read:** advisory describes the *surface*, not the escalation policy. Irreversible/destructive actions, real spend, credential changes and values calls still escalate **before** execution under the standing directive — they just render as a surface now instead of prose.

**Q6. Retention.** — ✅ **RESOLVED: split evidence from presentation.** Fully specified in §6.2.
The 30-day guess was wrong in one direction only: under Q5-advisory, action-review records *are* the audit trail of unsupervised execution, so they persist **permanently** in `brain.decisions` + the ledger, written at resolution time before the surface resolves. The A2UI envelope stays disposable at 30 days. `surface_id` is a weak reference that is allowed to dangle. Silence writes `no_objection` evidence before expiring — "nobody looked" is an audit fact too.

### 16.1 Revisit triggers

These are the conditions under which a resolved answer should be reopened. Written down now so reversal is evidence-driven rather than mood-driven.

| Resolution | Reopen when |
|---|---|
| Q1 same-repo | Companion build time materially degrades the dashboard's, or a non-Nous consumer wants the renderer as a package |
| Q2 PWA-only | Deep-link friction measured as real in Phase 1; or biometric-gated confirmation on irreversible actions becomes a requirement; or offline access is wanted |
| Q3 v1.0 | **Phase-0 kill criterion fires** (walker + binding not working in ~1 day) → fall back to web_core at v0.9.1; or `@a2ui/web_core` publishes a `v1_0` runtime export, which retroactively makes v1.0 strictly free |
| Q6 permanent evidence | Ledger growth becomes a real cost — then archive cold evidence rows, never delete them |
| §12.0 Svelte | `flutter/genui` ships v1.0 Stable *and* a native requirement from Q2 lands at the same time |

---

## Appendix A — Worked example: pre-execution escalation on the wire

Real surface, real Nous scenario: a DAG node wants to force-push.

This is the **blocking** path, and it survives the move to advisory review precisely because force-push is irreversible — §10.4's boundary keeps it escalating *before* execution. Contrast it with Appendix A2, which is the advisory Action Review card that covers everything else.

```jsonl
{"version":"v1.0","createSurface":{"surfaceId":"nous:escalation:approval:0f9e21","catalogId":"https://nous.fatykhov.us/a2ui/v1.0/nous-core/catalog.json","sendDataModel":true,"metadata":{"extensions":{"com_nous_nonce":"7c1f...","com_nous_priority":2}},"dataModel":{"summary":"Branch feat/f091-companion has diverged from origin after an interactive rebase. Completing the merge requires a force-push to the remote branch.","risk":"Irreversible. Overwrites 3 remote commits. No other collaborators on this branch.","recommendation":"force_push","options":[{"id":"force_push","label":"Force-push (recommended)","variant":"primary"},{"id":"merge_commit","label":"Abandon rebase, merge instead","variant":"secondary"},{"id":"abort","label":"Stop and leave it to me","variant":"borderless"}]},"components":[{"id":"root","component":"Column","children":["panel","opts","defer"],"align":"stretch"},{"id":"panel","component":"ApprovalPanel","title":"Force-push required on feat/f091-companion","summary":{"path":"/summary"},"risk":{"path":"/risk"},"recommendation":{"path":"/recommendation"}},{"id":"opts","component":"List","children":{"path":"/options","componentId":"opt_btn"}},{"id":"opt_btn","component":"Button","child":"opt_label","variant":{"path":"variant"},"action":{"event":{"name":"approval.choose","context":{"optionId":{"path":"id"},"traceId":"b2e1...","surfaceNonce":"7c1f..."}}}},{"id":"opt_label","component":"Text","text":{"path":"label"}},{"id":"defer","component":"Button","child":"defer_label","variant":"borderless","action":{"event":{"name":"approval.defer","context":{"traceId":"b2e1..."}}}},{"id":"defer_label","component":"Text","text":"Ask me later"}]}}
```

User taps "Force-push (recommended)":

```json
{"version":"v1.0","action":{"name":"approval.choose","surfaceId":"nous:escalation:approval:0f9e21","sourceComponentId":"opt_btn","timestamp":"2026-08-29T14:22:08Z","context":{"optionId":"force_push","traceId":"b2e1...","surfaceNonce":"7c1f..."}}}
```

Server: verifies `approval.choose` ∈ `allowed_actions` → validates nonce → runs censors → dispatches handler → writes `system.a2ui_actions` + F032 ledger entry → resolves the blocked DAG node. Then paints the outcome and tears down:

```jsonl
{"version":"v1.0","updateDataModel":{"surfaceId":"nous:escalation:approval:0f9e21","path":"/summary","value":"Approved — force-push complete. DAG node `merge-companion` resumed."}}
{"version":"v1.0","updateComponents":{"surfaceId":"nous:escalation:approval:0f9e21","components":[{"id":"root","component":"Column","children":["panel"]}]}}
{"version":"v1.0","deleteSurface":{"surfaceId":"nous:escalation:approval:0f9e21"}}
```

Note the teardown pattern: reduce `root`'s children *before* deleting, so the surface visibly resolves rather than vanishing mid-interaction.

---

## Appendix A2 — Worked example: advisory Action Review

The common case (Q5). Nous has **already acted**; the card is a reviewable record with course-correction affordances. Note `compensation` in the data model — it is what decides whether a Revert button is rendered at all.

```jsonl
{"version":"v1.0","createSurface":{"surfaceId":"nous:review:action:3ad70c","catalogId":"https://nous.fatykhov.us/a2ui/v1.0/nous-core/catalog.json","sendDataModel":true,"metadata":{"extensions":{"com_nous_nonce":"9d4a...","com_nous_priority":1}},"dataModel":{"did":"Re-ran the premarket DAG compose node after it failed validation, then sent the digest.","why":"Fix node classified the failure as validation_failed; dispatcher rule maps that to retry_as_is. Retry succeeded on attempt 1.","cost":"~40s compute. One email sent to Tfatykhov@gmail.com.","compensation":{"revertible":false,"handler":null,"note":"Email already delivered — cannot be unsent. A correction can be sent instead."},"traceId":"c7f2..."},"components":[{"id":"root","component":"Column","children":["card","acts"],"align":"stretch"},{"id":"card","component":"ActionReviewCard","title":"Retried premarket compose and sent digest","did":{"path":"/did"},"why":{"path":"/why"},"cost":{"path":"/cost"},"compensation":{"path":"/compensation"}},{"id":"acts","component":"Row","children":["ack","correct","rule"],"justify":"spaceBetween"},{"id":"ack","component":"Button","child":"ack_l","variant":"primary","action":{"event":{"name":"review.acknowledge","context":{"traceId":"c7f2...","surfaceNonce":"9d4a..."}}}},{"id":"ack_l","component":"Text","text":"Fine"},{"id":"correct","component":"Button","child":"correct_l","variant":"secondary","action":{"event":{"name":"review.course_correct","context":{"traceId":"c7f2...","surfaceNonce":"9d4a..."}}}},{"id":"correct_l","component":"Text","text":"Wrong call — tell me why"},{"id":"rule","component":"Button","child":"rule_l","variant":"borderless","action":{"event":{"name":"review.make_rule","context":{"traceId":"c7f2...","surfaceNonce":"9d4a..."}}}},{"id":"rule_l","component":"Text","text":"Make this a standing rule"}]}}
```

No Revert button is rendered, because `compensation.revertible` is `false` and the note says why. Tapping **Wrong call** expands a `TextField` (`longText`) bound to `/correction`, and submitting it writes `resolve_decision(outcome="failure", resolution_note=…)` against `traceId` — so a disagreement becomes calibration data rather than a lost complaint in a chat log.

If nothing is tapped, the record archives as `no_objection` after `a2ui_review_archive_days`.

---

## Appendix B — Nous file layout

```
nous/a2ui/
├── __init__.py
├── dsl.py                 # typed builder DSL → schema-valid envelopes
├── service.py             # SurfaceService: create/update/resolve, dedup, outbox
├── transport.py           # SSE generator (reuses chat_stream keepalive)
├── actions.py             # handler registry, allowlist, censor integration
├── validator.py           # JSON Schema + structural checks
├── conversation.py        # channel-tagged turn ingest, Telegram↔companion echo
├── builders/
│   ├── action_review.py
│   ├── escalation.py
│   ├── decision_sweep.py
│   ├── heartbeat_findings.py
│   ├── memory_graph.py
│   └── dag_monitor.py
└── catalogs/
    ├── basic/catalog.json         # vendored upstream @ pinned commit
    ├── nous_core/catalog.json
    └── json/                      # common_types, agent_to_renderer, ...

nous/api/rest.py           # +9 routes, before the Mount
sql/migrations/0NN_a2ui.sql

dashboard-app/src/companion/   # renderer (see §12)
tests/test_a2ui_builders.py    # every builder → schema validation
tests/test_a2ui_actions.py     # allowlist, nonce, censor rejection
tests/test_a2ui_conformance.py # spec examples as fixtures
tests/test_a2ui_conversation.py # one-store invariant: Telegram turn visible in companion and vice versa
```
