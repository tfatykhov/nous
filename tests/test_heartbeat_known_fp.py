"""Known heartbeat false positives: rules that auto-close a finding at ingest.

In-memory only (real FindingStore, rule files under tmp_path), so these run
in every CI tier.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from nous.a2ui.tools import register_a2ui_tools
from nous.api.tools import ToolDispatcher
from nous.heartbeat.finding_store import FindingStore
from nous.heartbeat.known_fp import KnownFalsePositiveRules, parse_rule
from nous.heartbeat.schemas import Finding, FindingAction, FindingState

TODAY = date(2026, 10, 8)

SNN_RULE = {
    "id": "snn-integration-deferral",
    "match": {"contains": "SNN integration deferral"},
    "reason": "Deferral is deliberate; re-reported every tick.",
    "added_by": "nous",
    "review_by": "2026-12-31",
}
SITE_RULE = {
    "id": "cognition-engines-action-not-deployed",
    "match": {"check": "site-monitor", "regex": r"action not deployed.*cognition[- ]engines"},
    "reason": "Static site has no deploy action.",
    "added_by": "tim",
    "review_by": "2026-12-31",
}


def _rules(*raw: dict, today: date = TODAY) -> KnownFalsePositiveRules:
    return KnownFalsePositiveRules(rules=[parse_rule(r) for r in raw], today=lambda: today)


def _store(rules: KnownFalsePositiveRules | None) -> FindingStore:
    store = FindingStore(known_fp_rules=rules)
    store._startup_suppression_seconds = 0
    return store


def _finding(summary: str = "Reminder: SNN integration deferral still open", **kw: Any) -> Finding:
    kw.setdefault("source", "self")
    kw.setdefault("check_name", "self_initiated")
    return Finding(summary=summary, **kw)


_MTIME_NS = [1_700_000_000_000_000_000]


def _write(path, data: Any) -> None:
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    # Strictly increasing mtime per write: two writes inside the filesystem's
    # timestamp granularity would otherwise share one.
    _MTIME_NS[0] += 1_000_000_000
    os.utime(path, ns=(_MTIME_NS[0], _MTIME_NS[0]))


# ---------------------------------------------------------------------------
# Match -> auto-closed, still visible, counted
# ---------------------------------------------------------------------------


def test_matching_finding_is_auto_closed_and_counted() -> None:
    store = _store(_rules(SNN_RULE))
    finding = _finding()

    assert store.ingest(finding) == FindingAction.AUTO_CLOSE

    tracked = store.get_tracked(finding.fingerprint())
    assert tracked.state == FindingState.AUTO_CLOSED_KNOWN_FP
    assert tracked.auto_closed_rule == "snn-integration-deferral"
    (listed,) = store.to_list()
    assert listed["state"] == "auto_closed_known_fp"
    assert listed["auto_closed_rule"] == "snn-integration-deferral"
    stats = store.stats()
    assert stats["by_state"] == {"auto_closed_known_fp": 1}
    assert stats["auto_closed_by_rule"] == {"snn-integration-deferral": 1}
    # Never offered for triage or on a digest/card.
    assert store.get_digest_items() == []


def test_recurring_match_counts_once_not_per_tick() -> None:
    store = _store(_rules(SNN_RULE))
    for _ in range(5):
        assert store.ingest(_finding()) == FindingAction.AUTO_CLOSE
    assert store.stats()["auto_closed_by_rule"] == {"snn-integration-deferral": 1}
    assert store.get_tracked(_finding().fingerprint()).seen_count == 5


def test_match_is_case_insensitive_and_check_scoped() -> None:
    store = _store(_rules(SITE_RULE))
    hit = _finding("ACTION NOT DEPLOYED for Cognition-Engines site", check_name="site-monitor")
    wrong_check = _finding("Action not deployed for cognition-engines site", check_name="other")
    agent_item = _finding("action not deployed: cognition engines", check_name="agent:site-monitor:1a2b3c4d")

    assert store.ingest(hit) == FindingAction.AUTO_CLOSE
    assert store.ingest(wrong_check) == FindingAction.TRIAGE
    assert store.ingest(agent_item) == FindingAction.AUTO_CLOSE


def test_unmatched_finding_flows_normally() -> None:
    store = _store(_rules(SNN_RULE))
    assert store.ingest(_finding("disk 91% full")) == FindingAction.TRIAGE


def test_auto_close_wins_over_startup_suppression() -> None:
    store = FindingStore(known_fp_rules=_rules(SNN_RULE))  # startup window active
    assert store.ingest(_finding()) == FindingAction.AUTO_CLOSE


def test_already_surfaced_finding_keeps_its_lifecycle() -> None:
    """A NEW/ACKNOWLEDGED finding is already in front of the owner: a rule
    added later does not silently take it away."""
    store = _store(None)
    finding = _finding()
    store.ingest(finding)
    store.acknowledge(finding.fingerprint())
    store._known_fp = _rules(SNN_RULE)

    assert store.ingest(finding) == FindingAction.SUPPRESS
    assert store.get_tracked(finding.fingerprint()).state == FindingState.ACKNOWLEDGED


def test_prune_drops_auto_closed_that_stopped_recurring() -> None:
    store = _store(_rules(SNN_RULE))
    store.ingest(_finding())
    store.get_tracked(_finding().fingerprint()).last_seen -= timedelta(days=8)
    assert store.prune(resolved_ttl_days=7) == 1
    # The counter is cumulative and survives the prune.
    assert store.stats()["auto_closed_by_rule"] == {"snn-integration-deferral": 1}


# ---------------------------------------------------------------------------
# Safety: urgent / escalated never auto-closed, expired rules stop matching
# ---------------------------------------------------------------------------


def test_urgent_finding_is_never_auto_closed() -> None:
    store = _store(_rules(SNN_RULE))
    assert store.ingest(_finding(urgency="high")) == FindingAction.TRIAGE
    assert store.stats()["auto_closed_by_rule"] == {}


def test_previously_closed_finding_reopens_when_it_turns_urgent() -> None:
    store = _store(_rules(SNN_RULE))
    assert store.ingest(_finding(urgency="normal")) == FindingAction.AUTO_CLOSE
    # Urgency is not part of the fingerprint: the same finding, now high.
    assert store.ingest(_finding(urgency="high")) == FindingAction.TRIAGE
    assert store.get_tracked(_finding().fingerprint()).state == FindingState.NEW


def test_escalated_finding_is_never_auto_closed() -> None:
    store = _store(None)
    finding = _finding()
    store.ingest(finding)
    tracked = store.get_tracked(finding.fingerprint())
    tracked.escalated = True
    store.resolve(finding.fingerprint())
    store._known_fp = _rules(SNN_RULE)

    assert store.ingest(finding) == FindingAction.TRIAGE


def test_expired_rule_stops_matching_and_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    rules = _rules({**SNN_RULE, "review_by": "2026-10-07"})
    store = _store(rules)
    with caplog.at_level(logging.WARNING, logger="nous.heartbeat.known_fp"):
        assert store.ingest(_finding()) == FindingAction.TRIAGE
        store.ingest(_finding())
        store.ingest(_finding("another SNN integration deferral note"))
    warnings = [r for r in caplog.records if "review_by" in r.getMessage()]
    assert len(warnings) == 1
    assert "snn-integration-deferral" in warnings[0].getMessage()


def test_rule_still_matches_on_its_review_by_date() -> None:
    store = _store(_rules({**SNN_RULE, "review_by": TODAY.isoformat()}))
    assert store.ingest(_finding()) == FindingAction.AUTO_CLOSE


def test_auto_closed_finding_flows_normally_once_its_rule_expires() -> None:
    day = [TODAY]
    rule = parse_rule({**SNN_RULE, "review_by": TODAY.isoformat()})
    rules = KnownFalsePositiveRules(rules=[rule], today=lambda: day[0])
    store = _store(rules)
    assert store.ingest(_finding()) == FindingAction.AUTO_CLOSE

    day[0] = TODAY + timedelta(days=1)
    assert store.ingest(_finding()) == FindingAction.TRIAGE
    tracked = store.get_tracked(_finding().fingerprint())
    assert tracked.state == FindingState.NEW
    assert tracked.auto_closed_rule is None


# ---------------------------------------------------------------------------
# The rule file: missing / invalid never crashes the heartbeat
# ---------------------------------------------------------------------------


def test_missing_file_means_no_rules(tmp_path, caplog: pytest.LogCaptureFixture) -> None:
    rules = KnownFalsePositiveRules(str(tmp_path / "nope.json"), today=lambda: TODAY)
    store = _store(rules)
    with caplog.at_level(logging.WARNING, logger="nous.heartbeat.known_fp"):
        assert store.ingest(_finding()) == FindingAction.TRIAGE
        store.ingest(_finding("x"))
    assert sum("not found" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.parametrize(
    "content",
    ["{not json", "[]", '{"rules": "nope"}', '"just a string"', ""],
)
def test_invalid_file_does_not_crash(tmp_path, content: str, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "fp.json"
    _write(path, content)
    store = _store(KnownFalsePositiveRules(str(path), today=lambda: TODAY))
    with caplog.at_level(logging.WARNING, logger="nous.heartbeat.known_fp"):
        assert store.ingest(_finding()) == FindingAction.TRIAGE
    assert any("invalid" in r.getMessage() for r in caplog.records)


def test_invalid_rules_are_skipped_and_valid_ones_load(tmp_path) -> None:
    path = tmp_path / "fp.json"
    _write(
        path,
        {
            "rules": [
                {**SNN_RULE, "id": "no-review", "review_by": None},
                {**SNN_RULE, "id": "bad-regex", "match": {"regex": "("}},
                {**SNN_RULE, "id": "empty-match", "match": {}},
                {**SNN_RULE, "id": "bad-date", "review_by": "next week"},
                "not an object",
                SNN_RULE,
                {**SNN_RULE},  # duplicate id
            ]
        },
    )
    rules = KnownFalsePositiveRules(str(path), today=lambda: TODAY)
    assert [r.id for r in rules.rules] == ["snn-integration-deferral"]


def test_file_is_reloaded_on_change_and_bad_edit_keeps_last_good(tmp_path) -> None:
    path = tmp_path / "fp.json"
    _write(path, {"rules": []})
    store = _store(KnownFalsePositiveRules(str(path), today=lambda: TODAY))
    assert store.ingest(_finding("first SNN integration deferral")) == FindingAction.TRIAGE

    _write(path, {"rules": [SNN_RULE]})
    assert store.ingest(_finding("second SNN integration deferral")) == FindingAction.AUTO_CLOSE

    # A half-written file must not reopen everything the rules closed.
    _write(path, '{"rules": [')
    assert store.ingest(_finding("third SNN integration deferral")) == FindingAction.AUTO_CLOSE


def test_matching_errors_are_contained() -> None:
    class _Broken(KnownFalsePositiveRules):
        def _maybe_reload(self) -> None:
            raise RuntimeError("boom")

    store = _store(_Broken(rules=[parse_rule(SNN_RULE)]))
    assert store.ingest(_finding()) == FindingAction.TRIAGE


def test_example_file_parses() -> None:
    here = os.path.dirname(__file__)
    path = os.path.join(here, "..", "docs", "examples", "known_false_positives.example.json")
    rules = KnownFalsePositiveRules(path, today=lambda: date(2026, 1, 1))
    assert len(rules.rules) == 4


# ---------------------------------------------------------------------------
# Runner: an auto-closed finding never reaches triage, Telegram or ack
# ---------------------------------------------------------------------------


async def test_runner_never_triages_an_auto_closed_finding() -> None:
    from unittest.mock import MagicMock

    from nous.heartbeat.registry import CheckRegistry
    from nous.heartbeat.runner import HeartbeatRunner

    settings = MagicMock()
    settings.heartbeat_daily_token_budget = 50_000
    store = _store(_rules(SNN_RULE))
    runner = HeartbeatRunner(
        settings=settings,
        registry=CheckRegistry(),
        runner=AsyncMock(),
        brain=AsyncMock(),
        heart=MagicMock(),
        bus=None,
        http_client=AsyncMock(),
        finding_store=store,
    )
    runner._send_telegram = AsyncMock()
    runner._cognitive_triage = AsyncMock()
    runner._tokens_used_today = 0

    await runner._triage([_finding(needs_action=True), _finding("disk full", needs_action=True)])

    triaged = runner._cognitive_triage.call_args[0][0]
    assert [f.summary for f in triaged] == ["disk full"]
    closed = store.get_tracked(_finding().fingerprint())
    assert closed.state == FindingState.AUTO_CLOSED_KNOWN_FP  # not acknowledged
    runner._send_telegram.assert_not_called()


# ---------------------------------------------------------------------------
# Companion: auto-closed items never render on a heartbeat_findings card
# ---------------------------------------------------------------------------


class _Service:
    def __init__(self) -> None:
        self.pushed: list[Any] = []

    async def push_built(self, built: Any, *, pre_broadcast: Any = None, **kwargs: Any) -> str:
        if pre_broadcast is not None:
            pre_broadcast()
        self.pushed.append(built)
        return "surface-1"


class _Runner:
    def __init__(self, store: FindingStore) -> None:
        self.finding_store = store


async def test_push_surface_drops_auto_closed_items_but_records_them() -> None:
    store = _store(_rules(SNN_RULE))
    dispatcher = ToolDispatcher()
    service = _Service()
    register_a2ui_tools(dispatcher, service, heartbeat_runner=_Runner(store))

    content, is_error = await dispatcher.dispatch(
        "push_surface",
        {
            "template": "heartbeat_findings",
            "params": {"findings": [{"message": "SNN integration deferral again"}, {"message": "disk full"}]},
        },
    )

    assert not is_error, content
    payload = json.loads(content)
    assert [s["state"] for s in payload["suppressed"]] == ["auto_closed_known_fp"]
    rendered = set((service.pushed[-1].data_model or {}).get("findings", {}))
    assert len(rendered) == 1
    assert store.stats()["by_state"] == {"auto_closed_known_fp": 1, "new": 1}
    assert store.stats()["auto_closed_by_rule"] == {"snn-integration-deferral": 1}


async def test_push_surface_with_only_auto_closed_items_pushes_nothing() -> None:
    store = _store(_rules(SNN_RULE))
    dispatcher = ToolDispatcher()
    service = _Service()
    register_a2ui_tools(dispatcher, service, heartbeat_runner=_Runner(store))

    content, is_error = await dispatcher.dispatch(
        "push_surface",
        {"template": "heartbeat_findings", "params": {"findings": [{"message": "SNN integration deferral"}]}},
    )

    assert not is_error, content
    assert json.loads(content)["pushed"] is False
    assert service.pushed == []
    assert store.stats()["auto_closed_by_rule"] == {"snn-integration-deferral": 1}
