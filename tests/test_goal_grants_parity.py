"""Per-goal tool grants change nothing until a goal has one: ``goal_grants_baseline.collect()`` (the tool policy and
the runner's offered tools and refusals for every kind of turn, authority, root, tool and sample input) equals the
recorded fixture with no grant hook bound, and so does ``collect_seams()`` (the tool loop's ``goal_lifted``, the
dispatcher's own bash arguments, and the bash tool's environment names and stdin). One difference from the record is
stated, and tested on its own: ``stated_difference``."""

from __future__ import annotations

from goal_grants_baseline import (
    MODES,
    STATED_REFUSAL,
    SYNTHETIC_ENV,
    collect,
    collect_seams,
    load,
    load_seams,
    stated_difference,
    stated_rows,
)

from nous.api.builtin_tools import _LINEAGE_BUILD_FIXED_ENV


def test_with_no_grant_hook_the_policy_and_the_runner_decide_as_recorded():
    _commit, baseline = load()
    assert stated_difference(collect(), baseline) == baseline


def test_the_one_stated_difference_is_an_allowlisted_bash_call_forced_into_a_tasks_offered_set_with_the_shell_off():
    """With the lineage shell off and no goal running any command, bash added to the offered names of an internal_only
    task or workflow step (its offered set never holds it then) is refused ``not_allowed`` in every mode where the
    base let through a command the lineage allowlist accepts. Every other row of the section is as recorded."""
    _commit, baseline = load()
    forced = collect()["auth_forced"]
    stated = stated_rows(baseline)
    kinds_roots = [(kind, root) for kind in ("subtask", "dag_node") for root in ("none", "root")]
    assert stated == {f"{kind}|internal_only|{root}|bash|1" for kind, root in kinds_roots}
    assert {key for key in forced if forced[key] != baseline["auth_forced"][key]} == stated
    for key in stated:
        assert forced[key] == dict.fromkeys(MODES, STATED_REFUSAL), key


async def test_with_no_hook_the_tool_loop_the_dispatcher_and_the_bash_tool_act_as_recorded():
    assert await collect_seams() == load_seams()


def test_the_record_names_only_the_invented_variables_and_the_fixed_build_ones():
    named = {name for started in load_seams()["bash_env"].values() for name in started["env"]}
    assert named and named <= set(SYNTHETIC_ENV) | set(_LINEAGE_BUILD_FIXED_ENV)
