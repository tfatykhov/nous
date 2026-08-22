# C1: Chunk-Leg Drop Capture — implementation plan

**Spec:** MAB evaluation program, 2026-08-22 (Spec C1)
**Validation:** decision `2f81de7b` — CONFIRMED on mechanism, approve with 6 revisions
**Extends:** F091 (`nous_system.retrieval_log`, migration 070)
**Scope:** telemetry only. Zero change to what any caller is served.
**No migration. No new config. No new flag.** Rides `retrieval_telemetry_enabled` + `tr.enabled`.

---

## 1. What the spec got right (verified against code, not docs)

Every line reference in the spec was checked against the working tree at `fd10fa6`.

| Spec claim | Verified |
|---|---|
| `search.py:289` `limit_expanded = max(fetch_base * 3, limit)` | ✅ verbatim |
| vector leg `LIMIT :limit_expanded` at `:308`, materialized `:311` | ✅ |
| keyword leg `LIMIT :limit_expanded` at `:332`, materialized `:335` | ✅ |
| `_rrf_merge` truncates and the surplus dies unreported | ✅ `:358` (pinned) / `:364` (coupled) |
| `_search_episode_chunks` vector-only branch pushes the cut into SQL | ✅ `:2230-2236` `ORDER BY … LIMIT :k` — an instrument there reads zero |
| `Heart.recall` already has a `dropped_out` seam | ✅ `heart.py:881`, consumed `retrieval_pipeline.py:843` + `:866-870` |
| Trace API supports what C1 needs | ✅ `leg(skip_reason=…)`, `add(rank=…)`, `drop(SLICED_OFF, stage)` |
| Register survivors before losers (the #595 lesson) | ✅ real — and chunk survivors are registered LATE (`:433`, assembly), not in `_run_stages` |
| `limit_expanded = 90` at the named eval config | ✅ `limit=30`, `chunk_rrf_penalty_limit` unset → `fetch_base=30` |

The spec's own self-correction — capture at `hybrid_search`, not `_search_episode_chunks` — is
correct and is the load-bearing insight. Building it where the earlier draft aimed would have
shipped an instrument that reads zero by construction.

## 2. The six revisions

### R1 — `hybrid_search` has THREE client-side exits; the spec covers one

```
search.py:337-339   if embedding is None: return keyword_results[:limit]   # exit 2 — UNCOVERED
search.py:358/:364  merged = _rrf_merge(...)                               # exit 1 — the spec's target
search.py:365-374   if require_keyword_hit: filter + [:limit]              # exit 3 — see R3
```

Exit 2 is **live for the chunk leg**: `_search_episode_chunks:2182-2183` sets
`query_vec = None` when there is no embedder or the embed returns empty, and the
docstring at `:2167` documents that degradation as intended behaviour. It cuts
`keyword_results` (up to `limit_expanded` rows) down to `limit` **in Python**.

Under the spec as written, `dropped_out` stays empty there and reads as *"nothing was
dropped"* — the precise lie C1.2 forbids for the vector-only path, reappearing one branch
over. Cover it, with a distinct stage name: it is not an RRF merge, and calling it one
would misattribute the mechanism.

- exit 1 → `sliced_off` @ **`chunk_rrf_merge`**
- exit 2 → `sliced_off` @ **`chunk_keyword_only_limit`**

### R2 — do not register the cut at Stage 1.5; drain it just before `finalize`

`max_candidates` defaults to 300 and `RetrievalTrace.add` is **first-wins across all legs**
(`retrieval_trace.py:338-348`). The chunk discard set is the largest population on the
path — up to `2 * limit_expanded − limit` ≈ **150 rows** at the eval config. Registering it
at Stage 1.5 (`:889`) puts it ahead of `keyed`, `keyed_r2`, `exemplar`, `spreading_activation`,
`brain` and `graph_expanded` in cap order, so those legs lose candidate detail on exactly the
sampled rows an operator opens. `_truncated` flags it, so it is not silent — but degrading
five other legs to instrument one is the wrong trade.

Buffer the cut on `_PipelineAccumulator` and drain it in `run_recall_pipeline` immediately
before `tr.finalize` (`:737`). Losers go last **globally**, not just within the chunk leg.

This also subsumes the spec's stage-level survivor pre-registration: `_tr_entries("chunk", …)`
at `:433` has already run by then, so served chunks hold their slots **and keep their
snippet** (see R6). One fewer moving part.

### R3 — `require_keyword_hit` falsifies gate 2's identity

At `:365-374` the merge result is filtered by keyword presence and re-truncated **after** the
slice. `(vector ∪ keyword) − served` then mixes rows the *filter* removed with rows the
*slice* removed, and labelling all of them `sliced_off@chunk_rrf_merge` misattributes the
gate. Not live — the chunk leg never sets the flag — but the parameter sits on a helper with
seven callers, and F091's whole contract is that the named stage is the true cause.

Guard it explicitly rather than leaving a latent mislabel: when `require_keyword_hit` is set,
`hybrid_search` records the keyword-filter casualties under their own stage
(`hybrid_keyword_filter`, disposition `filter_dropped`) and the slice casualties under the
merge stage. Cost: one set difference already being computed.

### R4 — gate 4 needs the sample rate pinned

`add()` returns early when `_capture_candidates` is False (`:335`), and
`NOUS_RETRIEVAL_TELEMETRY_CANDIDATE_SAMPLE_RATE` defaults to **0.1**. "`n_candidates` rises by
the discard count" is unobservable on ~90% of retrievals. The replay must set
`NOUS_RETRIEVAL_TELEMETRY_CANDIDATE_SAMPLE_RATE=1.0`, and either raise
`NOUS_RETRIEVAL_TELEMETRY_MAX_CANDIDATES` above `n_candidates_expected` or assert on
`truncated` being False before asserting the delta.

### R5 — the "R2 band sits entirely inside the window" claim is config-dependent

- eval config (`episode_chunk_recall_limit=30`, `chunk_rrf_penalty_limit` unset):
  `fetch_base=30` → `limit_expanded = 90`. Band 21–60 fits with room. ✅
- **validated prod config** (#580/#581: `chunk_rrf_penalty_limit=20`,
  `episode_chunk_recall_limit=20`): `fetch_base=20` → `limit_expanded = 60`. The band's top
  edge lands **exactly at the window boundary**.

The instrument's reach is a function of `fetch_base * 3`, not a constant. State it in the
docstring so the next reader does not carry the 90 across configs. Consistent with the
spec's own "no claim about prod diagnostic yield".

### R6 — pre-registration must carry content, or first-wins eats the snippet

`_tr_entries` at `:423` supplies `content=r.description`. Any earlier `tr.add` for the same
`(id, type)` without content wins and stores `snippet=""`. R2's placement avoids this
entirely; noted so it is not reintroduced.

---

## 3. Implementation

### Step 1 — `hybrid_search` gains a write-only `dropped_out` sink

**File:** `nous/heart/search.py`

Add `dropped_out: list | None = None` to the signature, mirroring `Heart.recall`'s parameter
and rationale. Entry shape, per the spec:

```python
(doc_id, vector_rank | None, keyword_rank | None, best_leg_score, stage)
```

Ranks are **1-based** positions within each leg's own list; `None` where the id was absent
from that leg. `stage` is appended to the spec's 4-tuple because R1/R3 mean the same sink now
receives rows dropped by three different mechanisms, and the caller must not have to guess
which. `best_leg_score` is `max` of whichever leg scores are present — the legs emit
different scales (cosine vs normalized `ts_rank_cd`), so this is a diagnostic magnitude, not
a comparable score, and the docstring says so.

Populate at all three exits:

| exit | condition | stage recorded |
|---|---|---|
| keyword-only | `embedding is None` | `keyword_only_limit` |
| merge slice | always (hybrid) | `rrf_merge` |
| keyword filter | `require_keyword_hit` | `keyword_filter` |

The caller prefixes `chunk_` — the helper is shared, so it names the mechanism, not the leg.

Constraints, all load-bearing:
- **Never fetch content** for these rows. The batch fetch at `retrieval_pipeline.py:2207-2210`
  stays keyed on served ids.
- **Never touch** `merge_limit`, `penalty_limit`, `limit_expanded`, or the returned list.
- The parameter is **write-only**, exactly as F091's collector is. No branch reads it.
- Guard the whole capture on `dropped_out is not None` so an untraced call pays nothing —
  same discipline as `retrieval_pipeline.py:843`'s `if tr.enabled` gate.

→ **verify:** `pytest tests/ -k "hybrid_search"` green; new test asserts byte-identical return
value with the sink supplied and omitted.

### Step 2 — `_search_episode_chunks` forwards it, and declares the vector-only skip

**File:** `nous/api/retrieval_pipeline.py:2140`

Add `dropped_out: list | None = None`; forward on the hybrid branch only.

The vector-only branch must **report why it captured nothing** rather than returning an empty
list that reads as "nothing was dropped". It cannot set a leg summary itself (it has no
trace handle, and threading one in would give the helper a second reason to exist), so it
signals the caller the same way it already signals `attempted`: append a sentinel the caller
translates into `tr.leg("chunk", skip_reason=…)`.

Simplest shape that does not grow the helper's contract: the caller knows
`chunk_hybrid_search_enabled` — it is the same `settings` object — so the caller sets the
`skip_reason` directly and the helper stays a pure forwarder. Chosen: **caller-side**, no
sentinel.

```
skip_reason = "vector-only path: cut pushed into SQL, no in-memory surplus"
```

→ **verify:** flag off → `dropped_out == []` **and** the leg carries the skip_reason.

### Step 3 — Stage 1.5 collects; assembly registers

**Files:** `nous/api/retrieval_pipeline.py` — `_PipelineAccumulator`, `_run_stages:889`,
`run_recall_pipeline:~736`

1. New accumulator field:
   ```python
   # F091/C1: chunk-leg RRF discard set, shape
   # (id, vector_rank|None, keyword_rank|None, best_leg_score, stage).
   # Buffered here rather than registered at Stage 1.5 because
   # RetrievalTrace.add is first-wins against a SHARED max_candidates
   # budget — see plan R2.
   chunk_dropped: list = field(default_factory=list)
   ```
2. Stage 1.5 passes `dropped_out=acc.chunk_dropped if tr.enabled else None`, gated exactly
   like `:843`.
3. Stage 1.5 sets the vector-only `skip_reason` when `chunk_hybrid_search_enabled` is off.
4. Immediately before `tr.finalize` (`:737`):
   ```python
   for _cid, _vrank, _krank, _score, _stage in acc.chunk_dropped:
       tr.add(_cid, "chunk", "chunk", score=_score, rank=_vrank)
       tr.drop(_cid, "chunk", _DISPOSITION_FOR[_stage], f"chunk_{_stage}")
   ```
   `rank=_vrank` (vector rank) is the entry rank — it is the rank the operator reasons in,
   and it is what makes "rank 34 in the vector leg, absent from keyword" actionable.

→ **verify:** served chunk ids byte-identical with capture on and off; every captured id has
exactly one disposition at the right stage.

### Step 4 — tests

`tests/test_retrieval_pipeline.py` and/or `tests/heart/test_search.py`, matching whichever
file already covers the touched function.

| # | Test | Asserts |
|---|---|---|
| 1 | **Served set unchanged** (blocking gate) | `_search_episode_chunks` returns byte-identical `(id, content, score, episode_id)` with `dropped_out` supplied and omitted, over a fixture with **more than `limit_expanded`** matching chunks |
| 2 | **Discard set is exactly the complement** | `{captured} == (vector ∪ keyword) − served`; every captured id gets exactly one `sliced_off@chunk_rrf_merge` |
| 3 | **Hybrid off is declared, not silent** | flag off → discard list empty **AND** leg carries the skip_reason — assert the skip_reason, not just the emptiness |
| 4 | **Keyword-only exit is covered** (R1) | `embedding=None` with > `limit` keyword hits → complement captured at `chunk_keyword_only_limit`, not silently empty |
| 5 | **`require_keyword_hit` attribution** (R3) | filter casualties get `filter_dropped@…keyword_filter`, slice casualties `sliced_off@…rrf_merge` |
| 6 | **Cap ordering** (R2) | with `max_candidates` set just above the served count, served chunks are all present and the discards are what truncates — never the reverse |

→ **verify:** `uv run pytest tests/ -k "chunk or hybrid_search or retrieval_trace" -v`.
CI (Postgres) is the gate; the local SQLite suite runs ~230 red on `main` and is not evidence.

### Step 5 — free acceptance gate on an eval clone (schema ≥ 070)

Replay one CR question with `NOUS_RETRIEVAL_TELEMETRY_CANDIDATE_SAMPLE_RATE=1.0` (R4) and
assert `n_candidates` rises by the discard count while `n_rendered` is unchanged.

### Step 6 — docs

`CLAUDE.md`: the F091 telemetry rows already document the master flag and the sample rate.
Add nothing new (no flag, no setting). One sentence on the new `chunk_rrf_merge` /
`chunk_keyword_only_limit` stages goes in the `NOUS_RETRIEVAL_TELEMETRY_ENABLED` row.

---

## 4. Falsifiable prediction (carried from the spec, unchanged)

Over the 130 gold-labelled CR questions, the ~60 current misses should split roughly
**~16 into `dropped_at:chunk_rrf_merge`** and **~26 remaining `never_retrieved`**, with the
balance already attributed elsewhere. A materially different split means one of us has the
mechanism wrong, and **that is the finding** — not a reason to retune the instrument.

## 5. Non-claims (carried from the spec, unchanged)

- Does **not** propose raising any allotment. R2's +0.1231 was measured on a chunk-only store;
  the same knob measured net-negative on prod (#579). Store composition decides that knob.
- Does **not** improve retrieval. Not one served byte changes.
- Does **not** resolve R3-unreachability. Golds outside the `limit_expanded` window stay
  `never_retrieved`, correctly — the residual becomes a *measured* number instead of a bucket
  contaminated by merge losses.
- **No claim about prod diagnostic yield.** Prod's chunk leg has never been measured against
  ground-truth gold, only against a cosine oracle defined by the retriever's own similarity
  function.
- The vector-only path gains nothing and is expected to gain nothing. Giving it this
  visibility needs an overscan query — a different, non-free change.

## 6. Sizing

`search.py` +~25 (three exits, not one), `_search_episode_chunks` +~4, accumulator +~6,
Stage 1.5 +~6, assembly +~6, six tests. No migration, no config, no flag.

---

## 7. Plan-review findings (team review, 2026-08-22)

Reviewed against `fd10fa6`, code-only. **Three are live defects in §3 as written above**;
§3 is amended by this section, which takes precedence where they disagree.

### F1 (live) — the `skip_reason` call fabricates `attempted=True`

`RetrievalTrace.leg()` defaults `attempted=True` (`retrieval_trace.py:288`) and ORs it
**stickily** (`:302`). Step 3 item 3 as written creates the Leg with `attempted=True`. But the
vector-only branch returns `[]` at `retrieval_pipeline.py:2218-2222` **without** touching
`attempted` — the helper's docstring (`:2150-2156`) exists for exactly this: *"Marking at the
call site would report the leg as attempted-and-silent on a run where it never queried at
all."* The plan reintroduces the bug that comment prevents.

**Fix:** pass `attempted=False` explicitly. The loop at `:717-724` ORs it back to True on runs
where the helper *did* query, so both cases end correct:

| case | helper | leg reads |
|---|---|---|
| hybrid off, embedder present | queries, marks `attempted` (`:2225-2226`) | `attempted=True` — skip_reason set but **not rendered**, see R7 |
| hybrid off, no embedder | returns `[]` at `:2219`, no mark | `attempted=False` → dashboard shows "skipped" + reason ✅ |

### F2 (live) — an exception after `hybrid_search` returns makes the drain blame the merge for a DB error

`dropped_out` is populated by reference **inside** `hybrid_search`; the content fetch at
`:2207-2210` runs after it, still inside Stage 1.5's `try` (`:888-922`). If the fetch raises,
`acc.chunk_results` is `[]` but `acc.chunk_dropped` already holds ~150 entries — and the drain
is unconditional. The error is filed under a *different leg name*
(`stage_errors["chunk_recall"]` → `tr.leg("chunk_recall", error=…)` at `:735`) while the
candidates carry `entry_leg="chunk"`.

**Failure:** the row reads "leg chunk: 0 returned, ~150 candidates, all
`sliced_off@chunk_rrf_merge`". An operator triaging "why did no chunks reach the model" reads
the disposition histogram and raises `NOUS_EPISODE_CHUNK_RECALL_LIMIT`. The actual cause was a
failed content fetch. This is the *bounds-must-not-fabricate-signal* class: the instrument
degrades into a confident wrong reading instead of less information.

**Fix:** clear `acc.chunk_dropped` in the Stage 1.5 `except`.

### F3 (live) — `rank=_vrank` is `None` for **every** row R1 was added to capture

`vector_results` is `[]` by construction on exit 2 — the vector query sits inside
`if embedding is not None` (`search.py:300`). So every discard captured at
`chunk_keyword_only_limit` lands with `entry_rank=None`, and the keyword rank, which *is*
known, is thrown away. R1's stated purpose is that this branch not read as "nothing was
dropped"; as written it reads as *dropped with no position* — unusable for the
"was the window too small" question.

**Fix:** `rank = _vrank if _vrank is not None else _krank`; document the ambiguity in the
drain comment (the tuple retains both, so the detail row is not lossy).

### F4 (spec correction, latent) — R3's own remedy names a gate that never fires

Under `require_keyword_hit`, `merge_limit = max(limit, len(vector) + len(keyword))`
(`search.py:347-351`) is `>= |vector ∪ keyword|`, so `_rrf_merge` returns **everything** and
performs **no slice at all**. The truncation is `merged[:limit]` at `:374` — *after* the
keyword filter, on the filtered list. R3 above prescribes "slice casualties under the merge
stage", naming a gate that did not fire, and §3 Step 1's table row "merge slice — always
(hybrid) — `rrf_merge`" is false in that branch.

**Fix:** in that branch only, two stages and no merge stage:
`merged − filtered` → `filter_dropped@…keyword_filter`; `filtered − returned` →
`sliced_off@…keyword_filter_limit`.

### F5 (interpretation; bears on §4) — reported ranks past the pin sit in an exact tie

When `penalty_limit` is set, `search.py:357-362` passes `cap_ranks_at_penalty=True` and
`_rrf_merge:187-189` clamps observed ranks to `penalty_rank`. At the config R5 names as
validated prod (`chunk_rrf_penalty_limit=20`, `NOUS_RRF_K=30`): 0-indexed vector rank 20
scores `0.7/50 + 0.3/51 ≈ 0.019882`, while ranks **21 through 59 all score `1/51 ≈ 0.019608`
— exactly identical**. Their relative order then comes from iterating `all_ids`, a `set`
(`:179`), under a stable sort on equal keys.

So "chunk X, vector rank 47, `sliced_off@chunk_rrf_merge`" invites the inference *raise the
allotment 20→25 and ranks 21-24 come in* — which is wrong: those slots go to five arbitrary
members of a ~39-way tie. §5 correctly declines to propose raising the allotment; the
instrument as designed will lead operators to propose it anyway. Note the instrument behaves
**differently across the two configs R5 contrasts** — with `chunk_rrf_penalty_limit` unset
(eval), `cap_ranks_at_penalty` defaults `False` (`:145`, `:364`) and no tie band exists.

**Fix:** docstring + a caveat on §4's prediction. No code change.

### F6 — `_DISPOSITION_FOR[_stage]` is an unguarded index in the hot path

The drain sits between assembly and `tr.finalize` (`:737`), outside any try/except. A stage
name added in `search.py` without a map entry raises `KeyError` and kills the whole
`recall_deep` call — inverting F091's own invariant (`retrieval_trace.py:349-353`:
*"Telemetry must never break the thing it observes"*).

**Fix:** `.get(_stage, UNACCOUNTED)` + WARN — `UNACCOUNTED` is the honest value here, it means
exactly "no site claimed this" — **plus** a test asserting map completeness against the stage
constants, so the drift is caught at build rather than absorbed at runtime.

### F7 — the identity is `− merged`, not `− served`

`retrieval_pipeline.py:2212-2216` filters `if cid in by_id` from a *separate* fetch
(`:2207-2210`). `hybrid_search` computes its complement against what **it** returns, so a row
in `merged` but absent from `by_id` is in neither the discard capture nor `acc.chunk_results`
— never `tr.add`ed at all. Reachability is very low (the fetch carries no `agent_id`/`active`
predicate so it cannot drop on filtering, and no hard-delete path for episodes or chunks
exists in `nous/`; only a concurrent cascade delete could do it, as `heart.db.session()` gives
no cross-statement snapshot isolation) — but it is the one drop on this path with **no
representation at all**.

**Fix:** state the identity as `(vector ∪ keyword) − merged` in Test 2, and give the fetch
miss `filter_dropped@chunk_content_fetch_miss` rather than leaving it invisible.

### F8 — forbid threading the sink through `hybrid_search_multi`

Not reachable today (verified: `:2188` calls `hybrid_search` directly; multi is called only
from `episodes.py:479`, `facts.py:3256`, `procedures.py:401`). If a future caller threads it,
`search.py:550-563` fans the **same list** across N per-variant calls, so an id dropped in
variant 1 but served after `_rrf_merge_n` fusion gets `tr.drop`ed then overridden by
`finalize` (`retrieval_trace.py:528-531`) into `RENDERED` with a `restored_from` naming a
rescue that never happened. `_rrf_merge_n`'s own slice (`:568-573`) is invisible to the sink
entirely. **Fix:** one docstring sentence — per-variant identity required, or do not forward.

### Amendment to Test 2 (from `finalize`'s override semantics)

`finalize` (`retrieval_trace.py:528-531`) overrides ANY prior disposition to `RENDERED`,
preserving the old gate on `restored_from`. With `NOUS_HEART_GRAPH_ALL_TYPES_ENABLED` on,
Stage 2b can surface a chunk the merge discarded — correct accounting, but it breaks
"every captured id ends `sliced_off@chunk_rrf_merge`".

**Fix:** assert the drop was **recorded** — `disposition == sliced_off` **OR**
`restored_from` names the merge stage — and pin Path A off in the fixture, documenting why.
Check the MAB eval config for Path A before interpreting §4's ~16/~26 split.

### Verified-correct plan claims (no action)

R1's premise (`:2182-2183`), R2's placement (`heart_primary` is the only leg registering
inside `_run_stages`; all others register at assembly `:428-472`, `:492`, `:547`, `:621`),
R5's arithmetic (`:284`, `:289`), R6, the 150-row sizing against `_DEFAULT_MAX_CANDIDATES=300`
(`retrieval_trace.py:67`), the 1-based rank convention (matches `retrieval_trace.py:379` and
`retrieval_pipeline.py:423`), and the complement identity under `penalty_limit is not None`
(`limit=penalty_limit` reaches only `penalty_rank` at `:173`; the slice uses `return_limit` at
`:202`).

One caveat on the rank convention: 1-based is right, but the reported rank is **off by one
relative to the penalty comparison** — in reported terms, "scores no better than absence" is
`entry_rank >= penalty_limit + 2`. Document it, or an operator comparing a reported 21 to a
pin of 20 concludes parity when the document was still strictly better than absence.

---

## 8. Test-design findings — §3 Step 4 is REPLACED by this section

### T1 (blocking) — test 6 as specified cannot fail

`finalize` (`retrieval_trace.py:507-527`) force-creates a `Candidate` for any item in
`results` missing from `_candidates`, **deliberately bypassing the cap**, and marks it
`RENDERED`. So under the WRONG implementation (discards registered at Stage 1.5, ahead of the
survivors) every served chunk is still "present, `rendered`" — the assertion passes in both
arms. `truncated` is `True` in both arms too, so it is not a discriminator either.

**The only discriminator is provenance.** Correct impl: served chunks are registered by
`_tr_entries("chunk", _m)` at `:431-433`, before the drain, so they carry
`entry_leg == "chunk"` and a non-empty `snippet` (from `r.description`, `:423-424`). Wrong
impl: `entry_leg == "(unrecorded — capture cap reached)"`, `snippet == ""`.

**Test 6 becomes:** every served chunk has `entry_leg == "chunk"` AND `snippet != ""`; the
discards are the ones absent. This also makes R6 testable rather than merely "noted".

### T2 (blocking framing) — the recall_deep snapshot is NOT a safety net for this change

`tests/test_retrieval_pipeline.py:1252-1295`
(`TestFormatPipelineTextSnapshot::test_format_matches_committed_snapshot`, against
`tests/fixtures/recall_deep_text_snapshot.txt`) will not trip on this change — **and would not
trip on a broken implementation either**, for two independent reasons:

- `_make_settings()` (`:207-237`) defaults `episode_chunks_enabled=False` (`:218`), and Stage
  1.5 is gated on it at `retrieval_pipeline.py:881` — the chunk leg never runs.
- The snapshot passes no `trace=`, so `tr = NULL_TRACE` (`:398`) and every capture site is a
  no-op.

§3's "byte-identical" verify lines are true but **vacuous for this change**. The real blocking
gate is **test 1**, which must exercise the leg with `chunk_hybrid_search_enabled=True`.

### T3 — test 2 is mutation-weak; assert tuples, not an id set

`merged` contains exactly `vector ∪ keyword` and `served = merged[:limit]`, so `merged[limit:]`
*is* the complement — set-equality is near-tautological. Wrong implementations that pass it:
0-based ranks or vector/keyword swapped (which breaks R2's whole payoff sentence);
`best_leg_score` as `min` or vector-only; duplicate sink entries (asserting through
`tr.to_dict()` hides them — `add`/`drop` are first-wins at `:338-348`/`:402`); and a fixture
whose two legs return the same ids.

**Fixture must contain** vector-only ids, keyword-only ids, and overlap ids **at differing
per-leg ranks**, each leg longer than `limit`. **Assert** the exact 5-tuple
`(id, vrank, krank, score, stage)` for one representative of each class, against the **raw
`dropped_out` list** (not the trace), plus `len(dropped_out) == len(complement)`.

### T4 — test placement: the axis is mock-vs-fake-session

Half the existing chunk coverage monkeypatches `hybrid_search` **itself** and therefore can
never exercise the capture: `tests/test_r2_chunk_hybrid.py:104`/`:138`/`:153`, and
`tests/test_rrf_search.py:238`/`:264` (patches `_rrf_merge`). The patterns that run the real
body are session-fakes — `_heart_shim` (`test_r2_chunk_hybrid.py:29-46`, dispatches on
`str(sql)`) and the inline `_Sess` (`test_rrf_search.py:353-357`, `:390-393`).

| test | file | reuse |
|---|---|---|
| 1, 2 | `tests/test_r2_chunk_hybrid.py` | `_heart_shim` + `rows_by_query`, branching on `"ts_rank"` / `"ORDER BY t.embedding"` |
| 4 | `tests/test_rrf_search.py` | `_Sess`; `embedding=None` hits `search.py:337-339` |
| 5 | `tests/test_rrf_search.py` | helper level only — `require_keyword_hit` has no chunk caller, so a chunk-level test would be fiction |
| 3, 6 | `tests/test_retrieval_pipeline.py` | `_make_settings` (`:207`) must grow `chunk_hybrid_search_enabled` / `chunk_rrf_penalty_limit` kwargs — its fixed `SimpleNamespace` (`:223-237`) lacks both |

Real-SQL chunk coverage exists only in `tests/integration/test_r2_chunk_hybrid_e2e.py` and
`test_f067_chunks_e2e.py`, both `skipif(not _eval_db_reachable())` on **127.0.0.1:5433** —
skipped in CI, which runs Postgres on 5432.

### T5 — test 1's ">90 chunks" premise is wrong; the fixture is cheap

`limit` and `penalty_limit` are **test parameters**. At `limit=5, penalty_limit=None`,
`limit_expanded = 15` — 20 canned rows is a complete fixture, no embeddings, no tsvector, no
Postgres. The eval config's 90 is an operating point, not a test requirement. ~30 lines.

One real-DB variant is still worth adding (test 2's complement is only *true* against the real
SQL windows): the CI `db` fixture (`tests/conftest.py:101-125`) works, and
`heart.episode_chunks.search_tsv` is `GENERATED ALWAYS AS` with a GIN index
(`sql/migrations/050_*.sql:23-24`, `:43`), so a plain `INSERT ... (content)` lights the
keyword leg. Mark it `@pytest.mark.postgres_only` so it skips cleanly on the SQLite default
(`conftest.py:51-54`) instead of erroring.

### T6 — test 5 is a MANUAL pre-merge gate, not CI

`.github/workflows/ci.yml`: on PR → `main`, `lint` is `continue-on-error: true` (`:30`/`:34`,
advisory), `test` runs `pytest tests/ -v --tb=short` (`:89`) against `pgvector/pgvector:pg17`
on **5432** (`:42-55`) with `init.sql` + `seed.sql` + all `sql/migrations/*.sql` applied
(`:73-79`), 15-minute timeout. So schema ≥ 070 **is** satisfied in CI and a Postgres-backed
unit test is viable — but the eval clone on 5433 is never started, and `--integration` is
never passed (`conftest.py:41-48` skips those items).

Record test 5 in the **PR body** as a manual gate with its command and both env vars
(`NOUS_RETRIEVAL_TELEMETRY_CANDIDATE_SAMPLE_RATE=1.0` per R4, plus the `max_candidates` raise
or a `truncated is False` pre-assert). §3 Step 5 currently reads like automation.

### T7 — the seam for test 6, and why it must bypass the logger

`RetrievalTrace.__init__` takes `max_candidates` and `capture_candidates` directly
(`retrieval_trace.py:244-256`), and `run_recall_pipeline(trace=…)` (`:362`) accepts any object.
Construct `RetrievalTrace(max_candidates=N, capture_candidates=True)` and pass it in — no
Settings, no logger. Going through `RetrievalLogger.start` (`tools.py:1406-1414`) would hit
`capture_candidates = random.random() < self._sample_rate` (`retrieval_logger.py:74`) at the
0.1 default and be flaky ~90% of the time. R4 names this hazard for the eval replay; it
applies to the unit test too.

### T8 (side finding) — `tests/sqlite_patches.py` is dead code that would break on revival

Zero importers repo-wide, so `sqlite_hybrid_search` (`:24`) never replaces the real one. Its
signature also lacks `active_filter`, `penalty_limit` and `require_keyword_hit`, so adding
`dropped_out` would `TypeError` there too if anyone revived it. Fake-session tests sidestep it
and run green on both backends. **Mention, do not fix** — pre-existing, out of scope.

### Corroboration

ReviewA (F7) and ReviewC independently found the `if cid in by_id` filter at
`retrieval_pipeline.py:2212-2216` as an unnamed UNACCOUNTED source. Two independent
derivations — treat as established.

---

## 9. Invariants + cost findings, and a correction to R5

### C1 (blocking) — R5 was WRONG about the live config, and the cap must move

Verified directly, not from CLAUDE.md: **`.env:240-241` and `.env.prod-snapshot:233-234`
both set `NOUS_CHUNK_HYBRID_SEARCH_ENABLED=true` and `NOUS_EPISODE_CHUNK_RECALL_LIMIT=30`,
and NEITHER sets `NOUS_CHUNK_RRF_PENALTY_LIMIT`.**

R5 above asserted 20/20 was "the validated prod config". That is a CLAUDE.md
*recommendation* from #580/#581 that **nobody applied** — I read advice as deployment. So:

- live is `fetch_base = 30` → `limit_expanded = 90` → **up to 150 discards per recall_deep,
  in prod**, not the 90 R5 reasoned from. R5 understated the volume by ~1.5×, which is
  exactly the margin that decides whether the cap holds.
- a second consequence, in the other direction: with `chunk_rrf_penalty_limit` unset,
  `cap_ranks_at_penalty` defaults **False** — so **ReviewA's F5 tie band does not exist in
  the live config.** F5 is real but *conditional on adopting the recommended pin*. Restated
  as such; it is a caveat for whoever applies the pin, not a live property.

**The cap must move.** `retrieval_telemetry_max_candidates=300` against ~110-150 existing
registrations plus 150 chunk discards makes `_truncated` a steady state rather than an
anomaly signal: the dashboard headline flips permanently to "candidates recorded", the cap
banner becomes wallpaper, and a gold chunk cut at the merge but past the cap is recorded
nowhere — so it *still* reads `never_retrieved`, the exact miscount C1 exists to fix.

**Resolution: raise to 600 and say so. §1/§6's "no new config" claim is retracted.** Quietly
requiring a config change while advertising none is the worse failure.

### C2 — split the question: `Leg.n_dropped` (exact, always) vs the candidate array (sampled)

"How many did the merge cut?" is one integer an operator wants on **every** row. "Which ones,
and was the gold among them?" is an eval question worth an array on a **sample**. Answering
the first only through the second leaves it unanswerable on ~90% of retrievals and
*unanswerable-but-plausible* whenever the cap truncates.

`Leg` is JSONB, so `n_dropped` costs no migration and is always-on. This is what makes a
truncated capture honest rather than lossy — the array degrades to a prefix, the count stays
true, and an absence in the array is never mistaken for an absence in the retrieval. (Prior
art: decision `c4a78805` — *a sighting is evidence on its own; an absence is only evidence
when the record is provably complete*.)

### C3 — the false "rescued" badge is LIVE, not latent

ReviewB filed this as latent on the grounds that `heart_graph_all_types_enabled` "defaults
off and is unset in both env files". **Both env files set it to `true`** (`.env:204`,
`.env.prod-snapshot:204`). So Stage 2b really does surface chunk-type neighbours today, and a
chunk the merge cut can reach the model by another road. Dropping it in the drain makes
`finalize` (`retrieval_trace.py:528-531`) override to `rendered` with
`restored_from="sliced_off@chunk_rrf_merge"` — a rescue badge naming a leg the item never
came back through.

**Fix:** the drain skips ids already present in `results`. The cut still happened and
`n_dropped` still counts it; only the misattribution is suppressed.

### C4 — verified with no action needed

`finalize` iterates `results`, never `_candidates`, so late-registered discards get no
`final_rank`, no override, and are not counted rendered (Q2 ✅). No aliasing: all three exits
return fresh lists and the capture only reads (Q6 ✅). The conservation law
(`sum(disposition_counts) == n_candidates`) is structural and cannot be broken by volume.
Nothing divides by `n_candidates`. Empty snippets store as `""`; ~300 raw bytes per discard
dict, TOAST-compresses well; the time-based retention sweep needs no resizing.

### C5 — rollup contamination: TWO of the three sources are permanent

The window rollup derives every percentage from `totals.sum` over
`disposition_totals`. Three things now mix populations in that denominator, and only one of
them washes out:

- **(a) permanent, and the sharpest.** Stage 1.5 is gated on
  `search_all or "fact" in search_types` (`retrieval_pipeline.py:881-883`), so a
  `memory_types=["decision"]` or `["episode"]` recall never runs the chunk leg and contributes
  **zero** discards — sitting in the same rollup as full-scope rows carrying +150 each. This
  is structural; it does not decay.
- **(b) permanent.** The default empty path filter (`Retrieval.svelte:17`) mixes `context`-path
  rows, which have no chunk leg at all, into the same denominator.
- **(c) transitional, 14 days.** Retention straddles the deploy, so the window briefly holds
  pre-C1 and post-C1 rows together.

Concrete failure: an operator opens `/dashboard/retrieval` the morning after deploy, sees the
rendered share fall from ~10% to ~4%, and reads an instrumentation artifact as a retrieval
regression — then keeps reading a distorted number **indefinitely** under (a)/(b).

An earlier draft of this section listed only (c) and called the whole thing transitional. That
was wrong in the way that matters: a caveat that says "this clears up in two weeks" when it
does not is worse than no caveat. Stated in the PR body and the release note; a real fix needs
a per-row "chunk capture present" marker, which a no-migration change cannot carry.

### C5b — detail view density (deferred, with reasoning)

`sliced_off` carries `order: 1` and renders `open` by default, so a sampled row now opens on a
~180-row table of empty-content chunk rows, pushing `below_floor`, `filter_dropped`,
`deduped`, `f071_excluded` and — worst — the `unaccounted` drift alarm (`order: 10`) several
screens down. Second-order: the within-group sort mixes RRF-normalized heart scores with
`best_leg_score`, which this plan itself declares non-comparable, so ordering inside the group
that matters becomes meaningless.

This is a regression **caused by this change**, so it is owed a fix: per-`disposition_stage`
counts in the `<summary>` (so "182 — 150 chunk_rrf_merge, 32 heart_recall_limit" is readable
without expanding) plus a row-count collapse threshold. Deferred to a follow-up rather than
bundled, because it is a dashboard-behaviour change and this PR's contract is telemetry-only —
but it is tracked, not dropped.

### C6 — capture gated on `capturing`, not `enabled`

Every real trace is `enabled`; capture is *sampled*. Gating the complement build on `enabled`
would construct ~180 set entries and up to 150 tuples on the ~90% of retrievals where `add`
discards all of them. Added a `capturing` property to `RetrievalTrace` **and** `NullTrace`
(mirroring how `enabled` is already mirrored, and for the same reason). Leg counters
deliberately ignore it.

---

## 10. What shipped

| File | Change |
|---|---|
| `nous/heart/search.py` | `dropped_out` sink + `_record_dropped` helper wired at all **three** exits; `HYBRID_DROP_STAGES` exported for map-completeness assertion |
| `nous/observability/retrieval_trace.py` | `Leg.n_dropped`; `leg(n_dropped=…)`; `capturing` property on both `RetrievalTrace` and `NullTrace` |
| `nous/api/retrieval_pipeline.py` | `_CHUNK_DROP_DISPOSITIONS` map (read via `.get`, never `[]`); `acc.chunk_dropped` buffer; Stage 1.5 forwards + declares the vector-only skip with `attempted=False` + clears the buffer on exception; drain before `finalize` skipping already-served ids |
| `nous/config.py` | `retrieval_telemetry_max_candidates` 300 → 600, with the reasoning inline |
| `nous/api/dashboard_queries.py` | `dropped` in the per-leg window rollup |
| `dashboard-app/.../Retrieval.svelte` | Dropped column; **`skip_reason` now renders for an attempted leg** (R7 — it was previously persisted and never shown) |
| `dashboard-app/.../api.ts` | `n_dropped` on `RetrievalLeg` |
| `tests/test_c1_chunk_drop_capture.py` | 17 tests, all mutation-proven |

**Mutation-proof:** 8 deliberate defects introduced one at a time, each caught —
0-based ranks, `min` instead of `max` for `best_leg_score`, keyword-only exit uncaptured,
double-counting under `require_keyword_hit`, swapped legs, missing exception-clear, missing
already-served skip, `n_dropped` gated on sampling.

**Regression attribution:** clean `main` and this branch both show **48 failed / 15 errors**
on the affected sweep; passes go 529 → 546 (the 17 new tests). The 3 `test_rrf_search`
failures are developer-`.env` leakage (`NOUS_RRF_K=30` vs an asserted default of 60),
pre-existing and out of scope. CI Postgres is the gate.
