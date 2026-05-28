# F075 Implementation Plan — Devil's Advocate Review

**Reviewer:** Devil's-advocate (adversarial)
**Plan under review:** `docs/superpowers/plans/2026-05-28-f075-temporal-fact-extraction.md`
**Spec referenced:** `docs/features/F075-temporal-fact-extraction.md` v2.17 (merged in PR #460 / `0115568`)
**Date:** 2026-05-28
**Mandate:** find the ways this plan WILL FAIL. Not approve.

---

## TL;DR — top three risks

1. **Critical: Phase 8 backfill is the single most likely cause of rework.** The spec absorbed ~10 codex rounds on it. The plan delegates with "trust the spec's pseudo-code" but leaves three load-bearing subtleties unstated: the `engine` vs `db.engine` access pattern, what `session_factory` actually is (`async_sessionmaker(engine)` from where?), and how `_classify_event_date` injects the tool_use schema. Unit tests won't catch any of these.
2. **High: Phase 0 gate is too narrow.** Two cases at $0.30 verifies PATTERN_MATCH on 2/2 sampled and PARTIAL_MATCH on 1/1. Sample size doesn't gate the bigger risk — that the live extractor's prompt addition shifts non-event fact yield on the rest of the summarizer's traffic. A passing Phase 0 says nothing about acceptance criterion #2 (no regression in non-temporal categories).
3. **High: Phase 12 PR scope is loose against a filthy working tree.** `git add -A` with "verify with `git diff --stat`" against the current `git status` (40+ untracked files including `f047_test.py`, `graph_*.json`, `identity_after*.json`, `evaluations/*.md`, `reports/baseline*`) is a coin flip. The plan's "only F075-related files" is aspirational, not enforced.

---

## Findings by severity

### Critical

#### C1. Phase 8 trusts the spec's pseudo-code without binding it to concrete imports

**Mechanism.** The spec's `_run_with_lock` (v2.14) takes `engine: AsyncEngine` and `session_factory`. The plan says "two-connection pattern" but doesn't say:

- Where does `engine` come from in the script entrypoint? `Database(Settings()).engine` is what the rest of the codebase uses, but the spec's snippet leaves it as a parameter.
- Is `session_factory` `Database._sessionmaker` (private) or a fresh `async_sessionmaker(engine, expire_on_commit=False)`?
- F047's `actionability_backfill.py` uses `db.session()` (the manager's helper). The plan's Phase 8 says "Two-connection pattern (`engine.connect()` for lock + `session_factory()` for batches)" — but if the implementer reads F047 as the template, they'll write the F047 single-session pattern that codex round-14 explicitly rejected.

**Failure mode.** Implementer copies F047, ships single-connection, lock leaks at every batch boundary. Unit tests pass because each test uses one batch.

**Fix.** Plan Phase 8 must cite the *exact* `engine.connect()` + `async_sessionmaker(engine)` construction and explicitly forbid the F047 single-session pattern.

#### C2. Phase 8 unit tests don't exercise the multi-batch lock-leak scenario

**Mechanism.** The plan's `tests/test_temporal_backfill.py` lists:
- `Concurrent invocations: second one sees lock held, exits cleanly` — tests *acquisition*, not lock-hold-across-batches.

There is no test that simulates: run batch 1, work session commits, then verify `pg_locks` still shows the advisory lock held by `lock_conn`. That's the exact regression codex round-13 → round-14 found.

**Failure mode.** Round-14 bug reintroduced silently. Symptom only appears in prod / smoke when an operator runs the script with N=2+ batches AND another process tries to acquire the lock mid-run.

**Fix.** Add a test: `test_lock_held_across_multiple_batch_commits` — insert >2*batch_size synthetic rows, run with batch_size=1, between batches assert via raw `SELECT * FROM pg_locks WHERE locktype='advisory'` that the lock is still held by the lock_conn pid.

#### C3. Phase 12 `git add -A` against a dirty tree is unsafe

**Mechanism.** Current `git status` shows ~40 untracked files at repo root and in `docs/`, `evaluations/`, `reports/`, `nous_eval/beam/`, `logs/`. `f047_test.py` and `graph_full.json` are at repo root. `git add -A` adds them all.

**Failure mode.** PR diff includes `identity_after.json` (potentially sensitive — agent identity snapshots), `logs/`, `.recovered/`, `f047_test.py` (presumably a one-off scratch). Reviewer asks "what is this?" → embarrassment + force-push to rewrite.

