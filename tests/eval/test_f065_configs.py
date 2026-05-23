"""F065 phase 4 — eval configs registry guard.

These configs land dormant in `nous_eval._DEFAULT_CONFIGS` so the F051
matrix runner can be invoked as:

    python -m nous_eval.retrieval --configs baseline,f065_penalty_on

without anyone needing to remember which Settings flag to flip. The
tests pin both the registry entry AND the flag name — typos in the
flag name would otherwise only fail at run time inside a Docker'd
eval DB.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.eval

try:
    from nous.config import Settings
    from nous_eval.retrieval import _DEFAULT_CONFIGS
except ImportError:
    pytest.skip(
        "nous_eval.retrieval (+deps) not yet available",
        allow_module_level=True,
    )


class TestF065PenaltyConfig:
    def test_config_registered(self) -> None:
        assert "f065_penalty_on" in _DEFAULT_CONFIGS

    def test_config_flips_inferred_edge_penalty_to_0_7(self) -> None:
        cfg = _DEFAULT_CONFIGS["f065_penalty_on"]
        assert cfg.flags == {"graph_inferred_edge_penalty": 0.7}

    def test_flag_applies_to_settings(self) -> None:
        """Flag name MUST match a real Settings field; pydantic's model_copy
        with update= validates unknown keys."""
        cfg = _DEFAULT_CONFIGS["f065_penalty_on"]
        base = Settings()
        # Confirm baseline default first — if the production default ever
        # changes to 0.7, this guard wakes us up so we can switch the eval
        # config to a new candidate value.
        assert base.graph_inferred_edge_penalty == 1.0
        overridden = base.model_copy(update=cfg.flags)
        assert overridden.graph_inferred_edge_penalty == 0.7


class TestF065AutosurfaceConfig:
    def test_config_registered(self) -> None:
        assert "f065_autosurface_on" in _DEFAULT_CONFIGS

    def test_config_flips_autosurface_flag(self) -> None:
        cfg = _DEFAULT_CONFIGS["f065_autosurface_on"]
        assert cfg.flags == {"graph_hub_autosurface_enabled": True}

    def test_flag_applies_to_settings(self) -> None:
        cfg = _DEFAULT_CONFIGS["f065_autosurface_on"]
        base = Settings()
        assert base.graph_hub_autosurface_enabled is False
        overridden = base.model_copy(update=cfg.flags)
        assert overridden.graph_hub_autosurface_enabled is True
