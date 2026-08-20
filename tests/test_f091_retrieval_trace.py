"""F091: retrieval trace collector — contract + drop-attribution invariants."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from uuid import uuid4

import pytest

from nous.observability.retrieval_trace import (
    BUDGET_TRUNCATED,
    BELOW_FLOOR,
    DEDUPED,
    F071_EXCLUDED,
    NULL_TRACE,
    RENDERED,
    SLICED_OFF,
    UNACCOUNTED,
    NullTrace,
    RetrievalTrace,
)


@dataclass
class FakeResult:
    id: object
    type: str
    score: float = 0.5
    summary: str = ""


def _trace(**kw) -> RetrievalTrace:
    kw.setdefault("query", "why is my fact missing")
    kw.setdefault("agent_id", "nous-test")
    return RetrievalTrace(**kw)


# ---------------------------------------------------------------------------
# Null-object contract
# ---------------------------------------------------------------------------


def test_nulltrace_exposes_every_public_method_of_retrievaltrace():
    """A capture method added to one must be added to the other.

    Instrumented call sites never guard on `is not None`, so a method present
    on RetrievalTrace but missing from NullTrace is an AttributeError that
    only fires with telemetry disabled — i.e. in the configuration least
    likely to be exercised in tests.
    """
    def public_methods(cls):
        return {
            name for name, _ in inspect.getmembers(cls)
            if not name.startswith("_")
        }

    missing = public_methods(RetrievalTrace) - public_methods(NullTrace)
    assert not missing, f"NullTrace is missing: {sorted(missing)}"


def test_nulltrace_swallows_every_call():
    t = NULL_TRACE
    t.leg("heart_primary", n_returned=5)
    t.add(uuid4(), "fact", "heart_primary", score=0.9)
    t.mutate(uuid4(), "fact", "boost", 0.5, 0.6)
    t.drop(uuid4(), "fact", SLICED_OFF, "limit")
    t.exclude_type("censor", "coherent_ranking")
    t.expansion(seed_id=uuid4(), neighbor_id=uuid4(), seed_type="fact",
                neighbor_type="decision", stage="stage2")
    t.finalize([], duration_ms=1.0)
    assert t.to_dict() == {}
    assert t.n_rendered == 0


# ---------------------------------------------------------------------------
# Drop attribution
# ---------------------------------------------------------------------------


def test_survivors_are_rendered_and_ranked():
    t = _trace()
    a, b = uuid4(), uuid4()
    t.add(a, "fact", "heart_primary", score=0.9)
    t.add(b, "fact", "heart_primary", score=0.7)

    t.finalize([FakeResult(a, "fact"), FakeResult(b, "fact")])

    d = t.to_dict()
    by_id = {c["id"]: c for c in d["candidates"]}
    assert by_id[str(a)]["disposition"] == RENDERED
    assert by_id[str(a)]["final_rank"] == 1
    assert by_id[str(b)]["final_rank"] == 2
    assert d["n_rendered"] == 2


def test_dropped_candidate_keeps_its_gate_and_is_not_rendered():
    t = _trace()
    kept, dropped = uuid4(), uuid4()
    t.add(kept, "fact", "heart_primary", score=0.9)
    t.add(dropped, "fact", "heart_primary", score=0.2)

    t.drop(dropped, "fact", BELOW_FLOOR, "exemplar_similarity_floor")
    t.finalize([FakeResult(kept, "fact")])

    by_id = {c["id"]: c for c in t.to_dict()["candidates"]}
    assert by_id[str(dropped)]["disposition"] == BELOW_FLOOR
    assert by_id[str(dropped)]["disposition_stage"] == "exemplar_similarity_floor"
    assert by_id[str(dropped)]["final_rank"] is None


def test_first_drop_wins():
    """An early gate's attribution must not be overwritten by a later stage
    that merely observes the candidate's absence."""
    t = _trace()
    fid = uuid4()
    t.add(fid, "fact", "heart_primary", score=0.5)

    t.drop(fid, "fact", DEDUPED, "keyed_r2_prefilter")
    t.drop(fid, "fact", F071_EXCLUDED, "f071")

    cand = t.to_dict()["candidates"][0]
    assert cand["disposition"] == DEDUPED
    assert cand["disposition_stage"] == "keyed_r2_prefilter"


