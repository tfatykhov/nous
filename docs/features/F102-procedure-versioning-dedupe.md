# F102: Procedure Versioning + Create-Time Dedupe

**Status:** in review. Versioning is always on; dedupe is behind `NOUS_PROCEDURE_DEDUPE_ENABLED` (default `true`).
**Code:** `nous/heart/procedures.py` (ProcedureManager), `sql/migrations/092_procedure_versions.sql`,
`scripts/diag/procedure_dedupe_report.py` · **Tests:** `tests/test_procedure_versioning.py`,
`tests/test_procedure_dedupe_report.py`

**Credit:** the design is adopted from paperclipai/paperclip's runner-created skills
(`doc/runner-created-skills.md`): an `idempotencyKey` on create, a name lock, `expectedVersionId`
optimistic concurrency on update, and version history with restore.

## Problem

`ProcedureManager` (store / update_body / append_notes / retire / reactivate) kept no history and did not
dedupe at create time. Edits overwrote the body, with no record and no protection against a concurrent
edit. The live catalog (read-only, 2026-10-24) held near-duplicate learned procedures. There were five
about not overclaiming email or send delivery, three about PR merge gates, two about idle or skipped
fix-stage nodes, and four about falling back to the shell after a tool failure. The learning and
consolidation paths had created them.

## Version history

- `heart.procedures.version` (INT, default 1) is the body's version.
- `heart.procedure_versions` holds **every** version, the current one included: name, domain, description,
  goals, core_patterns, core_tools, core_concepts, implementation_notes, tags, kind, runtime_metadata,
  `change_source`, `reason`, `created_at`. It is unique on (procedure_id, version), and its FK to
  `heart.procedures` uses ON DELETE CASCADE, as `procedure_task_affinity` does. The app never hard deletes.
- Migration 092 backfills version 1 (`change_source` `backfill`) for every existing row.
- Writers, each under a row lock (`FOR UPDATE`, fresh read):

  | Writer | change_source | Note |
  |---|---|---|
  | `store()` insert | `create` | version 1 |
  | `update_body()` | `update_body` | learn_skill's in-place refresh; the weak review's revise (reason `weak review: revise: ...`). The body the row already holds writes no version. An input that leaves runtime_metadata unset keeps the row's |
  | `append_notes()` | `append_notes` | only when notes are appended |
  | dedupe merge in `store()` | `dedupe_merge` | reason names the rule and the incoming name |
  | `restore()` | `restore` | reason `restored version N` |

- **Optimistic concurrency.** `update_body`, `append_notes` and `restore` take `expected_version`. On a
  mismatch they raise `ProcedureConflictError` (a `ValueError`) and write nothing. It is optional: the
  existing callers don't pass it, and `append_notes` already re-makes its choice under the lock.
- **Restore.** `restore(procedure_id, version)` writes the snapshot's name, domain, description and list
  fields back as a new version, then re-embeds. Kind, runtime_metadata (retirement, job records,
  `skip_until_scored`), active and the stats stay as they are. A snapshot name that another active row
  holds now raises `ProcedureConflictError`. A strategy card gives the name up, as in `_reactivate`.
  `list_versions(procedure_id)` returns them oldest first. Both are on `Heart`
  (`restore_procedure_version`, `list_procedure_versions`). No REST or tool surface: REST has only the
  list endpoint, so there is no obvious per-procedure place for one.
- **Not versioned:** retire, reactivate and activation and outcome counts don't change the body. A strategy
  card renamed by `_take_name_from_card` is bookkeeping, not an edit, so a card's latest snapshot can hold
  its old name. The diag scripts' raw SQL
  (`scripts/diag/proc_consolidate.py`, `backfill_proc_descriptions.py`) writes bodies without a
  version; they are one-off operator tools.

## Create-time dedupe in `store()`

In order:

1. **Idempotency key** (always on). `ProcedureInput.idempotency_key` is kept on the version the request
   wrote (the v1 of a created row, or the version a merge wrote), with `request_fingerprint`, the sha256
   of the request without its key (keys sorted, so a dict built in another order is the same request).
   The same key and request returns that procedure, even one retired since (logged at WARNING). The same key with a
   different request raises `ProcedureConflictError`. The key is unique per agent (partial unique index).
Steps 2 and 3 apply only to an active **auto-learned** input (an `auto:*` tag; never a skill, a strategy
card or a job lesson), and only merge into an active auto-learned procedure (`LEARNED_WHERE`).

2. **Name lock** (flag on). The target is the procedure whose normalized name matches (casefolded, `-`/`_`
   as spaces, whitespace collapsed), if the semantic probe agrees: its cosine to the input is at or above
   the threshold (computed for that row directly). Only without a vector to compare (an embed failure,
   SQLite, a row stored without one) does the name alone merge. A same-named row that is not alike is
   logged and left; the input goes on to step 3, and if it is inserted under a name an active row
   holds, it takes the first free `<name> (2)`, `(3)`, ... instead of failing on the unique name index
   (a hand-written input still fails there, as before F102). The learner
   names a lesson the model left unnamed after its cue (`Unnamed procedure: <cue>`), so unnamed lessons
   don't share a name.
