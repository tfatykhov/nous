# Rubric Dashboard Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Rubric" tab to the Nous dashboard that visualizes rubric dimensions/weights, outcome signals, version history, dimension-signal correlations, and evolution status.

**Architecture:** New dashboard tab follows the exact F021 pattern — a backend query function in `dashboard_queries.py`, a REST endpoint in `rest.py`, and a vanilla JS view module. One `GET /dashboard/rubric` endpoint returns all rubric data pre-shaped for visualization. The JS module uses Chart.js for charts (radar for weights, bar for signal distribution, line for weight history, heatmap-table for correlations).

**Tech Stack:** Vanilla JS, Chart.js (vendored), Starlette REST, SQLAlchemy raw SQL queries, pytest + httpx for tests.

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `nous/api/dashboard_queries.py` | Modify | Add `get_rubric_dashboard_data()` query function |
| `nous/api/rest.py` | Modify | Add `GET /dashboard/rubric` endpoint + route |
| `static/dashboard/js/rubric.js` | Create | Rubric dashboard view (register with `Dashboard.registerView`) |
| `static/dashboard/index.html` | Modify | Add nav link + view container + script tag |
| `tests/test_rubric_dashboard.py` | Create | Integration tests for endpoint + query function |

---

## Data Contract

`GET /dashboard/rubric` returns:

```json
{
  "active_rubric": {
    "version": "1.0.0",
    "status": "active",
    "dimension_count": 4,
    "created_at": "2026-03-25T...",
    "dimensions": [
      {"name": "Recall", "weight": 0.25, "description": "...", "scoring_criteria": "..."},
      ...
    ]
  },
  "version_history": [
    {"version": "1.0.0", "status": "active", "change_reason": "...", "dimension_count": 4, "created_at": "..."},
    ...
  ],
  "outcome_signals": {
    "total": 42,
    "by_type": {"completed": 25, "corrected": 8, "praised": 5, "reworked": 3, "self_corrected": 1},
    "recent": [
      {"signal_type": "completed", "confidence": 0.85, "evidence": "...", "created_at": "..."},
      ...
    ],
    "daily_trend": [
      {"date": "2026-03-28", "completed": 3, "corrected": 1, "praised": 0, "reworked": 0, "self_corrected": 0},
      ...
    ]
  },
  "correlations": {
    "data": [
      {"dimension": "Recall", "signal_type": "completed", "pearson_r": 0.68, "spearman_rho": 0.71},
      ...
    ],
    "sample_size": 42
  },
  "weight_history": [
    {"version": "1.0.0", "created_at": "...", "weights": {"Recall": 0.25, "Tool Selection": 0.25, ...}},
    ...
  ],
  "config": {
    "rubric_enabled": true,
    "evolution_enabled": false,
    "outcome_detection_enabled": true,
    "min_episodes_for_correlation": 50,
    "weight_change_cap": 0.05
  }
}
```

---

## Task 1: Backend Query Function

**Files:**
- Modify: `nous/api/dashboard_queries.py` (append new function at end of file)
- Test: `tests/test_rubric_dashboard.py`

- [ ] **Step 1: Write the failing test for `get_rubric_dashboard_data`**

Create `tests/test_rubric_dashboard.py`:

