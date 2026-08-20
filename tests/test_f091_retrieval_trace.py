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


def test_a_leg_that_ran_and_found_nothing_is_still_recorded():
    """The whole point of the leg record: "ran and returned 0" must be
    distinguishable from "never ran". Registering only inside `if results:`
    collapsed those two into the same empty output."""
    t = _trace()
    t.leg("context_decisions", attempted=True, n_returned=0)

    legs = {leg["name"]: leg for leg in t.to_dict()["legs"]}
    assert legs["context_decisions"]["attempted"] is True
    assert legs["context_decisions"]["n_returned"] == 0


def test_add_many_on_an_empty_list_is_harmless():
    t = _trace()
    t.add_many([], "empty_leg", type_of=lambda i: "fact", score_of=lambda i: 0.0)
    assert t.to_dict()["n_candidates"] == 0


def test_mark_rendered_covers_content_registered_after_finalize():
    """Parent-episode summaries are appended by the formatter AFTER the
    pipeline finished, so finalize's pass cannot see them. Without an explicit
    mark they stay `unaccounted` — memory delivered to the model that the
    counts deny."""
    t = _trace()
    t.finalize([])  # pipeline already done
    ep = uuid4()
    t.add(ep, "episode", "parent_episode", content="summary text")
    t.mark_rendered(ep, "episode", "parent_episode_section")

    d = t.to_dict()
    assert d["candidates"][0]["disposition"] == RENDERED
    assert d["n_rendered"] == 1


def test_mark_rendered_overrides_a_drop_because_delivery_is_authoritative():
    """Superseded contract. This originally asserted that mark_rendered refused
    to overwrite — which loses real content: an item can be dropped by one gate
    and still be delivered through a later channel. Delivery wins; the
    overridden gate is preserved rather than discarded."""
    t = _trace()
    fid = uuid4()
    t.add(fid, "fact", "heart_primary")
    t.drop(fid, "fact", SLICED_OFF, "limit")
    t.mark_rendered(fid, "fact", "late")

    cand = t.to_dict()["candidates"][0]
    assert cand["disposition"] == RENDERED
    assert cand["restored_from"] == f"{SLICED_OFF}@limit"


def test_mark_rendered_on_an_unknown_candidate_is_a_noop():
    t = _trace()
    t.mark_rendered(uuid4(), "episode", "late")
    assert t.to_dict()["n_candidates"] == 0


def test_mark_rendered_overrides_an_earlier_drop_and_keeps_the_gate():
    """A parent episode can be a Heart candidate already cut by the Heart
    limit, then be appended as parent context because a surviving fact pointed
    at it. It genuinely reached the model, so a late authoritative render must
    override — otherwise delivered content stays counted as dropped."""
    t = _trace()
    ep = uuid4()
    t.add(ep, "episode", "heart_primary", score=0.4)
    t.drop(ep, "episode", SLICED_OFF, "heart_recall_limit")
    t.finalize([])

    t.mark_rendered(ep, "episode", "parent_episode_section")

    cand = t.to_dict()["candidates"][0]
    assert cand["disposition"] == RENDERED
    assert cand["restored_from"] == f"{SLICED_OFF}@heart_recall_limit"
    assert t.to_dict()["n_rendered"] == 1


def test_mark_rendered_is_idempotent():
    t = _trace()
    ep = uuid4()
    t.add(ep, "episode", "parent_episode")
    t.mark_rendered(ep, "episode", "parent_episode_section")
    t.mark_rendered(ep, "episode", "parent_episode_section")

    cand = t.to_dict()["candidates"][0]
    assert cand["disposition"] == RENDERED
    # A second call must not manufacture a restored_from from RENDERED itself.
    assert cand["restored_from"] is None


def test_mark_not_delivered_downgrades_a_rendered_candidate():
    """The formatter's per-section scope gate can decline to emit something
    that WAS in the ranked result set — e.g. spreading surfaces a decision on a
    memory_types=["fact"] call. drop() refuses to touch a candidate that
    already has a disposition, so this is the only way to express it."""
    t = _trace()
    did = uuid4()
    t.add(did, "decision", "spreading_activation", score=0.6)
    t.finalize([FakeResult(did, "decision")])
    assert t.to_dict()["candidates"][0]["disposition"] == RENDERED

    t.mark_not_delivered(did, "decision", SLICED_OFF, "formatter_scope_filter")

    cand = t.to_dict()["candidates"][0]
    assert cand["disposition"] == SLICED_OFF
    assert cand["disposition_stage"] == "formatter_scope_filter"
    assert cand["final_rank"] is None
    assert t.to_dict()["n_rendered"] == 0


