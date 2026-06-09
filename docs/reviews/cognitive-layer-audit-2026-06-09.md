# Cognitive Layer — Deep Code Audit

**Date:** 2026-06-09
**Scope:** `nous/cognitive/*` (layer, context, critic, monitor, deliberation, intent, dedup, frames, action_gate, execution_ledger, claim_verifier, epistemic, rubric, correlation, usage_tracker, schemas). Runner (`nous/api/runner.py`) read for wiring verification only.
**Method:** code-only — every claim verified against function bodies at HEAD. No specs/docs consulted for behavior claims.
**Reachability config:** code defaults (`nous/config.py`) cross-checked against the prod overlay (`.env.prod-snapshot`). Verdicts: **LIVE** (fires under prod config), **LATENT** (flag-gated off or non-default config), **INERT** (computed, no consumer), **DEAD** (no caller).

**Prod overlay facts that shape severity:**

| Setting | Code default | Prod |
|---|---|---|
| `NOUS_CACHE_SPLIT_SYSTEM_PROMPT` | `True` (config.py:429) | (default) **true — split path is the live prompt path** |
| `NOUS_ACTION_GATING_ENABLED` | `True` | **`false` — ActionGate OFF in prod** |
| `NOUS_CLAIM_VERIFICATION_ENABLED` / `_MODE` | `True` / `enforce` (config.py:723-724) | (default) **enforce — LIVE** |
| `NOUS_EXECUTION_LEDGER_ENABLED` | `True` | (default) true — LIVE |
| `NOUS_CRITIC_MODE` | `shadow` (config.py:774) | **`advised`** + `NOUS_CRITIC_SKILL_INJECTION=enabled`, model haiku — critic LIVE per turn |
| `proc_catalog_enabled` / `proc_selection_graph_primary` | `True` / `True` (config.py:156,173) | (default) — F079 catalog + F080 §14.7 LIVE |
| `NOUS_EPISTEMIC_GATE_ENABLED` | `False` (config.py:1041) | (unset) — §2 gate LATENT |
| `NOUS_RECENCY_RESOLVER_ENABLED` | `False` | **`true`** (+ temporal extraction true) — Gap-2 resolver LIVE |
| `NOUS_GRAPH_HUB_AUTOSURFACE_ENABLED` | `False` (config.py:570) | **`true`** — F065 autosurface LIVE |
| `NOUS_CONTEXT_BUDGET_OVERRIDES` | `{}` | `{"total":13000,"facts":3000,"decisions":2500,"identity":1200,"user_profile":500}` |
| `NOUS_RUBRIC_*` | — | covered by `rubric-admission-audit-2026-06-09.md` (verified still live at HEAD, §4 below) |

---

## 1. How it actually works (verified spine)

**pre_turn** (`layer.py:349-941`): track session metadata + transcript → bump `agents.last_active` → initiation check → `FrameEngine.select` (pure pattern match, `frames.py:63-125`) → F024 critic classify (advised mode can override frame, activate skills) → `IntentClassifier.classify/plan_retrieval` (`intent.py`) → `ContextEngine.build` → subtask-result injection → deliberation start (decision/debug frames only, `deliberation.py:25`) → episode warm/create (with 48h cosine dedup) → working-memory focus + load recalled → censor check (abort/refuse/steer, F078) → return `TurnContext`.

**ContextEngine.build** (`context.py:125-997`): assembles ~14 sections (datetime, identity, anti-hallucination, F079 procedure catalog, §2 epistemic, cache hints, user profile, censors, frame, working memory, decisions, facts, §14.7/passive procedures, temporal episodes, semantic episodes). Per-type pipeline: retrieve → recency-resolve (facts) → staleness penalty → frame boost → diversity → conversation dedup → usage boost → relevance filter → collect IDs → format → truncate to per-section budget. Returns both flat `system_prompt` and `sections_by_tier` (F036).

**Runner consumption** (`runner.py:1896-1970`): **when `cache_split_system_prompt` is true and `sections_by_tier` is non-empty (every successful non-initiation build), the flat `turn_context.system_prompt` string is never read** — only the tier dict plus runner extras (frame instructions, diagnostic nudges, ledger section, pending corrections, telegram format) reach the LLM. This is the load-bearing fact behind CL-1/CL-2.