3. **Semantic near-duplicate** (flag on, Postgres). The input is probed through `_nearest_summaries` with
   the embedding `store()` already computed. The nearest procedure at or above
   `NOUS_PROCEDURE_DEDUPE_THRESHOLD` (0.85) is merged into. Without an embedding the check is skipped,
   with a WARNING.

**Merge.** The incoming goals, core_patterns, core_tools, core_concepts and tags are unioned into the
existing row (existing order first, new items appended). Of the incoming notes only `evidence:` lines
whose input item is not cited yet are appended, up to 10 evidence lines, as the learner's covered path
appends them (`new_evidence_notes`); the incoming `lesson:` line goes to the version's reason. A row
without a description takes the incoming one. The row's name, domain, kind, runtime_metadata and stats
stay. The new version's reason records the rule (with the cosine) and the incoming name, description and
lesson. Each merge is logged at INFO and writes a `procedure_merged` event. A merge that adds nothing
writes nothing, unless it carries an idempotency key to keep. A target that, under the row lock, is no
longer an active auto-learned how-to (retired, or made a skill by `learn_skill`) is not merged into; the
input is inserted instead. `Heart.store_procedure` emits `procedure_stored` from the returned row, so
after a merge the graph links the surviving row's own text.

Creates that dedupe or carry a key are serialized per agent with `pg_advisory_xact_lock`, so two learners
storing the same lesson at once can't both miss each other's row. No embedding call is made while the
lock is held: the input is embedded before the lock is taken, and so is the merged body, against the
target the probe finds then. The merge uses that vector only if the row, locked, is still the same row
at the same version; otherwise it keeps the row's old vector (it matched a moment ago). Other creates
take no lock.

The learner counts a store that came back as a merge (version above 1) as covered, not created. A merge
that added nothing to a row still at version 1 cannot be told apart from a create that way, and is
counted as created.

**Why only auto-learned procedures.** `learn_skill` and the bootstrap look a skill up by name and
*replace* its body (`update_body`), so a union would keep stale patterns. Distinct hand-authored skills
also sit close together: `evolution-gate` and `algorithm-evolution` are at cosine 0.907. A hand-written
input (MCP `teach` stores only a name and a description) would lose its description in a merge. And a
hand-written target that took an `auto:*` tag would turn into a learned row: the learner would append
evidence to it, and the report could retire it. So these are left out by population, not by threshold.

**Why 0.85.** It is the procedure learner's `_COVERED_COSINE`, already trusted in production to call a
lesson covered without an LLM check. On the live catalog every auto-learned pair at or above 0.85 is a
true duplicate (0.854 to 0.896). The first ambiguous auto pair is at 0.819, and some true duplicates go
down to 0.806. Merging at 0.82 would catch those but enters the ambiguous band, so the store-time merge
stays a conservative backstop. The learner's own probe and LLM check still handle the 0.5 to 0.85 band,
and the report script below takes `--threshold` to look lower.

With the flag off, `store()` is the legacy insert, apart from the version row and key replay.

## One-off report: `scripts/diag/procedure_dedupe_report.py`

It is a dry run by default (read-only transaction, nothing written). Every read is one REPEATABLE READ
snapshot. `--threshold` must be within [0, 1],
and `--apply` below 0.80 needs `--force`. The report counts the learned procedures it could not
compare because they have no embedding. It uses the same population and cosine
as the semantic merge. Pairs at or above the threshold link procedures into clusters. The survivor is the
best by Laplace effectiveness, then activations, then the oldest. Only members at or above the threshold
*to the survivor* are listed as duplicates; a member linked in through another member is reported and
kept. `--apply` retires each duplicate through `ProcedureManager.retire` with a reason
(`runtime_metadata.retired`: survivor, similarity, threshold) and archives it as superseded by the
survivor (`superseded_by`, `archived_at`), as a consolidation does. Nothing is hard deleted or merged.

```
python -m scripts.diag.procedure_dedupe_report [--threshold 0.85] [--agent nous-default] [--apply]
```

On a copy of the live catalog's 110 active how-to procedures (2026-10-24), 0.85 gives 4 clusters with 5
duplicates (merge gate, fix-stage, delivery reporting, shell fallback), and 0.80 gives 5 clusters with 11.

## Migration notes

- 092 (091 was taken by F101, written at the same time; the number may need a rebase). Splitter-safe
  plain statements; apply through the app's migrator. A re-run is a no-op (`IF NOT EXISTS`, and the
  backfill skips rows that already have their version).
- Rollback: `DROP TABLE heart.procedure_versions; ALTER TABLE heart.procedures DROP COLUMN version;`
  (code from before F102 never reads either).
