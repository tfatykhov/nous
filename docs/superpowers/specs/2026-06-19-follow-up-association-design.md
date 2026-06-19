# F083 — Follow-up Association (Design Spec)

**Date:** 2026-06-19
**Status:** Draft — design approved in brainstorming dialogue, pending spec review
**FORGE:** analysis `0c76ee32` (root-cause) → spec `ab018bba`
**Source analysis:** [project_recall_followup_association_2026_06_19] — 4-agent code-only team + personal verification at HEAD + live-Nous advisor consult (Nous decision `44fa3dcc`)

---

## 1. Problem

Nous "sometimes cannot associate a follow-up question after a session ends, and asks for clarification instead of connecting it to the prior conversation." Examples: *"what about the second option you mentioned?"*, *"can you continue what we were doing?"*, *"did that fix work?"* sent in a **new session** after the prior one ended.

### 1.1 Root cause (verified at HEAD)

`recall_recent` is **not** the cause — it is a blunt time-window dump (`tools.py:1130-1174` → `episodes.py:363-405`): no `session_id` filter, no topical relevance, never auto-invoked. The failure lives in the **context-assembly / frame-budget layer**:

1. **The `conversation` frame zeroes the episode retrieval budget** — `schemas.py:135` (frame default `episodes=0`) **and** `intent.py:225` (intent plan override). Semantic episode retrieval is gated `if budget.episodes > 0` (`context.py:941`), so it is **fully off** for an ordinary conversational follow-up. (The `question` frame keeps `episodes=500` at `schemas.py:136`, so the failure is frame-specific.)
2. **The only surviving prior-session signal is titles-only** — the temporal tier (`context.py:907-938`) injects the last 5 episode **titles** (48h window); the summary body line appears **only** when `temporal_boost` is active.
3. **Recap/deictic detection is too narrow** — `_RECAP_PATTERNS` (`layer.py:54-64`) is a tiny literal-substring set; the intent classifier (`intent.py:108`) has **no** "this references a prior session" signal. So `temporal_boost` rarely fires for real follow-ups, leaving episodes title-only / budget-zeroed.
4. **Nothing forces a recall** — the model must self-elect `recall_deep`/`recall_recent`; the identity-prompt hint ("recall_deep before answering questions about past work") is too narrow to cue a deictic follow-up. The epistemic gate that could force recall-first is **OFF** in prod (`config.py:1185`, default `False`).

The advisor (live Nous) independently ranked: **#1 (budget zeroing) = the cleanest single config win**; the **`open_threads` summarizer dimension** is the real richness gap (continuation needs unfinished-work content, not just more summary prose); session-continuity severance (cleared working memory, deleted message history) is **confirmed-but-not-the-bug** — the data persists in `episodes`/`facts`, so **do not touch cross-session continuity**.

### 1.2 Non-goals

- **No change to cross-session continuity** (message-history carryover, working-memory carryover). Intentionally severed; the advisor confirmed the data we need already lives in `episodes`/`facts`.
- **No `recall_recent` redesign** (session/topical filtering). Out of scope; it is not the load-bearing path.
- **No new DB tables or migration.** `open_threads` rides inside the existing `episodes.structured_summary` JSONB.

---

## 2. Goal & Success Metric

A follow-up that references a prior session **resolves from memory** instead of triggering a clarification request.

