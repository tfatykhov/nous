"""Bi-temporal facts: ``interval_relation``, the pure rule supersession (PR2) and the readers (PR3) use, plus
PR3's ``format_span``, ``sort_dated`` and ``as_date``.

Graphiti's rules: non-overlapping intervals coexist, a late-arriving earlier
fact is closed by the newer one, and a NULL date is never compared.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from nous.heart.temporal import Interval, as_date, close_before, format_span, interval_relation, sort_dated

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


# ---- PR3 (read and render): the same table as interval pairs, the date label, the date sort ----------------------


@pytest.mark.parametrize(
    ("a", "b", "relation"),
    [
        (Interval(D(2024, 3, 10), D(2024, 3, 10)), Interval(D(2024, 3, 10), D(2024, 3, 10)), "tie"),
        (Interval(D(2024, 3, 10), D(2024, 3, 10)), Interval(D(2024, 3, 12), D(2024, 3, 12)), "coexist"),
        (Interval(D(2024, 1, 1), None), Interval(D(2024, 6, 1), None), "new_later"),
        (Interval(D(2024, 6, 1), None), Interval(D(2024, 1, 1), None), "old_later"),
        (Interval(D(2024, 1, 1), D(2024, 2, 1)), Interval(D(2024, 5, 1), D(2024, 6, 1)), "coexist"),
        (Interval(D(2024, 3, 10), D(2024, 3, 10)), Interval(D(2024, 3, 11), D(2024, 3, 11)), "coexist"),
        (Interval(D(2024, 1, 1), D(2024, 1, 31)), Interval(D(2024, 2, 1), D(2024, 2, 29)), "coexist"),
        (Interval(D(2024, 1, 1), None), Interval(D(2024, 3, 1), D(2024, 3, 14)), "new_later"),
        (Interval(D(2024, 1, 1), None), Interval(D(2024, 1, 1), D(2024, 2, 1)), "tie"),
        (Interval(None, None), Interval(D(2024, 1, 1), None), "unknown"),
        (Interval(D(2024, 1, 1), None), Interval(None, None), "unknown"),
    ],
)
def test_interval_relation_truth_table_for_the_readers(a, b, relation):
    assert interval_relation(a, b) == relation


@pytest.mark.parametrize(
    ("valid_from", "valid_to", "event_date", "label"),
    [
        (D(2024, 3, 10), D(2024, 3, 10), D(2024, 3, 10), "[2024-03-10]"),  # point
        (D(2026, 2, 1), D(2026, 2, 28), None, "[2026-02]"),  # full month
        (D(2024, 2, 1), D(2024, 2, 29), None, "[2024-02]"),  # full leap month
        (D(2024, 1, 5), D(2024, 6, 1), None, "[2024-01-05 to 2024-06-01]"),  # closed span
        (D(2024, 2, 1), D(2024, 2, 20), None, "[2024-02-01 to 2024-02-20]"),  # part of a month
        (D(2024, 1, 5), None, None, "[since 2024-01-05]"),  # open
        (None, None, None, ""),  # no date
        (None, None, D(2024, 3, 10), "[2024-03-10]"),  # not backfilled: event_date
    ],
)
def test_format_span(valid_from, valid_to, event_date, label):
    assert format_span(valid_from, valid_to, event_date) == label


def test_sort_dated_orders_only_the_dated_items_and_keeps_the_rest_in_place():
    items = ["c", "x", "a", "y", "b"]  # rank order; x and y are undated
    dates = {"a": D(2024, 1, 1), "b": D(2024, 2, 1), "c": D(2024, 3, 1)}
    assert sort_dated(items, dates.get) == ["a", "x", "b", "y", "c"]


def test_sort_dated_is_stable_for_equal_dates_and_a_no_op_without_dates():
    items = ["b1", "a1", "b2"]
    same = {"b1": D(2024, 1, 1), "a1": D(2024, 1, 1), "b2": D(2024, 1, 1)}
    assert sort_dated(items, same.get) == ["b1", "a1", "b2"]
    assert sort_dated(items, lambda _: None) == items


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (D(2024, 3, 10), D(2024, 3, 10)),
        (datetime(2024, 3, 10, 15, 0), D(2024, 3, 10)),
        ("2024-03-10", D(2024, 3, 10)),
        ("2024-03-10T00:00:00+00:00", D(2024, 3, 10)),
        ("not a date", None),
        (None, None),
        (20240310, None),
    ],
)
def test_as_date(value, expected):
    assert as_date(value) == expected
