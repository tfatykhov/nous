# Implementation Plan — F080 + §14 (Coherent Ranking + Procedure Selection)

**Spec:** `docs/features/F080-coherent-cross-type-ranking.md` (§8–§14 authoritative).
**Forge:** design decision `a18e0836`.
**Principle:** procedures/censors are not knowledge — remove them from the ranked recall pool (F080); surface procedures via a name-menu + graph-primary critic-fallback selection that preloads bodies (§14). All behavior flag-gated, default OFF, byte-identical when off.

This plan is split into **two independent, flag-gated PRs**. Both can merge separately; the operator flips prod flags only after both land (so excluding procedures from recall never precedes the menu being in place).

---

## PR 1 — F080: knowledge-only recall pool (small, low-risk)

**Goal:** when `NOUS_COHERENT_RANKING_ENABLED=true`, `recall_deep` ranks only facts/episodes/decisions/chunks — censors and procedures are excluded from the pool. Default OFF ⇒ byte-identical to the committed snapshot.

### Files & changes
1. **`nous/config.py`** — add `coherent_ranking_enabled: bool = Field(default=False, ...)`. Add a `RuntimeConfig.get_coherent_ranking_enabled(settings)` resolver mirroring `get_cross_encoder_enabled` (so the hot path reads the resolver, not raw settings — matches decision 78ef3a3d's lesson about runtime-config bypass).
2. **`nous/heart/heart.py` `_recall` (~877)** — when the resolver is true, strip `"censor"` and `"procedure"` from `search_types` before `search_map` is built. Leaves the `else`/OFF path byte-identical.
3. **`nous/api/retrieval_pipeline.py` (~340-347)** — when `getattr(settings,"coherent_ranking_enabled",False)`, strip `"censor"` and `"procedure"` from `heart_types` before the `heart.recall` call. (Both sites: pipeline governs the explicit `types=` passed to recall; heart.py:877 governs the `types=None` default — gate both.)
4. **`nous/api/retrieval_pipeline.py` PipelineStats (~295 + dataclass def)** — add `coherent_ranking_applied: bool = False`, set from the flag. (Partial B5; full CE/MMR stat threading stays F080.1.)

### Tests — `tests/test_coherent_ranking.py` (postgres_only)
- Flag ON: seed facts+episodes+decisions+procedures+censors for the eval agent; assert `recall_deep` result contains **no** `type in {"censor","procedure"}`, and still returns facts/episodes/decisions.
- Flag OFF: assert procedures+censors still present (today's behavior) — guards the byte-identical contract.
- `coherent_ranking_applied` reflects the flag.

### Validation
- `uv run pytest tests/test_coherent_ranking.py -v` (+ the existing recall snapshot test with flag default OFF).
- Confirm no other `heart.recall(...)` caller depends on procedure/censor results (grep callers; the cognitive path uses per-type `search_*`, not `recall` — verify).

### Risk
- Low. Additive, flag-gated. The only trap: a caller that passes `types=None` and expects procedures — gate the heart.py:877 default on the flag so that path is unchanged when OFF.

---

## PR 2 — §14: cognitive procedure injection (graph-primary selection + name-menu + body-preload, drop Track B)

**Goal:** the every-turn system prompt surfaces procedures via (a) a full-breadth **name-menu**, (b) a **Recommended Procedures** section whose bodies are **preloaded**, chosen by **graph-primary K-line activation with critic fallback** (§14.7), and (c) `get_procedure` for on-demand depth. The cosine **Track B** embedding injection is removed.

### Flags (`nous/config.py`)
- `proc_selection_graph_primary: bool = False` — master switch for §14 behavior. OFF ⇒ today's behavior (critic name-pointer + catalog + Track B fallback) byte-identical.
- Reuse existing `critic_skill_slots` (slot budget), `proc_catalog_*` (menu).
- `proc_recommended_body_max_chars: int = Field(default=1200, ...)` — per-procedure preload cap.
- `proc_graph_neighbors_per_seed: int = Field(default=2, ...)` — K-line fan-out per recalled seed.

### Changes — `nous/cognitive/context.py` procedure section (~630-796)
All gated on `proc_selection_graph_primary`; OFF path runs the existing code verbatim.

1. **Selection (§14.7), new helper `_select_procedures(...)`** invoked in the procedure step (which already runs *after* facts/episodes/decisions are recalled, so seeds are in hand):
   - **Primary (graph K-line):** collect seed IDs from the already-recalled facts + decisions + episodes (with their recall scores). For each seed call `brain.neighbors(seed.id, node_type=seed.type, neighbor_type="procedure", limit=proc_graph_neighbors_per_seed)`. Score each activated procedure `edge_weight × seed_recall_score`; aggregate max per procedure; dedup; sort desc; take top `critic_skill_slots`. (Reuses the exact `brain.neighbors` traversal F080's pipeline uses — structural, not cosine.)
   - **Fallback (critic):** if fewer than `slots` selected, fill the remainder from the existing Track A `critic_skills` (already fetched by name).
   - **Floor:** if still empty and the input has an obvious name/intent match against the menu, include it. (Cheap; optional in v1 — at minimum the menu remains.)
   - Returns a list of `ProcedureDetail` (full objects) capped at `slots`.
2. **Body-preload** — replace the name-pointer block (~762-780) with `_format_procedures(selected)` truncated to `proc_recommended_body_max_chars` per item, in the "Recommended Procedures" section (dynamic tier). Bodies, not pointers.
3. **Name-menu** — the catalog (~265-364) renders **all active procedures** as `name — ≤60-char purpose`, ordered by utility/effectiveness. Drop `proc_catalog_desc_chars` to ~60; ensure all ~55 fit (raise `proc_catalog_max_chars` only if needed after compacting). Static tier (cache-friendly).
4. **Drop Track B** — delete the `embedding_procedures` branch (~686-744) under the flag; OFF path keeps it. (It is already inert in prod, gated `not catalog_rendered`.) Removing it also deletes a procedure-path instance of the boost-sort hazard.
5. **recalled_ids bookkeeping** — selected procedures' IDs go into `recalled_ids["procedure"]` (after selection, before any truncation — avoid the B-cog-A class bug).

### Tests — `tests/test_procedure_selection.py` (postgres_only)
- Graph-primary: seed a fact with a `summarized_by`/`related_to` edge to procedure P; recall surfaces the fact; assert P is selected and its **body** appears in "Recommended Procedures".
- Fallback: seed with no procedure edges + a critic skill → assert critic pick fills the slot.
- Name-menu: ≥ `proc_catalog_max`/char-cap procedures → assert every active name appears in the menu (full breadth) under the compact format.
- OFF path: `proc_selection_graph_primary=False` → existing name-pointer behavior unchanged.
- Determinism + body cap respected.

### Validation
- `uv run pytest tests/test_procedure_selection.py tests/test_context*.py -v`.
- Local: build a context for a seeded agent, eyeball the menu + recommended sections.
- Measurement hooks (log-only in v1): graph-primary hit-rate (did K-line fill ≥1 slot) so the driver mix (§14.7) is observable post-deploy.

### Risk
- Medium. Touches the every-turn context builder. Mitigated by the master flag (OFF = verbatim today) and the existing `try/except` around the procedure section (context.py:795).
- Watch: `brain.neighbors` cost per seed × seeds/turn — bound by `proc_graph_neighbors_per_seed` and only over already-recalled seeds (≤ ~10). One batched query preferred over N round-trips if the helper supports it.

---

## Sequencing & review
1. **Plan review** (this doc) — 3 agents: architecture, devil's-advocate, python-pro. Fold P1/P2 before coding.
2. **PR1 first** (small, clean) → impl → 3-agent impl review → fix → PR → codex → merge.
3. **PR2** → impl (helper + context rewrite + tests) → 3-agent impl review (incl. silent-failure-hunter) → fix → PR → codex → merge.
4. F051 probes (chunk-vs-fact, graph-vs-direct) ride with PR1 as eval-harness additions if cheap; else a follow-up. Not a merge blocker for the flag-gated code.

## Out of scope (deferred, per spec)
F080.1 (S2 chunk/graph cross-distribution coherence, CE head, B3 embed-fail, full CE/MMR stat threading); the broader cognitive ordering fixes B-cog-A/B/C/D except where PR2 naturally fixes the procedure-path instance.

---

## Plan v2 — review fixes (3-agent, 2026-06-08) — AUTHORITATIVE over the above where they differ

### PR1 (simplified — surgical, recall_deep-only)
- **Strip at ONE site only:** `retrieval_pipeline.py:340-347` — when `settings.coherent_ranking_enabled`, drop `"censor"` and `"procedure"` from the explicitly-built `heart_types`. **Do NOT touch `heart.py:877`** (the default-list strip created blast radius: `mcp.py:278` passes `types=["procedure"]`, `mcp.py:263` is the external `nous_recall("all")` contract, `censor_actions.py:90` recalls censors via `types=None`). Pipeline-only = recall_deep-only, zero blast radius. `nous_recall("all")` intentionally keeps procedures/censors (separate external surface).
- **No resolver.** `coherent_ranking_enabled` is a deploy-time flag; read `settings.coherent_ranking_enabled` directly. (Adding a half `RuntimeConfig` resolver without DB-key/load/persist/REST backing is dead indirection — arch P2-C, py P2-4.)
- `config.py`: `coherent_ranking_enabled: bool = Field(default=False, ...)`.
- `PipelineStats`: add `coherent_ranking_applied: bool = False` (dataclass at `retrieval_pipeline.py:74`; set at `:295`). Not formatted into text → snapshot-safe.
- **Test:** real `postgres_only` test (NOT the mock `test_format_matches_committed_snapshot` at `test_retrieval_pipeline.py:736`, which mocks `heart.recall`). Seed facts/episodes/decisions/procedures/censors; ON ⇒ no `type in {censor,procedure}` in `recall_deep`; OFF ⇒ both present.
- LOC ~80-120. Ships first.

### PR2 (revised — the body path is the redesign)
- **Body data path (the kill-shot fix):** `_select_procedures` gets K-line procedure **IDs** from `brain.neighbors(seed_id, node_type=seed_type, neighbor_type="procedure", limit=N, session=session)`; for each selected ID call `heart.get_procedure(id)` → `ProcedureDetail`; render via a **NEW** `_format_procedure_bodies(details, per_item_cap)` that assembles `description + core_patterns + implementation_notes + core_tools` per procedure, capped at `proc_recommended_body_max_chars` **per item**. Stop citing `_format_procedures` (renders no body) and `ProcedureDetail.body` (does not exist; fields at `schemas.py:244-267`).
- **Active/superseded filter:** the neighbor→object fetch MUST drop inactive/superseded procedures. Verify `heart.get_procedure(id)` (`heart.py:474`) filters `active AND superseded_by IS NULL`; if not, add it. (`brain.neighbors` itself does not filter — `brain.py:1276` — and post-dedup there are archived skills with live `auto_linked` edges. Without this, PR2 resurrects dead skills.)
- **Seeds = facts + decisions ONLY.** At the procedure step (`build()` step 7, `context.py:630`), `recalled_ids["episode"]` is empty (episodes are step 8, `:836`). Use `recalled_ids["fact"]` + `recalled_ids["decision"]` with `recalled_score_map`; `UUID(...)`-convert the str ids. TODO note: episode-cueing needs episode recall reordered before step 7 (don't — perturbs section priority + OFF byte-identity) or a separate episode-seed pass once proc↔episode edges exist.
- **Hot-path cost:** thread `session=session` into every `brain.neighbors`; **sequential `await`** (AsyncSession is not `gather`-safe — same reason `_recall` is sequential, `heart.py:880`); **cap seeds to top-5 by recall score** before the loop. Batched `source_id IN (:ids)` variant is the F080.1 follow-up.
- **Determinism:** add `ORDER BY weight DESC, <id>` to `_neighbors` union before `LIMIT` (`brain.py:1212` currently unordered → nondeterministic over-limit). Benefits F080 graph expansion too.
- **Catalog name-menu:** order by **name** (alpha) or `created_at` — NOT effectiveness (`_compute_effectiveness` is Python-computed, can't SQL-order, and per-use mutation busts the F036 **static** cache tier, `context.py:36/260`). Compact via a **flag-local** desc cap (new `proc_menu_desc_chars`, ON-path only) — do NOT change `proc_catalog_desc_chars` default (byte-identical OFF). Budget: 55 × ~100ch ≈ 5-6K > `proc_catalog_max_chars=4000` → cut purpose to ~30ch and/or raise the cap under the flag; acceptance = every active name present.
- **recalled_ids AFTER `_truncate_to_budget`** (`context.py:617` pattern) — the plan's "before truncation" was the B-cog-A bug inverted.
- **Land dark + measure-gate:** flag `proc_selection_graph_primary` default OFF. Add log-only metrics: graph-primary hit-rate (K-line filled ≥1 slot) and selected-proc set. **Forbid prod flip until critic-recall@2 + hit-rate land** (spec §14.5 gate; §14.4 token cost vs F079's measured saving).
- **Startup interlock guard:** WARN if `coherent_ranking_enabled=true` while the full-breadth menu isn't active (procedures excluded from recall + truncated menu = halved discovery).
- **Signatures:** `RuntimeConfig` is `nous/runtime_config.py` (singleton `.get().method()`); fetch is `heart.get_procedure(id)` / `heart.get_procedure_by_name(name)` (no `get_procedure_by_id`).
- LOC ~300-450 (Medium-Large). Ships after PR1.

### Resolved open questions
Q1 two PRs (PR1 first). Q2 inline `_select_procedures` on `ContextEngine`, cap seeds top-5, batched variant deferred. Q3 floor deferred (menu+critic+get_procedure suffice). Q4 order by name (cache-safe). Q5 PR1 pipeline-only ⇒ no non-recall_deep caller affected.

## Open questions for the review team (original — resolved above)
1. PR1 + PR2 as two PRs vs one stacked branch? (This plan assumes two.)
2. `_select_procedures` placement — inline in `build()` step 7 vs a new module; and whether `brain.neighbors` should gain a batched multi-seed variant to avoid N queries/turn.
3. Floor (task-only match) — ship in v1 or rely on menu + critic only?
4. Name-menu ordering signal — utility (F037 effectiveness) vs recency vs alpha?
5. Does any non-recall_deep caller of `heart.recall` break under PR1's procedure/censor exclusion?
