# F080 — Coherent Cross-Type Retrieval Ranking

**Status:** DRAFT v0 (scoping + open design question) — pending multi-agent debate
**Source:** `docs/reviews/memory-retrieval-whitepaper-2026-06-08.md` §7–§8
**Owner:** retrieval
**Stakes:** HIGH — touches the recall hot path (`heart._recall`, `run_recall_pipeline`) that every `recall_deep` call and the eval harness depend on.

---

## 1. Problem

The white-paper audit found that Nous's per-type search is sound (RRF hybrid, normalized `[0,1]`) but the **cross-type merge sorts incompatible score spaces as one**:

| Type | Score space at merge | Code |
|------|----------------------|------|
| Fact / Episode | normalized-RRF `[0,1]` | `search.py:110-115` |
| Procedure | boosted RRF, **can exceed 1.0** | `procedures.py:446-457` |
| Censor | **raw cosine hard-floored ≥0.7** | `censors.py:387,400` |
| Embed-failure leg (any type) | un-normalized FTS `~0.06` | `search.py:220-222` |

`heart._recall` copies each type's score verbatim (`heart.py:983-988`) and, with cross-encoder and MMR **off** (production reality, `.env.prod-snapshot`), sorts globally and truncates: `merged.sort(key=score)[:limit]` (`heart.py:1110-1112`). A **hard floor** (censors) and an **unbounded boost** (procedures) break the monotonic score↔relevance relationship *across types*; the `[:limit]` cut then **drops genuinely relevant facts** before the pipeline ever sees them. The pipeline's `rerank_by_score` sort (`retrieval_pipeline.py:277-278`, prod-on via chunks) repeats the mistake over the full cross-leg pool.

The two stages that would re-base everything into one comparable space — F042 cross-encoder and F030 MMR — are **exactly the two disabled in production** (CE because `bge-reranker-v2-m3` runs ~67s on the CPU-only 8 GB prod VM; MiniLM was rolled back). So the fix **cannot assume CE is available**.

### In scope (the coherence cluster + cheap ordering/observability fixes)

| ID | Issue | Location | Sev |
|----|-------|----------|-----|
| B1 | Censors at ≥0.7 floor displace mid-ranked facts | `censors.py:387,400` → `heart.py:1110` | P1 |
| B2 | Procedure boost pushes score >1.0 | `procedures.py:446-457` | P2 |
| B3 | Embed-failure leg returns un-normalized FTS ~0.06 | `search.py:220-222` | P2 |
| B4 | Pipeline `rerank_by_score` merges mixed scales | `retrieval_pipeline.py:277-278` | P1 |
| B5 | `PipelineStats.ce_reranked/mmr_applied` hardcoded False | `retrieval_pipeline.py:296-297` | P2 |
| B-cog-A | Recalled IDs collected pre-truncation → false "unused" penalty | `context.py:608-613` vs `:617` | P2 |
| B-cog-B | Relevance gap-filter runs on boost-sorted (non-monotonic) list | `search.py:434`, `context.py:961,992-1005` | P2 |
| B-cog-C | `_dedup_decisions` dead; decisions get no conversation dedup | `context.py:541,1415` | P2 |
| B-cog-D | `query_text` is a set-ordered keyword bag (nondeterministic) | `intent.py:138,192-193` | P2 |

### Out of scope (separate follow-ups — note, do not fix here)
- Graph-layer issues (B-graph-5..9), spreading-activation density gate, adjacency-boost ordering — F081 candidate.
- Unifying the two recency resolvers + two relevance pipelines (cognitive vs recall_deep) — larger refactor.
- Dead-code removal (date-aware boost F075 L3) — trivial cleanup PR.
- Embedding-model doc drift (small vs large) — doc-only.

---

## 2. The OPEN design question (to be resolved by debate)

**How do we make per-type scores comparable at the merge point, given CE is unavailable in prod?**

