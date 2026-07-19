# R3 v2 Bounded Iterative Keyed Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one bounded, deterministic, zero-LLM second round to the F085 keyed leg (MAB R3v2 spec): round-1 hits' entity keys (minus the query's round-1 keys) fetch a capped candidate set, ranked by attribute-key overlap → content overlap → recency, top-K2 merged in a score band strictly below round-1, `retrieval_leg='keyed_r2'` provenance, telemetry surfaced in a log line. Land-dark: `NOUS_KEYED_FACT_LEG_ROUNDS` default 1 = byte-identical.

**Architecture:** Round 2 lives entirely inside Stage 1.6 (`retrieval_pipeline.py:506-526`) + two `FactManager` additions. Key derivation prefers the round-1 hits' own `fact_entity_keys` rows (new `entity_keys_for_facts`), supplemented by vocab-matched content scanning (`extract_entity_candidates` over round-1 contents) up to the key cap — covering both sentences of the spec's §2.2 so the shipped policy matches the simulated one. The candidate fetch reuses `fetch_by_entity_keys` with two additive changes: `f.attribute_key`/`f.subject_key` join the SELECT (the ranking's primary signal — validation found it absent) and a `track: bool = True` param so round-2 bulk candidates are NOT access-tracked; only the K2 survivors get `track_access()` (round-10 invariant: tracking is for surfaced results). Ranking is pure Python and exactly the spec's policy. The round-2 band is DERIVED from round-1's actual floor — `keyed_fact_leg_score − 0.005·(keyed_fact_leg_k+1)` — so it stays below round-1 at any configured K (a fixed offset breaks when K is raised). Merge reuses the stable score-ordered insertion; id-dedup against all legs incl. round-1.

**Tech Stack:** Python 3.12, SQLAlchemy async raw `text()`, PostgreSQL, pytest `postgres_only`. Zero LLM calls anywhere in round 2.

## Global Constraints

- Branch `feat/r3v2-iterative-keyed` in worktree `E:\Projects\nous\.claude\worktrees\plan12-graph-seed-score` (off c346ad7). **Subagents cd there + verify branch first.**
- `NOUS_KEYED_FACT_LEG_ROUNDS=1` (default) is **byte-identical** to v1 output — pinned by test (mirror `test_flag_on_without_candidates_matches_flag_off` at test_keyed_fact_leg.py:144).
- **No LLM calls in round 2.** Everything deterministic and auditable; full determinism includes a final id tie-break in the ranking sort.
- **Sim-parity contract (review devil-P1 — THE acceptance question).** Three shipped-policy specifics may differ from what MAB's bounded simulation did, and each must be stated verbatim in the PR body + F085 doc with a REQUIRED gate-1 re-simulation before the ≈7M-token gate-3 replay: (1) round-2 KEY DERIVATION: entity rows of round-1 hits first (r1 rank order, alphabetical within a fact), then vocab-FILTERED content-scan keys (r1 rank order) — order matters because the 32-cap truncates; (2) only vocab members enter the key set (the raw extract_entity_candidates emits non-indexed quoted/TitleCase spans — filtered out); (3) RANKING exactly as specced (attribute-key word overlap → content word overlap → learned_at recency) + a final id tie-break for total determinism. Gate 1 is NOT "already green" for this implementation until MAB re-simulates the shipped policy.
- Round-2 candidates fetched WITHOUT access tracking; only K2 survivors tracked via existing `track_access(ids)` (facts.py:316-333).
- Fan-out guards with LOUD truncation (R1.3 convention): keys examined capped at `keyed_fact_leg_r2_max_keys` (32), fetched candidates hard-capped at `keyed_fact_leg_r2_max_candidates` (256); `keyed_r2_truncated` stat + the log line record truncation — no silent caps.
- Round-2 hits: `metadata["retrieval_leg"]="keyed_r2"` + the SAME subject/event_date/source_episode_id conventions as `_keyed_to_pipeline` (:1209-1257); band strictly below round-1 (derived base above); positional consequence: r2 items sit after r1 keyed items in the merged list.
- Telemetry surfaced (spec's v1 complaint): a `logger.info` inside the pipeline, gated on round-2 having run (fired OR truncated), carrying n_keyed / n_keyed_r2 / keys_examined / truncated; PLUS extend the existing consolidated recall_deep INFO line (tools.py:983-999) with `n_keyed_r2`/`keyed_r2_truncated` for grep parity.
- `PipelineStats` gains `n_keyed_r2: int = 0`, `keyed_r2_truncated: bool = False` (defaults construction-safe; only the all-kwargs site :384-401 wires them).
- Every `fact_entity_keys` read joins `heart.facts` on `active = true` and filters `ek.agent_id` (F085 invariant).
- Tests: postgres_only for DB paths; seed via `heart.db.session()` + commit + teardown (house pattern in test_keyed_fact_leg.py:40-140); local `brain` fixture exists in that file. CI is the merge gate.
- Commits `feat:`/`fix:`/`test:` + trailers `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` / `Claude-Session: https://claude.ai/code/session_01KdLp1gY3GQjNhtAFkJDkDz`.
- Acceptance (bounded-sim gate, displacement, CR replay, regression) runs externally in the MAB agent session — no evals here.

---

### Task 1: FactManager round-2 data methods

**Files:**
- Modify: `nous/heart/facts.py` (`fetch_by_entity_keys` :3692-3762; new `entity_keys_for_facts` beside it)
- Test: `tests/test_keyed_fact_leg.py` (append to TestKeyedFactLeg)

**Interfaces:**
- Produces: `fetch_by_entity_keys(keys, limit=8, *, track: bool = True)` — SELECT additionally returns `f.attribute_key, f.subject_key`; when `track=False` the inline access-tracking block (:3745-3762) is skipped entirely. Default-path callers unchanged (byte-identical results content-wise; two extra Row columns are additive).
- Produces: `entity_keys_for_facts(fact_ids: list[UUID]) -> dict[UUID, list[str]]` — agent-scoped, active-joined, keys sorted alphabetically per fact (determinism). Empty input → `{}`.

- [ ] **Step 1: Failing tests**

```python
    async def test_fetch_track_false_skips_access_tracking(self, heart, seed_keyed_corpus):
        # arch-P1: _get_fact does NOT exist in this file — use its own idiom
        # (heart.db.session + s.get(Fact, id), as at :189-192)
        gold = seed_keyed_corpus["gold_id"]
        async with heart.db.session() as s:
            before = (await s.get(Fact, gold)).recall_count
        rows = await heart.facts.fetch_by_entity_keys(["marriage of figaro"], limit=8, track=False)
        assert [r.id for r in rows] == [gold]
        async with heart.db.session() as s:
            after = (await s.get(Fact, gold)).recall_count
        assert after == before                                     # NOT tracked
        assert rows[0].attribute_key is not None or rows[0].attribute_key is None  # column present (no AttributeError)

    async def test_entity_keys_for_facts_groups_and_sorts(self, heart, seed_keyed_corpus):
        gold = seed_keyed_corpus["gold_id"]
        m = await heart.facts.entity_keys_for_facts([gold])
        assert m == {gold: ["marriage of figaro", "thomas kyd"]}   # alphabetical
        assert await heart.facts.entity_keys_for_facts([]) == {}

    async def test_entity_keys_for_facts_excludes_inactive(self, heart, seed_keyed_corpus):
        # the corpus's superseded (active=False) fact shares a key — must not appear
        sup = seed_keyed_corpus["superseded_id"]
        m = await heart.facts.entity_keys_for_facts([sup])
        assert m == {}
```

- [ ] **Step 2:** `NOUS_TEST_DB=postgres uv run pytest tests/test_keyed_fact_leg.py -k "track_false or entity_keys_for_facts" -v` → FAIL (TypeError: unexpected kwarg / AttributeError).

- [ ] **Step 3: Implement**

3a. `fetch_by_entity_keys`: add `f.attribute_key, f.subject_key` to the SELECT + GROUP BY; add keyword-only `track: bool = True`; wrap the existing tracking block in `if track and result_rows:` (content of the block unchanged). Docstring: one paragraph on why round-2 bulk candidates fetch untracked (only surfaced results accumulate recall signal — round-10 invariant; survivors are tracked separately via `track_access`).

3b. New method:

```python
    async def entity_keys_for_facts(self, fact_ids: list["UUID"]) -> dict["UUID", list[str]]:
        """R3v2: the fact_entity_keys rows of the given facts, grouped by fact,
        keys sorted alphabetically (deterministic round-2 key derivation).
        Active-joined + agent-scoped per the F085 read invariant."""
        if not fact_ids:
            return {}
        async with self.db.session() as session:
            rows = await session.execute(
                text(
                    "SELECT ek.fact_id, ek.entity_key "
                    "FROM heart.fact_entity_keys ek "
                    "JOIN heart.facts f ON f.id = ek.fact_id "
                    "WHERE ek.agent_id = :a AND f.agent_id = :a AND f.active = true "
                    "  AND ek.fact_id::text = ANY(:ids) "
                    "ORDER BY ek.fact_id, ek.entity_key"
                ),
                {"a": self.agent_id, "ids": [str(i) for i in fact_ids]},
            )
            out: dict = {}
            for r in rows:
                out.setdefault(r.fact_id, []).append(r.entity_key)
            return out
```

(`fact_id::text = ANY(:ids)` mirrors the facts.py:3449-3459 uuid-list precedent.)

- [ ] **Step 4:** Tests green (`NOUS_TEST_DB=postgres`, file + `tests/test_entity_keys.py` regression); SQLite skip-clean.
- [ ] **Step 5: Commit** `feat(heart): R3v2 data methods - untracked keyed fetch variant + entity_keys_for_facts`

---

### Task 2: Round 2 in Stage 1.6 — derivation, guards, ranking, band, telemetry

**Files:**
- Modify: `nous/api/retrieval_pipeline.py` (Stage 1.6 :506-526; `_PipelineAccumulator` :196-213; stats wiring :384-401; new `_rank_r2_candidates` + `_keyed_r2_to_pipeline` near `_keyed_to_pipeline` :1209; assembly :323)
- Modify: `nous/api/tools.py` (consolidated INFO line :983-999)
- Modify: `nous/config.py` (4 fields beside :315-326)
- Test: `tests/test_keyed_fact_leg.py` (new class TestKeyedR2)

**Interfaces:**
- Config: `keyed_fact_leg_rounds` (default 1, ge=1, le=2), `keyed_fact_leg_k2` (8, ge=1), `keyed_fact_leg_r2_max_keys` (32, ge=1), `keyed_fact_leg_r2_max_candidates` (256, ge=1).
- Accumulator: `keyed_r2_results: list`, `n_keyed_r2_dup: int`, `keyed_r2_truncated: bool`, `keyed_r2_ran: bool`.
- `_rank_r2_candidates(rows, query: str, k2: int) -> list[Row]` — pure function: query tokens = `set((normalize_key(query, max_len=1000) or "").split())`; per row `attr = len(qt & toks(row.attribute_key))`, `content = len(qt & toks(row.content))` where `toks(s)` = `set((normalize_key(s or "", max_len=1000) or "").split())`; sort key `(-attr, -content, -learned_at timestamp, str(id))`; return top k2.
- `_keyed_r2_to_pipeline(rows, settings, existing_ids) -> tuple[list[PipelineResult], int]` — identical metadata conventions to `_keyed_to_pipeline` but `retrieval_leg="keyed_r2"` and base = `keyed_fact_leg_score − 0.005·(keyed_fact_leg_k + 1)` (clamped ≥ 0), decay `− 0.005·rank`.

- [ ] **Step 1: Failing tests** (TestKeyedR2, postgres_only; seed helper `seed_hop_corpus` via heart.db.session+commit+teardown per the file's pattern):

```python
@pytest_asyncio.fixture
async def seed_hop_corpus(heart):
    """Two-hop shape: query mentions 'alpha station' -> r1 hit A (keys:
    {'alpha station','bridge person'}); B is reachable only via 'bridge person'
    (keys: {'bridge person','target city'}); C is a decoy reachable the same
    way but with no query-token overlap. A/B/C contents share no words with
    the query except as noted; no embeddings (Stage 1 blind)."""
    ...  # A: content "Facility records mention the liaison duties.", subject/attr per test
         # B: content "The station liaison Bridge Person relocated operations.",
         #    attribute_key="relocation city"    (overlaps query token 'city' if query asks it)
         # C: decoy, attribute_key="unrelated", content w/ no query tokens
         # + FactEntityKey rows as above; distinct learned_at values.

class TestKeyedR2:
    async def test_rounds_1_default_byte_identical(self, heart, brain, settings, seed_hop_corpus):
        q = 'Report about "Alpha Station" relocation city'
        s1 = settings.model_copy(update={"keyed_fact_leg_enabled": True})           # rounds default 1
        base_results, base_stats = await run_recall_pipeline(q, heart, brain, s1)
        again_results, _ = await run_recall_pipeline(q, heart, brain, s1)
        assert [(r.id, r.score, r.metadata) for r in base_results] == \
               [(r.id, r.score, r.metadata) for r in again_results]
        assert base_stats.n_keyed_r2 == 0 and base_stats.keyed_r2_truncated is False
        assert not any(r.metadata.get("retrieval_leg") == "keyed_r2" for r in base_results)

    async def test_two_hop_composition(self, heart, brain, settings, seed_hop_corpus):
        q = 'Report about "Alpha Station" relocation city'
        s2 = settings.model_copy(update={"keyed_fact_leg_enabled": True, "keyed_fact_leg_rounds": 2})
        results, stats = await run_recall_pipeline(q, heart, brain, s2)
        r1 = [r for r in results if r.metadata.get("retrieval_leg") == "keyed"]
        r2 = [r for r in results if r.metadata.get("retrieval_leg") == "keyed_r2"]
        assert seed_hop_corpus["A"] in {r.id for r in r1}
        assert seed_hop_corpus["B"] in {r.id for r in r2}          # the hop
        assert stats.n_keyed_r2 >= 1 and stats.keyed_leg_used
        # band: every r2 score strictly below every r1 keyed score; r2 after r1 positionally
        assert max(x.score for x in r2) < min(x.score for x in r1)
        assert min(results.index(x) for x in r2) > max(results.index(x) for x in r1)

    async def test_ranking_attribute_overlap_beats_content_and_decoy(self, heart, brain, settings, seed_hop_corpus):
        # B (attribute_key overlaps query) must outrank decoy C when both are candidates
        ...  # rounds=2, K2=1 -> only B survives; assert C absent, B present

    async def test_fanout_truncation_is_loud(self, heart, brain, settings, seed_hop_corpus, caplog):
        s = settings.model_copy(update={"keyed_fact_leg_enabled": True, "keyed_fact_leg_rounds": 2,
                                        "keyed_fact_leg_r2_max_keys": 1})
        with caplog.at_level("INFO"):
            _, stats = await run_recall_pipeline('..."Alpha Station"...', heart, brain, s)
        assert stats.keyed_r2_truncated is True
        assert any("keyed_r2" in rec.message for rec in caplog.records)   # surfaced telemetry

    async def test_r2_dedups_against_r1_and_survivors_only_tracked(self, heart, brain, settings, seed_hop_corpus):
        ...  # A (an r1 hit) also reachable via r2 keys -> appears ONCE (leg='keyed');
             # decoy C fetched as candidate but not selected -> C.recall_count unchanged;
             # B selected -> B.recall_count bumped by exactly 1
```

(Complete every `...` with real code at implementation time; the fixture's exact contents/keys must make each assertion deterministic — choose query/token overlaps accordingly and document them in the fixture docstring.)

- [ ] **Step 2:** Run → FAIL (unknown settings fields / no keyed_r2 results).

- [ ] **Step 3: Implement**

3a. config.py — 4 fields (style per :315-326):

```python
    keyed_fact_leg_rounds: int = Field(
        default=1, ge=1, le=2,
        description="R3v2: keyed-leg retrieval rounds. 1 = v1 behavior (byte-identical); 2 enables the bounded iterative round (multi-hop composition). Land-dark.",
    )
    keyed_fact_leg_k2: int = Field(
        default=8, ge=1,
        description="R3v2: round-2 allotment - max round-2 keyed facts merged per query.",
    )
    keyed_fact_leg_r2_max_keys: int = Field(
        default=32, ge=1,
        description="R3v2 fan-out guard: max round-2 keys examined (truncation is counted, never silent).",
    )
    keyed_fact_leg_r2_max_candidates: int = Field(
        default=256, ge=1,
        description="R3v2 fan-out guard: hard cap on round-2 candidates fetched before ranking (the p90-587 lesson).",
    )
```

3b. Stage 1.6 extension — inside the existing `try`, after round-1 `acc.keyed_results` lands:

```python
                rounds = getattr(settings, "keyed_fact_leg_rounds", 1)
                if rounds >= 2 and acc.keyed_results:
                    acc.keyed_r2_ran = True
                    r1_ids = [row.id for row in acc.keyed_results]
                    key_map = await heart.facts.entity_keys_for_facts(r1_ids)
                    r2_keys: list[str] = []
                    seen_k = set(candidates)              # MINUS round-1 query keys
                    # (1) exact + cheap: the round-1 hits' own entity rows, in
                    #     r1 rank order, alphabetical within a fact
                    for rid in r1_ids:
                        for k in key_map.get(rid, []):
                            if k not in seen_k:
                                seen_k.add(k); r2_keys.append(k)
                    # (2) spec 2.2 primary definition: vocab keys appearing in
                    #     round-1 fact CONTENTS (covers entities mentioned but
                    #     not indexed on the hit itself). CRITICAL (review
                    #     devil-P1a): extract_entity_candidates' quoted +
                    #     capitalized-span legs emit arbitrary NON-INDEXED
                    #     spans FIRST — unfiltered, they eat the 32-key cap
                    #     with keys that match nothing, silently dropping
                    #     coverage below the simulated 0.39. Only VOCAB
                    #     MEMBERS may enter the round-2 key set.
                    max_keys = getattr(settings, "keyed_fact_leg_r2_max_keys", 32)
                    for row in acc.keyed_results:
                        if len(r2_keys) >= max_keys:   # arch-P3: shared budget, stop early
                            break
                        for k in extract_entity_candidates(
                            row.content, vocab=vocab, max_candidates=max_keys,
                        ):
                            if k in vocab and k not in seen_k:   # devil-P1a vocab filter
                                seen_k.add(k); r2_keys.append(k)
                    if len(r2_keys) > max_keys:
                        acc.keyed_r2_truncated = True
                        r2_keys = r2_keys[:max_keys]
                    if r2_keys:
                        max_cand = getattr(settings, "keyed_fact_leg_r2_max_candidates", 256)
                        rows2 = await heart.facts.fetch_by_entity_keys(
                            r2_keys, limit=max_cand, track=False,
                        )
                        if len(rows2) >= max_cand:
                            acc.keyed_r2_truncated = True
                        r1_id_set = set(r1_ids)
                        rows2 = [r for r in rows2 if r.id not in r1_id_set]
                        survivors = _rank_r2_candidates(
                            rows2, query, getattr(settings, "keyed_fact_leg_k2", 8),
                        )
                        acc.keyed_r2_results = survivors
                        if survivors:
                            await heart.facts.track_access([r.id for r in survivors])
                    # R3v2: surfaced telemetry (v1's internal-only stats made
                    # live verification needlessly hard - rollout doc §3)
                    logger.info(
                        "keyed_r2: r1_hits=%d keys_examined=%d candidates=%d selected=%d truncated=%s",
                        len(acc.keyed_results), len(r2_keys),
                        len(rows2) if r2_keys else 0, len(acc.keyed_r2_results),
                        acc.keyed_r2_truncated,
                    )
```

(Adapt variable names to the real block; `vocab` and `candidates` are in scope from round 1. The log line fires only when round 2 RAN — it is inside the `rounds >= 2 and acc.keyed_results` branch.)

3c. `_rank_r2_candidates` (pure, near the converters): as specced in Interfaces — one shared `_fold_tokens(s)` helper using `normalize_key(s, max_len=1000)`; sort key `(-attr_overlap, -content_overlap, -learned_at.timestamp(), str(row.id))`. Docstring: THIS IS THE SIMULATED POLICY — any change requires MAB re-simulation (gate 1).

3d. `_keyed_r2_to_pipeline`: clone of `_keyed_to_pipeline` with `retrieval_leg="keyed_r2"` and `base = max(0.0, getattr(settings, "keyed_fact_leg_score", 0.55) - 0.005 * (getattr(settings, "keyed_fact_leg_k", 8) + 1))`; comment the derivation (config-proof band-below: one decay step under round-1's worst rank). Keep matched/subject/event_date/source_episode_id conventions identical (rows carry the same columns).

3e. Assembly (:323 area): after the round-1 keyed insertion, convert + insert r2 the same way:

```python
    existing_ids.update(r.id for r in keyed)   # arch-P2-2: single source of truth
    keyed_r2, acc.n_keyed_r2_dup = _keyed_r2_to_pipeline(acc.keyed_r2_results, settings, existing_ids)
    for kr in keyed_r2:
        pos = next((i for i, r in enumerate(results) if (r.score or 0.0) < kr.score), len(results))
        results.insert(pos, kr)
```

(RESOLVED, review arch-P2-2: `existing_ids` is frozen at :322 BEFORE the round-1 insertion loop, so it is stale w.r.t. r1 keyed ids — however Stage 1.6's `r1_id_set` filter already guarantees `acc.keyed_r2_results` contains no r1 id. The `existing_ids.update(...)` line above is added anyway for single-source-of-truth robustness so a future refactor of either filter can't silently break dedup. The `(r.score or 0.0)` idiom matches round-1's insertion loop at :326.)

3f. Stats wiring (:384-401): `n_keyed_r2=len(keyed_r2)`, `keyed_r2_truncated=acc.keyed_r2_truncated`. PipelineStats gains the two fields with defaults.

3g. tools.py consolidated INFO (:983-999): append `n_keyed_r2=%d keyed_r2_truncated=%s`.

- [ ] **Step 4:** All TestKeyedR2 + full `tests/test_keyed_fact_leg.py` + `tests/test_retrieval_pipeline.py` green (`NOUS_TEST_DB=postgres`); SQLite files skip-clean; recall_deep snapshot untouched (rounds default 1).
- [ ] **Step 5: Commit** `feat(retrieval): R3v2 bounded iterative keyed round - deterministic composition, land-dark`

---

### Task 3: Docs + gate

**Files:**
- Modify: `docs/features/F085-keyed-fact-selection.md` (new "R3 v2 — bounded iterative round (2026-07-19)" section), `CLAUDE.md` (4 env rows), `docs/features/INDEX.md` (extend the F085 row with an "R3v2 iterative round" clause)

- [ ] **Step 1:** F085 doc section: the measured chain (0.759 first above-noise arm, mh-driven; sim 0.02→0.44 round-2 ceiling, bounded 0.39@K2=8), the mechanism (trigger, key derivation incl. the entity-rows-then-content-scan order, fan-out guards, THE EXACT RANKING POLICY verbatim — sim-parity contract: changes require MAB re-simulation), band derivation, provenance/telemetry (log line format), flags, and the acceptance gates verbatim from the R3v2 spec. Documented deviations: PipelineOutcome→PipelineStats; attribute_key added to the fetch SELECT (spec assumed present); band derived from r1 floor (config-proof) rather than a fixed sub-band; round-2 key derivation covers BOTH spec sentences (entity rows preferred, content scan supplements).
- [ ] **Step 2:** CLAUDE.md env rows (match config descriptions); INDEX.md clause.
- [ ] **Step 3:** Gates: `NOUS_TEST_DB=postgres uv run pytest tests/test_keyed_fact_leg.py tests/test_entity_keys.py tests/test_retrieval_pipeline.py tests/test_write_path_adjudication.py -q` green; full SQLite `uv run pytest tests/ -q` failing-set matches the pre-branch baseline.
- [ ] **Step 4: Commit** `docs: R3v2 iterative keyed round - mechanism, sim-parity contract, env vars`

---

## Amendments after 2-agent team review (BINDING — folded into the tasks above)

Verdicts: v2rev-arch APPROVE-WITH-FIXES, v2rev-devil APPROVE-WITH-FIXES (opus). Provenance:

1. **[devil-P1a]** Round-2 content-scan keys are VOCAB-FILTERED (`k in vocab`) — the raw extractor's quoted/cap-span legs emit non-indexed spans first and would eat the 32-cap; plus a shared early-break budget across rows (arch-P3-5).
2. **[devil-P1b]** Sim-parity contract rewritten (Global Constraints): key-derivation ORDER + vocab filter + id tie-break all named; **gate-1 re-simulation of the shipped policy is REQUIRED** before the gate-3 replay — "already green" does not transfer to this implementation.
3. **[devil-P2]** Gate-2's "r2 never evicts r1" is a POOL-COMPOSITION/POSITIONAL guarantee (holds on the rerank=False path and in insertion order). Under rerank=True + recency-resolver-on, a dated superseded r1 fact can be ×0.3-downranked below the r2 band in FINAL ordering — that is resolver semantics acting on the fact, not r2 eviction; stated in the F085 doc section (Task 3).
4. **[arch-P1]** No `_get_fact` in the keyed test file — tests use the file's own `heart.db.session()` + `s.get(Fact, id)` idiom.
5. **[arch-P2-2]** `existing_ids` staleness resolved in plan text: `r1_id_set` already guarantees no r1 id reaches r2 results; `existing_ids.update(...)` added anyway for single-source-of-truth robustness.
6. **[arch-P2-3]** `entity_keys_for_facts` double-scopes (`ek.agent_id` AND `f.agent_id`) per the file's paranoid-scoping convention.
7. **[devil-P3-1]** Truncation flag is "possibly truncated" at the candidate cap (== LIMIT is indistinguishable); the log line's separate counts disambiguate — wording in doc + comment.
8. **[devil-P3-2]** The byte-identity invariant covers RESULTS (pinned by test + snapshot), NOT log text — the tools.py consolidated line legitimately gains two fields on every call.
9. **[devil-P3-3]** Band-derivation comment corrected: r2 base sits TWO decay steps under r1's worst rank (k−1 vs k+1) — deliberate safety margin; degenerate clamp at score≈0 noted as an accepted edge (operators setting keyed_fact_leg_score near 0 have disabled the leg in practice).
10. **[arch-P3-6]** Round-2 failures attribute to a distinct `stage_errors["keyed_r2"]` key (wrap the round-2 block in its own try/except inside the outer one).
11. **[arch-P3-7]** `TestKeyedR2` carries `@pytest.mark.postgres_only` explicitly.
12. **[devil-P3-4]** The two-hop fixture's overlap arithmetic must be computed by hand at implementation and documented in the fixture docstring; caplog capture verified viable (module logger propagates to root).

## Self-Review Notes (author)

- Spec §2.1 items 1-5 → Task 2 (trigger, key set w/ MINUS via `candidates`, guards+loud truncation, bounded selection exactly as simulated, provenance+surfaced telemetry). §2.2 flags → Task 2/3a. Non-goals respected (≤2 rounds enforced by `le=2`; zero LLM; no sh key-matching work).
- Gate 1's "re-simulate if the shipped ranking differs" → the ranking is implemented verbatim from the spec + documented verbatim in the F085 doc + PR body flags the one liberty taken (final id tie-break for full determinism — MAB should confirm their sim's tie handling or re-sim; everything else matches).
- Validation deltas honored: attribute_key/subject_key added to fetch SELECT; untracked bulk fetch + `track_access(survivors)`; `PipelineStats` naming; vocab reused (not re-fetched); `candidates` is the round-1 key set for the MINUS.
