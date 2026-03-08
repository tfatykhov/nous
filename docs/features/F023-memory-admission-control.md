# F023 — Memory Admission Control

> **Status:** Planned
> **Priority:** P1
> **Author:** Nous (spec), Tim (requirements)
> **Created:** 2026-03-08
> **Depends on:** F002 (Heart Module — `FactManager.learn()`), F017 (Context Quality Gate — usage tracking)
> **Research:** 016 — Agent Memory Synthesis (Gap G1: Memory Evolution), A-MAC paper (Mar 2026, Workday AI)
> **Inspired by:** A-MAC (Adaptive Memory Admission Control) — 5-factor scoring for memory admission

---

## Problem Statement

Nous stores every fact the LLM says to store. There is no quality gate on memory **admission**.

**What exists today:**
- `learn_fact()` is called by the LLM whenever it decides something is worth remembering
- Near-duplicate detection (cosine > 0.95) prevents exact repeats
- Contradiction detection (cosine 0.85–0.95) emits warnings but **does not block storage**
- Subject-based supersession replaces old facts about the same subject

**What's missing:**
- **No hallucination check** — if the LLM fabricates a fact, it enters long-term storage unchallenged. There is zero verification that the fact is grounded in actual conversation content.
- **No utility assessment** — a transient observation ("the API returned a 200") is stored with the same weight as a durable preference ("Tim prefers Celsius").
- **No novelty gate beyond dedup** — a fact that's 90% similar to an existing one passes dedup (which requires >0.95) but adds almost nothing new.
- **No type-based admission policy** — all categories (preference, technical, person, tool, concept, rule) are treated identically at admission time.

**Result:** Memory grows without curation. Over time, low-value facts dilute recall quality. The context quality gate (F017) tries to filter at retrieval time, but the damage is done — noise is already in the database competing for embedding similarity scores.

**The principle:** It's cheaper to prevent a bad memory than to filter it out later. Admission control is the first line of defense.

---

## Research Backing

### A-MAC (March 4, 2026 — Workday AI)

A-MAC proposes 5-factor composite scoring for memory admission:

| Factor | What it measures | Method | Latency |
|--------|-----------------|--------|---------|
| **Utility (𝒰)** | "Will this be useful in future?" | Single LLM call, temp=0, cached | ~2580ms |
| **Confidence (𝒞)** | "Is this grounded in conversation?" | ROUGE-L against recent turns | ~18ms |
| **Novelty (𝒩)** | "Do we already know this?" | Cosine similarity via Sentence-BERT | ~32ms |
| **Recency (ℛ)** | "How fresh is the source?" | Exponential decay, λ=0.01/hr | <1ms |
| **Type Prior (𝒯)** | "What kind of information is this?" | Rule-based pattern matching | ~14ms |

**Key findings:**
- **Type Prior is the single most impactful factor** — removing it drops F1 by 0.107
- Composite score with threshold θ*=0.55 achieves F1=0.583, precision=0.417, recall=0.972
- 31% faster than A-MEM (2644ms vs 3831ms)
- The LLM utility call is the bottleneck; all other factors are rule-based and fast

### Relevance to Nous

| A-MAC Factor | Nous Equivalent | Gap |
|-------------|----------------|-----|
| Utility (𝒰) | F017 usage tracking (post-hoc) | No pre-admission utility assessment |
| Confidence (𝒞) | None | **Critical gap — zero hallucination defense** |
| Novelty (𝒩) | Dedup at 0.95 | Threshold too high — 0.90 similarity passes |
| Recency (ℛ) | F017 staleness decay (retrieval-time) | Not applied at admission |
| Type Prior (𝒯) | `category` field on facts | Exists but unused for admission decisions |

---

## Design

### Admission Pipeline

New facts pass through a scoring pipeline **before** being written to the database. The pipeline slots into `FactManager._learn()` after embedding generation but before the `session.add(fact)` call.

```
learn_fact() called
    ↓
Generate embedding (existing)
    ↓
Near-duplicate check (existing, cosine > 0.95)
    ↓
┌─────────────────────────────────┐
│  F023 ADMISSION SCORING         │
│                                 │
│  1. Confidence (𝒞) — grounding  │
│  2. Novelty (𝒩) — redundancy    │
│  3. Type Prior (𝒯) — category   │
│  4. Recency (ℛ) — source age    │
│  5. Utility (𝒰) — usefulness    │
│                                 │
│  Score = weighted sum            │
│  Score < θ → REJECT             │
│  Score ≥ θ → ADMIT              │
└─────────────────────────────────┘
    ↓
Contradiction check (existing)
    ↓
Store to database
```

