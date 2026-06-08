# F080 Debate — Approach B Champion Brief: Per-Type Score Calibration to a Common [0,1]

**Position:** Champion of **Approach B**. The cross-type merge is incoherent because two
types deviate from a `[0,1]` space that **already exists** for everyone else. The fix is to
force the two deviants (and the embed-failure leg) back into that existing space — preserving
relevance *magnitude*, which is the signal rank fusion (Approach A) throws away.

**Authority:** code only. Every claim carries `file:line`, verified this session against
`nous/heart/{heart,search,censors,procedures,facts}.py` and `nous/api/retrieval_pipeline.py`.

---

## 0. The spine, stated once

**Fixed per-type affine maps INTO the RRF-normalized `[0,1]` space that `search.py` already
produces** — *not* per-candidate-set min-max, *not* a learned distributional map.

The load-bearing fact: **3 of the 4 types already emit normalized RRF `[0,1]`.**
- `_rrf_merge` divides by `max_score = 1/k` → `[0,1]` (`search.py:110-115`).
- `_rrf_merge_n` (the **prod path**, because query expansion is ON in `.env.prod-snapshot`)
  applies the *identical* `/max_score` normalization (`search.py:283-288`, docstring at `:246-250`
  asserts byte-identical normalization to `_rrf_merge` at N=1). **Verified this session** — my
  premise survives the config that actually ships.

So the "common space" is not invented. It is the codebase's own existing invariant. Facts
(`facts.py` via `hybrid_search`), episodes (`episodes.py:548`), and single-/multi-query
procedures' *hybrid* component all land in `[0,1]`. Approach B's entire job is to make the
**two deviants** conform:

| Type | What enters the merge today | Deviation | Affine map under B |
|------|-----------------------------|-----------|--------------------|
| Fact | RRF `[0,1]` (`search.py:224`) | none | **identity** |
| Episode | RRF `[0,1]` (`episodes.py:548`) | none | **identity** |
| Procedure | `hybrid × (1+boost)` → **>1.0** (`procedures.py:457`) | boost breaks ceiling | de-boost: use raw hybrid + bounded utility bonus |
| Censor | raw cosine **floored ≥0.7** (`censors.py:387,400`) | floor + wrong distribution | **exclude from pool** |
| Embed-fail leg | raw `ts_rank_cd` ~0.06 (`search.py:220-222`) | un-normalized | rank-normalize the leg into `[0,1]` |

Because the map is **identity for the already-normalized types**, the "fixed affine maps need
per-type calibration data" con (F080 §2 Approach B "Con") **shrinks to near zero**: there is no
data to gather for facts/episodes — they're already there. We only neutralize two pathologies
and re-normalize one failure leg.

**Why not min-max (the option the task lists first — it is a trap I decline):** per-candidate-set
min-max maps each type's best→1.0 and worst→0.0. A type whose best fact is 0.55 and a type whose
best is 0.95 *both* map their top item to 1.0 — which is **exactly the magnitude-discarding
behavior I'm supposed to beat Approach A with** (deliverable #5). It also degenerates when
`max==min` on a 1–2 item type, and the procedure outlier at 1.26 would set the ceiling. Min-max
is rank fusion with extra fragility. I reject it. Affine-into-existing-`[0,1]` is the principled
Approach-B spine.

---

## 1. Calibration mechanism

### Transform

For a candidate of type `t` with comparable score `s` entering the merge:

```
calibrated(s, t) = clamp01( a_t * s + b_t )
```

with **fixed, documented** `(a_t, b_t)`:

| Type | `a_t` | `b_t` | Rationale |
|------|-------|-------|-----------|
| fact | 1.0 | 0.0 | already RRF-`[0,1]` — identity |
| episode | 1.0 | 0.0 | already RRF-`[0,1]` — identity |
| procedure | 1.0 | 0.0 | applied to the **raw hybrid** (see §4); utility folds in separately |
| chunk | 1.0 | 0.0 | raw cosine `[0,1]` already (`retrieval_pipeline.py:897-903`) — identity |
| censor | — | — | **excluded** (§3); no map needed |
| graph-expanded | 1.0 | 0.0 | already `≤0.70`, in-band, honest decay score |
| embed-fail leg | rank-normalized | (§2) | re-based per §2 |

