"""F102: the one-off near-duplicate report (scripts/diag/procedure_dedupe_report.py).

Postgres only (pgvector cosines). Procedures are stored with dedupe off, so the duplicates
the report should find are in the table; each test rolls back.
"""

import math

import pytest
from sqlalchemy import select

from nous.heart import ProcedureInput
from nous.heart.procedures import ProcedureManager
from nous.storage.models import Procedure
from scripts.diag import procedure_dedupe_report as report

pytestmark = pytest.mark.postgres_only

AGENT = "f102-dedupe-report"


class _AxisEmbeddings:
    """A text containing a marker embeds at that marker's angle in the e0/e1 plane."""

    def __init__(self, angles: dict[str, float]) -> None:
        self.angles = angles

    async def embed(self, text: str) -> list[float]:
        vec = [0.0] * 1536
        for marker, angle in self.angles.items():
            if marker in text:
                vec[0], vec[1] = math.cos(angle), math.sin(angle)
                return vec
        vec[2] = 1.0
        return vec


async def _seed(db, session) -> dict[str, Procedure]:
    # A at 0; B at cos 0.90 to A; C at cos 0.90 to B but only ~0.62 to A; D unrelated; a skill near A.
    step = math.acos(0.90)
    embeddings = _AxisEmbeddings({"MARK_A": 0.0, "MARK_B": step, "MARK_C": 2 * step, "MARK_S": 0.05})
    manager = ProcedureManager(db, embeddings, AGENT, dedupe_enabled=False)
    stored = {}
    for key, tags in (("A", ["auto:episode_lesson"]), ("B", ["auto:episode_lesson"]),
                      ("C", ["auto:monitor_recovery"]), ("D", ["auto:episode_lesson"]),
                      ("S", ["skill", "local", "auto:x"])):
        detail = await manager.store(
            ProcedureInput(name=f"Procedure {key}", description=f"MARK_{key}" if key != "D" else "other", tags=tags),
            session=session,
        )
        stored[key] = await session.get(Procedure, detail.id)
    # B is the best: the survivor.
    stored["A"].success_count, stored["A"].failure_count = 3, 3
    stored["B"].success_count, stored["B"].failure_count = 9, 1
    stored["C"].success_count, stored["C"].failure_count = 1, 1
    await session.flush()
    return stored


async def test_dry_run_reports_the_cluster_and_writes_nothing(db, session):
    stored = await _seed(db, session)
    manager = ProcedureManager(db, None, AGENT)

    clusters, text = await report.run(session, manager, 0.85)

    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.survivor.id == stored["B"].id
    assert {m.id for m in cluster.duplicates} == {stored["A"].id, stored["C"].id}
    assert "Procedure B" in text and "Duplicates to retire: 2" in text
    assert "Procedure S" not in text  # a skill is never in the population, even with an auto: tag
    rows = (await session.execute(select(Procedure.active).where(Procedure.agent_id == AGENT))).scalars().all()
    assert all(rows)


async def test_a_member_linked_through_another_is_kept(db, session):
    stored = await _seed(db, session)
    # A becomes the survivor: C reaches the cluster only through B.
    stored["A"].success_count, stored["A"].failure_count = 50, 0
    await session.flush()

    clusters, _text = await report.run(session, ProcedureManager(db, None, AGENT), 0.85)

    cluster = clusters[0]
    assert cluster.survivor.id == stored["A"].id
    assert [m.id for m in cluster.duplicates] == [stored["B"].id]
    assert [m.id for m in cluster.kept] == [stored["C"].id]


async def test_apply_retires_the_duplicates_with_a_reason(db, session):
    stored = await _seed(db, session)

    await report.run(session, ProcedureManager(db, None, AGENT), 0.85, apply=True)

    for key in ("A", "C"):
        row = await session.get(Procedure, stored[key].id)
        await session.refresh(row)
        assert row.active is False
        assert row.runtime_metadata["retired"]["survivor"] == str(stored["B"].id)
    for key in ("D", "S"):
        row = await session.get(Procedure, stored[key].id)
        await session.refresh(row)
        assert row.active is True
    survivor = await session.get(Procedure, stored["B"].id)
    await session.refresh(survivor)
    assert survivor.active is True


async def test_the_report_counts_learned_procedures_it_could_not_compare(db, session):
    await _seed(db, session)
    unembedded = await ProcedureManager(db, None, AGENT, dedupe_enabled=False).store(
        ProcedureInput(name="No vector", tags=["auto:episode_lesson"]), session=session
    )
    assert (await session.get(Procedure, unembedded.id)).embedding is None

    _clusters, text = await report.run(session, ProcedureManager(db, None, AGENT), 0.85)

    assert "Not compared: 1 auto-learned procedure(s) without an embedding" in text


@pytest.mark.parametrize("argv", [["--threshold", "1.5"], ["--threshold", "0.7", "--apply"]])
async def test_the_cli_refuses_a_threshold_out_of_bounds_or_too_low_to_apply(argv):
    with pytest.raises(SystemExit) as exit_info:
        await report.main(argv)
    assert exit_info.value.code == 2
