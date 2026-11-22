"""F116 PR 2: the kind, age and supersession filters of the self-initiated heartbeat check.

The fixtures use the texts the F116 page quotes (section 3) for facts 78ccacac, 8f248bc2, 4ecbeeea, a02b0102 and the
quoted prefix of 55dfa56b. Every other fixture is synthetic and named ``synthetic_*``.

``TestDefaultOffParity`` pins what the check emits with every filter mode off: the findings, their order, their
fingerprints and every search the check makes. It was written against the check before the filters existed and is
not edited afterwards.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from sqlalchemy.dialects import postgresql

from nous.config import Settings
from nous.heart.schemas import EpisodeSummary, FactSummary
from nous.heartbeat.checks import PENDING_PROTOTYPES, SelfInitiatedCheck
from nous.heartbeat.pending_filter import PendingFilter
from nous.heartbeat.schemas import Finding

CHECK_NAME = "self_initiated"
KEYWORD_QUERY = "follow-up pending action TODO"
TEMPORAL_QUERIES = ["by Friday", "next week", "deadline", "due date", "end of week", "before Monday"]
PROMISE_QUERIES = [
    "I'll look into",
    "let me research",
    "I'll draft",
    "unfinished commitment",
    "ongoing task not completed",
]

# Texts quoted on the F116 page, section 3.
TEXT_78CCACAC = "The gate check (subtask 8f2bfae0) found the gate met and stopped, waiting for the user's approval"
TEXT_8F248BC2 = "requester is re-reviewing the heads of #800 … and #801"
TEXT_4ECBEEEA = "verdict of the v3 re-review is READY FOR A REAL RUN, PENDING TIM'S DECISIONS"
TEXT_A02B0102 = "Reviewing is the work currently in progress."
TEXT_55DFA56B = 'Heartbeat triage Nov 2 2026 (LOCAL-ONLY): "Pending action: Revie'


def _uid(prefix: str, n: int = 1) -> uuid.UUID:
    """A UUID whose first eight hex digits are ``prefix`` (the short id the F116 page uses)."""
    return uuid.UUID(f"{prefix}-0000-4000-8000-{n:012d}")


def _synthetic_id(n: int) -> uuid.UUID:
    return _uid("5e000000", n)


def _settings() -> MagicMock:
    s = MagicMock()
    s.agent_id = "test-agent"
    s.heartbeat_self_initiated_interval = 1800
    return s


def _fact(
    fid: uuid.UUID,
    content: str,
    *,
    now: datetime,
    age_days: float,
    source: str | None = "enumerative_extractor",
    category: str | None = None,
    actionable: bool | None = True,
    actionable_confidence: float | None = 0.85,
    tags: tuple[str, ...] = (),
    superseded_by: uuid.UUID | None = None,
) -> FactSummary:
    return FactSummary(
        id=fid,
        content=content,
        category=category,
        subject=None,
        confidence=1.0,
        active=True,
        score=0.9,
        superseded_by=superseded_by,
        actionable=actionable,
        actionable_confidence=actionable_confidence if actionable is not None else None,
        tags=list(tags),
        learned_at=now - timedelta(days=age_days),
        source=source,
    )


def _episode(eid: uuid.UUID, summary: str, *, now: datetime, age_days: float, outcome: str | None) -> EpisodeSummary:
    return EpisodeSummary(
        id=eid,
        title=summary[:40],
        summary=summary,
        outcome=outcome,
        started_at=now - timedelta(days=age_days),
        tags=[],
    )


def _scripted(script: dict[str, list]) -> AsyncMock:
    """A search whose result depends only on its query; every query not in the script returns []."""

    async def search(query: str, **_kwargs):
        return list(script.get(query, []))

    return AsyncMock(side_effect=search)


@dataclass
class Scenario:
    heart: MagicMock
    embeddings: MagicMock | None
    expected: list[tuple]  # (source, summary, urgency, needs_action, raw_data), in order
    fact_calls: list
    episode_calls: list
    facts: dict[str, FactSummary] = field(default_factory=dict)
    episodes: dict[str, EpisodeSummary] = field(default_factory=dict)

    def check(self) -> SelfInitiatedCheck:
        return SelfInitiatedCheck(self.heart, MagicMock(), _settings(), embeddings=self.embeddings)


def _embeddings() -> MagicMock:
    emb = MagicMock()
    emb.embed_batch = AsyncMock(return_value=[[0.1] * 10] * len(PENDING_PROTOTYPES))
    return emb


def _heart(fact_script: dict[str, list], episode_script: dict[str, list]) -> MagicMock:
    heart = MagicMock()
    heart.facts.search = _scripted(fact_script)
    heart.search_episodes = _scripted(episode_script)
    heart.schedules.get_due = AsyncMock(return_value=[])
    return heart


def _pending(fact: FactSummary, detection: str) -> tuple:
    return (
        "facts",
        f"Pending action: {fact.content[:100]}",
        "normal",
        True,
        {"fact_id": str(fact.id), "detection": detection},
    )


def _commitment(ep: EpisodeSummary) -> tuple:
    return (
        "episodes",
        f"Unresolved commitment: {ep.summary[:100]}",
        "normal",
        True,
        {"episode_id": str(ep.id), "detection": "promise_scan", "outcome": ep.outcome},
    )


def scenario_a(now: datetime) -> Scenario:
    """The embedding path fills the cap with the five quoted fixtures; nothing after it runs."""
    newer = _synthetic_id(99)
    f = {
        "78ccacac": _fact(_uid("78ccacac"), TEXT_78CCACAC, now=now, age_days=44),
        "8f248bc2": _fact(_uid("8f248bc2"), TEXT_8F248BC2, now=now, age_days=30, superseded_by=newer),
        "4ecbeeea": _fact(_uid("4ecbeeea"), TEXT_4ECBEEEA, now=now, age_days=21),
        "a02b0102": _fact(_uid("a02b0102"), TEXT_A02B0102, now=now, age_days=26),
        "55dfa56b": _fact(_uid("55dfa56b"), TEXT_55DFA56B, now=now, age_days=20, source="user_direct"),
        "synthetic_person": _fact(
            _synthetic_id(1),
            "synthetic: the owner's sister lives in Lisbon",
            now=now,
            age_days=3,
            source="user_direct",
            category="person",
        ),
    }
    facts_script = {
        PENDING_PROTOTYPES[0]: [f["78ccacac"], f["8f248bc2"]],
        PENDING_PROTOTYPES[1]: [f["synthetic_person"], f["4ecbeeea"]],
        PENDING_PROTOTYPES[2]: [f["a02b0102"], f["55dfa56b"]],
    }
    return Scenario(
        heart=_heart(facts_script, {}),
        embeddings=_embeddings(),
        expected=[
            _pending(f["78ccacac"], "embedding"),
            _pending(f["8f248bc2"], "embedding"),
            _pending(f["4ecbeeea"], "embedding"),
            _pending(f["a02b0102"], "embedding"),
            _pending(f["55dfa56b"], "embedding"),
        ],
        fact_calls=[call(PENDING_PROTOTYPES[i], limit=5) for i in range(3)],
        episode_calls=[],
        facts=f,
    )


def scenario_b(now: datetime) -> Scenario:
    """Two embedding facts, two rule facts from the keyword path, then one ongoing episode."""
    f = {
        "78ccacac": _fact(_uid("78ccacac"), TEXT_78CCACAC, now=now, age_days=44),
        "4ecbeeea": _fact(_uid("4ecbeeea"), TEXT_4ECBEEEA, now=now, age_days=21),
        "synthetic_rule_1": _fact(
            _synthetic_id(11),
            "synthetic: always run the linter before a commit",
            now=now,
            age_days=10,
            source="user_direct",
            category="rule",
            actionable_confidence=0.8,
        ),
        "synthetic_rule_2": _fact(
            _synthetic_id(12),
            "synthetic: reply to the owner in plain words",
            now=now,
            age_days=50,
            source="user_direct",
            category="rule",
            actionable_confidence=0.8,
        ),
    }
    e = {
        "synthetic_ongoing_3d": _episode(
            _synthetic_id(21),
            "I'll draft the release notes for the next version",
            now=now,
            age_days=3,
            outcome="ongoing",
        ),
    }
    facts_script = {
        PENDING_PROTOTYPES[0]: [f["78ccacac"], f["4ecbeeea"]],
        KEYWORD_QUERY: [f["synthetic_rule_1"], f["synthetic_rule_2"]],
    }
    return Scenario(
        heart=_heart(facts_script, {PROMISE_QUERIES[0]: [e["synthetic_ongoing_3d"]]}),
        embeddings=_embeddings(),
        expected=[
            _pending(f["78ccacac"], "embedding"),
            _pending(f["4ecbeeea"], "embedding"),
            _pending(f["synthetic_rule_1"], "keyword"),
            _pending(f["synthetic_rule_2"], "keyword"),
            _commitment(e["synthetic_ongoing_3d"]),
        ],
        fact_calls=[call(p, limit=5) for p in PENDING_PROTOTYPES] + [call(KEYWORD_QUERY, category="rule", limit=5)],
        episode_calls=[call(q, limit=3) for q in PROMISE_QUERIES],
        facts=f,
        episodes=e,
    )


def scenario_c(now: datetime) -> Scenario:
    """No embedding provider: the keyword path and the promise scan only; the temporal scan finds nothing."""
    f = {
        "synthetic_rule_3": _fact(
            _synthetic_id(13),
            "synthetic: TODO rotate the backup key every quarter",
            now=now,
            age_days=40,
            source="user_direct",
            category="rule",
            actionable=None,
        ),
        "synthetic_rule_4": _fact(
            _synthetic_id(14),
            "synthetic: TODO never paste a password into chat",
            now=now,
            age_days=5,
            source="user_direct",
            category="rule",
            actionable=False,
        ),
    }
    e = {
        "synthetic_ongoing_60d": _episode(
            _synthetic_id(31),
            "I'll look into the flaky upload test",
            now=now,
            age_days=60,
            outcome="ongoing",
        ),
        "synthetic_open_3d": _episode(
            _synthetic_id(32),
            "I'll draft a note on the backup plan",
            now=now,
            age_days=3,
            outcome=None,
        ),
        "synthetic_open_60d": _episode(
            _synthetic_id(33),
            "Let me research the storage options",
            now=now,
            age_days=60,
            outcome=None,
        ),
        "synthetic_done_3d": _episode(
            _synthetic_id(34),
            "I'll draft the invoice for March",
            now=now,
            age_days=3,
            outcome="success",
        ),
    }
    episode_script = {
        PROMISE_QUERIES[0]: [e["synthetic_ongoing_60d"], e["synthetic_open_3d"]],
        PROMISE_QUERIES[1]: [e["synthetic_open_60d"], e["synthetic_done_3d"]],
    }
    return Scenario(
        heart=_heart({KEYWORD_QUERY: [f["synthetic_rule_3"], f["synthetic_rule_4"]]}, episode_script),
        embeddings=None,
        expected=[
            _pending(f["synthetic_rule_3"], "keyword"),
            _commitment(e["synthetic_ongoing_60d"]),
            _commitment(e["synthetic_open_3d"]),
        ],
        fact_calls=[call(KEYWORD_QUERY, category="rule", limit=5)] + [call(q, limit=3) for q in TEMPORAL_QUERIES],
        episode_calls=[call(q, limit=3) for q in PROMISE_QUERIES],
        facts=f,
        episodes=e,
    )


SCENARIOS = {"a": scenario_a, "b": scenario_b, "c": scenario_c}


def _as_tuples(findings: list[Finding]) -> list[tuple]:
    return [(f.source, f.summary, f.urgency, f.needs_action, f.raw_data) for f in findings]


def _fingerprint_formula(source: str, summary: str) -> str:
    """``Finding.fingerprint()`` written out (nous/heartbeat/schemas.py), as the runner computes it."""
    normalized = re.sub(r"\d+", "N", summary)
    key = f"{CHECK_NAME}:{source}:{normalized}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


class TestDefaultOffParity:
    """Every filter mode off: the check's output and searches, pinned. Never edited after it was written."""

    @pytest.mark.parametrize("name", sorted(SCENARIOS))
    async def test_findings_order_and_searches(self, name):
        sc = SCENARIOS[name](datetime.now(UTC))
        check = sc.check()

        result = await check.run()

        assert _as_tuples(result.findings) == sc.expected
        assert result.has_updates is True
        assert sc.heart.facts.search.call_args_list == sc.fact_calls
        assert sc.heart.search_episodes.call_args_list == sc.episode_calls
        due_calls = 1 if name == "c" else 0
        assert sc.heart.schedules.get_due.await_count == due_calls

    @pytest.mark.parametrize("name", sorted(SCENARIOS))
    async def test_fingerprints(self, name):
        sc = SCENARIOS[name](datetime.now(UTC))
        result = await sc.check().run()

        for finding in result.findings:
            as_the_runner_sets_it = dataclasses.replace(finding, check_name=CHECK_NAME)
            assert as_the_runner_sets_it.fingerprint() == _fingerprint_formula(finding.source, finding.summary)

    async def test_the_78ccacac_fingerprint(self):
        sc = scenario_a(datetime.now(UTC))
        result = await sc.check().run()

        first = dataclasses.replace(result.findings[0], check_name=CHECK_NAME)
        assert first.raw_data["fact_id"] == str(_uid("78ccacac"))
        summary = "Pending action: " + TEXT_78CCACAC
        normalized = re.sub(r"\d+", "N", summary)
        expected = hashlib.sha256(f"self_initiated:facts:{normalized}".encode()).hexdigest()[:16]
        assert len(expected) == 16
        assert first.fingerprint() == expected


