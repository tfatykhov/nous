# F021.1 — Admission Control Dashboard View

> **Status:** Draft v2
> **Priority:** P2
> **Depends on:** F021 (Memory Dashboard), F023 (Memory Admission Control — live in shadow mode)
> **Estimated effort:** ~6–8 hours (schema migration + 2 API endpoints + 1 dashboard view with 9 sections)
> **Domain:** New `#/admission` view in existing dashboard
> **Changelog:**
> - v2: Address Emerson + Codex review — mandatory JSONB migration, bypass NULL handling, content truncation, empty state UX, scaling notes, phased implementation plan, time estimate revised

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

## Prerequisites

### Mandatory: Per-Dimension Score Persistence

Per-dimension scores are NOT currently persisted — only the composite `admission_score`. Without per-dimension data, Sections 3, 4 (dimension columns), and 9 (weight tuning) are non-functional.

**This migration MUST land first (or in the same PR).** The dashboard only shows dimension data for facts scored *after* the migration. Pre-migration facts will have `admission_scores = NULL`.

#### Schema Migration

```sql
ALTER TABLE heart.facts
    ADD COLUMN admission_scores JSONB DEFAULT NULL;

COMMENT ON COLUMN heart.facts.admission_scores IS
    'Per-dimension A-MAC scores at admission time. {utility, confidence, novelty, recency, type_prior}. NULL for pre-migration facts and bypassed facts.';
```

#### Code Modification — Fact Manager (not AdmissionController)

The `AdmissionController.score()` method computes and returns `AdmissionResult` but does NOT create the `Fact` row. The `Fact` row is created later in the **fact manager**. Therefore, per-dimension scores must be persisted at the fact manager's write point:

```python
# In fact_manager.py (or wherever Fact rows are created), NOT in admission.py:
fact.admission_score = admission_result.composite_score
if not admission_result.bypassed:
    fact.admission_scores = admission_result.scores  # JSONB dict
else:
    fact.admission_scores = None  # NULL for bypassed facts — see below
```

#### Bypassed Facts — NULL Scores

Bypassed facts return `AdmissionResult(composite_score=1.0, scores={}, bypassed=True)` — the `scores` dict is **empty**. Persisting `{}` would pollute dimension statistics (artificial 1.0 composite with no dimension data).

**Rule:** Bypassed facts get `admission_scores = NULL`. All dimension queries MUST filter:
```sql
WHERE admission_scores IS NOT NULL
```

This cleanly separates "scored and admitted" from "bypassed without scoring" in all analytics.

#### SQLAlchemy Model Update

Add the mapped column to `models.py`:
```python
admission_scores = Column(JSONB, nullable=True, default=None)
```

---

## Design

### New Dashboard View: Admission Control — `#/admission`

### Phased Implementation (Recommended)

**Phase 1 (MVP — enables shadow→enforcement decision):**
- Section 1: Shadow Mode Banner
- Section 2: Score Distribution Histogram
- Section 4: Would-Have-Been-Rejected List
- Section 9: Threshold Simulator

**Phase 2 (Analytics — deeper insight):**
- Section 3: Per-Dimension Box Plots
- Section 5: Admission by Source
- Section 6: Admission by Category
- Section 7: Trends Over Time
- Section 8: Bypass Breakdown

Phase 1 covers the critical path: "look at the histogram, review the reject list, slide the threshold, decide." Phase 2 adds diagnostic depth.

---

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

**Empty state:** If no facts have been scored yet:
```
ℹ️ No admission data yet — facts will appear here as they're scored by F023.
   Ensure NOUS_ADMISSION_ENABLED=true in your configuration.
```

#### Section 2: Score Distribution (Histogram)

- **X-axis:** Composite score (0.0 to 1.0, bucketed in 0.05 increments)
- **Y-axis:** Fact count
- **Threshold line:** Vertical red dashed line at 0.55 (or current threshold)
- **Color:** Green bars above threshold, red bars below
- **Insight:** Shows if scores cluster around the threshold (risky — small weight changes flip many facts) or are bimodal (clean separation)
- **Empty state:** "No scored facts in this time window"

**Note on threshold reclassification:** The "would_reject" count is computed at query time as `admission_score < threshold`. If the threshold config changes between scoring and viewing, counts shift retroactively. This is intended — the whole point is exploring different thresholds. The UI should display: _"Counts based on current threshold. Scores were computed at admission time."_

#### Section 3: Per-Dimension Breakdown (Box Plots)

- 5 side-by-side box plots showing min/Q1/median/Q3/max for each dimension
- Split by admitted vs would-reject
- Better for seeing spread and outliers than radar charts
- **Excludes bypassed facts** (WHERE admission_scores IS NOT NULL)
- Shows which dimensions best separate good facts from noise

#### Section 4: Would-Have-Been-Rejected List

- Table of facts that scored below threshold
- Columns: Content (truncated to 200 chars), Source, Category, Composite Score, Utility, Confidence, Novelty, Recency, Type Prior, Created At
- Sortable by any column
- Click row → expands to show full content (client-side expand, no extra API call)
- **This is THE critical view** — Tim reviews this list to gut-check: "would I miss any of these?" If yes → threshold too high. If no → safe to enforce.
- Pagination: limit=50, offset-based

#### Section 5: Admission by Source (Bar Chart)

- Grouped bar chart
- X-axis: source (knowledge_extractor, episode_summarizer, sleep_reflection, compaction_extraction, etc.)
- Y-axis: count
- Stacked or grouped: admitted vs would-reject vs bypassed
- Includes `avg_score` per source
- Shows which extraction pipelines produce the most noise

#### Section 6: Admission by Category (Bar Chart)

