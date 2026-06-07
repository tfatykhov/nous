# F079 — Memory Delivery Overhaul (procedures + episodes + pull path)

**Status:** SPEC v2 (rewrite). Supersedes the original `F079-effective-procedure-injection.md` (kept for
history; its §12 Step-0 result + §11 review carry forward). **Every claim here is code-verified this
session** (`docs/reviews/memory-injection-validation-2026-06-06.md`); do not trust the earlier
injection-only framing or any `context_log`-derived numbers.
**Date:** 2026-06-06.

---

## 1. The architecture is sound — fix the leaks, don't fight it

Nous delivers memory two ways (both read in code):
- **Passive pre-turn injection** (`ContextEngine.build`, `context.py:114-710`): auto-loads the cheap,
  always-useful types — **facts + decisions** (~reliably), user-profile, censors, frame, working-memory,
  and recent-episode **titles**.
- **Active pull** (`recall_deep` → `run_recall_pipeline`; `get_procedure`): the agent searches and loads
  on demand. `recall_deep` defaults `memory_types=["all"]` (`tools.py:648`) → reaches **all** types
  including procedures, episodes, chunks.

This split (auto-inject light types; pull the heavy/situational ones) is **defensible**. Step 0 proved
the agent **follows a procedure body precisely when it obtains it** (6/6, naming the procedure) and that
the working channel is **pull, triggered by a cue**. So the overhaul **strengthens pull + fixes the
specific leaks**, and does NOT force full procedure bodies into the system prompt (the demoted old
Phase A).

## 2. The defensible, code-verified bugs (what we're fixing)

