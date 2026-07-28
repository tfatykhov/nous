# Set `decisions.session_id` at record time; delete both legacy join tables

**Date:** 2026-07-28
**Status:** PLAN v2 — one gating question open (§6)
**Branch:** `fix/decision-session-id-wiring`

> **v2 supersedes v1.** v1 proposed populating `heart.episode_decisions` by mirroring the
> `learn_fact` runner-injection seam. Devil's-advocate review overturned it and **every claim was
> independently re-verified against code and prod before acceptance.** v1's failures are recorded
> in §7 because two of them were reasoning errors worth not repeating.

---

## 1. Root cause: three lines, not a missing mechanism

`nous/api/tools.py:810` builds `RecordInput(...)` **without `session_id`** — although:

- `RecordInput.session_id` already exists (`brain/schemas.py:55`)
- `ToolDispatcher` already injects `_session_id` for **five** other tools
  (`tools.py:100-116`: `spawn_task`, `cache_retrieve`, `run_python`, `ingest_document`, `recall_deep`)
  — just not `record_decision`

Verified consequence in prod: **every one of the 287 populated `session_id` values comes from the
deliberation path** (`cognitive/deliberation.py:125`). The `record_decision` tool has set
`session_id` on **zero** decisions, ever.

### 1.1 A secondary effect — **downgraded after verification**

`brain.py:1109 get_session_decisions` filters `Decision.session_id == session_id` and is called by
`DecisionReviewer.handle()` (`decision_reviewer.py:281`) on every `session_ended`. 718 of 1,005 prod
decisions have NULL `session_id` and never appear in that leg.

> **Correction (verified 2026-07-28).** An earlier draft of this plan claimed those 718 decisions
> are "invisible to session-end review." **That is false and the claim is withdrawn.** `handle()`
> unconditionally calls `self.sweep()` immediately afterwards (`decision_reviewer.py:302`), and
> `sweep()` → `get_unreviewed()` (`brain.py:1119-1140`) filters only on `agent_id`,
> `reviewed_at IS NULL`, and a 30-day cutoff — **it is not session-scoped.** The 718 are already
> being reviewed today.

What populating `session_id` actually buys here is narrow: the session-scoped leg is immediate and
age-unbounded, where `sweep()` is 30-day-capped and runs agent-wide. **Real but marginal — it is not
a justification for this change.** The justification is root-cause correctness (§1), enabling the
episode↔decision readers (§4 steps 6–8), and deleting ~200 lines of dead code.

---

## 2. Verified prod evidence

| Measurement | Value | Why it matters |
|---|---|---|
| decisions with `session_id` | 287 / 1005 | all from deliberation; tool path never sets it |
| decisions invisible to session-end review | **718** | the real bug |
| sessions with exactly 1 episode | **307 of 309** | session→episode is **99.4% determined** |
| `episodes.session_id` before 2026-05 | 0 / 497 | column landed in May |
| `episodes.session_id` since 2026-05 | **317 / 320 (99%)** | low join coverage is **historical, not structural** |
| heartbeat share of session-bearing decisions | 114 / 287 | heartbeat sessions never create episodes |
| episode outcomes | 711 success / 96 NULL / 10 abandoned / **0 failure** | see §3 |
| `discussed_in` episode→decision edges | 3, all `inferred`, weight 0.46–0.64 | a live producer already exists |

---

## 3. `EpisodeSignal` must be DELETED, not fed

v1 listed `decision_reviewer.py:109` as a "live reader waiting for rows". **It is not registered.**
`decision_reviewer.py:232-234` is `self._signals = [ErrorSignal()]` (+`GitHubSignal` when a token
exists). The comment at `:222-231` records audit HD-4 (2026-06-09) deliberately unwiring it:

> "EpisodeSignal mapped the (currently hardcoded "success") episode outcome onto every decision
> made during the session — a category error… Both fed Brain calibration false labels."

Confirmed: `layer.py:1882` hardcodes `outcome="success"`, and prod has **zero** `failure` episodes.
Rewiring it would relabel essentially every episode-linked decision `success @ 0.8` — clearing
`CONFIDENCE_THRESHOLD = 0.7` — i.e. **exactly the machine-artifact corruption PR #577 just reversed.**

**Action:** delete `EpisodeSignal` (`decision_reviewer.py:101-122`) and its only support,
`Brain.get_episode_for_decision` (`brain.py:1192-1210`).

---

## 4. Changes (Option C)

