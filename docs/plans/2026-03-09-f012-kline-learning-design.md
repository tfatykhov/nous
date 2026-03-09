# F012 K-Line Learning — Design

**Date:** 2026-03-09
**Status:** Approved
**Feature:** F012 — Auto-create procedures (K-lines) from repeated patterns

## Overview

F012 adds automatic procedure creation to Nous. Today procedures are manual-only — the agent must explicitly call `store_procedure()`. This feature gives the agent three learning pathways that detect repeated patterns in its own experience and crystallize them into reusable procedures with Minsky's level-band structure (goals / core_patterns / core_tools / core_concepts / implementation_notes).

**Core principle:** The agent learns by noticing what it keeps doing successfully, then formalizing that into a procedure it can recall next time.

## Three Learning Pathways

### Pathway 1: Decision Clustering (Sleep)

"You solved X the same way 3+ times — here's a procedure."

**Trigger:** Sleep cycle generalize phase.

**Algorithm:**
1. Query all reviewed decisions since last sleep with `outcome = "success"` or `"partial"`
2. Group by bridge function similarity (>0.85 cosine distance on bridge embeddings)
3. For clusters of 3+ decisions:
   - Check recency: at least 1 decision from last 7 days
   - Check success rate: >70% successful within cluster
   - Dedup: search existing procedures at >0.85 similarity — if match, update existing instead
4. For qualifying clusters, call background LLM to extract procedure (see LLM Extraction below)
5. Store via `heart.store_procedure()` with `created_by = "auto:decision_cluster"`

**Update path (dedup match):** Enrich existing procedure — merge new core_patterns, tools, notes via LLM call.

**Cap:** Max 2 procedures from decision clustering per sleep cycle.

### Pathway 2: Episode Lesson Learning (Sleep)

"You learned this lesson across 3+ sessions — codify it."

**Trigger:** Sleep cycle generalize phase (runs after decision clustering).

**Algorithm:**
1. Query episodes since last sleep with `outcome = "completed"` or `"resolved"` that have non-empty `lessons_learned`
2. Embed each lesson, group by similarity (>0.80 cosine distance)
3. Also check against lessons from older episodes (last 30 days) to catch slow-building patterns
4. For clusters of 3+ similar lessons:
   - Dedup against existing procedures (>0.85 similarity)
   - If match → update path (merge new lessons into existing procedure)
   - If no match → LLM extraction

Episode lessons tend to be more reflective and higher-level ("always check X before Y") vs decision clusters which are more operational ("when facing X, do Y with tool Z"). Different level-band emphasis, both valuable.

**Cap:** Max 1 procedure from episode lessons per sleep cycle (combined cap of 3 total with pathway 1).

### Pathway 3: Monitor Recovery Learning (Real-time)

"You hit error X and recovered with Y three times this session — procedure."

**Trigger:** Real-time, during `monitor.learn()` after each turn.

**Mechanism:**
1. When monitor detects a tool error followed by a successful recovery in the same session, record the pair `(error_pattern, recovery_action)` in working memory
2. Working memory is session-scoped — add `error_recovery_pairs: list[dict]` key
3. On each new error→recovery pair, check if this error pattern has occurred 3+ times this session
4. On 3rd match:
   - Dedup against existing procedures (>0.85 similarity)
   - If match → increment activation_count on existing procedure
   - If no match → LLM extraction to create recovery procedure

**Guards:**
- Max 1 procedure per session from monitor pathway
- Only fires on genuine error→recovery pairs (not just errors, not just successes)
- Error pattern matching uses existing `trigger_pattern` logic from censor creation

**Relationship to censors:** Censors say "don't do X." Recovery procedures say "when X happens, do Y." Complementary — the monitor already creates censors from errors, now it also creates procedures from recoveries.

## Timing: Hybrid Approach