```python
"""Tests for rubric dashboard query and endpoint."""

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from nous.storage.models import Episode, OutcomeSignal, RubricVersion


class MockAgentRunner:
    def __init__(self):
        self._conversations = {}

    async def start(self):
        pass

    async def close(self):
        pass


@pytest_asyncio.fixture
async def seed_rubric(db, settings):
    """Seed a rubric version, an episode, and outcome signals."""
    async with db.session() as session:
        rv = RubricVersion(
            agent_id=settings.agent_id,
            version="1.0.0",
            parent_version=None,
            change_reason="Initial rubric",
            dimensions=[
                {"name": "Recall", "weight": 0.25, "description": "Memory retrieval",
                 "scoring_criteria": "1-10", "min_weight": 0.10, "max_weight": 0.40},
                {"name": "Tool Selection", "weight": 0.25, "description": "Tool choice",
                 "scoring_criteria": "1-10", "min_weight": 0.10, "max_weight": 0.40},
                {"name": "Confidence Calibration", "weight": 0.25, "description": "Calibration",
                 "scoring_criteria": "1-10", "min_weight": 0.10, "max_weight": 0.40},
                {"name": "Proactivity", "weight": 0.25, "description": "Anticipation",
                 "scoring_criteria": "1-10", "min_weight": 0.10, "max_weight": 0.40},
            ],
            outcome_correlations={},
            status="active",
        )
        session.add(rv)
        await session.flush()

        # Create a real episode (OutcomeSignal has FK to heart.episodes)
        ep = Episode(
            agent_id=settings.agent_id,
            summary="Test episode for rubric dashboard",
            outcome="success",
        )
        session.add(ep)
        await session.flush()

        for sig_type in ["completed", "praised"]:
            sig = OutcomeSignal(
                agent_id=settings.agent_id,
                episode_id=ep.id,
                signal_type=sig_type,
                confidence=0.85,
                evidence="Test evidence",
            )
            session.add(sig)
        await session.commit()
    return rv


@pytest.mark.asyncio
async def test_get_rubric_dashboard_data(db, settings, seed_rubric):
    from nous.api.dashboard_queries import get_rubric_dashboard_data

    async with db.session() as session:
        data = await get_rubric_dashboard_data(session, settings.agent_id, settings)

    assert data["active_rubric"] is not None
    assert data["active_rubric"]["version"] == "1.0.0"
    assert len(data["active_rubric"]["dimensions"]) == 4
    assert data["outcome_signals"]["total"] == 2
    assert "completed" in data["outcome_signals"]["by_type"]
    assert "praised" in data["outcome_signals"]["by_type"]
    assert len(data["version_history"]) >= 1
    assert len(data["weight_history"]) >= 1
    assert "config" in data
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rubric_dashboard.py::test_get_rubric_dashboard_data -v`
Expected: FAIL with `ImportError: cannot import name 'get_rubric_dashboard_data'`

- [ ] **Step 3: Write `get_rubric_dashboard_data` in `dashboard_queries.py`**

Append to end of `nous/api/dashboard_queries.py`:

```python
from typing import Any  # Add to file imports if not already present


async def get_rubric_dashboard_data(
    session: AsyncSession, agent_id: str, settings: Any = None,
) -> dict:
    """Return rubric dashboard data: active rubric, signals, history, correlations, config."""

    # Active rubric
    result = await session.execute(
        text("""
            SELECT id, version, status, change_reason, dimensions,
                   outcome_correlations, created_at
            FROM heart.rubric_versions
            WHERE agent_id = :agent_id AND status = 'active'
            LIMIT 1
        """),
        {"agent_id": agent_id},
    )
    active_row = result.one_or_none()
    active_rubric = None
    if active_row:
        dims = active_row.dimensions if isinstance(active_row.dimensions, list) else []
        active_rubric = {
            "version": active_row.version,
            "status": active_row.status,
            "dimension_count": len(dims),
            "created_at": active_row.created_at.isoformat() if active_row.created_at else None,
            "dimensions": dims,
        }

    # Version history
    result = await session.execute(
        text("""
            SELECT id, version, status, change_reason, dimensions, created_at
            FROM heart.rubric_versions
            WHERE agent_id = :agent_id
            ORDER BY created_at DESC
            LIMIT 20
        """),
        {"agent_id": agent_id},
    )
    version_history = []
    weight_history = []
    for row in result:
        dims = row.dimensions if isinstance(row.dimensions, list) else []
        version_history.append({
            "version": row.version,
            "status": row.status,
            "change_reason": row.change_reason,
            "dimension_count": len(dims),
            "created_at": row.created_at.isoformat() if row.created_at else None,
        })
        weight_history.append({
            "version": row.version,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "weights": {d["name"]: d["weight"] for d in dims if "name" in d and "weight" in d},
        })

    # Outcome signals — totals by type
    result = await session.execute(
        text("""
            SELECT signal_type, COUNT(*) AS cnt
            FROM heart.outcome_signals
            WHERE agent_id = :agent_id
            GROUP BY signal_type
        """),
        {"agent_id": agent_id},
    )
    by_type = {row.signal_type: row.cnt for row in result}
    total_signals = sum(by_type.values())

    # Outcome signals — recent 20
    result = await session.execute(
        text("""
            SELECT signal_type, confidence, evidence, created_at
            FROM heart.outcome_signals
            WHERE agent_id = :agent_id
            ORDER BY created_at DESC
            LIMIT 20
        """),
        {"agent_id": agent_id},
    )
    recent_signals = [
        {
            "signal_type": row.signal_type,
            "confidence": float(row.confidence) if row.confidence else 0.0,
            "evidence": row.evidence,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in result
    ]

    # Outcome signals — daily trend (last 30 days)
    result = await session.execute(
        text("""
            SELECT CAST(d AS date) AS date,
                   COUNT(*) FILTER (WHERE s.signal_type = 'completed') AS completed,
                   COUNT(*) FILTER (WHERE s.signal_type = 'corrected') AS corrected,
                   COUNT(*) FILTER (WHERE s.signal_type = 'praised') AS praised,
                   COUNT(*) FILTER (WHERE s.signal_type = 'reworked') AS reworked,
                   COUNT(*) FILTER (WHERE s.signal_type = 'self_corrected') AS self_corrected
            FROM generate_series(
                CAST(NOW() - INTERVAL '30 days' AS date),
                CAST(NOW() AS date),
                '1 day'::interval
            ) d
            LEFT JOIN heart.outcome_signals s
                ON CAST(s.created_at AS date) = CAST(d AS date)
                AND s.agent_id = :agent_id
            GROUP BY CAST(d AS date)
            ORDER BY CAST(d AS date)
        """),
        {"agent_id": agent_id},
    )
    daily_trend = [
        {
            "date": row.date.isoformat() if hasattr(row.date, 'isoformat') else str(row.date),
            "completed": row.completed,
            "corrected": row.corrected,
            "praised": row.praised,
            "reworked": row.reworked,
            "self_corrected": row.self_corrected,
        }
        for row in result
    ]

    # Correlations from active rubric's stored data
    correlations_data = []
    correlation_sample = 0
    if active_row and active_row.outcome_correlations:
        oc = active_row.outcome_correlations
        for dim_name, signals in oc.items():
            if isinstance(signals, dict):
                for sig_type, stats in signals.items():
                    if isinstance(stats, dict):
                        correlations_data.append({
                            "dimension": dim_name,
                            "signal_type": sig_type,
                            "pearson_r": stats.get("pearson_r", 0),
                            "spearman_rho": stats.get("spearman_rho", 0),
                        })
                        correlation_sample = max(correlation_sample, stats.get("sample_size", 0))

    # Config
    config = {}
    if settings:
        config = {
            "rubric_enabled": getattr(settings, "rubric_enabled", False),
            "evolution_enabled": getattr(settings, "rubric_evolution_enabled", False),
            "outcome_detection_enabled": getattr(settings, "rubric_outcome_detection_enabled", False),
            "min_episodes_for_correlation": getattr(settings, "rubric_min_episodes_for_correlation", 50),
            "weight_change_cap": getattr(settings, "rubric_weight_change_cap", 0.05),
        }

    return {
        "active_rubric": active_rubric,
        "version_history": version_history,
        "outcome_signals": {
            "total": total_signals,
            "by_type": by_type,
            "recent": recent_signals,
            "daily_trend": daily_trend,
        },
        "correlations": {
            "data": correlations_data,
            "sample_size": correlation_sample,
        },
        "weight_history": weight_history,
        "config": config,
    }
```

Add `from typing import Any` to the imports if not already present.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_rubric_dashboard.py::test_get_rubric_dashboard_data -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nous/api/dashboard_queries.py tests/test_rubric_dashboard.py
git commit -m "feat(dashboard): add rubric dashboard query function"
```

---

## Task 2: REST Endpoint

**Files:**
- Modify: `nous/api/rest.py` (add endpoint handler + route)
- Test: `tests/test_rubric_dashboard.py`

- [ ] **Step 1: Write the failing endpoint test**

Append to `tests/test_rubric_dashboard.py`:

```python
# brain, heart, db, settings fixtures come from conftest.py — do NOT redefine them here.

@pytest_asyncio.fixture
async def brain(db, settings):
    from nous.brain.brain import Brain
    b = Brain(database=db, settings=settings)
    yield b
    await b.close()


@pytest_asyncio.fixture
async def cognitive(brain, heart, settings):
    from nous.cognitive.layer import CognitiveLayer
    return CognitiveLayer(brain, heart, settings, identity_prompt="You are Nous.")


@pytest.fixture
def app(brain, heart, cognitive, db, settings):
    from nous.api.rest import create_app
    return create_app(MockAgentRunner(), brain, heart, cognitive, db, settings)


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_dashboard_rubric_endpoint(client, seed_rubric):
    resp = await client.get("/dashboard/rubric")
    assert resp.status_code == 200
    data = resp.json()
    assert data["active_rubric"]["version"] == "1.0.0"
    assert data["outcome_signals"]["total"] == 2
    assert "config" in data