**post_turn** (`layer.py:943-1233`): monitor assess (structural surprise only) → monitor learn (censor auto-create, F012 error→recovery tracking, F039 correction detection) → deliberation finalize-or-delete (informational heuristics) → usage tracking (overlap of recalled content vs response) → procedure reinforcement (activate + outcome per recalled procedure) → output censor log-check → censor compliance log-check → session metadata bump → WM threads → `turn_completed` event → critic diagnostics → queue nudges for next turn.

**end_session** (`layer.py:1703-1827`): end-or-discard episode (trivial filter), extract `learned:` facts from reflection, clear WM, pop per-session dicts, emit `session_ended` (no `summary` key — rubric audit R1 confirmed still true at `layer.py:1805-1812`).

**F026 surfaces:** `ExecutionLedger` is in-memory per session in the runner (`runner.py:170, 2265-2270`), turn set from message count; `ClaimVerifier`/`IntentTracker` run post-response and inject corrections into the next turn's dynamic tier (`runner.py:2272-2334`); `ActionGate` checks each dispatch but is **disabled in prod**.

---

## 2. Findings register

### P1

#### CL-1 — P1 — Subtask results are injected into a discarded string and marked delivered anyway: silent, unrecoverable loss — **LIVE**
`layer.py:642-672` + `runner.py:1914-1949` + `nous/heart/subtasks.py:295-305`
Step 3b of pre_turn appends `_format_subtask_results(...)` ONLY to the flat `system_prompt` (`layer.py:663`) and never touches `sections_by_tier`. With `cache_split_system_prompt=true` (code default, not overridden in prod) the runner's split path (`runner.py:1915-1949`) never reads the flat string — so the "Completed/Blocked/Failed Subtask" context never reaches the LLM. Worse, the rows were already marked `delivered=true` at `layer.py:653-655` **before** rendering, so the loss is permanent: `get_undelivered` (`subtasks.py:282-293`) will never return them again. The parent agent (and DAG flows that the F061 docstring says should "react" to `blocked_reason`) silently never sees any subtask outcome. The Telegram notification (`subtask_worker.py:_notify_telegram`) is a separate human-facing channel and does not mitigate the agent-loop loss. The F078 censor-injection block immediately below (`layer.py:874-897`) documents this exact trap ("runner._build_system_prompt ignores the flat string when cache_split_system_prompt is true") and fixed itself — subtask injection was never given the same treatment.
**Fix:** mirror the censor-injection pattern — append `subtask_context` to `sections_by_tier["dynamic"]` when it's populated; ideally also move `mark_delivered` to after successful injection.

### P2

#### CL-2 — P2 — F065 hub-shift notice suffers the same severed wire AND self-suppresses — **LIVE**
`layer.py:906-918, 1292-1416` + `runner.py:1915-1949`
With `NOUS_GRAPH_HUB_AUTOSURFACE_ENABLED=true` in prod, `_compute_hub_shift_notice` runs every pre_turn, but the returned block is appended only to the flat `system_prompt` (`layer.py:912`) — discarded by the split path. Meanwhile the method **persists new snapshots for every entered/left transition** (`layer.py:1382-1414`) precisely so the notice is emitted once. Net effect: the agent never sees a hub-shift notice, and the snapshot writes guarantee each shift is permanently consumed unseen. The feature is paying two SQL queries + snapshot writes per turn for zero delivered output.
**Fix:** route the block into `sections_by_tier["dynamic"]` (same pattern as censors).

