"""Bi-temporal facts: how two facts' event-time intervals relate.

A fact's interval is ``valid_from`` .. ``valid_to`` (days, inclusive). No
``valid_to`` means it is still true, or its end is unknown. No ``valid_from``
means its time is unknown, and an unknown time is never compared.

Supersession on time (flag S, ``temporal_supersession_enabled``) uses these
rules, Graphiti's: non-overlapping intervals coexist, and when they overlap the
later start is current, so a late-arriving fact that started earlier is closed
by the stored one rather than replacing it.

Reading and rendering (PR3) add ``as_date``, ``format_span`` (the date label a
reader sees) and ``sort_dated`` (date order that leaves undated items in place).
"""

from __future__ import annotations

import calendar
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Literal, NamedTuple, Protocol

IntervalRelation = Literal["unknown", "coexist", "new_later", "old_later", "tie"]


class HasInterval(Protocol):
    @property
    def valid_from(self) -> date | None: ...

    @property
    def valid_to(self) -> date | None: ...


class Interval(NamedTuple):
    """An interval on its own, for a fact whose row is not at hand."""

    valid_from: date | None
    valid_to: date | None


def _end(x: HasInterval) -> date:
    return x.valid_to if x.valid_to is not None else date.max


def interval_relation(old: HasInterval, new: HasInterval) -> IntervalRelation:
    """How ``new`` relates to ``old`` in event time.

    - ``unknown``: either start is unknown. Callers fall back to today's logic.
    - ``coexist``: the intervals share no day (adjacent days included), so both
      can be true. Two one-day events on different days always coexist.
    - ``new_later`` / ``old_later``: they overlap and that fact started later.
    - ``tie``: they overlap and started the same day.
    """
    if old.valid_from is None or new.valid_from is None:
        return "unknown"
    if _end(old) < new.valid_from or _end(new) < old.valid_from:
        return "coexist"
    if new.valid_from > old.valid_from:
        return "new_later"
    if new.valid_from < old.valid_from:
        return "old_later"
    return "tie"


def close_before(start: date) -> date:
    """The ``valid_to`` that ends an interval the day before ``start``."""
    return start - timedelta(days=1)


def as_date(value: object) -> date | None:
    """``value`` as a date: a date as is, a datetime's date, an ISO date or datetime string parsed; else None."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return None
    return None


def format_span(valid_from: date | None, valid_to: date | None, event_date: date | None = None) -> str:
    """The date label a reader sees: ``[2024-03-10]``, ``[2026-02]``, ``[2024-01-05 to 2024-06-01]``,
    ``[since 2024-01-05]``, or ``""`` when no date is known (``event_date`` for a row not yet backfilled)."""
    if valid_from is None:
        return f"[{event_date.isoformat()}]" if event_date is not None else ""
    if valid_to is None:
        return f"[since {valid_from.isoformat()}]"
    if valid_to == valid_from:
        return f"[{valid_from.isoformat()}]"
    last_day = calendar.monthrange(valid_from.year, valid_from.month)[1]
    if valid_from.day == 1 and valid_to == valid_from.replace(day=last_day):
        return f"[{valid_from:%Y-%m}]"
    return f"[{valid_from.isoformat()} to {valid_to.isoformat()}]"


def sort_dated[T](items: list[T], date_of: Callable[[T], date | None]) -> list[T]:
    """``items`` with the dated ones in date order, each in a slot a dated item held; undated items keep their
    places. Stable: items with the same date keep their order."""
    slots = [i for i, item in enumerate(items) if date_of(item) is not None]
    if len(slots) < 2:
        return list(items)
    ordered = sorted((items[i] for i in slots), key=lambda item: date_of(item))  # type: ignore[arg-type,return-value]
    out = list(items)
    for slot, item in zip(slots, ordered, strict=True):
        out[slot] = item
    return out
