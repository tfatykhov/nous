"""E-16/E-17: the read-only diversity-loss report (scripts/diag/diversity_loss.py).

The replayed modifiers and the diversity rule are pinned to the live ContextEngine
methods, so a change there fails here instead of silently skewing the report. The SQL
loader runs on Postgres only.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from nous.cognitive.context import ContextEngine
from scripts.diag import diversity_loss as dl

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _engine(**settings) -> ContextEngine:
    engine = ContextEngine.__new__(ContextEngine)
    defaults = dict(
        staleness_penalty_enabled=True,
        staleness_half_life_days=20,
        relevance_floor_enabled=True,
        relevance_min_results={},
        relevance_max_results={},
        relevance_drop_ratio=0.6,
    )
    engine._settings = SimpleNamespace(**{**defaults, **settings})
    return engine


def _cand(cid, score, rank, disposition="rendered", stage="final", mutations=None):
    return dl.Candidate(cid, score, rank, mutations or [], disposition, stage)


# -- parity with the live code ------------------------------------------------


@pytest.mark.parametrize("age_days", [0.5, 10, 45, 90])
def test_episode_replay_matches_live_recency(age_days):
    started = datetime.now(UTC) - timedelta(days=age_days)
    live = ContextEngine._apply_episode_recency(
        _engine(), [SimpleNamespace(id="e", score=0.8, started_at=started)]
    )[0].score
    replayed = dl.replay_score(0.8, "episode", datetime.now(UTC), dl.ItemMeta("unknown", started), 20)
    assert replayed == pytest.approx(live, rel=1e-4)


@pytest.mark.parametrize(
    ("age_days", "category"),
    [(0, "process"), (5, "process"), (40, "tooling"), (400, "process"), (40, "rule")],
)
def test_decision_replay_matches_live_staleness(age_days, category):
    created = datetime.now(UTC) - timedelta(days=age_days, hours=1)
    item = SimpleNamespace(id="d", score=0.9, created_at=created, category=category)
    live = _engine()._apply_staleness_penalty([item])[0].score
    meta = dl.ItemMeta(category, created, category=category)
    assert dl.replay_score(0.9, "decision", datetime.now(UTC), meta, 20) == pytest.approx(live, rel=1e-6)


def test_staleness_off_replays_nothing():
    meta = dl.ItemMeta("process", NOW - timedelta(days=100), category="process")
    assert dl.replay_score(0.9, "decision", NOW, meta, None) == 0.9


def test_simulated_rule_matches_live_enforce_diversity():
    raws = [["a"], [], None, ["A", "b"], [], ["c"], [], ["a"], []]
    live_items = [SimpleNamespace(id=str(i), tags=raw) for i, raw in enumerate(raws)]
    kept = {x.id for x in _engine()._enforce_diversity(live_items, "tags", max_per_subject=2)}
    scored = [
        dl.Scored(_cand(str(i), 1.0, i), 1.0, dl.ItemMeta(dl.diversity_bucket(raw)))
        for i, raw in enumerate(raws)
    ]
    assert dl.simulate_drops(scored, dl._bucket, 2) == {str(i) for i in range(len(raws))} - kept


def test_traced_score_uses_only_pre_diversity_mutations():
    cand = _cand("e", 0.8, 1, mutations=[
        {"stage": "episode_recency", "score_before": 0.8, "score_after": 0.6},
        {"stage": "usage_boost", "score_before": 0.6, "score_after": 0.9},
    ])
    assert dl.traced_score(cand, "episode") == 0.6
    assert dl.traced_score(_cand("e", None, 1), "episode") == 0.0


# -- the analysis -------------------------------------------------------------


def _episode_build(traced: bool) -> tuple[dl.Build, dict]:
    # Four tagless chat episodes and one tagged one. The traced build records recency
    # mutations; the replayed one gets the same scores from the item ages.
    # e2 enters first but is 60 days old: recency halves it, so the sort puts it last and
    # entry order alone would not reproduce the trace's drops.
    ages = {"e1": 1, "e2": 60, "e3": 2, "e4": 3, "t1": 1}
    entry = {"e1": 0.9, "e2": 0.5, "e3": 0.85, "e4": 0.5, "t1": 0.3}
    meta = {
        cid: dl.ItemMeta("subtask" if cid == "t1" else "unknown", NOW - timedelta(days=age),
                         session_id=f"s-{cid}")
        for cid, age in ages.items()
    }
    disp = {"e1": ("rendered", "final"), "e3": ("rendered", "final"), "t1": ("rendered", "final"),
            "e2": ("filter_dropped", "diversity"), "e4": ("filter_dropped", "diversity")}
    cands = []
    for rank, cid in enumerate(["e2", "e1", "e3", "e4", "t1"], start=1):
        muts = []
        if traced:
            after = dl.replay_score(entry[cid], "episode", NOW, meta[cid], None)
            muts = [{"stage": "episode_recency", "score_before": entry[cid], "score_after": after}]
        cands.append(_cand(cid, entry[cid], rank, *disp[cid], mutations=muts))
    # A system episode dropped before diversity is not part of the population.
    cands.append(_cand("sys", 0.99, 6, "filter_dropped", "system_episode"))
    return dl.Build("b", NOW, traced, {"episode": cands}), meta


@pytest.mark.parametrize("traced", [True, False])
def test_tagless_episodes_hit_the_unknown_cap(traced):
    build, meta = _episode_build(traced)
    t = dl.analyse([build], meta, None, _engine())[("episode", "traced" if traced else "replayed")]

    assert t.builds == 1 and t.candidates == 5
    assert t.rule_reproduced == 1  # e1 and e3 keep the two 'unknown' slots
    assert t.drops == 2 and t.drop_buckets["unknown"] == 2
    assert t.tagless == 4
    assert t.rendered_hist[3] == 1
    # e4 (0.5) outscores the rendered tagged t1 (0.3): a cross-bucket inversion.
    assert t.builds_inverted == 1 and t.drops_inverted == 1
    assert (t.drops_inverted_same_bucket, t.drops_inverted_cross_bucket) == (0, 1)
    # Every episode has its own session, so a session key keeps both drops.
    assert t.drops_kept_by_session_key == 2
    if traced:
        assert (t.replay_compared, t.replay_off) == (5, 0)


def test_unsorted_decisions_show_a_same_bucket_inversion():
    # Older code kept entry order: d4 (0.9, rank 4) lost the third 'process' slot to d3 (0.4).
    meta = {c: dl.ItemMeta("process", NOW, category="process") for c in ("d1", "d2", "d3", "d4")}
    cands = [
        _cand("d1", 0.8, 1), _cand("d2", 0.7, 2), _cand("d3", 0.4, 3),
        _cand("d4", 0.9, 4, "filter_dropped", "diversity"),
    ]
    t = dl.analyse([dl.Build("b", NOW, False, {"decision": cands})], meta, 20)[("decision", "replayed")]
    assert t.rule_reproduced == 1
    assert (t.drops_inverted_same_bucket, t.drops_inverted_cross_bucket) == (1, 0)
    assert t.drop_buckets == {"process": 1}


def test_no_cap_estimate_uses_the_live_relevance_filter():
    meta = {c: dl.ItemMeta("unknown", NOW) for c in ("e1", "e2", "e3", "e4")}
    cands = [
        _cand("e1", 0.9, 1), _cand("e2", 0.8, 2),
        _cand("e3", 0.7, 3, "filter_dropped", "diversity"),
        _cand("e4", 0.1, 4, "filter_dropped", "diversity"),  # under 0.6 x 0.7: the gap cut
    ]
    t = dl.analyse([dl.Build("b", NOW, True, {"episode": cands})], meta, None, _engine())[("episode", "traced")]
    assert t.drops_pass_filter == 1 and t.builds_drop_passes_filter == 1
    assert t.uncapped_hist[3] == 1
    assert "drops the relevance filter would keep with no cap (estimate): 1" in dl.render({("episode", "traced"): t})


# -- the loader (Postgres) ----------------------------------------------------


@pytest.mark.postgres_only
async def test_loader_reads_context_traces_and_metadata(session):
    agent = f"div-loss-{uuid.uuid4().hex[:8]}"
    ep = uuid.uuid4()
    await session.execute(text(
        "INSERT INTO heart.episodes (id, agent_id, session_id, summary, started_at, tags) "
        "VALUES (:i, :a, 's1', 'x', :t, NULL)"
    ), {"i": ep, "a": agent, "t": NOW})
    legs = [{"name": "context_episodes", "attempted": True, "query": "q"}]
    cands = [
        {"id": str(ep), "type": "episode", "entry_leg": "context_episodes", "entry_score": 0.7,
         "entry_rank": 1, "mutations": [], "disposition": "filter_dropped", "disposition_stage": "diversity"},
        {"id": str(uuid.uuid4()), "type": "fact", "entry_leg": "context_facts", "entry_score": 0.5,
         "entry_rank": 1, "mutations": [], "disposition": "rendered", "disposition_stage": "final"},
    ]
    for rid, path in (("r-ctx", "context"), ("r-pipe", "pipeline")):
        await session.execute(text(
            "INSERT INTO nous_system.retrieval_log (id, agent_id, path, legs, candidates) "
            "VALUES (:id, :a, :p, CAST(:legs AS jsonb), CAST(:c AS jsonb))"
        ), {"id": f"{rid}-{agent}", "a": agent, "p": path, "legs": json.dumps(legs), "c": json.dumps(cands)})

    builds = await dl.load_builds(session, agent, 0)
    assert len(builds) == 1 and builds[0].traced
    (cand,) = builds[0].by_type["episode"]
    assert (cand.id, cand.entry_score, cand.stage) == (str(ep), 0.7, "diversity")
    assert "fact" not in builds[0].by_type

    meta = await dl.load_meta(session, builds)
    assert meta[str(ep)].bucket == "unknown" and meta[str(ep)].session_id == "s1"