For the shipping default, **every surviving type's map is identity** — the calibration is
purely *removing pathologies*, not *rescaling healthy signals*. That is the minimal, honest
form of Approach B and it is what makes acceptance criterion #3 (byte-identical OFF) trivial.

### Where it plugs in (the critical placement)

**Inside `heart._recall`, at the score-copy site `heart.py:983-988`, behind
`NOUS_COHERENT_RANKING_ENABLED`.** This is non-negotiable and is *why* B is correct where a
pipeline-only fix is not:

The load-bearing truncation `merged.sort(key=score)[:limit]` is at **`heart.py:1110-1112`** —
*inside* `_recall`, **before** `run_recall_pipeline` ever sees the list (whitepaper §3.5).
Displaced facts are dropped here. Fixing only the pipeline `rerank_by_score` sort
(`retrieval_pipeline.py:277-278`, the B4 site) is **too late** — the relevant facts are already
gone. So:

- **Primary surgery:** calibrate at `heart.py:983-988` so the `[:limit]` cut at `:1110-1112`
  operates on one comparable space.
- **Secondary:** apply the same calibration at the pipeline assembly (`retrieval_pipeline.py:220`)
  so the cross-*leg* pool (graph + chunks + decisions) competes coherently before
  `rerank_by_score`. Decisions arrive as `d.score` (`retrieval_pipeline.py` `_decisions_to_pipeline`),
  already `[0,1]` from `brain.query`'s own RRF.

Concretely at `:983-988` the loop becomes (flag-gated; OFF path unchanged):

```python
for item in raw_results:
    raw = getattr(item, "score", None)
    original_score = raw if raw is not None else 0.0
    if coherent_ranking_enabled:
        original_score = _calibrate(memory_type, item, original_score, fetch_limit)
    recall_result = self._to_recall_result(memory_type, item, original_score)
```

### Tiny-set behavior (the min-max degeneracy probe)

This is *why I chose fixed affine over min-max*. Fixed affine has **no candidate-set dependence**:
a single fact at RRF 0.55 maps to 0.55 whether it's alone or one of fifty. There is **no
`max==min` division** to blow up, because there is no division by a set-derived range. A
2-result type calibrates identically to a 200-result type. Min-max could not make this claim —
its whole transform is `(s - min)/(max - min)`, undefined at `max==min`. Stability on tiny sets
is a *property of the affine choice*, not a special case I have to guard.

(The only set-dependent leg is the embed-fail re-normalization in §2 — and there I use *rank*,
not min-max, precisely to keep the same degeneracy-immunity.)

---

## 2. Removing the pathologies at source (without mutating shared producers)

**The #2-vs-#3 tension, resolved explicitly.** Deliverable #2 says "strip the pathologies at
source"; deliverable #3 demands byte-identical OFF. If I literally edit `censors.py:387,400`,
`procedures.py:457`, or `search.py:220-222`, those producers also serve the **flag-OFF path** and
the **cognitive path** → the F051 snapshot breaks and I lose. **Resolution: "at source" means
"neutralize each pathology as the score enters the *comparable* merge score," not "edit the
producer SQL."** All neutralization lives in the flag-gated `_calibrate` at `heart.py:983-988`.
No producer is mutated. When `NOUS_COHERENT_RANKING_ENABLED=false`, the `else` branch at
`heart.py:1109-1112` runs verbatim → byte-identical.

