"""EvalSettings — pydantic-settings class for the F051 retrieval eval harness.

Reads ``NOUS_EVAL_*`` env vars (distinct from the main ``NOUS_*`` prefix) so
the eval harness can point at a separate Postgres (default ``localhost:5433``)
without colliding with the production ``NOUS_DB_*`` / unprefixed ``DB_*``
connection fields.

The ``db_url`` property returns an asyncpg DSN in the same shape as
``nous.config.Settings.db_url`` so ``Database(eval_settings)`` works without
conditionals on the caller side.
"""

from __future__ import annotations

import logging
import warnings
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class EvalSettings(BaseSettings):
    """Configuration for the F051 retrieval eval harness.

    All fields are ``NOUS_EVAL_*``-prefixed; ``env_file=".env"`` is honored for
    local dev parity with the main Settings. ``extra="ignore"`` keeps the
    class tolerant of unrelated ``NOUS_EVAL_*`` env vars that may get added
    later without a code change here.
    """

    model_config = SettingsConfigDict(
        env_prefix="NOUS_EVAL_",
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )

    # Eval DB connection (distinct from main Settings DB connection fields)
    db_host: str = "localhost"
    db_port: int = 5433
    db_user: str = "nous"
    db_password: str = "nous_eval"
    db_name: str = "nous_eval"
    db_pool_size: int = 5
    db_max_overflow: int = 2

    # Must match the agent_id embedded in the ingested corpus so WHERE agent_id
    # filters hit rows. Defaults to "nous-eval-corpus" — ingest.py writes the
    # corpus with the same value.
    agent_id: str = "nous-eval-corpus"

    # Database.__init__ passes this as ``echo=...``; leave at "info" for silent runs.
    log_level: str = "info"

    # Fixture management
    fixtures_dir: Path | None = None
    fixture_version: str = "v2026-Q2"
    top_k: int = 10
    report_dir: Path = Path("reports")
    run_history_enabled: bool = True
    run_history_insert_timeout_s: float = 5.0

    # F050 gate thresholds
    f050_gate_threshold: float = 0.07
    f050_gate_max_single_regression: float = 0.03
    f050_gate_require_majority_positive: bool = True

    # Git SHA override (otherwise resolved from git at runtime).
    # Useful for reproducing a historical run.
    git_sha_override: str = ""

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("fixtures_dir", mode="before")
    @classmethod
    def _empty_to_none(cls, v: object) -> Path | None:
        """Treat empty-string / None / literal 'None' as unset.

        Makes ``NOUS_EVAL_FIXTURES_DIR=`` in .env behave the same as leaving
        the var out entirely, which is the smoke-mode trigger.
        """
        if v is None or v == "" or v == "None":
            return None
        return Path(v) if not isinstance(v, Path) else v

    @field_validator("report_dir", mode="before")
    @classmethod
    def _coerce_report_dir(cls, v: object) -> Path:
        """Normalize report_dir to a Path; env var comes in as str."""
        if isinstance(v, Path):
            return v
        return Path(str(v))

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------

    @property
    def smoke_mode(self) -> bool:
        """True when no fixtures dir is configured or the configured dir is missing.

        In smoke mode only sources with ``requires_fixtures_dir: false`` load;
        gate decisions are flagged ``N/A — insufficient sources``.
        """
        if self.fixtures_dir is None:
            return True
        return not self.fixtures_dir.exists()

    @property
    def db_url(self) -> str:
        """asyncpg DSN matching main Settings.db_url contract.

        Allows ``Database(eval_settings)`` to work without conditionals —
        ``Database.__init__`` reads ``settings.db_url``.
        """
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    # ------------------------------------------------------------------
    # Security hygiene
    # ------------------------------------------------------------------

    def warn_if_default_password(self) -> None:
        """Emit a ``UserWarning`` if ``db_password`` is still ``nous_eval``.

        Port 5433 binds to ``127.0.0.1`` only (see docker-compose.yml), so the
        default-password risk is limited to local multi-user machines, but the
        operator gets a reminder nonetheless.
        """
        if self.db_password == "nous_eval":
            warnings.warn(
                "EvalSettings using default db_password='nous_eval'. "
                "For shared machines, set NOUS_EVAL_DB_PASSWORD to a stronger value.",
                UserWarning,
                stacklevel=2,
            )
