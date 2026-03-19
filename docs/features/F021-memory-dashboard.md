# F021 — Nous Memory Dashboard

> **Status:** Planned
> **Priority:** P1
> **Depends on:** None (existing REST API covers ~70% of data needs)
> **Estimated effort:** ~16–20 hours across 4 PRs
> **Domain:** Served at `/dashboard` on the Nous server
> **Supersedes:** 010.1 (Health Dashboard), F024 draft

## Changelog

- v1 (2026-03-19): Initial spec (as F024)
- v2 (2026-03-19): Renumbered to F021 (allocated in INDEX.md). Addressed Emerson review:
  - P1-1: Renumbered F024 → F021, supersedes 010.1
  - P1-2: `/facts` browse mode — `q` now optional, returns paginated list when omitted
  - P1-3: Static file serving — explicit `Mount` + `StaticFiles` pattern added
  - P2-1: `/dashboard/stats` removed — extend `/status` with `?dashboard=true` query param
  - P2-2: Graph payload — truncated labels (120 chars), full content loaded on click via existing detail endpoints
  - P2-3: Added `idx_graph_edges_created` index on `brain.graph_edges(created_at)`
  - P2-4: Calibration — reuse `calibration_snapshots` table directly, no recomputation
  - P2-5: Standardized all pagination on `limit/offset` (matches existing endpoints)
  - P3-1: Hash routing (`#/overview`, `#/graph`, etc.)
  - P3-2: Error and empty states specified for all views
  - P3-3: Vendor libraries (Chart.js, D3) in `static/dashboard/lib/` instead of CDN

---

## Problem

Nous has 18 tables across 2 schemas, a graph-augmented recall system, decision intelligence with calibration tracking, censors, procedures, and a 5-phase sleep consolidation pipeline. All of this is invisible unless you run SQL queries or ask Nous to self-report.

**What's missing:**
- No way to see memory contents at a glance — facts, episodes, decisions, their relationships
- No visualization of the knowledge graph — the graph_edges that power spreading activation are invisible
- No trend tracking — are facts growing too fast? Is graph density healthy? Are censors firing too often?
- No way to inspect decision quality — calibration data exists but requires API calls to see
- No way to browse and search memory without going through the chat interface

**Who needs this:**
- **Tim (primary)** — understanding what Nous remembers, spotting problems, monitoring health
- **Future Nous users** — anyone running their own agent needs observability into memory
- **Development** — debugging recall issues, validating F023 admission control, tuning graph density

## Goal

A lightweight, read-only web dashboard served from the Nous server that provides:

1. **At-a-glance memory health** — counts, trends, graph density
2. **Memory browser** — search and explore facts, episodes, decisions, procedures, censors
3. **Graph visualization** — interactive knowledge graph showing nodes, edges, clusters
4. **Decision intelligence** — calibration metrics, confidence distributions, outcome tracking
5. **System activity** — sleep events, censor activations, admission control stats (future)

---

## Design Principles

1. **Read-only** — The dashboard observes, it never modifies. No edit/delete/create actions.
2. **Lightweight** — No build step, no npm, no React. Vanilla HTML/CSS/JS + vendored Chart.js + D3. Served as static files from the Nous server.
3. **API-first** — All data comes from REST endpoints. Dashboard is a pure frontend consumer. New endpoints are added to the existing `rest.py`.
4. **Dark theme** — Consistent with the existing landing page design system. Same CSS variables (`--bg`, `--surface`, `--accent`, etc.).
5. **Responsive** — Works on desktop (primary) and tablet. Mobile is not a priority.
6. **Agent-scoped** — All queries filter by `agent_id`. Multi-agent support is future work.
7. **Extend, don't duplicate** — Reuse existing endpoints where possible. Extend `/status` rather than creating parallel aggregation endpoints.

---

## Architecture

