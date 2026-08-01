# NOUS_EPISODE_CHUNK_RECALL_LIMIT is inert above 2× the caller's limit

**Date:** 2026-08-01
**Branch:** `fix/chunk-recall-limit-inert`
**Decision:** FORGE `145459a4`

---

## 1. The claim, verified

> `NOUS_EPISODE_CHUNK_RECALL_LIMIT=30` in prod, but `min(30, limit*2) = 20` at every
> default call site. Someone deliberately raised that knob and got nothing.

Every part checks out. Line references are at `56771b5` (main).

| # | Assertion | Evidence | Verdict |
|---|---|---|---|
| 1 | The `min()` exists as described | `nous/api/retrieval_pipeline.py:705-707` — `limit=min(settings.episode_chunk_recall_limit, limit * 2)` | **TRUE**, verbatim |
| 2 | Prod sets the knob to 30 | `.env.prod-snapshot:234` `NOUS_EPISODE_CHUNK_RECALL_LIMIT=30` | **TRUE** |
| 3 | The path is live in prod | `.env.prod-snapshot:198` `NOUS_EPISODE_CHUNKS_ENABLED=true`; `:233` `NOUS_CHUNK_HYBRID_SEARCH_ENABLED=true` | **TRUE** — not dead config |
| 4 | Default call site yields 20 | `tools.py:957` `recall_deep(..., limit: int = 10)`; `_RECALL_DEEP_SCHEMA` (`tools.py:1872-1878`) also defaults 10 → `min(30, 20) = 20` | **TRUE** |
| 5 | The eval harness measured 20 too | `nous_eval/config.py:61` `top_k: int = 10` → `retrieval_runner.py:529` `limit=top_k` → 20 | **TRUE** |

### 1.0 The cap binds on every query

Read-only probe against prod (`192.168.1.141`, `agent_id='nous-default'`, 2026-08-01):

```
chunks | episodes | embedded
  5292 |      461 |     5292
```

The candidate pool is ~176× the allotment and fully embedded, so the leg's `LIMIT` is the
binding constraint on every query — there is no query where the corpus runs out first and
20 vs 30 collapses to the same result set. The 10 lost candidates are real, on every call.

Assertion 2 was the one that had to be checked rather than assumed: the *default*
`episode_chunk_recall_limit` is 10 (`config.py:1713`), so on stock config `min(10, 20) = 10`
and the coupling is invisible. It only bites once an operator raises the knob past `2 × limit`
— which is exactly what prod did.

### 1.1 How the 30 got recommended in the first place

This is the part worth recording. The evidence that justified `=30` is
`scripts/diag/probe_r2_gold_ranks.py`. At lines 79-90 it calls
`_search_episode_chunks` **directly**, with `limit=total` (the entire corpus), and then
computes `in_top_30` by slicing ranks:

```python
results = await _search_episode_chunks(
    heart=heart, query=question, agent_id=AGENT_ID, limit=total, ...
)
ranks = [i + 1 for i, r in enumerate(results) if r[0] in gold_ids]
...
# R1 composition: the proposed episode_chunk_recall_limit=30
"in_top_30": bool(best and best <= 30),
```

