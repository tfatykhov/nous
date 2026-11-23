"""F118 PR 3: how the server's settings and the stored values combine (``second_settings.resolve``), and when the
second judge sends. No database."""

from __future__ import annotations

import itertools

import pytest

from nous.config import Settings
from nous.judge import service
from nous.judge.jev_judge import QUESTION_VERSION, make_jev_judge
from nous.judge.second_settings import (
    NO_KEY_FILE,
    OTHER_KEY,
    UNREADABLE,
    SecondConfig,
    StoredRow,
    confirm_text,
    resolve,
    sends,
)

ENV_KEY = "ts-env-" + "E5" * 10
STORED_KEY = "ts-stored-" + "S7" * 10
SEALED = {"key_id": "0123456789abcdef", "nonce": b"\x00" * 12, "ciphertext": b"\x01" * 40}


def _settings(second: str = "", key: str = "", model: str = "", calls: int = 100) -> Settings:
    return Settings(
        _env_file=None,
        outside_judge_mode="shadow",
        outside_judge_second=second,
        TYPESAFE_API_KEY=key,
        outside_judge_jev_model=model,
        outside_judge_daily_calls=calls,
    )


@pytest.mark.parametrize(
    ("second", "key", "model", "calls"),
    list(itertools.product(["", "jev"], ["", ENV_KEY, f"  {ENV_KEY} "], ["", "jev-1.13.0"], [0, 1, 100])),
)
def test_with_nothing_stored_the_values_are_the_servers_as_the_environments_second_judge_reads_them(
    second, key, model, calls
):
    settings = _settings(second, key, model, calls)
    config = resolve(settings, None, None, None)
    built = make_jev_judge(settings)  # PR 2's judge from the same environment
    assert config.enabled is (second == "jev")
    assert config.model == settings.outside_judge_jev_model
    assert config.key == (built._key if built is not None else None)
    assert config.daily_calls == calls
    assert (config.enabled_source, config.model_source, config.calls_source) == ("server", "server", "server")
    assert config.key_source == ("server" if key.strip() else "none") and config.key_problem is None
    # PR 2 runs a second judge exactly when it is chosen and has a key; it sends only with a limit above 0.
    assert sends(config) is (second == "jev" and built is not None and calls > 0)
    assert resolve(settings, StoredRow(), None, None) == config


@pytest.mark.parametrize("enabled", [None, True, False])
@pytest.mark.parametrize("model", [None, "jev-2.0.1"])
@pytest.mark.parametrize("calls", [None, 0, 7, 100])
@pytest.mark.parametrize("second", ["", "jev"])
def test_each_stored_value_applies_over_the_servers(enabled, model, calls, second):
    settings = _settings(second, ENV_KEY, "jev-1.14.0", 100)
    config = resolve(settings, StoredRow(enabled=enabled, model=model, daily_calls=calls), None, None)
    assert config.enabled is (enabled if enabled is not None else second == "jev")
    assert config.enabled_source == ("stored" if enabled is not None else "server")
    assert config.model == (model or "jev-1.14.0")
    assert config.model_source == ("stored" if model is not None else "server")
    assert config.daily_calls == (calls if calls is not None else 100)
    assert config.calls_source == ("stored" if calls is not None else "server")
    assert sends(config) is (config.enabled and config.daily_calls > 0)


@pytest.mark.parametrize(("stored", "most"), [(101, 100), (5000, 3), (1, 0)])
def test_a_stored_limit_above_the_servers_is_used_lowered(stored, most):
    config = resolve(_settings(calls=most), StoredRow(daily_calls=stored), None, None)
    assert (config.daily_calls, config.calls_source) == (most, "lowered")


def test_a_stored_limit_at_the_servers_is_the_stored_one():
    config = resolve(_settings(calls=100), StoredRow(daily_calls=100), None, None)
    assert (config.daily_calls, config.calls_source) == (100, "stored")


@pytest.mark.parametrize("server_key", ["", ENV_KEY])
def test_a_stored_key_that_opens_is_used_over_the_servers(server_key):
    row = StoredRow(enabled=True, **SEALED)
    config = resolve(_settings(key=server_key), row, STORED_KEY, None)
    assert (config.key, config.key_source, config.key_problem) == (STORED_KEY, "stored", None)
    assert sends(config) is True