def test_mark_not_delivered_leaves_an_already_dropped_candidate_alone():
    """The FIRST gate to remove an item is the true cause."""
    t = _trace()
    fid = uuid4()
    t.add(fid, "fact", "heart_primary")
    t.drop(fid, "fact", BELOW_FLOOR, "exemplar_similarity_floor")
    t.mark_not_delivered(fid, "fact", SLICED_OFF, "formatter_scope_filter")

    cand = t.to_dict()["candidates"][0]
    assert cand["disposition"] == BELOW_FLOOR
    assert cand["disposition_stage"] == "exemplar_similarity_floor"


def test_mark_not_delivered_on_an_unknown_candidate_is_a_noop():
    t = _trace()
    t.mark_not_delivered(uuid4(), "decision", SLICED_OFF, "formatter_scope_filter")
    assert t.to_dict()["n_candidates"] == 0


def test_a_deliberately_skipped_leg_is_recorded_not_omitted():
    """A type in skip_types (short acks set skip_types={"fact"}) or a zero
    section budget produced NO leg row — indistinguishable from instrumentation
    that simply is not there."""
    t = _trace()
    t.leg("context_facts", attempted=False, n_returned=0,
          skip_reason="retrieval plan skipped 'fact'")

    leg = t.to_dict()["legs"][0]
    assert leg["attempted"] is False
    assert leg["n_returned"] == 0
    assert "fact" in leg["skip_reason"]


def test_skip_then_run_upgrades_attempted():
    """leg() ORs attempted, so a skip record can never mask a later real run."""
    t = _trace()
    t.leg("context_facts", attempted=False, skip_reason="section budget is 0")
    t.leg("context_facts", attempted=True, n_returned=3)

    leg = t.to_dict()["legs"][0]
    assert leg["attempted"] is True
    assert leg["n_returned"] == 3


# ---------------------------------------------------------------------------
# Non-finite scores (JSONB would reject the whole row)
# ---------------------------------------------------------------------------


def test_non_finite_scores_never_reach_json():
    """cross_encoder_rerank assigns float('-inf') to empty-text candidates.
    json.dumps emits the NON-STANDARD token -Infinity, PostgreSQL JSONB rejects
    it outright, and the writer swallows the error — so one such value silently
    discarded the ENTIRE retrieval row. Telemetry destroying its own record."""
    import json
    import math

    t = _trace()
    a, b, c = uuid4(), uuid4(), uuid4()
    t.add(a, "fact", "heart_primary", score=float("-inf"))
    t.add(b, "fact", "heart_primary", score=float("inf"))
    t.add(c, "fact", "heart_primary", score=float("nan"))
    t.mutate(a, "fact", "ce_rerank", float("-inf"), float("nan"))
    t.leg("heart_primary", scores=[float("-inf"), 0.5])
    t.expansion(seed_id=a, neighbor_id=b, seed_type="fact",
                neighbor_type="decision", stage="s",
                seed_score=float("nan"), edge_weight=float("inf"),
                path_strength=float("-inf"))
    t.finalize([], duration_ms=float("nan"))

    payload = json.dumps(t.to_dict())
    for token in ("Infinity", "-Infinity", "NaN"):
        assert token not in payload, f"{token} would be rejected by JSONB"

    # And it round-trips through strict JSON, which is what asyncpg hands PG.
    json.loads(payload, parse_constant=lambda c: (_ for _ in ()).throw(
        AssertionError(f"non-finite constant {c} survived")))

    by_id = {x["id"]: x for x in t.to_dict()["candidates"]}
    assert by_id[str(a)]["entry_score"] is None
    assert not any(
        isinstance(v, float) and not math.isfinite(v)
        for x in t.to_dict()["candidates"] for v in (x["entry_score"],)
    )


def test_finite_scores_are_left_untouched():
    t = _trace()
    fid = uuid4()
    t.add(fid, "fact", "heart_primary", score=0.0)
    assert t.to_dict()["candidates"][0]["entry_score"] == 0.0


def test_query_chars_zero_suppresses_the_query_entirely():
    """0 means ZERO CHARS, matching snippet_chars. Treating it as "unlimited"
    inverted the setting: an operator setting 0 to suppress user text got the
    complete unbounded message persisted for the whole retention window."""
    t = RetrievalTrace(query="a very private user message", query_chars=0)
    assert t.to_dict()["query"] == ""


def test_query_chars_negative_is_clamped_not_wrapped():
    """A negative slice would wrap and keep the message MINUS n chars."""
    t = RetrievalTrace(query="abcdefghij", query_chars=-3)
    assert t.to_dict()["query"] == ""