@pytest.mark.asyncio
async def test_dashboard_rubric_endpoint_empty(client):
    """Returns gracefully when no rubric data exists."""
    resp = await client.get("/dashboard/rubric")
    assert resp.status_code == 200
    data = resp.json()
    assert data["active_rubric"] is None
    assert data["outcome_signals"]["total"] == 0


@pytest.mark.asyncio
async def test_dashboard_rubric_endpoint_no_correlations(client, seed_rubric):
    """Rubric exists with signals but no correlations yet."""
    resp = await client.get("/dashboard/rubric")
    assert resp.status_code == 200
    data = resp.json()
    assert data["correlations"]["data"] == []
    assert data["correlations"]["sample_size"] == 0
```

Note: `heart` fixture comes from `conftest.py` — do NOT redefine it locally. Only `brain`, `cognitive`, `app`, and `client` need local definitions.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_rubric_dashboard.py::test_dashboard_rubric_endpoint -v`
Expected: FAIL (404 — route doesn't exist yet)

- [ ] **Step 3: Add the endpoint handler and route in `rest.py`**

In `nous/api/rest.py`, add the handler function near the other dashboard handlers (around line 975):

```python
    async def dashboard_rubric(request: Request) -> JSONResponse:
        """GET /dashboard/rubric - Rubric analytics for dashboard."""
        try:
            from nous.api.dashboard_queries import get_rubric_dashboard_data

            async with database.session() as session:
                data = await get_rubric_dashboard_data(session, settings.agent_id, settings)
            return JSONResponse(data)
        except Exception as e:
            logger.error("Dashboard rubric error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)
```

Add the route in the routes list, between the health route and the admission routes (around line 1272):

```python
        Route("/dashboard/rubric", dashboard_rubric),
```

Place it BEFORE the `/dashboard/admission` routes and AFTER `/dashboard/health`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_rubric_dashboard.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add nous/api/rest.py tests/test_rubric_dashboard.py
git commit -m "feat(dashboard): add GET /dashboard/rubric endpoint"
```

---

## Task 3: Frontend — Nav Link & View Container

**Files:**
- Modify: `static/dashboard/index.html`

- [ ] **Step 1: Add the nav link in the sidebar**

In `static/dashboard/index.html`, add a new nav link after the Admission link (after line 45) and before the closing `</div>` of `.nav-links`:

```html
            <a href="#/rubric" class="nav-link" data-view="rubric">
                <svg class="nav-icon" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M6 2a2 2 0 00-2 2v12a2 2 0 002 2h8a2 2 0 002-2V7.414A2 2 0 0015.414 6L12 2.586A2 2 0 0010.586 2H6zm5 6a1 1 0 10-2 0v2H7a1 1 0 100 2h2v2a1 1 0 102 0v-2h2a1 1 0 100-2h-2V8z" clip-rule="evenodd"/></svg>
                <span>Rubric</span>
            </a>
```

- [ ] **Step 2: Add the view container**

After the `view-admission` div (line 59), add:

```html
        <div id="view-rubric" class="view"></div>
```

- [ ] **Step 3: Add the script tag**

After the admission.js script (line 71), add:

```html
    <script src="js/rubric.js"></script>
```

- [ ] **Step 4: Verify by opening dashboard in browser**

Navigate to the dashboard and confirm the "Rubric" nav link appears. Clicking it should show an empty view (JS not yet created).

- [ ] **Step 5: Commit**

```bash
git add static/dashboard/index.html
git commit -m "feat(dashboard): add rubric nav link and view container"
```

---

## Task 4: Frontend — Rubric View JS Module

**Files:**
- Create: `static/dashboard/js/rubric.js`

- [ ] **Step 1: Create `rubric.js` with view registration and data fetch**

Create `static/dashboard/js/rubric.js`:

```javascript
/**
 * Nous Dashboard — Rubric View (F024 Phase 3b)
 *
 * Visualizes self-modifying rubric: current dimensions & weights,
 * outcome signal distribution, version history, correlation heatmap,
 * and weight evolution over time.
 */

/* global Dashboard, Chart, escapeHtml */

Dashboard.registerView('rubric', async function (container) {
    Dashboard.showLoading(container);

    try {
        var data = await Dashboard.apiGet('/dashboard/rubric');
        renderRubric(container, data);
    } catch (err) {
        Dashboard.showError(container, 'Failed to load rubric data.', function () {
            Dashboard.reloadView('rubric');
        });
    }
});

function renderRubric(container, data) {
    container.innerHTML = '<div class="view-header">' +
        '<h1>Self-Modifying Rubric</h1>' +
        '<p class="view-subtitle">F024 Phase 3b — Evaluation dimensions, outcome signals, and evolution tracking</p>' +
        '</div>' +
        '<div id="rubric-content"></div>';

    var content = document.getElementById('rubric-content');

    if (!data.active_rubric) {
        Dashboard.showEmpty(container, 'No active rubric — ensure NOUS_RUBRIC_ENABLED=true and the rubric has been seeded.');
        return;
    }

    renderRubricConfig(content, data);
    renderDimensionWeights(content, data);
    renderOutcomeSignals(content, data);
    renderSignalTrend(content, data);
    renderCorrelationHeatmap(content, data);
    renderWeightHistory(content, data);
    renderVersionHistory(content, data);
    renderRecentSignals(content, data);
}

// ── Config banner ────────────────────────────────────────────────────

function renderRubricConfig(el, data) {
    var cfg = data.config;
    var rubric = data.active_rubric;

    var banner = document.createElement('div');
    banner.className = 'admission-banner ' + (cfg.evolution_enabled ? 'enforced' : 'shadow');
    banner.innerHTML =
        '<div class="banner-indicator ' + (cfg.evolution_enabled ? 'enforced-indicator' : 'shadow-indicator') + '"></div>' +
        '<div class="banner-text">' +
        '<strong>Rubric v' + escapeHtml(rubric.version) + '</strong> — ' +
        rubric.dimension_count + ' dimensions' +
        (cfg.evolution_enabled ? ' | Evolution ACTIVE' : ' | Evolution OFF (observation mode)') +
        '<div class="banner-stats">' +
        'Outcome detection: ' + (cfg.outcome_detection_enabled ? 'ON' : 'OFF') + ' | ' +
        'Signals collected: ' + Dashboard.formatNumber(data.outcome_signals.total) + ' | ' +
        'Min episodes for correlation: ' + cfg.min_episodes_for_correlation + ' | ' +
        'Weight cap: \u00b1' + (cfg.weight_change_cap * 100).toFixed(0) + '%' +
        '</div></div>';
    el.appendChild(banner);
}

// ── Dimension weights (radar chart) ─────────────────────────────────

function renderDimensionWeights(el, data) {
    var dims = data.active_rubric.dimensions;

    var section = document.createElement('div');
    section.className = 'chart-section';
    section.innerHTML =
        '<h3>Current Dimensions & Weights</h3>' +
        '<div class="chart-grid">' +
        '<div class="chart-container" style="height:320px"><canvas id="rubric-radar-chart"></canvas></div>' +
        '<div id="rubric-dimension-cards"></div>' +
        '</div>';
    el.appendChild(section);

    // Radar chart
    var labels = dims.map(function (d) { return d.name; });
    var weights = dims.map(function (d) { return d.weight; });
    var minWeights = dims.map(function (d) { return d.min_weight || 0.10; });
    var maxWeights = dims.map(function (d) { return d.max_weight || 0.40; });

    var ctx = document.getElementById('rubric-radar-chart').getContext('2d');
    Dashboard.trackChart(new Chart(ctx, {
        type: 'radar',
        data: {
            labels: labels,
            datasets: [
                {
                    label: 'Current Weight',
                    data: weights,
                    borderColor: '#7c6af7',
                    backgroundColor: 'rgba(124, 106, 247, 0.2)',
                    pointBackgroundColor: '#7c6af7',
                    borderWidth: 2,
                },
                {
                    label: 'Min Bound',
                    data: minWeights,
                    borderColor: 'rgba(248, 113, 113, 0.4)',
                    backgroundColor: 'transparent',
                    borderDash: [4, 4],
                    borderWidth: 1,
                    pointRadius: 0,
                },
                {
                    label: 'Max Bound',
                    data: maxWeights,
                    borderColor: 'rgba(52, 211, 153, 0.4)',
                    backgroundColor: 'transparent',
                    borderDash: [4, 4],
                    borderWidth: 1,
                    pointRadius: 0,
                }
            ]
        },
        options: {
            scales: {
                r: {
                    min: 0,
                    max: 0.5,
                    ticks: { stepSize: 0.1, display: true },
                    grid: { color: '#1e1e2e' },
                    angleLines: { color: '#1e1e2e' },
                    pointLabels: { font: { size: 12 } }
                }
            },
            plugins: { legend: { display: true, position: 'bottom' } }
        }
    }));

    // Dimension detail cards
    var cards = document.getElementById('rubric-dimension-cards');
    var grid = document.createElement('div');
    grid.className = 'stat-cards';
    dims.forEach(function (d) {
        var pct = (d.weight * 100).toFixed(0);
        var card = document.createElement('div');
        card.className = 'stat-card';
        card.innerHTML =
            '<div class="stat-value">' + pct + '%</div>' +
            '<div class="stat-label">' + escapeHtml(d.name) + '</div>' +
            '<div class="stat-detail">' + escapeHtml(d.description) + '</div>';
        grid.appendChild(card);
    });
    cards.appendChild(grid);
}

// ── Outcome signal distribution (doughnut) ──────────────────────────

function renderOutcomeSignals(el, data) {
    var byType = data.outcome_signals.by_type;
    if (!byType || Object.keys(byType).length === 0) return;

    var section = document.createElement('div');
    section.className = 'chart-section';
    section.innerHTML =
        '<h3>Outcome Signal Distribution</h3>' +
        '<p class="section-note">Total: ' + Dashboard.formatNumber(data.outcome_signals.total) + ' signals detected from episodes</p>' +
        '<div class="chart-container" style="height:280px;max-width:500px"><canvas id="rubric-signal-doughnut"></canvas></div>';
    el.appendChild(section);

    var signalColors = {
        completed: '#34d399',
        praised: '#60a5fa',
        corrected: '#fb923c',
        reworked: '#f87171',
        self_corrected: '#a78bfa'
    };

    var types = Object.keys(byType);
    var counts = types.map(function (t) { return byType[t]; });
    var colors = types.map(function (t) { return signalColors[t] || '#6b6b8a'; });

    var ctx = document.getElementById('rubric-signal-doughnut').getContext('2d');
    Dashboard.trackChart(new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels: types.map(function (t) { return t.replace('_', ' '); }),
            datasets: [{
                data: counts,
                backgroundColor: colors,
                borderColor: '#111118',
                borderWidth: 2,
            }]
        },
        options: {
            plugins: { legend: { position: 'right' } }
        }
    }));
}