The probe never entered `run_recall_pipeline`, so the `min()` never applied to it. The
recommendation ("gold-chunk in top-30 goes 1/5 → 4/5; compose with
`NOUS_EPISODE_CHUNK_RECALL_LIMIT=30`") was measured *below* the cap and then applied
*above* it. Nobody re-measured through the real path.

That is the actual defect class here — not the arithmetic, which is trivial, but that a
tuning knob and the probe used to tune it disagreed about what the knob meant, and the
disagreement was silent in both directions.

### 1.2 What is NOT claimed

- **Not** that k=30 beats k=20 in prod. That is unmeasured. The R2 probe measured gold-chunk
  *rank*, on a 5-question MAB slice, on a different corpus. This change makes the configured
  value take effect; it does not establish that the configured value is right.
- **Not** that the merged answer changes. More chunks enter the pool; whether any survive
  ranking into the rendered context is a separate question.

---

## 2. Why the coupling is wrong (not merely surprising)

`limit * 2` reads as protective: don't let one leg flood a caller that asked for a small
result set.

### 2.1 The multiplier idiom is fine — this use of it is not

First draft of this plan called the chunk leg "the lone holdout" for having a `limit * N`
coupling at all. That is **false**, and the distinction it misses is the whole argument.
`limit * N` is a common, correct idiom here:

| Site | Expression | Shape |
|---|---|---|
| `heart/heart.py:967` | `fetch_limit = limit * 2  # Fetch more for merging` | over-fetch, then `merged[:limit]` at `:1198`/`:1202` |
| `brain/brain.py:780`, `:1471` | `limit * 3` (`limit_expanded`) | over-fetch |
| `heart/facts.py:3333`, `heart/search.py:246` | `limit * 3` (`limit_expanded`) | over-fetch |
| **`api/retrieval_pipeline.py:706`** | **`min(setting, limit * 2)`** | **cap** |

Every other multiplier **widens** a derived candidate pool before a narrowing merge. The
chunk leg's multiplier sits inside a `min()` against an **operator-configured setting**, so
it **narrows** a configured value.

A targeted sweep for the *clamp* shape specifically — `min(settings.<x>, …limit…)` across
all of `nous/` — returns **exactly one hit: line 706**. Two adjacent patterns exist and are
deliberately excluded, because neither lets a derived value beat config:

- **over-fetch, no setting involved** — `heart/heart.py:967`, `heart/search.py:246`,
  `brain/brain.py:780`/`:1471`, `heart/facts.py:3333`, `cognitive/context.py:348`/`:1870`,
  `api/dashboard_queries.py:173`
- **API-boundary clamps on user input**, `min(user_arg, const)` — `heart/censor_actions.py:89`
  and siblings, `api/mcp.py:239`, `api/rest.py:1408`/`:1548`/`:1595`/`:1982`,
  `api/tools.py:1459`

That is the defect, stated precisely: not "a coupling exists" but "a coupling outranks
config."

### 2.2 The sibling-consistency argument is WEAKER than this plan first claimed

First draft argued: the keyed leg (8) and exemplar leg (25) take flat settings with no
coupling, `exemplar_top_k=25` already exceeds `2 × 10`, nobody complained — therefore flat
allotments are safe and the chunk leg should match.

**Adversarial review falsified the "therefore".** Keyed and exemplar hits are inserted with
a *synthetic, banded* score — `base - 0.005 * rank`, where `base` is
`keyed_fact_leg_score` / `exemplar_leg_score`, both defaulting to **0.55**
(`config.py:448`, `:494`). Their own docstrings say it: "below the direct-hit head", "so
keyed hits can enter context without displacing higher-scoring direct/chunk hits". They
**structurally cannot outrank real content**, however many you fetch.

Chunks are not banded. `_chunks_to_pipeline` (`:1627`) passes the raw score straight
through — cosine, or RRF up to ~1.0. A chunk reaching rank #1 is F067's stated *purpose*.

So `exemplar_top_k=25` is safe padding; 30 chunk candidates is 30 more shots at displacing
a fact from the top of the rendered list. **"No complaint about exemplar=25" is not evidence
that chunk scale is safe** — it is evidence the other two legs were deliberately designed
not to need the argument.

The consistency observation below is therefore kept only as *context on house style*. It is
not load-bearing. The argument this change rests on is §2.1: a derived expression must not
silently outrank an operator-configured value.

The three optional legs, each of which has its own operator knob:

| Leg | Feature | Allotment | Coupled to caller `limit`? |
|---|---|---|---|
| chunk | F067 | `min(episode_chunk_recall_limit, limit * 2)` | **yes** |
| keyed fact | F085 | `keyed_fact_leg_k` (default 8) — `retrieval_pipeline.py:740` | no |
| exemplar | F086 | `exemplar_top_k` (default **25**) — `retrieval_pipeline.py:926` | no |

`exemplar_top_k` defaults to 25, which already exceeds `2 × 10 = 20` at the standard call
site, with no coupling and no complaint. So the chunk leg is not enforcing a shared
invariant — it is the lone holdout from an older design.

### 2.3 The documentation argument, quoted accurately this time

First draft quoted both sources as identically "F067 max chunks returned by the chunk-recall
leg". Only one of them says that. Verbatim:

- `CLAUDE.md:528` — "F067 max chunks returned by the chunk-recall leg." **Describes a flat
  allotment.** Wrong today, correct after the fix.
- `config.py:1715` — "F067 max chunks returned by the **new** chunk-recall leg **before RRF
  merge**."

That second phrase is the honest complication. "Before RRF merge" reads at least as
naturally as *"a pre-merge candidate cap"* — i.e. something a downstream `min()` might
legitimately bound — as it does as *"a guaranteed allotment"*. So the first draft's
"the docs are not wrong about the intent; the code is wrong about the docs" **asserted more
certainty than the source text supports**, and is withdrawn.

The weaker, defensible version: one description clearly implies a flat allotment, the other
is ambiguous, and **neither warns that the value can be silently reduced**. That is enough
to call the current state a documentation defect regardless of which reading was intended —
and the fix must retire the ambiguous phrase rather than read past it (`§7`).

---

## 3. The fix

One expression, `retrieval_pipeline.py:705-707`:

```python
-                limit=min(
-                    settings.episode_chunk_recall_limit, limit * 2
-                ),
+                limit=settings.episode_chunk_recall_limit,
```

Plus: a comment recording *why* the coupling is gone (so it is not
"helpfully" restored), a regression test, and the doc corrections.

### 3.0 The tradeoff this locks in, owned explicitly

Flat means the chunk leg **ignores the caller's `limit` entirely**. At `limit=1` — legal per
`_RECALL_DEEP_SCHEMA` (`tools.py:1872-1878`, `minimum: 1`) — today's leg returns 2 chunks;
after the fix it returns 30. A caller asking for a deliberately narrow, cheap search gets
1 fact and up to 30 chunks.

That contradicts the tool's own docstring (`tools.py:969-970`): *"limit: Per-leg result cap
(each search leg is capped individually…)"*.

**It is chosen anyway, for three reasons:**

1. **No multiplier can fix this without recreating the bug.** To honor `=30` at the standard
   `limit=10`, the multiplier must be ≥3. But `min(setting, limit * 3)` then silently
   re-caps an operator who sets 40 — the identical defect with a new constant. Preserving
   proportionality at the low end and honoring the knob at the default are mutually
   exclusive as long as one expression does both jobs.
2. **The docstring contract is already false.** At `limit=1` today, the keyed leg returns 8
   and the exemplar leg 25 — both ignore `limit` completely. This change makes an existing
   inaccuracy worse; it does not create one. So the docstring gets corrected here (`§7`)
   rather than a contract nothing honors being preserved for one leg.
3. **A per-leg allotment and a per-call limit are different concepts.** Conflating them is
   precisely what produced this bug. An operator who wants fewer chunks now has a knob that
   works — which is the entire point of the change.

**What this costs:** callers passing `limit < 10` get disproportionately many chunks. The
`limit` is fully LLM-controlled (schema exposes 1-50, no server-side clamp), so this is
reachable, not hypothetical — though no prompt or template pins the model to a non-default
value, and no evidence was found of it firing in prod today.

One amplification path deserves naming, surfaced by adversarial review: heartbeat dynamic
checks may call `recall_deep` (`heartbeat/dynamic.py:42-45`, no `limit` override) on a
schedule, against a hard daily token budget (`NOUS_HEARTBEAT_DAILY_TOKEN_BUDGET`). A check
that used a small `limit` for a cheap periodic scan would, post-fix, pay for 30 chunks every
tick. No such check exists today — flagging the mechanism, not a live defect.

### 3.1 Rejected alternatives

| Option | Why not |
|---|---|
| Document it as a ceiling, change nothing | Zero risk, zero value. Prod still measures 20, the operator's intent stays unmet, and the next person to raise the knob repeats the mistake. |
| New boolean flag gating the `min()` | Ceremony around one expression. The knob it would protect *is itself the dial* — see §3.2. |
| `max(setting, limit * 2)` | Makes the setting a floor. Nobody wants that; it inverts the documented meaning instead of fixing it. |
| **Widen the multiplier: `limit * 2` → `limit * 3`** (one-character diff) | Recommended by adversarial review as the smallest fix. Rejected — see §3.1.1. |
| Also make the `[:3]` chunk-seed cap configurable (`retrieval_pipeline.py:1058`) | Same bug family, different bug. Out of scope — flagged in §6, not fixed. |
| Change `recall_deep`'s default `limit` from 10 to 15 instead | "Fixes" it only at the default, by coincidence: `min(30, 30) = 30`. Leaves the clamp in place, so the knob stays inert above 30 and every non-default caller keeps the old bug. Treats the symptom at one input value. |

### 3.1.1 Why not `limit * 3` — the strongest rejected alternative

Adversarial review recommended this over decoupling, and it is a genuinely good proposal:
one character, fixes the reported bug, keeps the low end proportional.

| caller `limit` | today `×2` | proposed `×3` | flat |
|---|---|---|---|
| 1 | 2 | 3 | 30 |
| 5 | 10 | 15 | 30 |
| **10** | **20** | **30** ✓ | **30** ✓ |
| 25 | 30 | 30 | 30 |

It is rejected on one point: **`×3` works only because `30 = 3 × 10` coincidentally.**

Set the knob to 40 — a perfectly ordinary operator action, and the *exact* action that
produced this bug report — and `min(40, 30)` silently yields 30. The identical defect,
one constant later, discovered the same way: by someone eventually noticing their env var
did nothing.

The reported defect is not "the number is 20 instead of 30". It is **"the configured value
and the effective value diverge silently."** `×3` narrows the window in which that happens
to `limit < 10`; it does not close it. An eval at `top_k=5` would still measure 15 while
believing 30 — the same failure that produced this report, at a different input.

Flat closes the class. That is worth the low-`limit` tradeoff owned in §3.0, and it is
what makes the knob a *target* rather than a ceiling — which is the distinction the change
was requested to settle.

**Fair to the reviewer:** if the low-`limit` blowout ever proves to matter in practice,
`×3` is the right retreat, and it should be reached for deliberately rather than by
restoring `×2`.

### 3.2 Rollback, stated precisely

No new flag. Rollback is `NOUS_EPISODE_CHUNK_RECALL_LIMIT=20`.

**Correction to this plan's first draft.** It claimed `=20` restores today's behavior "for
any caller with `limit <= 15`". That is wrong, and the baseline test run caught it. Today's
value is `min(30, 2L)`; a flat 20 equals that only where `min(30, 2L) = 20`, i.e. **at
`L = 10` exactly**:

| caller `limit` | today (`min(30, 2L)`) | after fix, knob=20 | same? |
|---|---|---|---|
| 5 | 10 | 20 | no |
| **10** | **20** | **20** | **yes** |
| 15 | 30 | 20 | no |
| 25 | 30 | 20 | no |

No flat value can reproduce a limit-dependent function across all limits — that is the
nature of the change, not a defect in the rollback. What matters operationally: `L = 10` is
the value **every** real call site uses (`recall_deep` default 10, eval `top_k` 10), so
`=20` is an exact restore in practice and a divergence only for a hypothetical caller that
passes a non-default `limit`.

### 3.3 This does NOT land dark

Stated plainly because the temptation is to claim otherwise: prod already has the knob at
30 and chunks enabled, so **the deploy changes prod retrieval immediately** — the chunk leg
goes from 20 to 30 candidates per `recall_deep` call. That is the entire point of the fix,
and it is also its risk. Behavior on stock defaults (knob at 10) is unchanged, since
`min(10, 20)` was already 10.

---

## 4. Blast radius

**Changes:** the number of chunk candidates entering the merged pool, whenever an operator
has set the knob above `2 × limit`. Today that is prod (20 → 30) and nothing else.

**Does not change:**
- Stock-default deployments (`episode_chunk_recall_limit=10`) — `min(10, limit*2)` was
  already 10 for any `limit >= 5`.
- Any deployment with `NOUS_EPISODE_CHUNKS_ENABLED=false` — the whole stage is skipped.
- Every other leg, the merge, the rerank, the formatter.
- **The pre-turn cognitive path.** `nous/cognitive/context.py` does not call
  `run_recall_pipeline` at all, so the system-prompt context assembly is untouched. This is
  a `recall_deep`-and-eval change only.
- **Graph fan-out.** Stage 2b seeds `acc.chunk_results[:3]` regardless of allotment
  (`:1058`), so widening the leg does not widen the graph expansion.

**Callers affected:** `api/tools.py:1047` (`recall_deep`), `nous_eval/retrieval_runner.py:524`
(`limit=top_k`, `NOUS_EVAL_TOP_K` default 10), `nous_eval/multi_turn_eval.py:266` (default
10). **Eval numbers will shift** — any baseline captured before this change is not
comparable to one after it.

**Downstream cost:** the chunk leg's cost is one vector (or RRF hybrid) query plus one
content fetch, both already `LIMIT`-bound. Going 20 → 30 is +50% rows on two bounded
queries. No extra LLM calls, no extra embeddings — the query embed is computed once
regardless.

### 4.1 The real cost is in the rendered output, not the query

Traced during planning, because "+10 rows on a bounded query" understates it:

- **The formatter does not truncate.** `tools.py:400` renders the Heart Memory section with
  a flat `for i, result in enumerate(heart_results, 1)` — no cap, no token budget. So 10
  more chunks means 10 more full result lines, each carrying up to `episode_chunk_size`
  (600) chars of chunk text. Order **+6 KB (~1.5 K tokens) added to every prod `recall_deep`
  result.** That is the honest number.
- **It is addition, not truncation.** Nothing is dropped to make room. And the fresh tool
  result is safe from the pruner: `compaction.py:778` protects the last
  `keep_last_tool_results` (2) tool results, so the output the agent reads this turn is not
  soft-trimmed. The extra bulk costs tokens now and gets trimmed harder once it ages out of
  the protected zone.
- **Nothing reranks chunks. Ever.** `PipelineStats` sets `ce_reranked=False` and
  `mmr_applied=False` with the comment *"happens inside `heart.recall` already"*
  (`:596-597`). That is **Stage 1** — facts and episodes. Chunks are **Stage 1.5**, outside
  `heart.recall` entirely. So MMR and the cross-encoder never see a chunk candidate **in any
  configuration**, flags on or off. (Both are off in prod anyway: `NOUS_MMR_ENABLED=false`,
  `NOUS_CROSS_ENCODER_ENABLED=false`.) There is no ranking-quality mitigation between the
  leg's `LIMIT` and the rendered output — the allotment *is* the only control.
- **But ranking displacement is real.** In prod `rerank_by_score=True` — `tools.py:1043-1046`
  sets it whenever chunks are enabled and the search covers `fact`/`all`, which is the
  default path. So the 10 extra chunks are score-sorted *against* facts and episodes and can
  outrank them. Nothing falls out of the output, but items the agent reads top-first can be
  pushed down. R2's RRF renormalisation puts chunk scores on the same [0,1] scale as the
  heart legs, so this is a fair competition rather than a scale artifact — but "fair" is not
  "beneficial", and which way it nets out is exactly what §1.2 says is unmeasured.

---

## 5. Verification plan

1. **Prove the current behavior first.** A test that patches `_search_episode_chunks`, runs
   `run_recall_pipeline` with `episode_chunk_recall_limit=30, limit=10`, and asserts the
   captured `limit` kwarg. It must record **20** against current `main` and **30** after the
   fix. Establishing the failing baseline is the point — no test covers this expression today.
2. **Pin the flat contract.** Assert the leg limit equals the setting across a small matrix
   of caller limits (1, 10, 25) so a future reviewer cannot reintroduce a coupling without a
   red test.
3. **No regressions.** Full `uv run pytest tests/` with `NOUS_TEST_DB=postgres` (pytest
   falls back to SQLite silently otherwise). Compare the failure *set* before and after, not
   just the count.
4. **Confirm nothing pinned the old value.** Already checked: no test in the repo asserts
   `limit * 2` or the pipeline's chunk-limit computation. Every chunk test calls
   `_search_episode_chunks` directly with its own explicit limit.

---

## 5.1 Verification results

| Step | Result |
|---|---|
| Failing baseline established | `TestChunkLegAllotment` 4 failed / 3 passed against unmodified `main`; the diagnostic failure is `assert 20 == 30` at prod's knob=30 / `limit=10`. The 3 that passed are `limit=25`, `limit=50` (where `min()` was already non-binding) and the stock-default case — exactly the cases the fix must NOT change. |
| Post-fix | 7 / 7 green. |
| Full suite, pre-fix | 81 failed / 5377 passed = **77 pre-existing + my 4**. The 77 matches the known-stable pre-existing count from the previous PR — an independent check that the harness is configured correctly. |
| Lint | My edited line ranges produce zero `ruff check` errors. All four touched files already fail `ruff format --check` on `main` (verified via `git show main:<file>`), so no drift added. CI lint is `continue-on-error: true` regardless. |

**A false baseline was caught and discarded first.** The initial run reported *1097 errors*; they
were `socket.gaierror`, because the local `.env` sets `DB_HOST=postgres` (the compose service
name, unresolvable from the host). Confirmed by re-running one file with `DB_HOST=localhost`:
6 errors → 6 passed. The failure-set extraction was also wrong — `grep "^(FAILED|ERROR)"`
swept in application log lines beginning with `ERROR`, inflating 29 real failures into a
1,128-line "set". Correct invocation is `NOUS_TEST_DB=postgres DB_HOST=localhost`, anchoring
the grep to `^(FAILED|ERROR) tests/` and stripping the ` - reason` suffix. Same class of trap
as the previous PR's "77 regressions" false alarm: a harness misconfiguration that makes the
baseline look catastrophic and, uncaught, would have had garbage compared against garbage and
called clean.

## 6. Out of scope (flagged, not fixed)

- `retrieval_pipeline.py:1058` — `acc.chunk_results[:3]` hardcodes the Stage 2b chunk-seed
  fan-out at 3, independent of how many chunks the leg retrieved. Same family (a literal
  where a knob is implied), already logged in
  `docs/reviews/2026-07-02-nonconfigurable-constants-scan.json`. Note this *bounds* the blast
  radius: graph fan-out is unaffected by the allotment, since it only ever seeds 3 chunks.
- **`retrieval_pipeline.py:1799` — wrong return annotation.** `_search_episode_chunks` is
  annotated `list[tuple[UUID, str, float]]` (3-tuple) but both branches return 4-tuples
  (`:1847`, `:1867`), and its own docstring at `:1813` says 4. Pre-existing, in the function
  whose call site I am editing. Mentioned, not fixed — it is a real defect but not this one,
  and `tests/test_retrieval_pipeline.py:725-729` records that a 3-tuple assumption once
  masked a genuine unpacking bug, so it deserves its own change with its own test.
- **Dated review docs go stale and stay stale.**
  `docs/reviews/memory-retrieval-whitepaper-2026-06-08.md:88` is the only document that
  states the COUPLED semantics explicitly. It is a dated snapshot of what was true on
  2026-06-08, so it is left alone; rewriting history in review artifacts is worse than
  letting them date. Same for
  `docs/reviews/2026-07-02-nonconfigurable-constants-scan.json:1173-1212`.
- `CLAUDE.md:529` ("compose with `NOUS_EPISODE_CHUNK_RECALL_LIMIT=30`") and
  `docs/plans/2026-07-11-memory-simplification-plan.md:153` both already assume the FLAT
  semantics. They are advice that is unachievable today and becomes achievable after this
  change — no edit needed; the fix makes them true.
- Whether k=30 is the *right* value. Wants an eval-DB A/B on the prod-shape corpus. This
  change is a precondition for that measurement being meaningful — today the harness cannot
  express k=30 at all through the pipeline.

---

## 7. Files

| File | Change |
|---|---|
| `nous/api/retrieval_pipeline.py` | drop the `min()`; comment the reason so it is not "helpfully" restored |
| `nous/api/tools.py` | `recall_deep` docstring `:969-970` — "per-leg result cap" is false for the keyed, exemplar and (now) chunk legs. State that legs owning their own setting ignore `limit`. |
| `nous/config.py` | `episode_chunk_recall_limit` description — flat per-leg allotment; **retire "before RRF merge"** (ambiguous per §2.3, and stale: fusion happens inside `_search_episode_chunks`) |
| `CLAUDE.md` | same correction in the env table (`:528`) |
| `tests/test_retrieval_pipeline.py` | new `TestChunkLegAllotment`: leg limit equals the setting across caller limits 1/5/10/25/50; docstring owns the low-`limit` tradeoff from §3.0 |

## 7.1 Errors in this plan, and who caught them

Recorded because the pattern matters more than any single line.

| # | Error | Caught by |
|---|---|---|
| 1 | "Rollback to `=20` is exact for `limit <= 15`" — true only at `limit=10`. Conflated the *saturation* point of `min(30, 2L)` (correctly `L>=15`) with the *rollback-equivalence* point (`L=10`). | my own parametrized test, before review |
| 2 | "The chunk leg is the lone holdout for having a `limit * N` coupling" — false; the idiom is everywhere. The real distinction is widen-vs-narrow. | me, mid-sweep |
| 3 | "Sibling legs prove flat allotments are safe" — falsified. Keyed/exemplar are score-**banded** at 0.55 and cannot outrank content; chunks carry raw scores and are designed to reach rank #1. | adversarial review |
| 4 | Misquoted `config.py:1715`, omitting "before RRF merge" — the one phrase that supports the *opposite* reading. | adversarial review |
| 5 | Blast radius priced DB cost only, omitting that MMR/CE never see chunks at all. | adversarial review |

Three of the five were overclaims of *precision* — asserting a crisp boundary or a clean
equivalence without computing it. Same failure mode as the previous PR's false "lands dark"
claim.
