"""F118: the judge slot, what any judge takes and returns.

A judge turns one record into a verdict. The record (``JudgeInput``) is already bounded and redacted: the subject's
own fields, the claim it is compared with, and the evidence items. A judge has a ``name`` and an opaque ``version``
(an instruction version for a judge that asks a model, a model version for a classifier); a verdict is stored once per
subject, name and version. ``grade`` never raises; a cancellation passes through. ``PROVIDERS`` maps a provider name
to a factory that takes the settings and returns a judge, or None when it cannot be built.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from nous.config import Settings

# What a judge's ``status`` may be; the service adds ``skipped`` for a subject judged without a call.
JUDGE_STATUSES: tuple[str, ...] = ("ok", "error", "timeout")
# The label on every ``error`` and ``timeout`` verdict.
ERROR_LABEL = "judge_error"


@dataclass(frozen=True)
class Claim:
    """The grade a grader gave the subject, as it stood when the subject was handed to the judge."""

    grader: str
    grade: str
    note: str | None = None


@dataclass(frozen=True)
class JudgeInput:
    """One bounded, redacted record. ``subject`` holds the subject's own fields, ``evidence`` the items that were
    kept (each a dict of strings, numbers and booleans), ``evidence_dropped`` how many were left out to fit, and
    ``evidence_truncated`` whether anything was left out or cut."""

    kind: str
    subject_id: UUID
    subject: Mapping[str, Any]
    claim: Claim | None
    evidence: tuple[Mapping[str, Any], ...] = ()
    evidence_truncated: bool = False
    evidence_dropped: int = 0


@dataclass(frozen=True)
class JudgeOutput:
    """A judge's answer. ``status`` is one of ``JUDGE_STATUSES``; an ``error`` or ``timeout`` has the verdict
    ``cannot_tell``. Every other field is optional. ``tokens_in`` counts every kind of input token (uncached, read
    from a cache, written to it); ``calls`` is the number of model requests sent (None: the judge does not say, and
    one is assumed)."""

    status: str
    verdict: str
    confidence: float | None = None
    labels: tuple[str, ...] = ()
    reason: str | None = None
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    calls: int | None = None


def failed(status: str, model: str | None = None, **counts: int | None) -> JudgeOutput:
    """The output of a judge that gave no verdict (``error`` or ``timeout``); ``counts`` are its tokens and calls."""
    return JudgeOutput(status=status, verdict="cannot_tell", labels=(ERROR_LABEL,), model=model, **counts)


@runtime_checkable
class Judge(Protocol):
    """What the service calls. ``grade`` never raises, returns within about ``timeout`` seconds and sends at most
    ``max_calls`` model requests (None: no bound)."""

    name: str
    version: str

    async def grade(self, record: JudgeInput, *, timeout: float, max_calls: int | None = None) -> JudgeOutput: ...


PROVIDERS: dict[str, Callable[[Settings], Judge | None]] = {}


def register_provider(name: str, factory: Callable[[Settings], Judge | None]) -> None:
    """Make a provider available under ``name`` (a later registration replaces an earlier one)."""
    PROVIDERS[name] = factory
