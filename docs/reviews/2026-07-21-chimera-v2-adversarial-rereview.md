# Chimera v2 — Round-2 Adversarial Red-Team Review

**Reviewer:** claude-fable-5 (job efa75623)
**Date:** 2026-07-21
**Verdict:** **REVISE** (again) — the substrate swap is a genuine improvement on the math of P1 #1/#2, but the two documents contradict each other on the substrate itself, the "structural" citation-exactness claim overreaches what VSA can deliver, the build/eval plan still builds the *rejected* Hopfield substrate, and both round-1 P2 crash-safety findings are still present — one of them reasserted verbatim as "correct by design."

Prior round: job 22c61422 (REVISE, 4×P1 + 2×P2). This review assesses Chimera v2's response.

**Inputs reviewed.** *Document 1* — the Chimera v2 design note (the sections cited below are its §1, §2.1, §3.4 and the §8 comparison table). *Document 2* — the Chimera v2 build/eval plan (its §1.4 crash matrix and the eval-isolation gate). Both were supplied as attachments to review job efa75623, as their v1 predecessors were to job 22c61422. **Neither is archived anywhere this repository can reach**: a search of this repository's history, the sibling `tinyHippo` checkout and the author's Google Drive (2026-09-04) found no Chimera v2 design or build artifact, so the quotations below are verbatim from the job inputs but cannot be re-checked from the repo, and this review is the only surviving record of what v2 said. The nearest archived successor is [`chimera-v3-proposal.docx`](https://drive.google.com/file/d/1ECGPXaZqMgdGBZdOfrKxWnk3qiueWsPY/view) (author's Google Drive, 2026-07-25), which post-dates this review; whether it takes up the blocking fixes is not assessed here, and the fixes below should be applied against whichever Chimera document is current. Closing this provenance gap requires the author to attach the v2 inputs; it cannot be done from the review side.

---

## 1. Fact-check pass

Every load-bearing external citation was checked against a primary source. Verdicts:

### VERIFIED

| Claim | Verdict | Source |
|---|---|---|
| Gated DeltaNet exists as arXiv:2412.06464, "Improving Mamba2 with Delta Rule" | **TRUE** | [arXiv:2412.06464](https://arxiv.org/abs/2412.06464) |
| Delta-rule = error-correcting write: read current value at key, write only residual `v_t − S_{t-1}k_t` | **TRUE** — accurate description of the delta rule | [arXiv:2412.06464](https://arxiv.org/abs/2412.06464); [vLLM Qwen3-Next blog](https://vllm.ai/blog/2025-09-11-qwen3-next) ("delta rule for error-correcting memory updates") |
| Gating adds a data-dependent decay `α_t ∈ (0,1)` enabling selective forgetting | **TRUE** | [arXiv:2412.06464](https://arxiv.org/abs/2412.06464) abstract: "gating enables rapid memory erasure while the delta rule facilitates targeted updates" |
| Read is a direct matrix-vector product `S_t·q_t`, no iterative descent / β | **TRUE** (this is the defining property of the linear-attention family) | [vLLM Qwen3-Next blog](https://vllm.ai/blog/2025-09-11-qwen3-next) |
| Qwen3-Next deploys Gated DeltaNet in production | **TRUE, with a load-bearing caveat (see below)** | [vLLM blog](https://vllm.ai/blog/2025-09-11-qwen3-next); [Qwen3-Next-80B model card](https://huggingface.co/Qwen/Qwen3-Next-80B-A3B-Instruct) |
| Titans (Behrouz et al., Google): 170M–760M models beat GPT-4 on BABILong | **TRUE, literally** — holds in both few-shot and fine-tuned settings | [arXiv:2501.00663](https://arxiv.org/pdf/2501.00663); [NeurIPS 2025 proceedings](https://proceedings.neurips.cc/paper_files/paper/2025/file/a4ca07aa108036f80cbb5b82285fd4b1-Paper-Conference.pdf) |
| Titans: surprise = gradient-with-momentum; forgetting = learned adaptive gate | **TRUE** | [arXiv:2501.00663](https://arxiv.org/html/2501.00663) |
| xLSTM mLSTM = matrix memory + covariance update rule, competitive with SOTA | **TRUE** — and the covariance update is formally equivalent to Fast-Weight Programmers | [xLSTM, arXiv:2405.04517](https://arxiv.org/abs/2405.04517) |
| VSA/HRR: random high-dim vectors are near-orthogonal w.h.p.; binding via circular convolution, approximate unbind | **TRUE** | [Plate, HRR](https://press.uchicago.edu/ucp/books/book/distributed/H/bo3643252.html); [VSA survey, Springer](https://link.springer.com/article/10.1007/s10462-021-10110-3) |

### FALSE / IMPRECISE

- **"Yang, Wang, et al." (Doc 1 §2.1) — misattributed authorship.** The authors are **Songlin Yang, Jan Kautz, Ali Hatamizadeh**. There is no "Wang." This is a small error, but it is the *second consecutive round* in which a Chimera document has misattributed a primary citation (round 1: Benna-Fusi → Fusi/Drew/Abbott 2005). Pattern worth naming: citations in these docs are not being checked against the source.
- **"Qwen3-Next / Qwen3.5 (Alibaba, deployed)" (Doc 1 §2.1, §8 table) — ~~half-verified~~ CORRECTED 2026-09-03: fully verified.** Qwen3-Next with Gated DeltaNet is confirmed deployed. The original round-2 finding — that "**Qwen3.5**" as a *shipped* production system using DeltaNet was not confirmed by a primary source — **was already stale when written**, and is now plainly wrong. Qwen shipped the Qwen3.5 family from its own Hugging Face org in Feb 2026, five months before this review was dated: `Qwen/Qwen3.5-397B-A17B` (created 2026-02-16), plus `-122B-A10B`, `-35B-A3B`, `-27B`, `-9B`, `-4B`, `-2B`, `-0.8B` and FP8/GPTQ variants. Its own model card states the hidden layout as **`15 * (3 * (Gated DeltaNet -> MoE) -> 1 * (Gated Attention -> MoE))`**, and `config.json` carries `full_attention_interval: 4` with an explicit 60-entry `layer_types` array alternating `linear_attention` / `full_attention` ([Qwen3.5-397B-A17B model card](https://huggingface.co/Qwen/Qwen3.5-397B-A17B); [config.json](https://huggingface.co/Qwen/Qwen3.5-397B-A17B/blob/main/config.json)). **Do not drop "Qwen3.5" — cite the release, not the commentary.** Note the direction of the correction: the primary source *strengthens* the misframing point below, because the newer flagship kept the same 3:1 hybrid rather than going DeltaNet-only.

### VERIFIED-BUT-MISFRAMED (the important one)

- **Qwen3-Next does not validate "DeltaNet gives retrieval/citation exactness."** The production system is a **3:1 hybrid**: ~75% Gated DeltaNet layers *interleaved with a full softmax-attention layer every 4th layer* (structure confirmed in the model's own `config.json`: `full_attention_interval: 4` over 48 layers — [Qwen3-Next-80B-A3B-Instruct config](https://huggingface.co/Qwen/Qwen3-Next-80B-A3B-Instruct/blob/main/config.json)). **What the sources establish** is the interleaving itself and Qwen's own one-line gloss of the full-attention layers (below). **What they do not establish** is *why* the hybrid was chosen: no primary source or ablation says the full-attention layers were added specifically to supply retrieval fidelity that DeltaNet lacks, or that DeltaNet alone was judged untrustworthy for exact retrieval. **Citation corrected 2026-09-03:** an earlier draft of this paragraph attributed the quote *"full attention layers provide global context and strong retrieval capability, while linear layers provide efficient O(1)-per-token inference"* to the [vLLM blog](https://vllm.ai/blog/2025-09-11-qwen3-next). **That sentence does not appear in that post.** What the post actually says is: *"Gated DeltaNet (linear attention for long context efficiency)"* and *"Full Attention (full attention for high-fidelity reasoning)."* "High-fidelity reasoning" is all the source says; the retrieval-specific reading of it, and the stronger "strong retrieval capability" / "O(1)-per-token" phrasing, were **not** in the source and are withdrawn). What remains is an **inference, not a sourced fact**: a lab that shipped a 3:1 hybrid rather than a DeltaNet-only stack has, at minimum, not demonstrated that DeltaNet alone suffices for exact retrieval — and Qwen3.5 keeping the same 3:1 layout five months later is consistent with that reading, though it does not prove it. So the one production precedent the document leans on does **not validate** "DeltaNet gives retrieval/citation exactness": the deployed system pairs DeltaNet with softmax attention, and the reason is undocumented. Doc 1's central inference ("citation exactness is a design property of the key scheme") is therefore *unsupported* by the precedent it cites; whether it is actively undercut turns on a design rationale Qwen has not published.

- **Titans "beat GPT-4" is true but adversarial-benchmark-specific.** BABILong is engineered to defeat long-context transformers with distractor-dense haystacks; GPT-4's weak BABILong score is well known and does not imply general inferiority. The doc's usage ("headline result is specifically about retrieval at scale") is a *fair* framing — but a reader should not carry away "760M beats GPT-4" as a general capability claim.

### UNVERIFIABLE (internal claims — flagged, not accepted)

- "N≈5000, 82.2% discrimination retention validated" and "sparse population-vector encoding already validated for Hippocampus" — internal tinyHippo results; no external source. Not disputing, but they are load-bearing for the keyed-write dimensionality argument and are asserted, not shown.

---

## 2. Premise check

**Premise 1 — "The substrate swap structurally dissolves P1 #1 (composite vs. citation)."** *Partially true, overclaimed.* Switching the *write rule* from blind Hebbian sum (Hopfield) to error-correcting delta rule genuinely removes the superposition-catastrophe framing of round 1: a delta-rule write at key `k` *corrects toward* the new association instead of summing on top of the old one — the update subtracts the current readout at `k` before writing — so single-key readout is far cleaner than any β could make Hopfield. It is **not exact by construction**, and the review should not treat it as such: the correction is scaled by the learned write strength (β<1 leaves a residual of the old value), Gated DeltaNet additionally decays the prior state on every step, and later writes through non-orthogonal keys perturb the same readout. Exact replacement needs a normalized key, a full-strength update and no subsequent key interference — conditions the proposed random near-orthogonal keys *approximate* but do not guarantee. That half is real and is the strongest idea in v2, **as a large improvement rather than a guarantee**: the experiment plan should measure single-key readout error against the number and similarity of subsequent writes, not assume it is zero. **But the composite/multi-entity half relocates the exact same lossy-composition problem into the VSA bundling layer** (§3 below). "Structural dissolution" is *approximately* earned for single-entity keyed recall (subject to the residual above); it is *not* earned for composites, which is where P1 #1 actually lived ("compose an answer across facts" was the whole value proposition).

**Premise 2 — "No SNN-rule exemption is needed because DeltaNet is the same class as existing attention."** *True as stated, and a real improvement over v1's dodge.* DeltaNet has no spike-timing dynamics; it is linear-time matmul recurrence. This is a legitimately stronger position than v1's "not literally spiking so the rule doesn't apply." However, "same computational class as attention" answers the *latency/timing* concern, not the *inspectability* concern that P1 #2 was actually about (§4 below).

**Premise 3 — "Build risk is lower than v1."** *True.* `fla-org/flash-linear-attention` is a real, maintained reference implementation. This is a genuine and honest improvement over v1's hand-derived numpy Hopfield.

**Premise 4 (implicit) — "The two v2 documents describe the same architecture."** **FALSE, and this is a P1 finding.** Document 2 (storage/build/eval) was not updated for the swap. It still specifies a Hopfield Cortex end to end (§3 below).

---

## 3. Failure modes (prioritized)

### P1-A — The two documents contradict each other on the substrate. Document 2 builds the substrate Document 1 rejected.

Document 1 swaps Cortex from Hopfield/DAM to Gated DeltaNet and calls the Hopfield math "mathematically real, not a fixable engineering detail." Document 2 — the *build/eval/storage plan that engineering would actually execute* — still describes the Cortex as:

- "a continuous Dense Associative Memory / modern Hopfield network … Mathematically equivalent to one step of transformer self-attention `softmax(β·ξ·Xᵀ)·X`" (§0)
- `residual_energy … confidence score from the last convergence` and Hebbian-imprinted composites "formed by summing outer products" (§1.2) — i.e. the *blind Hebbian sum* Doc 1 says is the root defect
- `NOUS_CORTEX_BACKEND=chimera (adds the Hopfield re-rank leg …)` (§4.1)
- target metrics keyed to "the Hopfield update's sharp-mode regime (high β)" and "the composite/attractor-bridge mechanism" (§4.5)
- Phase 0 = "implement the Hopfield update as a pure-Python function" (§4.6)

**The build plan builds Hopfield.** If P1 #1 is unfixable for Hopfield (Doc 1's own claim), and the executable plan is Hopfield, then the plan executes the rejected design. Either (a) Doc 2 is stale and the swap exists only in the prose of Doc 1, or (b) the swap is not actually load-bearing for the build. Both readings are disqualifying for a PROCEED. The `cortex_composites` schema (Hebbian `imprint_strength`, `residual_energy`) is Hopfield-shaped; a DeltaNet Cortex has a *fixed-size matrix state*, not a growing table of imprinted composite vectors — the storage design does not even represent the v2 substrate. **This must be reconciled before any build sign-off.**

### P1-B — "Structural citation-exactness via VSA" overclaims what VSA delivers; the lossy-composition problem is relocated, not dissolved.

Doc 1 §3.4: composites are "assembled from citable parts" by binding role-filler tuples and "read out each bound component separately." VSA's own literature says this readout degrades with the number of bound components:

- Unbinding recovers *filler + noise*, and the noise **accumulates with the number of bindings in the bundle**: for unit-norm HRR vectors with the usual approximate inverse (circular correlation), unbinding one pair out of a bundle of *m* recovers the filler plus noise from **two** sources — the pair's *own* role vector, whose approximate inverse leaves a reconstruction residual even at *m* = 1 (it vanishes only with unitary role vectors or exact inverses, which neither document specifies), and one crosstalk term per *other* pair. Every term is of order-one norm, so **total reconstruction noise** on the recovered vector grows as ≈ √m against a unit-norm signal, the **scalar similarity** used to identify or clean up the filler sees variance ≈ m/d, and the identification SNR falls as ≈ d/m; the crosstalk share due to the other bindings alone is ≈ √(m − 1) of that norm. A per-coordinate figure of ≈ 1/√d therefore describes one such term, not a bundle, and a capacity analysis that counts only the other *m* − 1 pairs is systematically optimistic. Primary source for the decoding-noise and capacity analysis, including the unitary-vector (exact-inverse) case: Plate, T. A. (1995), "Holographic Reduced Representations", *IEEE Transactions on Neural Networks* 6(3): 623–641, [doi:10.1109/72.377968](https://doi.org/10.1109/72.377968); secondary: [VSA survey](https://link.springer.com/article/10.1007/s10462-021-10110-3), [HRR analyses](https://www.emergentmind.com/topics/holographic-reduced-representations-hrrs). Clean unbinding of a superposed bundle is limited to a small number of pairs at practical dimensions **unless a cleanup memory is used**.

So the v2 "composite = citable parts" claim holds only for small composites and only *with a cleanup step*. The document supplies exactly one cleanup memory: the Symbolic Ledger (Postgres), which "every Cortex composite must decode back to." **That is the tell.** The exactness in the citation path is coming from the Postgres lookup, not from the DeltaNet/VSA state preserving the parts faithfully. Which raises the question round 1 asked in a different form: *if Postgres is doing the exact recall, what is the DeltaNet+VSA layer buying you over the RRF+cross-encoder retrieval Nous already runs?* Doc 1 §1 concedes the sharp-regime answer ("not meaningfully different from what Nous's retrieval already does today") — and the VSA composite path, once you require ledger cleanup on every readout, collapses toward the same concession. This is P1 #1 wearing new vocabulary, not P1 #1 dissolved.

### P1-C — DeltaNet's fixed-size state is a capacity ceiling for an unbounded, lifelong fact store (new failure mode the swap introduces).

Gated DeltaNet's headline virtue in Qwen3-Next is a **fixed-size recurrent state** (constant per-token decode, no growing KV cache). For a bounded context window that is a feature. For Chimera's use case — a durable associative store over an ever-growing corpus of facts/decisions/entities — **a fixed d×d state is a hard capacity ceiling.** A d-dimensional matrix state holds ~O(d) faithfully-recoverable key-value associations; past that, the delta rule and the decay gate *evict* older associations (that is what they are for). The document conflates two different facts:

1. "Random high-dim vectors are near-orthogonal w.h.p." (true — there are astronomically many near-orthogonal *directions*), with
2. "A d-dim recurrent state can store arbitrarily many key-value pairs faithfully" (**false** — bounded by d).

Nous already holds facts in the many-thousands and grows monotonically. Nothing in either document addresses how many entity-keys fit in the Cortex state before eviction begins, what gets evicted, or how eviction interacts with the "citation-exactness by construction" promise. Hopfield's growing pattern matrix X did *not* have this ceiling (it grows with the corpus, at the cost of the compute the doc worried about). **The swap trades one problem (Hopfield's averaging) for a different one (DeltaNet's bounded capacity) and does not acknowledge the trade.**

### P1-D — The production-precedent argument does not transfer to the proposed use.

The battle-tested artifact is a **trained, end-to-end Gated DeltaNet sequence layer** whose gating/decay parameters are learned. The proposal (Doc 1 §5) is to "pull the DeltaNet layer from flash-linear-attention" and drive it with **hand-assigned fixed keys** as an untrained associative KV store. These are different objects. An untrained DeltaNet layer with externally-imposed keys has none of the learned gating behavior that made Qwen3-Next work, and the paper's benchmarks (language modeling, in-context retrieval) do not measure the fixed-key-store regime at all. "Qwen3-Next deployed it, so build risk is low" is only true for the trained-sequence-layer use — which is not the proposed use. This should be stated honestly: the reference impl gives you working code, not a validated mechanism for the thing Chimera wants.

### P2-A — Round-1 fsync/atomicity finding is only *partially* fixed; the specific gap remains.

Round 1 flagged missing **fsync barriers** on HDF5 checkpoint writes. Doc 2 §2.2 now writes to `checkpoint.tmp.h5` and atomically renames — which addresses *torn/partially-readable files*, but **not the finding**. Atomic rename without `fsync(file)` before the rename **and** `fsync(dir)` after it can, on power loss, leave a rename pointing at a file whose data blocks were never flushed (a zero-length or truncated checkpoint on ext4/xfs with delayed allocation), or lose the rename itself. The doc's claim "either fully present or not present at all" is **not guaranteed by rename alone** — it requires the fsync barriers round 1 named, which are still absent. Partial fix; reopen.

### P2-B — Round-1 idempotency finding is unaddressed and *reasserted as correct*.

Round 1 flagged **non-idempotent Hebbian imprint accumulation under retry**. Doc 2 §1.4 crash matrix now states: *"Next Hebbian imprint re-attempts; **idempotent by design (imprint accumulates, doesn't overwrite)**."* This is exactly backwards: an operation that **accumulates** is the definition of **non-idempotent** — retrying after an ambiguous failure double-counts the imprint. The document has taken the precise mechanism round 1 identified as the bug and relabeled it as the safety property. This is not a silent drop; it is a reassertion of the flawed reasoning, which is worse. (Fix: idempotency key / dedup on `(source_ids, imprint_epoch)`, or model imprint as an upsert to a target strength rather than an unconditional `+=`.) Note this also inherits P1-A's incoherence — under a DeltaNet substrate there is no "Hebbian imprint" at all, so this whole row is v1 residue.

### P2-C — Benna-Fusi misattribution: silently dropped, not acknowledged.

The round-1 Benna-Fusi/Fusi-Drew-Abbott misattribution does not appear in v2 (the hand-derived cascade decay it supported is now framed as inferior to Titans' learned forget gate, Doc 1 §2.2 — a reasonable move). But it was dropped silently rather than corrected-on-the-record, and a *new* misattribution appeared ("Yang, Wang") in its place. The corrective habit did not take.

### P3-A — The 3-arm eval (Doc 1 §6) is better but still bundles three mechanisms in Arm C.

Arm A (baseline) / Arm B (+entity layer) / Arm C (+DeltaNet Cortex) correctly isolates the entity-recurrence layer — the reviewer's core round-1 suspicion — which is real progress. **But Arm C bundles three separable things:** (1) the DeltaNet substrate, (2) the fixed-orthogonal-key assignment scheme, and (3) VSA role-filler binding for composites. A win in C over B cannot attribute among them, and given P1-B (VSA is the weak link), you specifically want to know whether any gain survives *without* the VSA composite path. Add an Arm C′ = baseline + entity + DeltaNet single-key recall **only** (no VSA composites), so the composite mechanism is isolated from the substrate.

### P3-B — The two documents also disagree on the eval design itself.

Doc 1 §6 proposes 3 isolated arms. Doc 2 §4.1 proposes a single `NOUS_CORTEX_BACKEND` flag flip (`legacy` vs `chimera`) that bundles "Hopfield re-rank leg + composite store + entity layer + Hippocampus" as "one variable changed." That is four mechanisms, not one, and it contradicts Doc 1's isolation discipline. Whichever is intended, the documents must agree.

---

## 4. Is P1 #2 (hot-path opacity) resolved on engineering merit? — partially, and the residual gap is now unguarded.

DeltaNet removes the *timing/latency* form of the hazard cleanly: no spikes, no STDP, standard matmul, same class as the attention already on the hot path. That part is genuinely closed.

The *inspectability* form is **not** closed by "it's linear-time matmul." DeltaNet's state `S_t` is a **continually-updated superposition matrix**; nothing in either document makes each write per-write readable or auditable, and the composite/VSA layer reintroduces non-inspectable blends on top of it. The proposal *does* offer an inspection path — every composite decodes to `source_ids` and to the Symbolic Ledger — but that path's fidelity is exactly the VSA-crosstalk question of P1-B. So the honest status is: **"inspectable *if* the ledger decode is faithful," and the decode is the unproven part.** The circularity of v1 (compliance rested on an unsolved decode) is *reduced* — delta-rule single-key recall is genuinely clean — but for composites it is *relocated*, not eliminated. The document should carry an explicit, testable inspectability metric ("given a Cortex-influenced answer, can we enumerate and filter the exact source associations that produced it, at composite scale, with measured fidelity?"). It has none.

---

## 5. Steelman — what is genuinely good in v2

- **The delta rule is the right instinct.** Error-correcting writes genuinely dominate blind Hebbian superposition for single-key faithful recall. This is a real, well-grounded improvement over v1's Hopfield, and the mechanism is described accurately.
- **Arguing SNN-compliance on merits, not on the deactivated rule.** v2 does *not* invoke the procedure deactivation anywhere; it argues "DeltaNet simply is not SNN, like a KV-cache is not SNN." That is the correct way to make the argument and is a real upgrade over v1's definitional dodge (see §7).
- **Lower build risk, honestly stated.** A maintained reference impl (`flash-linear-attention`) beats hand-derived numpy, and the doc says so plainly.
- **The eval-isolation discipline was retained, not discarded.** v2 explicitly refuses to let the math fixes excuse it from the confound objection, and keeps the "C must beat B or no GPU spend" gate. That is exactly the round-1 recommendation being honored.
- **Titans correctly parked as v3.** Separating "what stores facts" from "what decides what's worth storing" is a clean factoring, and Titans' learned forget gate is correctly flagged as superior to the v1 hand-derived decay.

---

## 6. Alternatives considered

- **Drop the VSA composite layer; keep DeltaNet single-key recall + Postgres composition.** If the ledger is doing the exact citation anyway (P1-B), compose answers in fact-space directly from ledger rows and use DeltaNet only for single-entity associative recall, where its delta rule is *much* cleaner than a Hebbian sum — but still measure the readout error (Premise 1) before building on it. This removes the one mechanism (VSA bundling) whose own literature contradicts the exactness claim. Strictly simpler; likely captures most of the benefit.
- **Modern Hopfield with cleanup was dismissed too fast.** Ramsauer et al. 2020 modern Hopfield has *exponential* storage capacity and near-exact retrieval at high β *with* a cleanup step — the doc's "sharp = no gain / averaging = uncitable" dichotomy is the v1 framing restated, and a keyed modern-Hopfield-with-cleanup is arguably in the same place DeltaNet+VSA+ledger lands, without the fixed-capacity ceiling (P1-C). Not necessarily better — but the head-to-head was asserted, not shown.
- **Phase 0 only (round-1 recommendation) is still the cheapest path to the decision that matters.** The one thing that gates everything is "does the entity-recurrence layer (Arm B) beat baseline (Arm A)." That is a pure-Postgres experiment requiring *neither* DeltaNet nor VSA nor a GPU. Running B-vs-A first, before committing to any substrate, would de-risk the entire program for near-zero cost and would resolve the reviewer's core suspicion before a single line of DeltaNet is written.

---

## 7. The procedure-deactivation question (a)/(b)/(c)

**(a) Does the underlying technical hazard still exist, independent of the rule's status?** **Yes — reduced but real.** The hazard round 1 named is "non-inspectable, hard-to-filter learned associations reaching a live decision path." DeltaNet's continually-updated superposition state `S_t` is exactly a learned, non-per-write-inspectable association store sitting on the hot path; the VSA composite layer adds non-inspectable blends on top. The delta rule *helps* (it *corrects toward* the latest association at a key instead of summing on top of it, so single-key recall is *approximately* attributable to the latest ledger association — approximately, because a β<1 write leaves a residual of the old value, gating decays the state, and later non-orthogonal writes interfere, which is why Premise 1 requires single-key readout error to be measured rather than assumed zero); the composite path *hurts* (VSA crosstalk makes multi-entity readout lossy — §3 P1-B). Net: the hazard is smaller than with spiking STDP, but it has not gone away, and the rule being deactivated changes none of that. Math and systems risk do not care whether a procedure is enforced.

**(b) Is the author using "the rule isn't binding anymore" as a permission structure to relax scrutiny?** **Not overtly — but there is a subtler, real version of the concern.** To the document's credit, v2 never invokes the deactivation; it argues compliance on engineering merits ("DeltaNet is not SNN, no exemption needed"), which is the honest and stronger form of the argument. So I do not see conscious rule-lawyering. **However:** the deactivation removed the *external forcing function* that would have made someone demand the missing inspectability evidence. And that evidence is in fact missing — there is no test, metric, or gate anywhere in either document for "can we enumerate and filter the source associations behind a Cortex-influenced answer at composite scale?" Under the old rule, that gap would have blocked sign-off. With the rule gone, nothing catches it, and the document sails past the gap without noticing it is a gap. So the risk is not that the author *invoked* the deactivation to dodge scrutiny — it is that the deactivation *silently relieved the pressure* that would have surfaced the still-open problem, and the still-open problem is real (§4). That is worth naming precisely because the document didn't need to argue it away — it just quietly didn't have to.

**(c) Citation-faithfulness and containment held to engineering merit only:** On that standard, independent of any rule: the "structural citation-exactness" claim is **overstated** for composites (VSA's own literature, §1/§3), the executable build plan **still builds the rejected Hopfield substrate** (§3 P1-A), the fixed-capacity ceiling is **unaddressed** (§3 P1-C), and the inspectability of hot-path associations is **asserted, not measured** (§4). Rule status was not used to reach any of these conclusions, and would not change any of them.

---

## 8. Recommendation: **REVISE**

v2 is a real intellectual step up from v1 — the delta rule is the right mechanism for the single-key half of the problem, the SNN argument is now honest, and the eval discipline survived. But it cannot PROCEED as written:

**Blocking (must fix before any build sign-off):**
1. **Reconcile the two documents.** Doc 2's storage/build/eval plan still builds a Hopfield Cortex (schema, β regimes, Hebbian imprint, Phase-0 code). Rewrite it for the DeltaNet substrate, including a storage model for a fixed-size matrix state (which `cortex_composites` does not represent). (P1-A)
2. **Retract or bound the "structural citation-exactness" claim.** State explicitly that composite citation fidelity depends on the Postgres cleanup step, quantify the VSA readout degradation at your intended composite sizes, and answer what the DeltaNet+VSA layer adds over existing RRF+cross-encoder retrieval once the ledger is doing the exact recall. (P1-B)
3. **Address DeltaNet's fixed-capacity ceiling** for an unbounded fact corpus — how many keys before eviction, what gets evicted, and how that interacts with "exactness by construction." (P1-C)
4. **Fix both round-1 P2s for real:** add fsync(file)+fsync(dir) barriers around the checkpoint rename (P2-A), and replace the non-idempotent `+=` imprint with an idempotency-keyed upsert — and correct the crash matrix, which currently calls accumulation "idempotent by design" (P2-B).

**Strongly recommended:**
5. Run Arm A vs Arm B (entity layer, pure Postgres, no substrate, no GPU) **first**, as the cheap gate on the whole program, and add an Arm C′ isolating DeltaNet single-key recall from the VSA composite path. Reconcile the §6/§4.1 eval-design contradiction. (P3-A/B)
6. Add an explicit, measured inspectability gate for hot-path associations (§4) — precisely because the rule that used to require it is no longer there to.
7. Fix the "Yang, Wang" authorship citation; institute a check-citations-against-source habit (two rounds, two misattributions). **Superseded in part, 2026-09-03:** "drop unconfirmed Qwen3.5" was itself an unchecked citation — Qwen3.5 shipped Feb 2026 with a documented Gated DeltaNet layout. Keep it and cite the model card. This review made the exact error it was naming, which is the strongest available argument for the habit it recommends.

The through-line across both rounds: the *ideas* keep getting better, but the *artifacts* keep shipping internal contradictions and unfixed crash-safety gaps that a careful read of the author's own prior review would have caught. The substrate is more defensible now. The document is not yet trustworthy as a build spec.

---

### Sources

- [Gated Delta Networks (arXiv:2412.06464)](https://arxiv.org/abs/2412.06464)
- [vLLM — Qwen3-Next hybrid architecture](https://vllm.ai/blog/2025-09-11-qwen3-next)
- [Qwen3-Next-80B-A3B model card](https://huggingface.co/Qwen/Qwen3-Next-80B-A3B-Instruct)
- [Titans: Learning to Memorize at Test Time (arXiv:2501.00663)](https://arxiv.org/pdf/2501.00663)
- [Titans — NeurIPS 2025 proceedings](https://proceedings.neurips.cc/paper_files/paper/2025/file/a4ca07aa108036f80cbb5b82285fd4b1-Paper-Conference.pdf)
- [xLSTM: Extended Long Short-Term Memory (arXiv:2405.04517)](https://arxiv.org/abs/2405.04517)
- [Plate — Holographic Reduced Representation (U. Chicago Press)](https://press.uchicago.edu/ucp/books/book/distributed/H/bo3643252.html)
- [A comparison of vector symbolic architectures (Springer, AI Review)](https://link.springer.com/article/10.1007/s10462-021-10110-3)
- [Holographic Reduced Representations — analysis](https://www.emergentmind.com/topics/holographic-reduced-representations-hrrs)