# ---------------------------------------------------------------------------
# The filters wired into the check
# ---------------------------------------------------------------------------


class _Rows:
    """A stand-in for the filter's batched read: no fact was confirmed, ``newer`` have a newer same-key fact."""

    def __init__(self, *, newer: set[str] = frozenset(), fail: bool = False):
        self.calls: list[list[str]] = []
        self._newer = newer
        self._fail = fail

    async def __call__(self, ids: list[str]):
        self.calls.append(list(ids))
        if self._fail:
            raise RuntimeError("database is down")
        return {i: (None, i in self._newer) for i in ids}


def _check(
    heart: MagicMock, embeddings: MagicMock | None, *, rows: _Rows | None = None, **modes: str
) -> SelfInitiatedCheck:
    """The check with the given modes set the way production sets them (settings), the read replaced by ``rows``."""
    settings = _settings()
    for layer, mode in modes.items():
        setattr(settings, f"heartbeat_pending_{layer}_mode", mode)
    check = SelfInitiatedCheck(heart, MagicMock(), settings, embeddings=embeddings)
    check._pending = PendingFilter(check._pending.cfg, fetch_rows=rows or _Rows(), check_name=CHECK_NAME)
    return check


def _scenario_check(name: str, *, rows: _Rows | None = None, **modes: str) -> tuple[Scenario, SelfInitiatedCheck]:
    sc = SCENARIOS[name](datetime.now(UTC))
    return sc, _check(sc.heart, sc.embeddings, rows=rows, **modes)


