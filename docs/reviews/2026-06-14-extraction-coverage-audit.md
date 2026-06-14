# Fact-extraction coverage audit — prod (`nous-default`), 2026-06-14

**Method:** 20 random prod episodes that have both F067 verbatim chunks and
extracted facts. For each, Sonnet diffed the chunk transcript against the
facts extracted from that episode (`source_episode_id`), labelling every
salient/queryable item CAPTURED or MISSED + a type. Read-only.
`scripts/diag/coverage_audit.py`.

## Result: coverage 0.70 (265 captured / 380 salient items, 30% missed)

| type | miss rate | n | note |
|---|---|---|---|
| **status_state** | **0.54** | 74 | worst — forecasts, deliverables, run/file state |
| **dated_event** | **0.45** | 40 | the EXTRACTION root of the BEAM event_ordering wall |
| **preference** | 0.36 | 14 | target audience, user traits |
| entity_relationship | 0.23 | 31 | |
| numeric_config | 0.23 | 84 | numbers/IDs/versions/paths |
| other | 0.28 | 47 | personal facts (user location, background) |
| procedure_howto | 0.16 | 38 | well-captured |
| decision_rationale | 0.13 | 52 | best-captured |

## The pattern: extractor is dev-knowledge-biased, drops user-task detail

Well-captured types (decision 0.13, procedure 0.16) are the "Nous building
itself" facts. The missed types are "assistant doing the user's tasks" facts:
weather forecasts (Annapolis Sat/Sun temps + winds + Bay chop; Portland ME
4-day forecast), trip dates (Portland May 15–18), deliverables (article
filename + 1,835 words), run details (workspace path, ETA), and personal facts
(user is Russian-born; located in Silver Spring). These are exactly the
specifics a factoid/temporal question asks about — and they never become facts,
so they can never be retrieved.

## Why this is the load-bearing gap

- **Confirms the temporal thread.** `dated_event` is missed 45% of the time at
  *extraction*. Fixing date-*stamping* (PR #523) could not move BEAM
  `event_ordering` because the dated events are dropped before they get a date.
  Coverage is the upstream lever, exactly as the BEAM result predicted.
- **Bounds the LME retrieval→QA gap from the write side.** A 30% extraction
  miss caps how much retrieval (0.91 hit@10) can deliver to QA (0.60) — you
  cannot retrieve a fact that was never written.

## Root cause (code)

The summarizer's `candidate_facts` instruction scopes extraction to "concrete,
reusable knowledge (tool configs, preferences, architectural decisions, API
behaviors)" (`episode_summarizer.py` `_SUMMARY_PROMPT`). That framing
under-weights: specific **dated events** the user did/experienced, **status/
state snapshots** the user may later reference, and **personal facts** about the
user. The episodes with 8K-char transcripts → 1 fact are this bias in action.

## Recommended fix (eval-gated)

Broaden the extraction mandate in the summarizer + knowledge-extractor prompts
to capture queryable specifics: named dated events ("user travelled to X on
DATE"), user/personal facts, and status snapshots the user is likely to ask
back about — while keeping a noise guard against pure ephemeral chit-chat.

**The tension is coverage vs fact-store bloat:** weather-per-day data is
queryable but voluminous. The fix must be measured both ways — re-run this
coverage audit (target the 0.54/0.45/0.36 types up) AND confirm QA on LME/BEAM
doesn't regress from added noise, before flipping.

**Caveats:** n=20, judge subjectivity on "salient"; facts scoped to
`source_episode_id` (a fact attributed elsewhere would undercount CAPTURED);
some `status_state` misses (weather) are arguably ephemeral — the fix is a
recall/noise tradeoff, not "extract everything."

## Fix (flag-gated, land-dark) + A/B validation

Three levers, all gated by `extraction_coverage_broadened` (default **False**):
1. **Prompt** — `episode_summarizer._COVERAGE_EXPANSION_INSTRUCTION` appended when
   the flag is on: broadens `candidate_facts` to queryable specifics (events,
   status/state, personal facts, named details) with a noise guard, adds
   `event`/`status` category homes.
2. **Token budget** — summary `max_tokens` 1500 → 3000 when broadened (the 1500
   cap truncated long fact lists).
3. **Stable cap** — the hardcoded `stable[:5]` in `_merge_summaries` (a hard
   ceiling on multi-chunk episodes) becomes `candidate_facts_stable_limit`
   (default 15) when broadened; legacy 5 when off.

**A/B (`scripts/diag/coverage_ab.py`, 10 prod transcripts, same Sonnet judge,
one variable = the flag):**

| | coverage | facts/episode |
|---|---|---|
| flag OFF | 0.73 | 3.5 |
| **flag ON** | **0.91** | 5.4 |
| lift | **+0.17** | +1.9 (bounded) |

Strong coverage lift (+25% relative) with a moderate, non-runaway fact increase.
Tests: `test_f025_chunked.py` (stable-cap broadened vs legacy, instruction
guards). Lands **dark** (flag OFF).

**Flip gate (before enabling in prod):** the coverage win is validated, but the
noise side must clear a QA non-regression check — re-run LME/BEAM QA with the
flag on and confirm the +1.9 facts/ep doesn't dilute retrieval/answers — before
flipping `NOUS_EXTRACTION_COVERAGE_BROADENED=true`.