| ID | Bug | Code | Severity |
|---|---|---|---|
| **B-telemetry** | Procedure/episode delivery has **no working telemetry**: `loaded_procedures`/`loaded_episodes`/`recent_conversations` counters never assigned (`context_logger.py:167-188`); `parse_system_sections` `SECTION_MARKERS` has **no "## Known Procedures"/"## Past Episodes"** marker (`context_logger.py:16-31`) so those keys never appear. Hid the bugs; caused two wrong measurements. | `context_logger.py:16-31,167-188` | **HIGH (prereq)** |
| **B-conv** | The **conversation/default frame** (the fallback for all unmatched + unseeded-agent traffic, `frames.py:199`) **hard-zeros procedures + episode-summaries** via TWO sources: frame budget 0 (`schemas.py:124`) AND the intent override `{procedures:0, episodes:0}` applied last (`intent.py:220-226` via `context.py:159`). Runtime-proven: forced budget=2000+floor=0 was still re-zeroed. | `schemas.py:124`, `intent.py:224-225`, `context.py:159` | **HIGH** |
| **B-floor** | Procedures alone carry a **0.40 score floor** (`context.py:535`, `config.py:132`) that runs **before** the min-k relevance rescue that protects facts/decisions/episodes (`context.py:777`). Prod cosine probe (6 real task queries × 62 prod procedures): top procedure scores `0.317/0.412/0.626/0.334/0.264/0.482` → **3 of 6 had nothing ≥0.40**; facts scored 0.45–0.69. So procedures are throttled to empty far more often than facts. | `context.py:535-542`, `config.py:132` | **MEDIUM** |
| **B-pull-thin** | `recall_deep` returns a procedure as **`name: description` only — no `implementation_notes`/body** (`heart.py:1122`); decisions drop `pattern` + never load `context` (`tools.py:389`); episodes are lossy summaries. (`get_procedure` does return the full body — `tools.py:1024` — but it's a 2nd round-trip the agent rarely makes.) | `heart.py:1122`, `tools.py:389` | **HIGH** |
| **B-pull-reinforce** | Pulled memories (recall_deep/get_procedure) **never enter `recalled_*`** → invisible to post-turn reinforcement (`tools.py:693-793`, `layer.py:1096-1114`). The stronger relevance signal (agent chose to pull) teaches the system nothing; passive injection does. | `layer.py:1096-1114` | **HIGH** |
| **B-aware** | The agent doesn't pull procedures unless cued (Step 0: cued→6/6, uncued→0; prod `get_procedure` 1 vs `recall_deep` 88). Nothing tells it which procedures exist/apply. | (design gap) | **MEDIUM** |

Out of scope / not bugs (code-verified): chunks-absent-from-passive-injection is **by-design deferral**
to `recall_deep` (only a gap if we decide passive chunk awareness is wanted); the prod budget override
does NOT zero procedures (`.env.prod-snapshot`); `critic_mode=advised`+`critic_skill_injection=enabled`
in prod (the critic path is live — the earlier "shadow/disabled" reading was the *code default*, not prod).

## 3. Design — strengthen pull, repair the injection leaks, restore telemetry

### Phase 0 — Telemetry first (HARD PREREQUISITE; no behavior change)
You cannot tell if any fix works while the signals are blind. Pure-win, ship first.
- Add `"## Known Procedures": "known_procedures"` and `"## Past Episodes": "past_episodes"` to
  `SECTION_MARKERS` (`context_logger.py:16-31`).
- Populate `loaded_procedures`/`loaded_episodes`/`recent_conversations` in the entry constructor
  (`context_logger.py:159-188`) by counting their parsed section content (mirror the facts/decisions
  `count("\n-")` heuristic).
- **Acceptance:** a turn that injects a procedure section shows `loaded_procedures>0` AND
  `'known_procedures' ∈ sections_present`. (PG-required test — see §5.)
- Flag: none (observability fix). This is also the **measurement substrate** for every later phase.

### Phase 1 — Strengthen the PULL path (Step 0's proven channel; highest leverage)
- **P1.1 recall_deep returns usable bodies.** `heart._to_recall_result` (`heart.py:1122`): procedures
  return `name` + `description` + a truncated slice of `implementation_notes`/`core_patterns` (enough to
  act on or judge relevance). Decisions: include `pattern` (`tools.py:389`). Episodes: when
  `episode_chunks_enabled`, prefer chunk content (already wired) else summary. Truncate on `\n`
  boundaries with a `[truncated]` marker (never mid-step). Flag: `NOUS_RECALL_FULL_BODIES` (default off,
  dark-launch).
- **P1.2 Reinforce pulls.** Thread `recall_deep`/`get_procedure` result IDs into a reinforcement hook so
  `usage_tracker` + procedure activation/outcome see them (parallel to the injection path at
  `layer.py:1096-1114`; keep `activation_count` semantics per the audit's decouple-rule —
  [[project_procedure_subsystem_audit]] R4). Flag: `NOUS_REINFORCE_PULLS` (default off).

### Phase 2 — Repair the injection leaks (passive backstop on the common path)
- **P2.1 Un-zero the conversation/default frame** for procedures + episodes. Fix **both** sources
  (two-sources-of-truth): give `conversation` a small non-zero procedure + episode budget
  (`schemas.py:124`) AND remove/raise the `intent.py:224-225` override (else the override re-zeros —
  `context.py:159`). Budget kept modest (awareness-sized, not full-menu). Flag: `NOUS_CONV_FRAME_PROC_EPISODE` (default off).
- **P2.2 Procedure score-floor parity.** Either route procedures through the same min-k relevance rescue
  the other types get (`context.py:777`) instead of the pre-filter floor, or lower
  `NOUS_PROCEDURE_SCORE_FLOOR`. Measure the procedure-cosine distribution (probe showed ~0.40) before
  picking the value — do not guess. Flag: tune `NOUS_PROCEDURE_SCORE_FLOOR` (exists) + a parity toggle.
- **P2.3 (optional) Frame-seeding hardening** — make the empty-`brain.frames` fallback log loudly / not
  silently degrade every unseeded agent to procedure-blind `conversation` (`frames.py:199`; `seed.sql`
  only seeds `nous-default`).

### Phase 3 — Awareness layer (cue the pull)
- **P3.1** A lightweight, cached **awareness cue** — a short "procedures available: {names + one-line
  when-to-use}" hint so the agent knows what to pull (Step 0: cue→pull→follow). This is the *only*
  surviving piece of the old "catalog," and it's awareness-sized, not the full body. Reuse `description`
  as the when-to-use text (not a new field — [[project_procedure_subsystem_audit]] R8); recompute from DB
  per build (no memoization). Flag: `NOUS_PROC_AWARENESS_CUE` (default off). Cache-tier decision deferred
  to review (the old R3 dedicated-cache-block concern applies if it grows).

