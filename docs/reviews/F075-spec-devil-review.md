# F075 — Devil's-Advocate Spec Review

**Reviewer role:** Adversarial. Find weaknesses, not blessings.
**Date:** 2026-05-27
**Subject:** `docs/features/F075-temporal-fact-extraction.md` (Draft v1)
**Verdict header:** Spec is **directionally plausible but operationally under-derisked**. The smoking-gun n=1 validation has done less work than the spec implies, and several mechanism claims do not survive code-reading.

---

## Finding 1 — Smoking-gun validation is n=1 (Critical)

**Claim under review:** §Problem says 3 of 5 failures are retrieval-miss, fixable by extractor.
**The mechanism behind that 3-of-5 number:** only conv 2 Q0 was traced end-to-end (SQL + retrieval pipeline + LLM rerun). Conv 4 Q1 and conv 5 Q1 are presumed to be the same shape "no info about when X happened" purely from the answer text. The spec then projects 3-of-5 cleanly into "≥0.60 temporal_reasoning."
**Sibling precedent:** the `BEAM_TERSE_LIST` diagnostic (memory `f074-terse-list-shim`) shows the same investigator's surface-shape intuition was wrong — what looked like a vocabulary gap was a topical-choice gap, and the shim moved 0.00 on the actual rubric. The exact same epistemic risk applies here: surface-shape "no info about when" failures may have different mechanisms (LLM hedge, abstention, contradicting context retrieved alongside the date, ambiguous rubric date).
**What we'd need to verify or change:** Before paying ~$12 to re-run BEAM, run the cheap diagnostic (~$0.20) for conv 4 Q1 and conv 5 Q1 too — read source chat, check if the date(s) the rubric expects appear at all, count how many times, and confirm the rate-limit-style "buried in unrelated context" structure repeats. If only 1 of the 3 retrieval-miss failures repeats the pattern, the expected lift drops from +0.18 to +0.06 and the entire feature shape is over-engineered.

## Finding 2 — `_apply_graph_adjacency_boost` is a footgun for `happened_before` (High)

**Claim under review:** §Layer 2 says "the existing boost summing edges across candidates will naturally include `happened_before` once we write them. **No new consumer code required.**"
**What the code actually does** (`nous/api/retrieval_pipeline.py:709-716`): the SQL filter is `WHERE agent_id = :a AND relation != 'contradicts' AND source_id = ANY(...) AND target_id = ANY(...)`. So yes, `happened_before` will be summed indiscriminately into the adjacency degree alongside `related_to`, `informed_by`, `summarized_by`, etc.
**The hidden risk:** the boost rewards "this candidate is connected to OTHER candidates in the same recall batch." For a date-arithmetic query like *"how many days between API key acquisition and wireframe completion"* both endpoint facts will be in the batch, and they will reinforce each other — that part is fine. **But** any unrelated date-anchored fact in the same episode will also be in the `happened_before` chain through that batch, and so it will inherit boost too, surfacing topical-neighbors-that-happen-to-be-time-adjacent. The boost is **multiplicative on existing relevance score**, so the worst case is a moderately-relevant time-neighbor moves above the correct fact.
**Worse:** there is no current consumer that treats `happened_before` differently from `related_to`. Layer 2 ships a new edge type with semantically-distinct meaning into a sum that treats all relations as equivalent — that *is* the F065 anti-pattern in reverse (edges with semantically-wrong consumer, instead of edges with no consumer).
**What we'd need to verify or change:** Either (a) gate `_apply_graph_adjacency_boost` to specific relations via a settings allowlist before writing `happened_before` edges, or (b) drop Layer 2 entirely from v1 and rely purely on the `event_date` SQL column (Layer 3) — the spec already admits at line 489 that "synthetic validation showed the fact embedding alone (no boost) ranked #3." If the synthetic fact wins on cosine alone, Layer 2 is unjustified for the validated use case and the marginal risk is unbounded.

## Finding 3 — Extractor sees the SUMMARY, not the transcript (Critical)

