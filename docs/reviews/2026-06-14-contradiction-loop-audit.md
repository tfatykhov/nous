# Contradiction-loop audit — prod (`nous-default`), 2026-06-14

Started as "close the deferred Fix C: surface fact contradictions in recall."
The data redirected it: genuine contradictions are rare, and the real bug is a
**91% silent MERGE-consolidation failure** caused by a too-small token cap.

## What the loop actually does on prod

| signal | count |
|---|---|
| `f031_contradiction_resolution` events | 819 |
| `contradicts` graph edges | **0** |
| facts with `contradiction_of` | 1 |

Resolution **raw** action: MERGE 774, KEEP_BOTH 40, SUPERSEDE 5.
Resolution **applied** action: KEEP_BOTH 748, MERGE 66, SUPERSEDE 5.
**`MERGE → KEEP_BOTH` downgrades for missing content: 708.**

So 95% of "contradictions" are actually **complementary facts the classifier
wants to merge** (genuine conflicts are 5/819), and **708 of 774 intended merges
(91%) were silently downgraded to KEEP_BOTH** because the MERGE arrived with
empty `merged_content`.

## Root cause — `max_tokens=300` truncation (confirmed 6/6)

`_phase_resolve_contradictions` called the resolver at `max_tokens=300`
(`sleep_handler.py:874`). The `resolve_contradiction` schema emits
`action, confidence, reason, merged_content` **in that order**, and the model
writes a verbose `reason` (~500 chars ≈ 130 tokens, despite "brief") that
exhausts the 300-token budget before `merged_content` (last) is emitted →
empty → the safety floor downgrades MERGE → KEEP_BOTH.

`scripts/diag/probe_merge_truncation.py` re-ran 6 real downgraded pairs at 300 vs
800: **6/6 returned MERGE with empty `merged_content` at 300 and MERGE with
510–819-char `merged_content` at 800.** Not the model declining — pure truncation.

**Impact:** 708 complementary-fact pairs that should have consolidated into one
fact were instead left as two near-duplicate facts — memory bloat, every sleep
cycle.

## Fix

- `sleep_handler.py:874` resolver `max_tokens` **300 → 1000** (800 was proven
  sufficient at 6/6 with ~2× headroom over the ~400-token observed usage; 1000
  is free tail headroom since `max_tokens` is a ceiling).
- `sleep_handler.py:1275` cluster `merge_facts` `max_tokens` **600 → 1000**
  (precautionary — same truncation class, multi-fact merges can be longer).

Tests green (`test_f031_consolidation`, `test_sleep_handler`).

## Secondary finding (low value — noted, not fixed)

The recall-surfacing of fact contradictions IS broken — `run_recall_pipeline`
Stage 5 (`retrieval_pipeline.py:680-686`) populates its contradiction-query id
set **only from `decision_results` + `graph_expanded`**, never facts, so
fact↔fact `contradicts` edges can never surface. But it's low value here:
genuine contradictions are rare (5), `contradicts` edges aren't even written on
KEEP_BOTH (#518 shipped Fix A/supersedes only; Fix B deferred), and after the
MERGE fix the genuine-KEEP_BOTH population shrinks further. Closing the surfacing
loop (Fix B contradicts edges + Stage 5 fact inclusion) stays deferred — the
corpus doesn't have enough genuine fact contradictions to justify it.