def _fingerprint(t: tuple) -> str:
    return _fingerprint_formula(t[0], t[1])


ALL_SHADOW = {"kind": "shadow", "age": "shadow", "supersede": "shadow"}
SHADOW_LAYERS = [{"kind": "shadow"}, {"age": "shadow"}, {"supersede": "shadow"}, ALL_SHADOW]
# What each scenario records in shadow, per mode set (the person fact of scenario a is skipped before the filter).
SHADOW_COUNTS = {
    ("a", "kind"): {"kind_note": 1},
    ("a", "age"): {"age_snapshot": 4},
    ("a", "supersede"): {"superseded": 1},
    ("a", "all"): {"age_snapshot": 4, "kind_note": 1},
    ("b", "kind"): {"kind_rule": 2},
    ("b", "age"): {"age_snapshot": 2, "age_fact": 1},
    ("b", "supersede"): {},
    ("b", "all"): {"age_snapshot": 2, "kind_rule": 2},
    ("c", "kind"): {"kind_rule": 1},
    ("c", "age"): {"age_fact": 1, "age_ongoing": 1},
    ("c", "supersede"): {},
    ("c", "all"): {"kind_rule": 1, "age_ongoing": 1},
}


def _label(modes: dict[str, str]) -> str:
    return "all" if len(modes) == 3 else next(iter(modes))


