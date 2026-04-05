"""F036: Prompt cache optimization — detects cache invalidations between API calls."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict, dataclass, field

logger = logging.getLogger(__name__)


def _hash(text: str) -> str:
    """SHA256 truncated to 16 hex chars."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


@dataclass
class CacheHashState:
    """Hashes from one API call for comparison."""
    static_hash: str = ""
    semi_stable_hash: str = ""
    dynamic_hash: str = ""
    tools_hash: str = ""
    model_hash: str = ""


@dataclass
class CacheBreakInfo:
    """Detected cache invalidation between consecutive API calls."""
    components_changed: list[str] = field(default_factory=list)
    estimated_tokens_lost: int = 0
    previous_hashes: dict[str, str] = field(default_factory=dict)
    current_hashes: dict[str, str] = field(default_factory=dict)


class CacheBreakDetector:
    """Detects prompt cache invalidations between consecutive API calls.

    Compares SHA256 hashes of system prompt tiers, tool schemas, and model
    between consecutive requests. Only reports breaks in stable/semi-stable
    components (dynamic changes every turn by design).
    """

    def __init__(self) -> None:
        self._previous: CacheHashState | None = None

    def check(
        self,
        static_text: str,
        semi_stable_text: str,
        dynamic_text: str,
        tools_json: str,
        model: str,
    ) -> CacheBreakInfo | None:
        """Compare current request hashes against previous.

        Returns CacheBreakInfo if a cache break is detected in stable
        components, None otherwise. Dynamic text changes are expected
        and not reported as breaks.
        """
        current = CacheHashState(
            static_hash=_hash(static_text),
            semi_stable_hash=_hash(semi_stable_text),
            dynamic_hash=_hash(dynamic_text),
            tools_hash=_hash(tools_json),
            model_hash=_hash(model),
        )

        if self._previous is None:
            self._previous = current
            return None  # First call, no comparison

        old_previous = self._previous
        self._previous = current

        changed: list[str] = []
        tokens_lost = 0

        if current.static_hash != old_previous.static_hash:
            changed.append("static_identity")
            tokens_lost += len(static_text) // 4
        if current.semi_stable_hash != old_previous.semi_stable_hash:
            changed.append("semi_stable_context")
            tokens_lost += len(semi_stable_text) // 4
        if current.tools_hash != old_previous.tools_hash:
            changed.append("tools")
            tokens_lost += len(tools_json) // 4
        if current.model_hash != old_previous.model_hash:
            changed.append("model")

        if not changed:
            return None

        logger.info(
            "F036: Cache break detected — changed: %s, tokens_lost: %d",
            changed, tokens_lost,
        )

        return CacheBreakInfo(
            components_changed=changed,
            estimated_tokens_lost=tokens_lost,
            previous_hashes=asdict(old_previous),
            current_hashes=asdict(current),
        )

    def last_semi_stable_hash(self) -> str | None:
        """Return the semi-stable hash from the previous API call, or None."""
        if self._previous is None:
            return None
        return self._previous.semi_stable_hash

    def reset(self) -> None:
        """Reset state (e.g., on session end)."""
        self._previous = None
