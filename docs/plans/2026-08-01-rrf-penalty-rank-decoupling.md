# Decouple the RRF penalty rank from the chunk leg's allotment

**Date:** 2026-08-01
**Branch:** `fix/rrf-penalty-rank-decoupling`
**Decision:** FORGE `ab009265` · measurement `11337c6f` · caused-by `145459a4` (PR #579)

---

## 1. What happened

PR #579 made `NOUS_EPISODE_CHUNK_RECALL_LIMIT` effective, moving prod's chunk leg from 20
rows to 30. A 60-query paired A/B on the frozen clone `nous_prod_20260801` then measured
that as a **net regression**:

| metric | result |
|---|---|
| chunks in top-10, per query | 3.77 → 2.93 (**−0.83**) |
| queries worse / same / better | **37** / 21 / 2 |
| top-10 identical | only 6 / 60 |
| newly admitted chunks reaching top-10 | **0 / 60** |
| payload | **+8,714 chars (~2,180 tokens)** per call |

The last row is the one that settles it: the extra 10 chunks never surfaced, *and* they
depressed the 20 that were already there. Strictly worse, at a token cost.

## 2. Cause — a row limit that is also a scoring input

`_rrf_merge` gives a document missing from one leg a penalty rank of `limit + 1`
(`search.py:161`). `hybrid_search` passes its own row `limit` straight in as that `limit`
(`search.py:309`). So the chunk leg's allotment is an argument to the **scoring function**,
not just a row count.

The normalizer is `max_score = 1.0 / k` (`search.py:183`) — independent of `limit` — so the
entire shift is attributable to `penalty_rank`.

**Reproduced directly against `_rrf_merge`** (k=30 as prod runs, `vector_weight=0.7`,
vector-rank-0 / keyword-miss document):

```
limit= 10  penalty_rank= 11  score=0.9195     <- heart legs run here
limit= 20  penalty_rank= 21  score=0.8765     <- prod before #579
limit= 30  penalty_rank= 31  score=0.8475     <- prod after #579
limit= 50  penalty_rank= 51  score=0.8111

with the penalty base pinned at 10:
return_limit= 10/20/30/50  ->  score=0.9195 in every case
```

The 20 → 30 delta is **−0.0290**, matching the ~0.03 uniform drop measured across the 60
queries. Prod's `NOUS_RRF_K=30` (not the 60 default) is what makes it this large; at k=60
the same shift would be roughly half.

**Two consequences, not one:**

1. **Raising the knob demotes chunks.** The measured regression above.
2. **Chunks carry a standing handicap against facts.** Both are scored into the same merged
   list, but the heart legs run at `limit=10` and the chunk leg at 30 — a constant ~0.07
   penalty for a chunk in the *identical* retrieval situation. A concrete candidate cause
   for chunks' median pipeline rank 18.0 vs facts' 7.0.

## 3. The fix

`_rrf_merge` already has the remedy: `return_limit`, added by codex #577 r1, which decouples
how many rows come back from the `limit` that defines `penalty_rank`. Its docstring
(`search.py:152-158`) documents this exact trap in as many words. `Brain._query` uses it
(`brain.py:836`). `hybrid_search` did not.

| File | Change |
|---|---|
| `nous/heart/search.py` | `hybrid_search` gains `penalty_limit: int \| None = None`; when set, it feeds `_rrf_merge`'s `limit` and the row count moves to `return_limit` |
| `nous/config.py` | new `chunk_rrf_penalty_limit: int \| None = None` |
| `nous/api/retrieval_pipeline.py` | chunk leg threads the setting through |
| `CLAUDE.md` | env-table row |
| `tests/test_rrf_search.py` | `TestPenaltyRankDecoupling`, 10 tests |

`_rrf_merge` itself is untouched.

### 3.1 Deviation from the proposal: the setting's name

Proposed as `chunk_rrf_penalty_rank`; shipped as **`chunk_rrf_penalty_limit`**.

The proposal's own sweep values are 10 and 20, and those are *limits* — `penalty_rank` is
`limit + 1`. A setting named `..._rank` set to 10 would produce a penalty rank of 11. Naming
it for a quantity it is not is precisely the class of silent semantic mismatch this change
exists to remove.

### 3.2 One subtlety in the merge call

`merge_limit` is deliberately inflated under `require_keyword_hit` (`search.py:304-308`) so
the merge does not truncate before the keyword filter runs. So `penalty_limit` must feed
`_rrf_merge`'s `limit` while `merge_limit` becomes `return_limit` — **not** a swap of both,
which would silently re-truncate that path.

## 4. Why this shape

- **Byte-identical by default.** `penalty_limit=None` leaves all six `hybrid_search` call
  sites exactly as they are. Only the chunk leg opts in, and only when the setting is set.
- **The setting is the flag.** Rollback is unsetting it; no separate feature flag.
- **Uses the remedy the codebase already prescribes** rather than inventing one.
- **Does not retune a constant.** #579 already tried making the knob effective; the defect
  is the coupling, not the value.

### Rejected alternatives

| alternative | why not |
|---|---|
| Hardcode a fixed penalty rank inside `hybrid_search` | Moves scores at all six call sites simultaneously — no land-dark path, no way to attribute a regression |
| Drop the penalty term entirely (textbook RRF omits absent legs) | Probably the correct end state, but affects every hybrid caller and deserves its own decision |
| Just lower `episode_chunk_recall_limit` back to 20 | Recovers the regression but leaves the chunk-vs-fact handicap and the trap intact for the next person who raises the knob |
| Revert #579 | The knob would go back to being silently inert. #579's defect was real; its *consequence* is what this fixes. |

## 5. Verification

| Step | Result |
|---|---|
| Mechanism reproduced | Direct `_rrf_merge` call — see §2. Delta matches the measured regression. |
| Tests fail without the fix | Verified by stashing **only** `nous/`: the 2 wiring tests fail (`penalty_limit` absent from the captured kwargs); the 7 invariant tests pass either way because they drive `_rrf_merge`'s pre-existing `return_limit` — correct, they pin a contract rather than the wiring. |
| Negative case | `test_score_varies_with_limit_when_not_pinned` asserts all four limits yield *different* scores, monotonically decreasing, with `scores[20] - scores[30] == 0.029`. Pins today's coupling so it cannot be silently reintroduced. |
| Wiring | `test_chunk_leg_threads_setting_through` — the setting must actually reach `hybrid_search`, else it is inert. That inertness is the bug #579 fixed; not re-testing it here would repeat the mistake. |

## 6. Rollout — deploy together, not in sequence

`NOUS_EPISODE_CHUNK_RECALL_LIMIT=30` is already in prod config. **Deploying #579 without
setting `NOUS_CHUNK_RRF_PENALTY_LIMIT` ships the measured regression.** They are one change
operationally.

Sweep values for validation:

| value | what it tests |
|---|---|
| unset | control — today's coupled behaviour |
| `20` | reproduces pre-#579 chunk scoring, making a clean k-sweep possible for the first time |
| `10` | puts chunks on the heart legs' penalty base — the standing-handicap hypothesis (§2.2) |

## 7. Out of scope (flagged, not fixed)

- **The other five `hybrid_search` call sites** — `graph_densifier.py:227`/`:371`,
  `episodes.py:482`, `facts.py:3254`, `procedures.py:409`, plus the direct `_rrf_merge` at
  `facts.py:3372`. Same latent defect wherever `limit` varies between invocations; each
  needs its own measurement. `brain.py:836` is already correct and is the reference.
- **`hybrid_search_multi`** — the chunk leg never reaches it (`_search_episode_chunks` calls
  the single-query entry point, which is also why query expansion cannot reach the chunk
  leg).
- **Whether a missing leg should carry a penalty term at all.** Textbook RRF omits absent
  legs. Likely the right end state; its own decision.

## 8. Lesson

#579's PR body said "whether k=30 beats k=20 is UNMEASURED" and treated that as future work.
It was not future work — it was a live risk being shipped. And the mechanism was written
down, in the docstring of the function the changed code calls, naming the remedy. The call
site was read; the callee was not.

**A knob that feeds a ranking function is not a row count.** Before changing any retrieval
limit, trace whether it reaches a scoring expression — if it does, a sweep of that knob
measures two variables and neither cleanly.
