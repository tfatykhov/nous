# User Profile Context Fixes Implementation Plan (v2 — post 3-agent review)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the User Profile section of the system prompt so post-initiation preference/person/rule facts actually reach the live prompt: replace the over-matching blob-level identity dedup with directional per-line coverage, give Tier-1 selection a recency pass (dark-gated) + deterministic ordering + configurable limit, make section truncation line-aware, and instrument the pipeline so suppression is observable.

**Architecture:** All changes are surgical edits inside `ContextEngine.build()`'s User Profile block (`nous/cognitive/context.py:475-506`), one SQL ordering change in `FactManager._list_by_category` (`nous/heart/facts.py`), and three new Settings fields. The dedup fix defaults ON (correctness-fix class, precedent `NOUS_SAME_SLOT_CONFLICT_ROUTING_ENABLED`) with a kill-switch back to legacy blob behavior. The Tier-1 recency pass is gated behind a NEW flag `NOUS_PROFILE_RECENCY_ENABLED` (default OFF, lands dark) because **prod already runs `NOUS_RECENCY_RESOLVER_ENABLED=true` + `NOUS_TEMPORAL_EXTRACTION_ENABLED=true`** (verified in `.env.prod-snapshot` 2026-07-23) — piggybacking on the resolver flag would make the recency pass live on deploy day, compounding with the dedup fix.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 async, pydantic-settings, pytest + pytest-asyncio against real Postgres (repo convention — no mocks for DB).