### Gate (carries over, advisor #3 / R1)
A positive Step 0 validated the **ceiling** (the agent follows a delivered body). Before any prod flip,
add a **selection-accuracy** check: does the production ranker actually surface/pull the procedure a human
judges correct, often enough? Measure on the eval instance (with Phase 0 telemetry live) per phase.

## 4. Sequencing & flags
**Phase 0 (telemetry) → Phase 1 (pull: P1.1+P1.2) → Phase 2 (injection leaks) → Phase 3 (awareness) →
selection-accuracy gate before prod.** Each phase independently shippable, behind its own flag, default
OFF, dark-launch per Nous convention. Phase 1 is highest leverage (pull is the proven channel); Phase 0
is the prerequisite that makes all the rest measurable.

| flag | default | gates |
|---|---|---|
| (none) | — | Phase 0 telemetry (markers + counters) — pure observability |
| ~~`NOUS_RECALL_FULL_BODIES`~~ | — | **REMOVED in §8.2** (catalog-first replaced the recall_deep top-1 body). Superseded by `NOUS_PROC_CATALOG_ENABLED` / `NOUS_PROC_CATALOG_MAX` / `NOUS_PROC_CATALOG_DESC_CHARS`. |
| `NOUS_REINFORCE_PULLS` | false | P1.2 reinforce pulled memories |
| `NOUS_CONV_FRAME_PROC_EPISODE` | false | P2.1 un-zero conversation frame procedures/episodes |
| `NOUS_PROCEDURE_SCORE_FLOOR` (exists) + parity toggle | 0.40 | P2.2 floor parity |
| `NOUS_PROC_AWARENESS_CUE` | false | P3.1 awareness cue |

## 5. Acceptance & test discipline (code-only, PG-required)
- **Phase 0:** PG-required test that INSERTs a turn with a `## Known Procedures` section and asserts
  `loaded_procedures>0` + key present — the kind of test whose absence let the dead-FTS bug ship green.
  **CI must run `NOUS_TEST_DB=postgres`** (SQLite default skips PG-only behavior — the recurring trap).
- **P1.1:** recall_deep result for a procedure contains body text (verbatim, `\n`-truncated), not just
  name+desc; OFF flag → byte-identical legacy output.
- **P1.2:** a pulled procedure increments its activation/outcome via the reinforcement hook; ban mocking
  `recall_deep`/`get_procedure`/`search_procedures` data sources in these tests.
- **P2.1:** in `conversation` frame with the flag on, a relevant procedure's section builds
  (`'known_procedures' ∈ sections_present` — now reliable post-Phase-0); OFF → unchanged.
- **P2.2:** a procedure scoring 0.42 survives to injection (floor) / a relevant procedure survives even
  at 0.35 under parity (min-k), per the chosen approach.
- **Gate:** selection-accuracy measured on the eval instance with real frames seeded (the eval gap that
  blocked the task-frame probe — fix it as part of the harness).

## 6. Risks / notes
- **Don't quote a procedure/episode injection rate until Phase 0 ships** — current telemetry is blind.
- Keep **F037 utility boost in the ranker** (`procedures.py:367-381`, shipped win) — drop effectiveness
  only from any *printed* line, never from ranking ([[project_procedure_subsystem_audit]] R5).