class TestOffRunsNothing:
    @pytest.mark.parametrize("explicit", [False, True])
    @pytest.mark.parametrize("name", sorted(SCENARIOS))
    async def test_no_filter_function_is_called(self, name, explicit, monkeypatch):
        called: list[str] = []

        def tripwire(fn_name):
            def fn(*_args, **_kwargs):
                called.append(fn_name)
                raise AssertionError(f"{fn_name} called with every mode off")

            return fn

        for fn_name in ("new_ledger", "prefetch", "screen_fact", "screen_episode"):
            monkeypatch.setattr(PendingFilter, fn_name, tripwire(fn_name))
        sc = SCENARIOS[name](datetime.now(UTC))
        settings = _settings()
        if explicit:
            for layer in ("kind", "age", "supersede"):
                setattr(settings, f"heartbeat_pending_{layer}_mode", "off")
        check = SelfInitiatedCheck(sc.heart, MagicMock(), settings, embeddings=sc.embeddings)

        result = await check.run()

        assert called == []
        assert check._pending.enabled is False
        assert _as_tuples(result.findings) == sc.expected
        assert sc.heart.facts.search.call_args_list == sc.fact_calls
        assert result.suppression is None

    async def test_a_filter_that_cannot_be_set_up_is_off(self, monkeypatch):
        def broken(*_args, **_kwargs):
            raise RuntimeError("bad config")

        monkeypatch.setattr("nous.heartbeat.checks.PendingFilterConfig.from_settings", broken)
        sc = scenario_a(datetime.now(UTC))
        settings = _settings()
        settings.heartbeat_pending_kind_mode = "on"
        check = SelfInitiatedCheck(sc.heart, MagicMock(), settings, embeddings=sc.embeddings)

        result = await check.run()

        assert check._pending.enabled is False
        assert _as_tuples(result.findings) == sc.expected
        assert result.suppression is None


