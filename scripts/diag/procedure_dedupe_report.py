"""F102 one-off: report near-duplicate clusters of auto-learned procedures.

DRY RUN BY DEFAULT: prints the clusters and writes nothing. With --apply, each cluster's
duplicates are retired into its survivor through ProcedureManager.retire with a reason
(active=false, runtime_metadata.retired) and archived as superseded by it (superseded_by,
archived_at, as a consolidation archives a row); nothing is hard deleted and nothing is merged.

The population and the cosine are the create-time dedupe's (nous/heart/procedures.py):
active auto-learned procedures (an ``auto:*`` tag), never a skill, a strategy card or a
job lesson, compared by their stored embeddings. Pairs at or above the threshold link
procedures into clusters. A cluster's survivor is its best by Laplace effectiveness, then
activations, then the oldest. Only members at or above the threshold to the survivor
itself are retired; a member linked in through another member is reported and kept.

Connection from the standard DB_* env vars via nous.config.Settings. Usage:
  python -m scripts.diag.procedure_dedupe_report [--threshold 0.85] [--agent nous-default] [--apply]
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.config import Settings
from nous.heart.procedures import LEARNED_PARAMS, LEARNED_WHERE, ProcedureManager
from nous.storage.database import Database

# The semantic dedupe's population (LEARNED_WHERE) among one agent's active rows.
_ACTIVE_LEARNED = f"agent_id = :agent_id AND active = true AND {LEARNED_WHERE}"
_LEARNED = f"{_ACTIVE_LEARNED} AND embedding IS NOT NULL"


# Below it the live catalog's pairs stop being clear duplicates (F102 spec, "Why 0.85").
_APPLY_FLOOR = 0.80


@dataclass
class Member:
    id: UUID
    name: str
    effectiveness: float | None
    activation_count: int
    created_at: datetime | None
    similarity: float | None = None  # cosine to the cluster's survivor


@dataclass
class Cluster:
    survivor: Member
    duplicates: list[Member] = field(default_factory=list)  # retired by --apply
    kept: list[Member] = field(default_factory=list)  # linked in, but below the threshold to the survivor


def _rank(member: Member) -> tuple[float, int, datetime, str]:
    # Best first: effectiveness (no outcomes yet ranks as the 0.5 prior), activations, oldest, id.
    effectiveness = member.effectiveness if member.effectiveness is not None else 0.5
    return (-effectiveness, -member.activation_count, member.created_at or datetime.max, str(member.id))


async def find_clusters(session: AsyncSession, agent_id: str, threshold: float) -> list[Cluster]:
    """This agent's near-duplicate clusters of auto-learned procedures at ``threshold``, largest first."""
    params = {**LEARNED_PARAMS, "agent_id": agent_id}
    members = {
        row.id: Member(
            id=row.id,
            name=row.name,
            effectiveness=(row.s + 1) / (row.s + row.f + 2) if row.s + row.f else None,
            activation_count=row.activation_count or 0,
            created_at=row.created_at,
        )
        for row in (await session.execute(text(f"""
            SELECT id, name, coalesce(success_count, 0) AS s, coalesce(failure_count, 0) AS f,
                   activation_count, created_at
            FROM heart.procedures WHERE {_LEARNED}
        """), params)).all()
    }
    pairs = {
        frozenset((row.a, row.b)): float(row.similarity)
        for row in (await session.execute(text(f"""
            WITH learned AS (SELECT id, embedding FROM heart.procedures WHERE {_LEARNED})
            SELECT x.id AS a, y.id AS b, 1 - (x.embedding <=> y.embedding) AS similarity
            FROM learned x JOIN learned y ON x.id < y.id
            WHERE 1 - (x.embedding <=> y.embedding) >= :threshold
        """), {**params, "threshold": threshold})).all()
    }

    parent = {pid: pid for pid in members}

    def root(pid: UUID) -> UUID:
        while parent[pid] != pid:
            parent[pid] = parent[parent[pid]]
            pid = parent[pid]
        return pid

    for pair in pairs:
        a, b = tuple(pair)
        if a not in parent or b not in parent:
            continue  # stored after the member read (main reads one snapshot; a caller's session may not)
        parent[root(a)] = root(b)
    components: dict[UUID, list[Member]] = {}
    for pid, member in members.items():
        components.setdefault(root(pid), []).append(member)

    clusters = []
    for component in components.values():
        if len(component) < 2:
            continue
        survivor, *others = sorted(component, key=_rank)
        cluster = Cluster(survivor=survivor)
        for member in others:
            member.similarity = pairs.get(frozenset((survivor.id, member.id)))
            (cluster.duplicates if member.similarity is not None else cluster.kept).append(member)
        clusters.append(cluster)
    clusters.sort(key=lambda c: (-(len(c.duplicates) + len(c.kept)), c.survivor.name))
    return clusters


