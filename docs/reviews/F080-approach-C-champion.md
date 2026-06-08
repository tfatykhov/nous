# F080 Approach C — Champion Case: Make the Cross-Encoder the Single Comparable Scorer

**Role:** Champion of Approach C (CE-viable-in-prod).
**Stance:** Code-grounded, adversarially honest. The honest conclusion is stated up front so the debate can use it.
**Date:** 2026-06-08

---

## TL;DR (the thesis, stated against my own interest)

A cross-encoder scoring `(query, summary)` pairs is the **principled endgame** for cross-type coherence: one model, one score space, no type priors, no rank-fusion magnitude loss, B1/B2 dissolved *by construction* — see §2, §4. The F042 infra already exists and already feeds `text_fn` uniformly across all types (`heart.py:1023`).

**But standalone C does not satisfy F080's acceptance criteria, and I will not pretend otherwise.** Two structural facts force this:

1. **CE reranks the head only.** `reranker.py:103-104,167` returns `head + tail`; the tail keeps its incoherent RRF/boost/floor/FTS scores. So even on the happy path, *something coherent must order the tail.*
2. **The fail-open target in the brief is the bug.** "Fail open to the existing score sort" lands every timeout/error/CE-OFF path on the exact mixed-scale sort (`heart.py:1110-1112`, `retrieval_pipeline.py:277-278`) that F080 exists to kill. A coherent floor must exist underneath CE.

