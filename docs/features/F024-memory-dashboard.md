# F024 — Nous Memory Dashboard

> **Status:** Planned
> **Priority:** P2
> **Depends on:** None (existing REST API covers ~70% of data needs)
> **Estimated effort:** ~16–20 hours across 4 PRs
> **Domain:** Served at `/dashboard` on the Nous server

## Changelog

- v1 (2026-03-19): Initial spec

---

## Problem

Nous has 23 tables across 3 schemas, a graph-augmented recall system, decision intelligence with calibration tracking, censors, procedures, and a 5-phase sleep consolidation pipeline. All of this is invisible unless you run SQL queries or ask Nous to self-report.

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
2. **Lightweight** — No build step, no npm, no React. Vanilla HTML/CSS/JS + a charting library. Served as static files from the Nous server.
3. **API-first** — All data comes from REST endpoints. Dashboard is a pure frontend consumer. New endpoints are added to the existing `rest.py`.
4. **Dark theme** — Consistent with the existing landing page (`landing.html`) design system. Same CSS variables (`--bg`, `--surface`, `--accent`, etc.).
5. **Responsive** — Works on desktop (primary) and tablet. Mobile is not a priority.
6. **Agent-scoped** — All queries filter by `agent_id`. Multi-agent support is future work.

---

## Architecture

```
┌─────────────────────────────────────────┐
│              Browser (SPA)              │
│                                         │
│  dashboard.html + dashboard.js          │
│  Chart.js (charts) + D3.js (graph)      │
│  Vanilla CSS (extends landing.html)     │
└──────────────┬──────────────────────────┘
               │ fetch() → JSON
               ▼
┌─────────────────────────────────────────┐
│          Nous REST API (rest.py)        │
│                                         │
│  Existing:                              │
│    GET /status         (overview stats) │
│    GET /decisions      (list/search)    │
│    GET /decisions/:id  (detail)         │
│    GET /episodes       (list/search)    │
│    GET /facts          (search)         │
│    GET /censors        (list)           │
│    GET /frames         (list)           │
│    GET /calibration    (metrics)        │
│    GET /identity       (agent identity) │
│                                         │
│  New (this feature):                    │
│    GET /dashboard/stats    (aggregates) │
│    GET /dashboard/graph    (edge data)  │
│    GET /dashboard/timeline (time series)│
│    GET /dashboard/health   (trends)     │
└──────────────┬──────────────────────────┘
               │ SQLAlchemy
               ▼
┌─────────────────────────────────────────┐
│        PostgreSQL + pgvector            │
│  nous_system (5) │ brain (8) │ heart(10)│
└─────────────────────────────────────────┘
```

### Tech Stack

| Component | Choice | Rationale |
|-----------|--------|-----------|
| **Frontend** | Vanilla HTML/CSS/JS | No build step, no dependencies to manage, consistent with landing.html |
| **Charts** | Chart.js (CDN) | Lightweight (~60KB gzipped), covers bar/line/doughnut/radar. No npm needed. |
| **Graph viz** | D3.js force-directed (CDN) | Industry standard for network graphs. Interactive zoom/pan/drag. |
| **Styling** | CSS extending landing.html vars | Dark theme already defined, just extend it |
| **Data** | REST API → JSON | Already have most endpoints, add 4 new ones |

### Why Not React/Vue/Svelte?

The dashboard is read-only with ~6 views. A framework would add:
- Build toolchain (node, npm, webpack/vite)
- Package management overhead
- Deployment complexity

Vanilla JS with Chart.js and D3 handles this scope perfectly. If the dashboard grows significantly (editing, real-time updates, complex state), we can migrate later.

---

## Dashboard Views

### 1. Overview (Landing View)

**Purpose:** At-a-glance health check. "Is my agent's memory healthy?"

**Layout:** Grid of stat cards + mini charts

