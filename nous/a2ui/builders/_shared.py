"""F092: helpers shared by the surface builders."""

from __future__ import annotations

from uuid import UUID


def _validated_trace_id(value: object) -> str | None:
    """Require a UUID-shaped trace_id or none at all.

    A malformed trace_id would make `review.course_correct` write its
    outcome against nothing and silently no-op — better to refuse at push
    time, where the producing agent gets an actionable error.
    """
    if value is None or value == "":
        return None
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"trace_id must be a UUID, got {value!r}") from exc