**Review status:** v1 reviewed by 3 agents 2026-07-23 (correctness / design devil's advocate / test quality) — all APPROVE WITH REVISIONS; every P1/P2 finding is integrated into this v2.

## Validated Findings This Plan Addresses

(Review doc validated against HEAD `1372922` on 2026-07-23; decision `024a7c6d`.)

1. **P1** — `text_overlap(fact[:200], whole_identity_blob)` makes the fact the *smaller* word-set, so a fact whose ≥3-char words 60%-appear *anywhere* in the identity blob is suppressed. Facts learned after initiation are structurally invisible with no log signal.
2. **P2** — Tier-1 selection is `ORDER BY confidence DESC LIMIT 20` (hardcoded, no override, no recency tiebreak, no `_resolve_recency` pass).
3. **P2** — Section-level truncation is a raw char slice mid-word (`_truncate_to_budget`), unlike `_format_facts`' per-fact word-boundary truncation.
4. **P3** — Identity and User Profile both `priority=1`; relative order is untested insertion-order.
5. **Observability** — "0 facts existed" vs "all deduped" vs "budget-truncated" collapse into the same absent/short section.

**Prod deployment facts (verified 2026-07-23, load-bearing for review):**
- `.env.prod-snapshot`: `NOUS_RECENCY_RESOLVER_ENABLED=true`, `NOUS_TEMPORAL_EXTRACTION_ENABLED=true`, `NOUS_CONTEXT_BUDGET_OVERRIDES={"...","user_profile": 500}`.
- Prod `agent_identity.preferences` (queried live, read-only): 1350 chars, 14 newlines, bulleted `### User / - ...` format → the per-line dedup fix IS effective in prod. On a single-line prose identity, per-line coverage degenerates to the blob metric — a **deliberate no-op**, documented + pinned by test.
- Deploy-day effect: the User Profile section will APPEAR in prod prompts (currently absent). With ~20 candidate facts × ~240 chars vs the 500-token (2000-char, pre-scale) prod budget, **truncation WILL fire** — Task 3's line-aware truncation and Task 2's retention order are load-bearing, not cosmetic. The section is `semi_stable` tier: expect a one-time cache-prefix miss on deploy plus a semi_stable cache break whenever a tier-1 fact changes. Accepted cost; stated in the PR body.
- The recency pass + score-sort stays INERT until the owner flips `NOUS_PROFILE_RECENCY_ENABLED` (new, default false).

**Explicitly out of scope** (validated as unnecessary or deferred):
- Excluding `superseded_by IS NOT NULL` facts — redundant; every supersession write path also sets `active=False` (facts.py:1265-66, 2113-14, 2167, 1607).
- Raising `user_profile` budget default — `NOUS_CONTEXT_BUDGET_OVERRIDES={"user_profile": N}` already works (prod already overrides to 500); leave default 200.
- Identity re-seeding / seed-fact-ID tracking — wider blast radius (static-tier cache semantics, identity ownership); separate feature if ever.

## Global Constraints

- Work on branch `fix/user-profile-context` in the worktree `E:\Projects\nous-worktrees\user-profile-context` (already created off `origin/main` @ 30bd24b; env installed via `uv sync --extra dev --extra runtime --extra agent`). Subagents MUST `cd` into the worktree and verify `git branch --show-current` prints `fix/user-profile-context` before any edit.
- All DB-touching tests use real Postgres via the existing `db` fixture (docker compose postgres on :5432 is up). No DB mocks. **Every pytest command for `tests/test_tiered_context.py` MUST be prefixed `NOUS_TEST_DB=postgres`** — the sqlite default errors on this file's `::jsonb` fixture (pre-existing; baseline verified 8/8 pass under Postgres, ERROR under sqlite).
- **Fresh-agent isolation is MANDATORY for every new test that asserts presence/absence/order/count of specific facts** (the module's `db` fixture is session-scoped with committed facts persisting across tests; isolation is by agent_id only). Follow the file's existing pattern: mint `f"test-<name>-{_uuid.uuid4().hex[:8]}"`, INSERT into `nous_system.agents`, build a Heart + ContextEngine against settings with that agent_id. Task 1 Step 6 defines the shared helper once; later tasks reuse it.
- New Settings fields follow existing style: env prefix `NOUS_` via pydantic-settings, `Field(default=..., description=...)`.
- Every new env var gets a row in the CLAUDE.md env-var table (Task 6).
- Kill-switch paths must reproduce today's behavior: `profile_identity_dedup_scope="blob"` reproduces current dedup exactly; `profile_recency_enabled=False` (default) keeps the recency pass + sort out of the pipeline entirely. **Known flagless change (accepted, note in PR):** the `_list_by_category` ORDER BY tiebreak (Task 2) is unconditional — equal-confidence ordering becomes learned_at DESC instead of DB-undefined; strictly deterministic, low risk.
- Commit style: `fix:` / `test:` / `docs:` prefixes; one logical change per commit.
- Full-suite baseline captured from origin/main in this worktree (sqlite default, 2026-07-23): **151 failed, 4821 passed, 348 skipped, 32 errors** (includes pre-existing `test_tiered_context.py` sqlite errors). After all tasks: re-run `uv run pytest tests/ -q` and diff — new failures/errors beyond the baseline set are yours to fix. The new tests will add entries to the pre-existing sqlite-error group for this file; verify them under `NOUS_TEST_DB=postgres` instead (matching the file's existing behavior).

---

### Task 1: Directional per-line identity dedup (P1 fix)

**Files:**
- Modify: `nous/cognitive/context.py` (module-level helper near `_one_line`; new constant near `_IDENTITY_OVERLAP_THRESHOLD`; User Profile block at ~475-506)
- Modify: `nous/config.py` (new Settings field, near `fact_format_max_chars` at ~202)
- Test: `tests/test_tiered_context.py` (new test classes + shared fresh-agent helper)

**Interfaces:**
- Produces: module-level function `_identity_coverage(fact_head: str, identity_lines: list[str]) -> float` in `nous/cognitive/context.py`; module-level constant `_IDENTITY_LINE_COVERAGE_THRESHOLD = 0.75`; Settings field `profile_identity_dedup_scope: str` (values `"line"` | `"blob"`, default `"line"`, env `NOUS_PROFILE_IDENTITY_DEDUP_SCOPE`); test helper `_fresh_engine(db, mock_embeddings, *, identity_prompt, settings_update=None) -> tuple[ContextEngine, Heart, Settings]` in `tests/test_tiered_context.py`.
- Consumes: existing `text_overlap` from `nous.utils` (blob mode only), `_IDENTITY_OVERLAP_THRESHOLD = 0.6` (unchanged, blob mode only).

**Design note (devil-P2):** line mode gets its OWN threshold, 0.75, not the blob's 0.6. Rationale: directional coverage of a correction against the bullet it corrects is high from scaffolding words alone — e.g. identity `- Tim prefers Fahrenheit for temperature` vs new fact `Tim prefers Celsius for temperature readings` = 4/6 ≈ 0.667 shared ≥3-char words. At 0.6 the correction is suppressed (defeats the plan's purpose); at 0.75 it survives while verbatim-seeded bullets (coverage 1.0) are still deduped.

- [ ] **Step 1: Write the failing unit tests**

Add to `tests/test_tiered_context.py`:

```python
from nous.cognitive.context import _identity_coverage


class TestIdentityCoverage:
    """Directional per-line dedup helper (P1 fix)."""

    def test_verbatim_seeded_fact_fully_covered(self):
        # auto_seed_from_facts writes facts verbatim as "- {content}" lines
        fact = "Tim prefers Celsius for all temperature readings"
        identity_lines = [
            "### Preferences",
            "- Tim prefers Celsius for all temperature readings",
            "- Tim wants concise answers",
        ]
        assert _identity_coverage(fact, identity_lines) >= 0.99

    def test_scattered_vocabulary_not_covered(self):
        # Words appear ACROSS lines but no single line covers the fact —
        # the blob-level bug this helper fixes (max single-line ≈ 0.43)
        fact = "Tim prefers email delivery for weekly reports"
        identity_lines = [
            "- Tim prefers Celsius for temperature",
            "- send delivery notifications to Telegram",
            "- weekly summary reports enabled",
            "- contact via email is tfatykhov@gmail.com",
        ]
        assert _identity_coverage(fact, identity_lines) < 0.6

    def test_correction_survives_line_threshold(self):
        # devil-P2: a same-slot CORRECTION shares scaffolding words with the
        # bullet it corrects (4/6 ≈ 0.667) — must stay BELOW the 0.75 line
        # threshold so corrections reach the prompt, while verbatim (1.0) dedups.
        from nous.cognitive.context import _IDENTITY_LINE_COVERAGE_THRESHOLD
        fact = "Tim prefers Celsius for temperature readings"
        identity_lines = ["- Tim prefers Fahrenheit for temperature"]
        cov = _identity_coverage(fact, identity_lines)
        assert 0.6 <= cov < _IDENTITY_LINE_COVERAGE_THRESHOLD

    def test_short_header_line_does_not_suppress(self):
        # Directional coverage: a short "### Preferences" header must not
        # cover a fact that merely contains the word "preferences" (1/8 = 0.125)
        fact = "Tim has strong preferences about code review workflows"
        identity_lines = ["### Preferences"]
        assert _identity_coverage(fact, identity_lines) < 0.3

    def test_single_line_identity_equals_blob_metric(self):
        # devil-P2a: on a single-line prose identity, per-line coverage equals
        # the legacy blob metric (fact is the smaller set) — a DELIBERATE no-op,
        # pinned here so it is never mistaken for a regression.
        from nous.utils import text_overlap
        fact = "Tim is a cognitive agent developer"
        prose = "Tim is a cognitive agent developer building Nous on Minsky principles"
        assert abs(_identity_coverage(fact, [prose]) - text_overlap(fact, prose)) < 1e-9

    def test_empty_inputs(self):
        assert _identity_coverage("", ["- something"]) == 0.0
        assert _identity_coverage("a fact here", []) == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `NOUS_TEST_DB=postgres uv run pytest tests/test_tiered_context.py::TestIdentityCoverage -v`
Expected: FAIL with `ImportError: cannot import name '_identity_coverage'`

- [ ] **Step 3: Implement the helper + constant**

In `nous/cognitive/context.py`, next to `_IDENTITY_OVERLAP_THRESHOLD` (~line 91):

```python
# Minimum text_overlap ratio to consider a fact redundant with identity prompt
_IDENTITY_OVERLAP_THRESHOLD = 0.6

# Line-mode threshold (2026-07-23 plan): directional per-line coverage needs a
# HIGHER bar than blob overlap — a same-slot correction shares ~0.67 of its
# words with the bullet it corrects (scaffolding words), and corrections
# reaching the prompt is the point of the fix. Verbatim-seeded bullets score 1.0.
_IDENTITY_LINE_COVERAGE_THRESHOLD = 0.75
```

After `_inline_name` (module level, ~line 64):

```python
def _identity_coverage(fact_head: str, identity_lines: list[str]) -> float:
    """Max fraction of the fact's meaningful (>=3-char) words covered by a
    SINGLE identity line.

    Directional on purpose: the legacy blob-level ``text_overlap`` made the
    fact the smaller word-set vs the whole identity prompt, so ~60% scattered
    vocabulary anywhere in the blob suppressed the fact. A per-line coverage
    only suppresses when one line (e.g. the verbatim "- {content}" bullet
    auto_seed_from_facts wrote) actually restates the fact. On a single-line
    identity this degenerates to the blob metric — deliberate no-op.
    """
    fact_words = {w for w in fact_head.lower().split() if len(w) >= 3}
    if not fact_words:
        return 0.0
    best = 0.0
    for line in identity_lines:
        line_words = {w for w in line.lower().split() if len(w) >= 3}
        if not line_words:
            continue
        cov = len(fact_words & line_words) / len(fact_words)
        if cov > best:
            best = cov
    return best
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `NOUS_TEST_DB=postgres uv run pytest tests/test_tiered_context.py::TestIdentityCoverage -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Add the Settings field**

In `nous/config.py`, after the `fact_format_full_top_n` / pre-turn render group (~line 206):

```python
    # User Profile identity dedup scope (2026-07-23 plan). "line" = directional
    # per-line coverage at _IDENTITY_LINE_COVERAGE_THRESHOLD (a fact is suppressed
    # only when ONE identity line covers >=75% of its meaningful words — fixes the
    # P1 over-suppression where blob-level overlap hid every post-initiation
    # preference/person/rule fact). "blob" = legacy whole-identity text_overlap
    # at 0.6 (kill-switch). Unknown values fall back to "blob" (fail-safe:
    # a typo degrades to today's behavior, never to no-dedup).
    profile_identity_dedup_scope: str = Field(
        default="line",
        description="User Profile vs identity dedup: 'line' (per-line coverage, default) or 'blob' (legacy whole-blob overlap).",
    )
```

- [ ] **Step 6: Add the shared fresh-agent test helper + wiring tests, then wire into build()**

First add the module-level helper to `tests/test_tiered_context.py` (below the existing fixtures; later tasks reuse it):

```python
async def _fresh_engine(db, mock_embeddings, base_settings, *, identity_prompt: str, settings_update: dict | None = None):
    """Fresh-agent isolation: mint a unique agent so committed facts from other
    tests in this module (session-scoped db, no truncation) can't pollute
    presence/absence/count assertions. Derives from the conftest `settings`
    fixture (NOT a raw Settings() — that would re-read env/.env and drift from
    the test config). Returns (engine, heart, settings). Caller must
    `await heart.close()` when done."""
    from sqlalchemy import text as sqltext
    agent_id = f"test-upf-{_uuid.uuid4().hex[:8]}"
    async with db.session() as session:
        await session.execute(
            sqltext("INSERT INTO nous_system.agents (id, name, config) VALUES (:id, :name, '{}'::jsonb) ON CONFLICT (id) DO NOTHING"),
            {"id": agent_id, "name": "UPF Test Agent"},
        )
        await session.commit()
    upd = {"agent_id": agent_id}
    if settings_update:
        upd.update(settings_update)
    s = base_settings.model_copy(update=upd)
    heart = Heart(db, s, embedding_provider=mock_embeddings)
    brain = Brain(database=db, settings=s)
    engine = ContextEngine(brain, heart, s, identity_prompt=identity_prompt)
    return engine, heart, s
```

(Imports `Brain`, `ContextEngine`, `Settings`, `Heart`, `FactInput`, `_uuid` already exist at the top of the file.)

**Every test calling `_fresh_engine` must take the conftest `settings` fixture as a parameter and pass it as the third argument** — e.g. `async def test_x(self, db, mock_embeddings, settings): engine, heart, s = await _fresh_engine(db, mock_embeddings, settings, identity_prompt=...)`. The plan's test snippets below show the call WITHOUT the fixture param for brevity at the signature; the implementer adds `settings` to each signature and call site (mechanical, applies to every `_fresh_engine` call in Tasks 1-5).

Then the wiring tests. NOTE on scope of assertions (tests-P1-1): the identity prompt is rendered verbatim in the `## Identity` section, so NEVER assert fact absence against the whole `system_prompt` — assert against the User Profile `ContextSection` from `result.sections`.

```python
def _profile_section(result):
    return next((s for s in result.sections if s.label == "User Profile"), None)


class TestProfileDedupScope:
    @pytest.mark.asyncio
    async def test_line_scope_retains_scattered_vocab_fact(self, db, mock_embeddings):
        """A fact whose words are scattered across identity lines survives 'line' scope."""
        identity = (
            "### Preferences\n"
            "- Tim prefers Celsius for temperature\n"
            "- send delivery notifications to Telegram\n"
            "- weekly summary reports enabled\n"
            "- contact via email always"
        )
        engine, heart, s = await _fresh_engine(db, mock_embeddings, identity_prompt=identity)
        try:
            async with db.session() as session:
                await heart.learn(
                    FactInput(content="Tim prefers email delivery for weekly reports", category="preference", subject="Tim"),
                    session=session,
                )
                await session.commit()
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-dedup-line",
                input_text="hello", frame=_frame(),
            )
            profile = _profile_section(result)
            assert profile is not None
            assert "email delivery for weekly reports" in profile.content
        finally:
            await heart.close()

    @pytest.mark.asyncio
    async def test_line_scope_still_dedups_verbatim_seeded_fact(self, db, mock_embeddings):
        """A fact restated verbatim as an identity bullet is still suppressed.
        (Green-first pin: legacy blob mode also suppresses this — correctness-F1.)"""
        content = "Tim prefers Celsius for all temperature readings"
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, identity_prompt=f"### Preferences\n- {content}",
        )
        try:
            async with db.session() as session:
                await heart.learn(
                    FactInput(content=content, category="preference", subject="Tim"),
                    session=session,
                )
                await session.commit()
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-dedup-verbatim",
                input_text="hello", frame=_frame(),
            )
            profile = _profile_section(result)
            assert profile is None or content not in profile.content
        finally:
            await heart.close()

    @pytest.mark.asyncio
    async def test_blob_scope_reproduces_legacy_suppression(self, db, mock_embeddings):
        """scope='blob' suppresses the scattered-vocab fact exactly like today."""
        identity = (
            "### Preferences\n"
            "- Tim prefers Celsius for temperature\n"
            "- send delivery notifications to Telegram\n"
            "- weekly summary reports enabled\n"
            "- contact via email always"
        )
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, identity_prompt=identity,
            settings_update={"profile_identity_dedup_scope": "blob"},
        )
        try:
            async with db.session() as session:
                await heart.learn(
                    FactInput(content="Tim prefers email delivery for weekly reports", category="preference", subject="Tim"),
                    session=session,
                )
                await session.commit()
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-dedup-blob",
                input_text="hello", frame=_frame(),
            )
            profile = _profile_section(result)
            assert profile is None or "email delivery for weekly reports" not in profile.content
        finally:
            await heart.close()

    @pytest.mark.asyncio
    async def test_unknown_scope_falls_back_to_blob(self, db, mock_embeddings):
        """tests-P3-1: a typo'd scope value degrades to legacy blob suppression,
        never to no-dedup."""
        identity = (
            "### Preferences\n"
            "- Tim prefers Celsius for temperature\n"
            "- send delivery notifications to Telegram\n"
            "- weekly summary reports enabled\n"
            "- contact via email always"
        )
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, identity_prompt=identity,
            settings_update={"profile_identity_dedup_scope": "garbage"},
        )
        try:
            async with db.session() as session:
                await heart.learn(
                    FactInput(content="Tim prefers email delivery for weekly reports", category="preference", subject="Tim"),
                    session=session,
                )
                await session.commit()
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-dedup-typo",
                input_text="hello", frame=_frame(),
            )
            profile = _profile_section(result)
            assert profile is None or "email delivery for weekly reports" not in profile.content
        finally:
            await heart.close()
```

Run: `NOUS_TEST_DB=postgres uv run pytest tests/test_tiered_context.py::TestProfileDedupScope -v`
Expected: `test_line_scope_retains_scattered_vocab_fact` FAILS (blob mode currently suppresses it). The other three PASS already (they pin legacy/fallback behavior — green-first pins per correctness-F1, that's fine).

Then replace the dedup filter inside the User Profile block in `build()` (`nous/cognitive/context.py`, currently lines 484-492):

```python
                if profile_facts and _effective_identity:
                    # Filter out facts already restated by the identity prompt.
                    # "line" (default): directional per-line coverage — suppress only
                    # when a single identity line covers >=_IDENTITY_LINE_COVERAGE_THRESHOLD
                    # of the fact's words (the verbatim auto-seed bullets). "blob"
                    # (legacy kill-switch, also the fallback for unknown values):
                    # whole-identity text_overlap, which over-matched scattered
                    # vocabulary and hid post-initiation facts (P1).
                    scope = getattr(self._settings, "profile_identity_dedup_scope", "line")
                    if scope == "line":
                        identity_lines = [
                            ln for ln in _effective_identity.splitlines() if ln.strip()
                        ]
                        profile_facts = [
                            f for f in profile_facts
                            if _identity_coverage(
                                (getattr(f, "content", "") or "")[:200],
                                identity_lines,
                            ) < _IDENTITY_LINE_COVERAGE_THRESHOLD
                        ]
                    else:
                        profile_facts = [
                            f for f in profile_facts
                            if text_overlap(
                                (getattr(f, "content", "") or "")[:200],
                                _effective_identity,
                            ) < _IDENTITY_OVERLAP_THRESHOLD
                        ]
```

- [ ] **Step 7: Run the wiring tests to verify all pass**

Run: `NOUS_TEST_DB=postgres uv run pytest tests/test_tiered_context.py -v`
Expected: PASS (all, including the file's pre-existing tests)

- [ ] **Step 8: Commit**

```bash
git add nous/cognitive/context.py nous/config.py tests/test_tiered_context.py
git commit -m "fix: User Profile identity dedup — directional per-line coverage (P1 over-suppression)"
```

---

### Task 2: Tier-1 selection — learned_at tiebreak, configurable limit, dark-gated recency pass (P2 fix)

**Files:**
- Modify: `nous/heart/facts.py:2950` (`_list_by_category` ORDER BY)
- Modify: `nous/cognitive/context.py` (User Profile block — pass limit, gated `_resolve_recency` + stable score sort)
- Modify: `nous/config.py` (two new Settings fields)
- Test: `tests/test_tiered_context.py`

**Interfaces:**
- Consumes: `ContextEngine._resolve_recency(facts: list) -> list` (exists, context.py:1378 — ALSO gated internally by `settings.recency_resolver_enabled`; mutates `recency_status`/`recency_date`/`score` on `FactSummary` items); `Heart.list_facts_by_category(categories, active_only=True, limit=20, session=None)` (exists, heart.py:419 — already accepts `limit`); test helper `_fresh_engine` + `_profile_section` from Task 1.
- Produces: Settings fields `profile_fact_limit: int` (default 20, env `NOUS_PROFILE_FACT_LIMIT`) and `profile_recency_enabled: bool` (default False, env `NOUS_PROFILE_RECENCY_ENABLED`); `_list_by_category` ordering `confidence DESC, learned_at DESC`.

**Design note (devil-P1):** prod runs `NOUS_RECENCY_RESOLVER_ENABLED=true` with temporal extraction populating `event_date`, so gating the Tier-1 recency pass on the resolver flag alone would make it LIVE on deploy day, compounded with Task 1's dedup change. It therefore gets its own flag, default OFF (lands dark). Effective activation requires BOTH `NOUS_PROFILE_RECENCY_ENABLED=true` AND `NOUS_RECENCY_RESOLVER_ENABLED=true` (the latter is checked inside `_resolve_recency` and is already true in prod — so post-deploy the owner flips only the new flag).

- [ ] **Step 1: Write the tests**

```python
class TestTier1Selection:
    @pytest.mark.asyncio
    async def test_equal_confidence_ordered_by_learned_at_desc(self, db, mock_embeddings):
        """learned_at DESC tiebreak: equal-confidence facts come newest-first.
        NOTE (correctness-F3 / tests-P1-2): pre-implementation the tie order is
        DB-UNDEFINED, so the red run may pass by luck occasionally — 5 rows make
        accidental full order ~1/120. Post-implementation it is deterministic."""
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import text as sqltext
        engine, heart, s = await _fresh_engine(db, mock_embeddings, identity_prompt="")
        try:
            ids = []
            base = datetime.now(timezone.utc)
            async with db.session() as session:
                for i in range(5):
                    r = await heart.learn(
                        FactInput(content=f"Tim distinct person fact number {i} here", category="person", subject=f"tb-{i}", confidence=0.9),
                        session=session,
                    )
                    await session.execute(
                        sqltext("UPDATE heart.facts SET learned_at = :t WHERE id = :id"),
                        {"t": base - timedelta(days=i), "id": r.id},
                    )
                    ids.append(r.id)  # ids[0] newest ... ids[4] oldest
                await session.commit()
            got = [f.id for f in await heart.list_facts_by_category(categories=["person"], limit=50)]
            assert [i for i in got if i in ids] == ids  # strict learned_at DESC within equal confidence
        finally:
            await heart.close()

    @pytest.mark.asyncio
    async def test_profile_limit_setting_respected(self, db, mock_embeddings):
        """NOUS_PROFILE_FACT_LIMIT caps the Tier-1 fetch. Fresh agent + exactly 4
        facts + empty identity (dedup skipped) => exactly 2 bullets render.
        NOTE (correctness-F2): model_copy(update=...) does NOT raise for the
        not-yet-existing field pre-impl; the red assertion is the bullet count
        (4 > 2 because the limit isn't consumed)."""
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, identity_prompt="",
            settings_update={"profile_fact_limit": 2},
        )
        try:
            async with db.session() as session:
                for i in range(4):
                    await heart.learn(
                        FactInput(content=f"Distinct preference number {i} about unrelated topic {i}", category="preference", subject=f"limit-subj-{i}"),
                        session=session,
                    )
                await session.commit()
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-limit",
                input_text="hello", frame=_frame(),
            )
            profile = _profile_section(result)
            assert profile is not None  # anti-vacuous (tests-P2-1)
            bullet_count = sum(
                1 for ln in profile.content.splitlines() if ln.strip().startswith("- ")
            )
            assert bullet_count == 2
        finally:
            await heart.close()

    @pytest.mark.asyncio
    async def test_recency_pass_tags_superseded_profile_fact(self, db, mock_embeddings):
        """With BOTH profile_recency_enabled and recency_resolver_enabled on,
        conflicting same-subject dated facts get current/superseded tags in the
        User Profile section and the older sinks last. Fresh agent with ONLY
        these 2 facts (tests-P1-3: shared-agent budget pressure could truncate
        the demoted tail line and break the assertion)."""
        from datetime import date
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, identity_prompt="",
            settings_update={"profile_recency_enabled": True, "recency_resolver_enabled": True},
        )
        try:
            async with db.session() as session:
                await heart.learn(
                    FactInput(content="Tim works at Initech as senior engineer", category="person", subject="recency-subj", event_date=date(2025, 1, 15)),
                    session=session,
                )
                await heart.learn(
                    FactInput(content="Tim works at Globex as senior engineer", category="person", subject="recency-subj", event_date=date(2026, 6, 15)),
                    session=session,
                )
                await session.commit()
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-recency",
                input_text="hello", frame=_frame(),
            )
            profile = _profile_section(result)
            assert profile is not None
            assert "[superseded 2025-01]" in profile.content
            assert "[current 2026-06]" in profile.content
            assert profile.content.index("Globex") < profile.content.index("Initech")
        finally:
            await heart.close()

    @pytest.mark.asyncio
    async def test_recency_pass_dark_by_default(self, db, mock_embeddings):
        """devil-P1: with profile_recency_enabled at its default (False), NO tags
        appear even though recency_resolver_enabled=True (prod's config)."""
        from datetime import date
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, identity_prompt="",
            settings_update={"recency_resolver_enabled": True},
        )
        try:
            async with db.session() as session:
                await heart.learn(
                    FactInput(content="Tim works at Initech as senior engineer", category="person", subject="recency-subj", event_date=date(2025, 1, 15)),
                    session=session,
                )
                await heart.learn(
                    FactInput(content="Tim works at Globex as senior engineer", category="person", subject="recency-subj", event_date=date(2026, 6, 15)),
                    session=session,
                )
                await session.commit()
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-recency-dark",
                input_text="hello", frame=_frame(),
            )
            profile = _profile_section(result)
            assert profile is not None
            assert "[superseded" not in profile.content
            assert "[current" not in profile.content
        finally:
            await heart.close()
```

(Verified by review: the mock embedding provider is SHA-256-seeded and near-orthogonal for distinct texts, so `heart.learn` will NOT merge the recency pair; the F075 differing-event_date dedup bypass additionally protects it; `event_date` persists unconditionally; the difflib ratio for the Initech/Globex pair ≈ 0.83 > 0.55 floor.)

- [ ] **Step 2: Run tests to verify the red set**

Run: `NOUS_TEST_DB=postgres uv run pytest tests/test_tiered_context.py::TestTier1Selection -v`
Expected: tiebreak test FAILS (probabilistically — see its docstring; if it passes by luck, proceed, the post-impl assertion is the deterministic one), limit test FAILS on `bullet_count == 2` (got 4), recency test FAILS (no tags rendered), dark-by-default test PASSES (pins current behavior).

- [ ] **Step 3: Implement**

`nous/heart/facts.py:2950` — change:

```python
        stmt = stmt.order_by(Fact.confidence.desc()).limit(limit)
```

to:

```python
        # Tie-break equal confidence by recency so a newer correction is never
        # crowded out by an older fact at the same confidence (2026-07-23 plan).
        # Unconditional (no flag): replaces DB-undefined tie order with a
        # deterministic one.
        stmt = stmt.order_by(Fact.confidence.desc(), Fact.learned_at.desc()).limit(limit)
```

`nous/config.py` — after `profile_identity_dedup_scope` (Task 1):

```python
    profile_fact_limit: int = Field(
        default=20, ge=1,
        description="Max preference/person/rule facts fetched for the Tier-1 User Profile section. Was hardcoded 20.",
    )
    # Dark flag (2026-07-23 plan): prod runs NOUS_RECENCY_RESOLVER_ENABLED=true,
    # so the Tier-1 recency pass must NOT piggyback on that flag or it goes live
    # on deploy. Effective activation requires BOTH this AND recency_resolver_enabled.
    profile_recency_enabled: bool = Field(
        default=False,
        description="Apply the pre-turn recency resolver (_resolve_recency) + demotion sort to Tier-1 User Profile facts. Requires recency_resolver_enabled too.",
    )
```

`nous/cognitive/context.py` — User Profile block: pass the limit:

```python
                profile_facts = await self._heart.list_facts_by_category(
                    categories=TIER1_FACT_CATEGORIES,
                    active_only=True,
                    limit=self._settings.profile_fact_limit,
                    session=session,
                )
```

and add the gated recency pass + stable sort after the dedup filter (i.e. after Task 1's scope block, before `if profile_facts:` / `_format_facts`):

```python
                # Gap-2 parity with the Tier-3 fact path, gated by its OWN dark
                # flag (prod already runs recency_resolver_enabled=true — see
                # config.py note): tag current/superseded on event_date conflicts.
                # The stable sort keeps confidence order for untouched facts and
                # sinks demoted (score*0.3) facts to the tail, where line-aware
                # truncation drops them first.
                if getattr(self._settings, "profile_recency_enabled", False):
                    profile_facts = self._resolve_recency(list(profile_facts))
                    profile_facts.sort(
                        key=lambda f: (getattr(f, "score", 1.0) or 0.0), reverse=True
                    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `NOUS_TEST_DB=postgres uv run pytest tests/test_tiered_context.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add nous/heart/facts.py nous/cognitive/context.py nous/config.py tests/test_tiered_context.py
git commit -m "fix: Tier-1 profile selection — learned_at tiebreak, NOUS_PROFILE_FACT_LIMIT, dark-gated recency pass"
```

---

### Task 3: Line-aware section truncation for User Profile (P2 fix)

**Files:**
- Modify: `nous/cognitive/context.py` (new method next to `_truncate_to_budget` at ~1357; wire in User Profile block)
- Test: `tests/test_tiered_context.py`

**Interfaces:**
- Produces: `ContextEngine._truncate_to_budget_lines(text: str, token_budget: int) -> str`.
- Consumes: `ContextEngine._truncate_to_budget` (exists, context.py:1357) as single-huge-line fallback; `CHARS_PER_TOKEN = 4` class attr; test helper `_fresh_engine` from Task 1.

- [ ] **Step 1: Write the failing tests**

```python
class TestLineAwareTruncation:
    @pytest.mark.asyncio
    async def test_drops_whole_lines_only(self, db, mock_embeddings):
        engine, heart, s = await _fresh_engine(db, mock_embeddings, identity_prompt="")
        try:
            lines = [f"- fact number {i} with some padding text here" for i in range(10)]
            text = "\n".join(lines)
            # Budget fits ~3 lines: 3 lines * ~44 chars ≈ 131 chars ≤ 33*4=132
            out = engine._truncate_to_budget_lines(text, 33)
            assert len(out) <= 33 * engine.CHARS_PER_TOKEN
            for ln in out.split("\n"):
                assert ln in lines  # every emitted line is intact — no mid-word slice
            assert not out.endswith("...")
        finally:
            await heart.close()

    @pytest.mark.asyncio
    async def test_under_budget_unchanged(self, db, mock_embeddings):
        engine, heart, s = await _fresh_engine(db, mock_embeddings, identity_prompt="")
        try:
            text = "- short line"
            assert engine._truncate_to_budget_lines(text, 100) == text
        finally:
            await heart.close()

    @pytest.mark.asyncio
    async def test_single_huge_line_falls_back_to_char_slice(self, db, mock_embeddings):
        engine, heart, s = await _fresh_engine(db, mock_embeddings, identity_prompt="")
        try:
            text = "x" * 10_000
            out = engine._truncate_to_budget_lines(text, 25)
            assert out == engine._truncate_to_budget(text, 25)
            assert out.endswith("...")
        finally:
            await heart.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `NOUS_TEST_DB=postgres uv run pytest tests/test_tiered_context.py::TestLineAwareTruncation -v`
Expected: FAIL with `AttributeError: 'ContextEngine' object has no attribute '_truncate_to_budget_lines'`

- [ ] **Step 3: Implement**

In `nous/cognitive/context.py`, directly after `_truncate_to_budget` (~line 1362):

```python
    def _truncate_to_budget_lines(self, text: str, token_budget: int) -> str:
        """Truncate to budget by dropping whole trailing lines.

        Unlike ``_truncate_to_budget`` (raw char slice), this never emits a
        mid-word/mid-fact fragment — used for list-shaped sections (User
        Profile) where a guillotined tail line can cut a qualifier off a
        preference. Falls back to the char slice when even the first line
        exceeds the budget.
        """
        max_chars = token_budget * self.CHARS_PER_TOKEN
        if len(text) <= max_chars:
            return text
        kept: list[str] = []
        used = 0
        for ln in text.split("\n"):
            add = len(ln) + (1 if kept else 0)  # +1 for the joining newline
            if used + add > max_chars:
                break
            kept.append(ln)
            used += add
        if not kept:
            return self._truncate_to_budget(text, token_budget)
        return "\n".join(kept)
```

Wire in the User Profile block — change:

```python
                    profile_text = self._truncate_to_budget(profile_text, self._scaled_budget(budget.user_profile))
```

to:

```python
                    profile_text = self._truncate_to_budget_lines(
                        profile_text, self._scaled_budget(budget.user_profile)
                    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `NOUS_TEST_DB=postgres uv run pytest tests/test_tiered_context.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add nous/cognitive/context.py tests/test_tiered_context.py
git commit -m "fix: line-aware User Profile truncation — drop whole facts, never mid-word slice"
```

---

### Task 4: Build-time User Profile instrumentation (observability)

**Files:**
- Modify: `nous/cognitive/context.py` (User Profile block)
- Test: `tests/test_tiered_context.py`

**Interfaces:**
- Produces: one `logger.info` line per build: `"User Profile: raw=%d deduped_out=%d final=%d truncated=%s"` — emitted ALWAYS when `budget.user_profile > 0` (including raw=0 and all-deduped cases; distinguishing those states is the point). `final` = post-dedup fact count (pre-truncation — the truncated flag carries the rest).
- Consumes: the block structure established by Tasks 1-3; test helpers `_fresh_engine` + `_profile_section`.

- [ ] **Step 1: Write the failing tests**

```python
class TestProfileInstrumentation:
    @pytest.mark.asyncio
    async def test_all_deduped_state_logged_and_section_omitted(self, db, mock_embeddings, caplog):
        """tests-P3-2 + observability: all-facts-deduped is distinguishable from
        no-facts-exist (raw=1 deduped_out=1 final=0), and the ContextSection is
        omitted entirely."""
        import logging
        content = "Tim uses spaces not tabs consistently everywhere"
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, identity_prompt=f"### Preferences\n- {content}",
        )
        try:
            async with db.session() as session:
                await heart.learn(
                    FactInput(content=content, category="preference", subject="instr-subj"),
                    session=session,
                )
                await session.commit()
            with caplog.at_level(logging.INFO, logger="nous.cognitive.context"):
                result = await engine.build(
                    agent_id=s.agent_id, session_id="s-instr",
                    input_text="hello", frame=_frame(),
                )
            assert _profile_section(result) is None
            profile_logs = [r for r in caplog.records if "User Profile:" in r.getMessage()]
            assert profile_logs, "expected a User Profile instrumentation log line"
            msg = profile_logs[0].getMessage()
            assert "raw=1" in msg and "deduped_out=1" in msg and "final=0" in msg
        finally:
            await heart.close()

    @pytest.mark.asyncio
    async def test_truncated_flag_true_when_budget_tiny(self, db, mock_embeddings, caplog):
        """tests-P3-3: the truncated=True branch fires under a tiny budget."""
        import logging
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, identity_prompt="",
            settings_update={"context_budget_overrides": {"user_profile": 10}},
        )
        try:
            async with db.session() as session:
                for i in range(5):
                    await heart.learn(
                        FactInput(content=f"Verbose distinct preference number {i} with plenty of padding words attached", category="preference", subject=f"trunc-{i}"),
                        session=session,
                    )
                await session.commit()
            with caplog.at_level(logging.INFO, logger="nous.cognitive.context"):
                await engine.build(
                    agent_id=s.agent_id, session_id="s-trunc",
                    input_text="hello", frame=_frame(),
                )
            profile_logs = [r for r in caplog.records if "User Profile:" in r.getMessage()]
            assert profile_logs and "truncated=True" in profile_logs[0].getMessage()
        finally:
            await heart.close()
```

NOTE for implementer: `context_budget_overrides` is applied in `build()` via `budget.apply_overrides` (context.py:195-196) — REPLACE semantics, so `{"user_profile": 10}` = 40 chars, guaranteeing truncation with 5 long facts. If the first test's `raw=1` is polluted (it cannot be — fresh agent), do NOT weaken to substring-presence; investigate.

- [ ] **Step 2: Run tests to verify they fail**

Run: `NOUS_TEST_DB=postgres uv run pytest tests/test_tiered_context.py::TestProfileInstrumentation -v`
Expected: FAIL — no log record matching "User Profile:"

- [ ] **Step 3: Implement**

Restructure the User Profile block to track counts. Final shape of the whole block (integrating Tasks 1-4 — this is the complete target state of `build()`'s section 1b):

```python
        # 1b. User Profile (Tier 1 — always loaded, no semantic search)
        # Dedup against identity prompt to avoid repeating the same info
        if budget.user_profile > 0:
            try:
                profile_facts = await self._heart.list_facts_by_category(
                    categories=TIER1_FACT_CATEGORIES,
                    active_only=True,
                    limit=self._settings.profile_fact_limit,
                    session=session,
                )
                raw_count = len(profile_facts)
                if profile_facts and _effective_identity:
                    # Filter out facts already restated by the identity prompt.
                    # "line" (default): directional per-line coverage — suppress only
                    # when a single identity line covers >=_IDENTITY_LINE_COVERAGE_THRESHOLD
                    # of the fact's words (the verbatim auto-seed bullets). "blob"
                    # (legacy kill-switch, also the fallback for unknown values):
                    # whole-identity text_overlap, which over-matched scattered
                    # vocabulary and hid post-initiation facts (P1).
                    scope = getattr(self._settings, "profile_identity_dedup_scope", "line")
                    if scope == "line":
                        identity_lines = [
                            ln for ln in _effective_identity.splitlines() if ln.strip()
                        ]
                        profile_facts = [
                            f for f in profile_facts
                            if _identity_coverage(
                                (getattr(f, "content", "") or "")[:200],
                                identity_lines,
                            ) < _IDENTITY_LINE_COVERAGE_THRESHOLD
                        ]
                    else:
                        profile_facts = [
                            f for f in profile_facts
                            if text_overlap(
                                (getattr(f, "content", "") or "")[:200],
                                _effective_identity,
                            ) < _IDENTITY_OVERLAP_THRESHOLD
                        ]
                deduped_out = raw_count - len(profile_facts)

                # Gap-2 parity with the Tier-3 fact path, gated by its OWN dark
                # flag (prod already runs recency_resolver_enabled=true — see
                # config.py note): tag current/superseded on event_date conflicts.
                # The stable sort keeps confidence order for untouched facts and
                # sinks demoted (score*0.3) facts to the tail, where line-aware
                # truncation drops them first.
                if getattr(self._settings, "profile_recency_enabled", False):
                    profile_facts = self._resolve_recency(list(profile_facts))
                    profile_facts.sort(
                        key=lambda f: (getattr(f, "score", 1.0) or 0.0), reverse=True
                    )

                was_truncated = False
                if profile_facts:
                    profile_text = self._format_facts(profile_facts)
                    _full_len = len(profile_text)
                    profile_text = self._truncate_to_budget_lines(
                        profile_text, self._scaled_budget(budget.user_profile)
                    )
                    was_truncated = len(profile_text) < _full_len
                    sections.append(
                        ContextSection(
                            priority=1,
                            label="User Profile",
                            content=profile_text,
                            token_estimate=self._estimate_tokens(profile_text),
                            tier=SECTION_TIERS.get("User Profile", "dynamic"),
                        )
                    )
                # final = post-dedup fact count (pre-truncation; truncated flag
                # carries the rest). Distinguishes 0-existed / all-deduped /
                # budget-truncated — the three states that were indistinguishable.
                logger.info(
                    "User Profile: raw=%d deduped_out=%d final=%d truncated=%s",
                    raw_count, deduped_out, len(profile_facts), was_truncated,
                )
            except Exception:
                logger.warning("Failed to load user profile facts for Tier 1 context")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `NOUS_TEST_DB=postgres uv run pytest tests/test_tiered_context.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add nous/cognitive/context.py tests/test_tiered_context.py
git commit -m "feat: User Profile build-time instrumentation (raw/deduped_out/final/truncated)"
```

---

### Task 5: Section-order regression test (P3)

**Files:**
- Modify: `nous/cognitive/context.py` (one comment only)
- Test: `tests/test_tiered_context.py`

**Interfaces:**
- Consumes: `BuildResult.system_prompt` (exists); Identity section appended before User Profile in `build()` body (both `priority=1`; `sorted()` is stable so insertion order is the contract); test helper `_fresh_engine`.

- [ ] **Step 1: Write the test (green-first — it's a regression pin)**

```python
class TestSectionOrder:
    @pytest.mark.asyncio
    async def test_identity_precedes_user_profile(self, db, mock_embeddings):
        """Both sections carry priority=1; stable sort makes insertion order the
        contract. Pin it so a build() body reorder can't silently flip the prompt."""
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings,
            identity_prompt="You are Nous, a cognitive agent for testing.",
        )
        try:
            async with db.session() as session:
                await heart.learn(
                    FactInput(content="Tim enjoys hiking in national parks on weekends", category="person", subject="order-subj"),
                    session=session,
                )
                await session.commit()
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-order",
                input_text="hello", frame=_frame(),
            )
            sp = result.system_prompt
            assert "## Identity" in sp and "## User Profile" in sp
            assert sp.index("## Identity") < sp.index("## User Profile")
        finally:
            await heart.close()
```

- [ ] **Step 2: Run the test**

Run: `NOUS_TEST_DB=postgres uv run pytest tests/test_tiered_context.py::TestSectionOrder -v`
Expected: PASS (pins current behavior)

- [ ] **Step 3: Add the contract comment in build()**

Above the Identity section append (`nous/cognitive/context.py` ~line 225):

```python
        # 1. Identity (always included)
        # NOTE: Identity and User Profile both carry priority=1; sorted() is
        # stable, so Identity renders first ONLY because this append precedes
        # the User Profile append below. Pinned by
        # tests/test_tiered_context.py::TestSectionOrder.
```

(Keep the existing `# 008: Use identity_override...` comment below it.)

- [ ] **Step 4: Run full tiered-context file**

Run: `NOUS_TEST_DB=postgres uv run pytest tests/test_tiered_context.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add nous/cognitive/context.py tests/test_tiered_context.py
git commit -m "test: pin Identity-before-User-Profile section order (priority-1 tie)"
```

---

### Task 6: Docs + full suite + branch finish

**Files:**
- Modify: `CLAUDE.md` (env-var table)
- No code changes.

- [ ] **Step 1: Add env-var rows to CLAUDE.md**

In the Environment Variables table, after the `NOUS_FACT_PIN_TOP_K` / `NOUS_SUPERSESSION_LINEAGE_MODE` cluster (pre-turn rendering group):

```markdown
| `NOUS_PROFILE_IDENTITY_DEDUP_SCOPE` | `line` | User Profile vs identity dedup (2026-07-23): `line` = directional per-line coverage (suppress a fact only when ONE identity line covers ≥75% of its meaningful words — fixes the P1 over-suppression that hid every post-initiation preference/person/rule fact; corrections sharing ~67% scaffolding words with the bullet they correct now survive); `blob` = legacy whole-identity overlap at 0.6 (kill-switch; also the fallback for unknown values). On a single-line prose identity the two modes coincide (deliberate no-op — prod identity verified multi-line bulleted 2026-07-23). |
| `NOUS_PROFILE_FACT_LIMIT` | `20` | Max preference/person/rule facts fetched for the Tier-1 User Profile section (was hardcoded 20). Section budget still applies after formatting; overflow now drops whole fact lines (never a mid-word slice), newest-first retained within equal confidence. |
| `NOUS_PROFILE_RECENCY_ENABLED` | `false` | **Land-dark.** Apply the pre-turn recency resolver (`_resolve_recency`: current/superseded tags + demotion sort on event_date conflicts) to Tier-1 User Profile facts. Requires `NOUS_RECENCY_RESOLVER_ENABLED=true` as well (already true in prod) — gated separately precisely so the Tier-1 pass does NOT go live on deploy day alongside the dedup fix. Flip after observing the dedup fix in prod. |
```

- [ ] **Step 2: Run the full suite and diff against baseline**

```bash
uv run pytest tests/ -q 2>&1 | tail -5
```

Expected: same failure/error set as the origin/main baseline (**151 failed, 4821 passed, 348 skipped, 32 errors**, sqlite default) PLUS the new tests appearing in the pre-existing `test_tiered_context.py` sqlite-error group. Verify the new tests separately: `NOUS_TEST_DB=postgres uv run pytest tests/test_tiered_context.py -v` → all pass. Any OTHER new failure = yours; fix before proceeding.

- [ ] **Step 3: Commit docs**

```bash
git add CLAUDE.md
git commit -m "docs: NOUS_PROFILE_IDENTITY_DEDUP_SCOPE / NOUS_PROFILE_FACT_LIMIT / NOUS_PROFILE_RECENCY_ENABLED env rows"
```

- [ ] **Step 4: Push branch + open PR**

```bash
git push -u origin fix/user-profile-context
gh pr create --title "fix: User Profile context pipeline — per-line identity dedup, Tier-1 selection, line-aware truncation, instrumentation" --body "<see required content below>"
```

PR body MUST include:
- The validated findings summary (P1 over-suppression mechanism, one sentence).
- **Prod impact statement (devil-P1/P2):** prod runs `NOUS_RECENCY_RESOLVER_ENABLED=true` + `NOUS_TEMPORAL_EXTRACTION_ENABLED=true` + `user_profile: 500` budget override; on deploy the User Profile section will APPEAR in prompts (that is the fix) with line-aware truncation active; expect a one-time semi_stable cache-prefix miss and per-fact-change semi_stable cache breaks thereafter; the Tier-1 recency pass stays DARK until `NOUS_PROFILE_RECENCY_ENABLED=true` is set.
- The three new env vars with defaults.
- Rollback lines: `NOUS_PROFILE_IDENTITY_DEDUP_SCOPE=blob` restores legacy dedup byte-identically; `NOUS_PROFILE_RECENCY_ENABLED` already defaults off. Note the one flagless change: `_list_by_category` equal-confidence tie order becomes learned_at DESC (was DB-undefined).
- Budget default unchanged (override via `NOUS_CONTEXT_BUDGET_OVERRIDES`).

- [ ] **Step 5: Codex review rounds**

Per repo workflow (`feedback_codex_recheck`): codex triggers on push of ready PRs. Address findings by fix-family, re-push, repeat until clean (clean may be a 👍 reaction).