```
┌─────────────────────────────────────────┐
│              Browser (SPA)              │
│                                         │
│  index.html + app.js                    │
│  Chart.js (charts) + D3.js (graph)      │
│  Hash routing (#/overview, #/graph, …)  │
│  Vanilla CSS (extends landing design)   │
└──────────────┬──────────────────────────┘
               │ fetch() → JSON
               ▼
┌─────────────────────────────────────────┐
│          Nous REST API (rest.py)        │
│                                         │
│  Extended (this feature):               │
│    GET /status?dashboard=true (+ trends)│
│    GET /facts?limit=50&offset=0 (browse)│
│    GET /episodes  (+ filters)           │
│    GET /decisions (+ filters)           │
│    GET /censors   (+ filters)           │
│                                         │
│  New (this feature):                    │
│    GET /dashboard/graph    (edge data)  │
│    GET /dashboard/calibration (charts)  │
│    GET /dashboard/activity (events)     │
│    GET /dashboard/health   (trends)     │
│                                         │
│  Static serving:                        │
│    Mount("/dashboard", StaticFiles(...))│
└──────────────┬──────────────────────────┘
               │ SQLAlchemy
               ▼
┌─────────────────────────────────────────┐
│        PostgreSQL + pgvector            │
│        brain (8) │ heart (10)           │
└─────────────────────────────────────────┘
```

### Static File Serving

```python
# In rest.py — route registration
from starlette.staticfiles import StaticFiles
from starlette.routing import Mount, Route

routes = [
    # ... existing API routes ...
    Route("/status", status),
    Route("/facts", search_facts),
    # ...
    
    # Dashboard static files — MUST be last (catch-all)
    Mount("/dashboard", app=StaticFiles(
        directory="static/dashboard",
        html=True  # serves index.html for /dashboard
    )),
]
```

**Note:** `html=True` means `/dashboard` serves `static/dashboard/index.html` automatically. Hash routing (`#/overview`, `#/graph`) is handled client-side — all routes resolve to the same `index.html`.

### Tech Stack

- **Frontend:** Vanilla HTML/CSS/JS — no build step, no dependencies to manage
- **Charts:** Chart.js (~60KB gzipped) — vendored in `static/dashboard/lib/chart.min.js`
- **Graph viz:** D3.js force-directed (~75KB gzipped) — vendored in `static/dashboard/lib/d3.min.js`
- **Styling:** CSS extending existing design vars
- **Routing:** Hash-based (`window.location.hash`) — no server-side routing needed
- **Data:** REST API → JSON — extend existing endpoints + 4 new ones

### Why Not React/Vue/Svelte?

The dashboard is read-only with ~6 views. A framework would add:
- Build toolchain (node, npm, webpack/vite)
- Package management overhead
- Deployment complexity

Vanilla JS with Chart.js and D3 handles this scope perfectly. If the dashboard grows significantly (editing, real-time updates, complex state), we can migrate later.

---

## Dashboard Views

### 1. Overview (Landing View) — `#/overview`

**Purpose:** At-a-glance health check. "Is my agent's memory healthy?"

**Layout:** Grid of stat cards + mini charts

**Stat Cards (top row):**

- **Total Facts** — `heart.facts WHERE active=true` — Count + trend arrow (vs 7 days ago)
- **Total Episodes** — `heart.episodes` — Count + trend arrow
- **Total Decisions** — `brain.decisions` — Count + trend arrow
- **Active Censors** — `heart.censors WHERE active=true` — Count
- **Graph Density** — `compute_graph_density()` — Float + health indicator (🟢 >3.0, 🟡 1.0-3.0, 🔴 <1.0)
- **Calibration (Brier)** — `brain.calibration_snapshots` — Score + trend arrow (lower = better)
- **Procedures** — `heart.procedures WHERE active=true` — Count
- **Active Schedules** — `heart.schedules WHERE active=true` — Count

**Mini Charts (bottom row):**
- **Memory Growth** — Line chart: facts + episodes + decisions over last 30 days (by `created_at` date bucketed)
- **Fact Categories** — Doughnut chart: breakdown by category (preference, technical, person, tool, concept, rule)
- **Decision Outcomes** — Doughnut chart: pending vs success vs partial vs failure
- **Edge Types** — Bar chart: graph_edges grouped by relation type