- Reuse the existing `last_activated` (`models.py:544`); only add `last_fired_at` if Phase 1.2's decouple
  needs a distinct "actually-fired" timestamp (R4).
- Eval-instance limitation: it has no seeded frames → everything defaults to `conversation`, which masks
  task/debug behavior. Seed frames in the harness before measuring Phases 2/3 / the gate.
- This rewrite is **coupled to the dedup audit** (`docs/reviews/procedure-subsystem-audit-2026-06-06.md`)
  only for the awareness cue (P3) — a cue listing duplicates is the old problem in a new shape; dedup
  should precede P3, not P0–P2.

## 7. References (all code-verified this session)
`docs/reviews/memory-injection-validation-2026-06-06.md` (the validated map),
`docs/reviews/procedure-delivery-sweep-2026-06-06.md` (the sweep, with retractions),
F079 v1 §12 (Step-0 result). Code: `context.py:114-710`, `intent.py:108-240`, `schemas.py:124`,
`heart.py:1091-1144`, `tools.py:609-1046`, `context_logger.py:16-188`, `layer.py:1096-1114`,
`config.py:132`, `frames.py:199`, `.env.prod-snapshot`.

---

## 8. DECISION — pull-only delivery + static awareness (caching-driven, AUTHORITATIVE)

Supersedes the earlier phased plan (§2-§6) and the prior review-synthesis. Decided 2026-06-07: rather
than measure-first, commit to the **pull path** for procedures and **drop per-turn passive injection**.
Phase 0 (telemetry) already shipped (PR #485). The remaining decision is *which delivery channel to
invest in*, and prompt caching makes it clear-cut.

### Why pull, not passive injection (the caching argument is decisive)
- **Passive injection lives in the system prompt and is query-ranked per turn** → it sits in the
  **uncached `dynamic` tier** and is **re-billed at full price every turn** (even turns that need no
  procedure). Query-ranked + cacheable are mutually exclusive; the only cacheable passive form is a
  *static, query-independent* block (breadth, not bodies).
- **Pull puts content in the messages** (tool results): written once, then **read from cache** on every
  later turn, and **only on the turns that actually pull**. Pay-once + on-demand vs pay-every-turn.
- **Procedures are situational, not always-relevant.** Facts/decisions are broadly useful → worth the
  per-turn passive cost (the existing design already pays it). Procedures matter only when the task
  matches → on-demand pull is the correct cost model. This *validates* the existing architecture's split
  (auto-inject facts/decisions; pull procedures/episodes/chunks).
- **Behavioral evidence (Step 0):** the agent already uses `recall_deep` 88×/38min; when cued it pulls
  and follows the body 6/6; and the **agent selects the right procedure better than the cosine ranker**
  (it knows its task) — which also sidesteps the 0.40-floor/selection problems entirely.

### What we build (one coherent unit) — and what we drop
**DROP** (cache-hostile, redundant with pull): P2.1 conversation-frame procedure budget; P2.2 score-floor
lowering. (They only mattered *if* we kept passive injection.) Passive procedure injection stays as-is
(effectively dormant) — we don't invest in it.

**BUILD:**
1. **Richer `recall_deep` procedure bodies (the depth).** `_to_recall_result` (`heart.py:1122`) returns
   more than `name: description`. **DTO change** (not a one-liner — verified): `ProcedureSummary`
   (`heart/schemas.py:270`) has no body fields, so add optional `core_patterns` / `implementation_notes`
   → populate from the full `Procedure` ORM at `procedures.py:383` (in scope, no extra query/migration)
   → read at `heart.py:1122`. **Keep it to ONE capped, newline-stripped line per procedure** (~200-300
   chars): this survives the runner's SmartCompress of `recall_deep` (string-array head/tail line
   selection) and avoids the embedded-newline/marker issues. Flag `NOUS_RECALL_FULL_BODIES` (default off).
   - SmartCompress note (code-verified): `recall_deep` IS compressed and its original is **not** cached
     (`smart_compress.py:294` — `original_text` only set for non-re-fetchable tools), so multi-line
     bodies would be shredded + unrecoverable. The one-line cap keeps each procedure a single
     head/tail-selectable line → no shredding, no `NON_REFETCHABLE_TOOLS` change needed.