**Stat Cards (top row):**
| Metric | Source | Display |
|--------|--------|---------|
| Total Facts | `heart.facts WHERE active=true` | Count + trend arrow (vs 7 days ago) |
| Total Episodes | `heart.episodes` | Count + trend arrow |
| Total Decisions | `brain.decisions` | Count + trend arrow |
| Active Censors | `heart.censors WHERE active=true` | Count |
| Graph Density | `compute_graph_density()` | Float + health indicator (🟢 >3.0, 🟡 1.0-3.0, 🔴 <1.0) |
| Calibration (Brier) | `brain.calibration_snapshots` | Score + trend arrow (lower = better) |
| Procedures | `heart.procedures WHERE active=true` | Count |
| Active Schedules | `heart.schedules WHERE active=true` | Count |

**Mini Charts (bottom row):**
- **Memory Growth** — Line chart: facts + episodes + decisions over last 30 days (by `created_at` date bucketed)
- **Fact Categories** — Doughnut chart: breakdown by category (preference, technical, person, tool, concept, rule)
- **Decision Outcomes** — Doughnut chart: pending vs success vs partial vs failure
- **Edge Types** — Bar chart: graph_edges grouped by relation type

**API Endpoint:** `GET /dashboard/stats`
```json
{
  "counts": {
    "facts": { "total": 247, "active": 231, "delta_7d": 18 },
    "episodes": { "total": 89, "delta_7d": 12 },
    "decisions": { "total": 333, "delta_7d": 8 },
    "censors": { "active": 14, "total": 16 },
    "procedures": { "active": 8, "total": 10 },
    "schedules": { "active": 6 },
    "graph_edges": { "total": 512 }
  },
  "graph_density": 3.7,
  "calibration": {
    "brier_score": 0.019,
    "accuracy": 0.82,
    "total_decisions": 333,
    "reviewed_decisions": 158
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
  }
}
```

---

### 2. Knowledge Graph

**Purpose:** Visualize the graph_edges network. See how memories connect. Identify clusters and orphans.

**Layout:** Full-width D3 force-directed graph with sidebar controls

**Graph Features:**
- **Nodes** — Colored by type: facts (blue), episodes (green), decisions (purple), procedures (orange)
- **Node size** — Proportional to edge count (more connected = bigger)
- **Edges** — Colored by relation type. Line style: solid for strong weight (>0.7), dashed for weak (<0.3)
- **Edge labels** — Show relation type on hover
- **Interactive:**
  - Click node → sidebar shows full content (fact text, episode summary, decision description)
  - Drag nodes to rearrange
  - Zoom/pan
  - Search box → highlights matching nodes (searches content text)
  - Filter by node type (toggle facts/episodes/decisions/procedures)
  - Filter by relation type
  - Filter by date range
- **Cluster detection** — Visually group tightly-connected nodes. Orphan nodes (0 edges) shown in a separate "disconnected" area
- **Stats overlay:**
  - Total nodes / edges
  - Avg edges per node (density)
  - Largest cluster size
  - Orphan count