// ── Signal trend over time (stacked area) ───────────────────────────

function renderSignalTrend(el, data) {
    var trend = data.outcome_signals.daily_trend;
    if (!trend || trend.length === 0) return;

    var hasData = trend.some(function (d) {
        return d.completed + d.corrected + d.praised + d.reworked + d.self_corrected > 0;
    });
    if (!hasData) return;

    var section = document.createElement('div');
    section.className = 'chart-section';
    section.innerHTML =
        '<h3>Signal Trend (30 Days)</h3>' +
        '<div class="chart-container" style="height:280px"><canvas id="rubric-signal-trend"></canvas></div>';
    el.appendChild(section);

    var labels = trend.map(function (d) { return d.date; });

    var ctx = document.getElementById('rubric-signal-trend').getContext('2d');
    Dashboard.trackChart(new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [
                { label: 'Completed', data: trend.map(function (d) { return d.completed; }), borderColor: '#34d399', backgroundColor: 'rgba(52,211,153,0.15)', fill: 'origin' },
                { label: 'Praised', data: trend.map(function (d) { return d.praised; }), borderColor: '#60a5fa', backgroundColor: 'rgba(96,165,250,0.15)', fill: '-1' },
                { label: 'Corrected', data: trend.map(function (d) { return d.corrected; }), borderColor: '#fb923c', backgroundColor: 'rgba(251,146,60,0.15)', fill: '-1' },
                { label: 'Reworked', data: trend.map(function (d) { return d.reworked; }), borderColor: '#f87171', backgroundColor: 'rgba(248,113,113,0.15)', fill: '-1' },
                { label: 'Self-corrected', data: trend.map(function (d) { return d.self_corrected; }), borderColor: '#a78bfa', backgroundColor: 'rgba(167,139,250,0.15)', fill: '-1' },
            ]
        },
        options: {
            plugins: { legend: { display: true, position: 'bottom' } },
            scales: {
                x: { title: { display: true, text: 'Date' } },
                y: { title: { display: true, text: 'Signals' }, beginAtZero: true, stacked: true }
            }
        }
    }));
}

