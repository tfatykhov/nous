"""Bi-temporal facts, PR2: ``interval_relation``, the pure rule supersession uses.

Graphiti's rules: non-overlapping intervals coexist, a late-arriving earlier
fact is closed by the newer one, and a NULL date is never compared.
"""

from __future__ import annotations

from datetime import date

import pytest

from nous.heart.temporal import Interval, close_before, interval_relation

D = date


def iv(start: str | None, end: str | None = None) -> Interval:
    return Interval(D.fromisoformat(start) if start else None, D.fromisoformat(end) if end else None)


def point(day: str) -> Interval:
    return iv(day, day)


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (point("2024-03-10"), point("2024-03-10"), "tie"),
        (point("2024-03-10"), point("2024-03-12"), "coexist"),
        (point("2024-03-10"), point("2024-03-11"), "coexist"),  # adjacent days do not overlap
        (iv("2024-01-01"), iv("2024-06-01"), "new_later"),  # open vs later open
        (iv("2024-06-01"), iv("2024-01-01"), "old_later"),  # open vs earlier open: a late arrival
        (iv("2024-01-01", "2024-01-31"), iv("2024-03-01", "2024-03-31"), "coexist"),  # disjoint spans
        (iv("2024-03-01", "2024-03-31"), iv("2024-01-01", "2024-01-31"), "coexist"),
        (iv("2024-01-01", "2024-01-31"), iv("2024-02-01"), "coexist"),  # a state after a closed span
        (iv("2024-01-01", "2024-02-15"), iv("2024-02-15"), "new_later"),  # one shared day overlaps
        (iv("2024-01-01"), iv("2024-01-01", "2024-03-01"), "tie"),
        (iv(None), iv("2024-01-01"), "unknown"),
        (iv("2024-01-01"), iv(None), "unknown"),
        (iv(None), iv(None), "unknown"),
        (iv(None, "2024-01-31"), point("2024-01-10"), "unknown"),  # an end with no start is never compared
    ],
)
def test_the_truth_table(old, new, expected):
    assert interval_relation(old, new) == expected


def test_a_trip_inside_a_residence_is_new_later():
    """Only the classifier keeps a trip from superseding the residence it falls in."""
    residence = iv("2024-01-01")
    trip = iv("2024-03-03", "2024-03-10")
    assert interval_relation(residence, trip) == "new_later"


def test_any_object_with_the_two_attributes_works():
    class Row:
        valid_from = D(2024, 6, 1)
        valid_to = None

    assert interval_relation(Row(), iv("2024-01-01")) == "old_later"


def test_close_before_is_the_day_before():
    assert close_before(D(2024, 6, 1)) == D(2024, 5, 31)
    assert close_before(D(2024, 3, 1)) == D(2024, 2, 29)
