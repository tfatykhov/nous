"""F114: the limits a goal runs within (its envelope): three presets, and any custom values inside the ranges
that migration 113's CHECKs hold. ``validate`` mirrors those CHECKs, so a bad value is refused in words before the
database would refuse it with an error."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from typing import Any


class EnvelopeError(ValueError):
    """An envelope outside the allowed ranges, an unknown preset or an unknown field. The text says which."""


@dataclass(frozen=True)
class Envelope:
    """Tokens are the continuation accounting's unit: uncached input plus output."""

    total_budget: int
    day_budget: int
    deadline_days: int
    sessions_per_day: int
    min_interval_seconds: int
    jobs_per_day: int
    quiet_hours_ok: bool


PRESETS: dict[str, Envelope] = {
    "small": Envelope(2_000_000, 500_000, 3, 2, 7_200, 1, False),
    "medium": Envelope(10_000_000, 2_000_000, 10, 4, 3_600, 2, False),
    "large": Envelope(40_000_000, 6_000_000, 30, 8, 1_800, 4, True),
}
CUSTOM = "custom"
FIELDS: tuple[str, ...] = tuple(field.name for field in fields(Envelope))

# (field, low, high, words): the CHECK ranges of brain.goals. None is no upper bound.
_RANGES: tuple[tuple[str, int, int | None, str], ...] = (
    ("total_budget", 1, 1_000_000_000, "the total budget"),
    ("day_budget", 1, None, "the day budget"),
    ("deadline_days", 1, 90, "the deadline in days"),
    ("sessions_per_day", 1, 24, "sessions per day"),
    ("min_interval_seconds", 60, None, "the interval between sessions in seconds"),
    ("jobs_per_day", 0, 20, "jobs per day"),
)


def problems(envelope: Envelope) -> list[str]:
    """What is wrong with ``envelope``, in words; empty when it fits the table's CHECKs."""
    found: list[str] = []
    for name, low, high, words in _RANGES:
        value = getattr(envelope, name)
        if not isinstance(value, int) or isinstance(value, bool):
            found.append(f"{words} must be a whole number")
        elif value < low or (high is not None and value > high):
            found.append(f"{words} must be at least {low}" + (f" and at most {high}" if high is not None else ""))
    if not isinstance(envelope.quiet_hours_ok, bool):
        found.append("quiet_hours_ok must be true or false")
    if not found and envelope.day_budget > envelope.total_budget:
        found.append("the day budget cannot be larger than the total budget")
    return found


def validate(envelope: Envelope) -> Envelope:
    """``envelope`` when it fits the table's CHECKs, else ``EnvelopeError`` listing every problem."""
    found = problems(envelope)
    if found:
        raise EnvelopeError("; ".join(found))
    return envelope


def envelope_for(preset: str, overrides: Mapping[str, Any] | None) -> tuple[str, Envelope]:
    """``(preset name, envelope)``: a preset as it is, or with ``overrides`` applied, which makes it ``custom``.

    ``custom`` itself needs every field in ``overrides``. Only the envelope's own fields can be overridden.
    """
    overrides = dict(overrides or {})
    unknown = sorted(set(overrides) - set(FIELDS))
    if unknown:
        raise EnvelopeError(f"not a limit of a goal: {', '.join(unknown)} (the limits are {', '.join(FIELDS)})")
    if preset == CUSTOM:
        missing = [name for name in FIELDS if name not in overrides]
        if missing:
            raise EnvelopeError(f"a custom envelope needs every limit; missing: {', '.join(missing)}")
        return CUSTOM, validate(Envelope(**overrides))
    if preset not in PRESETS:
        raise EnvelopeError(f"unknown preset {preset!r}: use {', '.join(PRESETS)} or {CUSTOM}")
    if not overrides:
        return preset, PRESETS[preset]
    return CUSTOM, validate(replace(PRESETS[preset], **overrides))
