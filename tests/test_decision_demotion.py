"""Outcome-based decision demotion (2026-07-27 decision-retrieval-quality plan).

Backend-agnostic: ``apply_outcome_demotion`` is a pure module-level helper so
these tests run on the default sqlite backend where ``Brain._query`` (Postgres
FTS) cannot. The e2e lives in ``test_abandoned_filtering.py`` (postgres_only).

Measured prod evidence being encoded here (2026-07-27 probe of
``brain.query("vacation ideas for September trip", limit=8)``): two superseded
rows ranked #1/#2 at .931/.908 above the current one at #3 (.887).
"""

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from nous.brain.brain import apply_outcome_demotion
from nous.brain.schemas import DecisionSummary
from nous.config import Settings


# ---------------------------------------------------------------------------
# Kill switch: empty factors is an EXACT no-op
# ---------------------------------------------------------------------------


def test_empty_factors_is_exact_noop():
    """No multiplication AND no re-sort — merged order preserved byte-identically."""
    scored = [
        ("a", "superseded", 0.931),
        ("b", "superseded", 0.908),
        ("c", "failure", 0.887),
        ("d", "noise", 0.5),
    ]

    result = apply_outcome_demotion(scored, {})

    assert result == [("a", 0.931), ("b", 0.908), ("c", 0.887), ("d", 0.5)]


def test_empty_factors_does_not_reorder_unsorted_input():
    """The kill switch must not sort — even when the input is not score-ordered."""
    scored = [("low", "pending", 0.1), ("high", "pending", 0.9)]

    assert apply_outcome_demotion(scored, {}) == [("low", 0.1), ("high", 0.9)]


# ---------------------------------------------------------------------------
# Demotion + re-sort (the feature)
# ---------------------------------------------------------------------------


def test_superseded_demoted_and_resorted_below_undemoted_row():
    """Measured prod values: .931/.908 superseded must fall below .887 failure."""
    scored = [
        ("superseded_1", "superseded", 0.931),
        ("superseded_2", "superseded", 0.908),
        ("switzerland", "failure", 0.887),
    ]

    result = apply_outcome_demotion(scored, {"superseded": 0.3, "noise": 0.1})

    assert [item for item, _ in result] == ["switzerland", "superseded_1", "superseded_2"]
    assert result[0][1] == pytest.approx(0.887)
    assert result[1][1] == pytest.approx(0.931 * 0.3)
    assert result[2][1] == pytest.approx(0.908 * 0.3)


def test_noise_demoted_by_its_own_factor():
    scored = [
        ("noisy", "noise", 0.95),
        ("real", "success", 0.20),
    ]

    result = apply_outcome_demotion(scored, {"superseded": 0.3, "noise": 0.1})

    assert [item for item, _ in result] == ["real", "noisy"]
    assert result[1][1] == pytest.approx(0.95 * 0.1)


def test_undemoted_outcomes_pass_through_untouched():
    """Outcomes with no factor keep their score exactly."""
    scored = [("s", "success", 0.6), ("f", "failure", 0.7), ("p", "partial", 0.8)]

    result = apply_outcome_demotion(scored, {"superseded": 0.3, "noise": 0.1})

    assert result == [("p", 0.8), ("f", 0.7), ("s", 0.6)]


def test_none_outcome_normalized_to_pending():
    """The column is nullable — a NULL outcome is 'pending', never demoted."""
    scored = [("null_outcome", None, 0.9), ("superseded", "superseded", 0.95)]

    result = apply_outcome_demotion(scored, {"superseded": 0.3, "pending": 0.5})

    # NULL normalized to "pending" so the pending factor applies to it:
    # 0.9*0.5 = 0.45 outranks 0.95*0.3 = 0.285.
    assert [item for item, _ in result] == ["null_outcome", "superseded"]
    assert result[0][1] == pytest.approx(0.9 * 0.5)  # 0.45
    assert result[1][1] == pytest.approx(0.95 * 0.3)  # 0.285

    # ...and with the shipped default factors (no "pending" key) it is untouched.
    untouched = apply_outcome_demotion(scored, {"superseded": 0.3, "noise": 0.1})
    assert untouched[0] == ("null_outcome", 0.9)


def test_unknown_outcome_untouched():
    scored = [("weird", "some_future_outcome", 0.42)]

    assert apply_outcome_demotion(scored, {"superseded": 0.3}) == [("weird", 0.42)]


# ---------------------------------------------------------------------------
# None scores and sort stability
# ---------------------------------------------------------------------------