2. **Static awareness directive (the cue).** A small, **fully static** section in the cached `static`
   tier (`SECTION_TIERS`): e.g. "You have learned procedures. When you start a task that may match one,
   call `recall_deep` to retrieve it and follow its steps before acting." Static = written into the
   cached block once, ~free every turn, **never busts** (no per-turn or CRUD change). This is what flips
   the agent from uncued→0 to cued→pull (Step 0). No static *name catalog* in v1 — `recall_deep` is
   query-based and finds the relevant procedure from the task; the directive only needs to remind the
   agent to search. (A static name catalog is a later option if breadth-awareness proves necessary; it
   would need its own cache block to avoid busting `static` on procedure CRUD — deferred.) Flag
   `NOUS_PROC_AWARENESS_CUE` (default off).
3. **Decision `pattern` fix (bonus).** `tools.py:389` already has `pattern` on `DecisionSummary` but
   doesn't print it — print it. No schema, no flag, ships in the same PR.

### Deferred (explicit follow-ups, not this PR)
- **P1.2 — reinforce pulls.** Today recall_deep/get_procedure results are invisible to post-turn
  reinforcement. Worth doing, but it's a *learning* concern (orthogonal to the delivery decision) and
  carries real complexity: must **decouple from `activation_count`** (which gates F037 + retire/star) and
  add a tool-loop→post_turn channel (contextvar like F071 `CURRENT_TURN_EXCLUDE_IDS`, threading
  `recalled_content_map`). Separate PR.
- Static name-catalog awareness (own cache block) — only if the directive alone proves insufficient.

