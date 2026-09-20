"""F084 write-path adjudication features must stay land-dark by default.

Regression guard for a P1 caught in review: docker-compose.yml defaulted
NOUS_EXTRACTION_ENUMERATIVE_ENABLED to `true`, so every user running the
documented stack with no override silently opted into the experimental
background-LLM extractor -- contradicting the application default
(Settings.extraction_enumerative_enabled = False) and .env.example, and
changing which facts get persisted plus incurring extra model calls.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from nous.config import Settings

_REPO_ROOT = Path(__file__).resolve().parents[1]
_COMPOSE = _REPO_ROOT / "docker-compose.yml"

#: env vars for features documented as land-dark (default OFF).
_LAND_DARK_SWITCHES = ["NOUS_EXTRACTION_ENUMERATIVE_ENABLED"]


def _compose_default(var: str) -> str | None:
    """Return the `:-default` used by docker-compose for `var`, if present."""
    text = _COMPOSE.read_text()
    m = re.search(rf"^\s*-\s*{var}=\$\{{{var}:-([^}}]*)\}}\s*$", text, re.M)
    return None if m is None else m.group(1)


class TestLandDarkDefaults:
    @pytest.mark.parametrize("var", _LAND_DARK_SWITCHES)
    def test_compose_does_not_enable_a_land_dark_switch(self, var):
        default = _compose_default(var)
        if default is None:
            pytest.skip(f"{var} is not passed through in docker-compose.yml")
        assert default.strip().lower() in {"false", "0", ""}, (
            f"docker-compose defaults {var} to {default!r}, which opts every "
            f"`docker compose up` into a land-dark feature"
        )

    def test_compose_default_matches_the_application_default(self):
        default = _compose_default("NOUS_EXTRACTION_ENUMERATIVE_ENABLED")
        if default is None:
            pytest.skip("not passed through")
        app_default = Settings.model_fields["extraction_enumerative_enabled"].default
        assert (default.strip().lower() == "true") is bool(app_default)

    def test_the_cap_is_still_passed_through(self):
        """Opting in must still get the tuned cap -- the point of the PR."""
        assert _compose_default("NOUS_ENUMERATIVE_MAX_FACTS_PER_EPISODE") == "20"