// ── Correlation heatmap (HTML table) ────────────────────────────────

function renderCorrelationHeatmap(el, data) {
    var corr = data.correlations.data;
    if (!corr || corr.length === 0) {
        var section = document.createElement('div');
        section.className = 'chart-section';
        section.innerHTML =
            '<h3>Dimension \u2194 Signal Correlations</h3>' +
            '<p class="section-note">No correlation data yet. Need ' +
            (data.config.min_episodes_for_correlation || 50) +
            '+ episodes with outcome signals before correlations are computed.</p>';
        el.appendChild(section);
        return;
    }

    // Build matrix: dims x signal_types
    var dims = [];
    var sigTypes = [];
    var matrix = {};
    corr.forEach(function (c) {
        if (dims.indexOf(c.dimension) === -1) dims.push(c.dimension);
        if (sigTypes.indexOf(c.signal_type) === -1) sigTypes.push(c.signal_type);
        matrix[c.dimension + '|' + c.signal_type] = c;
    });

    var section = document.createElement('div');
    section.className = 'table-section';
    section.innerHTML =
        '<h3>Dimension \u2194 Signal Correlations</h3>' +
        '<p class="section-note">Pearson r (Spearman \u03c1 in tooltip). Sample size: ' + data.correlations.sample_size + ' episodes.</p>';

    var html = '<table class="data-table correlation-table"><thead><tr><th>Dimension</th>';
    sigTypes.forEach(function (s) { html += '<th>' + escapeHtml(s.replace('_', ' ')) + '</th>'; });
    html += '</tr></thead><tbody>';

    dims.forEach(function (dim) {
        html += '<tr><td>' + escapeHtml(dim) + '</td>';
        sigTypes.forEach(function (sig) {
            var c = matrix[dim + '|' + sig];
            if (c) {
                var r = c.pearson_r;
                var color = correlationColor(r);
                html += '<td class="corr-cell" style="background:' + color + '" title="Pearson r=' +
                    r.toFixed(3) + ', Spearman \u03c1=' + c.spearman_rho.toFixed(3) + '">' +
                    r.toFixed(2) + '</td>';
            } else {
                html += '<td class="corr-cell" style="background:transparent">-</td>';
            }
        });
        html += '</tr>';
    });
    html += '</tbody></table>';
    section.innerHTML += html;
    el.appendChild(section);
}

