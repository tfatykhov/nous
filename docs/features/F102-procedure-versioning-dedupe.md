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
  | `update_body()` | `update_body` | learn_skill's in-place refresh |
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
  card renamed by `_take_name_from_card` is bookkeeping, not an edit. The diag scripts' raw SQL
  (`scripts/diag/proc_consolidate.py`, `backfill_proc_descriptions.py`) writes bodies without a
  version; they are one-off operator tools.

## Create-time dedupe in `store()`

In order:

1. **Idempotency key** (always on). `ProcedureInput.idempotency_key` is kept on the version the request
   wrote (the v1 of a created row, or the version a merge wrote), with `request_fingerprint`, the sha256
   of the request without its key. The same key and request returns that procedure. The same key with a
   different request raises `ProcedureConflictError`. The key is unique per agent (partial unique index).
2. **Name lock** (flag on). An active how-to input that is not a skill, compared with active how-to
   procedures that are not skills. Strategy cards and job lessons are never compared. A same-named
   procedure is the one whose normalized name (casefolded, `-`/`_` as spaces, whitespace collapsed)
   matches. A hit is merged.
3. **Semantic near-duplicate** (flag on, Postgres). An auto-learned input (`auto:*` tag) is probed with
   the embedding `store()` already computed, through `_nearest_summaries`, against active auto-learned
   procedures. The nearest at or above `NOUS_PROCEDURE_DEDUPE_THRESHOLD` (0.85) is merged into.

**Merge.** The incoming list fields are unioned into the existing row (existing order first, new items
appended), and the row is re-embedded. Its name, description, domain, kind, runtime_metadata and stats
stay. The new version's reason records the rule, the cosine and the incoming name, and every merge is
logged at INFO.

Creates that dedupe or carry a key are serialized per agent with `pg_advisory_xact_lock`, so two learners
storing the same lesson at once can't both miss each other's row. Strategy-card and job-lesson creates
take no lock.

**Why skills are left out.** `learn_skill` and the bootstrap look a skill up by name and *replace* its body
(`update_body`). A union would keep stale patterns. Distinct hand-authored skills also sit close together:
`evolution-gate` and `algorithm-evolution` are at cosine 0.907. So skills are left out by population, not by
threshold.

**Why 0.85.** It is the procedure learner's `_COVERED_COSINE`, already trusted in production to call a
lesson covered without an LLM check. On the live catalog every auto-learned pair at or above 0.85 is a
true duplicate (0.854 to 0.896). The first ambiguous auto pair is at 0.819, and some true duplicates go
down to 0.806. Merging at 0.82 would catch those but enters the ambiguous band, so the store-time merge
stays a conservative backstop. The learner's own probe and LLM check still handle the 0.5 to 0.85 band,
and the report script below takes `--threshold` to look lower.

With the flag off, `store()` is the legacy insert, apart from the version row and key replay.

## One-off report: `scripts/diag/procedure_dedupe_report.py`

It is a dry run by default (read-only transaction, nothing written). It uses the same population and cosine
as the semantic merge. Pairs at or above the threshold link procedures into clusters. The survivor is the
best by Laplace effectiveness, then activations, then the oldest. Only members at or above the threshold
*to the survivor* are listed as duplicates; a member linked in through another member is reported and
kept. `--apply` retires each duplicate through `ProcedureManager.retire` with a reason
(`runtime_metadata.retired`: survivor, similarity, threshold). Nothing is hard deleted or merged.

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