def test_unclaimed_candidate_is_unaccounted_not_silently_plausible():
    """Drift guard: a filter added later that forgets to report leaves a
    distinguishable value, not a believable-looking `sliced_off`."""
    t = _trace()
    ghost = uuid4()
    t.add(ghost, "fact", "heart_primary", score=0.4)

    t.finalize([])  # never rendered, never explicitly dropped

    assert t.to_dict()["candidates"][0]["disposition"] == UNACCOUNTED


def test_every_candidate_is_accounted_for():
    """n_rendered + dropped == n_candidates, the completeness invariant."""
    t = _trace()
    ids = [uuid4() for _ in range(5)]
    for i, fid in enumerate(ids):
        t.add(fid, "fact", "heart_primary", score=1.0 - i * 0.1)

    t.drop(ids[3], "fact", SLICED_OFF, "heart_recall_limit")
    t.drop(ids[4], "fact", F071_EXCLUDED, "f071")
    t.finalize([FakeResult(i, "fact") for i in ids[:3]])

    d = t.to_dict()
    assert sum(d["disposition_counts"].values()) == d["n_candidates"] == 5
    assert d["disposition_counts"][RENDERED] == 3
    assert UNACCOUNTED not in d["disposition_counts"]


def test_type_exclusion_is_recorded_without_candidates():
    """F080 drops censor/procedure BEFORE search, so there are no candidates
    to attribute — it needs its own channel."""
    t = _trace()
    t.exclude_type("censor", "f080_coherent_ranking")
    t.exclude_type("procedure", "f080_coherent_ranking")

    d = t.to_dict()
    assert {e["type"] for e in d["excluded_types"]} == {"censor", "procedure"}
    assert d["n_candidates"] == 0


def test_same_id_different_type_does_not_collide():
    t = _trace()
    shared = uuid4()
    t.add(shared, "fact", "heart_primary", score=0.9)
    t.add(shared, "episode", "heart_primary", score=0.4)

    t.drop(shared, "episode", SLICED_OFF, "limit")
    t.finalize([FakeResult(shared, "fact")])

    by_type = {c["type"]: c for c in t.to_dict()["candidates"]}
    assert by_type["fact"]["disposition"] == RENDERED
    assert by_type["episode"]["disposition"] == SLICED_OFF


def test_first_leg_to_report_owns_entry_attribution():
    t = _trace()
    fid = uuid4()
    t.add(fid, "fact", "heart_primary", score=0.8, rank=1)
    t.add(fid, "fact", "keyed", score=0.55, rank=1)

    cand = t.to_dict()["candidates"][0]
    assert cand["entry_leg"] == "heart_primary"
    assert cand["entry_score"] == 0.8


# ---------------------------------------------------------------------------
# Mutations, legs, expansions
# ---------------------------------------------------------------------------


def test_mutations_accumulate_in_order():
    t = _trace()
    fid = uuid4()
    t.add(fid, "fact", "heart_primary", score=0.50)
    t.mutate(fid, "fact", "adjacency_boost", 0.50, 0.58, reason="degree=3")
    t.mutate(fid, "fact", "recency_resolver", 0.58, 0.17, reason="superseded")

    muts = t.to_dict()["candidates"][0]["mutations"]
    assert [m["stage"] for m in muts] == ["adjacency_boost", "recency_resolver"]
    assert muts[1]["score_after"] == pytest.approx(0.17)


def test_mutating_unknown_candidate_is_a_noop():
    t = _trace()
    t.mutate(uuid4(), "fact", "boost", 0.1, 0.2)
    assert t.to_dict()["n_candidates"] == 0


