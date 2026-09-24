# User Profile Core/Intent Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Split the User Profile into (a) a small curated **always-on core** — universal hard facts (Celsius, formatting rules, contacts) that apply in every session — and (b) an **intent-selected Session Profile leg** that surfaces domain facts (trading stances, sailing contacts, project conventions) only when the turn is about that domain. Owner-defined criterion (2026-07-24): "hard facts like Celsius always there; trading/sailing only for very specific questions." Enforcement-grade rules stay in CENSORS (verified: 2 active refuse censors already cover the trading rule — the profile copy is informational).

**Why:** today's Tier-1 selection is confidence+recency top-20 with zero query awareness, and tier-1 categories are EXCLUDED from Tier-3 search (context.py:711) — so the other ~327 profile facts can never appear anywhere. Recency must never promote a sailing forecast into every prompt.

**Cache design:** core section stays `semi_stable` (curated membership → changes rarely). The intent leg is `dynamic` — that region is per-turn and never part of the cached prefix, so per-session variation is cache-free. This is the "separate prompt item we can modify without breaking cache for a broader section".

**Verified seams (this session):** facts have a `tags` ARRAY (no migration needed); `FactManager.search/_search` already supports `exclude_categories` (facts.py:3140-3193) — an `include_categories` param is the symmetric extension; `SECTION_TIERS` + `SECTION_MARKERS` registration points known; dashboard Identity view (PR #570) is the curation surface; `_fresh_engine` test helper exists.

## Review revisions folded in (devil, 2026-07-24 — cache claim VERIFIED against runner.py: dynamic block sits after the single cache breakpoint, never cached; semi_stable busts only on membership change)

- **P1 probation window:** core render = {tagged} ∪ {untagged tier-1 facts with `learned_at` within `NOUS_PROFILE_CORE_PROBATION_DAYS` (default 14)}, tagged first, capped at `profile_core_limit`. A universal fact learned tomorrow appears immediately and ages out unless tagged — no silent vanish, owner curates at leisure.
- **P2 leg quality:** the Session Profile leg reuses `_apply_relevance_filter(results, "fact")` + `_apply_staleness_penalty` before formatting — hybrid RRF always returns something; without the floor, marginal facts inject on every domain-ish turn.
- **P2 dedup-vs-core:** core-TAGGED facts BYPASS the identity per-line dedup — an explicit human tag outranks a word-overlap heuristic (otherwise tagging Celsius core while identity mentions Celsius makes the tag silently inert). Probation (untagged) facts still dedup normally.
- **P3:** `profile_core_tag` becomes a module CONSTANT (`PROFILE_CORE_TAG = "profile_core"`), not a setting (a runtime-changeable tag string orphans existing tags). `GET /profile/facts` gains `?core=true` filter for auditing the core set.
- **PROD PRECONDITION (verified this session, must fix before flip):** the censors budget (300 tokens = 1,200 chars) cannot hold the enforcement surface — prod has 1 abort (54ch) + 4 refuse censors (2,496ch, ALL trading-related) and `_truncate_to_budget` char-slices mid-censor, so ~2 of 4 refuse censors (including the Jun 4 explicit sell-underwater instruction) do NOT render today. Before flipping core: (a) raise `"censors"` in `NOUS_CONTEXT_BUDGET_OVERRIDES` to ~800, AND/OR (b) consolidate the 4 overlapping trading censors to 1-2 via the dashboard/API. ALSO: switch the censors section to `_truncate_to_budget_lines` (never cut an enforcement rule mid-sentence) — small flagless fix bundled in Task 2.

## Design decisions (locked)

1. **Core membership = explicit tag** `profile_core` on the fact (curated, never inferred — cache stability comes from curation). Rendering: when `NOUS_PROFILE_CORE_ENABLED=true` AND ≥1 tagged fact exists → the User Profile section renders ONLY tagged facts (cap `NOUS_PROFILE_CORE_LIMIT=12`, same confidence/learned_at order, same dedup/exclude-sources pipeline). **Fallback:** flag on but zero tagged facts → legacy top-N behavior (no fresh-agent cliff). Flag off → byte-identical legacy.
2. **Session Profile leg** (`NOUS_PROFILE_INTENT_LEG_ENABLED`, default False, land-dark): hybrid search restricted to `include_categories=TIER1_FACT_CATEGORIES`, using the same query text the Tier-3 fact leg used, limit `NOUS_PROFILE_INTENT_LEG_LIMIT=5`, budget `NOUS_PROFILE_INTENT_LEG_BUDGET=300` tokens, line-aware truncation, rendered as a NEW section `## Session Profile` registered `dynamic` in `SECTION_TIERS` and added to the observability `SECTION_MARKERS`. Excludes fact IDs already rendered in the User Profile section (no double-injection). Respects `profile_exclude_sources` (rule-scoped, same helper semantics) — reflection lessons must not sneak back via this leg... NOTE: exclude_sources in `list_by_category` is tier-1-rule-scoped; for the SEARCH path add nothing — search already excludes nothing by source, but the leg only touches tier-1 categories which are now clean; document rather than over-engineer.
3. **Curation surface:** `POST /facts/{fact_id}/core` body `{"core": true|false}` → new `Heart.set_fact_tag(fact_id, tag, present)` (ORM tags-array add/remove, ValueError→404, Tier-1 target guard mirroring #570's endpoints). Dashboard Identity view: core badge in the facts table + a Core on/off toggle in the row detail.
4. **Initial prod core set** (post-merge tagging; owner adjusts in dashboard): Celsius; EMAIL HTML single-renderer rule; Drift Digest delivery routing; "tomorrow"-resolution rule; workspace persistence rule; corporate-base-images rule; sailing contact email correction (person). NOT tagged: trading rules (censor-backed, domain), sailing/marina anything, bib/whitepaper convention, Ollama routing (borderline — owner call), neuroplasticity principle (recommend relocating to identity `values` section instead — it is Nous's own value, not user data).
5. **Both flags land dark.** Flip after the pending prod deploy, verified via the #569 instrumentation log + `context_log.sections_present` showing `session_profile`.

## Global Constraints

- Branch `feat/profile-core-intent-split`, fresh worktree off origin/main. cd + branch-verify before edits. Gate and commit are SEPARATE commands (no `pytest | tail && commit`).
- `NOUS_TEST_DB=postgres` for tiered-context/facts tests; fresh-agent isolation (`_fresh_engine`) for any presence/count assertion.
- Flag-off byte-identity for BOTH flags, pinned by tests.
- CLAUDE.md env rows for all five new settings; REST table row for the core endpoint.
- Frontend gates: `npm test && npm run check && npm run build` from dashboard-app/.

---

### Task 1: Core selection (backend)

**Files:** `nous/config.py`, `nous/heart/facts.py` (`list_by_category`/`_list_by_category`), `nous/heart/heart.py` (wrapper), `nous/cognitive/context.py` (User Profile block), `tests/test_tiered_context.py`.

- Settings: `profile_core_enabled: bool = False`, `profile_core_limit: int = 12 (ge=1)`, `profile_core_probation_days: int = 14 (ge=0; 0 disables probation)`. Tag = module constant `PROFILE_CORE_TAG = "profile_core"` (in `nous/cognitive/context.py` next to TIER1_FACT_CATEGORIES, imported where needed — NOT a setting).
- `_list_by_category` gains `require_tag: str | None = None` and `learned_within_days: int | None = None` (probation query) → tags filter via the ARRAY membership idiom (verify `Fact.tags.any()` vs `text(":t = ANY(tags)")` against an existing precedent). Threaded through wrapper, defaults None (byte-identical callers).
- context.py User Profile block when `profile_core_enabled`: core = tagged facts (require_tag) + probation facts (untagged, learned within probation_days), tagged first, deduped by id, capped at `profile_core_limit`; TAGGED facts bypass the identity per-line dedup (probation facts dedup normally); if the combined set is empty → legacy fallback. Instrumentation log gains `core=%s tagged=%d probation=%d`. **Expose `profile_fact_ids: set[str]` initialized to empty BEFORE the try-block and populated with the RENDERED facts' ids (correctness-P2-2 — the block may not run at budget 0/exception; these ids are the only real double-injection guard for Task 2 since recalled_ids["fact"] holds only non-tier-1 facts).** Thread `require_tag`/`learned_within_days` explicitly through `Heart.list_facts_by_category` (heart.py:419 wrapper — correctness-P3-3).
- Tests (fresh-agent): tagged-only render; probation fact (fresh learned_at) appears untagged, old untagged fact does not; tagged fact with identity-covered content still renders (dedup bypass); zero-tagged+zero-probation falls back to legacy; flag off byte-identical; cap respected with tagged-first priority.

### Task 2: Session Profile intent leg

**Files:** `nous/heart/facts.py` (`search`/`_search` + `include_categories`), `nous/heart/heart.py` (`search_facts`), `nous/cognitive/context.py` (new leg + `SECTION_TIERS`), `nous/observability/context_logger.py` (`SECTION_MARKERS`), `nous/config.py`, `tests/test_tiered_context.py`.

- `include_categories: list[str] | None = None` on the search path, symmetric to `exclude_categories` (same placeholder-binding style, facts.py:3189-3193). Default None → byte-identical.
- New leg in `build()` AFTER the Tier-3 facts section: gated on `profile_intent_leg_enabled` (own budget setting, independent of budget.user_profile); query text = **`_default_query`** (correctness-P2-1: the Tier-3 fact leg uses `_query_texts.get("fact", _default_query)` at context.py:707, where `_default_query` prepends current_topic — mirror EXACTLY, not `input_text`); `include_categories=TIER1_FACT_CATEGORIES` (**plain `IN`, no NULL guard** — null-category rows are correctly excluded from an include-list, unlike exclude's NULL-safe form); **pipeline parity (devil-P2): run `_apply_staleness_penalty` then `_apply_relevance_filter(results, "fact")`** before exclusion/formatting; drop results whose IDs are in `profile_fact_ids` (Task 1) — recalled_ids["fact"] exclusion is a documented no-op (category-disjoint) and may be included only as cheap belt-and-braces; format via `_format_facts`, truncate via `_truncate_to_budget_lines`, append `ContextSection(priority=6, label="Session Profile", ...)`. **priority=6 TIES Relevant Facts (context.py:777) — stable sort renders it immediately after; document the tie with a comment mirroring the priority-1 tie note (context.py:257)** (correctness-P3-1). Register `"Session Profile": "dynamic"` in SECTION_TIERS and `"## Session Profile": "session_profile"` in SECTION_MARKERS. Intent-leg tests are **postgres_only** and assert MEMBERSHIP in the section (not rank — RRF fuses an orthogonal mock-vector leg, rank is probabilistic); use a globally unique token in content+query so the FTS leg drives the match.
- **Bundled flagless fix (devil prod finding):** the Active Censors section switches from `_truncate_to_budget` to `_truncate_to_budget_lines` (context.py:599) — an enforcement rule must never be char-sliced mid-sentence. Pin with a test (over-budget censor list drops whole lines).
- Tests (fresh-agent): flag on → a domain fact (e.g. trading rule) NOT in the core appears in Session Profile when the input mentions trading, and does NOT appear for an unrelated input (mock-embedding caveat: use FTS-matchable distinctive tokens in content+query so the hybrid keyword leg drives the match deterministically); double-injection excluded (fact in core never repeats in Session Profile); flag off → section absent, byte-identical.

### Task 3: Core toggle endpoint + dashboard

**Files:** `nous/heart/facts.py` (`set_tag`), `nous/heart/heart.py` (`set_fact_tag`), `nous/api/rest.py` (handler + route `POST /facts/{fact_id}/core` BEFORE the param-less routes per convention), `tests/test_profile_facts_api.py`, `dashboard-app/src/views/Identity.svelte`, `dashboard-app/src/lib/types/api.ts`.

- `FactManager.set_tag(fact_id, tag, present, session=None)`: load ORM fact (ValueError if missing), add/remove tag in the ARRAY (idempotent), flush. Heart wrapper mirrors deactivate_fact.
- REST handler: UUID parse (400) → `get_current_fact` target guard — 404 missing / **409 `already_superseded` on id-mismatch (correctness-P3-2, mirroring update_fact rest.py:600-604)** / 409 `not_profile_fact` — → set/unset `PROFILE_CORE_TAG` → `{"status": "core_set"|"core_unset"}`.
- `GET /profile/facts` gains `?core=true` (filters to `require_tag=PROFILE_CORE_TAG`) for auditing the core set (devil-P3).
- Tests: toggle on/off round-trip visible in GET /profile/facts tags; `?core=true` returns only tagged; non-tier-1 target 409; unknown 404.
- Dashboard: `BrowserFact.tags` already returned — core badge (`tags.includes('profile_core')`) in the table via a precomputed display field, and a "Core: on/off" toggle button in the detail snippet calling the endpoint (apiSend POST), refresh-after-save idiom.

### Task 4: Docs + suites + PR + codex

- CLAUDE.md env rows (5 settings) + REST row. Full-suite diff vs baseline. PR body: the partition criterion (owner-defined), censor-enforcement note (trading verified), cache design (semi_stable core / dynamic leg), both flags dark + flip plan, deploy dependency (stacks on the already-pending deploy).

### Task 5 (post-merge, prod): initial tagging + censor budget + (post-deploy) flips

1. Tag the initial core set (design decision 4) via SQL `UPDATE heart.facts SET tags = array_append(tags, 'profile_core') WHERE id IN (...)` after matching by content anchors — capture affected IDs in session notes; idempotent guard `NOT ('profile_core' = ANY(tags))`.
2. **Censor enforcement precondition (must precede the flip):** raise `"censors"` to ~800 in prod `NOUS_CONTEXT_BUDGET_OVERRIDES` (today 4 trading refuse censors total 2,496 chars vs 1,200-char budget → ~2 don't render, including the Jun 4 explicit instruction) AND/OR consolidate the 4 overlapping trading censors to 1-2. Verify all abort+refuse censors fit the rendered section afterward.
3. Recommend relocating the neuroplasticity principle to identity `values` (append via update_section) — owner call.
4. AFTER the prod deploy lands: flip `NOUS_PROFILE_CORE_ENABLED=true` + `NOUS_PROFILE_INTENT_LEG_ENABLED=true`; verify via `context_log.sections_present` (expect `session_profile` on domain turns) and the User Profile instrumentation log (`core=True tagged=N probation=N`).
