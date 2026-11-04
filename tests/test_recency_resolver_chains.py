"""The pre-turn recency resolver (``ContextEngine._resolve_recency``) on chains of three or more same-subject facts.

Like recall_deep's copy (``_resolve_recency_conflicts``): superseded is sticky, each superseded fact is penalised
once, and the result does not depend on the order the facts arrive in.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from itertools import permutations
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nous.cognitive.context import ContextEngine
from nous.config import Settings
from nous.heart.schemas import FactSummary


def _engine(**flags) -> ContextEngine:
    brain = AsyncMock()
    brain.embeddings = MagicMock()
    settings = Settings(_env_file=None, recency_resolver_enabled=True, **flags)
    return ContextEngine(brain, AsyncMock(), settings, identity_prompt="")


def _fact(content: str, **dates) -> FactSummary:
    return FactSummary(
        id=uuid4(),
        content=content,
        category="person",
        subject="alice",
        confidence=1.0,
        active=True,
        score=0.9,
        **dates,
    )


def _points():
    """Flags off: three corrections of one date, each stated a day later (the B-1 rule)."""
    return [
        _fact(
            f"Alice's dentist visit is on day {d}",
            event_date=date(2024, 3, d),
            learned_at=datetime(2024, 3, 1, tzinfo=UTC).replace(day=n),
        )
        for n, d in ((1, 5), (2, 7), (3, 9))
    ]


def _states():
    """Resolver flag on: three overlapping open states, Paris since January, Berlin since June, Rome since September."""
    return [
        _fact(f"Alice lives in {city}", valid_from=date(2024, month, 1))
        for city, month in (("Paris", 1), ("Berlin", 6), ("Rome", 9))
    ]


@pytest.mark.parametrize(
    ("chain", "flags", "current"),
    [
        (_points, {}, "Alice's dentist visit is on day 9"),
        (_states, {"temporal_resolver_intervals_enabled": True}, "Alice lives in Rome"),
    ],
    ids=["points-flags-off", "states-resolver-on"],
)
@pytest.mark.parametrize("order", list(permutations(range(3))), ids=lambda o: "".join(map(str, o)))
def test_a_chain_has_one_current_fact_and_each_older_fact_is_penalised_once(chain, flags, current, order):
    facts = chain()
    out = _engine(**flags)._resolve_recency([facts[i] for i in order])
    result = {f.content: (f.recency_status, round(f.score, 4)) for f in out}
    for content, (status, score) in result.items():
        if content == current:
            assert (status, score) == ("current", 0.9), result
        else:
            assert (status, score) == ("superseded", 0.27), result


def test_a_superseded_fact_keeps_its_own_month_label():
    paris, berlin, rome = _states()
    _engine(temporal_resolver_intervals_enabled=True)._resolve_recency([rome, berlin, paris])
    assert [(f.recency_status, f.recency_date) for f in (paris, berlin, rome)] == [
        ("superseded", "2024-01"),
        ("superseded", "2024-06"),
        ("current", "2024-09"),
    ]


def test_a_pair_still_resolves_as_before():
    old, new = _points()[:2]
    _engine()._resolve_recency([new, old])
    assert (new.recency_status, new.score, old.recency_status, round(old.score, 4)) == (
        "current",
        0.9,
        "superseded",
        0.27,
    )