**Empty State:** "No data yet. Start a conversation with Nous to generate memories."

**API:** `GET /status?dashboard=true` — extends existing `/status` response with:
```json
{
  // ... existing /status fields ...
  "dashboard": {
    "deltas_7d": {
      "facts": 18,
      "episodes": 12,
      "decisions": 8
    },
    "distributions": {
      "fact_categories": { "preference": 45, "technical": 82, "person": 31, "tool": 22, "concept": 48, "rule": 19 },
      "decision_outcomes": { "pending": 175, "success": 130, "partial": 18, "failure": 10 },
      "decision_categories": { "architecture": 45, "process": 120, "tooling": 88, "security": 12, "integration": 68 },
      "edge_relations": { "related_to": 180, "extracted_from": 120, "supports": 85, "informed_by": 60, "evidence_for": 35, "contradicts": 12, "supersedes": 10, "caused_by": 8, "discussed_in": 2 }
    },
    "timeseries": {
      "labels": ["2026-02-18", "2026-02-19", "..."],
      "facts": [2, 5, 3, "..."],
      "episodes": [1, 2, 1, "..."],
      "decisions": [3, 1, 4, "..."]
    },
    "graph_density": 3.7
  }
}
```

When `?dashboard=true` is NOT present, the response is unchanged (backward compatible).

---

### 2. Knowledge Graph — `#/graph`

**Purpose:** Visualize the graph_edges network. See how memories connect. Identify clusters and orphans.

**Layout:** Full-width D3 force-directed graph with sidebar controls

**Graph Features:**
- **Nodes** — Colored by type: facts (blue), episodes (green), decisions (purple), procedures (orange)
- **Node size** — Proportional to edge count (more connected = bigger)
- **Node labels** — Truncated to 120 chars. Full content loaded on click via existing detail endpoints (`/decisions/{id}`, etc.)
- **Edges** — Colored by relation type. Line style: solid for strong weight (>0.7), dashed for weak (<0.3)
- **Edge labels** — Show relation type on hover
- **Interactive:**
  - Click node → sidebar shows full content (fetched via existing `/decisions/{id}`, `/facts?q=...`, etc.)
  - Drag nodes to rearrange
  - Zoom/pan
  - Search box → highlights matching nodes (searches label text client-side)
  - Filter by node type (toggle facts/episodes/decisions/procedures)
  - Filter by relation type
  - Filter by date range
  - Minimum edge count slider (hide low-connectivity nodes)
- **Cluster detection** — Visually group tightly-connected nodes. Orphan nodes (0 edges) shown in a separate "disconnected" area
- **Stats overlay:**
  - Total nodes / edges
  - Avg edges per node (density)
  - Largest cluster size
  - Orphan count

**Empty State:** "No graph edges yet. As Nous learns facts and makes decisions, connections will appear here."

**Error State:** "Failed to load graph data. Retry?" with retry button.

**API Endpoint:** `GET /dashboard/graph?limit=500&types=fact,episode,decision&min_edges=0`
```json
{
  "nodes": [
    {
      "id": "uuid-1",
      "type": "fact",
      "label": "Tim lives in Silver Spring, Mar…",
      "category": "person",
      "edge_count": 5,
      "created_at": "2026-03-01T10:00:00Z"
    }
  ],
  "edges": [
    {
      "source": "uuid-1",
      "target": "uuid-2",
      "relation": "extracted_from",
      "weight": 1.0,
      "auto_linked": true,
      "created_at": "2026-03-01T10:00:00Z"
    }
  ],
  "stats": {
    "total_nodes": 423,
    "total_edges": 512,
    "displayed_nodes": 500,
    "density": 3.7,
    "largest_cluster": 45,
    "orphan_count": 23
  }
}
```

**Performance:** Default limit 500 nodes (most connected first). Labels truncated to 120 chars server-side. Full content loaded on-demand via existing detail endpoints. For graphs >1000 nodes, server-side filtering by type, date range, and minimum edge count.

---

