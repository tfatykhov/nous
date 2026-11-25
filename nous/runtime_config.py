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

import json
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from nous.api.tool_loading import ToolLoadingConfig

logger = logging.getLogger(__name__)

# Keys used in nous_system.config table
_KEY_VECTOR_WEIGHT = "vector_weight"
_KEY_RRF_K = "rrf_k"
_KEY_CROSS_ENCODER_ENABLED = "cross_encoder_enabled"


class RuntimeConfig:
    """Singleton holding runtime-mutable configuration."""

    _instance: RuntimeConfig | None = None

    def __init__(self) -> None:
        self._overrides: dict[str, Any] = {}
        # F120: each agent's tool-loading setting, and why a stored one could not be read (it is Off then).
        self._tool_loading: dict[str, ToolLoadingConfig] = {}
        self._tool_loading_problem: dict[str, str] = {}

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

    # -- RRF K ----------------------------------------------------------------

    def get_rrf_k(self, settings: Any) -> int:
        """Resolve rrf_k: runtime override > env/settings > default."""
        if _KEY_RRF_K in self._overrides:
            return int(self._overrides[_KEY_RRF_K])
        return int(settings.rrf_k)

    def get_rrf_k_source(self, settings: Any) -> str:
        """Return the source of the current effective rrf_k."""
        if _KEY_RRF_K in self._overrides:
            return "runtime_override"
        if "rrf_k" in settings.model_fields_set:
            return "env_var"
        return "default"

    def set_rrf_k(self, value: int) -> None:
        """Set runtime override (call persist_to_db separately)."""
        self._overrides[_KEY_RRF_K] = value

    def clear_rrf_k(self) -> None:
        """Remove runtime override, falling back to settings."""
        self._overrides.pop(_KEY_RRF_K, None)

    # -- Cross-encoder enabled (F042) -----------------------------------------

    def get_cross_encoder_enabled(self, settings: Any) -> bool:
        """Resolve cross_encoder_enabled: runtime override > env/settings > default."""
        if _KEY_CROSS_ENCODER_ENABLED in self._overrides:
            return bool(self._overrides[_KEY_CROSS_ENCODER_ENABLED])
        return bool(settings.cross_encoder_enabled)

    def get_cross_encoder_enabled_source(self, settings: Any) -> str:
        """Return the source of the current effective cross_encoder_enabled."""
        if _KEY_CROSS_ENCODER_ENABLED in self._overrides:
            return "runtime_override"
        if "cross_encoder_enabled" in settings.model_fields_set:
            return "env_var"
        return "default"

    def set_cross_encoder_enabled(self, value: bool) -> None:
        """Set runtime override (call persist_to_db separately)."""
        self._overrides[_KEY_CROSS_ENCODER_ENABLED] = bool(value)

    def clear_cross_encoder_enabled(self) -> None:
        """Remove runtime override, falling back to settings."""
        self._overrides.pop(_KEY_CROSS_ENCODER_ENABLED, None)

    # -- Tool loading (F120) --------------------------------------------------

    def get_tool_loading(self, agent_id: str) -> ToolLoadingConfig:
        """The owner's tool-loading setting of ``agent_id``: Off until one is stored or set. A dict lookup, read at
        each model call."""
        from nous.api.tool_loading import OFF

        return self._tool_loading.get(agent_id, OFF)

    def tool_loading_problem(self, agent_id: str) -> str | None:
        """Why the stored setting of ``agent_id`` could not be read at start (it is Off), or None."""
        return self._tool_loading_problem.get(agent_id)

    def set_tool_loading(self, agent_id: str, config: ToolLoadingConfig) -> None:
        """Set ``agent_id``'s setting for the next model call (store it with ``persist_to_db`` first)."""
        self._tool_loading[agent_id] = config
        self._tool_loading_problem.pop(agent_id, None)

    async def _load_tool_loading(self, session: AsyncSession, agent_id: str) -> None:
        """Read ``agent_id``'s stored setting, and only that row. A row that cannot be read leaves it Off with one
        warning; nothing here raises."""
        from nous.api.tool_loading import config_key, parse_tool_loading

        try:
            row = (
                await session.execute(
                    text("SELECT value FROM nous_system.config WHERE key = :key"), {"key": config_key(agent_id)}
                )
            ).first()
        except Exception as exc:
            logger.warning("F120: the tool-loading setting could not be read (%s); it is off", type(exc).__name__)
            return
        if row is None:
            return
        config, problem = parse_tool_loading(row[0])
        self._tool_loading[agent_id] = config
        if problem is not None:
            self._tool_loading_problem[agent_id] = problem
            logger.warning("F120: the tool-loading setting is off: %s", problem)
        else:
            logger.info("Loaded runtime override: tool loading, mode %s", config.mode)

    # -- DB persistence -------------------------------------------------------

    async def load_from_db(self, session: AsyncSession, agent_id: str | None = None) -> None:
        """Load all persisted overrides from nous_system.config, and ``agent_id``'s tool-loading setting with a query
        of its own (F120), so a bad row of either never hides the other."""
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
                if key == _KEY_RRF_K and value is not None:
                    k = int(value)
                    if k > 0:
                        self._overrides[key] = k
                        logger.info("Loaded runtime override: %s = %s", key, k)
                if key == _KEY_CROSS_ENCODER_ENABLED and value is not None:
                    # asyncpg deserializes jsonb automatically; value is a
                    # Python bool here, not a string.
                    if isinstance(value, bool):
                        self._overrides[key] = value
                        logger.info("Loaded runtime override: %s = %s", key, value)
                    else:
                        logger.warning(
                            "Ignoring non-bool value for %s: %r (expected bool)",
                            key,
                            value,
                        )
        except Exception:
            logger.debug("nous_system.config table not available yet (normal on first run)")
        if agent_id is not None:
            await self._load_tool_loading(session, agent_id)

    async def persist_to_db(self, session: AsyncSession, key: str, value: Any) -> None:
        """Upsert a config value to nous_system.config."""
        await session.execute(
            text("""
                INSERT INTO nous_system.config (key, value)
                VALUES (:key, CAST(:value AS jsonb))
                ON CONFLICT (key) DO UPDATE SET value = CAST(:value AS jsonb)
            """),
            {"key": key, "value": json.dumps(value)},
        )
        await session.commit()
