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
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from nous.heart.schemas import EpisodeSummary, FactSummary
from nous.heartbeat.checks import PENDING_PROTOTYPES, SelfInitiatedCheck
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