Therefore **C is really C+A or C+B**: an A/B-normalized spine that produces a coherent ranking with CE OFF (satisfying acceptance #2), with CE layered on as an *optional ON-path relevance enhancer for the head*. That is the strongest defensible form of this approach, and it is the most useful thing this doc contributes to the debate.

Everything below designs *that* hybrid honestly, including the latency wall that killed standalone CE once already.

---

## 1. The latency budget — measured, not hand-waved

### 1.1 Hard budget

**Wall-clock budget for CE on the recall path: 1.5s p50, 2.0s hard `asyncio.wait_for` ceiling.** Rationale: `recall_deep` is *sometimes* on a user-interactive path (whitepaper §1, framing) and already pays multiple embeds + vector search + graph expansion before CE. CE must be *additive headroom*, not the dominant cost.

### 1.2 The one measured prod number, and a dev calibration I ran this session

The only empirical prod datapoint is the ghost that killed this: **BGE-reranker-v2-m3 ≈ 67s for one rerank on the prod CPU-only 8GB VM** (`project_prod_bge_too_slow`, `reference_gpu_capability`). At N=30 candidates that is **~2.2s/pair** — catastrophic.

To turn that single number into a *defensible* MiniLM prod estimate, I benchmarked both models on this dev CPU box (`torch 2.11.0+cpu`, `device='cpu'`, 512-char docs, this session — reproducible):

| Model | Dev CPU, N=30 | Dev per-pair |
|-------|---------------|--------------|
| `BAAI/bge-reranker-v2-m3` (code default) | **6.23s** | ~208 ms |
| `cross-encoder/ms-marco-MiniLM-L-6-v2` (prod override) | **0.335s** | ~11 ms |

**This gives an empirical dev→prod scaling factor, not a guessed one:**

```
prod BGE N=30  = 67s   (measured on prod VM)
dev  BGE N=30  = 6.23s (measured here, same model, same N)
-------------------------------------------------------
dev→prod factor = 67 / 6.23 ≈ 10.8×
```

The prod VM is ~**10.8× slower per CE pair** than this dev box. (Consistent with an 8GB 1–2 vCPU VM vs a desktop CPU.) Apply it to MiniLM:

> **Two rigor caveats, both in C's favor.** (a) 10.8× is transferred from a *compute-bound* model (BGE, 560M params) to MiniLM, whose per-call time has a larger fixed-overhead fraction — my dev curve shows it: N=10=0.150s but marginal pairs are ~11ms, implying ~0.04s fixed cost. So a flat 10.8× likely *over*estimates MiniLM prod latency. The conclusion is robust either way; verify on the prod VM. (b) The benchmark used 512-**char** docs, which exactly matches the code (`reranker.py:112` `[:text_limit]` is a *character* slice; `text_limit=512`, `config.py:740`) — ≈128 tokens, well under MiniLM's 512-*token* max, so no padding/truncation surprises.

| N | MiniLM dev (measured) | MiniLM prod (× 10.8, estimated) | Under 2.0s ceiling? | Under 1.5s p50? |
|---|------------------------|----------------------------------|---------------------|------------------|
| 10 | 0.150s | **~1.6s** | ✅ | borderline |
| 15 | 0.188s | **~2.0s** | ✅ (at ceiling) | ❌ |
| 20 | 0.213s | **~2.3s** | ❌ | ❌ |
| 30 | 0.335s | **~3.6s** | ❌ | ❌ |

### 1.3 The honest verdict on "does N=20–30 fit under 1.5s?"

**No.** Back-of-envelope and the measured-then-scaled number agree:

- **N=30 (current `cross_encoder_max_candidates` default) ≈ 3.6s on prod CPU — fails the budget outright.** This is the single most important sentence in this doc. Shipping CE at the default N=30 re-creates a ~3.6s interactive stall — a milder ghost than 67s, but a ghost.
- **N=10 MiniLM ≈ 1.6s prod** is the only configuration that clears the 2.0s ceiling with margin, and it still misses 1.5s p50.
- To make **N=30 fit ~1.5s**, you need **~2.4× speedup** beyond MiniLM-fp32: **int8 dynamic quantization or an ONNX-runtime export** (`optimum`/`onnxruntime`, typically 2–3× on CPU). That is unmeasured here and would have to be benchmarked on the prod VM before any flag flip — I am not going to claim a number I haven't run on the target hardware.

**Design-to-budget decision:** ship **MiniLM-L6, N=10**, `wait_for(2.0)`, with N as an env knob (`cross_encoder_max_candidates` already exists, `config.py:739`) so ops can dial it down to 6–8 if the prod VM is slower than my 10.8× estimate. Treat N=30 + int8-ONNX as a *measured follow-up*, not a launch claim.

**Hard design constraint: do NOT raise `text_limit` toward a true 512 *tokens*.** Inference scales ~3–4× with sequence length; lifting the cap from 512 chars (~128 tok) to 512 tokens would push N=10 from ~1.6s to ~5–6s prod. The 512-char cap is *why* N=10 is viable — it is a latency feature, not an oversight.

### 1.4 The mechanism (reuses F042 verbatim where possible)

- **Model:** `cross-encoder/ms-marco-MiniLM-L-6-v2` (already the prod override, `.env.prod-snapshot:28`). Code default `BAAI/bge-reranker-v2-m3` (`config.py:738`) **stays the default for the offline F043 CE-backfill** (sleep-time, not latency-bound) — do not put BGE near the recall hot path.
- **N (head):** 10 on the recall path (down from 30). `reranker.py` already head/tail-splits at `max_candidates` (`:103-104`) — no code change, just config.
- **Batching:** single `model.predict(pairs)` call (already batched, `reranker.py:136`). N=10 is one batch.
- **Off-event-loop:** `asyncio.to_thread(model.predict, pairs)` — already done (`reranker.py:136`).
- **Strict timeout + fail-open:** wrap the existing `cross_encoder_rerank` call site (`heart.py:1020`) in `asyncio.wait_for(..., timeout=settings.cross_encoder_timeout_seconds)`. On `TimeoutError`/any exception → fall through to **the coherent A/B fallback ranker (§3), NOT the raw sort.**
- **Cold-start warm-up:** `_load_cross_encoder` is `lru_cache(maxsize=1)` (`reranker.py:38`) — first recall after restart eats the model load. Measured here: MiniLM cold load **2.86s on dev**. On prod this is disk-read + deserialize of an ~80MB model (I/O-bound, *not* FLOP-bound — so the 10.8× compute factor does **not** apply); realistically ~2–8s on a slow VM. Either way it **cannot** land on a user turn. Add a startup warm-up coroutine in `main.py` lifecycle that calls `_load_cross_encoder(model)` + a dummy `predict([("warm","warm")])` once at boot, behind the same flag.

---

## 2. Why CE is the *right* comparable scorer (the principled case)

This is where C is genuinely superior to A and B, and I'll make the strongest version.

**A single model scoring `(query, doc)` pairs makes all types comparable by construction.** The whitepaper's entire headline defect (§8.1) is that the merge sorts four incompatible spaces:
- normalized RRF `[0,1]` (facts/episodes),
- boosted RRF `>1.0` (procedures, `procedures.py:446-457`),
- raw cosine floored `[0.7,1.0]` (censors, `censors.py:387,400`),
- raw `ts_rank_cd ~0.06` (embed-failure leg, `search.py:220-222`).

**A and B both *manage* this incompatibility; C *eliminates* it:**

- **Approach A (rank fusion)** is scale-free but throws away magnitude — a 0.95 fact and a 0.55 fact at ranks 0/1 fuse identically to two 0.60 facts (F080 §A con). It also *introduces type priors* as an explicit weight vector you must justify without overfitting (F080 open item #4).
- **Approach B (per-type calibration)** preserves magnitude but needs per-type affine maps / min-max that are outlier-sensitive on tiny candidate sets and require calibration data per type (F080 §B con).
- **Approach C** needs **none of that.** The CE reads `(query, summary)` and emits one sigmoid-normalized relevance score in `[0,1]` for *every* type through the *same* function. There is no type prior, no calibration table, no rank-fusion magnitude loss. A censor, a procedure, a fact, and an episode are scored on **identical footing** because the model never sees the type — it sees text-vs-query.

**Code evidence this is real, not aspirational:** the F042 call site already passes `text_fn=lambda r: r.summary or ""` (`heart.py:1023`) — type-blind by construction *today*. The plumbing to score all types uniformly is already there; CE-OFF is the only reason it's latent.

This is the principled endgame: the merge problem is fundamentally "how do I compare relevance across types," and the most direct answer is "use a model that scores relevance directly." A and B are *proxies* for that model. **The catch — and it is decisive — is that C's relevance signal only covers the head (§3).**

---

## 3. The fallback ranker — the concession that defines the approach

**Be blunt: CE reranks `candidates[:N]` and returns `head + tail` (`reranker.py:167`). The tail keeps its incoherent scores. And in prod, CE is OFF, so 100% of the pool is "tail."** Three surfaces need a coherent ranker that is *not* CE:

1. **The tail** (`candidates[N:]`) on the happy path — N=10 head, everything below stays mixed-scale.
2. **The CE-OFF path** — prod's actual config (`.env.prod-snapshot:26`). The feature does *nothing* here unless a fallback orders the pool.
3. **The fail-open path** — timeout/exception. The brief says "fail open to the existing score sort," but that sort *is* the F080 bug. Failing open to it ships the defect on every CE hiccup.

**So C must be layered on a coherent A/B spine.** Concretely, I endorse **C + A (rank fusion as the floor)** because A is parameter-light, reuses `_rrf_merge_n` (`search.py:359-377`), and is immune to all four pathologies at once (F080 §A pro). The composition:

```
merged (cross-type pool)
  → A: rank-fusion / rank-normalize into one coherent space   [coherent floor — orders TAIL + CE-OFF + fail-open]
  → if CE enabled AND warm AND within budget:
        CE reranks head[:10] into sigmoid [0,1]                [relevance enhancer — orders HEAD]
        re-sort head; head + tail
  → truncate [:limit]
```

The fail-open target becomes "the A-normalized order," never the raw sort. This is the honest architecture. **C does not fully replace A/B. It sits on top of one.** Anyone arguing standalone C is the spine is wrong on acceptance #2 (§5).

One subtlety the whitepaper flags (B-rm-3, `reranker.py:152,167`): after CE overwrites head scores into sigmoid `[0,1]` but leaves the tail in A-normalized space, head and tail are again on different scales. With an A spine this is *benign* — both are `[0,1]`-ish and CE only ever promotes within the already-coherent head — but the truncation `[:limit]` must happen *after* the head re-sort, and we must not re-sort head-against-tail on raw value. Land CE-head strictly above the A-tail by construction (head items were the top-N of the A order; CE only reorders *within* them).

---

## 4. Censor + procedure sub-decisions under a CE regime

**Claim: CE dissolves B1 and B2 *for head items*, and only for head items.**

- **B1 (censor ≥0.7 floor displaces facts):** under CE, a censor in the head is scored `CE(query, censor.summary)` — the same function as everything else. The `censors.py:387,400` hard-floor never reaches the comparable score; it only ever affected the *pre-CE* raw cosine, which CE overwrites (`reranker.py:152`). So a censor only ranks high in the head **if the CE judges it relevant to the query.** B1 dissolves for the head. ✅
- **B2 (procedure boost >1.0):** identical logic. `CE(query, procedure.summary)` ignores the `procedures.py:446-457` multiplier entirely — CE overwrites `.score`. The boost cannot push a procedure above a more-relevant fact in the head. B2 dissolves for the head. ✅

**But — and this is the honest boundary — they only dissolve for the head.** A censor or boosted procedure sitting in the *tail* (rank 11+) keeps its floored/boosted score, ordered by the A spine. Two consequences:
- With the **A spine**, even the tail censor/procedure is rank-normalized, so B1/B2 are *also* neutralized there (A is scale-free) — good, but that's **A doing the work, not C.**
- This is yet another proof that the useful unit is **C+A**, not C.

**Does CE make Sub-decision 1 (exclude censors) moot?** Partly. Under C+A, censors compete on honest relevance whether included or excluded. But I still **endorse F080 Sub-decision 1's exclusion** (`retrieval_pipeline.py:340-341`, drop `"censor"` from `heart_types`) independently: censors have a *dedicated* surface ("Active Guidance", `context.py:460`), so spending CE budget (N is precious at 10!) scoring censor pairs that have their own display is waste. **Exclude censors from the ranked pool; let CE's N=10 budget go entirely to facts/episodes/procedures.** This also shrinks the latency footprint.

**Sub-decision 2 (procedure boost):** under C+A, fold utility into the A fusion weight (rank-level), and let CE re-judge head procedures on relevance. Do **not** keep the raw >1.0 multiplier — it's invisible to CE and incoherent to A. Clamp it out of the comparable score; preserve it only as telemetry.

---

## 5. Acceptance criteria — all 7, candidly

| # | Criterion | C standalone | C+A (endorsed) |
|---|-----------|--------------|-----------------|
| 1 | No mixed-scale sort | ✅ head only; ❌ tail stays mixed | ✅ A makes tail coherent; CE refines head |
| 2 | **CE-off correctness (prod config)** | **❌ FAILS — prod is CE-OFF, feature is inert, pool stays mixed-scale** | ✅ A spine produces coherent order with CE off |
| 3 | Flagged + byte-identical OFF snapshot | ✅ `NOUS_CROSS_ENCODER_ENABLED` already gates; A spine behind `NOUS_COHERENT_RANKING_ENABLED` default OFF → snapshot intact | ✅ same |
| 4 | Ordering hazards fixed (re-sort after mutation, IDs after truncation, deterministic `query_text`) | ⚠️ orthogonal to CE — must fix regardless | ⚠️ same — not C's job, ships alongside |
| 5 | Observability honest (`ce_reranked`/`mmr_applied`) | ✅ **C improves this** — thread real `ce_reordered` out of `heart._recall` (already computed, `heart.py:1031`) into `PipelineStats` (`retrieval_pipeline.py:296`) | ✅ same |
| 6 | Measured on F051 (+ BEAM if feasible), no per-source regression >3% | ⚠️ must run; CE historically null/slightly-negative at K=5 (`project_ce_rerank_disable_2026_05_26`) | ⚠️ A-spine measured; CE measured as ON-delta |
| 7 | **No new hot-path latency beyond documented bound** | **❌ adds ~1.6–3.6s prod (N=10–30) on top of existing recall cost** | ⚠️ A is O(n) free; CE adds budgeted ~1.6s at N=10 with fail-open |

**The two candid failures:**

- **#2:** standalone C **fails its own deployment config.** Prod runs CE-OFF; a feature that only works when CE is ON does nothing in the environment F080 must fix. *Only C+A passes #2.* This is dispositive — it's why C cannot be the spine.
- **#7:** C is **additive latency by definition.** Even budgeted N=10 adds ~1.6s prod p50 on top of a recall path that already does 4× embeds (whitepaper §7 B-rm-1: facts/episodes/procedures/censors each `embed(query)` separately) + vector + graph. The "documented bound" is real but it is *new cost*, where A is essentially free (O(n) rank fusion). A devil will weigh this heavily.

**On the F051 history — read what it actually measured, because the standard reading is wrong.** Yes, CE measured **null-to-slightly-negative at K=5** (`project_ce_rerank_disable_2026_05_26`, decision `8a3f35da` flipped `NOUS_CROSS_ENCODER_ENABLED=false`). But that A/B ran on a **chunks-on, LongMemEval-shape corpus dominated by facts / episodes / chunks** — i.e. it tested *whether CE improves same-type relevance ranking where RRF is already strong.* Null there is unsurprising and is **not** evidence against C. It tested the wrong job.

F080's target is **cross-type displacement**: a censor hard-floored at ≥0.7 (`censors.py:387`) or a procedure boosted >1.0 (`procedures.py:446`) out-ranking a more-relevant fact at the `[:limit]` cut. The F051 corpus almost certainly had **few or no censors/procedures competing in the ranked pool**, so the one thing CE uniquely fixes was never exercised. **The decisive eval for Approach C has never been run** — because the harness lacks censor/procedure-vs-fact displacement probes. So the honest framing is not "CE is proven useless" but "**acceptance #6 needs new displacement probes, and until they exist no approach (A, B, or C) has been measured on the actual defect.**" That is a demand on the eval, not a concession on C.

---

## 6. Honest failure modes

1. **The 67s ghost / N=30 default.** BGE on prod = 67s. MiniLM at the current `max_candidates=30` default ≈ 3.6s prod (measured-then-scaled, §1.2). If someone enables CE without also lowering N to ~10, they re-create an interactive stall. **Mitigation:** ship N=10 as the recall-path default and document the 10.8× prod factor next to the knob.

2. **`asyncio.to_thread` + `wait_for` does NOT cancel the CPU work.** This is the most dangerous footgun and it is exactly why ops disabled CE before. `asyncio.wait_for` cancels the *await*, but the thread-pool `Future` running `model.predict` **cannot be cancelled once started** — the CPU burn runs to completion. On a 1–2 vCPU prod VM under concurrent interactive load, *timed-out-but-still-burning* CE calls pile up and saturate the threadpool/CPU. The fail-open returns fast to each caller while the box melts. **Mitigation:** a hard concurrency gate — a global `asyncio.Semaphore(1)` (or small bounded `ThreadPoolExecutor`) so at most one CE predict runs at a time; if the semaphore is contended, skip CE entirely and use the A floor. This bounds CPU at one core of CE work, never N-callers-deep. **This must be in the design or the devil is right to reject.**

3. **GPU dependency is a non-starter for prod.** `reference_gpu_capability`: prod VM is **CPU-only, 8GB, no GPU**. The dev RTX 4060 Ti and the cu124 wheel swap help **eval/dev only** — they do nothing for prod recall latency. The installed wheel is `2.11.0+cpu` (confirmed this session). Any "just use the GPU" rebuttal is invalid for the prod hot path.

4. **Cold-start model load on a user turn.** `lru_cache(maxsize=1)` means first-recall-after-restart pays ~2.86s dev → ~31s prod for MiniLM load. **Mitigation:** boot-time warm-up coroutine (§1.4). Without it, every deploy/restart gives the first user a 30s stall.

5. **int8/ONNX is unmeasured on prod.** My N=30→1.5s path depends on a 2.4× quantization speedup I have *not* benchmarked on the prod VM. I will not launch on an unmeasured number; N=30 is a follow-up gated on a prod-VM measurement.

6. **The "ops turns it off again" risk.** This already happened once (MiniLM rolled back, `project_prod_bge_too_slow`; CE flipped off, decision `8a3f35da`). The realistic outcome of shipping standalone C is a third disable. The *only* way C survives contact with ops is: (a) it's an *enhancement on a coherent A spine* so the feature still delivers value with CE OFF, and (b) the semaphore + budget + warm-up make CE-ON safe enough that ops *can* leave it on. If C ships without A underneath, it ships infra that does nothing in prod (CE-OFF) and risks a stall the day someone flips it.

---

## 7. Self-score

| Dimension | Score /10 | Justification |
|-----------|-----------|---------------|
| **Correctness** | 8 | CE-as-uniform-scorer is genuinely the principled answer for *comparability*; B1/B2 dissolve for the head by construction (§4), and that's airtight. Docked because the relevance *win* is unproven on prod-shape data (F051 null at K=5) and the head-only coverage is a real limit. |
| **Prod-safety** | 4 | This is C's weak axis and I won't inflate it. Additive latency, uncancellable-thread footgun, cold-start, and a prior history of being disabled. The semaphore + N=10 + budget + warm-up raise it from "reject on sight" (2) to "conditionally acceptable as an ON-path option" (4). Standalone C on prod-safety alone: **2–3.** |
| **Change-footprint** | 7 | Small: F042 already exists. Net new = lower N default, `wait_for` wrapper, a semaphore, a warm-up coroutine, and threading `ce_reordered` into `PipelineStats`. **But** the mandatory A spine underneath (§3) is *not* small — that's the real F080 surgery. So C-the-CE-part is cheap; C-the-shippable-feature inherits A's footprint. |
| **Measurability** | 7 | F051 paired A/B + flag-OFF byte-identical snapshot give clean measurement, and fixing B5 finally makes CE state observable. Docked because the decisive prod number (N=30 int8 latency on the 8GB VM) and the relevance delta on prod-shape data are *both* still unmeasured. |

### Devil's-advocate prediction

**The devil will reject standalone C on prod-safety, and will be right to.** Concretely, they will cite: (a) acceptance #2 — the feature is inert in prod's CE-OFF config; (b) the uncancellable-`to_thread` CPU-pileup footgun under concurrent interactive load; (c) "you disabled this exact thing twice already" (`project_prod_bge_too_slow`, decision `8a3f35da`); and (d) the F051 null result — re-adding latency for an unproven gain. My rebuttal to (d) (§5): that null measured *same-type* ranking, not the *cross-type displacement* F080 targets, so the decisive eval is owed under acceptance #6 — but I concede the devil holds the burden-of-proof high ground until those probes exist.

**The devil will *accept* C only in the form I'm championing: as the ON-path relevance enhancer layered on an A (rank-fusion) spine** that independently satisfies acceptance #2 and #7. In that form, CE is safe-by-default (OFF = A spine, fully coherent), bounded (semaphore + 2.0s budget + N=10), warm-started, and observable — and it earns its place as the principled head-refiner without being load-bearing for correctness.

**My recommendation to the debate:** adopt **A as the spine** (resolves B1–B4, satisfies #2/#7, zero latency), with **C as an optional, flag-gated, budget-bounded head-reranker** for deployments that can afford ~1.6s and where a future prod-VM benchmark proves a relevance gain. Do **not** make C the spine. The most useful sentence I can offer this debate: **"Approach C, honestly costed, *is* Approach A with a cross-encoder bolted onto the head — so pick A first, and treat C as the upgrade path, not the foundation."**
