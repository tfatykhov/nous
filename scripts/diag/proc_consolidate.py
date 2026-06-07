"""Procedure dedup Phase 0 — one-time, operator-gated consolidation.

Collapses the known duplicate clusters (audit:
docs/reviews/procedure-subsystem-audit-2026-06-06.md §6) into a single canonical
procedure each, archiving siblings (active=false, archived_at, superseded_by=canonical),
folding their per-frame procedure_task_affinity counts into the canonical, and deleting
their incident brain.graph_edges. Every cluster is one transaction; the whole run is one
transaction. Idempotent: a second run is a no-op (siblings already superseded are skipped,
their affinity rows already deleted).

Connection comes from the standard DB_* env vars (DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/
DB_NAME) via nous.config.Settings — point them at a throwaway DB (5433) to validate before
prod. Agent id from PROC_CONSOLIDATE_AGENT (default nous-default).

Modes:
  --dry-run   (default) run everything in a transaction, print the diff, ROLLBACK.
  --commit    run and COMMIT.

SAFETY: refuses to touch any cluster whose siblings carry non-null censor_ids or
related_procedures (those outbound arrays would be silently lost — handle by hand).
"""
from __future__ import annotations

import argparse
import asyncio
import os

from sqlalchemy import text

from nous.config import Settings
from nous.storage.database import Database

AGENT = os.environ.get("PROC_CONSOLIDATE_AGENT", "nous-default")

# Canonical selection rationale is in the audit; bodies were reviewed via _proc_bodies.json.
CLUSTERS: list[dict] = [
    {
        "name": "email",
        # send_email: richest body (12 patterns / 5 notes), domain=communication,
        # the F078 guarded-tool path, clears the cold-start floor (act>=5).
        "canonical": "54fedf18-1299-41ae-8ad8-e7a795f4f70a",
        "siblings": [
            "47fbb706-7aff-4811-8136-8973f4894197",  # Send Email via Gmail SMTP (skill,inline)
            "2a1f583b-d495-4a0e-9131-30d27dfc30c5",  # Send Email via Gmail SMTP (skill,inline)
            "576e10f4-4a45-4e3b-a7aa-8efd962c588a",  # Send Email via Gmail SMTP (skill,local)
            "828da799-9890-4a52-b99a-87bed68731be",  # email (skill,local)
            "97cb7b96-41f2-400f-bef1-b6d1ae38f77f",  # validated-email-sending (skill,inline)
        ],
        # Fold validated-email-sending's unique capability (delivery validation/logging)
        # into the canonical so nothing is lost on archive.
        "enrich_notes": [
            "When email delivery must be audited: capture the SMTP response code, append the "
            "send to a persistent log file, and record the outcome as a fact (folded in from "
            "validated-email-sending on consolidation).",
        ],
    },
    {
        "name": "action_gate",
        # duplicate_action_gate_recovery is the superset (verify-state + direct-exec, 8 patterns).
        "canonical": "a1cefc07-80cb-45cd-84ff-497564eab187",
        "siblings": [
            "408d59db-3cb1-4eb3-be75-1cd7045dc1d4",  # duplicate-action-gate-acknowledgment
        ],
        "enrich_notes": [],
    },
    {
        "name": "compaction",
        # Conversation Compaction Management is the richer of the 0.943-cosine pair.
        "canonical": "f045d70f-332e-4f14-b667-74aff68f7553",
        "siblings": [
            "656693fb-1319-4e92-89ee-0668246a4e1c",  # Conversation Compaction Awareness
        ],
        "enrich_notes": [],
    },
]


async def _fetch_row(conn, pid: str) -> dict | None:
    r = await conn.execute(
        text(
            "SELECT id::text, name, active, superseded_by::text AS superseded_by, "
            "censor_ids::text AS censor_ids, related_procedures::text AS related_procedures "
            "FROM heart.procedures WHERE id = :id AND agent_id = :agent"
        ),
        {"id": pid, "agent": AGENT},
    )
    m = r.mappings().first()
    return dict(m) if m else None


