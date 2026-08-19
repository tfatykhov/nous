"""F091 end-to-end check: trace -> RetrievalLogger -> Postgres -> dashboard query.

Exercises the REAL write path (nous/main.py's _write_retrieval_log SQL) and the
REAL read path (dashboard_queries.get_retrieval_data / get_retrieval_detail)
against the live nous DB, so a JSONB/param-binding mismatch surfaces here
rather than in prod.

Usage: uv run python -m scripts.diag.f091_e2e_check
"""

from __future__ import annotations

import asyncio
import json
import sys
from uuid import uuid4

from sqlalchemy import text

from nous.api.dashboard_queries import get_retrieval_data, get_retrieval_detail
from nous.config import Settings
from nous.observability.retrieval_logger import RetrievalLogger
from nous.observability.retrieval_trace import (
    BELOW_FLOOR,
    F071_EXCLUDED,
    RENDERED,
    SLICED_OFF,
)
from nous.storage.database import Database

AGENT = "f091-e2e-probe"


class _R:
    def __init__(self, rid, rtype):
        self.id, self.type = rid, rtype


def _build_trace(rl: RetrievalLogger):
    tr = rl.start(query="what did we decide about chunk penalties",
                  path="pipeline", session_id="sess-f091")
    ids = [uuid4() for _ in range(6)]

    tr.leg("heart_primary", attempted=True, n_returned=4, scores=[0.91, 0.44])
    tr.leg("exemplar", attempted=False, skip_reason="not classification-shaped")
    tr.leg("keyed", attempted=True, n_returned=1, n_deduped=2)
    tr.exclude_type("censor", "f080_coherent_ranking")

    for i, fid in enumerate(ids):
        tr.add(fid, "fact", "heart_primary", score=0.9 - i * 0.1, rank=i + 1,
               content=f"probe fact {i} " + "x" * 400)

    tr.mutate(ids[0], "fact", "adjacency_boost", 0.90, 0.97, reason="degree=4")
    tr.mutate(ids[1], "fact", "recency_resolver", 0.80, 0.24, reason="superseded")
    tr.drop(ids[3], "fact", SLICED_OFF, "heart_recall_limit")
    tr.drop(ids[4], "fact", BELOW_FLOOR, "exemplar_similarity_floor")
    tr.drop(ids[5], "fact", F071_EXCLUDED, "f071_cross_context_dedup")

    seed, nbr1, nbr2 = uuid4(), uuid4(), uuid4()
    for nbr, rel, w in ((nbr1, "evidence_for", 0.72), (nbr2, "related_to", 0.61)):
        tr.expansion(seed_id=seed, seed_type="fact", seed_score=0.9,
                     neighbor_id=nbr, neighbor_type="decision",
                     stage="stage2_heart_graph", hop=1,
                     edge_relation=rel, edge_weight=w,
                     extraction_method="inferred",
                     composed_score=0.9 * w, won_best_path=True)

    tr.finalize([_R(i, "fact") for i in ids[:3]], duration_ms=42.5)
    return tr, ids