**Fix.** Replace `git add -A` with an explicit pathspec listing every F075-related file, e.g. `git add sql/migrations/053_*.sql nous/storage/models.py nous/heart/schemas.py nous/heart/facts.py nous/handlers/episode_summarizer.py nous/handlers/fact_extractor.py nous/handlers/temporal_backfill.py nous/api/retrieval_pipeline.py nous/brain/graph_densifier.py nous/config.py scripts/backfill_temporal_facts.py tests/test_temporal_*.py tests/test_migration_053.py tests/test_f075_end_to_end.py CLAUDE.md docs/features/F075-*.md docs/features/INDEX.md`. Anything else is forbidden.

### High

#### H1. Phase 0 gate is necessary but insufficient

**Mechanism.** Phase 0 verifies the *intervention works on 3 sampled cases*. It does NOT verify:
- The summarizer prompt addition doesn't *reduce* non-event fact yield elsewhere (acceptance criterion #2).
- Conv 4 Q1 and conv 5 Q1 dates can actually be sourced from BEAM chat. The plan literally says "the actual date from BEAM source needs to be looked up; if not present, use the question's date references." That's circular — the *question* already references dates; if the *chat* doesn't, the synthetic-fact injection is fictional and Phase 0 measures rubric-against-rubric, not retrieval-against-chat.

**Failure mode.** Phase 0 passes on three rigged cases. Implementer ships. BEAM re-measurement shows acceptance #5 missed because conv 1, 3, 4, 5 didn't actually have extractable dates in their transcripts — only in their rubric questions.

**Fix.** Phase 0 prerequisite: *read the BEAM source chat for conv 4 Q1 and conv 5 Q1 first*, confirm the date strings exist in the transcript text, only then run the synthetic verification.

#### H2. Phase 9 integration test → BEAM acceptance criterion #5 gap is large