#### CL-3 — P2 — `_apply_usage_boost` sorts by boost factor, not boosted score: usage ratio overrides relevance and breaks downstream gap detection — **LIVE**
`context.py:1027-1044` + `usage_tracker.py:103-117`
The method computes `wrapped = score * boost` but then sorts by `x[1]` — the bare boost factor (0.3–2.0, purely the reference-rate, no relevance component). Any item with ≥2 retrievals gets a non-1.0 boost (`usage_tracker.py:113-117`), and since every context-injected item is recorded every turn (`layer.py:1060-1094`), most recalled items acquire stats within two turns. From then on facts/episodes are ordered by historical reference-rate, not by retrieval score. Two consequences: (a) a frequently-referenced but barely-relevant memory outranks the best match; (b) `_apply_relevance_filter` (`context.py:1046-1089`) runs immediately after and its score-gap cut (`score < prev_score * drop_ratio`) assumes a descending-score list — on a boost-ordered list the "gap" is noise, so the filter cuts arbitrarily. Note `apply_frame_boost` (`heart/search.py:494-496`) deliberately sorts by boost with a comment justifying it for *discrete tiers* (1.0/1.3); usage boost is continuous, so the copied pattern becomes a full re-sort by ref-rate.
**Fix:** sort by the wrapped (boosted) score: `boosted.sort(key=lambda x: getattr(x[0], "score", 0), reverse=True)`.

#### CL-4 — P2 — Claim verification grounds claims on tool *name only*, ignoring status: any `bash` call (even `ls`, even a BLOCKED/failed call) verifies "I pushed/committed/deployed" — **LIVE**
`claim_verifier.py:88-98` + `execution_ledger.py:140-163`
`verify()` builds `recent_ledger_tools = {action.tool_name for action in ledger.actions[-10:]}` and passes a claim if the expected tool name appears — with no check of `action.status` (`"error"`, `"timeout"`, and `"blocked"` entries count) and no check of the action's *arguments*. The third pattern maps "I pushed/committed/deployed" → `bash`; since nearly every working session contains a bash call within the last 10 actions, push/commit/deploy claims are effectively never flagged. Similarly "I saved X file" is grounded by any `write_file` of a *different* file, and a gate-blocked `write_file` (status `"blocked"`, recorded at `runner.py:1688`) still grounds the claim it just prevented. Prod runs `claim_verification_mode=enforce`, so this is the live integrity net with a tool-name-sized hole.
**Fix:** filter ledger lookup to `status == "success"`; for bash claims, require a write-classified or push-classified command (the ledger already stores `key_args["command"]` and `side_effect_type`).

#### CL-5 — P2 — Critic confidence-drift diagnostic can raise `TypeError` inside post_turn; non-streaming path has no guard — **LIVE (conditional)**
`layer.py:1209-1213, 1221-1227` + `critic.py:377-380` + `runner.py:544`
post_turn builds tool-history entries with `entry["confidence"] = tr.arguments.get("confidence")` — `None` when a `record_decision` call omits confidence. `_check_confidence_drift` then does `d.get("confidence", 0.5)` — the key **exists** with value `None`, so the default doesn't apply — and evaluates `all(c < 0.4 for c in confidences)` → `TypeError: '<' not supported between 'NoneType' and 'float'` once 3+ record_decision entries are in history and one lacks confidence. `run_diagnostics` is called with no try/except (`layer.py:1221`), and the non-streaming `run_turn` calls post_turn unprotected (`runner.py:544`) — the exception propagates after the response was generated, failing the turn for REST `/chat` callers and marking subtask turns failed. (The streaming path swallows it at `runner.py:1403-1408`.) Critic is LIVE in prod (`critic_enabled=True` default, mode advised).
**Fix:** `(d.get("confidence") or 0.5)` in `_check_confidence_drift`, or drop `None` confidences at capture time (`layer.py:1212`).

#### CL-6 — P2 — Intent-plan budget overrides silently clobber the operator's env budget overrides — **LIVE**
`context.py:164-184` + `schemas.py:114-134` + `intent.py:219-228`
`ContextBudget.for_frame` applies `settings.context_budget_overrides` first, then `retrieval_plan.budget_overrides` are applied **on top** with REPLACE semantics (`context.py:182-183`). `plan_retrieval` unconditionally sets `{"decisions":500,"facts":500,"procedures":0,"episodes":0}` for every conversation-frame turn and `{"decisions":3500,"procedures":2000}` for decision frames (`intent.py:220-228`). Prod explicitly configures `facts=3000, decisions=2500` via `NOUS_CONTEXT_BUDGET_OVERRIDES` — on every conversation-frame turn (the default frame, i.e. the majority) those operator values are silently reverted to 500/500/0/0. The operator knob appears to work (it shows up on task/question frames) but is dead on the most common frame.
**Fix:** apply env overrides *after* plan overrides, or have `plan_retrieval` scale rather than replace.