async def _preflight(conn) -> list[str]:
    """Validate every cluster; return a list of human-readable problems (empty = OK)."""
    problems: list[str] = []
    for c in CLUSTERS:
        canon = await _fetch_row(conn, c["canonical"])
        if canon is None:
            problems.append(f"[{c['name']}] canonical {c['canonical']} not found for agent {AGENT}")
            continue
        if not canon["active"]:
            problems.append(f"[{c['name']}] canonical {canon['name']} is not active")
        if canon["superseded_by"] is not None:
            problems.append(f"[{c['name']}] canonical {canon['name']} is itself superseded")
        for sid in c["siblings"]:
            sib = await _fetch_row(conn, sid)
            if sib is None:
                problems.append(f"[{c['name']}] sibling {sid} not found")
                continue
            # B3: outbound arrays would be silently lost — refuse rather than drop.
            if sib["censor_ids"] not in (None, "", "{}"):
                problems.append(f"[{c['name']}] sibling {sib['name']} has censor_ids={sib['censor_ids']} (union by hand)")
            if sib["related_procedures"] not in (None, "", "{}"):
                problems.append(f"[{c['name']}] sibling {sib['name']} has related_procedures={sib['related_procedures']} (union by hand)")
    return problems


async def _consolidate_cluster(conn, c: dict, *, verbose: bool) -> None:
    canon = c["canonical"]
    sibs = c["siblings"]
    params = {"canon": canon, "sibs": sibs, "agent": AGENT}

    # 1. Fold sibling per-frame affinity into the canonical (sum). Gated on the sibling
    #    not yet being superseded so a re-run cannot double-count.
    await conn.execute(
        text(
            """
            INSERT INTO heart.procedure_task_affinity
                (procedure_id, frame_type, activation_count, success_count,
                 failure_count, last_activated_at, active, agent_id)
            SELECT :canon, s.frame_type,
                   SUM(s.activation_count), SUM(s.success_count), SUM(s.failure_count),
                   MAX(s.last_activated_at), bool_or(s.active), :agent
            FROM heart.procedure_task_affinity s
            JOIN heart.procedures p ON p.id = s.procedure_id AND p.superseded_by IS NULL
            WHERE s.agent_id = :agent AND s.procedure_id = ANY(CAST(:sibs AS uuid[]))
            GROUP BY s.frame_type
            ON CONFLICT (procedure_id, frame_type, agent_id) DO UPDATE SET
                activation_count = heart.procedure_task_affinity.activation_count + EXCLUDED.activation_count,
                success_count    = heart.procedure_task_affinity.success_count + EXCLUDED.success_count,
                failure_count    = heart.procedure_task_affinity.failure_count + EXCLUDED.failure_count,
                last_activated_at = GREATEST(heart.procedure_task_affinity.last_activated_at, EXCLUDED.last_activated_at),
                active = heart.procedure_task_affinity.active OR EXCLUDED.active
            """
        ),
        params,
    )

    # 2. Remove the now-redundant sibling affinity rows (counts preserved in canonical).
    await conn.execute(
        text(
            "DELETE FROM heart.procedure_task_affinity "
            "WHERE agent_id = :agent AND procedure_id = ANY(CAST(:sibs AS uuid[]))"
        ),
        params,
    )

    # 2b. Fold the siblings' procedure-level outcome counters into the canonical so the
    #     merged capability keeps its learned effectiveness/usage signal (F037 reads
    #     activation/success/failure on the row). Gated on superseded_by IS NULL so a
    #     re-run sums over zero rows (idempotent); the raw counts stay on the archived
    #     rows as an audit trail (harmless — inactive rows are never searched/scored).
    await conn.execute(
        text(
            """
            UPDATE heart.procedures c SET
                activation_count = coalesce(c.activation_count, 0) + agg.act,
                success_count    = coalesce(c.success_count, 0) + agg.succ,
                failure_count    = coalesce(c.failure_count, 0) + agg.fail,
                neutral_count    = coalesce(c.neutral_count, 0) + agg.neu,
                updated_at = now()
            FROM (
                SELECT coalesce(sum(activation_count), 0) AS act,
                       coalesce(sum(success_count), 0)    AS succ,
                       coalesce(sum(failure_count), 0)    AS fail,
                       coalesce(sum(neutral_count), 0)    AS neu
                FROM heart.procedures s
                WHERE s.agent_id = :agent
                  AND s.id = ANY(CAST(:sibs AS uuid[]))
                  AND s.superseded_by IS NULL
            ) agg
            WHERE c.id = :canon AND c.agent_id = :agent
            """
        ),
        params,
    )

    # 3. Optional canonical body enrichment (append unique notes not already present).
    for note in c.get("enrich_notes", []):
        await conn.execute(
            text(
                "UPDATE heart.procedures "
                "SET implementation_notes = array_append(coalesce(implementation_notes, ARRAY[]::text[]), :note), "
                "    updated_at = now() "
                "WHERE id = :canon AND agent_id = :agent "
                "AND NOT (:note = ANY(coalesce(implementation_notes, ARRAY[]::text[])))"
            ),
            {"canon": canon, "agent": AGENT, "note": note},
        )

    # 4. Delete incident graph edges referencing the siblings (no FK/CASCADE; blast radius ~0).
    await conn.execute(
        text(
            "DELETE FROM brain.graph_edges WHERE agent_id = :agent AND ("
            "(source_id = ANY(CAST(:sibs AS uuid[])) AND source_type = 'procedure') OR "
            "(target_id = ANY(CAST(:sibs AS uuid[])) AND target_type = 'procedure'))"
        ),
        params,
    )

    # 5. Archive the siblings (idempotent via the superseded_by IS NULL guard).
    await conn.execute(
        text(
            "UPDATE heart.procedures "
            "SET active = false, archived_at = now(), superseded_by = :canon "
            "WHERE agent_id = :agent AND id = ANY(CAST(:sibs AS uuid[])) AND superseded_by IS NULL"
        ),
        params,
    )