**Mechanism.** Phase 9 uses a "synthetic conversation with 3 explicit dates" (the plan's exact wording). That's the easiest possible case. BEAM-100K conversations are 100K tokens, multi-day, with dates buried in tool output and code blocks. The summarizer's prompt addition has to extract from *that* corpus, not from a 3-message fixture.

**Failure mode.** Phase 9 passes. BEAM re-measurement shows temporal_reasoning ≤ 0.45 because the LLM happily extracts the 3 explicit dates in the fixture but misses dates in BEAM's `tool_output` / code-block / passing-aside contexts. Acceptance #5 fails post-PR. Rollback or rework.

**Fix.** Phase 9 should include a high-realism fixture *or* the plan should explicitly state that acceptance #5 is gated on a follow-up BEAM run AFTER PR merge, with rollback if it fails.

#### H3. The plan trusts FactSummary/RecallResult.metadata wire path but doesn't enumerate test coverage at all 4 sites

**Mechanism.** The plan's test list at Phase 5.6 has `test_factsummary_carries_event_date_at_all_construction_sites (parametrized over the 4 sites)`. But only one of the 4 sites is named (line 1046, 1160, 1256, 1355). If the implementer reads "parametrize over 4 sites" and parametrizes over the SAME site 4 times (which a tired implementer would do, especially if line numbers drift), the test passes and 3 of 4 sites are still broken.

**Failure mode.** Three of four FactSummary construction paths silently return `event_date=None`. Recall via those paths returns no event_date. Layer 3 (when shipped later) is silent no-op for those paths.

**Fix.** Plan must list which 4 functions/methods the parametrize covers, by name not line number, because line numbers in `facts.py` drift between branches.

#### H4. Phase 11 budgets 1 review pass; the spec needed 17

**Mechanism.** The spec PR went through 17 codex rounds, ~30 P1/P2 findings. The plan budgets ONE 3-agent review at Phase 11 with "All P1s resolved" gate. The codex-iteration memory's own takeaway: "wire-level integration gaps the architectural reviewers can't easily verify." 3-agent review = architectural reviewers. Codex review is not in the plan at all.

**Failure mode.** 3-agent review passes. PR opens. Codex reviews automatically. Finds 10+ wire-level bugs analogous to the spec's. PR sits open for days while implementer iterates. Branch staleness; merge conflicts; abandonment risk.

**Fix.** Phase 11 should explicitly include a codex review pass (e.g. push to PR draft state, request codex review, iterate to clean codex). Budget should assume 3-5 codex rounds even on a clean impl, given F075's history.

#### H5. Test set has zero coverage for legacy/malformed-DB-state edge cases

**Mechanism.** The plan tests happy paths. Missing:
- **`episode.started_at IS NULL`** — legacy episodes from pre-Episode.started_at-rollout. The summarizer prompt template includes `EPISODE_START_TIMESTAMP: {started_at.isoformat()}` ONLY when `started_at is not None`. But the prompt's "Resolve relative phrases against EPISODE_START_TIMESTAMP" instruction stays in the prompt regardless — LLM is told to anchor against something that's not there. Likely behavior: LLM emits guesses or omits dates. Not tested.
- **`Fact.embedding IS NULL`** in `_fetch_chunk_context` — caller guard returns None, good. But the *calling* `_process_batch` is supposed to handle the None gracefully and pass `chunk_context=None` to the classifier. Not tested that the classifier survives a None.
- **Malformed JSON from structured-output LLM call** — the spec says `call_background_llm_structured` "guarantees JSON" via tool_use. It doesn't guarantee `event_date` is in `YYYY-MM-DD`. The Pydantic validator drops bad dates, fine. But what if the LLM returns `event_date: "2024-13-45"` and the validator drops it to None and the fact is then stored without the date? The plan's tests cover this on `FactInput` level but not at the integration boundary.
- **DB constraint violation mid-batch** — what if migration 053 hasn't run but the script tries to UPDATE `event_date_classified_at`? Script error-out behavior is unspecified.
- **`Heart.search_facts` returning a fact without `event_date` populated** (legacy row) — Phase 4's pre-learn dedup bypass dereferences `existing[0].event_date`. If FactSummary doesn't expose it for some path, AttributeError. Plan doesn't test this regression.

**Failure mode.** Each of these surfaces in prod on `nous-default` (where many facts predate F075). Recoverable but embarrassing.

**Fix.** Each bullet above gets one explicit test.

### Medium

#### M1. Spec-drift hazards in the plan's paraphrases

The plan paraphrases spec sections. Specific drift hazards:

- **Plan Phase 5.5** says "Find the path that builds `RecallResult.metadata`. Add: `if fact.event_date is not None: metadata["event_date"] = fact.event_date.isoformat()`." Spec wire-path row 10 says: `RecallResult.metadata["event_date"] = fact.event_date.isoformat() if fact.event_date else None`. The plan's `if not None: add` is DIFFERENT from the spec's `add: value-or-None`. The spec writes `None` into the metadata dict; the plan omits the key. Downstream `metadata.get("event_date")` returns None either way, BUT a `"event_date" in metadata` check (which Phase 6.1 *literally uses*: `if "event_date" in r.metadata`) flips behavior. The Phase 6.1 wire then drops the None case. Probably fine but the plan and spec disagree on the dict shape.
- **Plan Phase 4.2** code snippet inverts the spec's logic awkwardly. Spec says "when both have dates AND dates differ → bypass dedup". Plan's snippet uses `if not (a is not None and b is not None and a != b): skip_dedup_path`. The double-negative is a known boolean-logic footgun. Codex called this exact pattern out in round 11.
- **Plan Phase 8** summarizes Phase 8 as "Copy the pseudo-code from spec §Layer 4 with extreme care — every line was earned by a codex round." This is a meta-instruction, not a contract. There's no checklist that says "verify these 17 specific things from rounds 11-17." Implementer reads "every line was earned" and trusts blindly.

#### M2. The `_merge_summaries` change drops a precondition

Plan Phase 3.3 replaces `merged_candidate_facts[:5]` with `dated[:event_limit] + stable[:5]`. But the current code is:

```python
return {
    ...
    "candidate_facts": merged_candidate_facts[:5],
    ...
}
```

The plan's replacement happens inline before the return. If the implementer adds the split logic but accidentally leaves the `merged_candidate_facts[:5]` AT the return, the change is dead. The plan doesn't say "delete the old line" explicitly.

#### M3. `event_date_classified_at` flag-gating has three sites; plan covers two

The plan Phase 4.1 sets `classified_at` for `_store_candidate_facts`. Phase 4.3 says "the direct LLM extraction path constructs `FactInput(...)` separately. Add `event_date=fact.get("event_date")` and the same conditional `event_date_classified_at` kwarg." But the spec wire-path row 11 explicitly handles `fact_extractor.py:189-196`. The plan paraphrases this and says "the direct LLM extraction path" — without giving the line number. An implementer who has already done Phase 4.1 may forget that Phase 4.3 is a separate site.

#### M4. Phase 1 verification uses `nous-eval-scratch` but Phase 8 smoke also uses it

Plan §1.3 verifies migration on `nous-eval-scratch`. Plan §8.3 smokes the backfill on `nous-eval-scratch`. If `nous-eval-scratch` is shared with concurrent eval work (e.g. F074 BEAM runs), tests stomp each other. The plan doesn't reserve / namespace.

#### M5. Rollback strategy missing details

Spec §Rollback handles flag-flips and migration-forward-only. The plan §Phase 12 doesn't enumerate:
- What happens to the `happened_before` edges already written if Layer 2 is disabled? They stay in `brain.graph_edges`. Adjacency boost still consumes them. Setting `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED=false` works but is a global flag (affects all relations) — the plan flips it ON in Phase 7 (implied by Layer 2 wiring) without telling the operator.
- What happens to facts that got `event_date_classified_at = NOW()` but `event_date = NULL` (terminal "no date found" state)? After rollback, those rows are *not* eligible for re-classification when the flag is re-enabled. The "terminal state" might mean wrong-classification stays sticky.
- Backfill-script side effects on prod aren't reverted by flag-flip.

#### M6. The 18th-rev cognitive risk

The spec is mature. The plan is tactical. Both feel "done." There's a known cognitive bias where reviewers signal-off on mature specs because everyone else already did. Phase 11 is the only structural check. If the plan-impl reviewer treats the impl as a "rote translation" of the spec, every wire-level subtlety codex earned for v2.7-v2.17 is at risk of regression.

**Mitigation suggestion.** Phase 11 reviewers should be given a checklist of the 17 codex rounds, specifically asked: "for each of these 17 findings, find the line of impl that addresses it and verify."

### Low

#### L1. `auto_link` constraint name fix (PR #452) not referenced

The plan touches `brain.graph_edges`. PR #452 fixed an `auto_link` constraint name issue. If the migration sequence has any drift, Phase 1 cold-start could fail. The plan doesn't reference #452. Worth checking before Phase 1.

#### L2. F074 BEAM harness still un-PR'd

Plan's "Open question #2" admits this. Means acceptance criterion #5 measurement isn't ready when impl PR merges. PR ships in a state where its primary acceptance metric cannot be measured. Bad optics.

#### L3. Conv 4 Q1 / Conv 5 Q1 date sourcing is hand-wavy

Plan §Phase 0 step 1: "date is approximate; the actual date from BEAM source needs to be looked up; if not present, use the question's date references." This is hedging. Either the source dates are there or they aren't. The plan should say which.

#### L4. `Fact` ORM relationship to existing F047 columns unverified

Plan adds `event_date` + `event_date_classified_at` to the ORM. The ORM already has F047's `actionable` + `actionable_confidence` + `actionable_classified_at` columns. Plan doesn't verify that adding two columns doesn't collide with index naming, alembic autogenerate, or batch SELECTs that use `SELECT *`. Low because the ORM is column-level and clean.

---

## Single most likely cause of impl PR rework

**Phase 8 backfill diverges from the spec's pseudo-code in subtle ways that won't show up in unit tests.** Specifically, the two-connection pattern. F047's pattern is the implementer's mental anchor. Codex round-14 explicitly forbade copying F047 here. The plan says "two-connection pattern" but doesn't say "DO NOT copy F047." An implementer who has internalized F047 (because they read the spec, which cites F047 ~10 times) will write the wrong shape, ship unit tests that pass (each test runs one batch), and the lock-leak only manifests under multi-batch prod load.

**Cost if missed.** Lock leaks accumulate until pool exhaustion. May not surface in eval-scratch; will surface on `nous-default` first run. PR is already merged when this is discovered → revert PR or hotfix.

**Concrete fix.** Plan Phase 8.1 must include a one-line comment in the script template: `# CRITICAL: do NOT copy F047's single-session pattern — see spec §Layer 4 "Hold the lock on a CHECKED-OUT raw connection" — codex round-14`.

---

## Recommendation

This is a competent plan and the spec is exceptionally mature. The risks above are mostly second-order. The plan does not need a redesign; it needs three hardening passes:

1. **Add explicit Phase 8 anti-patterns** (C1, C2, M1) — keep the implementer from regressing the round-14 fix.
2. **Tighten Phase 12 PR scope** (C3) — explicit pathspec, no `git add -A`.
3. **Bake in codex iteration into Phase 11** (H4) — assume 3-5 codex rounds even on a clean impl; budget time.

If those three are addressed inline, the implementer has a fair chance of landing this in one impl-review cycle. Without them, expect 2-3 cycles.