def test_leg_distinguishes_ran_empty_from_never_ran():
    t = _trace()
    t.leg("heart_primary", attempted=True, n_returned=0, scores=[])
    t.leg("exemplar", attempted=False, skip_reason="not classification-shaped")

    legs = {leg["name"]: leg for leg in t.to_dict()["legs"]}
    assert legs["heart_primary"]["attempted"] is True
    assert legs["exemplar"]["attempted"] is False
    assert legs["exemplar"]["skip_reason"] == "not classification-shaped"


def test_leg_tracks_score_range_across_updates():
    t = _trace()
    t.leg("heart_primary", scores=[0.4, 0.9])
    t.leg("heart_primary", scores=[0.2, 0.6])

    leg = t.to_dict()["legs"][0]
    assert leg["score_min"] == pytest.approx(0.2)
    assert leg["score_max"] == pytest.approx(0.9)


def test_expansion_captures_the_full_edge():
    t = _trace()
    seed, nbr = uuid4(), uuid4()
    t.expansion(
        seed_id=seed, seed_type="fact", seed_score=0.8,
        neighbor_id=nbr, neighbor_type="decision",
        stage="stage2_heart_graph", hop=1,
        edge_relation="evidence_for", edge_weight=0.72,
        extraction_method="inferred", path_strength=0.576,
        won_best_path=True,
    )
    e = t.to_dict()["expansions"][0]
    assert e["seed_id"] == str(seed)
    assert e["neighbor_id"] == str(nbr)
    assert e["edge_relation"] == "evidence_for"
    assert e["path_strength"] == pytest.approx(0.576)
    assert t.to_dict()["n_expansions"] == 1


def test_expansion_without_endpoints_is_ignored():
    t = _trace()
    t.expansion(seed_id=None, neighbor_id=uuid4(), seed_type="fact",
                neighbor_type="fact", stage="s")
    assert t.to_dict()["n_expansions"] == 0


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


def test_snippet_is_truncated():
    t = _trace(snippet_chars=10)
    fid = uuid4()
    t.add(fid, "fact", "heart_primary", content="x" * 500)
    assert t.to_dict()["candidates"][0]["snippet"] == "x" * 10


def test_max_candidates_caps_and_flags_truncation():
    t = _trace(max_candidates=3)
    for _ in range(10):
        t.add(uuid4(), "fact", "heart_primary")

    d = t.to_dict()
    assert d["n_candidates"] == 3
    assert d["truncated"] is True


def test_sampling_off_keeps_header_legs_and_expansions():
    """An unsampled retrieval still answers 'which legs fired' and 'how did
    graph expansion work' — only the per-candidate array is dropped."""
    t = _trace(capture_candidates=False)
    t.leg("heart_primary", n_returned=7)
    t.add(uuid4(), "fact", "heart_primary", score=0.9)
    t.expansion(seed_id=uuid4(), neighbor_id=uuid4(), seed_type="fact",
                neighbor_type="decision", stage="stage2")
    t.finalize([])

    d = t.to_dict()
    assert d["candidates"] is None
    assert d["n_candidates"] == 0
    assert d["legs"][0]["n_returned"] == 7
    assert d["n_expansions"] == 1


# ---------------------------------------------------------------------------
# Resurrection (F083 fact pinning)
# ---------------------------------------------------------------------------


def test_a_dropped_candidate_that_reaches_the_prompt_reads_rendered():
    """`_reinsert_pinned` exists to rescue facts past diversity/relevance
    demotion. The drop really happened, but the fact IS in the prompt — so
    the disposition must say rendered or the accounting misreports context."""
    t = _trace()
    fid = uuid4()
    t.add(fid, "fact", "context_facts", score=0.85)
    t.drop(fid, "fact", "filter_dropped", "diversity")

    t.finalize([FakeResult(fid, "fact")])

    cand = t.to_dict()["candidates"][0]
    assert cand["disposition"] == RENDERED
    assert cand["restored_from"] == "filter_dropped@diversity"
    assert cand["final_rank"] == 1


