# F021.1 — Admission Control Dashboard View

> **Status:** Draft v1
> **Priority:** P2
> **Depends on:** F021 (Memory Dashboard), F023 (Memory Admission Control — live in shadow mode)
> **Estimated effort:** ~4–5 hours (1 API endpoint + 1 dashboard view)
> **Domain:** New `#/admission` view in existing dashboard

---

## Problem

F023 is live in **shadow mode** — scoring every fact candidate across 5 dimensions (utility, confidence, novelty, recency, type_prior) but admitting everything. The scores are logged but there's no structured way to:

1. **See what would have been rejected** — the key data needed to decide when to flip from shadow → enforcement
2. **Understand score distributions** — is the 0.55 threshold right, or are we going to reject too many/few?
3. **Identify which dimension is doing the heavy lifting** — Zhang et al. says type_prior is most influential; is that true for Nous?
4. **Spot anomalies** — are certain sources (sleep_reflection, knowledge_extractor) consistently scoring low?
5. **Track admission trends over time** — is fact quality improving or degrading?

Without this view, the decision to enable enforcement is a blind leap. With it, it's data-driven.

---

## Design

### New Dashboard View: Admission Control — `#/admission`

#### Section 1: Shadow Mode Banner

Top of page, prominent:

```
⚠️ SHADOW MODE ACTIVE — All facts are being admitted. Scores are logged but not enforced.
   Threshold: 0.55 | Facts scored: 847 | Would reject: 203 (24.0%)

   [View would-reject list]
```

When enforcement is active, banner changes to:

```
✅ ENFORCEMENT ACTIVE — Facts below 0.55 are rejected.
   Admitted: 644 (76%) | Rejected: 203 (24%) | Bypassed: 89
```

#### Section 2: Score Distribution (Histogram)

- **X-axis:** Composite score (0.0 to 1.0, bucketed in 0.05 increments)
- **Y-axis:** Fact count
- **Threshold line:** Vertical red dashed line at 0.55 (or current threshold)
- **Color:** Green bars above threshold, red bars below
- **Insight:** Shows if scores cluster around the threshold (risky — small weight changes flip many facts) or are bimodal (clean separation)

#### Section 3: Per-Dimension Breakdown (Radar/Box Plot)

**Option A — Radar chart (average scores):**
- 5 axes: utility, confidence, novelty, recency, type_prior
- Two overlays: "admitted" average vs "would-reject" average
- Shows which dimensions separate good facts from noise

**Option B — Box plots per dimension:**
- 5 side-by-side box plots showing min/Q1/median/Q3/max for each dimension
- Split by admitted vs would-reject
- Better for seeing spread and outliers

Recommend **Option B** — more informative for threshold tuning.

#### Section 4: Would-Have-Been-Rejected List

- Table of facts that scored below threshold
- Columns: Content (truncated), Source, Category, Composite Score, Utility, Confidence, Novelty, Recency, Type Prior, Created At
- Sortable by any column
- Click row → expands to show full content
- **This is THE critical view** — Tim reviews this list to gut-check: "would I miss any of these?" If yes → threshold too high. If no → safe to enforce.
- Pagination: limit=50, offset-based

#### Section 5: Admission by Source (Bar Chart)

- Grouped bar chart
- X-axis: source (knowledge_extractor, episode_summarizer, sleep_reflection, compaction_extraction, etc.)
- Y-axis: count
- Stacked or grouped: admitted vs would-reject vs bypassed
- Shows which extraction pipelines produce the most noise

#### Section 6: Admission by Category (Bar Chart)

- Same layout as Section 5 but grouped by fact category
- X-axis: rule, preference, person, technical, tool, concept
- Validates type_prior settings — if "concept" facts are mostly rejected, the 0.60 prior may be correct. If "technical" facts are getting rejected, 0.70 may be too low.

#### Section 7: Trends Over Time (Line Charts)

Two charts:

**7a — Daily admission rate:**
- X-axis: date (last 30 days)
- Y-axis: percentage admitted
- Shows if fact quality is improving or degrading over time

**7b — Daily average composite score:**
- X-axis: date
- Y-axis: average score
- Trend line — is the average rising (quality improving) or falling?

#### Section 8: Bypass Breakdown (Doughnut)