**Primary metric — recall-precedes-clarification rate** (the advisor's framing): on a follow-up probe set, the fraction of turns where Nous either answers correctly from injected/recalled prior-session context, *or* calls a recall tool **before** asking the user to clarify. NOT "clarification count → 0" (clarification can be legitimate).

**Guard metric:** A1's episode-budget bump must not regress general retrieval — reuse the F050/F051 gate convention (no per-source MRR regression > 3% on the eval matrix).

---

## 3. Design — one feature, three layers

Each layer is independently flag-gated. Default-ON layers are low-risk; A2 + B land dark and flip only after local-instance evidence.

### Layer A — Prior-session context reaches the prompt

**A1 — Non-zero conversation-frame episode budget.** *(config; default ON)*
- `schemas.py:135`: `conversation` frame `episodes=0` → `episodes=600`.
- `intent.py:225`: conversation-frame `budget_overrides["episodes"]=0` → `600`.
- Rationale: stops hard-suppressing the semantic episode leg that every other frame already runs. ~600 tokens (≈900 in prod after `_scaled_budget` ×1.5, Review R11) ≈ 1–2 episode summaries — bounded.
- **Review R1 (interaction fix):** the existing rescue at `intent.py:231-234` is guarded `current_ep_budget == 0`. With A1 setting `episodes=600`, that guard is False, so C1's recency boost would never lift it. Fix: change the guard to `< 1000`. Then C1 lifts 600→1000 on a detected follow-up; A1's 600 floor covers follow-ups C1 misses. (No total-budget concern — `budget.total` is not enforced anywhere; it only feeds a log line at `context.py:997`.)

**A2 — First-turn last-episode full-summary injection.** *(behavior; flag `NOUS_FOLLOWUP_FIRST_TURN_EPISODE`, default OFF)*
- Hook: the temporal tier in `ContextEngine.build` (`context.py:907-938`), gated by a new `is_first_turn` param.
- Trigger: **verified first turn of a new session** — `is_first_turn = session_id not in self._active_episodes`, captured at the top of `pre_turn` and threaded into `build()`. **(Review R3:** do NOT infer first-turn from empty `conversation_messages` — that is also true after an LRU eviction + restore-miss, which would fire A2 mid-session. `_active_episodes` is session-lived and cleared only at `end_session`, so it survives conversation-dict eviction.)
- Behavior: instead of titles-only, inject the **single most-recent non-system episode's full** `structured_summary.summary` (and `open_threads` when Layer B is present), truncated to `recall_parent_episode_truncate` (500). Routes through the existing `_is_system_episode` filter.
- **Review R8 (unranked-tier note):** the temporal tier is intentionally outside the ranked retrieval stack (no relevance floor / dedup / recency-resolver / contradiction). A2 is bounded by first-turn + single most-recent episode + real (non-fabricated) content. Routing A2 through the recency-resolver is a deferred option, not built.
- Cache note (Review R11): this section rides the **dynamic** tier, which already changes every turn; A2 does NOT bust the cached static prefix — it adds ~500 tokens on turn 1 only.

### Layer B — Richer summaries: `open_threads`

**B — `open_threads` summarizer dimension.** *(behavior; flag `NOUS_EPISODE_OPEN_THREADS`, default OFF)*
- Hook: `episode_summarizer.py` — add an optional, flag-gated instruction **concatenated** onto `_SUMMARY_PROMPT` at `_summarize_single`, following the **exact** F075 pattern (`_F075_TEMPORAL_INSTRUCTION`, single-brace, concatenated not `.format`'d — see `episode_summarizer.py:82-112`).
- Schema addition: a top-level `"open_threads": ["<unfinished item / what we'd do next>", ...]` array in the summarizer JSON (`episode_summarizer.py:42-59`). Empty/omitted when nothing is unfinished (respect the NO PADDING rule at `:39`).
- Persistence: flows through `summary` dict → `heart.update_episode_summary` into `structured_summary` JSONB (no migration). Multi-part merge (`episode_summarizer.py:624-654`) extends `open_threads` like `candidate_facts` (capped).
- Consumption: A2 injects `open_threads` under the episode summary when present. This is what lets *"continue what we were doing"* resolve.

### Layer C — Detection + behavior

**C1 — First-turn-gated deictic follow-up detection.** *(detection; default ON)*
- **Review R2 (cross-session only):** the referent-conflation risk is *same-session*. A deictic phrase mid-conversation ("apply that fix") is already resolvable from live history and must NOT pull cross-session episodes. So C1 lives in `pre_turn` (not `classify`) and is gated on `is_first_turn`.
- Hook: in `pre_turn`, after `plan_retrieval` (`layer.py:604`), a narrow `_DEICTIC_FOLLOWUP` regex (module const in `layer.py`). On a first-turn match: `signals.temporal_recency = max(temporal_recency, 0.6)` and re-run `plan_retrieval`.
- **`_RECAP_PATTERNS` is NOT broadened** — explicit recap queries already work any-time; broadening them would over-trigger `temporal_boost` mid-session. All new C1 behavior is first-turn-gated.
- Regex discipline: must match cross-session referents ("the second option you mentioned", "continue what we were doing", "did that work?") and must NOT match same-session coding ("continue the loop", "use the first argument", "what about performance?"). Hard negatives are encoded as tests + probe negatives.
- Effect: with the R1 rescue fix, a first-turn follow-up lifts `episodes → 1000` and flips `temporal_boost` so summaries (not just titles) are injected. C1 is the bridge that makes A2/B fire on the right (cross-session) turns.

**C2 — Recall-before-clarify instruction.** *(prompt; flag `NOUS_RECALL_BEFORE_CLARIFY_PROMPT`, default ON)*
- A flag-gated `ContextSection` appended in `ContextEngine.build`, same pattern as the anti-hallucination block (`context.py:238-263`), priority just after Context Safety.
- Text (final wording in plan): *"Before asking the user to clarify a referent (a pronoun, 'that', 'the thing/option you mentioned', or a continuation of earlier work), first call `recall_deep` or `recall_recent` to resolve it from memory. Only ask the user to clarify if recall returns nothing relevant."*
- Fully static → caches cleanly (rides the static tier, never busts the prefix).
- The advisor rated this lowest-leverage but cheap; default ON.

---

## 4. Data flow (after F083)

```
New session, first user turn ("continue what we were doing")
  └─ runner.run_turn → cognitive.pre_turn
        ├─ is_first_turn = session_id not in self._active_episodes   (True; episode not yet created)
        ├─ IntentClassifier.classify  (PURE — no C1 here)
        ├─ plan_retrieval → A1: conversation episodes 0→600 (kill-switch)
        ├─ C1 (pre_turn, first-turn-gated): _DEICTIC_FOLLOWUP match → temporal_recency=0.6
        │      → re-run plan_retrieval → rescue 600→1000 (R1: guard now < 1000)
        ├─ _temporal_boost = recap OR temporal_recency>0.5  → True
        └─ ContextEngine.build(..., temporal_boost=True, is_first_turn=True)
              ├─ C2: "recall before clarify" section (static, default ON)
              ├─ Temporal tier (context.py:907-938)
              │     ├─ temporal_boost → inject episode summaries (not just titles)
              │     └─ A2 (flag + is_first_turn): inject most-recent episode FULL summary + open_threads
              └─ Semantic episode retrieval (budget.episodes=1000>0) runs
  └─ Model has prior-session context → answers; or, cued by C2, calls recall before clarifying
```

Layer B feeds this by ensuring `structured_summary.open_threads` exists to inject.

---

## 5. Flags

| Flag | Default | Layer | Gates |
|------|---------|-------|-------|
| `NOUS_FOLLOWUP_EPISODE_BUDGET_ENABLED` | `true` | A1 | Conversation-frame episode budget bump (kill-switch) |
| `NOUS_FOLLOWUP_DEICTIC_DETECTION_ENABLED` | `true` | C1 | First-turn-gated deictic detection (kill-switch) |
| `NOUS_RECALL_BEFORE_CLARIFY_PROMPT` | `true` | C2 | The static instruction section |
| `NOUS_FOLLOWUP_FIRST_TURN_EPISODE` | `false` | A2 | First-turn last-episode full-summary injection |
| `NOUS_EPISODE_OPEN_THREADS` | `false` | B | `open_threads` summarizer dimension + injection |

A1/C1/C2 ship default-ON behind kill-switches (low-risk, additive). A2 + B are land-dark; **the flag-default decision is made from local-instance evidence (§6).**

> **Resolved (review `b4d94716`):** A1 and C1 carry kill-switch flags (default ON) — `followup_episode_budget_enabled` and `followup_deictic_detection_enabled` — so a prod regression is disabled without a redeploy.

---

## 6. Validation — local instance, per-step (the user's mandate)

Reachability confirmed: the local Nous instance answers `POST http://192.168.1.141:8383/chat` (the existing consult harness `reports/_nous_consult*.py`).

**Method:** a scripted **follow-up probe set** — pairs of (seed session, follow-up sent as a *new* session) covering: deictic referent ("the second option you mentioned"), continuation ("continue what we were doing" — exercises Layer B), outcome check ("did that work?"), and 2–3 negative controls (genuinely ambiguous → clarification IS correct). Each follow-up is scored for **recall-precedes-clarification**.

**Per-step gate (one variable at a time, apples-to-apples):**
1. **Baseline** — capture probe outcomes on current HEAD.
2. **A1 only** — bump budget; re-run; verify episode retrieval now fires for conversation-frame follow-ups (log `budget.episodes`), no guard-metric regression.
3. **C1 only** — verify deictic probes on a NEW session flip `temporal_boost` (summaries injected, not titles) and `budget.episodes==1000` (the R1 rescue fix); verify the hard negatives AND a same-session deictic ("apply that fix" mid-conversation) do NOT trigger.
4. **C2 only** — verify the model calls recall before clarifying on deictic probes.
5. **A2 (flag ON)** — verify first-turn injection of the most-recent episode's full summary.
6. **B (flag ON)** — re-summarize seed episodes, verify `open_threads` populated, then verify A2 injects them and continuation probes resolve.

**Decision gate (A2 + B defaults):** flip `NOUS_FOLLOWUP_FIRST_TURN_EPISODE` and/or `NOUS_EPISODE_OPEN_THREADS` to default-ON **only if** the probe set shows a recall-precedes-clarification improvement attributable to that layer with no guard-metric regression. Otherwise they stay default-OFF (land-dark) pending the F051 harness. Record the verdict in FORGE + memory.

---

## 7. Testing

- **Unit:** A1 budget assertions (`schemas.py` frame defaults + `intent.py` override); C1 pattern matches + `temporal_recency` set + negative controls; A2 first-turn detection + injection content (flag on/off byte-diff); B schema parse + persistence + multi-part merge + flag-off byte-identity; C2 section present/absent by flag.
- **Integration (DB-backed, real Postgres):** end-to-end `pre_turn` → `build` produces the prior-session section for a deictic follow-up with a seeded prior episode.
- **Local-instance probe harness:** §6 (the acceptance test the user mandated).

---

## 8. Risks & mitigations

| Risk | Mitigation |
|------|-----------|
| A1 budget bump regresses general retrieval | Guard metric (≤3% per-source); kill-switch `followup_episode_budget_enabled` |
| **C1 conflates a same-session referent with a prior session** (correctness, not just tokens) | **R2:** C1 first-turn-gated (`is_first_turn`) so it never fires mid-session; narrow regex with hard-negative tests + probe negatives; kill-switch |
| **A2 fires mid-session after LRU-evict + restore-miss** (injects stale episode) | **R3:** `is_first_turn = session_id not in _active_episodes` (session-lived), NOT empty `conversation_messages` |
| A2/C1 fire on first post-**process-restart** turn of an ongoing session | **R14:** call `warm_active_episode(session_id)` (restores ongoing episodes from DB) BEFORE capturing `is_first_turn` |
| A2/C1 fire on subtask/heartbeat **background** turns | **R15:** `is_first_turn` ANDs in `not is_subtask` |
| A2 injects an unranked/superseded episode summary | **R8:** bounded to first-turn + single most-recent + `_is_system_episode`-filtered + real content; recency-resolver routing deferred |
| A2 prompt-cache impact | **R11:** A2 rides the dynamic tier (already volatile); does NOT bust the cached static prefix; +~500 tok turn-1 only |
| B summarizer JSON truncation (whole summary lost) | **R5:** `max_tokens` 1500→3000 when `episode_open_threads` on (mirrors `extraction_coverage_broadened`); long-transcript parse test |
| B `open_threads` malformed (null/str) crashes merge/read | **R6:** `isinstance(list)` + per-entry `str` filter at both merge and A2 read; never breaks the temporal tier (R9) |
| B summarizer padding / NO-PADDING regression | Concatenated instruction only when flag ON; explicit "do not invent/pad" + empty-array path |
| Summarizer lag — `open_threads` absent at follow-up time | Same async window as today; A2 falls back to `summary`, then titles; not a regression |

---

## 9. Files touched (preview — finalized in plan)

- `nous/config.py` — 3 new flags (+ 2 kill-switches if §5 resolved yes)
- `nous/cognitive/schemas.py` — A1 frame default
- `nous/cognitive/intent.py` — A1 override + C1 detection
- `nous/cognitive/layer.py` — C1 recap-pattern broadening (+ relocate/share follow-up pattern consts)
- `nous/cognitive/context.py` — A2 first-turn injection + C2 instruction section
- `nous/handlers/episode_summarizer.py` — B `open_threads` instruction + schema + merge
- `tests/` — per §7
- `scripts/diag/` — local-instance follow-up probe harness (§6)
- `docs/features/INDEX.md`, `CLAUDE.md` — flag docs + shipped row