def test_query_chars_positive_truncates():
    t = RetrievalTrace(query="abcdefghij", query_chars=4)
    assert t.to_dict()["query"] == "abcd"


def test_snippet_chars_zero_suppresses_snippets_too():
    """The sibling setting this one now matches."""
    t = _trace(snippet_chars=0)
    t.add(uuid4(), "fact", "heart_primary", content="secret content")
    assert t.to_dict()["candidates"][0]["snippet"] == ""


def test_undeliver_all_unclaims_rendered_when_nothing_reached_the_model():
    """The pipeline can finish and finalize can mark survivors delivered, and
    THEN a later step (the formatter) can raise so the tool returns only an
    error. Without un-claiming, the persisted row asserts memory reached the
    model when no memory text was ever emitted."""
    t = _trace()
    a, b, c = uuid4(), uuid4(), uuid4()
    t.add(a, "fact", "heart_primary", score=0.9)
    t.add(b, "fact", "heart_primary", score=0.8)
    t.add(c, "fact", "heart_primary", score=0.1)
    t.drop(c, "fact", BELOW_FLOOR, "floor")
    t.finalize([FakeResult(a, "fact"), FakeResult(b, "fact")])
    assert t.to_dict()["n_rendered"] == 2

    t.undeliver_all(SLICED_OFF, "recall_deep_failed")

    d = t.to_dict()
    assert d["n_rendered"] == 0
    by_id = {x["id"]: x for x in d["candidates"]}
    assert by_id[str(a)]["disposition"] == SLICED_OFF
    assert by_id[str(a)]["disposition_stage"] == "recall_deep_failed"
    assert by_id[str(a)]["final_rank"] is None
    # An earlier, more specific gate is the true cause and must survive.
    assert by_id[str(c)]["disposition"] == BELOW_FLOOR
    assert by_id[str(c)]["disposition_stage"] == "floor"


def test_undeliver_all_is_a_noop_when_nothing_was_rendered():
    t = _trace()
    fid = uuid4()
    t.add(fid, "fact", "heart_primary")
    t.drop(fid, "fact", BELOW_FLOOR, "floor")
    t.undeliver_all(SLICED_OFF, "recall_deep_failed")
    assert t.to_dict()["candidates"][0]["disposition"] == BELOW_FLOOR


# ---------------------------------------------------------------------------
# Capture cap must never manufacture a total loss
# ---------------------------------------------------------------------------

def test_capture_cap_does_not_zero_out_survivors():
    """The cap bounds ROW SIZE. It must never turn a normal retrieval into a
    row claiming nothing reached the model.

    `add` is first-wins and refuses new ids once full, so a stage that
    registers its LOSERS before its WINNERS could exhaust every slot on
    dropped candidates. `finalize` then found no survivor to mark and the row
    read "N entered -> 0 rendered, N dropped at a gate" — indistinguishable
    from a real systemic failure, on exactly the sampled rows an operator
    opens to diagnose one.
    """
    t = _trace(max_candidates=5)
    for _ in range(8):
        lost = uuid4()
        t.add(lost, "fact", "heart_primary", score=0.1)
        t.drop(lost, "fact", SLICED_OFF, "heart_recall_limit")

    survivors = [FakeResult(uuid4(), "fact") for _ in range(3)]
    t.finalize(survivors)

    d = t.to_dict()
    assert d["n_rendered"] == 3, "survivors must be represented even past the cap"
    assert d["truncated"] is True, "the cap must still be reported, not hidden"
    # Conservation: every recorded candidate carries exactly one disposition.
    assert sum(d["disposition_counts"].values()) == d["n_candidates"]
    ranks = sorted(c["final_rank"] for c in d["candidates"] if c["disposition"] == RENDERED)
    assert ranks == [1, 2, 3]


def test_capture_cap_backstop_records_lost_provenance_honestly():
    """A backstopped candidate must not invent an entry leg it never had."""
    t = _trace(max_candidates=1)
    lost = uuid4()
    t.add(lost, "fact", "heart_primary")
    t.drop(lost, "fact", SLICED_OFF, "heart_recall_limit")

    late = uuid4()
    t.finalize([FakeResult(late, "fact")])

    by_id = {c["id"]: c for c in t.to_dict()["candidates"]}
    assert by_id[str(late)]["disposition"] == RENDERED
    assert "cap" in (by_id[str(late)]["entry_leg"] or "")


def test_real_trace_and_null_trace_agree_on_enabled():
    """`enabled` must exist on BOTH, or a future `if tr.enabled:` guard raises
    only when telemetry is actually on — failing in the one case it guards."""
    assert NullTrace().enabled is False
    assert _trace().enabled is True