def test_restored_from_is_absent_for_an_ordinary_survivor():
    t = _trace()
    fid = uuid4()
    t.add(fid, "fact", "context_facts", score=0.9)
    t.finalize([FakeResult(fid, "fact")])
    assert t.to_dict()["candidates"][0]["restored_from"] is None


def test_a_drop_that_is_never_resurrected_keeps_its_gate():
    t = _trace()
    fid = uuid4()
    t.add(fid, "fact", "context_facts", score=0.3)
    t.drop(fid, "fact", "filter_dropped", "diversity")
    t.finalize([])

    cand = t.to_dict()["candidates"][0]
    assert cand["disposition"] == "filter_dropped"
    assert cand["restored_from"] is None


def test_rendered_count_matches_what_was_finalized_even_with_resurrection():
    t = _trace()
    ids = [uuid4() for _ in range(4)]
    for f in ids:
        t.add(f, "fact", "context_facts", score=0.5)
    t.drop(ids[0], "fact", "filter_dropped", "diversity")
    t.drop(ids[1], "fact", "filter_dropped", "diversity")

    t.finalize([FakeResult(ids[0], "fact"), FakeResult(ids[2], "fact")])

    d = t.to_dict()
    assert d["n_rendered"] == 2
    assert sum(d["disposition_counts"].values()) == 4


# ---------------------------------------------------------------------------
# Robustness: telemetry must never break the retrieval it observes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("content", [12345, {"a": 1}, ["x"], object(), 3.14])
def test_non_string_content_is_coerced_not_raised(content):
    """`add` takes whatever a producer's result object carries in its
    description/summary field. A raise here would propagate into the
    retrieval hot path and break recall — the exact outcome telemetry must
    never cause."""
    t = _trace()
    fid = uuid4()
    t.add(fid, "fact", "heart_primary", content=content)

    snippet = t.to_dict()["candidates"][0]["snippet"]
    assert isinstance(snippet, str)
    assert snippet


def test_none_content_yields_empty_snippet():
    t = _trace()
    t.add(uuid4(), "fact", "heart_primary", content=None)
    assert t.to_dict()["candidates"][0]["snippet"] == ""


def test_coerced_content_still_respects_the_truncation_bound():
    t = _trace(snippet_chars=5)
    t.add(uuid4(), "fact", "heart_primary", content=1234567890)
    assert t.to_dict()["candidates"][0]["snippet"] == "12345"


def test_leg_update_without_a_count_preserves_the_existing_one():
    """The pipeline's attempted-legs rollup runs AFTER the keyed/exemplar legs
    report their own yield. Passing 0 there overwrote a correct count with
    zero, so those legs must be updated with n_returned=None instead."""
    t = _trace()
    t.leg("keyed", attempted=True, n_returned=3, n_deduped=2)
    t.leg("keyed", attempted=True, n_returned=None)  # rollup pass

    leg = t.to_dict()["legs"][0]
    assert leg["n_returned"] == 3
    assert leg["n_deduped"] == 2


def test_leg_update_with_an_explicit_count_still_overwrites():
    t = _trace()
    t.leg("heart_primary", n_returned=3)
    t.leg("heart_primary", n_returned=9)
    assert t.to_dict()["legs"][0]["n_returned"] == 9


def test_budget_truncated_survives_finalize_when_excluded_from_rendered():
    """Section-budget truncation happens AFTER recalled_ids is collected, so
    the caller must exclude cut ids from what it hands finalize(). If it does,
    the drop must stick — finalize's resurrection rule is for items that
    genuinely reached the model."""
    t = _trace()
    kept, cut = uuid4(), uuid4()
    t.add(kept, "fact", "context_facts", score=0.9)
    t.add(cut, "fact", "context_facts", score=0.8)
    t.drop(cut, "fact", BUDGET_TRUNCATED, "section_budget_truncation")

    t.finalize([FakeResult(kept, "fact")])  # cut id deliberately absent

    by_id = {c["id"]: c for c in t.to_dict()["candidates"]}
    assert by_id[str(cut)]["disposition"] == BUDGET_TRUNCATED
    assert by_id[str(cut)]["restored_from"] is None
    assert t.to_dict()["n_rendered"] == 1


