# F075 Implementation Plan — Architecture Review

**Reviewer:** Architecture (feature-dev:code-architect)
**Plan under review:** `docs/superpowers/plans/2026-05-28-f075-temporal-fact-extraction.md`
**Spec referenced:** `docs/features/F075-temporal-fact-extraction.md` v2.17 (merged PR #460 / `0115568`)
**Date:** 2026-05-28

---

## Executive Summary

The plan is structurally sound: the 12-phase order is correct, all 15 spec wire-path rows are tracked within ±2 lines, and no hidden cross-phase dependencies exist. Phase sequencing passes. File:line fidelity passes.

Two P1 defects will silently ship broken feature behavior if not fixed before any code lands.

**P1 #1 — Layer 2 ships completely dark.** Phase 7 builds `happened_before` edges but no phase anywhere in the plan flips `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED` from its default `False` to `True`. The consumer (`_apply_graph_adjacency_boost` at `retrieval_pipeline.py:243-247`) is already-shipped code — it just needs the flag. Without the flip, every edge Phase 7 builds is dead weight and the BEAM temporal_reasoning score is unchanged. Spec line 443 explicitly marks this as a required impl-plan step.

**P1 #2 — Same-subject temporal facts still collapse.** Phase 5.2 adds the cosine-dedup bypass for distinct event_dates, which is correct. But the cosine bypass causes execution to fall through to `_supersede_by_subject` at `facts.py:453-458`. For canonical temporal_reasoning pairs — "API key obtained March 10" and "API key obtained March 12" — subjects are identical and `_supersede_by_subject` fires, deactivating the first fact. The date pair temporal_reasoning needs is destroyed. Spec §Plus (lines 322-326) explicitly mandates the bypass at both sites. Phase 5.2 covers the cosine site only.

Three P2 gaps: the wrong file named for `_to_recall_result` (it's `heart.py:1085`, not `facts.py`); a Phase 0 gate criterion that contradicts itself (table says "≥0.5", prose says "all 3 checks pass", diagnostic is binary); and an underestimated Phase 0 adaptation scope (hardcoded constants throughout, needs ~50-80 LOC non-trivial refactoring).

---

## P1 Findings

### P1 #1: Layer 2 ships 100% dark

Phase 7 builds `happened_before` edges and wires `_build_happened_before_edges()` into `run_backfill_cycle`. No phase mentions flipping `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED`.

Spec line 443: "**Required impl-plan step:** flip `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED` from `false` to `true` (current default in `config.py:1055-1056`). Without this flip Layer 2 ships dark."

The consumer is `_apply_graph_adjacency_boost` at `retrieval_pipeline.py:243-247, 699-738`. Default `False`. With flag at default, every edge Phase 7 builds is dead weight. BEAM temporal_reasoning improvement = 0.

**Fix.** Add a step to Phase 7 (or Phase 10): flip `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED=true` in `.env` / deployment config; add env-var row to CLAUDE.md. Phase 9 integration test: with boost on, the later-dated fact from a chain ranks above an unrelated fact at similar base score; with boost off, it does not.

### P1 #2: `_supersede_by_subject` bypass missing

Phase 5.2 adds the cosine-dedup bypass inside `_learn`. Correct. But when the bypass fires (dates differ → do NOT return), execution continues past `facts.py:381`, hits `session.add(fact)` at `facts.py:450`, and then hits:

```python
# facts.py:453-458
if check_contradictions and input.subject and embedding is not None:
    await self._supersede_by_subject(
        fact.id, input.subject, embedding, session,
        new_content=input.content,
    )
```

For "API key obtained on March 10" → "API key obtained on March 12": embedding similarity is high AND subjects are identical. `_supersede_by_subject` fires, writes a `supersedes` edge, deactivates the March-10 fact. Date pair destroyed.

Spec §Plus lines 322-326 require bypass at BOTH:
1. `Heart._learn` dedup check (Phase 5.2 covers this)
2. **Subject-based supersession path** — entirely absent from the plan

**Fix.** Add Phase 5.2b: inside `_learn`, immediately before `_supersede_by_subject` call at `facts.py:453-458`, guard with the event_date rule. Concretely, modify `_supersede_by_subject` to accept the new fact's `event_date` and skip deactivation of any candidate whose `event_date` differs from the new fact's. Add `test_supersession_skipped_when_both_facts_have_different_dates`.

---

## P2 Findings

### P2 #1: Phase 5.5 names the wrong file

Phase 5.5 says edit `nous/heart/facts.py` `_to_recall_result`. Spec wire-path row 10 same. Actual location: `nous/heart/heart.py:1085`. FactSummary branch at `heart.py:1099-1110`. There is no `_to_recall_result` in `facts.py`.

Both the plan and the spec name the wrong file. Wire-path row 10 survived 17 codex rounds — codex couldn't catch this because it can't run grep without explicit reference to where to look.

**Fix.** Change Phase 5.5: "Edit `nous/heart/heart.py:1099-1110` (FactSummary branch of `_to_recall_result` in `Heart` class at `heart.py:1085`). Add `event_date=item.event_date.isoformat() if item.event_date else None` to the metadata dict." Document the spec drift inline.

### P2 #2: Phase 0 gate criterion contradicts itself

Table row: "0.000 → ≥0.5 with single dated fact injected"
Prose: "all 3 cases produce answers that satisfy their rubric"

`diag_synthetic_temporal_validate.py` uses binary string matching (`"march 10" in tl`, `"2 days" in tl`) — three YES/NO checks. There is no numeric score. The "≥0.5" criterion would allow 2/3 failures.

**Fix.** Table gate: "All 3 auto-checks (CONFIRMED verdict) pass for all 3 conv/question pairs. Any MIXED/FAILED halts implementation."

### P2 #3: Phase 0 adaptation scope underestimated

Current hardcoded in `diag_synthetic_temporal_validate.py`:
- `AGENT_ID = "beam-100K-conv-002"`
- `QUESTION` — conv 2 Q0 specific
- Auto-check keywords — conv 2 semantics

Plus: BEAM source file lookup for conv 4/5 question text + expected date string extraction. ~50-80 LOC of non-trivial refactoring, not minor. If BEAM sources lack explicit dates for conv 4/5, the gate becomes non-deterministic.

**Fix.** Phase 0 step 1: before adapting, read BEAM sources for conv 4 + conv 5 question text + expected date strings. If sources don't contain explicit dates, gate is non-deterministic — flag risk and document fallback.

---

## P3 Findings

### P3 #1: Missing test for `_supersede_by_subject` bypass

After P1 #2 is fixed, add `test_supersession_skipped_when_both_facts_have_different_dates`. Distinct from the cosine-path test.

### P3 #2: Phase 9 integration test doesn't verify ranking change

Add: with boost ON and a third unrelated fact at similar base score, later-dated fact ranks above unrelated. With boost OFF, it does not.

### P3 #3: Open Question 3 already resolvable

`call_background_llm_structured` confirmed at `nous/handlers/__init__.py:86`. Remove from Open Questions.

---

## Phase sequencing: SOUND

| Dependency check | Verdict |
|---|---|
| Phase 2 (schemas) before Phase 1 (ORM)? | Phase 2 testable on pure Python, no DB needed — but ORM persistence requires Phase 1. Correct order. |
| Phase 5 (Heart) before Phase 7 (edges)? | Yes — learn/dedup paths don't require graph edges. Correct. |
| Phase 6 (pipeline) before Phase 7? | Yes — metadata copy requires only Phase 5's recall changes. Correct. |
| Phase 8 (backfill) requires Phase 1? | Yes, correct — reads `event_date_classified_at IS NULL`. |
| Phase 9 (integration) after Phase 7? | Yes, correct — checks `happened_before` edges. |

No hidden cross-phase dependencies.

---

## File:line fidelity: SOUND (14/15 within ±2 lines)

| Row | Spec/plan claim | Actual | Status |
|---|---|---|---|
| 3 | `models.py` Fact ORM 469-511 | 469-511 | ✓ |
| 7 | `schemas.py` FactSummary 151-167 | 151-167 | ✓ |
| 10 | `facts.py _to_recall_result` | `heart.py:1099-1110` | ✗ WRONG FILE |
| 13 | `episode_summarizer.py:445-468` | 445-468 | ✓ |
| 14 | `fact_extractor.py:249` | 249 | ✓ |

Row 10 file error propagated from spec v2.17 to the plan unchanged.

---

## Risk concentrations

| Phase | Risk | Rationale |
|---|---|---|
| Phase 8 (backfill) | Highest | Two-connection advisory lock pattern, 14 codex rounds in spec. Devil's review covers thoroughly. |
| Phase 5 (Heart) | High | Four dedup/supersession bypass sites: cosine (covered), `_supersede_by_subject` (MISSING P1 #2), two pre-learn sites (covered Phase 4). |
| Phase 7 (Layer 2) | Medium | SQL is verbatim from spec. Risk is the missing flag flip (P1 #1) — silent dark launch. |
| Phase 0 | Low-Medium | Underestimated adaptation scope (P2 #3). |

---

## What's NOT in the plan that should be

1. `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED=true` flip step + CLAUDE.md env-var row (P1 #1)
2. `_supersede_by_subject` bypass (P1 #2)
3. Explicit statement that `_learn` must NOT inject `event_date_classified_at` for non-F075 callers. Spec §Plus line 330 enumerates `tools.py:516`, `rest.py:1722`, `knowledge_extractor.py:127` — plan should re-document this exclusion.
