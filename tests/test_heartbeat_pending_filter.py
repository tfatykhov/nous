"""F116 PR 2: the self-initiated check's filters on their own (nous/heartbeat/pending_filter.py).

Transport type, the pure rules (no I/O, an injected clock), the filter with its batched read replaced, and the batched
read against real rows. The check with the filters wired in is tested in tests/test_heartbeat_stale_pending.py.
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nous.heart.schemas import EpisodeSummary, FactSummary
from nous.heartbeat import pending_filter
from nous.heartbeat.pending_filter import (
    KEEP,
    Candidate,
    PendingFilter,
    PendingFilterConfig,
    Verdict,
    episode_candidate,
    evaluate,
    fact_candidate,
    parse_mode,
)
from nous.heartbeat.schemas import CheckResult, Finding, SuppressionReport
from nous.storage.models import Fact

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
MODES = ("off", "shadow", "on")
LAYERS = ("kind", "age", "supersede")

# Texts quoted on the F116 page, section 3.
TEXT_78CCACAC = "The gate check (subtask 8f2bfae0) found the gate met and stopped, waiting for the user's approval"
TEXT_8F248BC2 = "requester is re-reviewing the heads of #800 … and #801"
TEXT_4ECBEEEA = "verdict of the v3 re-review is READY FOR A REAL RUN, PENDING TIM'S DECISIONS"
TEXT_A02B0102 = "Reviewing is the work currently in progress."
TEXT_55DFA56B = 'Heartbeat triage Nov 2 2026 (LOCAL-ONLY): "Pending action: Revie'


def _cfg(kind: str = "off", age: str = "off", supersede: str = "off", **hours: float) -> PendingFilterConfig:
    return PendingFilterConfig(kind=kind, age=age, supersede=supersede, **hours)


ALL_ON = _cfg("on", "on", "on")


def _fact(
    text: str,
    *,
    age_days: float | None = 1,
    source: str | None = "user_direct",
    category: str | None = None,
    tags: frozenset[str] = frozenset(),
    actionable_confidence: float | None = 0.85,
    event_date: date | None = None,
    superseded_by: str | None = None,
    active: bool | None = True,
    rows_known: bool = True,
    last_confirmed: datetime | None = None,
    newer_same_key: bool | None = False,
) -> Candidate:
    """A fact candidate as the filter sees it after a successful batched read (``rows_known``)."""
    return Candidate(
        kind="fact",
        id=str(uuid.uuid4()),
        text=text,
        summary=f"Pending action: {text[:100]}",
        source_label="facts",
        category=category,
        source=source,
        tags=tags,
        actionable_confidence=actionable_confidence,
        learned_at=None if age_days is None else NOW - timedelta(days=age_days),
        event_date=event_date,
        active=active,
        superseded_by=superseded_by,
        rows_known=rows_known,
        last_confirmed=last_confirmed,
        newer_same_key=newer_same_key,
    )


def _episode(text: str, *, age_days: float, outcome: str | None = "ongoing") -> Candidate:
    return Candidate(
        kind="episode",
        id=str(uuid.uuid4()),
        text=text,
        summary=f"Unresolved commitment: {text[:100]}",
        source_label="episodes",
        outcome=outcome,
        started_at=NOW - timedelta(days=age_days),
    )


def _add(report: SuppressionReport, n: int, *, shadow: bool, reason: str = "kind_rule") -> None:
    for i in range(n):
        report.add(reason, shadow=shadow, fingerprint=f"{i:016x}", kind="fact", item_id=f"id-{i}")


class TestSuppressionReport:
    def test_a_new_report_is_empty_and_false(self):
        report = SuppressionReport()
        assert not report
        assert (report.suppressed, report.shadow_suppressed, report.items, report.shadow_items, report.errors) == (
            {},
            {},
            [],
            [],
            0,
        )

    def test_add_counts_by_reason_and_keeps_the_item(self):
        report = SuppressionReport()
        report.add("age_fact", shadow=False, fingerprint="f" * 16, kind="fact", item_id="x")
        report.add("age_fact", shadow=False, fingerprint="e" * 16, kind="fact", item_id="y")
        report.add("kind_rule", shadow=True, fingerprint="d" * 16, kind="fact", item_id="z")

        assert report
        assert report.suppressed == {"age_fact": 2}
        assert report.shadow_suppressed == {"kind_rule": 1}
        assert report.items == [
            {"fingerprint": "f" * 16, "kind": "fact", "id": "x", "reason": "age_fact"},
            {"fingerprint": "e" * 16, "kind": "fact", "id": "y", "reason": "age_fact"},
        ]
        assert report.shadow_items == [{"fingerprint": "d" * 16, "kind": "fact", "id": "z", "reason": "kind_rule"}]

    def test_each_list_keeps_its_first_twenty_and_the_counts_stay_complete(self):
        report = SuppressionReport()
        _add(report, 30, shadow=False)
        _add(report, 25, shadow=True, reason="age_snapshot")

        assert report.suppressed == {"kind_rule": 30}
        assert report.shadow_suppressed == {"age_snapshot": 25}
        assert len(report.items) == 20 and len(report.shadow_items) == 20
        assert [i["id"] for i in report.items] == [f"id-{i}" for i in range(20)]

    def test_errors_alone_make_the_report_true(self):
        report = SuppressionReport()
        report.errors = 1
        assert report

    def test_merge_sums_counts_and_errors_and_caps_the_lists(self):
        tick = SuppressionReport()
        _add(tick, 15, shadow=False)
        tick.errors = 1
        run = SuppressionReport()
        _add(run, 10, shadow=False, reason="age_fact")
        _add(run, 3, shadow=True)
        run.errors = 2

        tick.merge(run)

        assert tick.suppressed == {"kind_rule": 15, "age_fact": 10}
        assert tick.shadow_suppressed == {"kind_rule": 3}
        assert tick.errors == 3
        assert len(tick.items) == 20
        assert [i["reason"] for i in tick.items[15:]] == ["age_fact"] * 5
        assert len(tick.shadow_items) == 3

    def test_merge_does_not_change_the_other_report(self):
        tick, run = SuppressionReport(), SuppressionReport()
        _add(run, 2, shadow=True)
        tick.merge(run)
        tick.add("age_fact", shadow=True, fingerprint="0" * 16, kind="fact", item_id="q")
        assert run.shadow_suppressed == {"kind_rule": 2}
        assert len(run.shadow_items) == 2


class TestCheckResult:
    def test_a_check_result_carries_no_report_by_default(self):
        assert CheckResult().suppression is None
        assert CheckResult(has_updates=True, findings=[]).suppression is None


# ---------------------------------------------------------------------------
# The pure part: modes, configuration, candidates and evaluate()
# ---------------------------------------------------------------------------


class TestParseMode:
    @pytest.mark.parametrize(
        ("value", "mode"),
        [
            ("on", "on"),
            ("off", "off"),
            ("shadow", "shadow"),
            (" Shadow ", "shadow"),
            ("ON", "on"),
            ("", "off"),
            ("   ", "off"),
            ("banana", "off"),
            ("enabled", "off"),
            (None, "off"),
            (True, "off"),
            (1, "off"),
        ],
    )
    def test_parse_mode(self, value, mode):
        assert parse_mode(value) == mode

    def test_a_mock_attribute_is_off(self):
        assert parse_mode(MagicMock()) == "off"


class TestConfig:
    def test_a_mock_settings_object_gives_every_mode_off_and_the_default_hours(self):
        cfg = PendingFilterConfig.from_settings(MagicMock())
        assert (cfg.kind, cfg.age, cfg.supersede) == ("off", "off", "off")
        assert (cfg.snapshot_ttl_hours, cfg.fact_max_age_hours, cfg.episode_max_age_hours) == (336.0, 720.0, 720.0)

    def test_the_settings_are_read(self):
        settings = SimpleNamespace(
            heartbeat_pending_kind_mode="on",
            heartbeat_pending_age_mode=" shadow",
            heartbeat_pending_supersede_mode="nonsense",
            heartbeat_pending_snapshot_ttl_hours=48,
            heartbeat_pending_fact_max_age_hours=100,
            heartbeat_pending_episode_max_age_hours=200,
        )
        cfg = PendingFilterConfig.from_settings(settings)
        assert (cfg.kind, cfg.age, cfg.supersede) == ("on", "shadow", "off")
        assert (cfg.snapshot_ttl_hours, cfg.fact_max_age_hours, cfg.episode_max_age_hours) == (48.0, 100.0, 200.0)

    @pytest.mark.parametrize("bad", [0, -5, True, "336", None])
    def test_an_hour_value_that_is_not_a_positive_number_gives_the_default(self, bad):
        cfg = PendingFilterConfig.from_settings(SimpleNamespace(heartbeat_pending_snapshot_ttl_hours=bad))
        assert cfg.snapshot_ttl_hours == 336.0

    def test_an_object_without_the_settings_gives_every_mode_off(self):
        assert PendingFilterConfig.from_settings(object()) == PendingFilterConfig()


class TestKindLayer:
    @pytest.mark.parametrize("category", ["rule", "preference", "person", "Rule"])
    def test_rule_preference_and_person_facts(self, category):
        c = _fact("synthetic: always run the linter before a commit", category=category, actionable_confidence=0.9)
        assert evaluate(c, _cfg(kind="on"), NOW) == Verdict("drop", "kind_rule")

    @pytest.mark.parametrize(
        "text",
        [
            TEXT_55DFA56B,
            TEXT_55DFA56B.lower(),
            '  heartbeat TRIAGE Nov 2 2026: "Pending action: x"',
            "Heartbeat triage November 12: the backup line is done",
            "Heartbeat triage 2026-11-02: nothing left to do",
            'Heartbeat triage: "Pending action: send the invoice" is done',
            "Heartbeat triage: “Pending action: send the invoice” is done",
            "HEARTBEAT FALSE-POSITIVE: the deploy reminder is done",
            "HEARTBEAT FALSE-POSITIVE (fact 07a88e08) do not re-flag",
            "HEARTBEAT SUPPRESSION: do not raise the backup line",
        ],
    )
    def test_triage_and_suppression_notes(self, text):
        assert evaluate(_fact(text), _cfg(kind="on"), NOW) == Verdict("drop", "kind_note")

    @pytest.mark.parametrize(
        "text",
        [
            "Heartbeat triage: ask the owner which findings to keep",
            "Heartbeat triage should run less often at night",
            "Heartbeat triage nightly summary needs a chart",
            "Heartbeat suppression TTL should be raised to 48 hours",
            "heartbeat suppression: lower the TTL",
            "Heartbeat false-positive rate needs a dashboard",
            "Heartbeat FALSE-POSITIVE count is wrong on the dashboard",
        ],
    )
    def test_a_task_that_starts_with_the_same_words_keeps(self, text):
        assert evaluate(_fact(text), _cfg(kind="on"), NOW) == KEEP

    def test_a_note_by_tag(self):
        c = _fact("synthetic: the upload test is known to be flaky", tags=frozenset({"hb-note"}))
        assert evaluate(c, _cfg(kind="on"), NOW) == Verdict("drop", "kind_note")

    def test_a_task_keeps(self):
        assert evaluate(_fact("TODO: send the invoice", category="technical"), _cfg(kind="on"), NOW) == KEEP

    def test_a_triage_mention_mid_sentence_keeps(self):
        c = _fact("Write up the Heartbeat triage notes for the owner")
        assert evaluate(c, _cfg(kind="on"), NOW) == KEEP

    def test_kind_does_not_apply_to_episodes(self):
        c = _episode("HEARTBEAT FALSE-POSITIVE: nothing to do", age_days=1)
        assert evaluate(c, _cfg(kind="on"), NOW) == KEEP


class TestAgeLayerFacts:
    @pytest.mark.parametrize(("text", "days"), [(TEXT_4ECBEEEA, 21), (TEXT_A02B0102, 26)])
    def test_status_lines_hide_after_the_snapshot_ttl(self, text, days):
        c = _fact(text, age_days=days, source="enumerative_extractor")
        assert evaluate(c, _cfg(age="on"), NOW) == Verdict("drop", "age_snapshot")
        assert evaluate(c, _cfg(age="on", snapshot_ttl_hours=720.0), NOW) == KEEP

    def test_78ccacac_at_44_days(self):
        c = _fact(TEXT_78CCACAC, age_days=44, source="enumerative_extractor")
        assert evaluate(c, _cfg(age="on"), NOW) == Verdict("drop", "age_snapshot")

    @pytest.mark.parametrize(("offset", "hidden"), [(1, False), (0, False), (-1, True)])
    def test_event_date(self, offset, hidden):
        event = NOW.date() + timedelta(days=offset)
        c = _fact(TEXT_78CCACAC, age_days=44, source="enumerative_extractor", event_date=event)
        expected = Verdict("drop", "age_snapshot") if hidden else KEEP
        assert evaluate(c, _cfg(age="on"), NOW) == expected

    def test_event_date_is_compared_with_the_utc_date(self):
        # 01:00 UTC on Nov 23 is still Nov 22 west of UTC: the UTC date decides, so an event on Nov 22 is past.
        now = datetime(2026, 11, 23, 1, 0, tzinfo=UTC)
        west = now.astimezone(timezone(timedelta(hours=-5)))
        c = _fact(TEXT_78CCACAC, age_days=44, source="enumerative_extractor", event_date=date(2026, 11, 22))
        assert evaluate(c, _cfg(age="on"), west) == Verdict("drop", "age_snapshot")

    @pytest.mark.parametrize(("confidence", "hidden"), [(0.95, False), (1.0, False), (0.9, True), (None, True)])
    def test_a_confident_verdict_is_exempt(self, confidence, hidden):
        c = _fact(TEXT_4ECBEEEA, age_days=21, source="enumerative_extractor", actionable_confidence=confidence)
        assert (evaluate(c, _cfg(age="on"), NOW) != KEEP) is hidden

    @pytest.mark.parametrize(("days", "hidden"), [(40, True), (31, True), (29, False), (5, False)])
    def test_other_facts_use_the_fact_cap(self, days, hidden):
        c = _fact("synthetic: send the invoice to the accountant", age_days=days, source="user_direct")
        assert evaluate(c, _cfg(age="on"), NOW) == (Verdict("drop", "age_fact") if hidden else KEEP)

    def test_a_fact_without_a_source_uses_the_fact_cap(self):
        assert evaluate(_fact("x", age_days=20, source=None), _cfg(age="on"), NOW) == KEEP
        assert evaluate(_fact("x", age_days=40, source=None), _cfg(age="on"), NOW) == Verdict("drop", "age_fact")

    def test_a_recent_confirmation_keeps_an_old_fact(self):
        yesterday = NOW - timedelta(days=1)
        assert evaluate(_fact("x", age_days=40, last_confirmed=yesterday), _cfg(age="on"), NOW) == KEEP

    def test_without_the_row_an_old_fact_keeps(self):
        c = _fact("x", age_days=40, rows_known=False, last_confirmed=None)
        assert evaluate(c, _cfg(age="on"), NOW) == KEEP

    def test_without_the_row_a_confirmed_old_fact_keeps(self):
        c = _fact("x", age_days=40, rows_known=False, last_confirmed=NOW - timedelta(days=1))
        assert evaluate(c, _cfg(age="on"), NOW) == KEEP

    def test_with_the_row_and_no_confirmation_an_old_fact_hides(self):
        c = _fact("x", age_days=40, rows_known=True, last_confirmed=None)
        assert evaluate(c, _cfg(age="on"), NOW) == Verdict("drop", "age_fact")

    def test_an_old_confirmation_does_not_keep_it(self):
        c = _fact("x", age_days=40, last_confirmed=NOW - timedelta(days=35))
        assert evaluate(c, _cfg(age="on"), NOW) == Verdict("drop", "age_fact")

    def test_no_learned_at_keeps(self):
        assert evaluate(_fact("x", age_days=None), _cfg(age="on"), NOW) == KEEP

    def test_a_learned_at_in_the_future_keeps(self):
        assert evaluate(_fact("x", age_days=-1), _cfg(age="on"), NOW) == KEEP

    def test_a_naive_learned_at_is_read_as_utc(self):
        fact = FactSummary(
            id=uuid.uuid4(),
            content="synthetic: send the invoice",
            category=None,
            subject=None,
            confidence=1.0,
            active=True,
            learned_at=(NOW - timedelta(days=30, hours=1)).replace(tzinfo=None),
            source="user_direct",
        )
        c = fact_candidate(fact)
        assert c.learned_at == NOW - timedelta(days=30, hours=1)
        c.rows_known = True
        assert evaluate(c, _cfg(age="on"), NOW) == Verdict("drop", "age_fact")

    @pytest.mark.parametrize(
        ("text", "hidden"),
        [
            ("Who owns the deploy?", True),
            ("Okay, who owns the deploy?", True),
            ("ok so is the deploy done ?  ", True),
            ("What remains: send the invoice", False),
            ("Who owns the deploy", False),
        ],
    )
    def test_question_form_is_a_trailing_question_mark(self, text, hidden):
        assert evaluate(_fact(text), _cfg(age="on"), NOW) == (Verdict("drop", "question_form") if hidden else KEEP)

    @pytest.mark.parametrize("offset", [1, 0])
    def test_a_question_with_an_event_date_today_or_later_keeps(self, offset):
        c = _fact("Who brings the cake to the party?", event_date=NOW.date() + timedelta(days=offset))
        assert evaluate(c, _cfg(age="on"), NOW) == KEEP

    def test_a_question_with_a_past_event_date_hides(self):
        c = _fact("Who brings the cake to the party?", event_date=NOW.date() - timedelta(days=1))
        assert evaluate(c, _cfg(age="on"), NOW) == Verdict("drop", "question_form")

    @pytest.mark.parametrize(("confidence", "hidden"), [(0.97, False), (0.95, False), (0.9, True)])
    def test_a_confident_question_keeps(self, confidence, hidden):
        c = _fact("Can you send the invoice to the accountant?", actionable_confidence=confidence)
        assert evaluate(c, _cfg(age="on"), NOW) == (Verdict("drop", "question_form") if hidden else KEEP)

    def test_a_question_needs_no_age(self):
        assert evaluate(_fact("Who owns the deploy?", age_days=None), _cfg(age="on"), NOW) == Verdict(
            "drop", "question_form"
        )


class TestAgeLayerEpisodes:
    def test_an_ongoing_episode_past_the_cap(self):
        c = _episode("I'll look into the flaky upload test", age_days=31)
        assert evaluate(c, _cfg(age="on"), NOW) == Verdict("drop", "age_ongoing")

    def test_a_recent_ongoing_episode_keeps(self):
        assert evaluate(_episode("I'll draft the notes", age_days=3), _cfg(age="on"), NOW) == KEEP

    def test_the_episode_cap_is_its_own_setting(self):
        c = _episode("I'll draft the notes", age_days=5)
        assert evaluate(c, _cfg(age="on", episode_max_age_hours=96.0), NOW) == Verdict("drop", "age_ongoing")

    @pytest.mark.parametrize("outcome", [None, "partial", "success"])
    def test_other_outcomes_are_untouched(self, outcome):
        assert evaluate(_episode("I'll draft the notes", age_days=31, outcome=outcome), _cfg(age="on"), NOW) == KEEP

    def test_an_episode_question_is_left_to_the_check(self):
        assert evaluate(_episode("Who owns the deploy?", age_days=1), _cfg(age="on"), NOW) == KEEP


class TestSupersedeLayer:
    def test_superseded(self):
        c = _fact(TEXT_8F248BC2, superseded_by=str(uuid.uuid4()))
        assert evaluate(c, _cfg(supersede="on"), NOW) == Verdict("drop", "superseded")

    def test_inactive(self):
        assert evaluate(_fact("x", active=False), _cfg(supersede="on"), NOW) == Verdict("drop", "inactive")

    @pytest.mark.parametrize(("mode", "decision"), [("on", "shadow_drop"), ("shadow", "shadow_drop"), ("off", "keep")])
    def test_a_newer_fact_with_the_same_key_is_only_recorded(self, mode, decision):
        result = evaluate(_fact("x", newer_same_key=True), _cfg(supersede=mode), NOW)
        assert result.decision == decision
        assert result.reason == (None if decision == "keep" else "newer_same_key")

    def test_the_same_key_rule_needs_the_row(self):
        assert evaluate(_fact("x", newer_same_key=True, rows_known=False), _cfg(supersede="on"), NOW) == KEEP

    def test_superseded_comes_before_the_same_key_rule(self):
        c = _fact("x", superseded_by=str(uuid.uuid4()), newer_same_key=True)
        assert evaluate(c, _cfg(supersede="on"), NOW) == Verdict("drop", "superseded")

    def test_supersede_does_not_apply_to_episodes(self):
        c = _episode("I'll draft the notes", age_days=1)
        c.active = False
        assert evaluate(c, _cfg(supersede="on"), NOW) == KEEP


# Fixtures for the mode matrix, each with the layers that hit it when that layer alone is on.
MATRIX = {
    "78ccacac": (_fact(TEXT_78CCACAC, age_days=44, source="enumerative_extractor"), {"age"}),
    "synthetic_rule_old": (_fact("synthetic: reply in plain words", age_days=50, category="rule"), {"kind", "age"}),
    "8f248bc2": (
        _fact(TEXT_8F248BC2, age_days=30, source="enumerative_extractor", superseded_by=str(uuid.uuid4())),
        {"age", "supersede"},
    ),
    "55dfa56b": (_fact(TEXT_55DFA56B, age_days=20), {"kind"}),
    "synthetic_ongoing_31d": (_episode("I'll look into the flaky upload test", age_days=31), {"age"}),
    "synthetic_rule_superseded_old": (
        _fact("synthetic: the backup runs at noon", age_days=40, category="rule", superseded_by=str(uuid.uuid4())),
        {"kind", "age", "supersede"},
    ),
}
REASON_ALONE = {
    ("78ccacac", "age"): "age_snapshot",
    ("synthetic_rule_old", "kind"): "kind_rule",
    ("synthetic_rule_old", "age"): "age_fact",
    ("8f248bc2", "age"): "age_snapshot",
    ("8f248bc2", "supersede"): "superseded",
    ("55dfa56b", "kind"): "kind_note",
    ("synthetic_ongoing_31d", "age"): "age_ongoing",
    ("synthetic_rule_superseded_old", "kind"): "kind_rule",
    ("synthetic_rule_superseded_old", "age"): "age_fact",
    ("synthetic_rule_superseded_old", "supersede"): "superseded",
}
COMBINATIONS = list(itertools.product(MODES, repeat=3))

# Spec 7.6: items that must surface under every combination.
CONTROLS = {
    "synthetic_waiting_on_merge": _fact(
        "waiting on Tim to merge #957", age_days=1, source="enumerative_extractor", category="technical"
    ),
    "synthetic_fresh_task": _fact("synthetic: send the invoice to the accountant", age_days=2),
    "synthetic_old_with_future_date": _fact(
        "synthetic: renew the domain", age_days=60, event_date=NOW.date() + timedelta(days=5)
    ),
    "synthetic_ongoing_3d": _episode("I'll draft the release notes", age_days=3),
}


def _combo_id(modes: tuple[str, ...]) -> str:
    return "-".join(modes)


class TestModeMatrix:
    @pytest.mark.parametrize(("name", "layer"), sorted(REASON_ALONE))
    def test_each_hitting_layer_alone(self, name, layer):
        c, layers = MATRIX[name]
        assert layer in layers
        assert evaluate(c, _cfg(**{layer: "on"}), NOW) == Verdict("drop", REASON_ALONE[(name, layer)])
        assert evaluate(c, _cfg(**{layer: "shadow"}), NOW) == Verdict("shadow_drop", REASON_ALONE[(name, layer)])

    @pytest.mark.parametrize("name", sorted(MATRIX))
    def test_a_layer_that_does_not_hit_keeps(self, name):
        c, layers = MATRIX[name]
        for layer in set(LAYERS) - layers:
            assert evaluate(c, _cfg(**{layer: "on"}), NOW) == KEEP

    @pytest.mark.parametrize("modes", COMBINATIONS, ids=_combo_id)
    @pytest.mark.parametrize("name", sorted(MATRIX))
    def test_every_combination(self, name, modes):
        c, layers = MATRIX[name]
        verdict = evaluate(c, _cfg(*modes), NOW)
        mode_of = dict(zip(LAYERS, modes, strict=True))
        on = [layer for layer in LAYERS if layer in layers and mode_of[layer] == "on"]
        shadow = [layer for layer in LAYERS if layer in layers and mode_of[layer] == "shadow"]
        if on:
            assert verdict == Verdict("drop", REASON_ALONE[(name, on[0])])
        elif shadow:
            assert verdict == Verdict("shadow_drop", REASON_ALONE[(name, shadow[0])])
        else:
            assert verdict == KEEP

    def test_kind_in_shadow_and_age_on_hides_by_age(self):
        c, _ = MATRIX["synthetic_rule_old"]
        assert evaluate(c, _cfg(kind="shadow", age="on"), NOW) == Verdict("drop", "age_fact")

    @pytest.mark.parametrize("modes", COMBINATIONS, ids=_combo_id)
    @pytest.mark.parametrize("name", sorted(CONTROLS))
    @pytest.mark.parametrize("rows_known", [False, True])
    def test_the_controls_keep_under_every_combination(self, name, modes, rows_known):
        c = CONTROLS[name]
        if c.kind == "fact":
            c = dataclasses.replace(c, rows_known=rows_known, newer_same_key=False)
        assert evaluate(c, _cfg(*modes), NOW) == KEEP


class TestBuilders:
    def test_a_fact_summary(self):
        newer = uuid.uuid4()
        fact = FactSummary(
            id=uuid.UUID("78ccacac-0000-4000-8000-000000000001"),
            content=TEXT_78CCACAC,
            category="Technical",
            subject=None,
            confidence=1.0,
            active=True,
            superseded_by=newer,
            actionable=True,
            actionable_confidence=0.8,
            tags=["HB-Note", "x"],
            event_date=date(2026, 12, 1),
            learned_at=NOW,
            source="enumerative_extractor",
        )
        c = fact_candidate(fact)
        assert (c.kind, c.id, c.text, c.source_label) == ("fact", str(fact.id), TEXT_78CCACAC, "facts")
        assert c.summary == f"Pending action: {TEXT_78CCACAC[:100]}"
        assert (c.category, c.source, c.tags) == ("Technical", "enumerative_extractor", frozenset({"hb-note", "x"}))
        assert (c.actionable_confidence, c.learned_at, c.event_date) == (0.8, NOW, date(2026, 12, 1))
        assert (c.active, c.superseded_by) == (True, str(newer))
        assert (c.rows_known, c.last_confirmed, c.newer_same_key) == (False, None, None)

    def test_a_long_fact_is_cut_in_the_summary_only(self):
        text = "x" * 150
        fact = FactSummary(id=uuid.uuid4(), content=text, category=None, subject=None, confidence=1.0, active=True)
        c = fact_candidate(fact)
        assert c.text == text and c.summary == "Pending action: " + "x" * 100

    def test_an_episode_summary(self):
        ep = EpisodeSummary(
            id=uuid.uuid4(), title="t", summary="I'll draft the notes", outcome="ongoing", started_at=NOW, tags=[]
        )
        c = episode_candidate(ep)
        assert (c.kind, c.id, c.source_label, c.outcome, c.started_at) == (
            "episode",
            str(ep.id),
            "episodes",
            "ongoing",
            NOW,
        )
        assert c.summary == "Unresolved commitment: I'll draft the notes"

    def test_an_episode_without_a_summary_uses_its_title(self):
        ep = EpisodeSummary(id=uuid.uuid4(), title="the title", summary="", outcome=None, started_at=NOW, tags=[])
        assert episode_candidate(ep).summary == "Unresolved commitment: the title"

    def test_the_mock_fact_of_the_older_tests(self):
        fact = MagicMock()
        fact.id = "fact-1"
        fact.content = "TODO: follow-up on this pending action"
        fact.score = 0.8
        fact.actionable = True
        fact.actionable_confidence = 0.85
        fact.category = None
        fact.tags = None
        c = fact_candidate(fact)
        assert (c.id, c.text, c.category, c.tags, c.actionable_confidence) == (
            "fact-1",
            "TODO: follow-up on this pending action",
            None,
            frozenset(),
            0.85,
        )
        assert (c.source, c.learned_at, c.event_date, c.active, c.superseded_by) == (None, None, None, None, None)
        assert evaluate(c, ALL_ON, NOW) == KEEP

    def test_a_bare_mock_never_raises(self):
        c = fact_candidate(MagicMock())
        assert c.kind == "fact" and c.text == "" and c.learned_at is None
        e = episode_candidate(MagicMock())
        assert e.kind == "episode" and e.outcome is None and e.started_at is None
        assert evaluate(c, ALL_ON, NOW) == KEEP and evaluate(e, ALL_ON, NOW) == KEEP


# ---------------------------------------------------------------------------
# PendingFilter and its ledger, with the batched read replaced
# ---------------------------------------------------------------------------


def _summary_fact(
    text: str,
    *,
    age_days: float,
    source: str = "enumerative_extractor",
    category: str | None = None,
    fid: uuid.UUID | None = None,
) -> FactSummary:
    return FactSummary(
        id=fid or uuid.uuid4(),
        content=text,
        category=category,
        subject=None,
        confidence=1.0,
        active=True,
        actionable=True,
        actionable_confidence=0.85,
        learned_at=datetime.now(UTC) - timedelta(days=age_days),
        source=source,
    )


class _Rows:
    """A stand-in for the batched read: records each call, answers every id with no confirmation."""

    def __init__(self, *, newer_same_key: set[str] = frozenset(), fail: Exception | None = None, sleep: float = 0):
        self.calls: list[list[str]] = []
        self._newer = newer_same_key
        self._fail = fail
        self._sleep = sleep

    async def __call__(self, ids: list[str]) -> dict[str, tuple[datetime | None, bool]]:
        self.calls.append(list(ids))
        if self._sleep:
            await asyncio.sleep(self._sleep)
        if self._fail is not None:
            raise self._fail
        return {i: (None, i in self._newer) for i in ids}


def _logged(caplog, level: int) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "nous.heartbeat.pending_filter" and r.levelno == level]


def _filter(rows: _Rows | None = None, **modes: str) -> PendingFilter:
    return PendingFilter(_cfg(**modes), fetch_rows=rows)


class TestPendingFilterSwitches:
    @pytest.mark.parametrize(
        ("modes", "enabled", "needs_rows"),
        [
            ({}, False, False),
            ({"kind": "shadow"}, True, False),
            ({"kind": "on"}, True, False),
            ({"age": "shadow"}, True, True),
            ({"supersede": "on"}, True, True),
            ({"kind": "on", "age": "on", "supersede": "on"}, True, True),
        ],
    )
    def test_enabled_and_needs_rows(self, modes, enabled, needs_rows):
        f = _filter(**modes)
        assert (f.enabled, f.needs_rows) == (enabled, needs_rows)


class TestPrefetch:
    async def test_one_call_per_list_and_each_id_once(self):
        rows = _Rows()
        f = _filter(rows, age="shadow")
        ledger = f.new_ledger()
        a, b, c = (_summary_fact("x", age_days=1) for _ in range(3))

        await f.prefetch([a, b], ledger)
        await f.prefetch([b, c], ledger)
        await f.prefetch([a, c], ledger)

        assert rows.calls == [[str(a.id), str(b.id)], [str(c.id)]]
        assert set(ledger.rows) == {str(a.id), str(b.id), str(c.id)}

    async def test_ids_that_are_not_uuids_are_not_read(self):
        rows = _Rows()
        f = _filter(rows, supersede="shadow")
        ledger = f.new_ledger()
        mock = MagicMock()
        mock.id = "fact-1"

        await f.prefetch([mock], ledger)
        assert rows.calls == []

    async def test_no_read_when_age_and_supersede_are_off(self):
        rows = _Rows()
        f = _filter(rows, kind="on")
        await f.prefetch([_summary_fact("x", age_days=40)], f.new_ledger())
        assert rows.calls == []

    async def test_no_read_without_a_database(self):
        f = PendingFilter(_cfg(age="on"))
        ledger = f.new_ledger()
        fact = _summary_fact("synthetic: send the invoice", age_days=40, source="user_direct")
        await f.prefetch([fact], ledger)
        assert ledger.rows == {} and not ledger.read_failed and ledger.report.errors == 0
        assert f.screen_fact(fact, ledger) == KEEP

    @pytest.mark.parametrize("how", ["raises", "times_out"])
    async def test_a_failed_read_keeps_every_item_that_needs_the_row(self, how, monkeypatch, caplog):
        if how == "raises":
            rows = _Rows(fail=RuntimeError("database is down"))
        else:
            monkeypatch.setattr(pending_filter, "READ_BUDGET_SECONDS", 0.05)
            rows = _Rows(sleep=5)
        f = _filter(rows, age="on")
        ledger = f.new_ledger()
        old = _summary_fact("synthetic: send the invoice", age_days=40, source="user_direct")
        later = _summary_fact("synthetic: call the bank", age_days=40, source="user_direct")

        with caplog.at_level(logging.WARNING, logger="nous.heartbeat.pending_filter"):
            await f.prefetch([old], ledger)
            await f.prefetch([later], ledger)

        assert ledger.read_failed
        assert len(rows.calls) == 1, "no read after the first failure in a run"
        assert f.screen_fact(old, ledger) == KEEP
        assert f.screen_fact(later, ledger) == KEEP
        assert ledger.report.errors == 1
        assert not ledger.report.suppressed and not ledger.report.shadow_suppressed
        assert len(_logged(caplog, logging.WARNING)) == 1

    async def test_a_new_ledger_reads_again(self):
        rows = _Rows(fail=RuntimeError("down"))
        f = _filter(rows, age="on")
        fact = _summary_fact("x", age_days=1)
        await f.prefetch([fact], f.new_ledger())
        await f.prefetch([fact], f.new_ledger())
        assert len(rows.calls) == 2

    async def test_a_cancellation_is_not_swallowed(self):
        f = _filter(_Rows(fail=asyncio.CancelledError()), age="on")
        with pytest.raises(asyncio.CancelledError):
            await f.prefetch([_summary_fact("x", age_days=1)], f.new_ledger())


class _Clock:
    """A fake monotonic clock: each read advances it by ``step`` seconds."""

    def __init__(self, step: float) -> None:
        self.now = 100.0
        self.step = step

    def __call__(self) -> float:
        return self.now


class TestReadBudget:
    """All the reads of one run share one budget of READ_BUDGET_SECONDS."""

    def _filter(self, clock: _Clock, monkeypatch) -> tuple[PendingFilter, list[float], list[list[str]]]:
        timeouts: list[float] = []
        calls: list[list[str]] = []

        async def wait_for(awaitable, timeout):
            timeouts.append(timeout)
            return await awaitable

        async def fetch(ids: list[str]):
            calls.append(list(ids))
            clock.now += clock.step
            return {i: (None, False) for i in ids}

        monkeypatch.setattr(pending_filter, "asyncio", SimpleNamespace(wait_for=wait_for))
        return PendingFilter(_cfg(age="on"), fetch_rows=fetch, clock=clock), timeouts, calls

    async def test_each_read_gets_what_is_left_and_none_runs_after_it_is_spent(self, monkeypatch, caplog):
        clock = _Clock(step=0.8)
        f, timeouts, calls = self._filter(clock, monkeypatch)
        ledger = f.new_ledger()
        lists = [[_summary_fact("x", age_days=40, source="user_direct")] for _ in range(4)]

        with caplog.at_level(logging.WARNING, logger="nous.heartbeat.pending_filter"):
            for facts in lists:
                await f.prefetch(facts, ledger)

        assert timeouts == pytest.approx([2.0, 1.2, 0.4])
        assert len(calls) == 3, "no read once the budget is spent"
        assert ledger.read_failed and ledger.report.errors == 1
        assert len(_logged(caplog, logging.WARNING)) == 1
        assert f.screen_fact(lists[0][0], ledger) == Verdict("drop", "age_fact"), "a row read in time is used"
        assert f.screen_fact(lists[3][0], ledger) == KEEP, "a list read after the budget keeps its items"

    async def test_fast_reads_leave_the_budget_almost_whole(self, monkeypatch):
        clock = _Clock(step=0.01)
        f, timeouts, calls = self._filter(clock, monkeypatch)
        ledger = f.new_ledger()
        for _ in range(6):
            await f.prefetch([_summary_fact("x", age_days=1)], ledger)
        assert len(calls) == 6 and not ledger.read_failed
        assert timeouts[-1] == pytest.approx(2.0 - 5 * 0.01)

    async def test_a_list_with_nothing_to_read_uses_no_budget(self, monkeypatch):
        clock = _Clock(step=0.5)
        f, timeouts, calls = self._filter(clock, monkeypatch)
        ledger = f.new_ledger()
        fact = _summary_fact("x", age_days=1)
        await f.prefetch([fact], ledger)
        await f.prefetch([fact], ledger)  # already read: no query
        await f.prefetch([_summary_fact("y", age_days=1)], ledger)
        assert timeouts == pytest.approx([2.0, 1.5])

    async def test_each_run_has_its_own_budget(self, monkeypatch):
        clock = _Clock(step=3.0)
        f, timeouts, calls = self._filter(clock, monkeypatch)
        first, second = f.new_ledger(), f.new_ledger()
        await f.prefetch([_summary_fact("x", age_days=1)], first)
        await f.prefetch([_summary_fact("y", age_days=1)], first)
        await f.prefetch([_summary_fact("z", age_days=1)], second)
        assert len(calls) == 2 and first.read_failed and not second.read_failed
        assert timeouts == pytest.approx([2.0, 2.0])


class TestScreen:
    async def test_a_fact_is_evaluated_and_counted_once(self):
        f = _filter(_Rows(), age="on")
        ledger = f.new_ledger()
        fact = _summary_fact(TEXT_4ECBEEEA, age_days=21)
        await f.prefetch([fact], ledger)

        first = f.screen_fact(fact, ledger)
        second = f.screen_fact(fact, ledger)

        assert first == second == Verdict("drop", "age_snapshot")
        assert ledger.report.suppressed == {"age_snapshot": 1}
        assert len(ledger.report.items) == 1
        assert ledger.age_hidden == [str(fact.id)]

    async def test_the_recorded_fingerprint_is_the_would_be_findings(self):
        f = _filter(_Rows(), age="shadow")
        ledger = f.new_ledger()
        fact = _summary_fact(TEXT_78CCACAC, age_days=44)
        await f.prefetch([fact], ledger)

        assert f.screen_fact(fact, ledger) == Verdict("shadow_drop", "age_snapshot")

        finding = Finding(
            source="facts",
            summary=f"Pending action: {fact.content[:100]}",
            check_name="self_initiated",
        )
        assert ledger.report.shadow_items == [
            {"fingerprint": finding.fingerprint(), "kind": "fact", "id": str(fact.id), "reason": "age_snapshot"}
        ]
        assert ledger.age_hidden == [], "shadow hides nothing"

    def test_an_episode_is_recorded_with_its_would_be_fingerprint(self):
        f = _filter(age="on")
        ledger = f.new_ledger()
        ep = EpisodeSummary(
            id=uuid.uuid4(),
            title="t",
            summary="I'll look into the flaky upload test",
            outcome="ongoing",
            started_at=datetime.now(UTC) - timedelta(days=60),
            tags=[],
        )

        assert f.screen_episode(ep, ledger) == Verdict("drop", "age_ongoing")
        finding = Finding(
            source="episodes", summary=f"Unresolved commitment: {ep.summary}", check_name="self_initiated"
        )
        assert ledger.report.items[0]["fingerprint"] == finding.fingerprint()
        assert ledger.age_hidden == [str(ep.id)]

    def test_a_kept_item_is_not_recorded(self):
        f = _filter(kind="on")
        ledger = f.new_ledger()
        assert f.screen_fact(_summary_fact("TODO: send the invoice", age_days=1), ledger) == KEEP
        assert not ledger.report

    def test_without_the_row_the_newer_same_key_rule_is_silent(self):
        f = _filter(supersede="on")
        ledger = f.new_ledger()
        assert f.screen_fact(_summary_fact("x", age_days=1), ledger) == KEEP

    async def test_the_newer_same_key_rule_records_from_the_row(self):
        fact = _summary_fact("x", age_days=1)
        f = _filter(_Rows(newer_same_key={str(fact.id)}), supersede="on")
        ledger = f.new_ledger()
        await f.prefetch([fact], ledger)
        assert f.screen_fact(fact, ledger) == Verdict("shadow_drop", "newer_same_key")
        assert ledger.report.shadow_suppressed == {"newer_same_key": 1} and not ledger.report.suppressed

    def test_an_error_while_evaluating_keeps_the_item(self, monkeypatch, caplog):
        def boom(*_args, **_kwargs):
            raise RuntimeError("bad rule")

        monkeypatch.setattr(pending_filter, "evaluate", boom)
        f = _filter(kind="on")
        ledger = f.new_ledger()

        with caplog.at_level(logging.ERROR, logger="nous.heartbeat.pending_filter"):
            assert f.screen_fact(_summary_fact("x", age_days=1), ledger) == KEEP
            assert f.screen_fact(_summary_fact("y", age_days=1), ledger) == KEEP
            ep = EpisodeSummary(id=uuid.uuid4(), title="t", summary="s", outcome=None, started_at=NOW, tags=[])
            assert f.screen_episode(ep, ledger) == KEEP

        assert ledger.report.errors == 3
        assert len(_logged(caplog, logging.ERROR)) == 1, "one ERROR line per run"

    def test_an_object_without_an_id_keeps(self):
        f = _filter(kind="on")
        ledger = f.new_ledger()
        assert f.screen_fact(object(), ledger) == KEEP
        assert f.screen_episode(object(), ledger) == KEEP
        assert ledger.report.errors == 2


# ---------------------------------------------------------------------------
# The batched read against real rows
# ---------------------------------------------------------------------------


class _SessionDb:
    """Hands the test's own session (rolled back after the test) to the filter's read."""

    def __init__(self, session):
        self._session = session

    @asynccontextmanager
    async def session(self):
        yield self._session


@pytest.mark.postgres_only
class TestBatchedRead:
    @pytest.fixture
    def agent(self) -> str:
        return f"test-f116-read-{uuid.uuid4().hex[:12]}"

    @pytest.fixture
    def make(self, session, agent):
        async def make(**kwargs) -> Fact:
            values = {
                "id": uuid.uuid4(),
                "agent_id": agent,
                "content": "synthetic fact",
                "active": True,
                "learned_at": NOW - timedelta(days=10),
            }
            values.update(kwargs)
            fact = Fact(**values)
            session.add(fact)
            await session.flush()
            return fact

        return make

    def _filter(self, session, agent) -> PendingFilter:
        return PendingFilter(_cfg(supersede="shadow"), db=_SessionDb(session), agent_id=agent)

    async def _read(self, f: PendingFilter, *facts: Fact) -> dict[str, tuple[datetime | None, bool]]:
        return await f._read_rows([str(x.id) for x in facts])

    async def test_an_older_fact_with_a_newer_same_key_fact(self, session, agent, make):
        older = await make(subject_key="backup", attribute_key="time", learned_at=NOW - timedelta(days=10))
        newer = await make(subject_key="backup", attribute_key="time", learned_at=NOW - timedelta(days=2))

        rows = await self._read(self._filter(session, agent), older, newer)

        assert rows[str(older.id)][1] is True
        assert rows[str(newer.id)][1] is False

    @pytest.mark.parametrize("direction", ["newer_contradicts_older", "older_contradicts_newer"])
    async def test_a_contradiction_pair_does_not_count(self, session, agent, make, direction):
        older = await make(subject_key="backup", attribute_key="time", learned_at=NOW - timedelta(days=10))
        if direction == "newer_contradicts_older":
            newer = await make(
                subject_key="backup",
                attribute_key="time",
                learned_at=NOW - timedelta(days=2),
                contradiction_of=older.id,
            )
        else:
            newer = await make(subject_key="backup", attribute_key="time", learned_at=NOW - timedelta(days=2))
            older.contradiction_of = newer.id
            await session.flush()

        rows = await self._read(self._filter(session, agent), older)

        assert rows[str(older.id)][1] is False

    @pytest.mark.parametrize(
        ("older_keys", "newer_keys"),
        [((None, None), (None, None)), (("backup", None), ("backup", None)), ((None, "time"), (None, "time"))],
    )
    async def test_missing_keys_do_not_count(self, session, agent, make, older_keys, newer_keys):
        older = await make(subject_key=older_keys[0], attribute_key=older_keys[1], learned_at=NOW - timedelta(days=9))
        await make(subject_key=newer_keys[0], attribute_key=newer_keys[1], learned_at=NOW - timedelta(days=1))
        rows = await self._read(self._filter(session, agent), older)
        assert rows[str(older.id)][1] is False

    async def test_another_agents_fact_does_not_count(self, session, agent, make):
        older = await make(subject_key="backup", attribute_key="time", learned_at=NOW - timedelta(days=10))
        await make(
            agent_id=f"{agent}-other", subject_key="backup", attribute_key="time", learned_at=NOW - timedelta(days=1)
        )
        rows = await self._read(self._filter(session, agent), older)
        assert rows[str(older.id)][1] is False

    async def test_another_agents_fact_is_not_read(self, session, agent, make):
        other = await make(agent_id=f"{agent}-other")
        assert await self._read(self._filter(session, agent), other) == {}

    @pytest.mark.parametrize("state", ["inactive", "superseded"])
    async def test_a_newer_fact_that_is_inactive_or_superseded_does_not_count(self, session, agent, make, state):
        older = await make(subject_key="backup", attribute_key="time", learned_at=NOW - timedelta(days=10))
        if state == "inactive":
            await make(subject_key="backup", attribute_key="time", learned_at=NOW - timedelta(days=1), active=False)
        else:
            newest = await make(subject_key="other", attribute_key="x", learned_at=NOW)
            await make(
                subject_key="backup", attribute_key="time", learned_at=NOW - timedelta(days=1), superseded_by=newest.id
            )
        rows = await self._read(self._filter(session, agent), older)
        assert rows[str(older.id)][1] is False

    async def test_last_confirmed_is_returned(self, session, agent, make):
        confirmed_at = NOW - timedelta(days=1)
        confirmed = await make(last_confirmed=confirmed_at)
        never = await make()
        rows = await self._read(self._filter(session, agent), confirmed, never)
        assert rows[str(confirmed.id)][0] == confirmed_at
        assert rows[str(never.id)][0] is None

    async def test_one_call_with_five_ids_returns_five_rows(self, session, agent, make):
        facts = [await make(content=f"synthetic fact {i}") for i in range(5)]
        rows = await self._read(self._filter(session, agent), *facts)
        assert set(rows) == {str(x.id) for x in facts}

    async def test_the_read_through_the_database(self, db, agent):
        f = PendingFilter(_cfg(age="on"), db=db, agent_id=agent)
        assert await f._read_rows([str(uuid.uuid4())]) == {}

    async def test_prefetch_through_the_read_feeds_the_rules(self, session, agent, make):
        older = await make(subject_key="backup", attribute_key="time", learned_at=NOW - timedelta(days=40))
        await make(subject_key="backup", attribute_key="time", learned_at=NOW - timedelta(days=2))
        f = PendingFilter(_cfg(age="shadow", supersede="on"), db=_SessionDb(session), agent_id=agent)
        ledger = f.new_ledger()
        summary = _summary_fact("synthetic fact", age_days=40, source="user_direct", fid=older.id)

        await f.prefetch([summary], ledger)

        assert ledger.rows[str(older.id)] == (None, True)
        assert f.screen_fact(summary, ledger) == Verdict("shadow_drop", "age_fact")
        assert ledger.report.errors == 0