**Claim under review:** §Layer 1 augments `FactExtractor` to produce date-anchored facts.
**What the code actually does** (`nous/handlers/fact_extractor.py:297-308`): `_extract_facts` consumes only `summary["summary"]` and `summary["key_points"]`. Both are produced by `EpisodeSummarizer` (`_SUMMARY_PROMPT`, ~150-word prose summary) which itself summarizes the transcript and does NOT have any instruction to preserve dates faithfully. The "March 10" the diagnostic found in `episode_chunks` is in the **raw transcript**, not the summary.
**The risk:** Even with a perfect F075 prompt addition, the extractor only sees what the summarizer kept. If the summarizer dropped "March 10" because it lived in a rate-limit code block (and the summarizer rightly compresses code-heavy content), the extractor cannot extract what it cannot see. The "0 rows contain March 10" observation in the diagnostic might be a summarizer-drop rather than an extractor-miss — and F075 fixes the latter, not the former.
**Spec also references `EPISODE_START_TIMESTAMP`** in the prompt template (line 109 "Resolve relative dates ... against the EPISODE_START_TIMESTAMP provided in the metadata block") — but the existing `_extract_facts` call site does not pass episode start. That's at minimum new plumbing not accounted for in the 80-LOC Layer 1 estimate; at maximum it's a contradiction with where the data actually lives.
**What we'd need to verify or change:** Before writing any code, instrument **one** failing conv: dump the `summary["summary"]` text for the episode containing "March 10" and grep for the date. If absent, F075 must also modify `EpisodeSummarizer`'s prompt — bringing this to 2 prompt mutations, doubling the regression-risk surface for non-event content (Finding 4). If present, then F075's Layer 1 lever is empirically valid.

## Finding 4 — Two-prompt collateral risk understated (High)

**Claim under review:** §Risks lists "Prompt-engineering risk" with mitigation "integration test with known-date corpus; track precision/recall in eval."
**Sibling precedent:** Memory `lme-qa-winning-recipe` records V10's timestamp-prompt finding — a small prompt addition produced category-specific regressions, with the win coming from one category and losses in others. Same shape applies here: a date-focused instruction to the FactExtractor (or EpisodeSummarizer if Finding 3 forces it) will plausibly compete for output tokens against the existing instructions ("preferences, person, rule, technical, concept, tool"). On non-event content, the extractor may produce fewer category-correct facts because attention is now split.
**Spec mitigation is insufficient:** "existing test suite" only checks output schema; "LME hand-label qrels" measures retrieval, not extractor output quality. Neither catches a 5% regression in `knowledge_update` or `information_extraction` BEAM abilities.
**What we'd need to verify or change:** Add an acceptance criterion: re-run BEAM ability scores for `knowledge_update` and `information_extraction` and require ≤0.05 regression. (The spec already commits to "Other ability scores stay within ±0.05" in §Acceptance #4, but only against prod-v3, and at n=5 the noise floor is ±0.025 — see Finding 6.) Make this a numeric gate, not an inspection step. Better: add a holdout fact-extraction unit test where the input has no date content and assert the output is byte-identical pre/post-prompt.

## Finding 5 — Dedup is silent on same-event-different-date (Medium)

**Claim under review:** §Layer 1 says "Date-anchored events should still go through the dedup check. If the same event was already captured (same entity+action+date), do not duplicate."
**The unaddressed case:** if the same event is extracted twice on different sessions with slightly different parsed dates (LLM relative-date resolution drift: "yesterday" resolved against episode_start vs against session_start), what happens? Today's dedup is hybrid-search RRF at 0.92 + native cosine at 0.95 on **content** — both score on the text, not on `event_date`. So `"Christina obtained the OpenWeather key on 2024-03-10"` and `"Christina got her OpenWeather key on 2024-03-11"` would both pass dedup and both live in facts with different `event_date`. Retrieval surfaces both. Date-arithmetic LLM picks one. Spec promises this is "category B (wrong-date), not addressable" — but F075 will CREATE category-B failures it does not currently have.
**What we'd need to verify or change:** Either (a) make `event_date` part of dedup key (composite on subject + event_date) — this is a real schema change and unbudgeted; (b) prefer the older/more-confident date in a tie via post-extract reconciliation; (c) accept the risk explicitly and add a regression check that the `wrong-date` failure count does not rise. Without any of these, F075 may move 3 failures from class-A to class-A-fixed but introduce 1-2 new class-B failures, netting a smaller lift than projected.

