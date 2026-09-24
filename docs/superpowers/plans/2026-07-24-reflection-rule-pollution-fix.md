# Reflection→Rule Pollution Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Stop end-of-session and sleep reflections from polluting the Tier-1 User Profile: session lessons must never be stored as `category="rule"`, and the User Profile selection must exclude reflection sources defensively. Then remediate prod (1,148 + 329 mislabeled facts).

**Diagnosis (verified against prod 2026-07-24, decision e8df351b):** `layer.py:1910-1914` hardcodes `category="rule"` (+ default confidence 1.0, no subject) for every `learned:` line in the end-of-session reflection — 1,148 active facts, 1,093 subject-less, all conf 1.00, = 60% of the entire `rule` category. Tier-1 selects `confidence DESC, learned_at DESC LIMIT 20` → the newest session's lessons fill all 20 User Profile slots (reproduced the user's pasted section verbatim). Secondary: `sleep_handler.py:762-768` stores LLM-chosen categories at conf 0.8; the LLM picked `rule` for 329 lesson facts.

**Design decisions (locked, post 2-agent review 2026-07-24):**
1. Reflection lessons are lessons, not user directives: `layer.py` reflection facts become `category="technical"`, `subject="lesson_learned"`, `confidence=0.7` (matches the sleep FALLBACK path's 0.7; below the structured path's 0.8 — single-session lessons are weaker evidence).
2. `sleep_handler`: THREE fixes — (a) post-map LLM category `"rule"` → `"technical"` on the structured path (~762-768), (b) the **fallback path at ~810-816 hardcodes `category="rule"` too** (correctness-P2-1) → literal change to `"technical"`, (c) remove `"rule"` from `_REFLECTION_SCHEMA`'s category enum (~line 123) so the LLM stops proposing it (keep the post-map as drift defense — belt and braces).
3. Defense in depth: SQL-level source exclusion in `_list_by_category`, driven by new setting `profile_exclude_sources: list[str] = ["reflection", "sleep_reflection"]` (env `NOUS_PROFILE_EXCLUDE_SOURCES`). **Must be in SQL, not a Python post-filter** (post-filtering after LIMIT lets excluded rows consume the whole limit) and **NULL-safe** (`source IS NULL OR source NOT IN (...)` — plain NOT IN silently drops legit NULL-source legacy facts; prod has 11 across tier-1).
4. Side effect (intended, documented): recategorized lessons LEAVE the always-on Tier-1 channel and ENTER the Tier-3 semantic search pool (tier-1 categories are excluded from tier-3 search) — session lessons become topic-triggered instead of permanently injected.
5. Prod backfill is post-merge, manual, watermarked, reversible (Task 5 — not part of the PR).
6. **Documented behavior changes (both intended, both in PR body):** (a) prod admission is BLOCKING (shadow=false, threshold 0.60) and type_prior technical=0.70 vs rule=0.95 at weight 0.30 → future reflection lessons lose 0.075 composite score; borderline lessons will be admission-rejected — acceptable (fewer low-value lessons stored). (b) As `technical`, lessons become eligible for the sleep `stale_scan` (60-day never-recalled deactivation; `rule` was exempt) — a feature: session lessons SHOULD age out; they are ephemeral learnings, not standing directives.
7. **Post-backfill expectation (devil-P2-2, verified by prod sampling):** the top-20 becomes user_direct (~0.96) + enumerative_extractor (~0.95) facts. The 181 enumerative `rule` facts are mislabeled document-atoms ("The DAG completion_check exit code rule...", article recommendations) — a KNOWN next pollution tier, deliberately out of scope (fixing enumerative category assignment + its backfill is a follow-up; source-excluding enumerative here would also kill its 87 person + 79 preference facts). State this in the PR; do not claim the profile is fully clean.
8. `rest.py::list_profile_facts` (PR #570 dashboard endpoint) passes the same `exclude_sources` — its docstring promises "exactly what the agent can draw from", so it must mirror the prompt path.

## Global Constraints

- Branch `fix/reflection-rule-pollution` in a fresh worktree off `origin/main`. Subagents/lead MUST cd + verify branch before edits.
- Tests: real Postgres where DB-touching (`NOUS_TEST_DB=postgres`); new REST-independent tests go in the suites that already cover these code paths (find existing `end_session` reflection tests via `grep -rn 'learned:' tests/` and sleep reflection tests via `grep -rn 'sleep_reflection\|_phase_reflect\|structured_facts' tests/`).
- Kill-switch/off behavior: `NOUS_PROFILE_EXCLUDE_SOURCES=[]` must reproduce today's selection byte-identically.
- CLAUDE.md env-table row for the new setting.
- Commits: `fix:`/`test:`/`docs:`, one logical change each. Full-suite diff vs origin/main baseline at the end.

---

### Task 1: layer.py reflection fact reclassification

**Files:** Modify `nous/cognitive/layer.py:1908-1916`; test in the existing end_session/reflection test file.

Change the FactInput construction to:

```python
                    # Session lessons are LESSONS, not user directives — category
                    # "rule" here polluted the Tier-1 User Profile (2026-07-24
                    # diagnosis: 1,148 conf-1.0 reflection facts = the whole
                    # top-20). technical/lesson_learned/0.7 mirrors the sleep-
                    # reflection convention and moves lessons into the Tier-3
                    # semantic pool instead of the always-on profile.
                    fact_input = FactInput(
                        content=learned_text,
                        source="reflection",
                        category="technical",
                        subject="lesson_learned",
                        confidence=0.7,
                        source_episode_id=ep_uuid,
                    )
```

Test (red-first): locate the existing test exercising `end_session` with a `learned:` reflection (grep). Add/extend an assertion that the stored fact has `category == "technical"`, `subject == "lesson_learned"`, `confidence == pytest.approx(0.7)`. Expected red: current values rule/None/1.0.

Commit: `fix: end-session reflection lessons stored as technical/lesson_learned/0.7 — never rule (Tier-1 pollution)`

### Task 2: sleep_handler category demotion

**Files:** Modify `nous/handlers/sleep_handler.py:762-768`; test in the existing sleep-reflection structured-facts test.

```python
                    category = fact.get("category", "concept")
                    if category == "rule":
                        # Sleep reflections produce lessons; genuine user rules
                        # arrive via user_direct/correction paths. "rule" here
                        # pollutes the Tier-1 User Profile (2026-07-24).
                        category = "technical"
                    result = await self._heart.learn(FactInput(
                        subject=subject,
                        content=fact["content"],
                        source="sleep_reflection",
                        confidence=0.8,
                        category=category,
                    ))
```

Also: fallback path (~810-816) — change the literal `category="rule"` to `category="technical"`; `_REFLECTION_SCHEMA` (~line 123) — remove `"rule"` from the category enum.

Test (red-first): sleep tests MOCK heart.learn (`test_sleep_handler.py:652` AsyncMock) — assert on `heart.learn.call_args` FactInput fields, not stored rows (correctness-P3-3). Feed a structured fact dict with `category="rule"` → assert called FactInput.category == "technical"; a `category="preference"` dict stays `"preference"` (mapping is rule-only); and a fallback-path case (structured facts empty, lessons present) asserts category "technical". Nearest scaffold: `test_reflect_stores_patterns_and_lessons` (test_sleep_handler.py:643).

Commit: `fix: sleep-reflection facts never stored as category rule (structured map + fallback literal + schema enum)`

### Task 3: Tier-1 source exclusion (SQL, NULL-safe)

**Files:** Modify `nous/heart/facts.py` (`_list_by_category` + `list_by_category` signature), `nous/heart/heart.py:419-427` (wrapper), `nous/cognitive/context.py` User Profile block, `nous/config.py`; tests in `tests/test_tiered_context.py` (fresh-agent `_fresh_engine` pattern) and/or `tests/test_facts.py`.

`nous/config.py` (next to `profile_fact_limit`):

```python
    profile_exclude_sources: list[str] = Field(
        default_factory=lambda: ["reflection", "sleep_reflection"],
        description="Fact sources excluded from the Tier-1 User Profile section (SQL-level, NULL-source facts always kept). Reflection lessons are not user profile data. Empty list disables.",
    )
```

`facts.py::_list_by_category` — add `exclude_sources: list[str] | None = None` param; when non-empty:

```python
        if exclude_sources:
            # NULL-safe: plain NOT IN drops NULL-source rows (legacy facts).
            stmt = stmt.where(
                or_(Fact.source.is_(None), Fact.source.notin_(exclude_sources))
            )
```

(`or_` already imported at facts.py:17.) Thread the param through `list_by_category` and `Heart.list_facts_by_category` (default `None` — all existing callers byte-identical). `context.py` User Profile block passes `exclude_sources=self._settings.profile_exclude_sources or None`. **Also** `rest.py::list_profile_facts` (~line 533) passes the same (design decision 8) — its dashboard view must mirror the prompt path.

Tests (red-first, fresh-agent isolation):
1. Fact learned with `source="reflection"`, category `rule` (simulating legacy pollution via direct FactInput) does NOT appear in the User Profile section; a NULL-source tier-1 fact DOES appear.
2. `profile_exclude_sources=[]` override → the reflection-source fact appears (legacy behavior pinned).

Commit: `fix: NOUS_PROFILE_EXCLUDE_SOURCES — SQL-level Tier-1 source exclusion (reflection, sleep_reflection)`

### Task 4: Docs + suites + PR

- CLAUDE.md env row:

```markdown
| `NOUS_PROFILE_EXCLUDE_SOURCES` | `["reflection", "sleep_reflection"]` | Fact sources excluded (SQL-level, NULL-safe) from the Tier-1 User Profile section. Reflection lessons are lessons, not user profile data — they polluted all 20 profile slots at conf 1.00 (2026-07-24 diagnosis). Empty list disables. |
```

- Full backend suite diff vs origin/main baseline; `NOUS_TEST_DB=postgres` runs for the touched test files.
- PR body: diagnosis summary (with prod numbers), the three changes, the intended Tier-1→Tier-3 migration effect for lessons, backfill plan (post-merge, separate), rollback (`NOUS_PROFILE_EXCLUDE_SOURCES=[]`; write-path changes are forward-only but old behavior restorable by revert).
- Codex rounds until clean.

### Task 5 (post-merge, manual, prod): backfill

Preconditions: PR merged + prod deploy NOT required (data-only; the selection filter arrives with next deploy, but recategorization helps immediately).

1. Snapshot/watermark: record `now()` and counts.
2. Verify blast radius (expect ~1,148 and ~329):

```sql
SELECT count(*) FROM heart.facts WHERE agent_id='nous-default' AND active AND category='rule' AND source='reflection';
SELECT count(*) FROM heart.facts WHERE agent_id='nous-default' AND active AND category='rule' AND source='sleep_reflection';
```

3. Capture exact pre-state FIRST (mandatory — makes rollback exact regardless of any premise):

```sql
CREATE TABLE IF NOT EXISTS nous_system._backfill_20260724_rule_recat AS
SELECT id, category, subject FROM heart.facts
WHERE agent_id='nous-default' AND category='rule' AND source IN ('reflection','sleep_reflection');
```

4. Apply (also stamp subject on the subject-less reflection rows):

```sql
UPDATE heart.facts SET category='technical', subject=COALESCE(subject, 'lesson_learned')
WHERE agent_id='nous-default' AND category='rule' AND source='reflection';
UPDATE heart.facts SET category='technical'
WHERE agent_id='nous-default' AND category='rule' AND source='sleep_reflection';
```

(No active-only filter: inactive mislabeled rows are equally wrong; category is not part of any embedding.)

5. Rollback (recorded in memory + PR) — restore exactly from the capture table:

```sql
UPDATE heart.facts f SET category=b.category, subject=b.subject
FROM nous_system._backfill_20260724_rule_recat b WHERE f.id = b.id;
```

Drop the capture table only after the change has soaked (days, not minutes).

6. Post-verify: re-run the Tier-1 top-20 query — expect user_direct rules/preferences/person facts, zero reflection lessons.