@pytest.mark.parametrize("problem", [NO_KEY_FILE, OTHER_KEY, UNREADABLE])
@pytest.mark.parametrize("server_key", ["", ENV_KEY])
def test_a_stored_key_that_does_not_open_is_never_replaced_by_the_servers(problem, server_key):
    row = StoredRow(enabled=True, **SEALED)
    config = resolve(_settings("jev", server_key), row, None, problem)
    assert (config.key, config.key_source, config.key_problem) == (None, "stored", problem)
    assert sends(config) is False


def test_with_no_stored_key_a_problem_given_is_not_reported():
    config = resolve(_settings(key=ENV_KEY), StoredRow(), None, OTHER_KEY)
    assert (config.key, config.key_source, config.key_problem) == (ENV_KEY, "server", None)


@pytest.mark.parametrize(
    ("enabled", "key", "calls", "expected"),
    [
        (True, STORED_KEY, 1, True),
        (False, STORED_KEY, 1, False),
        (True, None, 1, False),
        (True, "", 1, False),
        (True, STORED_KEY, 0, False),
        (False, None, 0, False),
    ],
)
def test_it_sends_only_on_with_a_key_and_a_limit_above_0(enabled, key, calls, expected):
    config = SecondConfig(enabled, "jev-1.14.0", key, calls, "stored", "server", "stored", "stored", None)
    assert sends(config) is expected


def test_the_key_is_in_no_repr():
    config = resolve(_settings("jev", ENV_KEY), StoredRow(**SEALED), STORED_KEY, None)
    assert STORED_KEY not in repr(config) and ENV_KEY not in repr(config)
    row = StoredRow(enabled=True, **SEALED)
    assert repr(SEALED["ciphertext"]) not in repr(row) and repr(SEALED["nonce"]) not in repr(row)


def test_the_confirmation_says_what_leaves_to_which_company_and_the_limit():
    from nous.judge import evidence

    text = confirm_text(25)
    assert text.startswith(
        "Send a copy of each decision Nous resolves to TypeSafe (api.typesafe.ai), an outside company?"
    )
    # The number of characters is the record's own bound.
    assert f"up to {evidence.EVIDENCE_MAX_CHARS:,} characters" in text
    assert "At most 25 copies a day." in text and "Nothing is sent while this is off." in text
    assert "At most 1 copy a day." in confirm_text(1)
    assert "At most 5,000 copies a day." in confirm_text(5000)
    # Without the hourly pass the sentence is word for word the one owners have said yes to.
    assert text == (
        "Send a copy of each decision Nous resolves to TypeSafe (api.typesafe.ai), an outside company? The copy holds "
        "the decision, Nous's note and grade, and what its tools did in that session, with their inputs and results, "
        "up to 16,000 characters. Text that looks like a key or a password is removed; everything else is sent as it "
        "is. TypeSafe says it does not train on what it receives; it does not say for how many days it keeps it. At "
        "most 25 copies a day. Nothing is sent while this is off."
    )


def test_with_the_hourly_pass_the_confirmation_also_says_what_it_sends_and_how_far_back():
    assert confirm_text(25, hourly=True) == (
        "Send a copy of each decision Nous resolves to TypeSafe (api.typesafe.ai), an outside company? The copy holds "
        "the decision, Nous's note and grade, and what its tools did in that session, with their inputs and results, "
        "up to 16,000 characters. Decisions are also sent by an hourly pass: about once an hour, and two minutes after "
        "a start or a saved change, up to 5 that Jev has not graded yet with this model and its current instructions, "
        "from those Nous graded in the last 7 days and those graded by the owner, the review route or an automatic "
        "check in the last 30 days. With a new model or new instructions they are sent again. Decisions graded by the "
        "owner, the review route or an automatic check are sent without the grade and its note. A copy whose request "
        "failed is sent again, at most 3 times for one model and one set of instructions. Text that looks like a key "
        "or a password is removed; everything else is sent as it is. TypeSafe says it does not train on what it "
        "receives; it does not say for how many days it keeps it. At most 25 copies a day. Nothing is sent while this "
        "is off."
    )
    assert "At most 1 copy a day." in confirm_text(1, hourly=True)
    assert confirm_text(25, hourly=False) == confirm_text(25)
    # The sentence's "two minutes after a start" and "about once an hour" are the pass's own times.
    assert (service.SWEEP_FIRST_DELAY_SECONDS, service.SWEEP_INTERVAL_SECONDS) == (120.0, 3600.0)
    # "with this model and its current instructions": the pass's key is the judge's version, the question version and
    # the model, so a new one of either sends the window again.
    assert make_jev_judge(_settings(key=ENV_KEY, model="jev-1.13.0")).version == f"{QUESTION_VERSION}:jev-1.13.0"