function correlationColor(r) {
    // Red for negative, green for positive, intensity by magnitude
    var abs = Math.min(Math.abs(r), 1.0);
    var alpha = (abs * 0.6 + 0.05).toFixed(2);
    if (r >= 0) return 'rgba(52, 211, 153, ' + alpha + ')';
    return 'rgba(248, 113, 113, ' + alpha + ')';
}

// ── Weight evolution over versions (line chart) ─────────────────────

function renderWeightHistory(el, data) {
    var wh = data.weight_history;
    if (!wh || wh.length < 2) return;

    // Reverse so oldest first
    wh = wh.slice().reverse();

    var section = document.createElement('div');
    section.className = 'chart-section';
    section.innerHTML =
        '<h3>Weight Evolution</h3>' +
        '<p class="section-note">How dimension weights have changed across rubric versions</p>' +
        '<div class="chart-container" style="height:280px"><canvas id="rubric-weight-history"></canvas></div>';
    el.appendChild(section);

    var labels = wh.map(function (v) { return 'v' + v.version; });

    // Collect all dimension names
    var allDims = {};
    wh.forEach(function (v) {
        Object.keys(v.weights).forEach(function (d) { allDims[d] = true; });
    });

    var dimColors = ['#7c6af7', '#34d399', '#60a5fa', '#fb923c', '#f87171', '#a78bfa', '#fbbf24'];
    var datasets = Object.keys(allDims).map(function (dim, i) {
        return {
            label: dim,
            data: wh.map(function (v) { return v.weights[dim] || null; }),
            borderColor: dimColors[i % dimColors.length],
            backgroundColor: 'transparent',
            spanGaps: true,
        };
    });

    var ctx = document.getElementById('rubric-weight-history').getContext('2d');
    Dashboard.trackChart(new Chart(ctx, {
        type: 'line',
        data: { labels: labels, datasets: datasets },
        options: {
            plugins: { legend: { display: true, position: 'bottom' } },
            scales: {
                x: { title: { display: true, text: 'Version' } },
                y: { title: { display: true, text: 'Weight' }, min: 0, max: 0.5 }
            }
        }
    }));
}

// ── Version history table ───────────────────────────────────────────