async def _report(conn) -> None:
    """Print before-state + planned actions for each cluster."""
    for c in CLUSTERS:
        canon = await _fetch_row(conn, c["canonical"])
        print(f"\n=== cluster: {c['name']} ===")
        print(f"  canonical : {canon['name'] if canon else '??'}  ({c['canonical']})")
        # affinity that will fold in
        r = await conn.execute(
            text(
                "SELECT frame_type, SUM(activation_count) a, SUM(success_count) s, SUM(failure_count) f "
                "FROM heart.procedure_task_affinity "
                "WHERE agent_id = :agent AND procedure_id = ANY(CAST(:sibs AS uuid[])) "
                "GROUP BY frame_type ORDER BY frame_type"
            ),
            {"agent": AGENT, "sibs": c["siblings"]},
        )
        aff = r.mappings().all()
        edge_r = await conn.execute(
            text(
                "SELECT count(*) FROM brain.graph_edges WHERE agent_id = :agent AND ("
                "(source_id = ANY(CAST(:sibs AS uuid[])) AND source_type='procedure') OR "
                "(target_id = ANY(CAST(:sibs AS uuid[])) AND target_type='procedure'))"
            ),
            {"agent": AGENT, "sibs": c["siblings"]},
        )
        edge_n = edge_r.scalar() or 0
        for sid in c["siblings"]:
            sib = await _fetch_row(conn, sid)
            print(f"  archive   : {sib['name'] if sib else '??'}  ({sid})")
        cnt = (
            await conn.execute(
                text(
                    "SELECT coalesce(sum(activation_count),0) a, coalesce(sum(success_count),0) s, "
                    "coalesce(sum(failure_count),0) f, coalesce(sum(neutral_count),0) n "
                    "FROM heart.procedures WHERE agent_id=:agent AND id = ANY(CAST(:sibs AS uuid[]))"
                ),
                {"agent": AGENT, "sibs": c["siblings"]},
            )
        ).mappings().first()
        print(f"  counters folding into canonical: act={int(cnt['a'])} succ={int(cnt['s'])} fail={int(cnt['f'])} neu={int(cnt['n'])}")
        print(f"  affinity folding in: {[(m['frame_type'], int(m['a']), int(m['s']), int(m['f'])) for m in aff]}")
        print(f"  incident edges to delete: {edge_n}")
        if c.get("enrich_notes"):
            print(f"  canonical enrich notes: {len(c['enrich_notes'])}")