**API Endpoint:** `GET /dashboard/graph?limit=500&types=fact,episode,decision`
```json
{
  "nodes": [
    {
      "id": "uuid-1",
      "type": "fact",
      "label": "Tim lives in Silver Spring, Maryland",
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

**Performance consideration:** Large graphs (>1000 nodes) will be slow in D3. Default limit is 500 nodes (most connected first). User can adjust. Server-side filtering by type, date range, and minimum edge count.

---

### 3. Memory Browser

**Purpose:** Search, browse, and inspect individual memory items across all types.

**Layout:** Tabbed interface — Facts | Episodes | Decisions | Procedures | Censors

#### 3a. Facts Tab
- **Search bar** — Full-text search (uses existing `search_tsv` GIN index)
- **Filters:** Category, active/inactive, confidence range, date range, has subject
- **Table view:**
  - Content (truncated), Category, Subject, Confidence, Source, Learned At, Active
  - Click row → expands to show: full content, tags, source episode link, source decision link, graph edges
- **Sort:** By date (default), confidence, category, subject

#### 3b. Episodes Tab
- **Search bar** — Full-text search
- **Filters:** Outcome, frame used, date range, has structured summary
- **Table view:**
  - Title, Summary (truncated), Outcome, Frame, Duration, Started At
  - Click row → expands to show: full summary, structured summary (key points, outcome rationale), lessons learned, linked decisions, linked procedures
- **Sort:** By date (default), duration, outcome

#### 3c. Decisions Tab
- **Search bar** — Full-text search
- **Filters:** Category, stakes, outcome, confidence range, date range, reviewed/unreviewed
- **Table view:**
  - Description (truncated), Category, Stakes, Confidence, Outcome, Created At
  - Click row → expands to show: full description, context, pattern, reasons (typed), tags, bridge definition (structure/function), review status, graph edges
- **Sort:** By date (default), confidence, stakes, category

#### 3d. Procedures Tab
- **Search bar** — Full-text search
- **Filters:** Domain, active/inactive, min activation count
- **Table view:**
  - Name, Domain, Activation Count, Success Rate, Last Activated
  - Click row → expands to show: description, goals, core patterns, core tools, implementation notes, censor links
- **Sort:** By date, activation count, success rate

#### 3e. Censors Tab
- **Filters:** Action (warn/block/absolute), active/inactive, domain, created_by (manual/auto)
- **Table view:**
  - Trigger Pattern, Action, Reason, Domain, Activation Count, False Positives, Created By
  - Click row → expands to show: full reason, source decision, source episode, last activated
- **Sort:** By activation count, date, action severity

**API:** Uses existing endpoints (`/facts`, `/episodes`, `/decisions`, `/censors`) with added pagination and filter parameters.

**New parameters needed on existing endpoints:**
```
?page=1&per_page=50&sort=created_at&order=desc
&category=technical&active=true
&confidence_min=0.5&confidence_max=1.0
&date_from=2026-03-01&date_to=2026-03-19
```

---

### 4. Decision Intelligence

**Purpose:** Understand decision quality, calibration, reasoning patterns.

**Layout:** Metrics cards + charts

**Charts:**
- **Calibration Curve** — X: predicted confidence (bucketed 0.0-0.1, 0.1-0.2, ...), Y: actual success rate. Perfect calibration = diagonal. Shows over/under-confidence.
- **Confidence Distribution** — Histogram of confidence scores across all decisions. Are we clustering at extremes? Healthy = bell curve.
- **Outcome by Category** — Stacked bar chart: for each category (architecture, process, tooling, security, integration), show success/partial/failure/pending proportions.
- **Outcome by Stakes** — Same, grouped by stakes level.
- **Reason Type Usage** — Bar chart: how often each reason type is used (analysis, pattern, empirical, authority, intuition, analogy, elimination, constraint).
- **Reason Type × Outcome** — Which reason types correlate with success vs failure? Heatmap or grouped bar.
- **Brier Score Over Time** — Line chart from `calibration_snapshots`. Trending down = improving.
- **Decisions Per Day** — Bar chart, last 30 days.

**API Endpoint:** `GET /dashboard/calibration`
```json
{
  "calibration_curve": [
    { "bucket": "0.0-0.2", "predicted_avg": 0.15, "actual_success_rate": 0.10, "count": 5 },
    { "bucket": "0.2-0.4", "predicted_avg": 0.32, "actual_success_rate": 0.28, "count": 12 }
  ],
  "confidence_histogram": [
    { "range": "0.0-0.1", "count": 2 },
    { "range": "0.1-0.2", "count": 5 }
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
  "brier_history": [
    { "date": "2026-03-01", "score": 0.045 },
    { "date": "2026-03-08", "score": 0.032 }
  ],
  "daily_decisions": [
    { "date": "2026-03-01", "count": 5 }
  ]
}
```

---

### 5. System Activity

**Purpose:** Monitor operational health — sleep events, censor activations, schedule activity.

**Layout:** Timeline + stat cards

**Components:**
- **Activity Timeline** — Reverse-chronological feed of system events from `nous_system.events`:
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

### 6. Graph Health (Sub-view of Overview)

**Purpose:** Track graph-specific metrics over time to validate F022 and future F023.

**Charts:**
- **Graph Density Over Time** — Line chart. Target: >3.0 for spreading activation.
- **Edge Creation Rate** — Bar chart, daily. Spike detection.
- **Edge Type Distribution Over Time** — Stacked area chart. Shows if one type dominates.
- **Node Degree Distribution** — Histogram. Power-law distribution is healthy (few hubs, many leaf nodes). Uniform = suspicious.
- **Orphan Nodes Over Time** — Line chart. Rising = FactGraphLinker not keeping up.
- **Auto-linked vs Manual Edges** — Ratio over time.

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
    { "degree": 1, "count": 45 },
    { "degree": 2, "count": 30 }
  ],
  "orphan_trend": [
    { "date": "2026-03-01", "count": 40 },
    { "date": "2026-03-19", "count": 23 }
  ]
}
```

---

## New REST API Endpoints

| Endpoint | Method | Purpose | Estimated Complexity |
|----------|--------|---------|---------------------|
| `GET /dashboard/stats` | GET | Aggregated counts, distributions, timeseries | Medium — 8 aggregate queries |
| `GET /dashboard/graph` | GET | Nodes + edges for D3 visualization | Medium — joins across 4 tables |
| `GET /dashboard/calibration` | GET | Decision intelligence analytics | Medium — bucketed aggregations |
| `GET /dashboard/activity` | GET | System events timeline | Low — event log query |
| `GET /dashboard/health` | GET | Graph health trends | Medium — time-bucketed edge stats |

**Existing endpoints needing updates:**
| Endpoint | Change |
|----------|--------|
| `GET /facts` | Add pagination, sort, category/confidence/date filters |
| `GET /episodes` | Add pagination, sort, outcome/frame/date filters |
| `GET /decisions` | Add pagination, sort, category/stakes/outcome/date filters |
| `GET /censors` | Add pagination, sort, action/domain/created_by filters |

---

## File Structure

```
nous/
├── nous/
│   ├── api/
│   │   ├── rest.py              (add 5 new endpoints + pagination on existing)
│   │   └── dashboard_queries.py (new — SQL queries for dashboard aggregations)
│   └── ...
├── static/
│   └── dashboard/
│       ├── index.html           (SPA shell — navigation + view containers)
│       ├── css/
│       │   └── dashboard.css    (extends landing.html design system)
│       ├── js/
│       │   ├── app.js           (routing, view switching, API client)
│       │   ├── overview.js      (stat cards + mini charts)
│       │   ├── graph.js         (D3 force-directed graph)
│       │   ├── browser.js       (tabbed memory browser)
│       │   ├── decisions.js     (decision intelligence charts)
│       │   ├── activity.js      (system activity timeline)
│       │   └── health.js        (graph health charts)
│       └── lib/                 (vendored or CDN — Chart.js, D3.js)
└── ...
```

---

## Implementation Plan

### PR 1 — API Layer (~5 hours)
**New file:** `nous/nous/api/dashboard_queries.py`
- All 5 new endpoint query functions
- Pagination helper (shared across existing endpoints)
- Date bucketing utility

**Modified file:** `nous/nous/api/rest.py`
- 5 new dashboard endpoints
- Pagination + filtering on existing list endpoints (/facts, /episodes, /decisions, /censors)
- Static file serving for `/dashboard/*`

**Tests:**
- Unit tests for each query function
- Integration tests for each endpoint
- Pagination edge cases

### PR 2 — Dashboard Shell + Overview (~4 hours)
- `index.html` — SPA shell with navigation sidebar
- `dashboard.css` — dark theme extending landing.html variables
- `app.js` — client-side routing, API client wrapper, loading states
- `overview.js` — stat cards, mini charts (Chart.js)
- Responsive layout (desktop primary, tablet secondary)

### PR 3 — Knowledge Graph + Memory Browser (~6 hours)
- `graph.js` — D3 force-directed graph with all interactive features
- `browser.js` — tabbed memory browser with search, filters, expandable rows
- This is the most complex PR — graph rendering and interaction handling

### PR 4 — Decision Intelligence + System Activity + Graph Health (~5 hours)
- `decisions.js` — calibration curve, histograms, heatmaps
- `activity.js` — event timeline, censor stats, schedule status
- `health.js` — graph health trend charts
- Final polish, cross-view navigation (click fact in browser → highlight in graph)

---

## Design System

Extends the existing landing page design system:

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
  
  /* New — node type colors */
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

## Navigation

Sidebar navigation (collapsible):

```
🧠 Nous Dashboard
─────────────────
📊 Overview          (default view)
🕸️ Knowledge Graph
📚 Memory Browser
🎯 Decisions
⚡ Activity
📈 Graph Health
─────────────────
⚙️ Settings (future)
```

---

## Performance Considerations

1. **Graph rendering** — Default limit 500 nodes. Server-side filtering. Progressive loading for larger graphs.
2. **Dashboard queries** — All aggregate queries use existing indexes. Time-bucketed queries may need a materialized view if slow (>500ms).
3. **Caching** — Overview stats can be cached for 60 seconds (stat cards don't need real-time). Graph data cached for 5 minutes.
4. **Bundle size** — Chart.js (~60KB gzip) + D3 (~75KB gzip) + app code (~30KB). Total: ~165KB. Acceptable.

---

## Future Enhancements (Not in Scope)

1. **Real-time updates** — WebSocket push for live event stream
2. **Memory editing** — Edit/delete facts, retire censors from dashboard (breaks read-only principle, needs auth)
3. **Multi-agent view** — Compare memory across agents (needs F024 shared memory first)
4. **Embedding space visualization** — t-SNE/UMAP projection of all embeddings in 2D (compute-heavy, needs backend support)
5. **Admission control dashboard** — F023-specific: admission scores, rejection rates, score distributions (add after F023 implementation)
6. **Export** — CSV/JSON export of filtered memory contents
7. **Diff view** — Compare two time snapshots of memory state
8. **Search with embeddings** — Semantic search from dashboard (needs embedding generation client-side or server-side proxy)

---

## Success Criteria

1. **Page load < 2 seconds** — Including API calls and chart rendering
2. **Graph renders 500 nodes smoothly** — >30fps pan/zoom on modern browser
3. **All 6 views functional** — Overview, Graph, Browser, Decisions, Activity, Health
4. **Zero build step** — `git clone` + `python -m nous` serves the dashboard. No npm install.
5. **All data from existing schema** — No new database tables required
6. **Works with current REST API auth** — Same agent_id scoping

---

## Open Questions

1. **Auth** — Dashboard currently inherits Nous server auth (none for local, API key for hosted). Should we add basic auth for dashboard specifically?
2. **Embedding visualization** — Worth adding t-SNE/UMAP as a future view? Would need server-side dimensionality reduction.
3. **Mobile** — Is tablet enough, or do we need phone support?
4. **Graph layout algorithm** — Force-directed is intuitive but can be messy for large graphs. Consider hierarchical layout for specific views (e.g., episode → facts → edges).
5. **Integration with F023** — When admission control ships, add a dedicated "Admission" tab showing scores, rejections, threshold tuning.

---

## Research Backing

- **Minsky Ch. 6 (Insight & Introspection):** "B-Brains observe A-Brains." The dashboard IS a B-brain for humans — observing the agent's internal state to detect dysfunction.
- **Minsky Ch. 15 (Consciousness & Memory):** "Self-knowledge is a narrative." The dashboard provides a different narrative than what the agent reports about itself — raw data vs. self-report.
- **Minsky Ch. 9 (Summaries):** The overview view is a summary of the agent's state. The graph view is the structural detail. Both are needed — you can't understand a system at only one level.

---

## References

- **Existing schema:** `nous/sql/init.sql` (23 tables, 3 schemas)
- **Existing REST API:** `nous/nous/api/rest.py` (23 endpoints)
- **Existing landing page:** `nous/landing.html` (design system reference)
- **Graph density:** `nous/nous/brain/spreading_activation.py` (`compute_graph_density()`)
- **F022:** Graph-augmented recall (the data this dashboard visualizes)
- **F023:** Memory admission control (future dashboard integration)