class TestShadowNeverChangesOutput:
    @pytest.mark.parametrize("modes", SHADOW_LAYERS, ids=_label)
    @pytest.mark.parametrize("name", sorted(SCENARIOS))
    async def test_findings_and_searches_are_unchanged(self, name, modes):
        sc, check = _scenario_check(name, **modes)

        result = await check.run()

        assert _as_tuples(result.findings) == sc.expected
        assert sc.heart.facts.search.call_args_list == sc.fact_calls
        assert sc.heart.search_episodes.call_args_list == sc.episode_calls
        assert result.suppression is not None
        assert result.suppression.shadow_suppressed == SHADOW_COUNTS[(name, _label(modes))]
        assert result.suppression.suppressed == {} and result.suppression.items == []
        assert result.suppression.errors == 0

    @pytest.mark.parametrize("name", sorted(SCENARIOS))
    async def test_the_recorded_fingerprints_are_the_findings(self, name):
        sc, check = _scenario_check(name, **ALL_SHADOW)

        result = await check.run()

        emitted = {_fingerprint(t) for t in sc.expected}
        recorded = {item["fingerprint"] for item in result.suppression.shadow_items}
        assert recorded and recorded <= emitted
        by_id = {t[4].get("fact_id") or t[4].get("episode_id"): _fingerprint(t) for t in sc.expected}
        for item in result.suppression.shadow_items:
            assert by_id[item["id"]] == item["fingerprint"]

    async def test_a_failed_read_in_shadow_changes_nothing(self):
        sc, check = _scenario_check("a", rows=_Rows(fail=True), **ALL_SHADOW)

        result = await check.run()

        assert _as_tuples(result.findings) == sc.expected
        assert result.suppression.errors == 1
        # Only the rules that need no row (the age rule needs last_confirmed; the note and superseded_by do not).
        assert result.suppression.shadow_suppressed == {"kind_note": 1, "superseded": 1}


def _custom(
    facts_script: dict[str, list], episode_script: dict[str, list] | None = None, *, embedded: bool = True
) -> tuple[MagicMock, MagicMock | None]:
    return _heart(facts_script, episode_script or {}), _embeddings() if embedded else None


