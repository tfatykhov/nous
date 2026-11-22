"""F116 PR 2: the self-initiated check's filters on their own (nous/heartbeat/pending_filter.py).

Transport type, the pure rules (no I/O, an injected clock), the filter with its batched read replaced, and the batched
read against real rows. The check with the filters wired in is tested in tests/test_heartbeat_stale_pending.py.
"""

from __future__ import annotations

import dataclasses
import itertools
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nous.heart.schemas import EpisodeSummary, FactSummary
from nous.heartbeat.pending_filter import (
    KEEP,
    Candidate,
    PendingFilterConfig,
    Verdict,
    episode_candidate,
    evaluate,
    fact_candidate,
    parse_mode,
)
from nous.heartbeat.schemas import CheckResult, SuppressionReport

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
            "HEARTBEAT FALSE-POSITIVE: the deploy reminder is done",
            "HEARTBEAT SUPPRESSION: do not raise the backup line",
        ],
    )
    def test_triage_and_suppression_notes(self, text):
        assert evaluate(_fact(text), _cfg(kind="on"), NOW) == Verdict("drop", "kind_note")

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
