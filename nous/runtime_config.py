"""Mutable runtime configuration — loaded from DB, updated via API.

Settings (pydantic-settings) is immutable after startup and handles env vars /
defaults.  RuntimeConfig holds live overrides that can change at runtime via
the /admin API.  Resolution order for each knob:

  1. Explicit caller parameter (per-call override)
  2. Runtime override set via API  (stored here + persisted to nous_system.config)
  3. Env var / Settings default
  4. Hardcoded fallback
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Keys used in nous_system.config table
_KEY_VECTOR_WEIGHT = "vector_weight"


class RuntimeConfig:
    """Singleton holding runtime-mutable configuration."""

    _instance: RuntimeConfig | None = None

    def __init__(self) -> None:
        self._overrides: dict[str, Any] = {}

    @classmethod
    def get(cls) -> RuntimeConfig:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset singleton (for tests)."""
        cls._instance = None

    # -- Vector weight --------------------------------------------------------

    def get_vector_weight(self, settings: Any) -> float:
        """Resolve vector_weight: runtime override > env/settings > default."""
        if _KEY_VECTOR_WEIGHT in self._overrides:
            return float(self._overrides[_KEY_VECTOR_WEIGHT])
        return float(settings.vector_weight)

    def get_vector_weight_source(self, settings: Any) -> str:
        """Return the source of the current effective vector_weight."""
        if _KEY_VECTOR_WEIGHT in self._overrides:
            return "runtime_override"
        if "vector_weight" in settings.model_fields_set:
            return "env_var"
        return "default"

    def set_vector_weight(self, value: float) -> None:
        """Set runtime override (call persist_to_db separately)."""
        self._overrides[_KEY_VECTOR_WEIGHT] = value

    def clear_vector_weight(self) -> None:
        """Remove runtime override, falling back to settings."""
        self._overrides.pop(_KEY_VECTOR_WEIGHT, None)

    # -- DB persistence -------------------------------------------------------

    async def load_from_db(self, session: AsyncSession) -> None:
        """Load all persisted overrides from nous_system.config."""
        try:
            result = await session.execute(
                text("SELECT key, value FROM nous_system.config")
            )
            for row in result:
                key, value = row[0], row[1]
                if key == _KEY_VECTOR_WEIGHT and value is not None:
                    w = float(value)
                    if 0.0 <= w <= 1.0:
                        self._overrides[key] = w
                        logger.info("Loaded runtime override: %s = %s", key, w)
        except Exception:
            logger.debug("nous_system.config table not available yet (normal on first run)")

    async def persist_to_db(self, session: AsyncSession, key: str, value: Any) -> None:
        """Upsert a config value to nous_system.config."""
        await session.execute(
            text("""
                INSERT INTO nous_system.config (key, value)
                VALUES (:key, CAST(:value AS jsonb))
                ON CONFLICT (key) DO UPDATE SET value = CAST(:value AS jsonb)
            """),
            {"key": key, "value": str(value)},
        )
        await session.commit()