- Shows bypassed facts by bypass reason: user_stated, identity, censor, supersede, contradict
- Count + percentage
- Ensures bypass isn't being overused (if 80% of facts are bypassed, the gate is irrelevant)

#### Section 9: Threshold Simulator (Interactive)

- Slider: threshold from 0.0 to 1.0
- As slider moves, shows:
  - "At threshold X: Y facts admitted, Z rejected (W%)"
  - Updates the histogram coloring in real-time (client-side, no API call)
- Helps find the right threshold empirically
- **Read-only** — doesn't change the actual config, just simulates

---

## API

### New Endpoint: `GET /dashboard/admission`

```
GET /dashboard/admission?days=30
```

Response:

```json
{
  "config": {
    "enabled": true,
    "shadow_mode": true,
    "threshold": 0.55,
    "weights": {
      "utility": 0.25,
      "confidence": 0.15,
      "novelty": 0.20,
      "recency": 0.10,
      "type_prior": 0.30
    }
  },
  "summary": {
    "total_scored": 847,
    "admitted": 644,
    "would_reject": 203,
    "bypassed": 89,
    "rejection_rate": 0.240,
    "avg_composite_score": 0.62
  },
  "score_distribution": [
    { "bucket": "0.00-0.05", "count": 3 },
    { "bucket": "0.05-0.10", "count": 5 },
    { "bucket": "0.50-0.55", "count": 28 },
    { "bucket": "0.55-0.60", "count": 35 }
  ],
  "dimension_stats": {
    "utility": {
      "admitted": { "min": 0.30, "q1": 0.55, "median": 0.65, "q3": 0.80, "max": 0.95 },
      "rejected": { "min": 0.10, "q1": 0.25, "median": 0.35, "q3": 0.45, "max": 0.60 }
    },
    "confidence": { "admitted": {}, "rejected": {} },
    "novelty": { "admitted": {}, "rejected": {} },
    "recency": { "admitted": {}, "rejected": {} },
    "type_prior": { "admitted": {}, "rejected": {} }
  },
  "by_source": {
    "knowledge_extractor": { "admitted": 180, "rejected": 95, "bypassed": 0 },
    "episode_summarizer": { "admitted": 120, "rejected": 45, "bypassed": 0 },
    "sleep_reflection": { "admitted": 40, "rejected": 38, "bypassed": 0 },
    "user_stated": { "admitted": 0, "rejected": 0, "bypassed": 65 },
    "identity": { "admitted": 0, "rejected": 0, "bypassed": 12 }
  },
  "by_category": {
    "rule": { "admitted": 45, "rejected": 2, "avg_score": 0.82 },
    "preference": { "admitted": 38, "rejected": 5, "avg_score": 0.78 },
    "person": { "admitted": 62, "rejected": 8, "avg_score": 0.75 },
    "technical": { "admitted": 180, "rejected": 65, "avg_score": 0.63 },
    "tool": { "admitted": 55, "rejected": 28, "avg_score": 0.59 },
    "concept": { "admitted": 120, "rejected": 85, "avg_score": 0.55 }
  },
  "daily_trend": [
    {
      "date": "2026-03-19",
      "scored": 32,
      "admitted": 24,
      "rejected": 8,
      "bypassed": 5,
      "avg_score": 0.61
    }
  ],
  "bypass_breakdown": {
    "user_stated": 65,
    "identity": 12,
    "censor": 5,
    "supersede": 4,
    "contradict": 3
  }
}
```

### New Endpoint: `GET /dashboard/admission/rejected`

Paginated list of would-reject / actually-rejected facts for the review table.

```
GET /dashboard/admission/rejected?limit=50&offset=0&sort=composite_score&order=asc
```

Response:

```json
{
  "facts": [
    {
      "id": "uuid",
      "content": "The meeting went well and was productive",
      "category": "concept",
      "source": "episode_summarizer",
      "composite_score": 0.32,
      "scores": {
        "utility": 0.20,
        "confidence": 0.45,
        "novelty": 0.15,
        "recency": 0.90,
        "type_prior": 0.60
      },
      "explanation": "SHADOW_WOULD_REJECT (score=0.320, threshold=0.550): utility=0.20, confidence=0.45, novelty=0.15, recency=0.90, type_prior=0.60",
      "created_at": "2026-03-19T15:30:00Z"
    }
  ],
  "total": 203,
  "limit": 50,
  "offset": 0
}
```

