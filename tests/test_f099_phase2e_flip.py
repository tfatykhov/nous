"""F099 Phase 2e-9: the flip. `CONTINUATION_RUNNER_READY` is True, the owner turns the flag on, and until then prod runs
exactly as before. The pins that said "not ready" are replaced here and in two earlier files."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
from f099_support import env_factory, runner_env  # noqa: F401
from test_f099_phase2c_parity import PROD, Untouchable

import nous.main as main
from nous.brain import continuation
from nous.config import Settings
from nous.handlers.continuation_runner import ContinuationRunner

ROOT = Path(__file__).resolve().parents[1]
# The settings that make a flag-on process valid: intentions need the inbox.
BASE = {"result_inbox_enabled": True, "intentions_enabled": True}


def test_the_runner_is_ready_and_the_constant_is_set_in_one_place():  # PIN
    assert continuation.CONTINUATION_RUNNER_READY is True
    assignments = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "nous").rglob("*.py")
        if re.search(r"^CONTINUATION_RUNNER_READY\b.*=", path.read_text(encoding="utf-8"), re.MULTILINE)
    ]
    assert assignments == ["nous/brain/continuation.py"]  # nothing else sets or clears it


def test_the_flag_is_off_by_default_and_compose_passes_it_with_a_false_default():  # PIN
    assert Settings(_env_file=None).continuation_enabled is False
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "- NOUS_CONTINUATION_ENABLED=${NOUS_CONTINUATION_ENABLED:-false}" in compose


def test_the_gate_lets_the_owners_flag_through_without_a_warning(caplog):
    settings = Settings(_env_file=None, continuation_enabled=True, **BASE)
    with caplog.at_level(logging.WARNING, logger="nous.main"):
        main._gate_continuation_flag(settings)
    assert settings.continuation_enabled is True
    assert "not shipped in this build" not in caplog.text


def test_a_build_that_clears_the_constant_is_still_gated(monkeypatch, caplog):
    """The mechanism stays: a build without the runner forces the flag off with the same WARNING."""
    monkeypatch.setattr(continuation, "CONTINUATION_RUNNER_READY", False)
    settings = Settings(_env_file=None, continuation_enabled=True, **BASE)
    with caplog.at_level(logging.WARNING, logger="nous.main"):
        main._gate_continuation_flag(settings)
    assert settings.continuation_enabled is False
    assert "continuation runner is not shipped in this build" in caplog.text


async def test_prods_flags_build_no_runner_after_the_flip():  # PIN: the whole point of "until the owner turns it on"
    """Inbox, intentions and result memory on, continuation off: with the constant True, still no runner."""
    settings = Settings(_env_file=None, **PROD)
    assert continuation.CONTINUATION_RUNNER_READY is True and settings.continuation_enabled is False
    main._gate_continuation_flag(settings)
    assert settings.continuation_enabled is False
    untouched = {name: Untouchable() for name in ("database", "runner", "heart", "brain", "bus", "dispatcher")}
    assert await main._build_continuation_runner(settings, **untouched) is None


@pytest.mark.postgres_only
async def test_with_the_flag_on_the_runner_is_built_wired_and_started(runner_env):  # noqa: F811
    """What the owner gets: the same build the 2c-2 rehearsal made with the constant monkeypatched, with none."""
    env = await runner_env()  # continuation on
    main._gate_continuation_flag(env.settings)
    assert env.settings.continuation_enabled is True
    built = await main._build_continuation_runner(
        env.settings,
        database=env.db,
        runner=env.runner,
        heart=env.heart,
        brain=env.brain,
        bus=None,
        dispatcher=env.dispatcher,
    )
    try:
        assert isinstance(built, ContinuationRunner) and built._task is None
        assert env.runner._root_cancelled == built.root_is_cancelled  # the cancel reaches every tool call
        await built.start()
        assert built._task is not None
    finally:
        await built.stop()


def test_the_docs_say_what_the_flip_did():
    env_doc = (ROOT / "docs/reference/environment-variables.md").read_text(encoding="utf-8")
    row = next(line for line in env_doc.splitlines() if line.startswith("| `NOUS_CONTINUATION_ENABLED`"))
    assert "Until PR-2e" not in row and "the owner turns it on" in row
    rest = (ROOT / "docs/reference/rest-api.md").read_text(encoding="utf-8")
    assert "| GET | `/intentions` |" in rest and "| POST | `/intentions/{root_id}/cancel` |" in rest
    contract = (ROOT / "docs/superpowers/plans/2026-10-06-f099-phase2-contract.md").read_text(encoding="utf-8")
    assert "Superseded by 2e" in contract
    shipped = (ROOT / "docs/reference/shipped-features.md").read_text(encoding="utf-8")
    assert "| F099 Phase 2e |" in shipped