class TestKindFilter:
    async def test_on_hides_a_note_on_the_embedding_path(self):
        sc, check = _scenario_check("a", kind="on")

        result = await check.run()

        f = sc.facts
        assert _as_tuples(result.findings) == [
            _pending(f["78ccacac"], "embedding"),
            _pending(f["8f248bc2"], "embedding"),
            _pending(f["4ecbeeea"], "embedding"),
            _pending(f["a02b0102"], "embedding"),
        ]
        assert result.suppression.suppressed == {"kind_note": 1}
        assert result.suppression.items[0]["id"] == str(f["55dfa56b"].id)

    async def test_on_hides_rule_facts_on_the_keyword_path(self):
        sc, check = _scenario_check("b", kind="on")

        result = await check.run()

        assert _as_tuples(result.findings) == [
            _pending(sc.facts["78ccacac"], "embedding"),
            _pending(sc.facts["4ecbeeea"], "embedding"),
            _commitment(sc.episodes["synthetic_ongoing_3d"]),
        ]
        assert result.suppression.suppressed == {"kind_rule": 2}

    async def test_on_without_an_embedding_provider_raises_no_fact_finding(self):
        sc, check = _scenario_check("c", kind="on")

        result = await check.run()

        assert [t for t in _as_tuples(result.findings) if t[0] == "facts"] == []
        assert result.suppression.suppressed == {"kind_rule": 1}

    async def test_shadow_emits_and_records(self):
        sc, check = _scenario_check("b", kind="shadow")
        result = await check.run()
        assert _as_tuples(result.findings) == sc.expected
        assert result.suppression.shadow_suppressed == {"kind_rule": 2}


class TestAgeFilter:
    async def test_on_hides_old_status_lines_and_adds_the_digest(self):
        sc, check = _scenario_check("a", age="on")

        result = await check.run()

        f = sc.facts
        hidden = [str(f[k].id) for k in ("78ccacac", "8f248bc2", "4ecbeeea", "a02b0102")]
        assert _as_tuples(result.findings) == [
            _pending(f["55dfa56b"], "embedding"),
            (
                "facts",
                "Pending items hidden by age cap: 4",
                "low",
                False,
                {"detection": "age_digest", "count": 4, "ids": hidden},
            ),
        ]
        assert result.suppression.suppressed == {"age_snapshot": 4}

    async def test_on_hides_an_old_rule_fact_on_the_keyword_path(self):
        sc, check = _scenario_check("b", age="on")

        result = await check.run()

        tuples = _as_tuples(result.findings)
        assert _pending(sc.facts["synthetic_rule_2"], "keyword") not in tuples
        assert _pending(sc.facts["synthetic_rule_1"], "keyword") in tuples
        assert result.suppression.suppressed == {"age_snapshot": 2, "age_fact": 1}

    async def test_on_hides_an_old_ongoing_episode(self):
        sc, check = _scenario_check("c", age="on")

        result = await check.run()

        e, f = sc.episodes, sc.facts
        assert _as_tuples(result.findings) == [
            _commitment(e["synthetic_open_3d"]),
            (
                "facts",
                "Pending items hidden by age cap: 2",
                "low",
                False,
                {
                    "detection": "age_digest",
                    "count": 2,
                    "ids": [str(f["synthetic_rule_3"].id), str(e["synthetic_ongoing_60d"].id)],
                },
            ),
        ]
        assert result.suppression.suppressed == {"age_fact": 1, "age_ongoing": 1}

    async def test_a_failed_read_keeps_every_aged_fact(self):
        sc, check = _scenario_check("a", rows=_Rows(fail=True), age="on")

        result = await check.run()

        assert _as_tuples(result.findings) == sc.expected
        assert result.suppression.errors == 1
        assert result.suppression.suppressed == {}

    async def test_shadow_emits_and_records(self):
        sc, check = _scenario_check("c", age="shadow")
        result = await check.run()
        assert _as_tuples(result.findings) == sc.expected
        assert result.suppression.shadow_suppressed == {"age_fact": 1, "age_ongoing": 1}


class TestSupersedeFilter:
    async def test_on_hides_a_superseded_fact_on_the_embedding_path(self):
        sc, check = _scenario_check("a", supersede="on")

        result = await check.run()

        tuples = _as_tuples(result.findings)
        assert _pending(sc.facts["8f248bc2"], "embedding") not in tuples
        assert len(tuples) == 4
        assert result.suppression.suppressed == {"superseded": 1}

    async def test_on_hides_a_superseded_rule_fact_on_the_keyword_path(self):
        now = datetime.now(UTC)
        fact = _fact(_synthetic_id(41), "synthetic: TODO archive the logs", now=now, age_days=2, category="rule")
        fact = fact.model_copy(update={"superseded_by": _synthetic_id(42)})
        heart, embeddings = _custom({KEYWORD_QUERY: [fact]}, embedded=False)

        off = await _check(heart, embeddings).run()
        on = await _check(heart, embeddings, supersede="on").run()

        assert _pending(fact, "keyword") in _as_tuples(off.findings)
        assert _pending(fact, "keyword") not in _as_tuples(on.findings)
        assert on.suppression.suppressed == {"superseded": 1}

    async def test_a_newer_same_key_fact_is_recorded_but_still_emitted_when_on(self):
        sc = scenario_a(datetime.now(UTC))
        target = str(sc.facts["78ccacac"].id)
        check = _check(sc.heart, sc.embeddings, rows=_Rows(newer={target}), supersede="on")

        result = await check.run()

        assert _pending(sc.facts["78ccacac"], "embedding") in _as_tuples(result.findings)
        assert result.suppression.shadow_suppressed == {"newer_same_key": 1}
        assert result.suppression.suppressed == {"superseded": 1}

    async def test_shadow_emits_and_records(self):
        sc, check = _scenario_check("a", supersede="shadow")
        result = await check.run()
        assert _as_tuples(result.findings) == sc.expected
        assert result.suppression.shadow_suppressed == {"superseded": 1}