Three candidate approaches are sketched below. The debate must (a) pick a spine, (b) decide the **censor question** and the **procedure-boost question** as sub-decisions, and (c) prove the choice works with CE off and does not regress the byte-identical `recall_deep` snapshot when the feature flag is off.

### Approach A — Second-stage rank fusion (RRF over the per-type ranked lists)
Treat each type's already-ranked result list as an input ranking and fuse them with the **existing** RRF machinery (`_rrf_merge_n`, `search.py:359-377`), optionally with per-type weights (= explicit type priors). Scale-free by construction (uses *ranks*, not raw scores), so the censor floor / procedure >1.0 / embed-fail ~0.06 problems all dissolve — a item's contribution depends only on its rank within its own type.
- **Pro:** reuses proven, normalized RRF; parameter-light; immune to all four score-space pathologies at once; works with CE off.
- **Con:** discards *magnitude* information (a 0.95 fact and a 0.55 fact at ranks 0/1 fuse the same as two 0.6 facts); type priors become an explicit tuning surface; needs a defensible default weight vector.

### Approach B — Per-type score calibration to a common `[0,1]`
Normalize each type's scores into one space before merge (min-max over the candidate set, or a fixed per-type affine map), and **remove the magnitude pathologies at the source**: strip the censor hard-floor from the *comparable* score, apply the procedure utility boost as a post-normalization tie-adjust rather than a raw multiplier, and route embed-failure legs through the same normalization.
- **Pro:** preserves magnitude/relevance signal; conceptually direct; per-type maps are inspectable.
- **Con:** min-max is outlier-sensitive and unstable on tiny candidate sets; fixed affine maps need calibration data per type; more surface area to get wrong.

