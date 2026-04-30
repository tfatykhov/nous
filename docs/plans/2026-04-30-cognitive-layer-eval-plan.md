# Heart + Brain + Context Management — Eval Plan, 2026-04-30

After 9 PRs shipped today (#383–#391), the storage and retrieval layers
are validated. This plan enumerates every remaining untested test/tune
item across **Brain**, **Heart**, and **Context Management** (the
cognitive layer where everything below gets assembled for the LLM).

## Coverage matrix (post-2026-04-30)

| Layer | Tested | Untested |
|---|---|---|
| **Brain** | F027 supersession, F058 calibration, F022 edge precision, F031 resolution | quality scoring, guardrails, spreading activation, dedup, bridge extractor, decision reviewer |
| **Heart** | admission, dedup, episode summary, procedures retrieval, graph backfill, fact→episode wiring | censors, censor actions, F012 procedure synthesis, episode compression, subtask quality, working memory persistence |
| **Context management** | — | **all** (frames, intent, context, dedup, monitor, working memory, compaction, anti-hallucination prompt) |

## Priority — execution today

Items 1-6 below are **today's scope.** They sit in **Context Management**
because that layer is load-bearing for everything beneath it: if frame
selection is wrong, every retrieval improvement we shipped earlier today
goes to waste. Brain and Heart gaps are catalogued in Tiers 2-3 for
future sessions.

---

## Tier 1 — Context Management (today's scope, items 1-6)

### 1. Frame selection accuracy
**Target:** `cognitive/frames.py::FrameEngine.select`
**Why critical:** Wrong frame → wrong tools available + wrong system prompt + wrong retrieval budgets. Frame is the entry point for every turn.
**Method:** 30 hand-curated user messages across 6 frame types (decision/task/conversation/question/debug/creative). Run `FrameEngine.select` against eval-DB-seeded frames. Sonnet judge: which frame is correct given the message? Compare to engine's choice.
**Output:** Per-frame accuracy, confusion matrix, mis-classification examples.
**Cost:** ~$1, 1 hr.

### 2. Intent classification accuracy
**Target:** `cognitive/intent.py::IntentClassifier.classify`
**Why critical:** Pattern-matching gates that route retrieval. Wrong route → search wrong source → bad answers.
**Method:** 30 hand-labeled messages with expected `IntentSignals` (is_question, is_greeting, temporal_recency, memory_type_hints, entity_mentions, topic_keywords). No LLM in SUT path. Score by exact-signal match.
**Output:** Per-signal precision/recall.
**Cost:** $0 (pure-pattern), ~1 hr.

### 3. Compaction fidelity
**Target:** `nous/api/compaction.py::ConversationCompactor.compact`
**Why critical:** When token budget exceeds threshold, compaction silently drops content. Load-bearing facts going missing is hard to debug.
**Method:** 20 synthetic long conversations with embedded "load-bearing facts." Run compaction. Sonnet judge: are the facts preserved or paraphrased intact?
**Output:** Per-fact preservation rate; failure samples.
**Cost:** ~$2, 2 hr.

### 4. Working memory selection quality
**Target:** Pre-turn working memory loading (`cognitive/layer.py::pre_turn`)
**Why critical:** Pre-turn loads recently-relevant items (>=0.7 score, max 10) into the LLM's context. Wrong selection → noise dominates the prompt.
**Method:** Build synthetic sessions with known relevant + irrelevant items. Observe what gets loaded. Sonnet judge: "given the user's current message, which items are actually relevant?" Compare to loaded set.
**Output:** Per-session precision (loaded ∩ relevant / loaded), recall (loaded ∩ relevant / all relevant).
**Cost:** ~$1, 2 hr (needs DB-backed fixtures).

### 5. Anti-hallucination prompt A/B
**Target:** `NOUS_ANTI_HALLUCINATION_PROMPT=true` flag effect
**Why critical:** Live in prod with no measurement. If it's not actually reducing hallucinations, it's just prompt overhead.
**Method:** 20 prompts known to elicit hallucination (specific dates, recent news, citations). Run with flag on/off. Sonnet judge: did the response hallucinate?
**Output:** Hallucination-rate delta on/off.
**Cost:** ~$2, 2 hr.

### 6. End-to-end context packing quality
**Target:** Full `pre_turn` pipeline output
**Why critical:** The umbrella eval. Frame + intent + retrieval + dedup + budget all converge here. If any step is broken, this measures the cumulative damage.
**Method:** For each (user_message, gold_answer) pair, run full `pre_turn` against the prod-snapshot agent. Inspect assembled context. Sonnet judge: was the gold-supporting memory present and not truncated?
**Output:** Per-type retention rate, truncation rate, frame correctness, end-to-end "answer is supportable" rate.
**Cost:** ~$3, 3-4 hr.

---

## Tier 2 — Brain untested (next session)

### 7. Decision quality scoring
**Target:** `nous/brain/quality.py::QualityScorer.compute`
**Why critical:** Used as a gate (`quality_block_threshold=0.5`) — low-quality decisions get silently rejected. If the scorer is miscalibrated, real decisions get blocked or noise gets through.
**Method:** 50 prod-snapshot decisions hand-labeled by Sonnet judge (high/med/low quality). Compare to `compute()` output. Sweep threshold to find optimum.

### 8. Guardrails (CEL evaluation correctness)
**Target:** `nous/brain/guardrails.py::GuardrailEngine.check`
**Why critical:** Block/allow gates run in production silently. False positives block legitimate work; false negatives let bad decisions through.
**Method:** Synthetic decision contexts with hand-curated expected outcomes. Verify CEL expressions evaluate correctly across edge cases (high-stakes + low-confidence, missing reasons, unusual categories).

### 9. Spreading activation isolation
**Target:** `nous/brain/spreading_activation.py`
**Why critical:** Currently measured indirectly via F051 retrieval. Isolated test would show whether multi-hop is helping or hurting.
**Method:** Use the F051 fixture; run retrieval with spreading on/off; measure delta. Reuse existing harness.

### 10. Decision dedup (`_is_noise`)
**Target:** `nous/brain/brain.py::Brain._is_noise_decision`
**Why critical:** Hard rejects "noise" decisions before they reach the DB. Could be over- or under-rejecting.
**Method:** 100 prod-snapshot decision attempts (success + abandoned). Run `_is_noise`. Sonnet judge: was this actually noise? Score precision/recall.

### 11. Bridge extractor
**Target:** `nous/brain/bridge.py`
**Why critical:** Generates `structure`/`function` for every decision. Used in retrieval and analysis. Never measured.
**Method:** 30 prod-snapshot decisions. Sonnet judge: are the extracted structure/function clauses faithful to the decision content?

### 12. Decision reviewer handler
**Target:** `nous/handlers/decision_reviewer.py`
**Why critical:** Auto-reviews decisions periodically; sets outcome (success/partial/failure). Drives calibration data — if reviews are wrong, calibration drifts.
**Method:** 50 reviewed decisions from prod snapshot. Sonnet judge re-evaluates outcome. Compare to handler's verdict.

---

## Tier 3 — Heart untested (next session)

### 13. Censors firing accuracy
**Target:** `nous/heart/censors.py`
**Why critical:** Block patterns at runtime. False positives = blocked legitimate work. False negatives = missed safety check.
**Method:** Synthetic + prod-snapshot scenarios. Hand-curate expected fire/no-fire. Run censor matcher. Score.

### 14. Censor actions (F031 read-only tools)
**Target:** `nous/heart/censor_actions.py`
**Why critical:** When a censor fires, it can run a read-only tool to gather context for an unblock decision. Validate the tool execution + unblock-decision flow.
**Method:** Synthetic censor configs with `trigger_action`. Verify tool fires, output is consumed by unblock-pattern check.

### 15. F012 procedure synthesis quality
**Target:** `nous/brain/procedure_learner.py`
**Why critical:** Sleep-cycle clusters decisions → synthesizes procedures. If bad procedures get created, every future task using them gets worse.
**Method:** Run procedure learner on prod-snapshot decision clusters. Sonnet judge: is the synthesized procedure coherent and useful?

### 16. Episode compression fidelity
**Target:** Episode `compression_tier` transitions ('raw' → 'summary' → 'archived')
**Why critical:** Old episodes get compressed; what's lost?
**Method:** 20 episodes with known transcript content. Trigger compression. Sonnet judge: is critical content preserved?

### 17. Subtask result quality
**Target:** Subtask worker output consumed by parent agent
**Why critical:** Background work output is consumed by parent — no quality eval.
**Method:** Run 20 representative subtasks via worker. Sonnet judge: did the subtask produce useful output for the parent?

### 18. Working memory persistence (cleanup correctness)
**Target:** F049 `WorkingMemoryManager.cleanup_stale`
**Why critical:** F049 shipped but only unit-tested. Verify behavior at scale.
**Method:** Seed eval DB with synthetic stale + fresh WM rows. Run cleanup. Verify only stale rows deleted.

---

## Tier 1 implementation order (today)

1. **Intent classification** — pure-pattern, no LLM, no DB. Easiest. Build first.
2. **Frame selection** — needs DB-backed frames. Easy with eval-DB.
3. **Compaction fidelity** — synthetic conversations, LLM judge. Medium.
4. **Working memory selection** — needs DB fixtures. Medium-hard.
5. **Anti-hallucination A/B** — needs full agent runs. Hard. May rate-limit.
6. **Context packing** — full pre-turn integration. Hardest. May require new fixtures.

Items that hit OAT rate limits get queued for re-run; scripts ship regardless.

---

## Operating model going forward

- Each eval ships as `scripts/eval/eval_<feature>.py` + a markdown report.
- Findings drive follow-up PRs (prompt rewrites, threshold tuning, persistence wire-ups).
- After deploys, re-run with prod data once persistence accumulates.
- Quarterly: re-baseline all evals against the latest snapshot to catch drift.