class TestControls:
    """Spec 7.6: these surface with every layer on."""

    async def test_the_controls_surface_with_every_layer_on(self):
        now = datetime.now(UTC)
        waiting = _fact(_synthetic_id(51), "waiting on Tim to merge #957", now=now, age_days=1, category="technical")
        task = _fact(
            _synthetic_id(52),
            "synthetic: send the invoice to the accountant",
            now=now,
            age_days=2,
            source="user_direct",
        )
        dated = _fact(
            _synthetic_id(53), "synthetic: renew the domain", now=now, age_days=60, source="user_direct"
        ).model_copy(update={"event_date": now.date() + timedelta(days=5)})
        ongoing = _episode(_synthetic_id(54), "I'll draft the release notes", now=now, age_days=3, outcome="ongoing")
        heart, embeddings = _custom({PENDING_PROTOTYPES[0]: [waiting, task, dated]}, {PROMISE_QUERIES[0]: [ongoing]})

        result = await _check(heart, embeddings, kind="on", age="on", supersede="on").run()

        assert _as_tuples(result.findings) == [
            _pending(waiting, "embedding"),
            _pending(task, "embedding"),
            _pending(dated, "embedding"),
            _commitment(ongoing),
        ]
        assert not result.suppression


class TestMaxItems:
    async def test_hidden_facts_take_no_slot(self):
        now = datetime.now(UTC)
        rules = [
            _fact(_synthetic_id(60 + i), f"synthetic: rule {i}", now=now, age_days=1, category="rule") for i in range(3)
        ]
        kept = [_fact(_synthetic_id(70 + i), f"synthetic: task {i}", now=now, age_days=1) for i in range(2)]
        heart, embeddings = _custom({PENDING_PROTOTYPES[0]: [*rules, kept[0]], PENDING_PROTOTYPES[1]: [kept[1]]})

        off = _check(heart, embeddings)
        off._params["max_pending_items"].value = 2
        on = _check(heart, embeddings, kind="on")
        on._params["max_pending_items"].value = 2

        assert _as_tuples((await off.run()).findings) == [_pending(r, "embedding") for r in rules[:2]]
        assert _as_tuples((await on.run()).findings) == [_pending(k, "embedding") for k in kept]