### 3. Memory Browser — `#/browser`

**Purpose:** Search, browse, and inspect individual memory items across all types.

**Layout:** Tabbed interface — Facts | Episodes | Decisions | Procedures | Censors

**Shared Pagination:** All tabs use `limit/offset` pagination (consistent with existing API pattern). Default: `limit=50, offset=0`. Pagination controls: Previous / Next / page indicator.

#### 3a. Facts Tab
- **Search bar** — Full-text search (uses existing `search_tsv` GIN index). **Optional** — when empty, returns all facts paginated (browse mode).
- **Filters:** Category, active/inactive, confidence range, date range, has subject
- **Table view:**
  - Content (truncated), Category, Subject, Confidence, Source, Learned At, Active
  - Click row → expands to show: full content, tags, source episode link, source decision link, graph edges
- **Sort:** By date (default), confidence, category, subject
- **Empty State:** "No facts stored yet."

**API Change:** `GET /facts` — `q` parameter becomes **optional**:
```python
# Current: q is required, returns 400 if missing
# New: q is optional
#   - If q provided: semantic search (existing behavior)
#   - If q omitted: return paginated list sorted by created_at desc

GET /facts?limit=50&offset=0&sort=created_at&order=desc
GET /facts?q=flight&limit=50&offset=0
GET /facts?category=technical&active=true
GET /facts?confidence_min=0.5&date_from=2026-03-01
```

Response includes total count for pagination:
```json
{
  "facts": [...],
  "total": 247,
  "limit": 50,
  "offset": 0
}
```

#### 3b. Episodes Tab
- **Search bar** — Full-text search
- **Filters:** Outcome, frame used, date range, has structured summary
- **Table view:**
  - Title, Summary (truncated), Outcome, Frame, Duration, Started At
  - Click row → expands to show: full summary, structured summary (key points, outcome rationale), lessons learned, linked decisions
- **Sort:** By date (default), duration, outcome
- **Empty State:** "No episodes recorded yet."

**API Change:** `GET /episodes` — add filters:
```
GET /episodes?limit=50&offset=0&sort=created_at&order=desc
GET /episodes?outcome=success&frame=task
GET /episodes?date_from=2026-03-01&date_to=2026-03-19
```

#### 3c. Decisions Tab
- **Search bar** — Full-text search
- **Filters:** Category, stakes, outcome, confidence range, date range, reviewed/unreviewed
- **Table view:**
  - Description (truncated), Category, Stakes, Confidence, Outcome, Created At
  - Click row → expands to show: full description, context, pattern, reasons (typed), tags, review status, graph edges
- **Sort:** By date (default), confidence, stakes, category
- **Empty State:** "No decisions recorded yet."

**API Change:** `GET /decisions` — add filters:
```
GET /decisions?limit=50&offset=0&sort=created_at&order=desc
GET /decisions?category=architecture&stakes=high
GET /decisions?outcome=success&reviewed=true
GET /decisions?confidence_min=0.7&date_from=2026-03-01
```

#### 3d. Procedures Tab
- **Search bar** — Full-text search
- **Filters:** Domain, active/inactive, min activation count
- **Table view:**
  - Name, Domain, Activation Count, Success Rate, Last Activated
  - Click row → expands to show: description, goals, core patterns, core tools, implementation notes
- **Sort:** By date, activation count, success rate
- **Empty State:** "No procedures learned yet."

#### 3e. Censors Tab
- **Filters:** Action (warn/block/absolute), active/inactive, domain, created_by (manual/auto)
- **Table view:**
  - Trigger Pattern, Action, Reason, Domain, Activation Count, False Positives, Created By
  - Click row → expands to show: full reason, source decision, source episode, last activated
- **Sort:** By activation count, date, action severity
- **Empty State:** "No censors configured yet."

**API Change:** `GET /censors` — add filters:
```
GET /censors?limit=50&offset=0
GET /censors?action=block&active=true
```

---

### 4. Decision Intelligence — `#/decisions`

**Purpose:** Understand decision quality, calibration, reasoning patterns.