## Finding 6 — n=5 noise floor against a 0.18 expected lift (High)

**Claim under review:** §Acceptance #4 says re-run BEAM Phase 1 at n=5 conv with threshold ≥0.55 (ideally ≥0.60), current 0.417.
**The math:** BEAM Phase 1 noise at n=5 conv is ±0.025 from `f074-beam-results` (your own characterization in the task brief). Six temporal_reasoning Qs per conv × 5 conv = 30 Q. A 0.18 expected lift moves the metric ~7× the noise floor, so the **direction** would be detectable. But the **magnitude** required for the gate (≥0.55 = +0.13) is only ~5× noise, and the "ideal" target (≥0.60 = +0.18) only ~7×. If the real lift is 0.06 (Finding 1 worst case), n=5 cannot distinguish that from noise.
**What we'd need to verify or change:** Specify the gate more rigorously: (a) require both `temporal_reasoning ≥ 0.55` AND no individual conv-level temporal score regresses by >0.10 (catches lift-on-average-with-one-conv-getting-worse); (b) require non-overlapping confidence interval against prod-v3 baseline; (c) if the n=5 result falls in 0.45-0.55 (i.e., ambiguous), commit to an n=20 re-run BEFORE flipping any defaults. Currently the spec allows a "lucky 5-conv" pass to lock in the change.

## Finding 7 — Synthetic validation phrasing is the best possible case (High)

**Claim under review:** §Smoking-gun finding — synthetic fact `"Christina obtained the OpenWeather API key on March 10, 2024."` ranked #3 with score 0.827.
**What was actually validated:** the embedding cosine between *that exact sentence* and the date-arithmetic query. The sentence is a clean SVO-on-date construction with the entity name, the action verb, and the ISO-resolved date in the order a curator would write. **Real extractor output won't be this clean.** Possible LLM outputs:
- `"User got their OpenWeather API key around the 10th of March."` (lower cosine — pronoun drift + relative phrasing)
- `"OpenWeather API key acquisition: March 10, 2024."` (header-style, low cosine to natural-language question)
- `"On March 10, 2024, Christina obtained the API key for OpenWeather and configured rate limits."` (mixed-topic, embedding dragged toward "rate limits" — same root failure that caused the chunk to rank low)
**The synthetic ranked #3.** If real extraction produces a phrasing 0.05 less aligned, that drops to rank 8-12 — possibly outside top-K, restoring the original failure.
**What we'd need to verify or change:** Before committing to Layer 1's design, run an extractor draft prompt on the 3 retrieval-miss episodes, dump the actual produced fact text, embed it, rank it against the live K=20 result set. If the produced fact ranks worse than #5 in any of the 3 cases, the prompt needs more constraint on output phrasing OR Layer 3 (boost) is mandatory, not optional.

## Finding 8 — Scope creep concealed in "440 LOC" (Medium)

**Claim under review:** Cost section says "~440 LOC + 30 tests. ~2-3 days for one engineer."
**Hidden scope items not in that count:**
- **EpisodeSummarizer prompt change** (Finding 3) — at least 1 prompt mutation + 5 tests + a snapshot-update for the existing summarizer tests.
- **Episode start timestamp threading** — `_extract_facts` and the prompt template need access to `episode.started_at`. Not currently in `_EXTRACT_PROMPT.format()` args. Touches handler + summarizer + tests.
- **Settings update in `CLAUDE.md` env-var table** — spec mentions 5 new rows but the table is already 250+ rows. New rows need a careful insert location for the row-ordering convention. ~30 min, but real time.
- **Sleep cycle re-run after backfill** — spec promises §Layer 4 step "triggers `GraphDensifier.run_backfill_cycle` at end" — but `run_backfill_cycle` operates per-cycle, not per-episode. The 5K-fact backfill will produce up to ~N-per-episode chains for ~hundreds of episodes; one `run_backfill_cycle` call may not cover all episodes (depends on `NOUS_GRAPH_BACKFILL_MAX_FACTS=50` per cycle). Either the backfill script must run multiple densifier passes OR document that edges will fill in over multiple sleeps.
- **Eval re-run is two passes:** LME N=20 pre-check ($5) AND BEAM ($7), per spec line 444. Plus the n=20 re-run if Finding 6 hits.
**Realistic estimate:** 600-750 LOC, 35-45 tests, 4-5 days. The 2-3 day estimate is for the happy-path subset only.
**What we'd need to verify or change:** Re-plan with the summarizer prompt change explicit, the episode-start-timestamp wiring explicit, and a written guarantee that the densifier covers all backfilled facts in finite time (or accept the multi-sleep convergence model).

