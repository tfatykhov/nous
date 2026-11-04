"""Bi-temporal facts: how two facts' event-time intervals relate.

A fact's interval is ``valid_from`` .. ``valid_to`` (days, inclusive). No
``valid_to`` means it is still true, or its end is unknown. No ``valid_from``
means its time is unknown, and an unknown time is never compared.

Supersession on time (flag S, ``temporal_supersession_enabled``) uses these
rules, Graphiti's: non-overlapping intervals coexist, and when they overlap the
later start is current, so a late-arriving fact that started earlier is closed
by the stored one rather than replacing it.
"""

from __future__ import annotations

from datetime import date, timedelta
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