**Layout:** Metrics cards + charts

**Charts:**
- **Calibration Curve** — X: predicted confidence (bucketed 0.0-0.1, 0.1-0.2, ...), Y: actual success rate. Perfect calibration = diagonal.
- **Confidence Distribution** — Histogram of confidence scores across all decisions.
- **Outcome by Category** — Stacked bar chart: for each category, show success/partial/failure/pending proportions.
- **Outcome by Stakes** — Same, grouped by stakes level.
- **Reason Type Usage** — Bar chart: how often each reason type is used.
- **Reason Type × Outcome** — Heatmap: which reason types correlate with success vs failure.
- **Brier Score Over Time** — Line chart from `brain.calibration_snapshots` (reuse existing aggregated data, no recomputation).
- **Decisions Per Day** — Bar chart, last 30 days.

**Empty State:** "No decisions recorded yet. Decisions are created when Nous makes significant choices."

**API Endpoint:** `GET /dashboard/calibration`
```json
{
  "calibration_curve": [
    { "bucket": "0.0-0.2", "predicted_avg": 0.15, "actual_success_rate": 0.10, "count": 5 }
  ],
  "confidence_histogram": [
    { "range": "0.0-0.1", "count": 2 }
  ],
  "outcome_by_category": {
    "architecture": { "success": 30, "partial": 5, "failure": 2, "pending": 8 }
  },
  "outcome_by_stakes": {
    "low": { "success": 80, "partial": 10, "failure": 3, "pending": 20 }
  },
  "reason_type_stats": {
    "analysis": { "count": 120, "success_rate": 0.85 },
    "pattern": { "count": 45, "success_rate": 0.78 }
  },
  "brier_history": [],
  "daily_decisions": [
    { "date": "2026-03-01", "count": 5 }
  ]
}
```

**Note on Brier history:** `brier_history` is populated directly from `brain.calibration_snapshots` table — no recomputation needed. The existing `calibration` endpoint already queries this; the dashboard endpoint simply returns it in a chart-friendly format.

---

### 5. System Activity — `#/activity`

**Purpose:** Monitor operational health — sleep events, censor activations, schedule activity.

**Layout:** Timeline + stat cards

**Components:**
- **Activity Timeline** — Reverse-chronological feed of system events:
  - Sleep phases completed
  - Censor activations (with trigger details)
  - Auto-censor creations
  - Censor escalations
  - Schedule fires
  - Subtask completions/failures
  - Each event: icon + type + timestamp + summary
  - Filter by event type

- **Censor Activity Cards:**
  - Most active censors (by activation_count)
  - Recent false positives
  - Auto-created vs manual censors ratio
  - Escalation events

- **Schedule Status:**
  - Active schedules list with next fire time
  - Recent schedule fires with results
  - Fire count trends

- **Sleep Activity:**
  - Last sleep timestamp
  - Facts created during sleep reflection
  - Procedures created during generalization
  - Censors pruned / retired

**Empty State:** "No system activity yet. Activity appears as Nous processes conversations, fires schedules, and runs sleep cycles."

**API Endpoint:** `GET /dashboard/activity?hours=168` (default: last 7 days)
```json
{
  "events": [
    {
      "type": "censor_activated",
      "data": { "censor_id": "uuid", "trigger": "api.key", "action": "block" },
      "created_at": "2026-03-19T15:30:00Z"
    }
  ],
  "censor_stats": {
    "total_activations_7d": 23,
    "top_censors": [
      { "id": "uuid", "trigger_pattern": "api.key", "activations": 8 }
    ],
    "auto_created": 3,
    "manual_created": 11,
    "false_positives_7d": 1
  },
  "schedule_stats": {
    "active": 6,
    "fires_7d": 12,
    "next_fires": [
      { "id": "uuid", "task": "Dev.to stats check", "next_fire_at": "2026-03-23T10:00:00Z" }
    ]
  },
  "sleep_stats": {
    "last_sleep": "2026-03-19T04:00:00Z",
    "facts_created": 3,
    "procedures_created": 1,
    "censors_retired": 0
  }
}
```