- **Pathways 1 & 2 (sleep):** Run during sleep handler's "generalize" phase (currently empty, waiting for this feature). Batched processing benefits from accumulated data.
- **Pathway 3 (real-time):** Fires during `monitor.learn()` because error recovery is time-sensitive — the agent shouldn't wait for sleep to learn a fix it keeps needing.

## LLM Extraction

All pathways use `NOUS_BACKGROUND_MODEL` for structured extraction. One LLM call per procedure candidate.

**Decision cluster prompt:**
```
Given these {N} similar successful decisions, extract a reusable procedure.

Decisions:
{decision descriptions, contexts, outcomes, bridge structure+function}

Output a procedure with:
- name: short descriptive name
- domain: category/domain this applies to
- description: when and why to use this procedure
- goals: what this procedure achieves (upper fringe)
- core_patterns: the repeatable approach (core)
- core_tools: tools/techniques involved (core)
- core_concepts: key ideas to keep in mind (core)
- implementation_notes: specific details (lower fringe)
```

**Episode lesson prompt:**
```
Given these {N} episodes with similar lessons, extract a reusable procedure.

Episodes:
{summary, outcome, lessons_learned, tags}

Output a procedure with:
- name, domain, description
- goals: what situations this helps with
- core_patterns: the approach that kept working
- core_tools: tools/techniques mentioned
- core_concepts: the underlying insight
- implementation_notes: caveats, edge cases observed
```

**Monitor recovery prompt:**
```
The agent encountered this error pattern 3 times and recovered the same way each time.

Error pattern: {trigger_pattern}
Recovery actions: {list of successful recovery steps}
Context: {what the agent was doing}

Extract a recovery procedure with:
- name, domain, description
- goals: when to apply this recovery
- core_patterns: the recovery steps
- core_tools: tools used in recovery
- core_concepts: why this recovery works
- implementation_notes: edge cases, when NOT to use this
```

## Procedure Lifecycle & Review

### Reinforcement

When context assembly surfaces a procedure and the turn succeeds → `activate()` + `record_outcome("success")`. When turn fails → `record_outcome("failure")`. Wire this in `cognitive/layer.py` post_turn — track which procedures were in context, record outcomes.

### Staleness Flagging

During sleep "review" phase, query procedures not activated in 30+ days. Add to review queue alongside low-effectiveness procedures.

### Weak Procedure Review (Sleep)

Query procedures with effectiveness < 0.3 OR not activated in 30+ days. Feed to background LLM:

```
This auto-learned procedure has low effectiveness or hasn't been used recently.

Procedure: {name, domain, description, core_patterns}
Stats: {activation_count, success_count, failure_count, effectiveness, last_activated}

Should this procedure be:
A) KEPT — still valuable, just hasn't been needed
B) REVISED — the core insight is good but needs updating (provide revision)
C) RETIRED — no longer useful, retire it
```

- KEPT → no action
- REVISED → apply LLM-provided updated fields
- RETIRED → `procedure.retire()` (soft delete)

**Cap:** Review max 3 weak procedures per sleep cycle.

### Observability

- `created_by` tags: `auto:decision_cluster`, `auto:episode_lesson`, `auto:monitor_recovery`
- Existing effectiveness metric tracks quality over time
- Sleep handler logs procedure learning activity (created N, updated M, retired K)

## Architecture

### New Components

```
nous/handlers/procedure_learner.py    # Sleep-cycle handler (pathways 1 & 2)
nous/cognitive/monitor.py             # Enhancement to existing learn() (pathway 3)
```

### Modified Components

```
nous/handlers/sleep_handler.py        # Wire ProcedureLearner into generalize phase
nous/cognitive/layer.py               # Track procedures in context, record outcomes post-turn
nous/config.py                        # New configuration variables
```

### No New Tables

Everything uses the existing `heart.procedures` table + event bus.

### Data Flow