**Censor floor (`censors.py:387,400`).** The `> 0.7` is a SQL `WHERE` filter that *selects* which
censors return — it is a retrieval gate, not a score I can subtract away cleanly (the floor means
I never even see sub-0.7 censors, so I can't re-base the distribution honestly). The clean answer
is **exclude censors from the ranked pool entirely** (§3). One line, no normalization, pathology
gone at the root.

**Procedure boost (`procedures.py:446-457`).** The producer returns only the *boosted*
`final_score`. To de-boost at the merge I must **carry the raw hybrid score** through
`ProcedureSummary` (the one unavoidable wire change — see §8 footprint). `procedures.py:438`
already has `hybrid_score = scores.get(p.id, 0.0)` in scope; I add `raw_hybrid_score=hybrid_score`
to the `ProcedureSummary(...)` constructor (`:459-473`). **Adding a field changes no existing
value** → OFF stays byte-identical. At calibration I read the raw hybrid (already `[0,1]`) as the
*relevance* axis and fold utility as a bounded bonus (§4). The boosted `score` field stays for any
OFF-path consumer.

**Embed-failure leg (`search.py:220-222`).** When `embedding is None`, `hybrid_search` returns
`keyword_results[:limit]` as raw `ts_rank_cd/(1+ts_rank_cd)` (~0.06) — un-normalized, collapsing
the whole type ~10–20× (whitepaper B3). I **rank-normalize this leg into `[0,1]`** at calibration
time: the leg is already `ORDER BY score DESC` (`search.py:214`), so I map by within-leg rank to
the *same* `[0,1]` band RRF would have produced —
`calibrated_i = 1/k_norm · (rrf_contribution at rank i)`, i.e. reuse `_rrf_merge` with an empty
vector list so the math is *literally the existing normalization* (`search.py:105,113-115`). This
is the honest re-base: a keyword-only type now competes at the rank it earned, not at a raw
`ts_rank_cd` magnitude that was never comparable to cosine. Detecting the degraded leg: it carries
no vector contribution, surfaced via a `degraded: bool` flag threaded from `hybrid_search`'s
`embedding is None` branch into the per-type result (small additive field, OFF-path inert).

All four types now land in one honest `[0,1]`.

---

## 3. Censor sub-decision — **EXCLUDE**, and it's *more* principled, not just cheaper

Drop `"censor"` from `heart_types` in the `recall_deep` path (`retrieval_pipeline.py:340-341`),
gated by the flag. Justification — consistent with the affine spine:

1. **Censors are not in the RRF space.** They emit *raw cosine*, hard-floored (`censors.py:387,400`).
   To "normalize them in" I'd need a defensible cosine→RRF affine map — but cosine and RRF are
   different distributions, so any `(a_censor, b_censor)` is a **guess**, and a guess is exactly the
   "fixed maps need calibration data" con biting me. Excluding dodges the one map I *can't* make
   honestly.
2. **Embedding-model drift kills any fixed cosine map.** Prod runs `text-embedding-3-large`; code
   embeds `small`. A cosine→`[0,1]` calibration tuned on one model silently mis-maps under the
   other. Exclusion is **immune** to that drift; a fixed cosine affine is not.
3. **Censors already have a dedicated surface.** "Active Censors / Active Guidance"
   (`context.py:460`) renders them unconditionally. They do not need to *compete for ranked slots*
   against facts. Letting them displace mid-ranked facts at `heart.py:1110` is pure downside —
   they're shown anyway.

This is the rare case where the one-line change is the *principled* answer, not a shortcut. It
removes B1 at the root and removes the one map I couldn't defend.

---

## 4. Procedure-boost sub-decision — **bounded convex bonus**, provably ≤1.0

I reject `min(1.0, boosted)` clamping: it manufactures **ties at the ceiling** (1.26 and 1.05 both
→ 1.0), a *new* monotonicity pathology. Instead:

**Relevance and utility are different axes.** Hybrid score = "does this procedure match the query."
Effectiveness = "has this procedure worked before." Conflating them with a multiplier
(`procedures.py:457`) is the bug. Post-calibration, utility is a **bounded within-type bonus**:

```
relevance = raw_hybrid                      # already [0,1], carried per §2/§8
utility    = clamp01( effectiveness )       # effectiveness ∈ [0,1] by construction
final      = (1 - w) * relevance + w * utility,   w = NOUS_PROCEDURE_UTILITY_WEIGHT (default small, e.g. 0.15)
```

**Proof `final ∈ [0,1]`:** `relevance ∈ [0,1]`, `utility ∈ [0,1]`, `w ∈ [0,1]` ⇒ `final` is a
convex combination of two `[0,1]` values ⇒ `final ∈ [0,1]`. No saturation, no ceiling ties,
strictly monotone in both axes. A maximally-effective procedure can rise by at most `w` over its
pure-relevance position — bounded, inspectable, and it *cannot* out-of-band any fact.

This keeps the cross-type utility signal alive (unlike "post-rank within the procedure slice,"
which would let a barely-relevant effective procedure never surface against facts) while *proving*
the `[0,1]` invariant. Utility weight `w` is a single tunable, defaulting small so relevance
dominates.

---

## 5. Magnitude preservation — the cross-type case that buries rank fusion

This is my central selling point over Approach A. The decisive example is **cross-type**, not
within-type:

> Candidate set: one **fact at RRF 0.95** (dominant, near-perfect query match) and one
> **procedure that is rank-0 of its own type but only RRF 0.60** (weak match, just the best
> *available* procedure).

- **Approach A (rank fusion over per-type lists):** both are *rank 0 of their type*. RRF fuses
  them on rank alone (`_rrf_merge_n`, `search.py:234-290`) — the 0.60 procedure contributes the
  *same* `1/(k+0)` as the 0.95 fact. With any type prior that even slightly favors procedures, the
  weak 0.60 procedure **ties or beats** the strong 0.95 fact. Magnitude is gone by construction —
  the F080 §2 Approach A "Con" admits this ("a 0.95 fact and a 0.55 fact at ranks 0/1 fuse the
  same as two 0.6 facts").
- **Approach B (affine-preserving):** `calibrate(0.95, fact) = 0.95`,
  `calibrate(0.60, procedure) = (1-w)·0.60 + w·utility ≤ 0.60 + w`. With `w=0.15` and even a
  perfectly-effective procedure, the procedure tops out at `0.51 + 0.15 = 0.66 < 0.95`. The
  dominant fact **stays on top**, where relevance says it belongs.

This is the everyday prod case: the agent asks something a fact answers crisply, and there happens
to be a tangentially-related effective procedure. B serves the fact; A risks burying it under a
procedure that merely "ranked first among procedures." Keeping magnitude is not academic — it's the
difference between answering the question and surfacing a vaguely-related habit.

---

## 6. Acceptance criteria (F080 §3) — walkthrough

1. **No mixed-scale sort.** ✅ After `_calibrate`, every value in `merged` is drawn from the single
   documented RRF-`[0,1]` space (identity for healthy types, de-boosted for procedures,
   rank-normalized for the embed-fail leg, censors excluded). The `[:limit]` cut at
   `heart.py:1110-1112` now compares like-for-like. **B fixes the cut where it happens; A would too,
   but B keeps magnitude while doing it.**
2. **CE-off correctness.** ✅ **Emphasis.** `_calibrate` is pure arithmetic on already-fetched
   scores — zero model calls, zero `sentence-transformers`, zero latency. It does **not** depend on
   CE or MMR; it is precisely the re-basing step that CE/MMR would otherwise have done, made cheap.
   This is B's headline advantage given `bge ~67s` on the CPU-only prod VM and MiniLM rolled back.
3. **Byte-identical OFF.** ✅ **Emphasis.** Everything is behind `NOUS_COHERENT_RANKING_ENABLED`
   (default OFF). No producer SQL is mutated (§2). The only structural change is *adding* a
   `raw_hybrid_score`/`degraded` field — additive, changes no existing value. When OFF, the
   `else` branch at `heart.py:1109-1112` and the censor inclusion at `:889-890` run verbatim → the
   F051 `recall_deep` snapshot is byte-identical. This is *constructed*, not hoped for.
4. **Ordering hazards.** ✅ Calibration is followed by exactly one score sort at `:1110` (heart)
   and one at `retrieval_pipeline.py:278` (pipeline). The procedure de-boost removes the
   non-monotonic input to those sorts. (B-cog-A/B/D cognitive-path ordering fixes share the
   boost-sort root cause but live on a sibling surface — I scope them as a paired PR, F080 §6 q5.)
5. **Observability honest.** ✅ Thread a real `coherent_ranking_applied` (and the true
   `ce_reordered`/`mmr_active` already local to `_recall`) out into `PipelineStats`, fixing the
   `retrieval_pipeline.py:296-297` hardcoded-`False` (B5). Without this, no eval of B can be trusted.
6. **Measured.** ✅ **Emphasis + honest caveat in §7.** F051 paired A/B per-source MRR/P@k/R@k,
   gate: no per-source regression >3%, aggregate non-negative, displacement cases improve. Plus the
   synthetic unit test (§ test plan F080 §5) to *prove the mechanism* independent of corpus mix.
7. **No new hot-path latency.** ✅ `_calibrate` is O(n) arithmetic over the already-materialized
   candidate list. No I/O, no model. Strictly cheaper than the CE-revival path (Approach C).

---

## 7. Honest failure modes (what falsifies Approach B)

1. **Fixed-map calibration drift.** My affine maps are mostly identity, so there is little to
   drift — but the *moment* I introduce a non-identity map for any type (e.g. if a future reviewer
   insists on normalizing censors in rather than excluding), that map needs distribution data I may
   not have pre-ship, and it will drift with the embedding model (small→large). **This is the real
   weakness, and it is exactly why I exclude censors rather than map them.** If the debate forces
   censor-normalize-in, B's con becomes load-bearing.
2. **Embed-fail rank-normalization is set-dependent.** The one leg that *is* candidate-set-sensitive
   is §2's degraded-leg re-rank. I keep it rank-based (not min-max) to stay degeneracy-immune, but a
   1-result degraded leg maps to a single point I picked by convention — defensible, not derived.
3. **The procedure raw-hybrid wire change is real surface area.** Carrying `raw_hybrid_score` touches
   `ProcedureSummary` + its constructor + the calibration read site. More places to get wrong than
   Approach A's pure rank fusion (which never needs raw magnitudes). I concede this in §8.
4. **Measurability dilution (the honest one).** B's wins are *concentrated* in queries where a
   censor or effective procedure would have displaced a more-relevant fact. If the F051 corpus is
   thin on censors/effective-procedures, aggregate MRR may **barely move** — I'll see "no
   regression" rather than "improvement," and a skeptic can call B a no-op. Mitigation: the §5
   synthetic unit test proves the *mechanism* deterministically; F051 proves *no-regression*. But I
   won't pretend the aggregate number will swing hard.
5. **What falsifies B outright:** if removing the pathologies does **not** improve the displacement
   cases in the synthetic test, then the pathologies were never actually displacing relevant items,
   and B's entire premise is dead. That test is the go/no-go. (It also falsifies the whitepaper's
   headline if it fails — so it's worth running first.)

---

## 8. Self-score (1–10)

| Axis | Score | Justification |
|------|-------|---------------|
| **Correctness** | **9** | Fixes the bug *at the truncation site* (`heart.py:1110`) where it actually drops facts, and uniquely **preserves relevance magnitude** (§5) — the thing CE/MMR would have preserved and A discards. Premise verified against the prod `_rrf_merge_n` path (`search.py:283-288`). |
| **Prod-safety** | **9** | Byte-identical OFF is *constructed* (gate everything, mutate no producer, additive fields only — §2/§3), not asserted. CE-independent, O(n), zero new I/O. The half-point off: the `raw_hybrid_score` field touches a shared DTO. |
| **Change-footprint** | **6** | **I concede this to Approach A.** B needs: `_calibrate` block at `heart.py:983-988`, the `ProcedureSummary.raw_hybrid_score`/`degraded` wire, the censor `heart_types` exclusion, the pipeline-side mirror at `retrieval_pipeline.py:220`, and the `PipelineStats` honesty fix. Pure rank-fusion-over-`_rrf_merge_n` is a smaller diff. B buys correctness/magnitude with footprint. |
| **Measurability** | **7** | The mechanism is unit-testable deterministically (§5 synthetic set) and F051-gateable for no-regression. Docked because the *aggregate* win is corpus-dependent and may under-read if the eval set is censor/procedure-thin (§7.4). |

**Net:** Approach B is the only spine that fixes the merge **with CE off** *and* keeps the
relevance-magnitude signal that distinguishes a 0.95 dominant fact from a 0.60 best-available
procedure. It concedes change-footprint to rank fusion and concedes that its aggregate win is
concentrated rather than broad — but it wins on correctness and is provably reversible. The two
go/no-go invariants (verified, not polish): (a) `_rrf_merge_n` normalizes to `[0,1]` — **confirmed
`search.py:283-288`**; (b) all neutralization is flag-gated in `_recall` with no producer mutation —
**the byte-identical-OFF guarantee rests entirely on this.**
