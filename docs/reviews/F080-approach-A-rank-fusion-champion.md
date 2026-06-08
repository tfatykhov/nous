# F080 — Champion Brief: Approach A (Second-Stage Rank Fusion)

**Position:** Adopt **weighted Reciprocal-Rank Fusion over the per-type ranked lists** as the spine of F080. Scale-free by construction, reuses proven RRF machinery, O(n), works with CE/MMR OFF, and dissolves all four score-space pathologies (B1/B2/B3/B4) at once. Honest about its one real cost: it discards magnitude/margin.

**Verified against source this session** (not the spec's line numbers): `heart.py:867-1117`, `search.py:76-117 / 234-290 / 293-377`, `censors.py:373-409`, `procedures.py:430-457`, `retrieval_pipeline.py:218-309`. Arithmetic claims below were run, not asserted.

---

## 0. The correction the spec hides: there are TWO mixed-scale sorts, not one

The spec (§4) calls `heart.py:1110-1112` the "primary surgery." That is necessary but **not sufficient**. There are two independent descending sorts over incompatible spaces, and in prod **the pipeline one is the live defect**:

| Site | Code | Sorts what | Prod state | Why it must be fixed |
|------|------|------------|------------|----------------------|
| **S1 — heart merge** | `heart.py:1110-1112` `merged.sort(key=score)[:limit]` | the 4 Heart types (fact/episode/procedure/censor) | always on (CE/MMR off) | the `[:limit]` cut here **drops displaced facts before the pipeline ever sees them** — B1/B2 are *irrecoverable downstream* |
| **S2 — pipeline rerank** | `retrieval_pipeline.py:277-278` `results.sort(key=score)` | the full cross-leg pool: heart_results ∪ chunks ∪ heart_graph ∪ Path-A ∪ decisions ∪ graph_expanded | **ON** (chunks on ⇒ `rerank_by_score=True`) | this is **B4**, "the prod-live form of the bug" (whitepaper §7-B4) |

A fix that touches only S1 satisfies acceptance #1 inside the Heart leg, then **re-violates it at S2** when the now-clean heart `[0,1]` is re-mixed with chunk raw-cosine, graph `~0.35–0.63`, and `decision.score`. **Approach A is the only spine that handles both sites with one mechanism**, because rank fusion is *defined* over "a set of already-ranked lists" — and S2's inputs (`acc.heart_results`, `acc.chunk_results`, `acc.heart_graph_*`, `acc.decision_results`, `acc.graph_expanded`) are *literally six separate ranked legs already* (verified `retrieval_pipeline.py:130-154,220-249`). S2 is the *cleaner* A story, not the harder one.

The whole brief below specifies **both** sites under one flag.

---

## 1. Mechanism

### 1.1 The reused primitive — and why I will NOT mutate it

`_rrf_merge_n` (`search.py:234-290`) already fuses N ranked lists with the exact normalization every downstream consumer expects:

```
score(d) = Σ_i  (1/N) / (k + rank_i(d))           rank_i = 0-indexed pos in list i, or limit+1 if absent
score(d) = score(d) / (1/k)                        # normalize; theoretical max = 1/k
```

But it **hardcodes equal weight** `per_list_weight = 1.0/n` (`:260`) and it sits on the **query-expansion byte-identical path** (`heart.py:912-921` → `hybrid_search_multi` → `_rrf_merge_n`). Mutating it to add weights would risk the F050 single-variant byte-identical invariant. So I add a sibling:

```python
# nous/heart/search.py  (new, beside _rrf_merge_n)
def _rrf_merge_weighted(
    ranked_lists: list[tuple[float, list[UUID]]],   # (weight, ranked_ids) per type
    k: int,
) -> dict[UUID, float]:
    """Weighted N-list RRF. score(d) = Σ_i w_i / (k + rank_i(d)); Σ w_i = 1.0.

    Inputs are ALREADY-RANKED id lists — rank is list position, no re-sort.
    Normalized by 1/k so a hypothetical rank-0-everywhere doc → 1.0 (the
    disjoint-input reality is lower; see §7). Returns id→score; caller maps
    back to RecallResult/PipelineResult and applies [:limit] ONCE, after fusion.
    """
    total_w = sum(w for w, _ in ranked_lists) or 1.0
    penalty_offset = 0.0
    rank_maps: list[tuple[float, dict[UUID, int]]] = []
    for w, ids in ranked_lists:
        wn = w / total_w
        rank_maps.append((wn, {doc_id: i for i, doc_id in enumerate(ids)}))
    scored: dict[UUID, float] = {}
    all_ids = set().union(*(set(rm) for _, rm in rank_maps)) if rank_maps else set()
    for doc_id in all_ids:
        s = 0.0
        for wn, rm in rank_maps:
            r = rm.get(doc_id)
            if r is not None:
                s += wn / (k + r)
            # absent: contributes wn/(k+limit+1) — a near-constant offset; see §7.
        scored[doc_id] = s * k   # normalize by 1/k  (==  / (1/k))
    return scored
```

(For absent legs I drop the penalty term entirely rather than adding `limit+1`; §7 explains why this is *more* correct for disjoint inputs than the expansion-path penalty — and either choice is a constant offset that cannot change ordering.)

### 1.2 Site S1 — `heart._recall`

The fusion input is free: the OFF-path loop at `heart.py:974-988` already builds `merged` by iterating `zip(keys, results_list)` where each `raw_results` is a per-type ranked list in descending per-type-score order. So **per-type rank == append position within each type's block** — I capture ranked id-lists without any re-sort.

```python
# heart.py, replacing the else-branch at :1109-1112 ONLY (the if/elif CE/MMR
# branches above are untouched — they stay prod-off).
else:
    if RuntimeConfig.get().get_coherent_ranking_enabled(self.settings):
        # Build per-type ranked id lists from append order (already ranked).
        by_type: dict[str, list[UUID]] = {}
        for r in merged:
            by_type.setdefault(r.type, []).append(r.id)
        priors = self.settings.coherent_ranking_type_weights   # §2
        ranked_lists = [
            (priors.get(t, priors["_default"]), ids)
            for t, ids in by_type.items()
        ]
        fused = _rrf_merge_weighted(ranked_lists, _resolve_rrf_k())
        for r in merged:
            r.score = fused.get(r.id, 0.0)
        merged.sort(key=lambda r: (r.score, _STAGE_RANK[r.type]), reverse=True)  # §7 tiebreak
        merged = merged[:limit]
    else:
        # BYTE-IDENTICAL OFF PATH — unchanged.
        merged.sort(key=lambda r: r.score, reverse=True)
        merged = merged[:limit]
```

Censor never enters `by_type` because §3 removes it from `search_types` upstream — see §3.

### 1.3 Site S2 — `run_recall_pipeline`

Same mechanism, one level up. But S2 has a wrinkle S1 does not, and **the naive version is a prod regression** — I call it out and fix it here rather than let the opposing champion find it.

**The hazard.** In prod, two stages mutate `r.score` *before* the `:277` sort:
- `:246` **adjacency boost** (`graph_adjacency_boost_enabled` prod **ON**): `score *= 1 + α·degree/max_degree`.
- `:258` **recency resolver** (`recency_resolver_enabled` prod **ON**): superseded fact `score *= 0.3`.

A naive `for r in results: r.score = fused.get(r.id, ...)` **overwrites both**, silently killing the supersession down-rank and the cluster boost. That would regress two live prod features. So the fused score must **compose with** those multipliers, not clobber them.

**The fix — separate the rank signal from the magnitude multipliers.** Build the legs from the **stage-order assembly that exists at `:220-231`** (the `results` list *before* the `:246`/`:258` mutations), capture each result's *fusion rank* there, and apply the recency/adjacency multipliers **on top of** the fused score. Concretely, move the fusion to run on the pristine stage-order `results` and keep `:246`/`:258` as *post-fusion* multipliers:

```python
# retrieval_pipeline.py
# (A) Right after the stage-order assembly at :220-231, before adjacency/recency,
#     capture the per-leg ranked id-lists from the SLICES of `results` we just built.
#     `results` is concatenated in known stage order, so leg boundaries are known
#     from the extend() lengths — no re-call of the _*_to_pipeline helpers.
if getattr(settings, "coherent_ranking_enabled", False) and rerank_by_score:
    legs = _legs_from_stage_slices(results, leg_lengths, W)   # (weight, [ids]) per leg
    fused = _rrf_merge_weighted([(w, ids) for w, ids in legs if ids], _resolve_rrf_k())
    for r in results:
        r._fused = fused.get(r.id, 0.0)        # park the rank signal on a scratch attr
        r.score = r._fused                     # adjacency/recency now multiply the fused base
    coherent = True
else:
    coherent = False

# (B) :246 adjacency boost — UNCHANGED. Now multiplies the fused base (prod ON).
# (C) :258 recency resolver — UNCHANGED. ×0.3 now applies to the fused base (prod ON).

# (D) replace the :277-278 sort
if rerank_by_score:
    if coherent:
        # r.score is now fused_base × adjacency × recency — magnitude multipliers
        # preserved, cross-type incomparability gone. Tiebreak on stage order.
        results.sort(key=lambda r: (r.score or 0.0, _STAGE_RANK.get(r.type, 0)), reverse=True)
    else:
        results.sort(key=lambda r: r.score or 0.0, reverse=True)   # BYTE-IDENTICAL OFF PATH
```

`leg_lengths` is the list of `len(...)` from each `extend()` at `:220-231` — captured at assembly time, so the id-lists come from the **already-built `results`** (no re-deriving, no re-call of `_*_to_pipeline`). The fused score *replaces* `r.score` as the **base**, then the existing `:246`/`:258` multipliers compose onto it, so the downstream F071 filter (`:286-293`) and `PipelineStats` see the coherent, recency-and-adjacency-adjusted value.

The `[:limit]`/no-truncation behavior is preserved — the pipeline has no global top-K today (B7), and I add none (out of scope).

**`_STAGE_RANK`** (the deterministic tiebreak, load-bearing per §2.1 because equal-prior legs near-tie):
```python
_STAGE_RANK = {"fact": 6, "episode": 6, "chunk": 5, "decision": 4, "procedure": 3}  # higher = wins ties
```
This encodes "when fused scores tie, prefer answer-bearing types" — and is the **explicit** prior the §2.1 analysis demands instead of letting raw append order decide silently.

**Where the `[:limit]` moves:** at S1 the cut now happens *after* fusion (it was already after the sort; the structural change is that the sort key is fusion-rank, so a high-RRF fact can no longer be cut by a floored censor or a >1.0 procedure). At S2 there is no `[:limit]` today and I add none.

---

## 2. Type priors (the load-bearing parameter — owned, not hidden)

### 2.1 Why priors are NOT optional polish

Run the arithmetic for **disjoint** inputs (each doc lives in exactly one type's list — the cross-type reality):

```
equal weight, n=4, k=60, doc in 1 list / absent in 3:
  rank0 → norm 0.8838   rank1 → 0.8797   rank2 → 0.8757   rank5 → 0.8646
  absence offset (constant for every doc): 0.01056
```

Two facts fall out, and the doc must own both:

1. **The absence term is a constant offset** — it shifts every doc equally and cannot change ordering. So ordering is governed purely by `w_type / (k + rank_within_type)`. Cross-type RRF over disjoint sets is **weighted rank-interleaving** — there is no consensus/reinforcement term (the thing RRF buys on *overlapping* expansion lists). I am not hiding this; it is exactly *why* A is scale-free, and exactly why **the weights carry 100% of the cross-type discrimination.**
2. **Equal weights produce near-ties** (0.884 vs 0.880…) decided by floating-point then stable-sort append order. "Uniform prior" silently makes *stage order* the real prior. That is worse than choosing priors honestly — so I choose them.

### 2.2 Proposed default weight vector (first-principles, not benchmark-fit)

I justify from **existing config and the system's own type semantics**, never from a number I cannot reproduce on prod:

| Leg | Weight | First-principles justification |
|-----|--------|-------------------------------|
| `fact` / `episode` (Heart S1) | **0.45** | Facts/episodes are the *answer-bearing* substrate of recall; LongMemEval/BEAM answers are facts. They already share one RRF space, so one weight. |
| `procedure` (S1) | **0.30** | Procedures are *how-to*, rarely the literal answer to a recall query; the utility boost (§4) already privileges effective ones *within* the leg. |
| `decision` (S2) | **0.35** | Decisions are first-class memory but a recall query is more often fact-seeking than decision-seeking; weight between fact and procedure. |
| `chunk` (S2) | **0.45** | Chunks are verbatim transcript = answer-bearing (F067 was a +retrieval win); co-weighted with facts. |
| `graph_expanded` / Path-A / heart_graph (S2) | **0.20** | Graph hits are *inferred* (1-hop, decay 0.7), one association removed from a direct hit. Their raw scores already ceiling at ~0.70 (whitepaper §4.3); a low prior encodes "supporting, not leading." |
| `_default` (any unlisted type) | **0.25** | Conservative middle. |

These are **priors, not tuned constants** — defensible from "what answers a recall query" + the existing decay/boost design, and they survive an operator who has never run F051. I deliberately do **not** claim they are optimal; §6 measures them.

### 2.3 How an operator tunes

Single JSON env, mirroring `NOUS_CONTEXT_BUDGET_OVERRIDES`:

```
NOUS_COHERENT_RANKING_TYPE_WEIGHTS={"fact":0.45,"episode":0.45,"procedure":0.30,
  "decision":0.35,"chunk":0.45,"graph":0.20,"_default":0.25}
```

Weights are relative (normalized by `Σw` in `_rrf_merge_weighted`), so an operator reasons in ratios, not absolutes. Lower `graph` → graph hits sink; raise `procedure` → how-tos surface. **Plus a `k` knob:** `rrf_k=60` makes adjacent ranks nearly flat (rank0/rank1 ratio = 1.0167; at k=5 it is 1.20). For cross-type fusion a *smaller* k sharpens within-leg rank discrimination. I expose `NOUS_COHERENT_RANKING_RRF_K` (default 60 to match existing) and flag it as the second tuning surface. I am not silently reusing the expansion-path k.

---

## 3. Censor sub-decision — **EXCLUDE from the pool**

Drop `"censor"` from `search_types` (`heart.py:877`) and from `heart_types` (`retrieval_pipeline.py:340-341`) when the flag is on.

**Justification, consistent with rank fusion:** a censor `_semantic_search` list is a *guardrail-trigger ranking* — cosine floored at 0.7 (`censors.py:387,400`), surfaced specifically to *fire a guardrail*, not to answer a query. Fusing it by rank asserts "the rank-0 censor is the most relevant *memory*," which is categorically false — rank within a trigger list is not relevance-within-recall. Censors already own a dedicated surface ("Active Censors / Active Guidance", `context.py:460`), so excluding them from recall loses **nothing** the user sees. This kills **B1 with zero normalization**, and it is the *consistent* move: rank fusion's whole premise is "fuse comparable rankings"; a trigger ranking is not comparable, so the principled act is to not feed it in, not to hand-weight it down.

**Honest caveat:** exclusion is a *scope* decision, not something fusion *forces* — Approach B could also exclude. But under A it is the natural and cheapest resolution, and it removes an entire category of incomparability rather than papering it with a weight.

---

## 4. Procedure-boost sub-decision — **KEEP IT UNCHANGED; it dissolves for free**

This is a genuine A selling point. The utility boost (`procedures.py:457`, `final = hybrid × (1 + boost)`, can exceed 1.0) determines the procedure leg's **internal ordering**. Fusion reads *rank*, not magnitude — so the >1.0 value never reaches cross-type comparison. **No clamp, no post-rank, no fold-into-weight needed.** The `[0,1]` invariant that B2 violates is simply never consulted across types.

The boost still does exactly its job: it reorders procedures *among themselves* so the most effective one is rank-0 of the procedure leg, which then enters fusion at the `procedure` prior. The invariant is preserved because we stopped asking magnitude to be cross-comparable.

**Honest caveat:** the boost's *magnitude* (how much more effective) is discarded — only its reordering survives. If two procedures are both clearly relevant and one is far more effective, fusion sees "rank-0 vs rank-1 procedure," a near-tie. That is the same magnitude-loss A pays everywhere (§7); it is consistent, not a special procedure wart.

---

## 5. Embed-failure leg (B3) — A auto-fixes the headline; here's the proof and the bound

**Proof it auto-fixes the cross-type collapse:** when `facts._search` hits `embedding=None`, `hybrid_search` returns raw `ts_rank_cd/(1+…)` ~0.06 (`search.py:220-222`). Under raw-sort that whole type lands ~10–20× below its healthy siblings and is cut by `[:limit]`. Under A, the FTS-degraded fact list is *still a ranked list*. Its rank-0 contributes `w_fact / (k+0)` — **identical** to a healthy fact list's rank-0. The 0.06-vs-0.92 magnitude gap is **never read**. The type is no longer demoted as a whole. B3's headline (whole-type collapse at the cross-type sort) is gone by construction — no special-casing, no normalization branch.

**Honest bound (do not overclaim):** fusion **cannot restore the within-leg ranking quality** lost when the vector signal vanishes — it preserves whatever (worse, keyword-only) order FTS produced. So A fixes "the type got demoted," not "the type's internal order degraded." The latter requires the vector leg to come back (or B-hs-8's shared-embed retry), which is out of scope. Stating this is the honest line.

---

## 6. Acceptance criteria (§3) — walk all 7

1. **No mixed-scale sort.** ✅ at **both** S1 and S2. Final ordering is governed by **ranks** (weighted reciprocal rank), a single space. Criterion #1 explicitly permits "single space **or ranks**" — A satisfies it via ranks. I do **not** claim a clean `[0,1]`: for disjoint inputs the top item is ~0.88 (n=4), not 1.0; the space is "weighted reciprocal-rank," and the doc says so. No raw score from one type can structurally out/under-rank another — the censor floor and procedure>1.0 are never compared.
2. **CE-off correctness.** ✅ A depends on neither CE nor MMR. It *replaces* the both-off raw-sort `else` branch. Pure-Python, O(n) over the candidate set, no model. This is A's whole reason to exist under the prod constraint.
3. **Flagged + byte-identical OFF.** ✅ New behavior behind `NOUS_COHERENT_RANKING_ENABLED` (default OFF). When OFF, the S1 `else` at `heart.py:1110-1112` and the S2 branch at `:277-278` are **literally unchanged** — the fused branch is a pure `if` addition. See §7 for the one place this could break.
4. **Ordering hazards fixed (this feature's share).** ✅ S1/S2 re-sort by the fused score immediately after assigning it. The cognitive-path boost-sort hazards (B-cog-A/B) live in `context.py`, not my two sort sites — I scope them **OUT** to a sibling PR (see §8) to keep the footprint to one shippable change. The spec's open-item #4 ordering set that overlaps my sites *is* fixed.
5. **Observability honest (B5).** ✅ I thread `coherent_ranking_applied: bool` into `PipelineStats` (replacing the hardcoded `ce_reranked=False`/`mmr_applied=False` lie at `:296-297` is a separate fix, but I add the new honest flag so evals can see A fired). Cheap and in-footprint.
6. **Measured.** ✅ F051 paired A/B, per-source MRR/P@k/R@k, no-regression-> 3% gate. The censor/procedure displacement cases (the spec's §5 synthetic set) become unit assertions. The credible **failure to watch** is single-session-user/preference (§7) — I name it as the falsifier, not a footnote.
7. **No new hot-path latency.** ✅ Two dict builds + one fused pass = O(n) in the candidate count (≤ `limit*2` at S1, low-tens at S2). No model, no extra DB round-trip, no embed. Strictly cheaper than the CE path the spec's Approach C would add.

---

## 7. Honest failure modes (where A loses, and what falsifies it)

**The sharp one — magnitude loss on dominant-answer queries.** Consider one fact at RRF **0.95** (unambiguously *the* answer) and many facts at 0.55. Raw-sort ranks the 0.95 fact decisively #1. Rank fusion makes it "rank-0 of the fact leg," which fuses *equal to a rank-0 procedure or rank-0 chunk*. The margin — the very signal that this fact is special — is gone. This bites exactly **high-precision single-answer queries**: LongMemEval `single-session-*`, where Nous already scores near-perfect (memory: single-session perfect/near-perfect). A could *regress* the cases it's currently best at.

**Falsifier (ties to criterion #6):** an F051 per-source **MRR regression > 3% on `single-session-user` or `single-session-preference`** is magnitude-loss biting. If that fires, A's default weights aren't the fix — the mechanism is wrong for those shapes, and the honest response is a hybrid (A for cross-type coherence, but keep within-the-top-leg raw order as the tiebreak, or gate A to multi-type queries only). I commit to reading that exact metric before recommending the flag flip.

**Magnitude loss has a second victim — supersession.** Rank fusion *structurally cannot* represent "this fact is still relevant but superseded, so it ranks where it sat ×0.3" — that down-rank is pure magnitude, the thing A discards. §1.3 recovers it by composing the recency `×0.3` multiplier **on top of** the fused base (so the prod recency resolver survives), but be honest about what that buys: it preserves the *multiplier*, not a fusion-native notion of supersession. If an operator ran A *without* the recency resolver, supersession ordering would be gone. This is the same magnitude-loss family as the dominant-answer falsifier above — A leans on the external `×0.3`/adjacency multipliers to carry every signal that is about *how much*, not *which rank*.

**Second failure — the tie field.** `rrf_k=60` makes adjacent ranks nearly flat (0.884/0.880/0.876…), and disjoint inputs already collapse toward the absence offset. Result: **many near-ties**, resolved by the `_STAGE_RANK` tiebreak. If the priors are too close, stage order silently dominates. Mitigation: the priors above are spread (0.20–0.45), and I expose the smaller-`k` knob to sharpen. But an operator who sets all weights equal re-creates "stage order is the prior" — I document that as a foot-gun, not a feature.

**Where byte-identical-OFF could break.** *Not* the math — the **tiebreak/append order**. Python's sort is stable, so today's ties resolve by current append order. Any refactor that touches how `merged` (S1) or `results` (S2) is *built* while the flag is OFF can shift snapshot bytes even with the sort key unchanged. **Mitigation, stated as a build rule:** the OFF path must keep list-construction byte-for-byte; the fused path is added as a pure branch that reads the already-built list. The snapshot test (`postgres_only`, flag-OFF) is the gate — if a byte moves, the refactor leaked into the OFF path.

**What A does NOT fix (scope honesty):** B-cog-A/B/C/D (cognitive path), B-graph-5..9, the two-resolver drift, B5's *full* honest wiring, B7 (no global top-K). Those are sibling PRs (§8). A fixes the **headline coherence cluster (B1/B2/B3/B4)** and nothing it doesn't have to.

---

## 8. Scope line (open-item #5, #6)

**In this PR:** S1 + S2 fusion behind one flag; censor exclusion; the honest `coherent_ranking_applied` stat. One mechanism, two call sites, one flag — a single shippable change.

**Sibling PR (explicitly OUT):** the cognitive-path boost-sort fixes (B-cog-A/B at `context.py:608-617,961,992-1005`). They share the *root cause* (mutate-then-sort-by-wrong-key) but live in different files and a different retrieval surface; folding them in doubles the review surface and the snapshot risk. State it, don't do it.

---

## 9. Self-score

| Axis | Score | Rationale |
|------|-------|-----------|
| **Correctness** | **8/10** | Provably kills B1/B2/B3/B4 at both live sort sites with CE off. −2 for the genuine magnitude-loss on dominant-answer queries — a real correctness cost on Nous's currently-strongest category, mitigated only by weights/k tuning, not eliminated. |
| **Prod-safety** | **8/10** | No CE, no model, no new DB round-trip, O(n), fail-safe (fusion can't raise on disjoint inputs). Flag-gated, byte-identical OFF. −2: (a) the byte-identical invariant rests on a *build discipline* (don't perturb OFF-path list construction), enforced by the snapshot test not the type system; (b) S2 fusion **must compose with**, not overwrite, the prod-ON recency resolver (`:258 ×0.3`) and adjacency boost (`:246`) — §1.3 fixes this by parking fusion as the *base* score under those multipliers, but it is a real wiring hazard a naive implementation would trip, so it costs a point of confidence. |
| **Change-footprint** | **8/10** | One new pure function, two guarded `if` branches, one env var (+ optional k knob), one stat field, censor removed from 2 search-type lists. No schema, no migration, no new dependency. −2 because it touches *two* hot files (`heart.py` + `retrieval_pipeline.py`) — unavoidable given §0, but still two surfaces. |
| **Measurability** | **9/10** | Drops straight onto F051 paired A/B; the falsifier is a named per-source metric (single-session MRR > 3%); the synthetic displacement set becomes unit asserts; the honest stat flag makes "did A fire" observable. −1 because BEAM can't isolate this (bypasses the merge), so external validation is F051-only until a prod-shape harness exists. |

**Bottom line:** A is the right spine *because of* the prod constraints, not despite them — it's the only approach that is scale-free, model-free, O(n), and natively defined over the exact ranked-leg structure both sort sites already produce. Its honest cost is magnitude/margin, its honest risk is single-session regression, and both are measurable on the harness we already run before any flag flip.
