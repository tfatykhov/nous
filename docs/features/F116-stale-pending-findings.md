# F116: Stale "Pending action" / "Unresolved commitment" findings

**Status:** PR 1 (this page and migration 119, `heart.pending_resolutions`; no behaviour change) and PR 2 (L1, L4 and L3a in `nous/heartbeat/pending_filter.py`, each with its own mode, default `off`; see [5.6](#56-pr-2-as-built-l1-l4-and-l3a)) in review; PR 1 supersedes #952. PR 3 to PR 6 not started; see [Rollout](#53-rollout). Every layer ships behind its own setting, default `off`. Design approved by Tim on 2026-11-21 with all the defaults in [Decisions](#9-decisions-tim-2026-11-21).
**Base:** code references are at `origin/main` `bf260047` (re-checked 2026-11-21 when this page was written); lines drift, re-grep at build.
**Related:** Issue #369 (closed), F047 actionability, F034.1/F034.4 finding lifecycle, [F111](F111-obligation-ledger.md) obligation ledger, PR #711 (known false positives), PR #503 / #786 (shipped parts of #369).

## 0. Feature and migration numbers

F116 was free on 2026-11-21: `docs/features/` has files up to F111, `INDEX.md` lists up to F115, and no PR or branch mentions F116 (`gh pr list --state all --search F116` is empty). F109 is claimed by a draft outside the repo. The newest migration on `main` is `118_goal_commands_mode.sql` and no open PR adds a `119_*` file, so this feature takes **119**.

## 1. Goals and non-goals

**Problem in numbers (heartbeat_tick events, last 30 days, 1,429 ticks):** the self-initiated check emitted 3,853 "Pending action" findings but only **18 distinct summaries**. The top 14 account for nearly all of them; 11 map to one fact each by `content[:100]`, and most are status snapshots of finished work. Each repeat can wake a triage turn (1,043 `heartbeat_triage` events since April; the Nov 21 one spent 1,644 tokens on four findings).

**Goals**
1. Stop the recurring false-positive classes below without Tim or Nous hand-writing a rule per sentence.
2. Close a stale item once, with a recorded reason, so it does not refire.
3. Keep every layer cheap and deterministic: DB reads and at most a few cached GitHub calls per tick.
4. Fail open: if a check cannot decide, the item still surfaces.
5. Ship each layer alone, behind its own setting, with a shadow mode that records "would suppress".

**Non-goals**
- Replacing the heartbeat, FindingStore, or the F047 classifier.
- An LLM call per fact per tick. (The existing write-time Haiku classifier in `actionability.py` is unchanged.)
- Fixing `_temporal_scan` "Approaching deadline" findings (261 in 30 days, weekday-word matches). That is a separate follow-up feature ([decision 8](#9-decisions-tim-2026-11-21)); the L2/L4 hooks are reusable there.
- Replacing F111. F111 tracks promises made going forward; this feature cleans the legacy fact/episode scan.

## 2. Current behaviour

### 2.1 Three emit sites in `nous/heartbeat/checks.py` (`SelfInitiatedCheck`, class at :230)
| Site | Lines | What it does |
|---|---|---|
| `_embedding_search` | :293–362 | For each of 5 `PENDING_PROTOTYPES` (:184–190) runs `facts.search(limit=5)`. Skips `category == "person"` and tags `resolved`/`identity` (:324–327). Uses persisted `fact.actionable` (:334–348); NULL falls back to `_looks_like_pending` / `_is_observation` / score. Emits `"Pending action: {fact.content[:100]}"` (:354). |
| `run()` keyword fallback | :517–545 (`run` at :501) | Searches `"follow-up pending action TODO"` with **`category="rule"`** (:518). Skips `actionable == False` (:533); emits on `actionable == True` or `_looks_like_pending` (:535, summary :539). |
| `_promise_scan` | :367–431 | 5 hardcoded queries over episodes. Flags `outcome == "ongoing"` or `48h < age <= max_stale_age_hours` for non-terminal outcomes (:399–407). Skips `_is_question_form` and `_is_system_envelope` (:411). Emits `"Unresolved commitment: …"` (:416). |

None of the three looks at: the fact's category beyond `person`, whether it is a triage/suppression note, its age, `superseded_by` / `active`, the source (`enumerative_extractor` vs. `user_direct`), or whether a PR, subtask, DAG or schedule it names is finished.

### 2.2 Identity and suppression downstream
- `Finding.fingerprint()` (`nous/heartbeat/schemas.py:36–40`) = sha256 of `check_name:source:summary` with digits replaced by `N`, first 16 hex. The key is the **first 100 characters of the text, not the fact id**. `raw_data` carries `fact_id` / `episode_id` but is not part of identity.
- `FindingStore.ingest` (`finding_store.py:51`, fingerprint at :60) is in memory only. `_known_fp_match` (:124–138) returns no rule for `urgency == "high"`, escalated findings, or findings already NEW/ACKNOWLEDGED, so rules close a finding only at first sight. A restart clears the state (`_in_startup_suppression`, :445).
- `KnownFalsePositiveRules` (`known_fp.py:138`, `match` at :163; `KnownFalsePositiveRule.matches` :59–69) matches `check` / `contains` / `regex` against `finding.summary` only (the 100-character text). Rules need `review_by` (hard stop; maximum horizon enforced). File: `NOUS_HEARTBEAT_KNOWN_FP_PATH`, default `/tmp/nous-workspace/heartbeat/known_false_positives.json` (`config.py:2365`); wired at `main.py:1730–1734`; added by PR #711 (merged 2026-10-08).
- Runner: `ingest` at `runner.py:998`; the `heartbeat_tick` event at `runner.py:919` carries `findings[]` (source, summary, urgency, check_name), `by_source`, `by_urgency`. There is **no findings table**: the store is memory plus these events. Events keep about 30 days.

### 2.3 Actionability (F047)
`ActionabilityClassifier` (`nous/heart/actionability.py:135`): `_HARD_NO_CATEGORIES = {"person", "preference"}` (:138); **`rule` is not included**. Tiers: hard filter, substring heuristics, Haiku, default fail-closed. Called at write time from `facts.py:1102`. Seen in the DB: rule facts `6b90518b` and `50dd4e68` are `actionable=true`, confidence 0.80. 90 rule facts learned in the last 90 days are `actionable=true` (78 still active).

### 2.4 Supersession today
`facts.py` marks the old fact `active=false`, `superseded_by=<new>`, reason `superseded` (:1474, :1786, :1942; helper `mark_fact_inactive` :203; reasons `FactDeactivationReason` :198–200). Search soft-penalizes (x0.3) or drops superseded facts (`apply_supersession_filter`, :597–628). The heartbeat paths never check `superseded_by` or `active` themselves; they depend on search. Fact `8f248bc2` is now `active=false`, reason `merged`, yet fired 249 times in the 30-day window.

### 2.5 What Issue #369 actually shipped
| Layer | Status | Evidence |
|---|---|---|
| A. Question-form filter | **Shipped for episodes only** (PR #503, `e5e8344d`; wh-start or trailing `?`, filler prefix stripped) | `_is_question_form` :202–210, used :411. Not applied to facts. |
| B. Max stale-age cap | **Shipped for episodes only**, tunable `max_stale_age_hours` 336 h (72–720) (:261–273) | `outcome == "ongoing"` is deliberately **uncapped** (:392–407), so a stuck "ongoing" episode refires forever. No cap for facts. |
| C. Auto `actionable=False` on resolution (superseded fact or closed PR/issue) | **Not shipped.** | No code writes `actionable=False` outside the classifier; the only hit for "actionable=False" in `nous/heartbeat/` is a comment (`checks.py:529`). |
| Extra (PR #786, `d7ed01a4`, Oct 20) | Shipped: skip subtask-result envelope and terminal-outcome episodes in `_promise_scan` | `_ENVELOPE_PREFIXES` :218, `_TERMINAL_OUTCOMES` :222, `_is_system_envelope` :225. Result: **0** "Unresolved commitment" findings in the 30-day tick window. |

So layer C, the one that handles the recurring "referenced work is finished" class, was never built. Related tests: `tests/test_heartbeat_actionability_integration.py`, `tests/test_heartbeat_known_fp.py`.

## 3. False-positive classes (fixtures)

Ids were seen in the DB. Counts are firings in the 30-day tick window where the fact could be matched by `content[:100]`.

| # | Class | Fixture | Why it fires | Layer that fixes it |
|---|---|---|---|---|
| 1a | Finished-work narrative with an id | `78ccacac` "The gate check (subtask 8f2bfae0) found the gate met and stopped, waiting for the user's approval" (225x). Subtask `8f2bfae0` is `completed` (2026-10-08); PR #711 merged 2026-10-08. | "waiting for … approval" | L2 (subtask terminal), L4 (snapshot TTL) |
| 1b | Same, PR numbers | `8f248bc2` "requester is re-reviewing the heads of #800 … and #801" (249x); both PRs merged | "re-reviewing" | L2 (PR merged), L3 (now inactive) |
| 1c | Same, no id | `4ecbeeea` "verdict of the v3 re-review is READY FOR A REAL RUN, PENDING TIM'S DECISIONS" (639x); `a02b0102` "Reviewing is the work currently in progress." (DAG done) | wording only | L4 (snapshot TTL) and L3b (structured close). L2 cannot help: no referent in the text. |
| 2 | Standing rules | `6b90518b` (107x), `50dd4e68` (186x), category `rule`, `actionable=true` | the classifier lacks `rule`; the keyword path searches only `category="rule"` (:518) | L1 |
| 3 | Triage/suppression notes | `55dfa56b` "Heartbeat triage Nov 2 2026 (LOCAL-ONLY): "Pending action: Revie…" (161x, `user_direct`, `actionable=true`); `cba7d135` and `32eaf959` (both `actionable=false`) | the note quotes the phrase it suppresses | L1 (note filter). `cba7d135` is inactive and `32eaf959` is `actionable=false`, so the fact paths already skip them; their remaining route is episodes. |
| 4 | Subtask-result header | facts `959d299d`, `31d61953` describe it; the episodes are already skipped since PR #786 | envelope text matches promise queries | L5 (regression lock plus gaps) |
| 5 | Suppression note does not stop the source | notes exist (62 facts start "HEARTBEAT FALSE-POSITIVE" in the last 90 days; 6 are not `actionable=false`), source facts keep firing | no link between note and source | L3b |

Most fixtures are `source = enumerative_extractor` facts: point-in-time status lines extracted from a finished episode (`78ccacac`, `4ecbeeea`, `a02b0102`, `8f248bc2`, `9aede1f5`, `17f06c77`), 19–44 days old on 2026-11-21. That is the cheapest discriminator available and drives L4.

## 4. Design

One new module `nous/heartbeat/pending_filter.py` with `evaluate(candidate) -> Verdict(keep|drop, reason, evidence)`, called at the three emit sites in `checks.py` (near :344, :534, :409) before a `Finding` is built. Layers run cheap to expensive: L1, L4, L3a, L5, L2. The first `drop` wins. Each layer has `NOUS_HEARTBEAT_PENDING_<LAYER>_MODE` = `off` (default at merge) | `shadow` (evaluate, record, still emit) | `on`. Settings go next to `heartbeat_known_fp_path` (`config.py:2365`).

Candidate fields: kind (`fact`/`episode`), id, text, category, tags, source, `actionable`, `learned_at`/`last_confirmed`/`event_date`, `active`, `superseded_by`, `subject_key`/`attribute_key` (all already columns on `heart.facts`), episode `session_id`/`outcome`/`started_at`.

### L1. Kind filter (`..._KIND_MODE`)
- Skip `category in {rule, preference, person}` on both fact paths (extends :324–327 and adds it to the keyword path).
- Skip facts that are triage or suppression notes: content starts with `HEARTBEAT FALSE-POSITIVE` or `HEARTBEAT SUPPRESSION` in capitals, or `Heartbeat triage` in any case followed by a date (`Nov 2 2026`, `2026-11-02`) or by a colon and a quote (`Heartbeat triage: "Pending action: ...`), or carries tag `hb-note`. A task that only starts with these words ("Heartbeat triage: ask the owner ...", "Heartbeat suppression TTL should be raised") is not a note. Same prefixes for episode summaries from heartbeat sessions (L5).
- **Write time:** add `rule` to `_HARD_NO_CATEGORIES` (`actionability.py:138`); add a Tier-0 rule returning `(False, 1.0, "hard_filter_note")` for the note prefixes. Fixes new facts without relying on read-time filters.
- **Backfill:** run the existing `nous/handlers/actionability_backfill.py` (dry-run default) for the 78 active rule facts and the 6 notes. Tim reviews the dry-run list first.
- Keyword fallback (:517–521): today it searches only `category="rule"`. With L1 on it would return nothing useful. Change it to an unfiltered search with L1 applied after, or drop the path; decide at build after reading `facts.search`'s filter options.

### L2. Referent resolution (`..._REFERENT_MODE`; sub-settings `_DB`, `_GITHUB`)
Extract from the text (pure regex, no LLM):
- PR/issue numbers: `#\d{2,5}`, `PR #n`, `owner/repo#n` (default repo `NOUS_GITHUB_DEFAULT_REPO`, token `github_token`, `config.py:1081`).
- Ids following `subtask|DAG|schedule|job|decision` within 30 characters: 8-hex short ids or full UUIDs; `job-YYYYMMDD-HHMMSS-hex8`.

Resolve:
| Referent | Source | Terminal when |
|---|---|---|
| subtask | `heart.subtasks.status` | `completed`, `failed`, `cancelled` (not `pending`, `running`) |
| DAG | `nous_system.execution_dags.status` | `completed`, `failed`, `cancelled` |
| schedule | `heart.schedules.active` | `active = false` (a one-shot schedule that fired also counts) |
| PR / issue | GitHub `GET /repos/{repo}/issues/{n}` (works for both) | `state = closed` (merged or not) |
| job | only if the runner's status is readable in-process; otherwise not resolved | out of v1 unless confirmed at build |

Short-id lookups use `id::text LIKE 'prefix%'` and count as resolved only if exactly one row matches (ambiguous = unknown).

Decision: **drop only if at least one referent resolved and every referent is terminal.** Any non-terminal or unknown referent keeps the item. No referents = L2 is neutral.

GitHub safety: in-process cache (terminal results 24 h, open results 10 min), conditional requests with ETag, a per-tick budget (default 10 lookups) and a 3 s timeout each; budget exhausted, 403/429, timeout or any error = unknown = keep, logged once per tick. DB lookups are batched into one query per table per tick. DB referents ship first, GitHub one step later ([decision 4](#9-decisions-tim-2026-11-21)).

On drop, L2 **closes** the item (see [5.1](#51-migration-119-additive-one-table)): the fact gets `actionable=false` with reason `referents_terminal`, so it does not come back. Shadow mode only records. Closing is allowed after one clean week in shadow ([decision 3](#9-decisions-tim-2026-11-21)).

### L3. Supersession and structured suppression (`..._SUPERSEDE_MODE`)
- **L3a, read side:** skip a fact if `superseded_by IS NOT NULL` or `active = false` at emit time, and if a newer active fact exists with the same non-null `(subject_key, attribute_key)`. One indexed query per candidate. Closes `8f248bc2`-style leaks between deactivation and search refresh.
- **L3b, structured link:** the table `heart.pending_resolutions` ([5.1](#51-migration-119-additive-one-table)). The `learn_fact` tool (`nous/api/tools.py:1735`) gets optional `suppresses: [fact_id|episode_id]` and `suppress_reason`. A call writes the note fact (actionable=false by L1) and one resolution row per target, with `review_by` (default 60 days, maximum 120, mirroring `known_fp.py`'s no-permanent-rules stance; [decision 5](#9-decisions-tim-2026-11-21)). The target's `actionable` is not changed: only the row hides it, through the table lookup at read time, so the suppression ends by itself at `review_by` ([5.1](#51-migration-119-additive-one-table)). `evaluate` checks the table by id first. No more free-text "do not re-flag" notes needed.
- **L3c, legacy notes (shadow only in v1):** parse fact-id prefixes inside existing "HEARTBEAT FALSE-POSITIVE" notes ("fact 07a88e08") and report which sources they would close. Never auto-apply from free text; Tim reviews the list.

### L4. Age, snapshot and question-form filters (`..._AGE_MODE`)
- **Snapshot TTL:** facts with `source = enumerative_extractor` are point-in-time status lines. Surface them only while `learned_at` (or `last_confirmed`) is within `snapshot_ttl_hours` (default **336 h = 14 days**, [decision 2](#9-decisions-tim-2026-11-21); tunable like `max_stale_age_hours`). Exempt: `event_date` in the future; `actionable_confidence >= 0.95`.
- **Other facts:** cap at `fact_max_age_hours` (default 720 h) unless `event_date` is in the future or `last_confirmed` is recent.
- **Episodes:** apply the same 720 h ceiling to `outcome == "ongoing"` (today uncapped, :392–407).
- **Question form for facts:** reuse `_is_question_form` (:202) on fact text. Low value, cheap.
- **Visibility:** items hidden by age are not silent. One low-urgency digest finding per day: `"Pending items hidden by age cap: N"` (digits normalize, so one fingerprint), with ids in `raw_data`.

Expected effect on the 30-day sample: each of the six `enumerative_extractor` fixtures would have stopped firing 14 days after it was learned (today they are 19–44 days old and still firing).

### L5. Episode boilerplate and self-reference (`..._EPISODE_MODE`)
Already shipped (PR #786): envelope prefix skip and terminal-outcome skip. Remaining:
- Lock it with regression tests and counters (`episode_envelope`, `episode_terminal`).
- Skip episodes whose `session_id` starts with `heartbeat-` (the heartbeat's own triage turns; [decision 9](#9-decisions-tim-2026-11-21)). Reason `episode_self`.
- Run L2 on episode summaries (they name subtasks, DAGs, PRs). A resolved episode cannot be "closed" with `actionable`, so L2 writes a `heart.pending_resolutions` row for it.
- `_promise_scan` stays ([decision 6](#9-decisions-tim-2026-11-21)); revisit after F111 PR B2.

### L6. `known_false_positives.json` (unchanged)
Keep the file and reader as the manual, expiring override of last resort (human-readable, `review_by` enforced, hot reload; [decision 7](#9-decisions-tim-2026-11-21)). Changes:
1. Stop adding rules for recurring classes; use L3b instead.
2. Two sharp edges are documented in the example file, with no code change in v1: rules match only the 100-character summary (`known_fp.py:65–68`), and are bypassed for `urgency == "high"` or already-NEW findings (`finding_store.py:133–138`).
3. **The 10 current rules** (all have `review_by` 2026-11-30 or 2027-01-15): after the replay ([section 8](#8-replay-evaluation-read-only)), rules the new layers already cover are not renewed and are removed at expiry. Expected coverage: one rule about PR work in progress (L2: #641 merged), one about a pending report (L2, if the full fact text names DAG `dd943554`), one that matches a suppression note (L1). Likely still needed: one review the user put off (a user deferral, no referent; L3b), three rules from the deadline scan (temporal scan, out of scope), three rules about status lines of a sign-off, pending decisions and unfinished work (L3b or L4). Review on 2026-11-30.

## 5. Data model, settings, rollout

### 5.1 Migration 119 (additive, one table)
`heart.pending_resolutions`: one row per fact or episode a layer (or the owner) closed, with the reason and its evidence. The DDL is in the [appendix](#appendix-the-ddl-of-migration-119).

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` pk | `gen_random_uuid()` |
| `agent_id` | `text` not null | agent-scoped like every table |
| `subject_kind` | `text` not null | `fact` or `episode` |
| `subject_id` | `uuid` not null | the fact or episode id; no foreign key (it points at one of two tables, and memory is soft-deleted) |
| `reason` | `text` not null | `referents_terminal`, `superseded`, `suppressed_by_note`, `age_cap`, `owner_closed` |
| `evidence` | `jsonb` not null, default `{}` | referents and their states, `prior_actionable`, `note_fact_id` |
| `created_by` | `text` not null | `heartbeat`, `tool` or `owner` |
| `created_at` | `timestamptz` not null, default `now()` | |
| `review_by` | `timestamptz` | when the close expires; required for `suppressed_by_note` (decision 5); NULL for a close on a terminal referent, which does not expire |
| `reopened_at` | `timestamptz` null | set when the close is undone |

`UNIQUE (agent_id, subject_kind, subject_id)`: one resolution per item. No new columns on `heart.facts`. A row hides its item while `reopened_at IS NULL AND (review_by IS NULL OR review_by > now())`. A close without `review_by` (a terminal referent, an owner's close) also sets the fact's `actionable=false`, `actionable_confidence=1.0` through the existing fact manager and stores `prior_actionable` in `evidence`. A close with `review_by` (a suppression) does not change `actionable`: the fact paths skip `actionable == False` before a finding is built (`checks.py:338`, :533), so a suppression that set it would never end. It is enforced only by the table lookup at read time and ends by itself at `review_by`. Reopen = set `reopened_at`, and restore `prior_actionable` if the close stored one. Closing an item whose row was reopened, or whose `review_by` has passed, updates that row: new `reason`, `evidence`, `created_at` and `review_by`, and `reopened_at` back to NULL.

No grant: the app owns and reads the table, and `scripts/ops/db_roles.sql` is not touched (the `db_query` reader is not granted it). `sql/init.sql` is not changed: like the other recent tables, the table comes from the migration. There is no down migration in this repo; the rollback is `DROP TABLE IF EXISTS heart.pending_resolutions;` plus deleting the `119` row from `nous_system.schema_migrations`, safe while no layer writes the table.

Fingerprints are unchanged: no new ids. Resolution rows reference existing fact and episode ids; observability uses `Finding.fingerprint()` computed from the would-be summary.

### 5.2 Settings (all default `off` at merge)
`NOUS_HEARTBEAT_PENDING_{KIND,AGE,SUPERSEDE,EPISODE,REFERENT}_MODE` = off|shadow|on; `NOUS_HEARTBEAT_PENDING_REFERENT_GITHUB` (bool), `..._GITHUB_BUDGET` (10), `..._SNAPSHOT_TTL_HOURS` (336), `..._FACT_MAX_AGE_HOURS` (720). Rollback for any layer: set it to `off`; closed items stay closed until `reopened_at` is set or their `review_by` passes (a reopen script ships with the layer that first writes rows).

### 5.3 Rollout
Each step is its own small PR.
1. **PR 1 (this one):** this page and migration 119. No behaviour change.
2. **PR 2:** L1 + L4 + L3a, all in `shadow` (no network). Run the replay ([section 8](#8-replay-evaluation-read-only)) after 3 days. Gate: no "would suppress" on the control fixtures ([7.6](#7-test-plan)); Tim sees the 18-summary table.
3. **PR 3:** turn L1 + L4 + L3a `on`. Run the L1 backfill after Tim approves its dry-run.
4. **PR 4:** L2 with DB referents (subtask, DAG, schedule); closes after one clean week in shadow.
5. **PR 5:** L2 GitHub referents.
6. **PR 6:** L3b, the `learn_fact` `suppresses` parameter; triage turns start using it.

`known_false_positives.json` is reviewed at the 2026-11-30 expiry (L6), whichever PR has landed by then.

### 5.4 Observability
- `heartbeat_tick` event data (`runner.py:919`) gains `suppressed: {reason: count}`, `suppressed_items: [{fingerprint, kind, id, reason}]` (capped at 20) and `shadow_suppressed` (same shape). The check exposes `last_suppressed` for the runner to copy. No new table for metrics.
- Reasons: `kind_rule`, `kind_note`, `age_snapshot`, `age_fact`, `age_ongoing`, `superseded`, `inactive`, `closed_by_note`, `referents_terminal`, `episode_envelope`, `episode_terminal`, `episode_self`, `question_form`.
- Counters on `/heartbeat` stats beside `auto_closed_by_rule` (`finding_store.py:418`).
- Daily digest finding (L4) plus a WARNING when GitHub lookups fail more than 50% in a tick.

### 5.5 Failure modes (fail open)
| Failure | Behaviour |
|---|---|
| DB lookup error or timeout | referent unknown, item surfaces; one WARNING per tick |
| GitHub 403/429/5xx, no token, budget spent | unknown, item surfaces |
| Ambiguous short id (two matches) | unknown, item surfaces |
| Mixed referents (one open) | surfaces |
| Resolution write fails after a drop decision | item is dropped for this tick only; next tick re-decides (no half-closed state) |
| Regex false extraction ("#1" in prose) | fewer than 2 digits ignored; a nonexistent PR = unknown = surfaces |
| Setting parse error | treated as `off` |
| `evaluate` raises | caught per candidate; candidate surfaces; ERROR counter |

A real open item must never disappear without a row in `pending_resolutions` or a counter increment plus the daily digest.

### 5.6 PR 2 as built: L1, L4 and L3a

The self-initiated check calls the filter last on each of its three paths, right before it would build a finding, so the filter only counts an item that passed every existing check, and a hidden item takes no `max_pending_items` slot. (The promise scan fills up to `max_pending_items` of its own before the check keeps the slots the fact paths left, so an episode past those slots is screened and counted too.) With every mode `off` the check builds no per-run state and calls nothing of the filter. The fingerprint recorded for an item is the one the finding would have had.

| Setting | Default | Range |
|---|---|---|
| `NOUS_HEARTBEAT_PENDING_KIND_MODE` | `off` | `off`, `shadow`, `on` |
| `NOUS_HEARTBEAT_PENDING_AGE_MODE` | `off` | `off`, `shadow`, `on` |
| `NOUS_HEARTBEAT_PENDING_SUPERSEDE_MODE` | `off` | `off`, `shadow`, `on` |
| `NOUS_HEARTBEAT_PENDING_SNAPSHOT_TTL_HOURS` | `336` | 24 to 2160 |
| `NOUS_HEARTBEAT_PENDING_FACT_MAX_AGE_HOURS` | `720` | 72 to 8760 |
| `NOUS_HEARTBEAT_PENDING_EPISODE_MAX_AGE_HOURS` | `720` | 72 to 8760 |

A mode that is not one of the three words is `off`. The settings are read when the check is built, so a change needs a restart. The episode limit is a setting of its own. `..._EPISODE_MODE` and `..._REFERENT_*` come with L5 and L2.

How the code relates to the design above:

1. The search result the check gets carries neither `last_confirmed` nor the slot keys. The filter reads them with one query per search result list (named columns of `heart.facts`). The reads of one check run share 2 seconds in all: each read gets what is left, and once it is spent, or after a failed read, no other read runs in that check run and no item is hidden by a rule that needs the row.
2. No new index: the same-key lookup uses `idx_facts_conflict_slot` (migration 064).
3. `inactive` is a defensive rule: search returns active facts only and builds each result from the same row.
4. A newer active fact with the same subject and attribute keys is recorded as `newer_same_key` in shadow only, whatever the mode. The write path keeps two active facts with the same keys and no marker when it judged them compatible (intervals that coexist, a different `event_date`, a relation that is neither an update nor a contradiction, a confidence under 0.8, no classifier answer), and two open tasks about one subject can look like that. A pair linked by `contradiction_of` is not counted.
5. The question rule on facts is a trailing `?` only (after a leading ok or hey), so a fact such as "What remains: send the invoice" is a task. It is a rule of the AGE mode and shares its exemptions: a question with an `event_date` today or later, or a stored actionability confidence of 0.95 or more, surfaces. Episodes keep the check's existing question test.
6. With a mode not `off`, a tick whose findings were all hidden still writes its `heartbeat_tick` event, with `findings_count` 0 and no triage.
7. The report travels on the check's result (`CheckResult.suppression`), not on an attribute of the check: a run started from `POST /heartbeat/check/{name}/trigger` can run beside a tick. The runner adds a tick's reports to the event; the report of a started run is returned with it and not counted.
8. The counts since start are on the runner (`get_stats()["pending_filter"]` and `GET /heartbeat/status`), not in the finding store, which knows only the findings it ingested.

The daily line (AGE `on`): "Pending items hidden by age cap: N" counts every item the AGE mode hid in that run, questions included (the tick event's `suppressed` has the count by reason), with up to 20 ids and the UTC date in `raw_data`. Each run adds it until a heartbeat tick has taken that day's line; the day is then marked in memory, so a restart can repeat it that day, and a run started from `POST /heartbeat/check/{name}/trigger` returns it without using up the day's line. It then goes through the tick like any finding: the finding store takes it and the triage step routes it, it starts no model turn because `needs_action` is false, and once acknowledged it counts toward the check's accumulation threshold (`NOUS_HEARTBEAT_ESCALATION_ACCUMULATION_THRESHOLD`) and is listed in the daily digest.

The keyword fallback searches rule facts only (`category="rule"` filters both search legs), so with KIND `on` every result of that search is hidden. The call is kept as it is: with KIND `off` or in `shadow` it runs as before and is measured. With KIND `on` and no embedding provider the check raises no fact finding.

The write-time half of L1 (`rule` in `_HARD_NO_CATEGORIES`, the note prefixes) and the L1 backfill come with PR 3, when KIND is turned on: a verdict stored on a fact outlives the setting.

Known limits to measure in shadow:

- A duplicate learn confirms the existing fact (`FactManager._confirm`), which refreshes `last_confirmed`, so a status line that is stated again does not age out.

## 6. Interaction notes
- F047 stays the write-time verdict; this feature adds read-time gates and one reason-bearing close path.
- F111 `brain.commitments` is the right tracker for new promises. Once the F111 watcher is on, `_promise_scan` can be narrowed further (decision 6 keeps it for now).
- FindingStore auto-resolve (`runner.py`, around :960–980) closes ACKNOWLEDGED findings absent for N ticks, so a source fact closed here clears its tracked finding without extra code.

## 7. Test plan
New file `tests/test_heartbeat_stale_pending.py` (unit, mocked heart/facts/GitHub), plus extensions to `tests/test_heartbeat_actionability_integration.py`. Fixtures are the real texts from section 3, built as `Fact`/`Episode` stubs.

1. **L1:** (a) `6b90518b`, `50dd4e68` stubs (`category=rule`, `actionable=True`) produce no finding on the embedding and keyword paths. (b) `55dfa56b`, `959d299d`, `31d61953`, `32eaf959` stubs are skipped by prefix; also with `actionable=None`. (c) the classifier returns `(False, 1.0, "hard_filter")` for `rule`, `"hard_filter_note"` for the prefixes. (d) control: a `technical` fact "TODO: send the invoice" still surfaces.
2. **L2:** (a) `78ccacac` text with subtask `8f2bfae0` `completed` is dropped and closed (`actionable=false`, resolution row with reason `referents_terminal`). (b) `8f248bc2` text with #800 and #801 both `closed` is dropped; with #801 `open` it surfaces. (c) unknown PR (GitHub 404/timeout) surfaces. (d) ambiguous short id surfaces. (e) GitHub budget of 1 with 3 PRs: lookups stop, item surfaces. (f) cache: second tick makes zero requests for a terminal PR. (g) **regression:** "waiting on Tim to merge #NNN" where the stubbed PR is `open` surfaces and is not closed.
3. **L3:** (a) `superseded_by` set or `active=false` skipped. (b) a newer fact with the same `(subject_key, attribute_key)` skips the older one. (c) `learn_fact(suppresses=[id])` creates the note plus a resolution row and leaves the source's `actionable` as it was; the source stops firing; after `review_by` it fires again with no write. (d) the legacy-note parser (shadow) reports targets and writes nothing. (e) a close without `review_by` sets `actionable=false`; `reopened_at` restores the old `actionable`; closing it again updates the one row (new reason, evidence, `created_at` and `review_by`, `reopened_at` NULL).
4. **L4:** (a) `4ecbeeea`, `a02b0102` stubs (`enumerative_extractor`, 21 and 26 days old) are dropped at a 14-day TTL and kept at 30; (b) `event_date` tomorrow exempts; (c) an `ongoing` episode older than 720 h is dropped, a younger one surfaces; (d) the digest finding appears once with the count; (e) fact "Who owns the deploy?" dropped as question form.
5. **L5:** the header `[Background subtask result — unverified output…` episode (ongoing and non-terminal) never produces "Unresolved commitment"; a `heartbeat-` session episode is skipped; a real `ongoing` episode "I'll draft the report" surfaces.
6. **Controls (must always surface):** open-PR-awaiting-Tim fact, fresh `user_direct` task fact, a fact with a future `event_date`, and an episode `ongoing` for 3 days. Run them under every mode combination (all `on`, each alone).
7. **Modes and failure:** `shadow` never changes the emitted findings but fills `shadow_suppressed`; `off` is byte-identical to current output (golden test); every failure row in 5.5 has one test; fingerprints of surviving findings are unchanged.
8. **Migration (PR 1, `tests/test_pending_resolutions_schema.py`):** the DDL applies twice cleanly; the unique key rejects a duplicate; the CHECKs reject a bad `subject_kind`, `reason` or `created_by` and a `suppressed_by_note` row without `review_by`; the migration file equals the appendix.

## 8. Replay evaluation (read-only)
Populations. No findings table exists; use `nous_system.events`:
- `heartbeat_tick`: `data->'findings'` = `{source, summary, urgency, check_name}` per tick (1,429 ticks, 5,922 findings in 30 days). 3,853 are `self_initiated` "Pending action" with 18 distinct summaries; 0 are "Unresolved commitment".
- `heartbeat_triage`: `data` has `session_id`, `findings_count`, `response_summary` (free text, truncated).
- Columns: `nous_system.events` (id, agent_id, session_id, event_type, data jsonb, created_at, event_id, trace_id, caused_by); `heart.facts` includes `actionable`, `superseded_by`, `source`, `learned_at`, `last_confirmed`, `event_date`, `subject_key`, `attribute_key`, `deactivation_reason`.

Plan (a read-only script; read-only `db_query` or a read-only psql role; no writes):
1. **Dedupe:** `jsonb_array_elements(data->'findings')`, group by `summary` → about 18 rows with counts, first/last seen, urgency mix. Compute the fingerprint offline with the same formula as `schemas.py:36–40`.
2. **Join to source:** `heart.facts` where `left(content,100) = substr(summary, length('Pending action: ')+1)` (worked for 12 of 14 top summaries; the rest need a fuzzy/`ILIKE` join or are rule-closed). Pull category, source, learned_at, actionable, active, deactivation_reason, superseded_by, `source_episode_id`.
3. **Labels (ground truth), in priority order:**
   a. a later "HEARTBEAT FALSE-POSITIVE …" or triage note naming the fact id prefix or quoting the summary (62 notes in 90 days);
   b. a `known_false_positives.json` rule that matches the summary (rules are Tim-approved triage outcomes);
   c. the fact later `active=false` with reason `merged`/`superseded`/`stale`, or `actionable` flipped to false;
   d. Tim's decisions recorded as "Triage … heartbeat" in `brain.decisions` (via `recall_deep`, not SQL);
   e. residue: hand-label the ≤18 summaries and mark each `FP`, `TP` (genuinely open) or `?`.
4. **Run the filter offline:** reimplement `evaluate` over the joined rows, with referent states read from `heart.subtasks`, `nous_system.execution_dags`, `heart.schedules` and `gh issue view` (cached), **evaluated as of each summary's first-seen time** where possible (subtask `completed_at`, DAG `completed_at`, PR `closedAt`), so a layer is not credited for a state that did not exist yet.
5. **Metrics, per layer and combined, weighted by firings and by distinct summary:**
   - recall = FP firings suppressed / all FP firings;
   - precision = suppressed that are FP / all suppressed;
   - **hard gate: zero TP suppressed**; any TP suppression blocks `on`;
   - also firings removed per day and the share of `heartbeat_triage` turns whose findings would all have vanished.
6. **Output:** a table in `research/heartbeat-stale-pending/replay-YYYY-MM-DD.md` with each summary, label, label source, per-layer decision and reason. Repeat in shadow mode live (`shadow_suppressed` in tick events) for 3 days as a second check; the live data also gives `fact_id`, which the old events lack.

Limits: only about 30 days of ticks; tick events omit `fact_id`; labels from free-text notes are noisy, which is why the ≤18-row hand review is the tie-breaker.

## 9. Decisions (Tim, 2026-11-21)
Tim accepted every recommended default.
1. **Rule, preference and person facts never count as tasks** (hard filter, L1). A rule like "remind me on Friday" has to be a schedule or a commitment instead.
2. **Snapshot TTL for extractor-made status facts: 14 days** (L4); the daily digest line shows what was hidden.
3. **Auto-close on terminal referents** (set `actionable=false` with a reason row, reversible by `reopened_at`) is allowed after one clean week in shadow with zero true positives.
4. **DB referents first** (subtask, DAG, schedule); GitHub PR lookups one step later (10 lookups per tick, cached, existing token).
5. **Structured suppression:** `learn_fact` gets `suppresses`; free-text "do not re-flag" facts stop; `review_by` defaults to 60 days so nothing is hidden forever.
6. **Keep `_promise_scan`** for now, with L5; revisit after F111 PR B2.
7. **Keep `known_false_positives.json`** as the expiring manual override; don't renew rules the replay shows are covered; review on 2026-11-30.
8. **"Approaching deadline" findings** go to a separate follow-up feature (its own F-number) reusing L2/L4.
9. **Skip `heartbeat-*` session episodes** in `_promise_scan`.

## Appendix: the DDL of migration 119

`sql/migrations/119_pending_resolutions.sql` holds exactly this block (a test compares the two, and applies it twice).

```sql
-- 119: heart.pending_resolutions, why a stale pending item stopped surfacing (F116 PR 1)
--
-- One row per fact or episode that a heartbeat layer, the learn_fact tool or the owner closed, so the self-initiated
-- check does not raise it as "Pending action" or "Unresolved commitment" again. subject_id is a heart.facts or a
-- heart.episodes id (subject_kind says which), with no foreign key because it points at one of two tables and memory
-- is soft-deleted. evidence holds the referents and their states, prior_actionable and note_fact_id. review_by is when
-- the close expires. A suppression note always has one (60 days by default), a close on a terminal referent may not.
-- Reopening sets reopened_at. One resolution per item and agent. Additive: heart.facts is not changed.
--
-- Writer and reader: the app only. No grant, and scripts/ops/db_roles.sql is not touched. Rollback while no layer
-- writes it is DROP TABLE IF EXISTS heart.pending_resolutions and deleting version 119 from schema_migrations.

CREATE TABLE IF NOT EXISTS heart.pending_resolutions (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id     TEXT        NOT NULL,
    subject_kind TEXT        NOT NULL CHECK (subject_kind IN ('fact', 'episode')),
    subject_id   UUID        NOT NULL,
    reason       TEXT        NOT NULL CHECK (reason IN ('referents_terminal', 'superseded', 'suppressed_by_note',
                                                        'age_cap', 'owner_closed')),
    evidence     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_by   TEXT        NOT NULL CHECK (created_by IN ('heartbeat', 'tool', 'owner')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    review_by    TIMESTAMPTZ,
    reopened_at  TIMESTAMPTZ,
    CONSTRAINT chk_pending_resolutions_note_expires CHECK (reason <> 'suppressed_by_note' OR review_by IS NOT NULL),
    CONSTRAINT uq_pending_resolutions_subject UNIQUE (agent_id, subject_kind, subject_id)
);
```
