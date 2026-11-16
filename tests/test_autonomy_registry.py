"""F115: the registry of the limits the owner may change, the host ceilings and the cross rule (no database)."""

from __future__ import annotations

import logging
import math

import pytest
from annotated_types import Ge, Gt, Le, Lt

from nous.autonomy import registry
from nous.autonomy.registry import LIMITS, by_key, ceiling_problem, cross_problem, parse_ceilings, range_problem
from nous.config import Settings

KEYS = (
    "max_turns",
    "subtask_tool_call_limit",
    "continuation_max_turns_per_root",
    "continuation_max_spawns_per_root",
    "continuation_max_depth",
    "continuation_max_tokens_per_root",
    "continuation_stall_limit",
    "intention_root_ttl_hours",
    "intention_proposal_ttl_hours",
    "subtask_default_timeout",
    "subtask_max_timeout",
)
RANGES = {
    "max_turns": (5, 1000),
    "subtask_tool_call_limit": (1, 200),
    "continuation_max_turns_per_root": (1, 20),
    "continuation_max_spawns_per_root": (1, 30),
    "continuation_max_depth": (1, 6),
    "continuation_max_tokens_per_root": (10_000, 1_000_000),
    "continuation_stall_limit": (1, 5),
    "intention_root_ttl_hours": (1, 168),
    "intention_proposal_ttl_hours": (1, 72),
    "subtask_default_timeout": (60, 6000),
    "subtask_max_timeout": (60, 10_000),
}


def test_the_registry_lists_exactly_the_eleven_live_limits_in_order():
    assert tuple(limit.key for limit in LIMITS) == KEYS
    assert {limit.key: (limit.low, limit.high) for limit in LIMITS} == RANGES


@pytest.mark.parametrize("limit", LIMITS, ids=lambda limit: limit.key)
def test_each_key_is_a_settings_field_whose_own_rule_holds_the_whole_range(limit):
    field = Settings.model_fields[limit.key]
    assert field.annotation in (int, float), field.annotation
    default = field.default
    assert limit.low <= default <= limit.high, (limit.key, default)
    for rule in field.metadata:
        if isinstance(rule, Ge):
            assert limit.low >= rule.ge
        elif isinstance(rule, Gt):
            assert limit.low > rule.gt
        elif isinstance(rule, Le):
            assert limit.high <= rule.le
        elif isinstance(rule, Lt):
            assert limit.high < rule.lt
    # The ends of the range validate as the field's own value.
    for value in (limit.low, limit.high):
        Settings(_env_file=None, **{limit.key: value})


@pytest.mark.parametrize("limit", LIMITS, ids=lambda limit: limit.key)
def test_each_limit_says_what_it_is_and_when_it_applies(limit):
    assert limit.label and limit.unit and limit.applies
    assert limit.label[0].isupper() and not limit.label.endswith(".")
    assert limit.applies.endswith(".")


def test_no_new_setting_name_contains_proposal_or_owner_action():
    for name in ("autonomy_settings_enabled", "tool_guard_enabled", "autonomy_ceilings", "autonomy_locked_tools"):
        assert name in Settings.model_fields
        assert "proposal" not in name and "owner_action" not in name


@pytest.mark.parametrize(
    ("value", "refused"),
    [
        (5, False),
        (1000, False),
        (4, True),
        (1001, True),
        (True, True),
        (False, True),
        (50.0, True),
        (float("nan"), True),
        (math.inf, True),
        ("50", True),
        (None, True),
    ],
)
def test_a_value_must_be_a_whole_number_inside_the_range(value, refused):
    problem = range_problem(by_key("max_turns"), value)
    assert (problem is not None) is refused
    if refused:
        assert "a whole number from 5 to 1,000" in problem


def test_the_float_keys_take_whole_hours_only():
    ttl = by_key("intention_root_ttl_hours")
    assert range_problem(ttl, 72) is None
    for value in (1.5, float("nan"), float("inf"), -1, 0, 169):
        assert range_problem(ttl, value) is not None, value


def test_a_ceiling_refuses_a_value_above_it_and_names_it():
    limit = by_key("subtask_tool_call_limit")
    assert ceiling_problem(limit, 60, None) is None
    assert ceiling_problem(limit, 60, 60) is None
    problem = ceiling_problem(limit, 61, 60)
    assert problem is not None and "60" in problem and "server" in problem


def test_parse_ceilings_keeps_valid_entries_and_drops_the_rest_with_one_warning(caplog):
    caplog.set_level(logging.WARNING)
    parsed = parse_ceilings(
        " subtask_tool_call_limit = 60 , max_turns=800,bogus=3,max_turns,depth=x,subtask_max_timeout=0"
    )
    assert parsed == {"subtask_tool_call_limit": 60, "max_turns": 800}
    warnings = [r for r in caplog.records if r.name == registry.__name__ and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "NOUS_AUTONOMY_CEILINGS" in warnings[0].getMessage()


def test_parse_ceilings_of_nothing_is_no_ceiling(caplog):
    caplog.set_level(logging.WARNING)
    assert parse_ceilings("") == {}
    assert parse_ceilings("  ") == {}
    assert not caplog.records


def test_a_ceiling_outside_the_range_is_dropped():
    assert parse_ceilings("max_turns=4") == {}
    assert parse_ceilings("max_turns=1001") == {}
    assert parse_ceilings("max_turns=5,max_turns=6") == {"max_turns": 6}


def test_the_cross_rule_is_the_two_subtask_timeouts():
    assert cross_problem({"subtask_default_timeout": 600, "subtask_max_timeout": 600}) is None
    assert cross_problem({"subtask_default_timeout": 600, "subtask_max_timeout": 3600}) is None
    problem = cross_problem({"subtask_default_timeout": 3601, "subtask_max_timeout": 3600})
    assert problem is not None and "3,600" in problem
    assert cross_problem({"max_turns": 10}) is None


def test_the_cross_rule_judges_only_a_pair_that_holds_a_changed_key():
    host = {"subtask_default_timeout": 4000, "subtask_max_timeout": 3600, "max_turns": 10}
    assert cross_problem(host) is not None
    assert cross_problem(host, changed=["max_turns"]) is None
    assert cross_problem(host, changed=[]) is None
    assert cross_problem(host, changed=["subtask_max_timeout"]) is not None


def test_by_key_of_an_unknown_key_is_none():
    assert by_key("nope") is None
    assert by_key("goals_queue_headroom") is None