def format_report(clusters: list[Cluster], threshold: float, unembedded: int = 0) -> str:
    """The clusters as plain text, one block per cluster."""
    def line(m: Member) -> str:
        eff = f"{m.effectiveness:.2f}" if m.effectiveness is not None else "n/a"
        sim = f"cos {m.similarity:.3f}  " if m.similarity is not None else ""
        return f"{sim}eff {eff}  act {m.activation_count:<5}  {m.id}  {m.name}"

    out = [f"Near-duplicate clusters of auto-learned procedures at cosine >= {threshold:.2f}: {len(clusters)}"]
    for cluster in clusters:
        out.append("")
        out.append(f"  survivor   {line(cluster.survivor)}")
        out.extend(f"  duplicate  {line(m)}" for m in cluster.duplicates)
        out.extend(f"  kept       {line(m)}  (below the threshold to the survivor)" for m in cluster.kept)
    out.append("")
    out.append(f"Duplicates to retire: {sum(len(c.duplicates) for c in clusters)}")
    if unembedded:
        out.append(f"Not compared: {unembedded} auto-learned procedure(s) without an embedding (run reembed_all)")
    return "\n".join(out)


async def run(
    session: AsyncSession, manager: ProcedureManager, threshold: float, *, apply: bool = False
) -> tuple[list[Cluster], str]:
    """Find the clusters of ``manager``'s agent; with ``apply``, retire each duplicate. The caller commits."""
    clusters = await find_clusters(session, manager.agent_id, threshold)
    unembedded = (await session.execute(
        text(f"SELECT count(*) FROM heart.procedures WHERE {_ACTIVE_LEARNED} AND embedding IS NULL"),
        {**LEARNED_PARAMS, "agent_id": manager.agent_id},
    )).scalar_one()
    if apply:
        for cluster in clusters:
            for member in cluster.duplicates:
                await manager.retire(member.id, session, reason={
                    "reason": "F102 near-duplicate",
                    "survivor": str(cluster.survivor.id),
                    "similarity": round(member.similarity or 0.0, 4),
                    "threshold": threshold,
                })
                await session.execute(
                    text("UPDATE heart.procedures SET superseded_by = :survivor, archived_at = now()"
                         " WHERE id = :id AND agent_id = :agent_id"),
                    {"survivor": cluster.survivor.id, "id": member.id, "agent_id": manager.agent_id},
                )
    return clusters, format_report(clusters, threshold, unembedded)


async def main(argv: list[str] | None = None) -> int:
    settings = Settings()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--threshold", type=float, default=settings.procedure_dedupe_threshold)
    parser.add_argument("--agent", default=settings.agent_id)
    parser.add_argument("--apply", action="store_true", help="retire the duplicates (default: report only)")
    parser.add_argument("--force", action="store_true", help=f"allow --apply below {_APPLY_FLOOR}")
    args = parser.parse_args(argv)
    if not 0.0 <= args.threshold <= 1.0:
        parser.error("--threshold must be between 0 and 1")
    if args.apply and args.threshold < _APPLY_FLOOR and not args.force:
        parser.error(f"--apply below {_APPLY_FLOOR} retires procedures in the ambiguous band; add --force to mean it")

    db = Database(settings)
    await db.connect()
    try:
        async with db.session() as session:
            # One snapshot for every read: the members, the pairs and the unembedded count agree.
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            if not args.apply:
                await session.execute(text("SET TRANSACTION READ ONLY"))
            manager = ProcedureManager(db, None, args.agent)
            _, report = await run(session, manager, args.threshold, apply=args.apply)
            print(report)
            if args.apply:
                await session.commit()
                print("Applied: duplicates retired.")
            else:
                print("Dry run: nothing written. Re-run with --apply to retire the duplicates.")
    finally:
        await db.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
