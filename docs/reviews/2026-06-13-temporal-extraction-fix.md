# Temporal extraction fix — `happened_before` 0.27 → ?

Follow-on to `docs/reviews/2026-06-13-edge-precision-audit.md`, which found
`happened_before` (F075 temporal) at **0.27** precision — the worst gate-eligible
relation. This doc diagnoses the root cause and fixes it.

## Why it's load-bearing

Prod has `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED=true` and
`NOUS_RECENCY_RESOLVER_ENABLED=true`. The adjacency boost
(`retrieval_pipeline.py:787`) excludes only `contradicts`, so the 0.27-precision
`happened_before` edges **actively boost prod retrieval ranking today**. The
recency resolver consumes `event_date` directly, so wrong dates also mis-resolve
fact conflicts. This is a live retrieval-quality bug, not a dark feature.

## Diagnosis (prod probe, `scripts/diag/probe_happened_before.py`)

`happened_before` is built mechanically (`graph_densifier._build_happened_before_edges`):
each dated active fact links to the next-distinct-`event_date` active fact **in
the same episode**, no semantic gate. So edge precision == `event_date`
extraction precision. A 25-edge prod sample + corpus stats decompose the 0.27:

| failure mode | corpus share | mechanism |
|---|---|---|
| **Bibliographic dates** | 25% of dated facts are first-of-month; **46% of edges** originate from one | The extractor stamps a cited paper's *publication* month ("Xu, Jan 2026, arXiv:…") as an event. Month-only → `YYYY-MM-01`, so papers chain P13→P14 by pub date (gap=28/31 dominate the distribution). |
| **Wrong-year** | 13% of dated facts | Relative dates resolve to the prior year — `event_date=2025-05-25` on facts learned `2026-05-25`, producing bogus 365-day chains between same-day facts. |
| co-episode-but-unrelated | residual | dated facts in one episode chained though narratively unrelated |

Only **17% of active facts carry any `event_date`** (415/2392 on prod, 419 on the
eval copy). The corpus skews to research-paper reading, which makes the
bibliographic mode dominant *here*, but the prompt guards are universal.

Both producers lacked the guard: the live `episode_summarizer`
`_F075_TEMPORAL_INSTRUCTION` and the F075.1 backfill `_CLASSIFY_SYSTEM`. The
existing bad dates came from the **backfill** (the dated facts predate F075's
live ship date), so remediation must re-run a corrected backfill.

## Fix (PR — both producers + reclassify tooling)

1. **`episode_summarizer._F075_TEMPORAL_INSTRUCTION`** — added: (a) exclude the
   publication/arXiv/release/version date of any *referenced artifact* (it's
   bibliographic metadata, not an event); (b) omit `event_date` when only
   month/year is known (no false first-of-month); (c) take the year from
   `EPISODE_START_TIMESTAMP` — never assume a prior year.
2. **`scripts/backfill_temporal_facts.py::_CLASSIFY_SYSTEM`** — same bibliographic
   + month-granularity guards; per-fact message now injects the fact's
   `learned_at` as the year anchor.
3. **`--reclassify` mode** — re-examines facts that already carry an
   `event_date` (eligibility `event_date IS NOT NULL`) so the corrected prompt
   can null bibliographic dates / fix wrong-year ones on legacy data. Reused for
   both the eval measurement and the prod remediation (step 4).

Tests: `test_f075_backfill.py` (year-anchor injection, backward-compat without
`learned_at`, system-prompt guard regression), `test_f075_end_to_end.py`
(summarizer-prompt guard regression). All green.

## Measurement (eval copy `:5433/nous_eval_prod`, apples-to-apples)

Before reclassify: 419 dated facts — 105 first-of-month (25%), 53 wrong-year
(13%); 50 `happened_before` edges, 23 (46%) from a first-of-month source.

_Procedure: delete `happened_before` edges → `--reclassify` all 419 with the
corrected prompt (rebuilds edges from corrected dates) → re-run
`nous_eval.run_edge_audit` on `happened_before`._ Report:
`reports/edge-audit-temporal-postfix.md`.

**Result — the dominant noise is eliminated:**

| metric | before | after |
|---|---|---|
| dated facts | 419 | 263 (**156 bad dates → NULL**) |
| first-of-month (bibliographic) | 105 (25%) | **8 (3%)** — 92% corrected |
| wrong-year | 53 (13%) | **1 (0%)** — 98% corrected |
| `happened_before` edges | 50 | **8** (rebuilt from corrected dates) |
| edges from a bibliographic source | 23 (46%) | **0 (0%)** |
| **`happened_before` precision (judged)** | **0.27** | **0.62** (5 YES / 1 WEAK / 2 NO, n=8) |

Precision is now underpowered (n<15) *because* the fix correctly removed the
noise edges — fewer-but-cleaner is the goal. The residual 2 NO + 1 WEAK are NOT
the fixed modes; they are (a) genuinely-dated facts chained to an **unrelated**
co-episode fact (self-eval snapshot → research doc — an edge-construction
relatedness gap, not an extraction error), and (b) a standing workflow guideline
that received a date. A more aggressive "discrete events only" prompt guard was
considered and **rejected**: it would also null legitimate dated state snapshots
("autopilot live state Apr 30", "repo state May 23"), trading precision for
recall ambiguously. The proper fix for the residual is an edge-construction
relatedness gate (separate, deferred).

**The larger banked win is not the 8 edges** — it is the 156 corrected
`event_date`s feeding the live `recency_resolver` (no more wrong-year
supersessions) and any future date-aware retrieval. `happened_before` precision
is just the visible proxy.

## Step 4 — prod remediation (after eval validation)

Run the same `--reclassify` on prod (`nous-default`), which corrects the ~415
dated facts and rebuilds `happened_before`. Then re-audit on live prod and
confirm the recency resolver no longer sees wrong-year dates.