### Acceptance / validation
- Flags off ⇒ byte-identical (recall_deep returns today's `name: description`; no awareness section).
- `NOUS_RECALL_FULL_BODIES` on ⇒ a procedure pulled via `recall_deep` carries a one-line capped body
  (verbatim slice, newline-stripped) — PG-required test (search path), ban mocking the data source.
- `NOUS_PROC_AWARENESS_CUE` on ⇒ a static `## ...` section appears in the `static` tier; byte-stable
  across turns.
- Live: on the eval instance, a task turn with the cue on → agent calls `recall_deep` → gets a usable
  body. (Grade the pull + body, not prose.)
- **Caching invariant:** nothing new added to the per-turn `dynamic` tier; the awareness cue is static
  (cache-stable); bodies ride in messages (cached as history). Confirm the `static` block hash is
  unchanged turn-over-turn with the cue on.

## 8.1 Unified delivery (refinement — single surface, no dup, no bloat)

Goal clarified 2026-06-07: **one unified way to pull procedures into context; avoid
duplication and context bloat.** The P1 implementation left THREE surfaces (passive
`## Known Procedures` + recall_deep body slice + get_procedure), and the live A/B
showed the agent did recall_deep **then** get_procedure (a second copy of the same
procedure). Unified design:

1. **Passive embedding (Track B) OFF** — flag `proc_passive_injection_enabled`
   (default True; set False for unified mode) skips only the **embedding-similarity**
   procedure slots in `build()`. Those duplicate the recall_deep cosine path, so they
   are the bloat. Cosine-matched procedures then enter context **only** via the pull
   path → no passive+pull duplication. **Critic-recommended skills (Track A) are NOT
   gated** (review M1): they are a classifier-driven push with no recall_deep equivalent
   (recall_deep is pure cosine), so gating them would silently disable F024 skill
   injection. Track A keeps firing in unified mode (capped at `critic_skill_slots`,
   targeted, non-duplicative).
2. **recall_deep gives the FULL one-line body to the TOP-ranked procedure ONLY**
   (others keep name+desc) — done after the final sort in `Heart._recall`. One body
   (no bloat, bounded regardless of how many procedures surface); generous cap
   (`recall_body_max_chars` default 800, le=2000) so recall_deep alone usually suffices.
   The body is whitespace-collapsed to ONE line, so recall_deep's SmartCompress cannot
   **shred** it mid-body (it does per-LINE head/tail selection; a single line is atomic).
   It can still be dropped *wholesale* on >50-line recalls — rare for top-K=10 — and
   `recall_deep` is not in the refetchable-cache set, so a dropped body isn't
   recoverable that turn. Acceptable for the top-1 line that sits in the head region.
3. **get_procedure → fallback** (explicit deep-dive). It remains a registered,
   model-selectable tool; nothing *enforces* "fallback only" — duplication is
   **behaviorally discouraged** (the live A/B saw 0 calls), not structurally impossible.
4. Static awareness cue (unchanged) triggers the single flow.

**Deploy coupling (operator):** unified mode is `proc_passive_injection_enabled=false`
+ `recall_full_bodies=true` + `proc_awareness_cue=true`. Turning passive off WITHOUT the
cue leaves the agent no implicit list and no instruction to pull → procedures become
undiscoverable. The three flags ship independently (all default OFF) but must be flipped
together.

**Live validation (eval instance, unified mode on):** `get_procedure` calls dropped
to **0** (was 4–5; recall_deep body now suffices), passive sections = **0** (single
surface). The exact-token follow metric was noisy at n=3 (1–2/3, non-monotonic in the
cap) → **effectiveness needs the larger selection-accuracy measurement** (the gate),
not n=3. The structural invariants (single surface, no dup, no bloat) are confirmed;
the flag flip in prod is gated on that selection-accuracy measurement.

## 8.2 Catalog-first delivery (supersedes 8.1's recall_deep top-1 body)

Decision 2026-06-07 (`8c9039d6`): the §8.1 "recall_deep gives the top-1 a one-line
body" approach conflated **breadth** (what procedures exist) and **depth** (the full
steps for the one I'm using) into a single ranker-chosen, truncated surface. Two
weaknesses: depth was truncated and *the cosine ranker* (not the agent) chose which one
body to show; breadth was capped at cosine top-K. This also produced a codex P2 (the CE
reranks on name+desc, so it can pick the wrong top-1 to body).

The fix mirrors **Claude Code progressive disclosure** (and the F079 spec's own
"catalog + auto-inline body" intent): split breadth from depth.

1. **Breadth — static `## Procedure Catalog`** (`proc_catalog_enabled`, default OFF).
   Lists active procedures as `- name (domain): description` — **stable fields only** (NO
   activation_count/effectiveness, which change per use). It is query-independent → rides
   the **static cache tier** → byte-identical *between procedure CRUD events* and cached
   then. It is NOT immutable: a learned/edited/retired procedure changes the bytes and
   busts the static block (identity+safety+catalog share one breakpoint) on the next turn
   — acceptable because procedure CRUD (sleep learning) is rare vs turns and sleep fires
   at session-end/idle when the cache is cold anyway. Bounded by `proc_catalog_max` (row
   cap, default 100) AND `proc_catalog_desc_chars` (per-row description truncation, default
   120 — keeps every NAME while bounding total size; `description` is unbounded `Text`).
   The list orders by `created_at desc, id` (deterministic — same-txn timestamp ties don't
   reshuffle it). Not gated by frame `budget.procedures`, so it appears even in the
   conversation frame (but skipped on the initiation path, where build() isn't called).
2. **Depth — `get_procedure(<name>)`** loads the **full untruncated** body on selection.
   `get_procedure` now accepts a **name** (falls back to `get_procedure_by_name`) as well
   as a UUID, and renders `core_patterns` + `implementation_notes` (was missing
   core_patterns). The tool **schema description** advertises name-first so the agent
   doesn't detour through `recall_deep` to find a UUID. `get_procedure_by_name` resolves
   same-name duplicates **deterministically** (`ORDER BY activation_count desc, created_at
   desc, id`) → the most-proven version, the same one every time (was an arbitrary
   `LIMIT 1`; this path ships live regardless of the catalog flag).
3. **Removed:** the §8.1 recall_deep top-1 body machinery (`recall_full_bodies`,
   `recall_body_max_chars`, `_procedure_body_line`, the post-rank expansion, and the
   per-result body metadata). recall_deep returns procedures as name+desc again — the
   catalog is breadth, get_procedure is depth. This dissolves the codex P2.
4. **Critic (option C — one surface, marked):** Track-A Critic skills stay ungated (M1),
   but when the catalog is rendered they are NOT re-listed in full (that re-duplicated
   name+desc). Instead a slim **dynamic** `★ Recommended for this task: …` pointer names
   this turn's Critic picks and points back into the cached catalog. Names only → no
   content duplication; dynamic tier → the per-turn picks never bust the static catalog.
   (Literal `★` *inside* the catalog body was rejected — it would make the catalog
   per-turn and bust the static cache every turn.) When the catalog is OFF, Critic falls
   back to the full `## Known Procedures` section (unchanged).
5. **Track-B / cue / failure:** Track-B passive embedding is gated off when
   `proc_passive_injection_enabled=false` **OR** a catalog actually rendered — so
   catalog+passive can never double-list (enforced in code). `proc_awareness_cue` is the
   **cue-only fallback** when the catalog is off; it ALSO fires when the catalog is enabled
   but **failed/empty** this turn, so unified mode never loses all procedure awareness on a
   transient DB error.
6. **Catalog hardening (codex rounds):** name-dedup keeps the SAME row
   `get_procedure_by_name` resolves (highest activation_count, then newest) so the catalog
   entry and the loaded body agree; fetch uses 3× headroom so dup rows can't crowd out
   unique names before the cap; `proc_catalog_max_chars` (default 4000, `ge=500`) is a hard
   total-size cap with a final truncate backstop; `get_procedure` retries a UUID-shaped
   input as a name after an id miss.

**Unified mode** = `proc_passive_injection_enabled=false` + `proc_catalog_enabled=true`
(breadth via catalog, depth via get_procedure). All flags default OFF.

**Live validation (real Nous instance on :8077 vs nous_eval_live, catalog on):**
- Catalog reaches the prompt: the agent listed all 4 procedure names *"from my procedure
  catalog"* with **no search call**.
- Name selection works: on a "draft release notes" task the agent reported *"I called
  `get_procedure` with the name exactly as listed in my Procedure Catalog (no search or
  UUID lookup needed)"* and loaded the full body (the VALIDATION-TOKEN + required format
  surfaced verbatim).
- The real-instance test caught a wiring gap the unit tests missed: the handler accepted
  names but the **advertised tool schema** still said "UUID", so the agent first detoured
  through `recall_deep`. Fixing the schema string closed it.

**HARD prerequisite before flipping `proc_catalog_enabled` in any environment:** skill-path
dedup must land first. The catalog faithfully lists DB duplicates — the procedure audit
found 51/62 are skill-path dups (the live `nous` DB shows 6+ identical "Always run tests
before merging" rows; 3 drifted "Send Email via Gmail SMTP" rows). A catalog of dozens of
near-identical lines defeats both the no-bloat goal AND agent selection (the live
validation used 4 *clean* procedures — the opposite of the deployment condition). The flip
is gated on (a) dedup landing and (b) the selection-accuracy measurement running on a
**dup-heavy** corpus, not the 4-proc smoke. This rework deletes §8.1's body path, so there
is no automatic fallback if catalog-first underperforms the gate — the dedup+measurement
gate is the safety net.