```
Sleep Cycle (generalize phase)
  └── ProcedureLearner handler
       ├── find_decision_clusters(agent_id, since=last_sleep)
       │    └── Brain.query() + embedding similarity grouping
       ├── find_episode_patterns(agent_id, since=last_sleep)
       │    └── Heart.search_episodes() + lesson similarity
       ├── For each candidate cluster:
       │    ├── dedup_check() → search existing procedures (>0.85 sim)
       │    │    ├── Match found → update existing procedure
       │    │    └── No match → LLM extraction → heart.store_procedure()
       │    └── Tag created_by = "auto:decision_cluster" or "auto:episode_lesson"
       └── review_weak_procedures() → flag low-effectiveness for LLM review

Monitor (real-time, per-turn)
  └── monitor.learn() enhancement
       ├── Track error→recovery pairs in working memory
       ├── On 3rd repeat: dedup_check → LLM extraction → store_procedure()
       └── Tag created_by = "auto:monitor_recovery"
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `NOUS_PROCEDURE_LEARNING_ENABLED` | `true` | Master switch for all three pathways |
| `NOUS_PROCEDURE_CLUSTER_MIN_SIZE` | `3` | Min decisions/episodes in cluster to trigger learning |
| `NOUS_PROCEDURE_SIMILARITY_THRESHOLD` | `0.85` | Cosine similarity for clustering decisions/dedup |
| `NOUS_PROCEDURE_EPISODE_SIMILARITY` | `0.80` | Cosine similarity for episode lesson clustering |
| `NOUS_PROCEDURE_SUCCESS_RATE_MIN` | `0.70` | Min success rate in decision cluster |
| `NOUS_PROCEDURE_MONITOR_TRIGGER_COUNT` | `3` | Error→recovery repeats before real-time learning |
| `NOUS_PROCEDURE_MAX_PER_SLEEP` | `3` | Max new procedures per sleep cycle (pathways 1+2) |
| `NOUS_PROCEDURE_MAX_PER_SESSION` | `1` | Max new procedures from monitor (pathway 3) |
| `NOUS_PROCEDURE_STALENESS_DAYS` | `30` | Days inactive before flagged for review |
| `NOUS_PROCEDURE_WEAKNESS_THRESHOLD` | `0.3` | Effectiveness below which triggers review |

## Testing Strategy

**Unit tests** (~15 tests in `tests/test_procedure_learner.py`):
- Cluster detection: 2 decisions → no procedure, 3+ → procedure created
- Success rate gate: cluster with <70% success → skipped
- Recency gate: all decisions >7 days old → skipped
- Dedup: similar procedure exists → update not create
- Episode lesson grouping at 0.80 threshold
- Monitor error→recovery pair tracking in working memory
- Monitor 3rd repeat triggers learning
- Cap enforcement (max per sleep, max per session)
- LLM extraction output → valid ProcedureInput mapping

**Integration tests** (~8 tests in `tests/test_procedure_learner_integration.py`):
- Full sleep cycle with seeded decisions → procedures created in DB
- Monitor pathway: 3 identical errors with recovery → procedure in DB
- Dedup: run learning twice on same data → no duplicates
- Weak procedure review: low-effectiveness procedure → LLM decides fate
- Procedure reinforcement: procedure in context + successful turn → activation_count incremented
- End-to-end: created procedure surfaces in next context assembly

Mocking: LLM calls mocked in unit tests, real DB in integration tests (matching existing patterns).

## Thresholds Summary

| Gate | Value | Rationale |
|------|-------|-----------|
| Cluster size | 3+ | Enough repetition to indicate a pattern, not a coincidence |
| Decision similarity | 0.85 | High bar — only genuinely similar decisions cluster |
| Episode lesson similarity | 0.80 | Slightly lower — lessons are more abstractly worded |
| Success rate | >70% | Majority successful, allowing some partial outcomes |
| Recency | 1 in last 7 days | Don't learn from purely historical patterns |
| Monitor trigger | 3 repeats/session | Conservative for real-time path |
| Staleness | 30 days | Flag unused procedures for review |
| Weakness | <0.3 effectiveness | Roughly 2x more failures than successes |

All thresholds are configurable via environment variables for tuning based on real usage.