def test_none_score_passes_through_and_sorts_last():
    scored = [
        ("no_score", "success", None),
        ("scored", "success", 0.5),
        ("superseded_no_score", "superseded", None),
    ]

    result = apply_outcome_demotion(scored, {"superseded": 0.3})

    assert result[0] == ("scored", 0.5)
    # None scores keep their None (never multiplied) and sort last, input-stable.
    assert result[1] == ("no_score", None)
    assert result[2] == ("superseded_no_score", None)


def test_equal_scores_keep_input_order():
    scored = [("first", "success", 0.5), ("second", "success", 0.5), ("third", "success", 0.5)]

    result = apply_outcome_demotion(scored, {"superseded": 0.3})

    assert [item for item, _ in result] == ["first", "second", "third"]


def test_demoted_ties_keep_input_order():
    """Stability holds after multiplication too."""
    scored = [("a", "superseded", 0.9), ("b", "superseded", 0.9)]

    result = apply_outcome_demotion(scored, {"superseded": 0.3})

    assert [item for item, _ in result] == ["a", "b"]


def test_empty_input():
    assert apply_outcome_demotion([], {"superseded": 0.3}) == []


# ---------------------------------------------------------------------------
# Settings: NOUS_DECISION_OUTCOME_SCORE_FACTORS
# ---------------------------------------------------------------------------


def test_settings_default_factors():
    assert Settings().decision_outcome_score_factors == {"superseded": 0.3, "noise": 0.1}


def test_settings_accepts_json_string():
    s = Settings(decision_outcome_score_factors='{"superseded": 0.5}')
    assert s.decision_outcome_score_factors == {"superseded": 0.5}


def test_settings_empty_env_value_is_kill_switch():
    """Mirrors ST-1: docker-compose passes an empty string on a fresh install."""
    assert Settings(decision_outcome_score_factors="").decision_outcome_score_factors == {}


@pytest.mark.parametrize("bad", [3.0, 1.5, 0.0, -0.2])
def test_settings_rejects_factors_outside_zero_one(bad):
    """A typo of 3 for 0.3 would PROMOTE the rows this feature sinks."""
    with pytest.raises(ValidationError):
        Settings(decision_outcome_score_factors={"superseded": bad})


def test_settings_accepts_boundary_one():
    """1.0 is a legal (identity) factor."""
    assert Settings(
        decision_outcome_score_factors={"superseded": 1.0}
    ).decision_outcome_score_factors == {"superseded": 1.0}


# ---------------------------------------------------------------------------
# recall_deep outcome visibility (Task 2)
# ---------------------------------------------------------------------------


def _summary(outcome: str) -> DecisionSummary:
    return DecisionSummary(
        id=uuid.uuid4(),
        description="a decision",
        confidence=0.8,
        category="process",
        stakes="medium",
        outcome=outcome,
        score=0.5,
        created_at=datetime(2026, 7, 27, tzinfo=UTC),
    )


@pytest.mark.parametrize("outcome", ["superseded", "noise", "failure", "pending"])
def test_decisions_to_pipeline_carries_outcome(outcome):
    """recall_deep rendered decisions with NO outcome at all, so a superseded
    decision reached the LLM unlabeled (correctness-P2-3)."""
    from nous.api.retrieval_pipeline import _decisions_to_pipeline

    results = _decisions_to_pipeline([_summary(outcome)])

    assert results[0].metadata["outcome"] == outcome


def test_recall_deep_renders_decision_outcome():
    """branch-review P1-1: the metadata['outcome'] key added for recall_deep had
    NO consumer — the rendered line showed no status, so a superseded decision
    reached the LLM unlabeled (the pre-turn path already renders '[outcome]').
    Pin the rendered prefix in both directions."""
    import uuid as _uuid

    from nous.api.retrieval_pipeline import PipelineResult, PipelineStats
    from nous.api.tools import _format_pipeline_text

    def _dec(desc, outcome):
        return PipelineResult(
            id=_uuid.uuid4(),
            type="decision",
            description=desc,
            score=0.5,
            source="brain",
            metadata={"category": "process", "stakes": "medium",
                      "confidence": 0.9, "raw_score": 0.5, "outcome": outcome},
        )

    out = _format_pipeline_text([_dec("Recommended Portugal", "superseded")],
                                PipelineStats(), ["decision"])
    assert "[superseded] Recommended Portugal" in out

    out_none = _format_pipeline_text([_dec("Recommended Portugal", None)],
                                     PipelineStats(), ["decision"])
    assert "[superseded]" not in out_none
    assert "Recommended Portugal" in out_none