---

## Data Source

All data comes from `heart.facts` table using the columns F023 already added:

- `admission_score` — composite score (NULL for pre-F023 facts)
- `source` — extraction pipeline that created the fact
- `category` — fact category
- `created_at` — for time-series

Per-dimension scores are NOT currently persisted — only the composite. To support the per-dimension breakdown views, we have two options:

### Option A: Parse from logs (cheap, fragile)
Shadow mode logs contain per-dimension scores in the explanation string.
Not recommended — log parsing for production dashboards is brittle.

### Option B: Persist per-dimension scores (recommended)

Add a JSONB column to `heart.facts`:

```sql
ALTER TABLE heart.facts
    ADD COLUMN admission_scores JSONB DEFAULT NULL;

COMMENT ON COLUMN heart.facts.admission_scores IS
    'Per-dimension A-MAC scores at admission time. {utility, confidence, novelty, recency, type_prior}';
```

This is a small schema addition (~100 bytes per fact) that unlocks all the per-dimension analytics. Without it, Sections 3, 4 (dimension columns), and 9 (simulator) can't work properly.

**Modification to F023:** In `AdmissionController.score()`, persist `scores` dict to the fact alongside `admission_score`:

```python
fact.admission_score = admission_result.composite_score
fact.admission_scores = admission_result.scores  # NEW — JSONB
```

---

## Navigation Update

Add to dashboard sidebar:

```
🧠 Nous Dashboard
─────────────────
📊 Overview          #/overview
🕸️ Knowledge Graph   #/graph
📚 Memory Browser    #/browser
🎯 Decisions         #/decisions
⚡ Activity          #/activity
📈 Graph Health      #/health
🔬 Admission Control #/admission   ← NEW
```

---

## File Changes

```
nous/
├── api/
│   └── dashboard_queries.py     (add: admission aggregation queries)
│   └── rest.py                  (add: 2 new endpoints)
├── heart/
│   └── admission.py             (modify: persist per-dimension scores)
├── sql/
│   └── init.sql                 (add: admission_scores JSONB column)

static/
└── dashboard/
    ├── index.html               (add: admission nav item)
    ├── js/
    │   ├── app.js               (add: admission route)
    │   └── admission.js         (new: admission view)
```

---

## Implementation Plan

### Single PR (~4-5 hours)

1. **Schema** (~30 min)
   - Migration: add `admission_scores JSONB` to `heart.facts`
   - Modify F023 `AdmissionController.score()` to persist per-dimension scores

2. **API** (~1.5 hours)
   - `GET /dashboard/admission` — aggregation queries (score histogram, by-source, by-category, daily trends, bypass breakdown, dimension stats)
   - `GET /dashboard/admission/rejected` — paginated rejected facts list
   - Add to dashboard_queries.py

3. **Frontend** (~2.5 hours)
   - `admission.js` — all 9 sections
   - Score distribution histogram (Chart.js)
   - Per-dimension box plots (Chart.js)
   - Rejected facts table with expand/sort
   - By-source and by-category bar charts
   - Daily trend line charts
   - Bypass doughnut
   - Threshold simulator slider (client-side only)
   - Shadow mode banner

4. **Navigation** (~15 min)
   - Add `#/admission` route to app.js
   - Add sidebar nav item

---

## Success Criteria

1. All 9 sections render with real data from shadow mode
2. Would-reject list is reviewable and sortable
3. Threshold simulator works client-side without API calls
4. View loads in < 2 seconds
5. Tim can answer "should I flip to enforcement?" by looking at the dashboard

---

## Relationship to F021 Open Question #5

F021 spec listed "Admission control dashboard — F023-specific: admission scores, rejection rates, score distributions" as a Future Enhancement. This spec promotes it from future to now, since F023 is live in shadow mode and the data exists.

---

## References

- **F023:** Memory Admission Control spec (v3, live in shadow mode)
- **F021:** Memory Dashboard spec (v2, PR #156)
- **Zhang et al. (2026):** A-MAC paper — threshold 0.55, type_prior most influential
- **A-MAC code:** github.com/GuilinDev/Adaptive_Memory_Admission_Control_LLM_Agents