### Factor Details

#### 1. Confidence / Grounding Check (𝒞)

**Purpose:** Detect hallucinated facts that aren't grounded in actual conversation.

**Method:** Compare candidate fact text against recent conversation turns using ROUGE-L (longest common subsequence ratio). If the fact content has no textual support in the conversation, it's likely hallucinated.

**Implementation:**
```python
def compute_grounding_score(fact_content: str, recent_messages: list[str]) -> float:
    """ROUGE-L of fact against recent conversation turns.
    
    Returns max ROUGE-L score across all recent messages.
    Uses the last N messages from working memory / episode transcript.
    """
    best_score = 0.0
    fact_tokens = fact_content.lower().split()
    for message in recent_messages:
        msg_tokens = message.lower().split()
        lcs_len = _longest_common_subsequence(fact_tokens, msg_tokens)
        # ROUGE-L recall: LCS / len(fact)
        if len(fact_tokens) > 0:
            score = lcs_len / len(fact_tokens)
            best_score = max(best_score, score)
    return best_score
```

**Configuration:**
- `admission_grounding_window`: Number of recent messages to check (default: 20)
- Source: Working memory conversation turns, or episode transcript

**Edge cases:**
- Facts with `source="system"` or `source="migration"` bypass grounding (they don't come from conversation)
- Facts created by `run_python` inline `learn_fact()` bypass grounding (already programmatic)
- Grounding score of 0.0 doesn't auto-reject — it heavily penalizes but the composite score decides

#### 2. Novelty Score (𝒩)

**Purpose:** Catch near-redundant facts that pass the 0.95 dedup threshold but add minimal new information.

**Method:** Cosine similarity against top-1 existing fact. The novelty score is inversely proportional to similarity.

**Implementation:**
```python
def compute_novelty_score(embedding: list[float], session: AsyncSession) -> float:
    """1.0 = completely novel, 0.0 = exact duplicate.
    
    Uses the existing nearest-neighbor search infrastructure.
    Novelty = 1 - max_similarity(existing_facts)
    """
    # Reuse existing _find_duplicate infrastructure with lower threshold
    top_match = await _find_nearest(embedding, session, threshold=0.70)
    if top_match is None:
        return 1.0  # Nothing similar exists
    return 1.0 - top_match.similarity
```

**Key difference from existing dedup:** Dedup is binary (>0.95 = reject). Novelty is continuous (0.70–0.95 range penalizes proportionally). A fact at 0.92 similarity still gets stored if other factors are strong enough.

#### 3. Type Prior (𝒯)

**Purpose:** Weight admission by information category. Preferences and identity facts are almost always worth storing. Transient observations have a higher bar.

**Implementation:**
```python
TYPE_PRIORS = {
    "preference": 0.95,   # Almost always store — durable, personal
    "person": 0.90,       # People info is high-value, rarely transient
    "rule": 0.90,         # Rules are explicitly declared, always store
    "technical": 0.70,    # Depends — architecture decisions vs. debug output
    "tool": 0.60,         # Tool facts often become stale
    "concept": 0.65,      # Concepts vary — some are core, some are tangential
    None: 0.50,           # Uncategorized gets no boost
}

def compute_type_prior(category: str | None) -> float:
    return TYPE_PRIORS.get(category, 0.50)
```

**Rationale:** A-MAC found Type Prior to be the single most impactful factor. This aligns with Nous's existing category system — the categories exist, they're just not used for admission.

#### 4. Recency Score (ℛ)

**Purpose:** Information from the current conversation is more likely to be relevant than information extracted from stale context.

**Method:** Exponential decay based on how long ago the source conversation turn occurred.

**Implementation:**
```python
def compute_recency_score(source_timestamp: datetime | None) -> float:
    """Exponential decay with half-life of 24 hours.
    
    Facts from the current conversation score ~1.0.
    Facts extracted from day-old context score ~0.5.
    Facts from week-old context score ~0.008.
    """
    if source_timestamp is None:
        return 0.75  # Unknown age, neutral score
    age_hours = (datetime.now(UTC) - source_timestamp).total_seconds() / 3600
    half_life_hours = 24.0
    decay_lambda = 0.693 / half_life_hours  # ln(2) / half_life
    return math.exp(-decay_lambda * age_hours)
```

**Note:** This is different from F017's staleness decay (14-day half-life for retrieval scoring). Admission recency is much more aggressive (24h half-life) because it's about the freshness of the **source**, not the age of the stored fact.

#### 5. Utility Score (𝒰) — Phase 2 only

**Purpose:** LLM-assessed prediction of whether this fact will be useful in future interactions.

**Why Phase 2:** This requires an LLM call (~2500ms latency) and adds cost. Phases 1 implements the 4 rule-based factors first. If admission quality is sufficient without it, this factor can remain optional.

**Implementation (Phase 2):**
```python
UTILITY_PROMPT = """Rate the long-term utility of this fact for a personal AI assistant.
Consider: Will the user need this information again? Is it actionable? Is it specific to a moment or enduring?

Fact: {fact_content}
Context: {recent_context_summary}

Rate 0.0 (completely ephemeral) to 1.0 (critical to remember). Return ONLY the number."""

async def compute_utility_score(fact_content: str, context: str, llm: LLMHandler) -> float:
    response = await llm.generate(UTILITY_PROMPT.format(...), temperature=0)
    return float(response.strip())
```

### Composite Score & Threshold

```python
# Phase 1 weights (no utility factor)
WEIGHTS_P1 = {
    "grounding": 0.30,   # Most important — anti-hallucination
    "novelty": 0.25,     # Second — prevent redundancy
    "type_prior": 0.25,  # Third — category-based policy
    "recency": 0.20,     # Fourth — freshness bonus
}

# Phase 2 weights (with utility factor)
WEIGHTS_P2 = {
    "grounding": 0.25,
    "novelty": 0.20,
    "type_prior": 0.20,
    "recency": 0.10,
    "utility": 0.25,
}

ADMISSION_THRESHOLD = 0.45  # Below this, reject

def compute_admission_score(factors: dict[str, float], weights: dict[str, float]) -> float:
    return sum(factors[k] * weights[k] for k in weights)
```

**Threshold rationale:** A-MAC uses 0.55. Nous uses 0.45 because:
- Single-user system — false rejections are more costly than noise (Tim can't easily re-teach facts)
- Lower initial threshold with plan to tighten based on empirical data
- Configurable via `admission_threshold` setting

### Rejection Behavior

When a fact is rejected:

1. **Log the rejection** with full factor breakdown (for calibration)
2. **Emit event** `fact_rejected` with scores (for event bus handlers / metrics)
3. **Return a modified FactDetail** with `admitted=False` and reason string
4. **The LLM sees the rejection** — tool response says "Fact not stored (admission score 0.38 < 0.45). Reasons: low grounding (0.22), low novelty (0.15). If this fact is important, you can override with confidence=1.0"

**Override mechanism:** If `confidence=1.0` is explicitly set by the LLM, bypass admission scoring. This is the escape hatch for facts that the LLM is certain about but that fail grounding (e.g., inferences, computations, or facts from web research that aren't in conversation turns).

---

## Implementation Phases

### Phase 1 — Rule-Based Admission (4 factors)

**Scope:** Implement grounding, novelty, type prior, and recency scoring in `FactManager._learn()`.

**Changes:**
- New file: `nous/heart/admission.py` — all scoring functions, weights, threshold
- Modified: `nous/heart/facts.py` — `_learn()` calls admission scoring after embedding generation
- Modified: `nous/api/tools.py` — `learn_fact` response includes admission score when relevant
- New config: `admission_enabled` (default: `true`), `admission_threshold` (default: `0.45`), `admission_grounding_window` (default: `20`)

**Integration point in `_learn()`:**
```python
async def _learn(self, input, exclude_ids, check_contradictions, session, **kwargs):
    # Generate embedding (existing)
    embedding = await self._generate_embedding(input.content)
    
    # Dedup check (existing)
    if embedding is not None:
        dupe = await self._find_duplicate(embedding, exclude_ids, session)
        if dupe is not None:
            return await self._confirm(dupe.id, session)
    
    # >>> F023: Admission scoring (NEW) <<<
    if self._admission_enabled and not self._bypass_admission(input):
        score, factors = await self._compute_admission_score(
            input, embedding, session
        )
        if score < self._admission_threshold:
            return self._rejected_detail(input, score, factors)
    
    # Store fact (existing)
    fact = Fact(...)
    session.add(fact)
    ...
```

**Bypass conditions** (admission scoring is skipped when):
- `admission_enabled` is False (global toggle)
- `input.confidence == 1.0` (explicit override by LLM)
- `input.source` is `"system"`, `"migration"`, or `"initiation"` (non-conversational)
- `input.category == "rule"` (rules are always explicit declarations)

**Estimated effort:** ~200 LOC in `admission.py`, ~40 LOC changes in `facts.py`, ~15 LOC in `tools.py`

**Testing:**
- Unit tests for each scoring function with known inputs/outputs
- Integration test: learn a fact with low grounding → verify rejection
- Integration test: learn a fact with confidence=1.0 → verify bypass
- Regression test: existing learn_fact behavior unchanged when admission_enabled=False

### Phase 2 — LLM Utility Assessment

**Scope:** Add the 5th factor (Utility) with an LLM call. Rebalance weights.

**Changes:**
- Modified: `nous/heart/admission.py` — add `compute_utility_score()`, update weights to `WEIGHTS_P2`
- New config: `admission_utility_enabled` (default: `false`), `admission_utility_model` (default: same as main model)

**Cost control:**
- Utility call uses a small/fast model (configurable)
- Result is cached by fact content hash (same fact text → same utility score)
- Can be disabled independently (`admission_utility_enabled=false` falls back to Phase 1 weights)

**Estimated effort:** ~60 LOC

### Phase 3 — Calibration & Feedback Loop

**Scope:** Use F017's usage tracking data to validate and tune admission decisions.

**The insight:** F017 tracks which recalled memories the model actually references. This is ground truth for utility. A fact that was admitted but never referenced in any response is evidence the admission threshold should be higher. A fact that was rejected but would have been useful (counterfactual) is evidence the threshold should be lower.

**Changes:**
- New: `nous/heart/admission_calibration.py` — periodic analysis of admitted facts vs. usage data
- New event handler: `on_admission_calibration` — runs weekly, computes:
  - Precision: what % of admitted facts have been referenced at least once?
  - Type-Prior accuracy: which categories have the best/worst admission-to-usage ratio?
  - Threshold recommendation: based on F1 optimization over historical data
- New: `admission_calibration_report` — stored as an episode, surfaced in weekly growth report (F007)

**Estimated effort:** ~150 LOC

### Phase 4 — Contradiction-Aware Admission

**Scope:** Integrate admission scoring with F022's contradiction detection edges.

When a new fact has high similarity to an existing fact (0.85–0.95 range), the current system emits a warning. With F022 Phase 3 (contradiction detection), it would create a `contradicts` or `supersedes` edge.

F023 Phase 4 adds admission logic for contradictions:
- If new fact **supersedes** old fact AND scores higher on admission → admit new, mark old as superseded
- If new fact **contradicts** old fact AND scores lower → reject new, keep old
- If scores are close → admit both, flag for user review

**Depends on:** F022 Phase 3

**Estimated effort:** ~80 LOC

---

## Configuration

```python
# admission.py defaults
ADMISSION_CONFIG = {
    "admission_enabled": True,              # Global toggle
    "admission_threshold": 0.45,            # Composite score floor
    "admission_grounding_window": 20,       # Recent messages to check for grounding
    "admission_utility_enabled": False,     # Phase 2 LLM utility scoring
    "admission_utility_model": None,        # Model for utility calls (None = default)
    "admission_log_rejections": True,       # Log rejected facts with full breakdown
    "admission_bypass_confidence": 1.0,     # Confidence level that bypasses scoring
}

# Type priors (tunable)
TYPE_PRIORS = {
    "preference": 0.95,
    "person": 0.90,
    "rule": 0.90,
    "technical": 0.70,
    "concept": 0.65,
    "tool": 0.60,
    None: 0.50,
}

# Phase 1 weights
WEIGHTS = {
    "grounding": 0.30,
    "novelty": 0.25,
    "type_prior": 0.25,
    "recency": 0.20,
}
```

---

## Metrics & Observability

| Metric | What it measures | Where |
|--------|-----------------|-------|
| `facts_admitted` | Count of facts passing admission | Event bus counter |
| `facts_rejected` | Count of facts failing admission | Event bus counter |
| `admission_score_histogram` | Distribution of admission scores | Weekly calibration report |
| `rejection_reasons` | Breakdown by which factor caused rejection | Logged per rejection |
| `grounding_score_avg` | Average grounding score across all candidates | Trend tracking |
| `false_rejection_rate` | Facts rejected that would have been useful | Phase 3 calibration |

---

## Migration & Rollout

**Phase 1 rollout strategy:**

1. **Week 1: Shadow mode** — compute admission scores but don't reject anything. Log all scores. This builds baseline data without risk.
2. **Week 2: Soft mode** — reject facts below threshold but include them in a `rejected_facts` staging table (retrievable if needed). The LLM is told facts were rejected.
3. **Week 3: Full mode** — rejected facts are not stored. Override via confidence=1.0 remains available.

**Rollback:** Set `admission_enabled=False` in config. Zero code changes needed. All existing facts are unaffected.

**Schema changes:** None for Phase 1. The existing `Fact` model is unchanged. Rejection metadata is logged via events, not schema columns.

Phase 3 adds an optional `admission_score` column to the `facts` table for historical analysis:
```sql
ALTER TABLE heart.facts ADD COLUMN admission_score FLOAT;
ALTER TABLE heart.facts ADD COLUMN admission_factors JSONB;
```

---

## Interaction with Other Features

| Feature | Interaction |
|---------|------------|
| **F002 (Heart)** | Direct modification of `FactManager._learn()` |
| **F016 (Context Pruning)** | Pre-prune fact extraction feeds into learn_fact — admission scoring applies to extracted facts too |
| **F017 (Quality Gate)** | Usage tracking provides ground truth for admission calibration (Phase 3). Complementary: F017 filters at retrieval, F023 filters at admission |
| **F022 (Graph Recall)** | Phase 4 integrates with contradiction edges. Admitted facts get auto-linked via graph edges |
| **F008 (Memory Lifecycle)** | Admission is the first lifecycle gate. Facts that pass admission enter the normal lifecycle (confirm → archive → retire) |

---

## What We're NOT Building

- **Full A-MAC replication** — A-MAC is designed for multi-user benchmarks with grid-search weight optimization. Nous is single-user. We take the architecture, not the tuning methodology.
- **RL-based admission** — MemexRL trains admission as a learnable action. Interesting but overkill for Nous's scale. Rule-based + calibration feedback is sufficient.
- **Blocking all low-grounding facts** — Some valuable facts are inferences or computations that won't appear in conversation text. The confidence=1.0 override exists for this reason.
- **Retroactive scoring of existing facts** — Existing facts stay. Admission applies only to new facts going forward. (A retroactive cleanup pass could be a separate task.)

---

## Open Questions

1. **Grounding window size.** 20 messages is a guess. Too small → misses context from earlier in conversation. Too large → slow and may match coincidentally. Need empirical data from shadow mode.

2. **Override UX.** When a fact is rejected, the LLM is told it can retry with confidence=1.0. But will the LLM actually do this appropriately, or will it just always override? May need a censor or rate limit on overrides.

3. **Web research facts.** Facts extracted from web_fetch/web_search aren't in conversation turns — they're in tool output. Grounding check needs to include recent tool outputs, not just user/assistant messages. This affects the grounding window implementation.

4. **Category assignment accuracy.** Type Prior is the most impactful factor, but it depends on correct category assignment. Currently the LLM chooses the category. If it miscategorizes, the type prior is wrong. Should we validate category assignment as part of admission?

5. **Threshold tuning timeline.** How many rejected/admitted facts do we need before calibration data is meaningful? Estimate: ~100 admission events (at ~2-5 facts/day, that's 3-6 weeks of shadow mode data).

6. **Pre-prune extraction interaction.** F016 extracts facts from tool output before pruning. These facts have unusual grounding characteristics — they're grounded in tool output, not conversation. Need special handling or a separate grounding check against tool output content.