---

### 6. Graph Health — `#/health`

**Purpose:** Track graph-specific metrics over time to validate F022 and future F023.

**Charts:**
- **Graph Density Over Time** — Line chart. Target: >3.0 for spreading activation.
- **Edge Creation Rate** — Bar chart, daily. Spike detection.
- **Edge Type Distribution Over Time** — Stacked area chart. Shows if one type dominates.
- **Node Degree Distribution** — Histogram. Power-law distribution is healthy.
- **Orphan Nodes Over Time** — Line chart. Rising = FactGraphLinker not keeping up.
- **Auto-linked vs Manual Edges** — Ratio over time.

**Empty State:** "No graph data yet. Graph edges are created as Nous links facts, episodes, and decisions."

**Required Index (new):**
```sql
CREATE INDEX idx_graph_edges_created ON brain.graph_edges(created_at);
```

This index supports time-series queries for edge creation trends without full table scans.

**API Endpoint:** `GET /dashboard/health?days=30`
```json
{
  "density_history": [
    { "date": "2026-03-01", "density": 2.1 },
    { "date": "2026-03-19", "density": 3.7 }
  ],
  "daily_edges": [
    { "date": "2026-03-19", "count": 12, "auto": 10, "manual": 2 }
  ],
  "degree_distribution": [
    { "degree": 0, "count": 23 },
    { "degree": 1, "count": 45 }
  ],
  "orphan_trend": [
    { "date": "2026-03-01", "count": 40 },
    { "date": "2026-03-19", "count": 23 }
  ]
}
```

---

## New REST API Endpoints

| Endpoint | Method | Purpose | Complexity |
|----------|--------|---------|------------|
| `GET /dashboard/graph` | GET | Nodes + edges for D3 visualization | Medium |
| `GET /dashboard/calibration` | GET | Decision intelligence analytics | Medium |
| `GET /dashboard/activity` | GET | System events timeline | Low |
| `GET /dashboard/health` | GET | Graph health trends | Medium |

**Extended existing endpoints:**

| Endpoint | Changes |
|----------|---------|
| `GET /status` | Add `?dashboard=true` for trends, distributions, timeseries |
| `GET /facts` | Make `q` optional (browse mode), add category/confidence/date filters, add `total` to response |
| `GET /episodes` | Add offset, outcome/frame/date filters, add `total` to response |
| `GET /decisions` | Add category/stakes/outcome/confidence/date filters, add `total` to response |
| `GET /censors` | Add offset, action/active filters, add `total` to response |

**Pagination standard:** All list endpoints use `limit` + `offset` (existing pattern). Response always includes `total` count for client-side pagination controls.

---

## File Structure

```
nous/
├── api/
│   ├── rest.py                  (extend: static mount, endpoint changes)
│   └── dashboard_queries.py     (new: SQL queries for dashboard aggregations)
├── sql/
│   └── init.sql                 (add: idx_graph_edges_created index)
└── ...

static/
└── dashboard/
    ├── index.html               (SPA shell — nav + view containers)
    ├── css/
    │   └── dashboard.css        (dark theme extending existing design)
    ├── js/
    │   ├── app.js               (hash routing, API client, loading/error states)
    │   ├── overview.js          (stat cards + mini charts)
    │   ├── graph.js             (D3 force-directed graph)
    │   ├── browser.js           (tabbed memory browser)
    │   ├── decisions.js         (decision intelligence charts)
    │   ├── activity.js          (system activity timeline)
    │   └── health.js            (graph health charts)
    └── lib/
        ├── chart.min.js         (vendored Chart.js)
        └── d3.min.js            (vendored D3.js)
```

**Note:** File paths are `nous/api/rest.py` (not `nous/nous/api/rest.py`). Verified against codebase.

---

## Navigation

Hash-based routing. Sidebar (collapsible):

```
🧠 Nous Dashboard
─────────────────
📊 Overview          #/overview (default)
🕸️ Knowledge Graph   #/graph
📚 Memory Browser    #/browser
🎯 Decisions         #/decisions
⚡ Activity          #/activity
📈 Graph Health      #/health
```

