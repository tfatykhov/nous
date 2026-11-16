"""Per-goal tool grants change nothing until a goal has one: ``goal_grants_baseline.collect()`` (the tool policy and
the runner's offered tools and refusals for every kind of turn, authority, root, tool and sample input) equals the
recorded fixture with no grant hook bound, and so does ``collect_seams()`` (the tool loop's ``goal_lifted``, the
dispatcher's own bash arguments, and the bash tool's environment names and stdin)."""

from __future__ import annotations

from goal_grants_baseline import SYNTHETIC_ENV, collect, collect_seams, load, load_seams

from nous.api.builtin_tools import _LINEAGE_BUILD_FIXED_ENV


def test_with_no_grant_hook_the_policy_and_the_runner_decide_as_recorded():
    _commit, baseline = load()
    assert collect() == baseline


async def test_with_no_hook_the_tool_loop_the_dispatcher_and_the_bash_tool_act_as_recorded():
    assert await collect_seams() == load_seams()


def test_the_record_names_only_the_invented_variables_and_the_fixed_build_ones():
    named = {name for started in load_seams()["bash_env"].values() for name in started["env"]}
    assert named and named <= set(SYNTHETIC_ENV) | set(_LINEAGE_BUILD_FIXED_ENV)