def test_budget_truncated_is_resurrected_if_caller_still_reports_it_rendered():
    """Documents the trap: finalize treats its argument as authoritative, so a
    caller that forgets to exclude budget-cut ids silently undoes the
    attribution. context.py filters via _tr_budget_cut for exactly this."""
    t = _trace()
    cut = uuid4()
    t.add(cut, "fact", "context_facts", score=0.8)
    t.drop(cut, "fact", BUDGET_TRUNCATED, "section_budget_truncation")

    t.finalize([FakeResult(cut, "fact")])

    cand = t.to_dict()["candidates"][0]
    assert cand["disposition"] == RENDERED
    assert cand["restored_from"] == f"{BUDGET_TRUNCATED}@section_budget_truncation"


# ---------------------------------------------------------------------------
# Best-path resolution (won_best_path is decided at finalize, not at record time)
# ---------------------------------------------------------------------------


def _exp(t, seed, nbr, strength, stage="s"):
    t.expansion(seed_id=seed, seed_type="fact", neighbor_id=nbr,
                neighbor_type="decision", stage=stage, path_strength=strength)


def test_a_later_stronger_path_wins_over_an_earlier_weak_one():
    """Recorded eagerly this was first-arrival-wins, which named the LOSER as
    the winner wherever the pipeline does best-composed-path replacement."""
    t = _trace()
    nbr = uuid4()
    _exp(t, uuid4(), nbr, 0.1)   # weak, arrives first
    _exp(t, uuid4(), nbr, 0.9)   # strong, arrives second

    t.finalize([])

    exps = t.to_dict()["expansions"]
    assert [e["won_best_path"] for e in exps] == [False, True]


def test_exactly_one_winner_per_neighbour_and_stage():
    t = _trace()
    nbr = uuid4()
    for s in (0.2, 0.7, 0.5):
        _exp(t, uuid4(), nbr, s)
    t.finalize([])
    assert sum(1 for e in t.to_dict()["expansions"] if e["won_best_path"]) == 1


def test_same_neighbour_via_different_stages_each_get_a_winner():
    """Stages are independent traversals — one must not suppress the other."""
    t = _trace()
    nbr = uuid4()
    _exp(t, uuid4(), nbr, 0.3, stage="stage2")
    _exp(t, uuid4(), nbr, 0.8, stage="stage4")
    t.finalize([])
    assert all(e["won_best_path"] for e in t.to_dict()["expansions"])


def test_none_strength_loses_to_a_scored_path():
    t = _trace()
    nbr = uuid4()
    _exp(t, uuid4(), nbr, None)
    _exp(t, uuid4(), nbr, 0.01)
    t.finalize([])
    exps = t.to_dict()["expansions"]
    assert [e["won_best_path"] for e in exps] == [False, True]


def test_best_paths_resolve_even_when_candidates_are_not_sampled():
    """Expansions are captured at 100% regardless of sampling, so their
    resolution must not sit behind the candidate-capture guard."""
    t = _trace(capture_candidates=False)
    nbr = uuid4()
    _exp(t, uuid4(), nbr, 0.1)
    _exp(t, uuid4(), nbr, 0.9)
    t.finalize([])
    assert [e["won_best_path"] for e in t.to_dict()["expansions"]] == [False, True]


def test_query_is_truncated_at_construction():
    """The context path passes the raw user message; an untruncated copy would
    sit in a diagnostics table for the full retention window."""
    t = RetrievalTrace(query="x" * 5000, query_chars=500)
    assert len(t.to_dict()["query"]) == 500