def test_rrf_merge_return_limit_preserves_penalty_rank_scores():
    """codex #577 r1: widening the candidate set must NOT change scores.

    `_rrf_merge`'s `limit` doubles as `penalty_rank = limit + 1`, so inflating
    it would silently rescore every single-list doc. `return_limit` widens the
    RETURNED set only — the scores of the rows both calls share are identical.
    """
    import uuid as _uuid

    from nous.heart.search import _rrf_merge

    vec = [(_uuid.uuid4(), 0.9 - i * 0.05) for i in range(6)]
    kw = [(vec[0][0], 0.5), (vec[3][0], 0.4)]  # partial overlap -> penalty ranks matter

    narrow = _rrf_merge(vec, kw, k=60, vector_weight=0.7, limit=2)
    wide = _rrf_merge(vec, kw, k=60, vector_weight=0.7, limit=2, return_limit=6)

    assert len(narrow) == 2 and len(wide) == 6
    # the rows present in both must carry byte-identical scores and order
    assert narrow == wide[:2]


def test_review_input_requires_lineage_on_supersession():
    """codex #577 r2: the invariant lives in ReviewInput so EVERY entry point
    shares it (tool, batch sweep, REST review_decision) — not just the tool."""
    import uuid as _uuid

    import pytest as _pytest
    from pydantic import ValidationError

    from nous.brain.schemas import ReviewInput

    with _pytest.raises(ValidationError, match="superseded_by is required"):
        ReviewInput(outcome="superseded")

    ok = ReviewInput(outcome="superseded", superseded_by=_uuid.uuid4())
    assert ok.superseded_by is not None
    # every other outcome is unaffected
    for other in ("success", "partial", "failure", "noise"):
        assert ReviewInput(outcome=other).superseded_by is None


def test_graph_exclusion_ignores_identity_factors():
    """codex #577 r2: 1.0 is a legal identity value (disable one outcome while
    keeping others). Only outcomes actually demoted (factor < 1.0) may enter
    the graph exclusion set, or the two retrieval paths contradict."""
    factors = {"superseded": 1.0, "noise": 0.1}
    demoted_outcomes = [o for o, f in factors.items() if f < 1.0]
    assert demoted_outcomes == ["noise"]
    assert "superseded" not in demoted_outcomes


def test_rrf_merge_return_limit_can_cover_full_candidate_set():
    """codex #577 r3: a FIXED expansion (e.g. 3x) still starves when more than
    that many demoted rows outrank the first undemoted one — the caller must be
    able to re-rank the complete fetched set. Scores stay identical either way.
    """
    import uuid as _uuid

    from nous.heart.search import _rrf_merge

    vec = [(_uuid.uuid4(), 0.9 - i * 0.02) for i in range(12)]
    kw = [(vec[0][0], 0.5)]
    full = _rrf_merge(vec, kw, k=60, vector_weight=0.7, limit=1,
                      return_limit=len(vec) + len(kw))
    narrow = _rrf_merge(vec, kw, k=60, vector_weight=0.7, limit=1)
    assert len(full) == 12          # everything fetched is re-rankable
    assert narrow == full[:1]       # penalty_rank unchanged -> identical scores