function renderVersionHistory(el, data) {
    var history = data.version_history;
    if (!history || history.length === 0) return;

    var section = document.createElement('div');
    section.className = 'table-section';
    section.innerHTML =
        '<h3>Version History</h3>';

    var html = '<table class="data-table"><thead><tr>' +
        '<th>Version</th><th>Status</th><th>Dimensions</th><th>Change Reason</th><th>Created</th>' +
        '</tr></thead><tbody>';

    history.forEach(function (v) {
        var statusClass = v.status === 'active' ? 'badge-success' :
                          v.status === 'rollback' ? 'badge-failure' : 'badge-pending';
        html += '<tr>' +
            '<td><strong>' + escapeHtml(v.version) + '</strong></td>' +
            '<td><span class="badge ' + statusClass + '">' + escapeHtml(v.status) + '</span></td>' +
            '<td>' + v.dimension_count + '</td>' +
            '<td>' + escapeHtml(v.change_reason) + '</td>' +
            '<td>' + Dashboard.formatDate(v.created_at) + '</td>' +
            '</tr>';
    });

    html += '</tbody></table>';
    section.innerHTML += html;
    el.appendChild(section);
}

// ── Recent outcome signals table ────────────────────────────────────

function renderRecentSignals(el, data) {
    var signals = data.outcome_signals.recent;
    if (!signals || signals.length === 0) return;

    var section = document.createElement('div');
    section.className = 'table-section';
    section.innerHTML = '<h3>Recent Outcome Signals</h3>';

    var signalColors = {
        completed: 'badge-success',
        praised: 'badge-success',
        corrected: 'badge-partial',
        reworked: 'badge-failure',
        self_corrected: 'badge-pending'
    };

    var html = '<table class="data-table"><thead><tr>' +
        '<th>Type</th><th>Confidence</th><th>Evidence</th><th>Detected</th>' +
        '</tr></thead><tbody>';

    signals.forEach(function (s) {
        var badge = signalColors[s.signal_type] || 'badge-pending';
        html += '<tr>' +
            '<td><span class="badge ' + badge + '">' + escapeHtml(s.signal_type.replace('_', ' ')) + '</span></td>' +
            '<td>' + (s.confidence * 100).toFixed(0) + '%</td>' +
            '<td class="content-cell">' + escapeHtml(Dashboard.truncate(s.evidence || '-', 120)) + '</td>' +
            '<td>' + (s.created_at ? Dashboard.formatDateTime(s.created_at) : '-') + '</td>' +
            '</tr>';
    });

    html += '</tbody></table>';
    section.innerHTML += html;
    el.appendChild(section);
}
```

- [ ] **Step 2: Verify the view renders in the browser**

Open the dashboard, click the "Rubric" tab. With data present, you should see:
1. Config banner showing rubric version and evolution status
2. Radar chart with dimension weights + detail cards
3. Doughnut chart of outcome signal types
4. Stacked area chart of signal trends over 30 days
5. Correlation heatmap table (if correlations exist)
6. Weight evolution line chart (if >1 version)
7. Version history table
8. Recent signals table

- [ ] **Step 3: Commit**

```bash
git add static/dashboard/js/rubric.js
git commit -m "feat(dashboard): add rubric view JS module with charts and tables"
```

---

## Task 5: Add CSS for Correlation Heatmap

**Files:**
- Modify: `static/dashboard/css/dashboard.css`

- [ ] **Step 1: Add correlation-specific CSS**

Append to `static/dashboard/css/dashboard.css`:

```css
/* Rubric correlation heatmap */
.correlation-table td.corr-cell {
    text-align: center;
    font-weight: 600;
    font-size: 0.85rem;
    min-width: 70px;
    transition: opacity 0.15s;
}
.correlation-table td.corr-cell:hover {
    opacity: 0.8;
}
```

- [ ] **Step 2: Commit**

```bash
git add static/dashboard/css/dashboard.css
git commit -m "feat(dashboard): add rubric correlation heatmap CSS"
```

---

## Task 6: Run Full Test Suite

- [ ] **Step 1: Run rubric dashboard tests**

Run: `uv run pytest tests/test_rubric_dashboard.py -v`
Expected: All PASS

- [ ] **Step 2: Run existing dashboard tests to ensure no regressions**

Run: `uv run pytest tests/test_rest_dashboard.py tests/test_dashboard_queries.py tests/test_admission_dashboard.py -v`
Expected: All PASS (existing tests unchanged)

- [ ] **Step 3: Verify in browser**

Open `http://localhost:PORT/dashboard#/rubric` and verify all sections render correctly.

---

## Summary

| Task | Description | Files |
|------|-------------|-------|
| 1 | Backend query function | `dashboard_queries.py`, `test_rubric_dashboard.py` |
| 2 | REST endpoint | `rest.py`, `test_rubric_dashboard.py` |
| 3 | Nav link + view container | `index.html` |
| 4 | Rubric JS view module | `rubric.js` |
| 5 | Correlation CSS | `dashboard.css` |
| 6 | Full test run | — |