async def _verify(conn) -> list[str]:
    """Post-consolidation invariants (run inside the same tx)."""
    problems: list[str] = []
    for c in CLUSTERS:
        canon = await _fetch_row(conn, c["canonical"])
        if not canon or not canon["active"] or canon["superseded_by"] is not None:
            problems.append(f"[{c['name']}] canonical not healthy post-merge")
        for sid in c["siblings"]:
            sib = await _fetch_row(conn, sid)
            if sib is None:
                problems.append(f"[{c['name']}] sibling {sid} vanished")
                continue
            if sib["active"]:
                problems.append(f"[{c['name']}] sibling {sib['name']} still active")
            if sib["superseded_by"] != c["canonical"]:
                problems.append(f"[{c['name']}] sibling {sib['name']} superseded_by != canonical")
            # affinity rows for the sibling must be gone
            r = await conn.execute(
                text("SELECT count(*) FROM heart.procedure_task_affinity WHERE procedure_id = :id"),
                {"id": sid},
            )
            if (r.scalar() or 0) != 0:
                problems.append(f"[{c['name']}] sibling {sib['name']} still has affinity rows")
            # incident edges must be gone
            r = await conn.execute(
                text(
                    "SELECT count(*) FROM brain.graph_edges WHERE "
                    "(source_id = :id AND source_type='procedure') OR (target_id = :id AND target_type='procedure')"
                ),
                {"id": sid},
            )
            if (r.scalar() or 0) != 0:
                problems.append(f"[{c['name']}] sibling {sib['name']} still has incident edges")
    return problems


def _lit(s: str) -> str:
    """Single-quote SQL literal with '' escaping."""
    return "'" + str(s).replace("'", "''") + "'"


def _uuid_array(ids: list[str]) -> str:
    return "ARRAY[" + ",".join(_lit(i) for i in ids) + "]::uuid[]"


def emit_migration_sql() -> str:
    """Emit the consolidation as a static, splitter-safe SQL data migration.

    Same operations as _consolidate_cluster (single source of truth = CLUSTERS), with
    inline literals so it can run as a numbered migration on a single deploy. Scoped to
    PROC_CONSOLIDATE_AGENT and the known ids — a no-op on any DB without those rows
    (fresh installs / CI). Idempotent via the superseded_by IS NULL guards. No DO $$
    blocks / no BEGIN-COMMIT (auto-migrator splitter constraints).
    """
    from backfill_proc_descriptions import BACKFILL, _MARKERS  # local sibling import

    a = _lit(AGENT)
    out: list[str] = []
    out.append("-- Procedure dedup Phase 0 — one-time consolidation (generated by")
    out.append("-- scripts/diag/proc_consolidate.py --emit-sql; audit §6). Collapses the duplicate")
    out.append("-- clusters into one canonical each (archive-not-delete: active=false + superseded_by),")
    out.append("-- folding affinity + outcome counters and deleting incident graph edges. The loop-close")
    out.append("-- code (bootstrap/reactivate redirect on superseded_by, shipped in #488) is deployed")
    out.append("-- before this runs (run_migrations precedes bootstrap), so archived skill rows are not")
    out.append("-- re-imported at startup. No-op on DBs lacking these ids. Splitter-safe: no DO blocks,")
    out.append("-- no BEGIN/COMMIT (run_migrations wraps the file in one transaction).")
    out.append("")
    for c in CLUSTERS:
        canon = _lit(c["canonical"])
        sibs = _uuid_array(c["siblings"])
        out.append(f"-- ===== cluster: {c['name']} =====")
        out.append(
            "INSERT INTO heart.procedure_task_affinity (procedure_id, frame_type, activation_count, "
            "success_count, failure_count, last_activated_at, active, agent_id)\n"
            f"SELECT {canon}, s.frame_type, SUM(s.activation_count), SUM(s.success_count), "
            f"SUM(s.failure_count), MAX(s.last_activated_at), bool_or(s.active), {a}\n"
            "FROM heart.procedure_task_affinity s "
            "JOIN heart.procedures p ON p.id = s.procedure_id AND p.superseded_by IS NULL\n"
            f"WHERE s.agent_id = {a} AND s.procedure_id = ANY({sibs})\n"
            "GROUP BY s.frame_type\n"
            "ON CONFLICT (procedure_id, frame_type, agent_id) DO UPDATE SET "
            "activation_count = heart.procedure_task_affinity.activation_count + EXCLUDED.activation_count, "
            "success_count = heart.procedure_task_affinity.success_count + EXCLUDED.success_count, "
            "failure_count = heart.procedure_task_affinity.failure_count + EXCLUDED.failure_count, "
            "last_activated_at = GREATEST(heart.procedure_task_affinity.last_activated_at, EXCLUDED.last_activated_at), "
            "active = heart.procedure_task_affinity.active OR EXCLUDED.active;"
        )
        out.append(f"DELETE FROM heart.procedure_task_affinity WHERE agent_id = {a} AND procedure_id = ANY({sibs});")
        out.append(
            "UPDATE heart.procedures c SET activation_count = coalesce(c.activation_count,0)+agg.act, "
            "success_count = coalesce(c.success_count,0)+agg.succ, failure_count = coalesce(c.failure_count,0)+agg.fail, "
            "neutral_count = coalesce(c.neutral_count,0)+agg.neu, updated_at = now() "
            "FROM (SELECT coalesce(sum(activation_count),0) act, coalesce(sum(success_count),0) succ, "
            "coalesce(sum(failure_count),0) fail, coalesce(sum(neutral_count),0) neu FROM heart.procedures s "
            f"WHERE s.agent_id = {a} AND s.id = ANY({sibs}) AND s.superseded_by IS NULL) agg "
            f"WHERE c.id = {canon} AND c.agent_id = {a};"
        )
        for note in c.get("enrich_notes", []):
            nl = _lit(note)
            out.append(
                "UPDATE heart.procedures SET implementation_notes = array_append(coalesce(implementation_notes, "
                f"ARRAY[]::text[]), {nl}), updated_at = now() WHERE id = {canon} AND agent_id = {a} "
                f"AND NOT ({nl} = ANY(coalesce(implementation_notes, ARRAY[]::text[])));"
            )
        out.append(
            f"DELETE FROM brain.graph_edges WHERE agent_id = {a} AND ((source_id = ANY({sibs}) AND source_type = 'procedure') "
            f"OR (target_id = ANY({sibs}) AND target_type = 'procedure'));"
        )
        out.append(
            f"UPDATE heart.procedures SET active = false, archived_at = now(), superseded_by = {canon} "
            f"WHERE agent_id = {a} AND id = ANY({sibs}) AND superseded_by IS NULL;"
        )
        out.append("")
    markers = "ARRAY[" + ",".join(_lit(m) for m in _MARKERS) + "]"
    out.append("-- ===== backfill block-scalar-broken descriptions =====")
    for pid, (name, desc) in BACKFILL.items():
        out.append(
            f"UPDATE heart.procedures SET description = {_lit(desc)}, updated_at = now() "
            f"WHERE id = {_lit(pid)} AND agent_id = {a} AND trim(description) = ANY({markers});"
        )
    out.append("")
    return "\n".join(out)