### Approach C — Make CE viable in prod (single relevance model orders everything)
Confront the latency constraint: a smaller/quantized cross-encoder, top-N-only reranking with a strict wall-clock budget and fail-open, and/or async precompute, so the cross-encoder can be the single comparable scorer in prod.
- **Pro:** strongest relevance signal; collapses the whole problem to one model; CE infra already exists (F042).
- **Con:** empirically falsified once already (BGE 67s, MiniLM rolled back); reintroduces a heavy dependency on the hot path; latency risk to `recall_deep`; may still need a fallback ranker (so doesn't fully replace A/B).

### Sub-decision 1 — The censor question
Censors already have a dedicated surface ("Active Censors" / "Active Guidance", `context.py:460`). **Should `recall_deep` even retrieve censors into the ranked pool?** Excluding `"censor"` from `heart_types` (`retrieval_pipeline.py:340-341`) is a one-line change that removes B1 entirely without any normalization. Debate: exclude vs normalize-in.

### Sub-decision 2 — The procedure-boost question
The utility boost (`procedures.py:446-457`) is a real signal but it breaks the `[0,1]` invariant. Options: keep boost but clamp to `[0,1]`; move boost to a post-normalization re-rank within the procedure slice; or fold utility into the fusion weight. Debate.

---

## 3. Acceptance criteria (independent of which approach wins)

1. **No mixed-scale sort.** After the fix, the final ranking compares values drawn from a single, documented space (or ranks). No raw score from one type can structurally out- or under-rank another type independent of relevance.
2. **CE-off correctness.** The fix produces a coherent ranking with `NOUS_CROSS_ENCODER_ENABLED=false` and `NOUS_MMR_ENABLED=false` (prod config). It must not *depend* on CE.
3. **Flagged + reversible.** New behavior behind a flag (`NOUS_COHERENT_RANKING_ENABLED`, default OFF). When OFF, `recall_deep` output is **byte-identical** to its committed snapshot (the F051 harness invariant).
4. **Ordering hazards fixed.** Boost/staleness/usage mutations are followed by a score re-sort (or the gap-filter sorts its own input); recalled IDs are collected **after** truncation; `query_text` is deterministic.
5. **Observability honest.** `PipelineStats.ce_reranked`/`mmr_applied` reflect what actually ran inside `heart._recall`.
6. **Measured.** The change is evaluated on the F051 retrieval harness (per-source MRR/P@k/R@k) and, if feasible, the BEAM harness. Target: no per-source regression > 3%; aggregate non-negative; the censor/procedure displacement cases improve.
7. **No new hot-path latency** beyond a documented bound (rank fusion is O(n); any CE-revival path must hold a wall-clock budget with fail-open).

---

## 4. Integration points (verified against source)

- **Merge site:** `nous/heart/heart.py:983-988` (score copy), `:1110-1112` (sort+truncate). Primary surgery.
- **RRF infra to reuse:** `nous/heart/search.py:76-117` (`_rrf_merge`), `:359-377` (`_rrf_merge_n`).
- **Score sources to neutralize:** `censors.py:387,400`; `procedures.py:446-457`; `search.py:220-222` (embed-fail path).
- **Pipeline sort:** `nous/api/retrieval_pipeline.py:277-278`; type-keyed exclusion `:286-293`; stats `:296-297`.
- **Cognitive ordering fixes:** `nous/cognitive/context.py:541,608-617,945-1005`; `nous/heart/search.py:399-435`; `nous/cognitive/intent.py:138,192-193`.

## 5. Test plan (sketch)
- Unit: a synthetic merged set with one censor@0.78, one procedure@1.26, one fact@0.92, one fact@0.55, one embed-failed fact@0.06 → assert the post-fix ranking places them by *relevance intent*, not raw magnitude.
- Snapshot: flag-OFF `recall_deep` byte-identical to committed snapshot (postgres_only).
- Determinism: same input twice → identical `query_text` and identical ranking.
- Harness: F051 paired A/B, per-source no-regression gate.

---

## 6. Open items for the debate to close
1. Spine: A / B / C / hybrid?
2. Censor sub-decision: exclude vs normalize?
3. Procedure-boost sub-decision: clamp / post-rank / fold-into-weight?
4. If type priors are introduced (Approach A), what is the default weight vector and how is it justified without overfitting?
5. Is the cognitive-path ordering fix (B-cog-A/B) part of this feature or a sibling PR? (It shares the boost-sort root cause.)
6. Minimal-change footprint vs principled-but-larger: where is the line for a single shippable PR?

---

# RESOLVED DESIGN (post-debate)

**Status:** v1 — design closed. Architecture-review gate satisfied by the multi-agent debate below.
**Forge decision:** `a18e0836`.

## 7. How the design was decided (debate record)

A 5-agent debate ran on the open question in §2. Round 1: three champions argued competing spines. Round 2: a devil's-advocate attacked all of them and an independent synthesis-judge adjudicated. A final advisor pass broke the remaining tie. Briefs of record: `docs/reviews/F080-approach-A-rank-fusion-champion.md`, `docs/features/F080-approach-B-calibration-champion.md`, `docs/reviews/F080-approach-C-champion.md`.

| Approach | Champion self-score (corr/prod/footprint/meas) | Verdict |
|----------|-----------------------------------------------|---------|
| A — 2nd-stage rank fusion | 8 / 8 / 8 / 9 | **Rejected as spine.** Devil P1-b: RRF over *disjoint* type-sets has no reinforcement term — it degenerates to weighted rank-interleave, and the default type priors (fact 0.45 vs procedure 0.30, k=60) make *every fact down to rank-29* outrank the single best procedure. No benchmark justification for the weights. |
| B — per-type calibration | 9 / 9 / 6 / 7 | **Adopted as spine (at S1).** Verified premise: 3/4 heart types already emit normalized RRF `[0,1]`; the defect is exactly two un-normalized deviants (procedure boost, censor floor). |
| C — CE revival | 8 / **4** / 7 / 7 | **Deferred to F080.1.** Champion concedes standalone C fails acceptance #2 (prod is CE-off) and adds 1.6–3.6 s hot-path latency; it is really "A/B spine + optional CE head." |

**Tie-break (devil vs judge → advisor).** The judge picked B and claimed S2 is then "already coherent." The devil (P1-a) showed that conflates *range* `[0,1]` with *comparable distribution* — at the pipeline sort (`retrieval_pipeline.py:277`, prod-on) chunk-cosine, fact-RRF, and graph-edge-weight are all `[0,1]` but drawn from different distributions, so B alone leaves a residual S2 mismatch. The advisor broke the tie: a blanket S2 rank-normalization (my initial lean) **transposes** the magnitude bug (best-of-a-weak-leg → ties best-of-a-strong-leg) and is *actively wrong* for the graph leg (`edge_weight×0.7 ≤ 0.70` is meant to keep associative neighbors below direct hits). Decisive fact: **B@S1 fully kills the two P1 *displacers*** (censor, procedure) — the headline bug — and the residual S2 chunk/graph mismatch is milder, corpus-dependent, and best **measured** before prod-path surgery (the LME +26pp chunk win was obtained *with* chunks already at raw-cosine at S2, so "chunks get buried" may be overstated).

## 8. Verdict

> **REVISED 2026-06-08 (owner correction) — see §13.** The procedure sub-decision below changed from *calibrate-to-compete* to *exclude-from-pool*. §13 is authoritative where it differs from §9.3.

**Spine = Approach B applied at S1 (`heart._recall`):** exclude **censors and procedures** from the ranked recall pool; surface them through their dedicated channels (Active Censors section; F079 Procedure Catalog + `get_procedure`). This removes both P1 displacers (B1, B2) **by exclusion**, at the merge, *before* the `[:limit]` cut that drops facts. The remaining ranked pool — facts, episodes, decisions — is knowledge that already shares the normalized RRF `[0,1]` space, so **no per-type calibration is required**. The S2 cross-distribution mismatch (chunk-cosine, graph-edge-weight vs RRF) is **scoped out** to F080.1 behind an F051 measurement gate — F080 explicitly does **not** claim to fully coheres S2, which converts the devil's P1-a from a "kill" into a correct, stated boundary.

## 9. The optimized design (buildable)

### 9.1 Flags & config (`nous/config.py`)
- `NOUS_COHERENT_RANKING_ENABLED: bool = false` — master switch. When OFF, `recall_deep` is byte-identical to the committed F051 snapshot.
- `NOUS_PROCEDURE_UTILITY_WEIGHT: float = 0.15` (`ge=0.0, le=1.0`) — the `w` in the convex transform.
- Resolve via the existing `RuntimeConfig` resolver pattern (mirror `get_cross_encoder_enabled`), not raw `settings` on the hot path.

### 9.2 Censor **and procedure** exclusion (two sites — both required)
- `nous/heart/heart.py:877` — when coherent ranking is on, strip both `"censor"` and `"procedure"` from `search_types` before `search_map` is built.
- `nous/api/retrieval_pipeline.py:340-347` — strip both from `heart_types` (this also governs the `heart_section_eligible` formatter gate).

  Both sites needed; one alone leaves a half-exclusion. **Verified safe:**
  - *Censors* — enforcement via `heart.check_censors` (`layer.py:777`), display via `heart.list_censors` (`context.py:463`); neither reads the ranked recall pool.
  - *Procedures* — breadth via the F079 Procedure Catalog (`context.py:265-364`, prod-on), depth via the `get_procedure` tool (`tools.py:1006`, in the conversation/question/decision/creative/debug frames). `recall_deep`'s documented contract is "decisions, facts, episodes" — procedures were never part of it.
  - Honest caveat: a query-relevant censor that didn't fit the budget-truncated "Active Censors" section is no longer recoverable via recall (marginal). For procedures, exclusion makes the catalog the **sole** discovery surface — see the binding companion requirement in §13.

### 9.3 Procedure handling — SUPERSEDED by §13
> The original §9.3 proposed a convex utility transform to keep procedures *in* the ranked pool with a bounded score. The owner correction (§13) replaces this with **exclusion** (procedures are capabilities, not knowledge; they have their own catalog + selection surface). The convex transform and the `ProcedureSummary.raw_hybrid_score` wire change are **withdrawn**. See §13 for the authoritative procedure decision and its companion catalog-breadth requirement.

### 9.4 Composition discipline (mandatory — the devil's verified traps)
- `PipelineResult` is `@dataclass(frozen=True)` → **never mutate in place; use `dataclasses.replace`.**
- The calibrated value is a **base score**. The prod-ON recency resolver (`×0.3`, `retrieval_pipeline.py:258`) and adjacency boost (`:246`, internal re-sort `:791`) must multiply *on top of* it. Because B's calibration happens at S1 (inside `heart._recall`, before the pipeline), the pipeline multipliers compose naturally and `:246/:258/:277` are **unchanged**. Do not add a second sort.
- Adjacency boost can lift a calibrated `[0,1]` base to ~1.15 — document this as the new logged ceiling so >1.0 scores in dashboards aren't mistaken for the old bug.

### 9.5 Observability (partial B5)
Add `coherent_ranking_applied: bool` to `PipelineStats` (`retrieval_pipeline.py:295`), set from the flag. (Full `ce_reranked`/`mmr_applied` truth-threading out of `heart._recall` is its own signature change → F080.1.)

## 10. Phasing

**F080 (this PR):** §9.1–§9.5. Files: `config.py`, `heart/schemas.py` (ProcedureSummary field), `heart/procedures.py` (wire), `heart/heart.py:877,983-988` (exclude + transform), `api/retrieval_pipeline.py:340-347,295` (exclude + stat). Tests: synthetic-merge unit test + flag-OFF byte-identical snapshot + determinism. F051 paired A/B **plus two new probes** (chunk-vs-fact, graph-vs-direct).

**F080.1 (deferred, data-gated):** S2 cross-distribution coherence — method chosen *per leg* from the F051 probes (chunks may want rank/percentile normalization; graph likely stays low). Optional budget-bounded CE head over the B spine (requires prod-VM N≈10 MiniLM latency measurement + `Semaphore(1)` CPU gate + warm-up + the displacement probes). B3 embed-failure leg re-normalization (only if the manager-level `degraded` tag stays off the shared `hybrid_search` return contract — else it breaks byte-identical-OFF; defer if invasive). Full CE/MMR stat threading.

**Sibling PR (same release):** cognitive-path ordering fixes that share the boost-sort root cause — B-cog-A (collect recalled IDs *after* `_truncate_to_budget`, `context.py:617`), B-cog-B (re-sort by score after frame/usage boost before the gap filter, `search.py:434`/`context.py:961,992-1005`), B-cog-C (wire or delete dead `_dedup_decisions`, `context.py:541,1415`), B-cog-D (deterministic `query_text`, `intent.py:138,192-193`).

## 11. Acceptance & tests (supersedes §3 where they differ)
1. **No P1 displacer.** Unit test on a synthetic merged set (censor@0.78, procedure raw=0.60/eff=0.9, fact@0.92, fact@0.55, embed-failed fact@0.06): flag-ON ⇒ censor absent; procedure calibrated to `(1−0.15)·0.60 + 0.15·0.9 = 0.645`; the 0.92 fact ranks #1; the procedure sits between the two facts.
2. **CE-off correctness.** Pure arithmetic, zero model/IO — passes with CE+MMR off (prod).
3. **Byte-identical OFF.** Existing `postgres_only` snapshot test passes unchanged with the flag default OFF (rests on: no producer SQL mutated, `raw_hybrid_score` defaults `None`).
4. **Determinism.** Same input twice ⇒ identical ranking (this PR's surface; `query_text` determinism is the sibling PR).
5. **Observability.** `coherent_ranking_applied == True` under flag-ON.
6. **Measured + S2 probes.** F051 paired A/B: no per-source MRR regression > 3%; aggregate ≥ 0; **plus** new chunk-vs-fact and graph-vs-direct ranking probes whose results are the explicit input to the F080.1 S2 decision (and a guard that F080 did not regress the chunk win).
7. **No new hot-path latency.** O(n) arithmetic over the already-materialized candidate list; no IO. State the bound in the PR.

## 12. Residual risks for the implementer
1. `raw_hybrid_score` must reach `_recall` intact via `ProcedureSummary`; audit every construction site (incl. cognitive path) for breakage.
2. Censor exclusion at **both** sites or it half-applies; assert end-to-end absence in the unit test.
3. Byte-identical-OFF rests on build discipline — no producer SQL touched; all behavior behind the flag; verify with the snapshot test after any change to the score-copy loop.
4. Do **not** overwrite `r.score` anywhere the recency/adjacency multipliers run; keep the single sort at `:277`.
5. The chunk/graph S2 residual is **knowingly** unfixed in F080 — the acceptance-#6 probes exist to size it; do not silently "fix" it with a blanket rank-norm (it transposes the magnitude bug and mis-ranks graph).

---

## 13. Procedure decision — REVISED (owner correction, 2026-06-08) — AUTHORITATIVE

**Decision: exclude procedures from the ranked recall pool entirely (like censors); do not calibrate them to compete.**

**Rationale.** Procedures/skills are *capabilities*, not *knowledge*. Ranking them by relevance against facts/episodes in a single top-K pool is a category error — and redundant, because F079 already provides the correct surfaces: the **Procedure Catalog** (breadth — the agent sees its skills, `context.py:265-364`, prod-on) and the **`get_procedure`** tool (depth — the agent deliberately selects a skill for the task and loads its body, `tools.py:1006`). Skill selection should be a **task-conditioned agent decision over the catalog**, independent of how semantically similar a stored memory happens to be. `recall_deep`'s own contract ("decisions, facts, episodes") confirms procedures were never meant to be in that pool.

**Consequences (net simplification vs §9.3):**
- **B2 is fixed by exclusion**, not calibration. The convex utility transform and the `ProcedureSummary.raw_hybrid_score` wire change are **withdrawn**.
- After excluding censors **and** procedures, the S1 recall pool is **facts + episodes** — both normalized RRF `[0,1]`, already comparable. Decisions (RRF) join at S2. **No per-type calibration is required anywhere in F080.** The feature reduces to: *the ranked recall pool is knowledge only.*
- Remaining F080 surgery: the two-site exclusion (§9.2), the `coherent_ranking_applied` stat (§9.5), and the frozen-`replace()` / base-before-multipliers discipline (§9.4) — which is now trivially satisfied because nothing rewrites a score (we only *remove* two types).

**Companion requirement (BINDING — must ship with F080 or as an immediate predecessor).** Excluding procedures makes the catalog the **sole** skill-discovery surface, so it must present **full breadth**. Current prod caps: `proc_catalog_max=100` (covers the ~55 deduped procedures) but `proc_catalog_max_chars=4000` with `proc_catalog_desc_chars=120` ⇒ only **~28 rows fit** — ~27 skills would be invisible. Fix one of:
  - compact the catalog row format (name + terse ≤60-char descriptor) so all ~55 fit in ~2–3 K chars (preferred), and/or
  - raise `proc_catalog_max_chars`, and
  - order the catalog by utility/effectiveness (F037 signal) so that if truncation ever bites, the most-useful skills survive.
  Acceptance: assert every active procedure name appears in the rendered catalog for a representative agent, OR is reachable by `get_procedure` by a name the agent can see.

**Sibling consideration → now specified in §14.** The same principle applies to the cognitive path's procedure injection. That redesign (critic-preload + name-menu + drop the embedding path) is specified concretely in §14 and supersedes the "full-breadth catalog with descriptions" companion requirement above — because once bodies come from critic-preload or `get_procedure`, the breadth surface only needs **names**, which dissolves the `proc_catalog_max_chars` truncation entirely.

**Updated phasing.** F080 (this PR) = exclude censors + procedures from the ranked pool (§9.2) + `coherent_ranking_applied` stat (§9.5) + F051 probes (chunk-vs-fact, graph-vs-direct). No calibration, no `ProcedureSummary` change. The cognitive-path procedure redesign (the breadth-menu + critic-preload that this exclusion leans on) is **§14**, shipped as its own PR. F080.1 unchanged from §10.

---

## 14. Cognitive-path procedure injection redesign (sibling feature — design only)

**Relationship:** F080 (§13) removes procedures from the `recall_deep` ranked pool. This section redesigns how procedures enter the **every-turn system prompt** so that removal loses nothing. Together they realize one principle: *procedures are selected for the task and loaded, never relevance-ranked against memory.* This evolves the F079 injection model; it ships as a **separate PR** from the F080 recall_deep exclusion.

### 14.1 Current behavior (verified, prod config)
- **Track A — Critic (live, prod `NOUS_CRITIC_SKILL_INJECTION=enabled`, `NOUS_CRITIC_MODE=advised`, Haiku):** each task-ish turn the critic recommends ≤ `critic_skill_slots` (prod **2**) skill names; `context.py:656-666` fetches each as a full `ProcedureDetail` via `get_procedure_by_name`. **But** when the catalog rendered, `context.py:755-780` emits only a *name pointer* — `"★ Recommended for this task: \`name\` — load with get_procedure"` — **not the body** (F079 "option C", to save tokens). So the LLM still round-trips through `get_procedure` to act.
- **Track B — embedding passive injection:** `context.py:686-744`, gated `proc_passive_injection_enabled AND not catalog_rendered` (`:647-650`). **Inert in prod** (the catalog renders, so this never runs). When it *does* run it pushes procedures through the staleness → frame-boost → dedup → usage-boost → relevance pipeline — i.e. the same boost-sort-then-gap-filter hazard as B-cog-B.
- **Catalog (F079):** `context.py:265-364`, name + ≤`proc_catalog_desc_chars` (120) description, capped by `proc_catalog_max_chars` (4000) ⇒ ~28 of ~55 rows.
- **`get_procedure`:** `tools.py:1006` — on-demand body load, agent-selected.

### 14.2 Target design — three surfaces
1. **Recommended Procedures (preloaded bodies).** Replace the name-pointer at `context.py:762-780` with the **actual body** (or a rich, bounded summary) of the procedures chosen by the **selection mechanism in §14.7** (graph-primary K-line activation, critic fallback). Proactive depth: the common case ("the skill I need is one the system surfaced") needs no `get_procedure` round-trip. Bounded by the slot count and a per-body char cap.
2. **Skill Menu (names only, full breadth).** Reduce the catalog to **name + ≤~60-char purpose for *every* active procedure**. All ~55 fit in ~1–2 K chars, so `proc_catalog_max_chars` truncation no longer hides skills. This is the discovery backstop that makes "the LLM can `get_procedure` more" actually usable — and the safety net for a critic miss (the LLM browses the menu and pulls by name).
3. **`get_procedure` (unchanged).** On-demand depth for anything in the menu the critic didn't preload.

### 14.3 Changes
- **Delete / hard-disable Track B** (embedding passive injection, `context.py:686-744`). Already inert in prod; removing it also deletes a procedure-path instance of the B-cog-B boost-sort hazard. (Keep `proc_passive_injection_enabled` as a dead-off compatibility flag or remove it.)
- **Preload critic bodies** in the "Recommended Procedures" section (the `catalog_rendered and critic_procedures` branch), with a body/summary size cap.
- **Compact the catalog to a full-breadth name menu** (name + terse purpose, all active procedures; order by utility/effectiveness so any future truncation drops least-useful first).
- `get_procedure` unchanged.

### 14.4 The honest tradeoff
Preloading bodies spends tokens that the F079 name-pointer deliberately saved. Bounded (≤2 picks) but real. Mitigations: cap preloaded body chars; preload a rich *summary* rather than the full body; or preload only the top-1 when the procedure budget is tight. This is a proactive-vs-lazy choice — worth it only if the critic's picks are usually the ones acted on (measure below).

### 14.5 Measurement (gates the design)
- **Critic recall@2** — how often the Haiku critic's ≤2 picks contain the skill the turn actually needs. High ⇒ body-preload is a clear win; low ⇒ lean on the menu + `get_procedure` and keep preload minimal.
- **Menu legibility** — names + one-liners must be self-explanatory (this is why the procedure-dedup description backfill mattered); spot-check that an LLM can pick the right skill from the menu alone.

### 14.6 Scope
Design only — no code here. Separate PR from F080's recall_deep exclusion; sequence either order (they're independent), but ship both before flipping any "procedures excluded from recall" flag in prod so the menu+selection surfaces are in place first.

### 14.7 Selection mechanism — graph-primary (K-line) with critic fallback

**Decision: graph-driven K-line activation is the primary selector; the §14.1 critic is the fallback; a task-only match is the final floor.** Procedure selection is conditioned on the *situation* (the turn's recalled memory), not just the bare task string — the task triggers and anchors the goal; the retrieved memory determines which skill the situation calls for.

**Local density measurement (`graph_latest.json`, 2026-06-08).** 23/23 procedures linked (100%, F040 `auto_linked` edges). 56 procedure-touching edges: **procedure↔fact 29**, procedure↔procedure 20, procedure↔decision 7, **procedure↔episode 0**. So the live activation substrate is overwhelmingly **recalled-fact → procedure** (and facts are the most common recall result), with procedure↔procedure for neighborhood expansion. (Export likely capped/stale — 23 vs 55 active prod procedures — so re-measure on the live/local instance; the *structure* is what the design relies on.)

**Primary — K-line activation (structural, not cosine).** After the turn's recall produces seed memories, for each seed call `brain.neighbors(seed.id, node_type=seed.type, neighbor_type="procedure")` (the same edge-traversal F080 uses for graph expansion — reuse it). Score each activated procedure as `edge_weight × seed_recall_score` (a procedure linked to a highly-relevant recalled memory ranks high). Optionally one-hop expand `procedure↔procedure` (the 20 edges) to pull closely-related skills once one fires, bounded. Dedup, take top-`slots`. This is memory-driven **but edge-based** — it does not reintroduce the cosine coupling §13 removed.

**Fallback ladder:**
1. **Graph K-line** (above) fills the recommended slots.
2. **If it yields fewer than `slots`** (sparse cues / no procedure edges hit this turn) → **critic** (§14.1: Haiku, advised, conditioned on `task goal + recalled memory + skill menu`) fills the remainder.
3. **If both are empty** → **task-only floor** (direct name/intent match against the menu) so something obvious still surfaces.

The selected set (graph ∪ critic, capped at `slots`) is what gets its **bodies preloaded** in the §14.2 "Recommended Procedures" section; the name-menu (§14.2.2) and `get_procedure` remain the breadth + on-demand paths.

**Honest density caveats (carry into measurement).**
- Cue off **all** recalled types, not just decisions/episodes — facts are the dense substrate (29 edges) and decisions are thin (7), episodes absent (0). A decision/episode-only activation would mostly miss.
- With proc↔fact at 29 edges over ~1011 facts, a given turn's ~10 recalled items won't always hit a procedure edge — so the **fallback carries a meaningful share of turns today**. That's expected and correct; measure the graph-primary hit-rate to size it.
- **Upgrade path:** grow procedure↔decision and procedure↔episode density (F012 K-line learning, which the audit found at 0; F040 densification with procedure-aware thresholds) so situational (not just topical-fact) cues activate skills. Until then, graph-primary leans on fact cues and the fallback covers the rest.

**Measurement (gates the "graph as main" emphasis).** Graph-primary hit-rate (fraction of task turns where K-line activation fills ≥1 slot) and selection precision@slots (are the activated procedures the ones acted on?) vs the critic fallback. If hit-rate is high, graph leads as designed; if low, it's effectively critic-led until density grows — either way the output is correct, only the *driver mix* shifts.