async def main() -> int:
    settings = Settings()
    database = Database(settings)
    await database.connect()

    async def _write(payload: dict):
        async with database.session() as s:
            await s.execute(text(
                "INSERT INTO nous_system.retrieval_log "
                "(id, agent_id, session_id, turn_number, trace_id, path, query, "
                "duration_ms, legs, excluded_types, n_candidates, n_rendered, "
                "n_expansions, disposition_counts, candidates, expansions, truncated) "
                "VALUES (:id, :agent_id, :sid, :turn, :trace, :path, :query, "
                ":dur, :legs, :excl, :n_cand, :n_rend, :n_exp, :disp, "
                ":cands, :exps, :trunc)"
            ), {
                "id": payload["id"], "agent_id": AGENT,
                "sid": payload.get("session_id"), "turn": payload.get("turn_number"),
                "trace": payload.get("trace_id"), "path": payload.get("path"),
                "query": payload.get("query"), "dur": payload.get("duration_ms"),
                "legs": json.dumps(payload.get("legs", [])),
                "excl": json.dumps(payload.get("excluded_types", [])),
                "n_cand": payload.get("n_candidates", 0),
                "n_rend": payload.get("n_rendered", 0),
                "n_exp": payload.get("n_expansions", 0),
                "disp": json.dumps(payload.get("disposition_counts", {})),
                "cands": (json.dumps(payload["candidates"])
                          if payload.get("candidates") is not None else None),
                "exps": json.dumps(payload.get("expansions", [])),
                "trunc": payload.get("truncated", False),
            })
            await s.commit()

    failures: list[str] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        print(f"  {'PASS' if cond else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
        if not cond:
            failures.append(label)

    try:
        async with database.session() as s:
            await s.execute(
                text("DELETE FROM nous_system.retrieval_log WHERE agent_id = :a"),
                {"a": AGENT},
            )
            await s.commit()

        # sample_rate=1.0 so candidate capture is deterministic in this probe.
        rl = RetrievalLogger(db_writer=_write, candidate_sample_rate=1.0,
                             snippet_chars=200, agent_id=AGENT)
        tr, ids = _build_trace(rl)
        rl.commit(tr)

        # Drain the fire-and-forget write.
        for _ in range(50):
            if not rl._pending_tasks:
                break
            await asyncio.sleep(0.05)

        print("\nWRITE PATH")
        async with database.session() as s:
            row = (await s.execute(
                text("SELECT id, n_candidates, n_rendered, n_expansions, "
                     "disposition_counts, jsonb_array_length(candidates) AS nc "
                     "FROM nous_system.retrieval_log WHERE agent_id = :a"),
                {"a": AGENT},
            )).first()
        check("row persisted", row is not None)
        if row is None:
            return 1
        check("n_candidates == 6", row.n_candidates == 6, str(row.n_candidates))
        check("n_rendered == 3", row.n_rendered == 3, str(row.n_rendered))
        check("n_expansions == 2", row.n_expansions == 2, str(row.n_expansions))
        check("candidates JSONB round-tripped", row.nc == 6, str(row.nc))
        counts = row.disposition_counts or {}
        check("dispositions sum to n_candidates",
              sum(counts.values()) == row.n_candidates, str(counts))
        check("no unaccounted candidates", "unaccounted" not in counts, str(counts))

        print("\nREAD PATH — list")
        async with database.session() as s:
            data = await get_retrieval_data(s, AGENT, limit=10)
        check("one entry returned", data["count"] == 1, str(data["count"]))
        e = data["entries"][0]
        check("has_candidates true", e["has_candidates"] is True)
        check("excluded_types surfaced",
              [x["type"] for x in e["excluded_types"]] == ["censor"],
              str(e["excluded_types"]))
        check("leg rollup keeps skipped leg",
              "exemplar" in data["leg_totals"], str(list(data["leg_totals"])))
        check("leg rollup counts dedup",
              data["leg_totals"].get("keyed", {}).get("deduped") == 2,
              str(data["leg_totals"].get("keyed")))
        check("disposition totals match row",
              data["disposition_totals"] == counts, str(data["disposition_totals"]))

        print("\nREAD PATH — detail")
        async with database.session() as s:
            det = await get_retrieval_detail(s, AGENT, row.id)
        check("detail found", det is not None)
        groups = det["candidates_by_disposition"]
        check("grouped by disposition", groups is not None)
        check(f"{RENDERED} group has 3", len(groups.get(RENDERED, [])) == 3,
              str({k: len(v) for k, v in groups.items()}))
        check("drop stage preserved",
              groups[SLICED_OFF][0]["disposition_stage"] == "heart_recall_limit",
              groups[SLICED_OFF][0]["disposition_stage"])
        rendered = groups[RENDERED]
        check("rendered sorted by entry score desc",
              [c["entry_score"] for c in rendered] == sorted(
                  [c["entry_score"] for c in rendered], reverse=True),
              str([c["entry_score"] for c in rendered]))
        boosted = next(c for c in rendered if c["id"] == str(ids[0]))
        check("mutation recorded on boosted candidate",
              boosted["mutations"][0]["stage"] == "adjacency_boost",
              str(boosted["mutations"]))
        check("snippet truncated to 200",
              all(len(c["snippet"]) <= 200 for g in groups.values() for c in g))
        exp = det["expansions"]
        check("2 expansion edges", len(exp) == 2, str(len(exp)))
        check("edge relation preserved",
              {x["edge_relation"] for x in exp} == {"evidence_for", "related_to"},
              str([x["edge_relation"] for x in exp]))

        print("\nREAD PATH — unsampled retrieval")
        rl2 = RetrievalLogger(db_writer=_write, candidate_sample_rate=0.0,
                              agent_id=AGENT)
        tr2 = rl2.start(query="unsampled probe", path="context")
        tr2.leg("context_facts", attempted=True, n_returned=5)
        tr2.add(uuid4(), "fact", "context_facts", score=0.5)
        tr2.expansion(seed_id=uuid4(), neighbor_id=uuid4(), seed_type="fact",
                      neighbor_type="procedure", stage="context_proc_graph")
        tr2.finalize([], duration_ms=3.0)
        rl2.commit(tr2)
        for _ in range(50):
            if not rl2._pending_tasks:
                break
            await asyncio.sleep(0.05)

        async with database.session() as s:
            det2 = await get_retrieval_detail(s, AGENT, tr2.id)
        check("unsampled row persisted", det2 is not None)
        check("candidates is None (not empty) when unsampled",
              det2["candidates_by_disposition"] is None,
              str(det2["candidates_by_disposition"]))
        check("legs still recorded when unsampled", len(det2["legs"]) == 1)
        check("expansions still recorded when unsampled", det2["n_expansions"] == 1)

        async with database.session() as s:
            await s.execute(
                text("DELETE FROM nous_system.retrieval_log WHERE agent_id = :a"),
                {"a": AGENT},
            )
            await s.commit()

        print(f"\n{'ALL CHECKS PASSED' if not failures else 'FAILURES: ' + str(failures)}")
        return 1 if failures else 0
    finally:
        await database.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