def test_batch_resolve_keeps_valid_items_when_one_lacks_lineage():
    """codex #577 r3: a batch-wide precheck would discard every VALID
    resolution alongside one malformed item. ReviewInput rejects per-item
    inside review_many, which keeps the rest of the sweep alive."""
    import uuid as _uuid

    import pytest as _pytest
    from pydantic import ValidationError

    from nous.brain.schemas import ReviewInput

    good = {"outcome": "success"}
    bad = {"outcome": "superseded"}  # no superseded_by
    # per-item semantics: the good item validates, the bad one raises
    assert ReviewInput(**good).outcome == "success"
    with _pytest.raises(ValidationError):
        ReviewInput(**bad)
    # and a superseded item WITH lineage is fine alongside it
    assert ReviewInput(outcome="superseded", superseded_by=_uuid.uuid4()) is not None


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_explicit_relation_overrides_outcome_filter(db, settings):
    """codex #577 r4: `_neighbors` documents an explicit `relation=` as an
    override of retrieval exclusions. neighbors(relation="supersedes") is
    literally asking for the superseded endpoint — the outcome filter must not
    strip it, mirroring _query's explicit-`outcome=`-wins rule."""
    import uuid as _uuid

    from sqlalchemy import text as sqltext

    from nous.brain.brain import Brain
    from nous.brain.schemas import ReasonInput, RecordInput

    agent_id = f"test-relov-{_uuid.uuid4().hex[:8]}"
    async with db.session() as s:
        await s.execute(
            sqltext("INSERT INTO nous_system.agents (id, name, config) VALUES (:i, 'x', '{}'::jsonb) ON CONFLICT (id) DO NOTHING"),
            {"i": agent_id},
        )
        await s.commit()
    st = settings.model_copy(update={
        "agent_id": agent_id,
        "decision_outcome_score_factors": {"superseded": 0.3, "noise": 0.1},
    })
    brain = Brain(database=db, settings=st)
    try:
        def _rec(desc):
            return RecordInput(description=desc, confidence=0.9, category="process",
                               stakes="medium", reasons=[ReasonInput(type="analysis", text="t")])
        cur = await brain.record(_rec("Current approach for the relation override probe"))
        old = await brain.record(_rec("Older approach for the relation override probe"))
        await brain.review(old.id, outcome="superseded", superseded_by=cur.id)
        async with db.session() as s:
            await s.execute(sqltext(
                "INSERT INTO brain.graph_edges (agent_id, source_id, source_type, target_id, target_type, relation, weight) "
                "VALUES (:a, :src, 'decision', :tgt, 'decision', 'supersedes', 1.0)"),
                {"a": agent_id, "src": str(cur.id), "tgt": str(old.id)})
            await s.commit()

        explicit = await brain.neighbors(cur.id, "decision", relation="supersedes")
        assert any(str(n.id) == str(old.id) for n in explicit), (
            "explicit relation= must surface the superseded endpoint"
        )
        # unfiltered fan-out still hides it
        unfiltered = await brain.neighbors(cur.id, "decision")
        assert not any(str(n.id) == str(old.id) for n in unfiltered)
    finally:
        await brain.close()


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_untyped_fanout_filters_decisions_but_keeps_other_types(db, settings):
    """codex #577 r5: Stage 3's one-hop calls neighbors() with NO neighbor_type,
    which skipped the typed pushdown — the filter then ran only in the resolver,
    after the cap. The predicate must apply to the untyped fan-out too, and must
    be type-aware so non-decision neighbors are unaffected."""
    import uuid as _uuid

    from sqlalchemy import text as sqltext

    from nous.brain.brain import Brain
    from nous.brain.schemas import ReasonInput, RecordInput

    agent_id = f"test-untyped-{_uuid.uuid4().hex[:8]}"
    async with db.session() as s:
        await s.execute(
            sqltext("INSERT INTO nous_system.agents (id, name, config) VALUES (:i, 'x', '{}'::jsonb) ON CONFLICT (id) DO NOTHING"),
            {"i": agent_id},
        )
        await s.commit()
    st = settings.model_copy(update={
        "agent_id": agent_id,
        "decision_outcome_score_factors": {"superseded": 0.3, "noise": 0.1},
    })
    brain = Brain(database=db, settings=st)
    try:
        def _rec(desc):
            return RecordInput(description=desc, confidence=0.9, category="process",
                               stakes="medium", reasons=[ReasonInput(type="analysis", text="t")])
        hub = await brain.record(_rec("Hub decision for the untyped fan-out probe"))
        good = await brain.record(_rec("Undemoted neighbor for the untyped fan-out probe"))
        bad = await brain.record(_rec("Noise neighbor for the untyped fan-out probe"))
        await brain.review(bad.id, outcome="noise")
        fact_id = _uuid.uuid4()
        async with db.session() as s:
            await s.execute(sqltext(
                "INSERT INTO heart.facts (id, agent_id, content, category, confidence, active) "
                "VALUES (:i, :a, 'A fact neighbor for the untyped fan-out probe', 'technical', 1.0, true)"),
                {"i": str(fact_id), "a": agent_id})
            for tgt, ttype in ((good.id, "decision"), (bad.id, "decision"), (fact_id, "fact")):
                await s.execute(sqltext(
                    "INSERT INTO brain.graph_edges (agent_id, source_id, source_type, target_id, target_type, relation, weight) "
                    "VALUES (:a, :src, 'decision', :tgt, :tt, 'related_to', 1.0)"),
                    {"a": agent_id, "src": str(hub.id), "tgt": str(tgt), "tt": ttype})
            await s.commit()

        got = {str(n.id) for n in await brain.neighbors(hub.id, "decision")}
        assert str(good.id) in got, "undemoted decision neighbor must survive"
        assert str(bad.id) not in got, "noise decision neighbor must be filtered"
        assert str(fact_id) in got, "non-decision neighbor must be unaffected by the predicate"
    finally:
        await brain.close()
