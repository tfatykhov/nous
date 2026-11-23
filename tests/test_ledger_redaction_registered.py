"""F118 PR 3: a secret value that is not in the environment (an API key saved on the dashboard) is registered with the
ledger's redaction before it is used and masked like the environment's own secrets until it is forgotten.

No database. The conftest fixture ``_no_registered_secrets`` ends every test with an empty registry.
"""

from __future__ import annotations

import json
import os
import threading

import pytest

from nous.cognitive import ledger_store
from nous.cognitive.ledger_store import (
    SECRET_MASK,
    forget_own_secret,
    redact_output,
    redact_structure,
    redacted_head,
    redaction_scan_pad,
    register_own_secret,
)

# Not shaped like any key the patterns know: only the registry can mask it.
VALUE = "plain-words-7Qx2Lm9Rv"
ESCAPED = 'quote"and\\slash-and-é-4Kd8'


def _spellings(value: str) -> list[str]:
    return [value, json.dumps(value, ensure_ascii=False)[1:-1], json.dumps(value)[1:-1], repr(value)[1:-1]]


def test_an_unregistered_value_is_not_masked():
    assert redact_output(f"the value {VALUE} here") == f"the value {VALUE} here"


@pytest.mark.parametrize("value", [VALUE, ESCAPED])
def test_a_registered_value_is_masked_in_every_spelling_by_every_redaction(value):
    register_own_secret(value)
    for spelling in _spellings(value):
        text = f"before {spelling} after"
        assert spelling not in redact_output(text)
        assert SECRET_MASK in redact_output(text)
        assert spelling not in json.dumps(redact_structure({"k": [text, {"n": text}]}))
        assert spelling not in redacted_head(text, 400)
        assert spelling not in redacted_head("x" * 500 + text + "y" * 900, 2000)


def test_the_scan_pad_counts_a_registered_value():
    long_value = "L" * 400
    assert redaction_scan_pad() < 400
    register_own_secret(long_value)
    assert redaction_scan_pad() >= 400
    forget_own_secret(long_value)
    assert redaction_scan_pad() < 400


def test_registrations_are_counted():
    register_own_secret(VALUE)
    register_own_secret(VALUE)
    forget_own_secret(VALUE)
    assert VALUE not in redact_output(VALUE)  # once forgotten, still registered once
    forget_own_secret(VALUE)
    assert redact_output(VALUE) == VALUE
    forget_own_secret(VALUE)  # one more than registered: ignored
    assert redact_output(VALUE) == VALUE
    register_own_secret(VALUE)
    assert VALUE not in redact_output(VALUE)


@pytest.mark.parametrize("value", ["", "short", "7-chars", None, 12345678, b"bytes-value-1"])
def test_a_value_shorter_than_8_characters_or_not_text_is_refused_without_the_value(value):
    with pytest.raises(ValueError) as raised:
        register_own_secret(value)  # type: ignore[arg-type]
    assert str(raised.value) == "a registered secret must be a string of at least 8 characters"
    assert ledger_store._registered_spellings == ()


def test_the_environment_is_not_changed():
    before = dict(os.environ)
    register_own_secret(VALUE)
    redact_output(VALUE)
    forget_own_secret(VALUE)
    assert dict(os.environ) == before
    assert VALUE not in "".join(os.environ.values())


def test_with_nothing_registered_the_redaction_reads_the_environment_alone():
    """The default: no value registered, the same values as before the registry existed."""
    assert ledger_store._registered_spellings == ()
    assert ledger_store._secrets_now() == ledger_store._own_secrets()


def test_registering_while_another_thread_redacts_never_raises_and_never_misses_an_earlier_value():
    stop = threading.Event()
    failures: list[BaseException] = []
    registered: list[str] = []
    lock = threading.Lock()

    def redact_loop() -> None:
        try:
            while not stop.is_set():
                with lock:
                    known = list(registered)  # every value whose registration ended before this call began
                text = " ".join(known)
                out = redact_output(text)
                for value in known:
                    assert value not in out, value
        except BaseException as exc:  # noqa: BLE001 - reported in the test's own thread
            failures.append(exc)

    readers = [threading.Thread(target=redact_loop) for _ in range(3)]
    for thread in readers:
        thread.start()
    try:
        for i in range(200):
            value = f"registered-value-{i:04d}-Zq"
            register_own_secret(value)
            with lock:
                registered.append(value)
            if i % 3 == 0:
                forget_own_secret(f"never-registered-{i}")
    finally:
        stop.set()
        for thread in readers:
            thread.join(30)
    assert failures == []