## Finding 9 — Hidden assumption in the diagnostic chain (Medium)

**Claim under review:** §Problem's reasoning chain is unusually clean — five intuitive levers falsified neatly, leaving one survivor: "the gap is at ingest."
**The hidden assumption:** that the right fix can be reasoned about per-failure-class independently. The diagnostic separates "class A retrieval-miss" from "class B wrong-date" as if F075 only touches A. **But F075 changes extractor output for ALL episodes, including the ones whose class-A and class-B failures share the same underlying property** — that the source has weakly-grounded date references. F075 will produce more dates in the candidate pool overall. For class-B (wrong-date) episodes the candidate pool was already noisy; adding more dated facts to the same noisy pool plausibly worsens those failures (Finding 5 mechanism). The chain treats A and B as independent, but the corpus is shared.
**Sibling precedent:** the V14→V18 LME finding (`lme-v18-cognitive-overhead`) — adding capabilities targeted at one weakness regressed an unrelated category because attention/scoring is shared.
**What we'd need to verify or change:** Add an acceptance criterion that the **wrong-date failure count** does not increase. Concretely: count conv 2 Q1 + conv 3 Q1 in the re-run; if either moves from `wrong-date` to `still wrong-date but a different date now`, the feature is at parity or worse on those Qs and the spec's 0.7 ceiling claim must be revised.

---

## Summary table

| # | Finding | Severity |
|---|---|---|
| 1 | n=1 smoking-gun extrapolated to 3-of-5 without per-Q diagnostic | Critical |
| 2 | `_apply_graph_adjacency_boost` will treat `happened_before` like any other edge — semantic mismatch | High |
| 3 | FactExtractor reads summary, not transcript — Layer 1 may target the wrong stage | Critical |
| 4 | Two-prompt collateral on non-event content not measured rigorously | High |
| 5 | Dedup blind to event_date — creates class-B failures it claims not to | Medium |
| 6 | n=5 BEAM noise (±0.025) too close to gate threshold (≥0.55) for confident accept | High |
| 7 | Synthetic validation phrasing was a best-case; real extractor output may rank worse | High |
| 8 | 440-LOC / 2-3-day estimate excludes summarizer change, timestamp threading, multi-pass densifier | Medium |
| 9 | Per-class reasoning ignores shared-corpus interactions between class A and class B | Medium |

---

## Recommendation

Do NOT proceed to implementation as scoped. Before plan/spec lock:

1. **Spend $0.20 on three diagnostic dumps** (conv 4 Q1, conv 5 Q1, plus a clean reread of conv 2 Q1) and re-verify the 3-of-5 retrieval-miss class distribution.
2. **Read the episode summaries themselves** for the failing conversations. If "March 10" is absent from the summary text, Layer 1 must be split into a summarizer change + an extractor change, doubling the prompt-collateral risk.
3. **Resolve the adjacency-boost mechanism** (Finding 2) — either explicitly allowlist relations in `_apply_graph_adjacency_boost` as part of F075, or drop Layer 2 entirely and rely on the SQL column.
4. **Define the gate numerically and pre-register it** — include knowledge_update and information_extraction floors, wrong-date count ceiling, and an n=20 re-run trigger band.

These are 4 hours of pre-work that derisk a 5-day implementation against the most likely failure modes. Skipping them recreates the F065 trap structurally.