#### CL-7 — P2 — Monitor auto-censor creation is structurally near-unreachable: the surprise threshold and the candidate source are mutually exclusive — **LIVE (no-op)**
`monitor.py:97-116, 148-184` + `runner.py:426-431, 523-531`
`learn()` creates censors only when `surprise_level > 0.7` AND `censor_candidates` non-empty. But `assess()` sets surprise 0.9 only on a *turn-level* error and 0.3 on tool errors — while candidates are derived exclusively from tool errors. In the non-streaming runner, a turn-level error leaves `tool_results = []` (initialized `runner.py:427`, never assigned on the exception path) → candidates empty. So: tool errors → candidates but surprise 0.3 (no creation); turn error → surprise 0.9 but no candidates. The only window is the streaming path where accumulated `all_tool_results` contain non-transient errors AND the turn itself then errors — a rare corner. The "Learn → create censors from failures" leg of the loop has effectively never fired; the `_error_to_censor_text` machinery (`monitor.py:394-403`) is consumed-but-inert in practice.
**Fix:** decide the intended trigger — e.g. create censors at `surprise >= 0.3` with the existing per-session cap, or set surprise 0.8 when a non-transient tool error repeats.

#### CL-8 — P2 — Critic skill "activation" in pre_turn + blanket per-turn reinforcement in post_turn double-count and auto-praise procedures — **LIVE**
`layer.py:507-526` (pre_turn activate) + `layer.py:1096-1114` (post_turn activate **again** + outcome)
In advised mode every critic-recommended skill is activated in pre_turn; then post_turn loops over **all** `recalled_procedure_ids` (which include those same skills plus §14.7/passive picks) calling `activate_procedure` a second time and `record_procedure_outcome("success"|"failure")` keyed solely on whether the *turn* had any error. Effects: activation counts inflate 2x per turn for critic skills and 1x/turn for every procedure that merely sat in context; effectiveness converges to the global turn-success rate rather than measuring the procedure (the procedure-subsystem audit's "effectiveness→telemetry" finding — this is the code path that does it). With §14.7 LIVE (graph-primary preload, `context.py:635-706`), up to 5 procedures get a success-credit every error-free turn whether or not the agent used them.
**Fix:** reinforce only procedures whose content overlapped the response (the usage-tracker overlap already computed at `layer.py:1077-1094`), and don't re-activate critic skills in post_turn.

### P3

#### CL-9 — P3 — `budget.total` is never enforced; several sections bypass budgets entirely — **LIVE**
`context.py:961-974`. The per-section truncations are the only enforcement; `total` is used only for the fill-ratio log line. Datetime, anti-hallucination, procedure catalog (`proc_catalog_max_chars`, default 4000 chars), epistemic routing, cache hints, Recent Conversations, and §14.7 first-body-always-shows (`context.py:683-688`) are all outside any `ContextBudget` field. With prod `total=13000` the real prompt can exceed it; harmless today but the knob misleads.

#### CL-10 — P3 — Execution-ledger turn numbers regress after compaction — **LIVE (display) / LATENT (gate)**
`runner.py:417, 975` computes `set_turn((len(conversation.messages)+1)//2)`. Compaction replaces history with a summary, shrinking `messages` — the next turn number jumps backward (e.g. 40 → 8) while existing `ExecutedAction.turn` values stay high. Consequences: `_build_section`'s `a.turn >= cutoff_turn` (`execution_ledger.py:235-237`) classifies all pre-compaction actions as "recent" forever (ledger prompt bloat, mitigated only by the token-budget shrink loop), and ActionGate's window check `action.turn > min_turn` (`action_gate.py:166-180`) would treat stale actions as in-window duplicates if the gate were enabled.
**Fix:** keep a monotonic per-session turn counter instead of deriving from message count.

#### CL-11 — P3 — Recalled IDs are collected before render truncation: items cut by `_truncate_to_budget` are still recorded as "shown" — **LIVE**
`context.py:608-617` (facts; same shape for decisions 544-556, episodes 939-945). IDs/scores enter `recalled_ids` after the relevance filter but **before** `_truncate_to_budget` cuts the formatted text. A tail item that never reached the prompt is (a) penalized by the usage tracker as retrieved-but-unreferenced (`layer.py:1060-1094`), (b) excluded from `recall_deep` by F071 (`runner.py:499-501` builds the exclusion set from these IDs), and (c) loaded into working memory. The §14.7 procedure path explicitly fixed this ("B-cog-A", `context.py:673-688`); the fact/decision/episode paths still have it. Small budgets (conversation frame facts=500 tokens) make the truncation cut realistic.

#### CL-12 — P3 — Monitor per-session state never cleaned up: `_last_errors`, `_error_recovery_pairs`, `_session_procedure_counts` leak — **LIVE**
`monitor.py:63-65` vs `layer.py:1788-1801`. `end_session` pops `monitor._session_censor_counts` only. The three F012 dicts grow one entry per session with tool errors and are never removed (session IDs are unique, so this is an unbounded slow leak in a long-lived process, same class as the documented `_active_episodes` P1-8 but without the comment).

#### CL-13 — P3 — Recency resolver can poison the relevance-filter gap cut; resolver also mutates shared Pydantic rows in place — **LIVE**
`context.py:1248-1293`. `_resolve_recency` down-ranks the older fact (`*0.3`) but leaves it in its original list position; `_apply_relevance_filter` later interprets that in-place score cliff as a diminishing-returns boundary and cuts every fact after it (`context.py:1075-1087`), so one superseded fact can evict unrelated facts ranked below it. Mutating `score`/`recency_status` directly on the shared summary objects is safe only because each build constructs fresh DTOs — worth a comment, since `_ScoredWrapper` (`heart/search.py:441-452`) cannot be attribute-assigned (`__slots__`) and any reordering of the pipeline (resolver after staleness wrap) would crash.

#### CL-14 — P3 — Deliberation quality gate deletes real decisions whose response happens to open with chat prefixes — **LIVE**
`deliberation.py:33-36, 54-58` + `layer.py:1050-1056`. `finalize()` is called with `description=turn_result.response_text[:500]`; rule 3 rejects any description starting with "sure", "okay", "let me", "i'll", "here's", "done", "got it" — common openers for substantive decision/debug turns. The decision (with its captured thinking-block trace, `layer.py:1024-1038`) is then hard-deleted. The gate evaluates the *response style*, not the decision.

#### CL-15 — P3 — Working-memory load threshold and procedure floor compare rank-encoded RRF scores against fixed thresholds — **LIVE**
`layer.py:1460-1504` (0.7 floor) and `context.py:794-799` (`procedure_score_floor` 0.40 vs `search_procedures` scores). `hybrid_search` normalizes RRF by the theoretical max (`heart/search.py:171-177`), so a document at rank 0 in both legs scores 1.0 **on any query regardless of closeness** — the 0.7 WM floor therefore admits the top hit of even a hopeless query, and mid-pack genuinely-relevant items (rank 5 ≈ 0.6 at k=30) are excluded. Same threshold-space-vs-rank-space mismatch class as audit S1 / the retrieval whitepaper's central defect, on the pre-turn injection path. (The §14.7 cosine leg already migrated to raw cosine — `context.py:1429-1450` — these two consumers did not.)

#### CL-16 — P3 — `run_python` ledger args contradict the spec comment; unknown-tool fallback captures code bodies — **LIVE (display) / LATENT (gate)**
`execution_ledger.py:87, 295-307`. `_KEY_ARGS["run_python"] = []` is falsy, so `_summarize_args` falls into the unknown-tool fallback and captures the first 5 args including `code[:80]` — the comment says "spec says skip". Harmless for display; for the (disabled) gate it makes two different run_python calls with the same first-80-chars look identical.

#### CL-17 — P3 — Session restore after process restart loses transcript/significance state: episodes end with empty transcript and trivial-filter misfires — **LIVE (post-restart)**
`layer.py:1723-1752`. `_session_metadata` is process-memory; after a restart `warm_active_episode` restores the episode ID but `meta` is `None` at end_session → `transcript_text = ""` (no F067 chunking input, no F025 persistence), `is_trivial` is False by construction (meta None) so even a genuinely trivial restored session is kept. Conversations *are* DB-restored (`runner.py:2354-2356`) — the cognitive layer's session state isn't.

#### CL-18 — P3 — Critic `_needs_critic` tail logic is dead: the function unconditionally invokes the critic for any message above the passthrough gate — **LIVE**
`critic.py:135-143`. After the short-message early-return, both the sentence-endings check and the action-signals check are followed by `return True` anyway — three branches, one outcome. Combined with `critic_max_latency_ms=5000` and prod advised mode, every message longer than 5 words (or ending in "?") pays a synchronous Haiku call serial to the turn. Intent may be "err toward invoking" (docstring) — then the two checks should be deleted.

#### CL-19 — P3 — Frame-selection multi-word patterns substring-match across word boundaries; overridden frames get no usage credit — **LIVE**
`frames.py:101-103`: `pattern_lower in input_clean` matches "what if" inside "somewhat ifs"-shaped text since the cleaned input is one flat string (single-word patterns got the tokenization fix, multi-word didn't). Also when the critic overrides the frame (`layer.py:472-475`), the heuristic frame's `usage_count` was already incremented in `select()` while the actually-used frame gets none — frame usage telemetry counts the loser.

#### CL-20 — P3 — Intent `topic_keywords` set-ordering makes the retrieval query non-deterministic across processes — **LIVE**
`intent.py:134-138, 192-194`: `list(set(...))[:10]` order depends on per-process hash randomization; the joined `query_text` (used for embeddings + FTS) differs between runs/replicas for the same input, defeating cross-process reproducibility and the sha256-keyed embedding cache across restarts. `sorted(set(...))` would fix it.

### INFO

- **CL-21 — INFO** — Multi-level usage "strength" computed then discarded: `layer.py:1080-1088` derives 1.0/0.5/0.2 tiers but only `strength > 0` is consumed; `record_retrieval` has no strength parameter. Dead computation.
- **CL-22 — INFO** — `Assessment.facts_extracted` is always 0 (`monitor.py:146, 246` — never incremented) and `Assessment.episode_recorded` is never set anywhere. Dead DTO fields.
- **CL-23 — INFO** — Working-memory "Loaded context" items (loaded at ≥0.7 last turn) duplicate the same top facts re-retrieved into "Relevant Facts" this turn; conversation-dedup doesn't compare against the WM section. Token duplication on consecutive same-topic turns (`layer.py:1460-1504` + `context.py:1590-1599`).
- **CL-24 — INFO** — Critic diagnostic cooldowns are instance-level, not session-scoped (`critic.py:77-80`, documented); `_current_turn` shared across concurrent sessions can suppress/allow nudges cross-session.
- **CL-25 — INFO** — `pre_turn` topic-prefixed default query uses the *previous* turn's `current_task` (WM focus is updated at step 6, after the build at step 3) — first turn on a new topic retrieves under the old topic prefix (`context.py:519-525`, `layer.py:720-742`).
- **CL-26 — INFO** — When `_is_duplicate_episode` suppresses creation (`layer.py:704-705`), the session never registers an episode, so every subsequent pre_turn re-runs warm query + embed + cosine search; and the session's facts get no `source_episode_id`.
- **CL-27 — INFO** — `SessionMetadata.transcript` is unbounded (1000 chars/turn); a 600-turn prod session (`NOUS_MAX_TURNS=600`) holds ~600KB per session until end_session.
- **CL-28 — INFO** — Streaming post_turn call (`runner.py:1405`) doesn't pass `is_background` — currently harmless because background turns use non-streaming `run_turn`, but the #462 invariant is one call-site change from regressing.
- **CL-29 — INFO** — `_check_censor_compliance` (`layer.py:166-192`) counts ≥2 five-letter word hits as "compliant" — log-only, very weak signal, frequently false-positive on topic words.
- **CL-30 — INFO** — `_classify_bash_command` (`execution_ledger.py:350-381`) inspects only the first token: `cat x | sh`, `find -delete`, `sed -i` classify as read-only "none"; chained `ls && rm -rf` is "none". Acceptable-approximate by its own docstring, but note Tier-1 gate auto-approves "none" (`action_gate.py:132-134`) — relevant only if the gate is re-enabled.

---

## 3. Rubric subsystem — status of 2026-06-09 audit findings at HEAD

Verified against current `rubric.py` / `correlation.py` / `layer.py` bodies — **all rubric-side findings R1–R15 remain live as written**; nothing in the cognitive layer changed since the audit:
- `session_ended` still carries no `summary` key (`layer.py:1805-1812`) → R1 (P1) live.
- Episode outcome still hardcoded `"success"` (`layer.py:1747-1749`) → R15 live.
- `rollback`/version-string fragility unchanged (`rubric.py:194-217`) → R7 live; `create_version` read-mark-insert race unchanged (`rubric.py:162-185`) → R8 live.
- `suggest_weights` post-normalization cap drift unchanged (`correlation.py:157-165`) → R12 live.
- `RubricManager.load_correction_context` still has zero callers → R6 (dead surface) live.
No re-discovery performed; see `docs/reviews/rubric-admission-audit-2026-06-09.md` for the full register.

---

## 4. Dead-code inventory (cognitive layer)

| Symbol | Location | Status |
|---|---|---|
| `ContextEngine.expand` | context.py:1624-1675 | DEAD — no callers in `nous/` |
| `ContextEngine.refresh_needed` | context.py:1602-1622 | DEAD — no callers |
| `ContextEngine._dedup_decisions` | context.py:1677-1709 | DEAD — superseded by `_enforce_diversity`, no callers |
| `ExecutionLedger.has_blocked_actions_this_turn` | execution_ledger.py:170-176 | DEAD — no callers |
| `Assessment.facts_extracted` / `episode_recorded` | schemas.py:220-221 | INERT — never set to non-default (CL-22) |
| `critic._needs_critic` sentence/action checks | critic.py:135-142 | DEAD branches — unconditional `return True` follows (CL-18) |
| usage "strength" tiers | layer.py:1080-1088 | INERT — computed, not consumed (CL-21) |
| `monitor._error_to_censor_text` + censor-candidate pipeline | monitor.py:104-108, 148-184 | Effectively unreachable in non-streaming path (CL-7) |
| `IntentTracker.check_ghost_planning` `ledger` param | claim_verifier.py:160-165 | INERT — explicitly reserved |
| `RubricManager.load_correction_context` | rubric.py:259-275 | DEAD (rubric audit R6) |
| `FrameSelection.match_method` consumers | schemas.py:84 | INERT — set everywhere, read nowhere outside logs/tests |

---

## 5. Improvement opportunities (non-bug)

1. **One injection helper for pre_turn prompt additions.** Three call sites hand-append to the flat prompt; one of three remembered the dynamic tier. A `_inject_dynamic(section_text)` helper that writes both representations would have prevented CL-1/CL-2 and will prevent the next one.
2. **Make `TurnContext.system_prompt` authoritative or delete it.** Today it is a lie under the default config — either have the runner always derive the flat string from `sections_by_tier` + extras, or build the tier dict as the only product.
3. **Score-space discipline at the cognitive boundary.** CL-3, CL-13, CL-15 are all "a calibrated-looking threshold applied to an uncalibrated score" — the same class the storage audit fixed on the write side (S1) and §14.7 fixed for its cosine leg. A typed score (`RankScore` vs `CosineScore`) or at minimum a naming convention (`rrf_score`) would make these grep-able.
4. **Session-state registry.** Seven per-session dicts on `CognitiveLayer` + four on `MonitorEngine` + runner-side ledgers/corrections, each individually (and inconsistently — CL-12) cleaned in end_session. A single `SessionState` object popped once would eliminate the leak class and the post-restart asymmetry (CL-17).
5. **Procedure reinforcement should require evidence of use** (CL-8): the overlap computation already exists in the same function; gating `record_procedure_outcome` on it is a ~5-line change that would make `effectiveness` mean something before any F079/§14.5 measurement work trusts it.
6. **Claim-verifier precision** (CL-4): the ledger already stores per-action `side_effect_type`, `status`, and the bash command — the verifier uses none of them. Matching expected-tool against *successful* actions with side-effect ∈ {write, external} would close most of the hole without new state.