`app.js` listens to `hashchange` events:
```javascript
window.addEventListener('hashchange', () => {
    const view = location.hash.slice(2) || 'overview';
    loadView(view);
});
```

---

## Error & Empty States

Every view handles three states:

1. **Loading** — Skeleton cards/placeholders with pulse animation. No spinners.
2. **Empty** — Friendly message explaining what generates this data + icon. Per-view messages defined above.
3. **Error** — Red banner: "Failed to load [view name]. [error detail]" + Retry button. Retry calls the same fetch with exponential backoff (max 3 retries).

API client wrapper:
```javascript
async function apiGet(path, retries = 3) {
    for (let i = 0; i < retries; i++) {
        try {
            const res = await fetch(path);
            if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
            return await res.json();
        } catch (err) {
            if (i === retries - 1) throw err;
            await new Promise(r => setTimeout(r, 1000 * Math.pow(2, i)));
        }
    }
}
```

---

## Design System

Extends the existing design system:

```css
:root {
  --bg: #0a0a0f;
  --surface: #111118;
  --border: #1e1e2e;
  --accent: #7c6af7;
  --accent-dim: #4f46a8;
  --accent-glow: rgba(124, 106, 247, 0.15);
  --text: #e2e2f0;
  --muted: #6b6b8a;
  --green: #34d399;
  --red: #f87171;
  
  /* Node type colors */
  --fact-color: #60a5fa;      /* blue */
  --episode-color: #34d399;   /* green */
  --decision-color: #a78bfa;  /* purple */
  --procedure-color: #fb923c; /* orange */
  --censor-color: #f87171;    /* red */
}
```

**Card component:**
```css
.stat-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 1.5rem;
}
.stat-card:hover {
  border-color: var(--accent-dim);
  box-shadow: 0 0 20px var(--accent-glow);
}
```

**Typography:**
- Headers: Inter, 700 weight
- Body: Inter, 400 weight
- Monospace (IDs, code): JetBrains Mono or system monospace

---

## Implementation Plan

### PR 1 — API Layer (~5 hours)
**New file:** `nous/api/dashboard_queries.py`
- All 4 new endpoint query functions
- Pagination helper (shared across existing endpoints)
- Date bucketing utility

**Modified file:** `nous/api/rest.py`
- 4 new dashboard endpoints
- Extend `/status` with `?dashboard=true` support
- Make `/facts` `q` parameter optional (browse mode)
- Add filters + `total` count to `/facts`, `/episodes`, `/decisions`, `/censors`
- Add `Mount("/dashboard", StaticFiles(directory="static/dashboard", html=True))`
- Static mount MUST be last in route list (catch-all)

**Modified file:** `nous/sql/init.sql`
- Add `CREATE INDEX idx_graph_edges_created ON brain.graph_edges(created_at);`

**Tests:**
- Unit tests for each query function
- Integration tests for each endpoint
- Pagination edge cases
- Browse mode for `/facts` (no `q` param)
- `?dashboard=true` backward compatibility

### PR 2 — Dashboard Shell + Overview (~4 hours)
- `index.html` — SPA shell with navigation sidebar
- `dashboard.css` — dark theme extending existing variables
- `app.js` — hash routing, API client wrapper, loading/error/empty states
- `overview.js` — stat cards, mini charts (Chart.js)
- Vendored `lib/chart.min.js` and `lib/d3.min.js`
- Responsive layout (desktop primary, tablet secondary)

### PR 3 — Knowledge Graph + Memory Browser (~6 hours)
- `graph.js` — D3 force-directed graph with all interactive features
- `browser.js` — tabbed memory browser with search, filters, expandable rows
- Node labels truncated to 120 chars, full content loaded on click
- This is the most complex PR — graph rendering and interaction handling