| # | File | Change | ~Lines |
|---|---|---|---|
| 1 | `api/tools.py:106` | dispatcher: inject `_session_id` for `record_decision` (copy the `ingest_document` pattern verbatim) | 1 |
| 2 | `api/tools.py:767` | `record_decision(..., _session_id: str \| None = None)` | 1 |
| 3 | `api/tools.py:810` | `RecordInput(..., session_id=_session_id)` | 1 |
| 4 | `heart/schemas.py:72` | add `session_id`; drop `decision_ids` | 2 |
| 5 | `heart/episodes.py:661,669` | build detail from `episode.session_id`; drop the `selectinload` | 2 |
| 6 | `handlers/episode_summarizer.py:478` | query decisions by `ep.session_id` | ~6 |
| 7 | `brain/graph_densifier.py:680-693` | swap the `episode_decisions` join for a `session_id` join — **gets simpler**, and the PR #557 `agent_id` hazard dissolves because `brain.decisions` is agent-scoped | ~8 |
| 8 | `handlers/sleep_handler.py:2316-2320` | swap raw SQL to `SELECT id FROM brain.decisions WHERE session_id=…` | ~3 |
| 9 | delete `EpisodeSignal` + `get_episode_for_decision` | §3 | — |
| 10 | drop **both** join tables | migration `067`; after 5–8 neither has a reader | — |

**Deletion surface for `episode_procedures`** (unchanged from v1, still correct — zero readers, and
the concept lives on `procedures.success_count/failure_count` at `procedures.py:958` with 13,402
success / 1,344 failure across 62 of 70 active procedures):
`storage/models.py:481,677,685-709`; `storage/__init__.py:15,44`; `heart/episodes.py:23,299-327`;
`heart/heart.py:234-242`; `tests/test_database.py:73`; `tests/test_episodes.py:16,147-166`;
`tests/test_heart.py:103`.

Net: **~24 lines changed, ~200 removed** across 8 files + one migration.

### 4.1 Why this beats v1

- Fixes `get_session_decisions` (718 decisions) — v1 did not
- **No idempotency problem** — no composite-PK join row, so no `ON CONFLICT` needed
- **O1/O2/O3 all evaporate** — deliberation already sets `session_id`; decisions are agent-scoped;
  `dispatch` is bare `handler(**args)` with no unknown-key rejection
- **Survives process restart.** v1 depended on `get_active_episode_id`, documented in-memory-only
  (`runner.py:2591-2594`, `layer.py:349-354`) — it silently produces no link after a restart.
  A `session_id` join has no such failure mode.
- No backfill needed: readers derive historical links live

---

## 5. Correcting a false claim in the codebase

`graph_densifier.py:643-645` states no other mechanism rebuilds episode→decision edges. **False.**
`handlers/decision_graph_linker.py:165-173` writes `discussed_in` edges by cosine on the
decision-record path, live — all 3 prod edges are its `inferred` output. The leg simply rarely
clears `graph_threshold_fact_episode`. Fix the comment while in the file.

---

## 6. RESOLVED — no flag needed

**Question:** once `record_decision` sets `session_id`, does `DecisionReviewer` newly auto-label
decisions at scale — repeating the #577 corruption?

**Answer: no. Measured zero.** Verified against prod:

| | |
|---|---|
| decisions with NULL `session_id` | 718 |
| …of those, still unreviewed | 93 |
| …that `ErrorSignal` would auto-fail (`COALESCE(confidence_raw, confidence) < 0.4`) | **0** |

Two independent reasons the risk is nil: `sweep()` is not session-scoped, so these decisions are
already exposed to the same signals today (§1.1); and `ErrorSignal` is the **only** wired signal
(`decision_reviewer.py:232-234`) and its sole remaining branch is a `< 0.4` low-confidence prior
that **no** unreviewed NULL-session decision trips.

**Ships without a feature flag.** Re-run this query before merge to confirm it still holds.

---

## 7. What v1 got wrong (kept deliberately)

1. **"Four live readers waiting for rows."** The most important one was *deliberately decommissioned*
   eight weeks ago. I read the cited function but not its registration context.
2. **Circular rejection of the session_id mechanism.** v1 rejected it because `session_id` is NULL on
   71.4% of decisions — but it is NULL *precisely because* of the bug being fixed. The "15 ambiguous
   decisions" objection also dissolves: decision→*which* episode is consumed **only** by the
   decommissioned `EpisodeSignal`; both live readers want episode→decisions-in-session, a set, which
   is unambiguous.
3. **O4 was already answered** in a comment nine lines above the code v1 cited.

---

## 8. Test plan

1. `record_decision` through the dispatcher → `decisions.session_id` populated.
2. `record_decision` with no session → succeeds, `session_id` NULL, no exception.
3. `get_session_decisions` returns tool-recorded decisions (regression for the 718).
4. `EpisodeDetail` exposes `session_id`; `decision_ids` gone — update every consumer/test.
5. Densifier + sleep-handler legs produce the same edges via the `session_id` join as the old join
   would have, on seeded data.
6. Full-suite failure-set diff against baseline (the #577 lesson: snapshot breaks are only visible
   this way).

## 9. Rollback

Steps 1–3 are additive — revert and `session_id` simply stops being set. Steps 4–8 are reader
rewrites, revertible with the commit. Step 10 (table drops) is irreversible but the tables have
**0 rows, 0 readers, 0 writers** once 4–8 land; ship it as the final commit so it can be dropped
from the PR if anything upstream regresses.
