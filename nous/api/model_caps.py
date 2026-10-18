"""What a model accepts, decided by its version rather than by the word "haiku".

All of Nous's background code was written for models that do not think unless
asked: a request with no ``thinking`` field got a plain answer. Claude Haiku 5.5
thinks by default, so the same request spends much of a small ``max_tokens``
on thinking. It also accepts ``thinking: {"type": "disabled"}``, adaptive
thinking and the effort parameter, all of which Haiku 4.5 rejects with a 400.
Sonnet 5.5 and Opus 5.5 reject ``{"type": "disabled"}``, so it is never sent to
them.
"""

from __future__ import annotations

import re
from typing import Any

# claude-haiku-4-5, claude-haiku-4-5-20251001, claude-haiku-5-5
_FAMILY_FIRST = re.compile(r"claude-haiku-(\d+)")
# claude-3-5-haiku-20241022, claude-3-haiku-20240307
_VERSION_FIRST = re.compile(r"claude-(\d+)(?:-\d+)?-haiku")


def _haiku_major(model: str) -> int | None:
    """The major version of a Haiku model id, or None when the id names no version."""
    lowered = model.lower()
    match = _FAMILY_FIRST.search(lowered) or _VERSION_FIRST.search(lowered)
    return int(match.group(1)) if match else None


def is_legacy_haiku(model: str) -> bool:
    """True for a Haiku below major version 5: no thinking, no adaptive thinking, no effort parameter.

    ``claude-haiku-4-5``, ``claude-haiku-4-5-20251001`` and ``claude-3-5-haiku-...``
    are legacy; ``claude-haiku-5-5`` is not. A Haiku id that names no version is
    treated as legacy, which keeps the runner's behavior for it as it was before
    Haiku 5: no ``thinking`` and no effort sent.
    """
    if "haiku" not in model.lower():
        return False
    major = _haiku_major(model)
    return major is None or major < 5


def disables_thinking_by_default(model: str) -> bool:
    """True for a Haiku of major version 5 or later.

    It is the only family known both to think when a request has no ``thinking``
    field and to accept ``{"type": "disabled"}``. Nous's background requests were
    written for models that do not think unless asked, so they get thinking
    disabled on these models instead of losing their token budget to it.
    """
    if "haiku" not in model.lower():
        return False
    major = _haiku_major(model)
    return major is not None and major >= 5


def with_default_thinking(payload: dict[str, Any]) -> dict[str, Any]:
    """``payload`` itself, or a copy with ``thinking`` disabled when its model would otherwise think unasked.

    A payload that already has a ``thinking`` key is returned unchanged, so an
    explicit choice (adaptive, enabled, disabled) always stands. So is a payload
    for any model other than Haiku 5 and later: those requests stay byte-identical.
    """
    if "thinking" in payload or not disables_thinking_by_default(payload.get("model") or ""):
        return payload
    return {**payload, "thinking": {"type": "disabled"}}