### PR 4 — Decision Intelligence + System Activity + Graph Health (~5 hours)
- `decisions.js` — calibration curve, histograms, heatmaps
- `activity.js` — event timeline, censor stats, schedule status
- `health.js` — graph health trend charts
- Final polish, cross-view navigation (click fact in browser → highlight in graph)

---

## Performance Considerations

1. **Graph rendering** — Default limit 500 nodes. Server-side filtering. Progressive loading for larger graphs. Labels truncated server-side to 120 chars.
2. **Dashboard queries** — All aggregate queries use existing indexes. `idx_graph_edges_created` added for time-series. Time-bucketed queries may need materialized view if slow (>500ms).
3. **Caching** — Overview stats cached 60 seconds (stat cards don't need real-time). Graph data cached 5 minutes.
4. **Bundle size** — Chart.js (~60KB gzip) + D3 (~75KB gzip) + app code (~30KB). Total: ~165KB. Vendored locally.
5. **On-demand detail loading** — Graph nodes and browser rows load full content on click via existing detail endpoints, not in the initial payload.

---

## Database Changes

**New index only** — no new tables:

```sql
-- Support time-series queries on graph edges
CREATE INDEX IF NOT EXISTS idx_graph_edges_created 
ON brain.graph_edges(created_at);
```

---

## Future Enhancements (Not in Scope)

1. **Real-time updates** — WebSocket push for live event stream
2. **Memory editing** — Edit/delete facts, retire censors from dashboard (breaks read-only principle, needs auth)
3. **Multi-agent view** — Compare memory across agents
4. **Embedding space visualization** — t-SNE/UMAP projection of all embeddings in 2D
5. **Admission control dashboard** — F023-specific: admission scores, rejection rates, score distributions
6. **Export** — CSV/JSON export of filtered memory contents
7. **Diff view** — Compare two time snapshots of memory state
8. **Semantic search from dashboard** — Needs embedding generation server-side proxy

---

## Success Criteria

1. **Page load < 2 seconds** — Including API calls and chart rendering
2. **Graph renders 500 nodes smoothly** — >30fps pan/zoom on modern browser
3. **All 6 views functional** — Overview, Graph, Browser, Decisions, Activity, Health
4. **Zero build step** — `git clone` + `python -m nous` serves the dashboard. No npm install.
5. **All data from existing schema** — One new index, no new tables
6. **Backward compatible** — Existing API responses unchanged unless `?dashboard=true` is passed
7. **Works with current REST API auth** — Same agent_id scoping

---

## Open Questions

1. **Auth** — Dashboard currently inherits Nous server auth (none for local, API key for hosted). Should we add basic auth for dashboard specifically?
2. **Embedding visualization** — Worth adding t-SNE/UMAP as a future view? Would need server-side dimensionality reduction.
3. **Mobile** — Is tablet enough, or do we need phone support?
4. **Graph layout algorithm** — Force-directed is intuitive but can be messy for large graphs. Consider hierarchical layout for specific views.
5. **Integration with F023** — When admission control ships, add a dedicated "Admission" tab showing scores, rejections, threshold tuning.

---

## Research Backing

- **Minsky Ch. 6 (Insight & Introspection):** "B-Brains observe A-Brains." The dashboard IS a B-brain for humans — observing the agent's internal state to detect dysfunction.
- **Minsky Ch. 15 (Consciousness & Memory):** "Self-knowledge is a narrative." The dashboard provides a different narrative than what the agent reports about itself — raw data vs. self-report.
- **Minsky Ch. 9 (Summaries):** The overview view is a summary of the agent's state. The graph view is the structural detail. Both are needed.

---

## References

- **Existing schema:** `nous/sql/init.sql` (18 tables, 2 schemas)
- **Existing REST API:** `nous/api/rest.py` (23 endpoints)
- **Graph density:** `nous/brain/spreading_activation.py` (`compute_graph_density()`)
- **Calibration snapshots:** `nous/storage/models.py` (`CalibrationSnapshot`)
- **F022:** Graph-augmented recall (the data this dashboard visualizes)
- **F023:** Memory admission control (future dashboard integration)
- **010.1:** Health Dashboard (superseded by this spec)
