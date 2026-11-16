"""Per-goal tool grants change nothing until a goal has one: ``goal_grants_baseline.collect()`` (the tool policy and
the runner's offered tools and refusals for every kind of turn, authority, root, tool and sample input) equals the
recorded fixture with no grant hook bound."""

from __future__ import annotations

from goal_grants_baseline import collect, load


def test_with_no_grant_hook_the_policy_and_the_runner_decide_as_recorded():
    _commit, baseline = load()
    assert collect() == baseline