async def main() -> int:
    ap = argparse.ArgumentParser(description="Procedure dedup Phase 0 consolidation")
    ap.add_argument("--commit", action="store_true", help="COMMIT (default is dry-run + rollback)")
    ap.add_argument("--emit-sql", metavar="PATH", help="write the consolidation as a static SQL migration and exit")
    args = ap.parse_args()

    if args.emit_sql:
        with open(args.emit_sql, "w", encoding="utf-8", newline="\n") as f:
            f.write(emit_migration_sql())
        print(f"wrote {args.emit_sql}")
        return 0
    mode = "COMMIT" if args.commit else "DRY-RUN"

    settings = Settings()
    db = Database(settings)
    await db.connect()
    rc = 0
    try:
        # Explicit transaction so dry-run can ROLLBACK and --commit can COMMIT
        # without fighting begin()'s implicit auto-commit-on-exit.
        async with db.engine.connect() as conn:
            trans = await conn.begin()
            print(f"[{mode}] agent={AGENT} db={settings.db_name}@{settings.db_host}:{settings.db_port}")
            problems = await _preflight(conn)
            if problems:
                print("PREFLIGHT FAILED:")
                for p in problems:
                    print("  -", p)
                await trans.rollback()
                return 2
            await _report(conn)
            for c in CLUSTERS:
                await _consolidate_cluster(conn, c, verbose=False)
            verify_problems = await _verify(conn)
            if verify_problems:
                print("\nVERIFY FAILED (rolling back):")
                for p in verify_problems:
                    print("  -", p)
                await trans.rollback()
                return 3
            n_arch = sum(len(c["siblings"]) for c in CLUSTERS)
            print(f"\n[{mode}] verified OK — {n_arch} siblings archived across {len(CLUSTERS)} clusters")
            if args.commit:
                await trans.commit()
                print("[COMMIT] changes persisted")
            else:
                await trans.rollback()
                print("[DRY-RUN] transaction rolled back — no changes persisted")
    finally:
        await db.disconnect()
    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