class TestDigest:
    def _setup(self, now: datetime, kept: int = 1) -> tuple[MagicMock, MagicMock, list[FactSummary]]:
        old = [_fact(_synthetic_id(80 + i), f"synthetic: status {i}", now=now, age_days=40) for i in range(2)]
        fresh = [_fact(_synthetic_id(90 + i), f"synthetic: task {i}", now=now, age_days=1) for i in range(kept)]
        heart, embeddings = _custom({PENDING_PROTOTYPES[0]: [*old, *fresh[:3]], PENDING_PROTOTYPES[1]: fresh[3:]})
        return heart, embeddings, old

    async def test_once_a_day_after_the_cut(self):
        heart, embeddings, old = self._setup(datetime.now(UTC))
        check = _check(heart, embeddings, age="on")

        first = await check.run()
        second = await check.run()
        check._last_age_digest = datetime.now(UTC).date() - timedelta(days=1)
        third = await check.run()

        digest = ("Pending items hidden by age cap: 2", "low", False)
        firsts = [f for f in first.findings if f.raw_data.get("detection") == "age_digest"]
        assert len(firsts) == 1 and first.findings[-1] is firsts[0]
        assert (firsts[0].summary, firsts[0].urgency, firsts[0].needs_action) == digest
        assert firsts[0].raw_data == {"detection": "age_digest", "count": 2, "ids": [str(o.id) for o in old]}
        assert [f for f in second.findings if f.raw_data.get("detection") == "age_digest"] == []
        assert second.suppression.suppressed == {"age_snapshot": 2}, "the items stay hidden"
        thirds = [f for f in third.findings if f.raw_data.get("detection") == "age_digest"]
        assert len(thirds) == 1
        firsts[0].check_name = thirds[0].check_name = CHECK_NAME
        assert thirds[0].fingerprint() == firsts[0].fingerprint()

    async def test_the_digest_can_make_the_list_one_longer_than_the_cap(self):
        heart, embeddings, _ = self._setup(datetime.now(UTC), kept=5)
        result = await _check(heart, embeddings, age="on").run()
        assert len(result.findings) == 6
        assert result.findings[-1].summary == "Pending items hidden by age cap: 2"

    async def test_shadow_never_adds_it(self):
        heart, embeddings, _ = self._setup(datetime.now(UTC))
        result = await _check(heart, embeddings, age="shadow").run()
        assert [f for f in result.findings if f.raw_data.get("detection") == "age_digest"] == []
        assert result.suppression.shadow_suppressed == {"age_snapshot": 2}

    async def test_other_rules_do_not_add_it(self):
        now = datetime.now(UTC)
        question = _fact(_synthetic_id(99), "Who owns the deploy?", now=now, age_days=1)
        heart, embeddings = _custom({PENDING_PROTOTYPES[0]: [question]})
        result = await _check(heart, embeddings, age="on").run()
        assert result.findings == [] and result.suppression.suppressed == {"question_form": 1}


class TestFingerprintsUnchanged:
    @pytest.mark.parametrize(
        ("name", "modes"),
        [("a", {"kind": "on"}), ("a", {"supersede": "on"}), ("b", {"kind": "on"}), ("c", {"age": "on"})],
    )
    async def test_survivors_carry_the_parity_fingerprints(self, name, modes):
        sc, check = _scenario_check(name, **modes)

        result = await check.run()

        parity = {t[1]: _fingerprint(t) for t in sc.expected}
        survivors = [f for f in result.findings if f.raw_data.get("detection") != "age_digest"]
        assert survivors
        for finding in survivors:
            assert dataclasses.replace(finding, check_name=CHECK_NAME).fingerprint() == parity[finding.summary]


class _CapturingDb:
    """The fact manager's database as the filter uses it: records each statement and answers every id it binds."""

    def __init__(self) -> None:
        self.statements: list = []

    @asynccontextmanager
    async def session(self):
        yield self

    async def execute(self, stmt):
        self.statements.append(stmt)
        params = stmt.compile(dialect=postgresql.dialect()).params
        ids = next(v for v in params.values() if isinstance(v, list))
        return [SimpleNamespace(id=i, last_confirmed=None, newer_same_key=False) for i in ids]


class TestWiring:
    """The check builds its filter from the settings and reads the rows through the fact manager's database."""

    async def test_settings_from_the_environment_and_the_fact_managers_database(self, monkeypatch):
        for layer in ("kind", "supersede"):
            monkeypatch.delenv(f"NOUS_HEARTBEAT_PENDING_{layer.upper()}_MODE", raising=False)
        monkeypatch.setenv("NOUS_HEARTBEAT_PENDING_AGE_MODE", "shadow")
        settings = Settings(_env_file=None)
        sc = scenario_a(datetime.now(UTC))
        db = _CapturingDb()
        sc.heart.facts.db = db
        sc.heart.facts.agent_id = "agent-marker"

        check = SelfInitiatedCheck(sc.heart, MagicMock(), settings, embeddings=sc.embeddings)
        result = await check.run()

        assert _as_tuples(result.findings) == sc.expected
        assert len(db.statements) == 3, "one read per prototype result list"
        params = db.statements[0].compile(dialect=postgresql.dialect()).params
        assert "agent-marker" in params.values()
        assert next(v for v in params.values() if isinstance(v, list)) == [
            sc.facts["78ccacac"].id,
            sc.facts["8f248bc2"].id,
        ]
        assert result.suppression.shadow_suppressed == {"age_snapshot": 4}
        assert result.suppression.errors == 0