- Same layout as Section 5 but grouped by fact category
- X-axis: rule, preference, person, technical, tool, concept
- Includes `avg_score` per category
- Validates type_prior settings — if "concept" facts are mostly rejected, the 0.60 prior may be correct. If "technical" facts are getting rejected, 0.70 may be too low.

#### Section 7: Trends Over Time (Line Charts)

Two charts:

**7a — Daily admission rate:**
- X-axis: date (last N days, based on `days` param)
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

**Parameters:**
- `days` (integer, default: 30) — look-back window in days
- `source` (string, optional) — filter by source pipeline
- `category` (string, optional) — filter by fact category

**Empty state:** Returns zero counts, empty arrays. Frontend handles display.

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
    "avg_composite_score": 0.62,
    "threshold_note": "Counts based on current threshold (0.55). Actual scores were computed at admission time."
  },
  "score_distribution": [
    { "bucket": "0.00-0.05", "count": 3 },
    { "bucket": "0.05-0.10", "count": 5 },
    { "bucket": "0.50-0.55", "count": 28 },
    { "bucket": "0.55-0.60", "count": 35 }
  ],
  "dimension_stats": {
    "_note": "Excludes bypassed facts (admission_scores IS NULL). Only available for facts scored after JSONB migration.",
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
    "knowledge_extractor": { "admitted": 180, "rejected": 95, "bypassed": 0, "avg_score": 0.58 },
    "episode_summarizer": { "admitted": 120, "rejected": 45, "bypassed": 0, "avg_score": 0.64 },
    "sleep_reflection": { "admitted": 40, "rejected": 38, "bypassed": 0, "avg_score": 0.52 },
    "user_stated": { "admitted": 0, "rejected": 0, "bypassed": 65, "avg_score": null },
    "identity": { "admitted": 0, "rejected": 0, "bypassed": 12, "avg_score": null }
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
GET /dashboard/admission/rejected?limit=50&offset=0&sort=composite_score&order=asc&days=30
```

**Parameters:**
- `limit` (integer, default: 50)
- `offset` (integer, default: 0)
- `sort` (string, default: "composite_score") — sortable column
- `order` (string, default: "asc") — asc or desc
- `days` (integer, default: 30) — look-back window

Response:

```json
{
  "facts": [
    {
      "id": "uuid",
      "content_preview": "The meeting went well and was productive...",
      "content_full": "The meeting went well and was productive and everyone agreed on next steps",
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

**Security note:** `content_preview` is truncated to 200 characters server-side. `content_full` is included for the expand-on-click UX. The dashboard inherits the API's auth model — if the dashboard is exposed without authentication, consider removing `content_full` and requiring a separate authenticated call.

---

## Scaling Considerations

Current fact count (~1,041) makes all queries trivially fast. At 10K+ facts:
- **JSONB percentile queries** (Section 3) will be the bottleneck — extracting per-dimension values and computing `percentile_cont` across all rows
- **Mitigation options:** (a) index `admission_score` for the histogram, (b) add a partial index on `admission_scores IS NOT NULL`, (c) cache aggregations with a 5-minute TTL, (d) materialized view for dimension stats refreshed on schedule
- Not blocking for v1 but document for future reference

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
│   └── admission.py             (no changes — scoring logic unchanged)
│   └── fact_manager.py          (modify: persist admission_scores at write point)
│   └── models.py                (modify: add admission_scores JSONB column to Fact model)
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

### Phase 1 — MVP (enables shadow→enforcement decision) (~4 hours)

1. **Schema + Model** (~30 min)
   - Migration: add `admission_scores JSONB` to `heart.facts`
   - Add `admission_scores` to `Fact` model in `models.py`
   - Modify fact manager to persist per-dimension scores (NULL for bypasses)

2. **API — core endpoints** (~1.5 hours)
   - `GET /dashboard/admission` — summary, score histogram, bypass breakdown
   - `GET /dashboard/admission/rejected` — paginated rejected facts list with content truncation
   - Add to dashboard_queries.py

3. **Frontend — MVP sections** (~2 hours)
   - Section 1: Shadow Mode Banner (with empty state)
   - Section 2: Score Distribution Histogram (Chart.js)
   - Section 4: Rejected facts table with expand/sort
   - Section 9: Threshold Simulator slider (client-side only)
   - Navigation: add `#/admission` route

### Phase 2 — Analytics (~3-4 hours)

4. **API — dimension + trend queries** (~1 hour)
   - Dimension stats with JSONB extraction + percentile_cont
   - By-source and by-category aggregations
   - Daily trend aggregation

5. **Frontend — analytics sections** (~2-3 hours)
   - Section 3: Per-Dimension Box Plots
   - Section 5: Admission by Source (bar chart)
   - Section 6: Admission by Category (bar chart)
   - Section 7: Daily Trends (line charts)
   - Section 8: Bypass Breakdown (doughnut)

---

## Success Criteria

1. All 9 sections render with real data from shadow mode (Phase 2 complete)
2. Would-reject list is reviewable and sortable
3. Threshold simulator works client-side without API calls
4. View loads in < 2 seconds
5. Tim can answer "should I flip to enforcement?" by looking at the dashboard
6. Bypassed facts don't pollute dimension statistics
7. Empty state handled gracefully for fresh installs

---

## Relationship to F021 Open Question #5

F021 spec listed "Admission control dashboard — F023-specific: admission scores, rejection rates, score distributions" as a Future Enhancement. This spec promotes it from future to now, since F023 is live in shadow mode and the data exists.

---

## References

- **F023:** Memory Admission Control spec (v3, live in shadow mode)
- **F021:** Memory Dashboard spec (v2, PR #156)
- **Zhang et al. (2026):** A-MAC paper — threshold 0.55, type_prior most influential
- **A-MAC code:** github.com/GuilinDev/Adaptive_Memory_Admission_Control_LLM_Agents
