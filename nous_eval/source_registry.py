"""Source registry — resolves the qrel source matrix at harness startup.

Sources (longmemeval, ai_hand_labeled, probes, silver_episodes,
synthetic_haiku) are declared in ``nous/eval/config/sources.yaml``. At
startup the registry:

1. Substitutes ``${NOUS_EVAL_FIXTURES_DIR}`` + resolves each source's path.
2. Applies CLI filters (``--sources``, ``--exclude``, ``--gate-only``,
   ``--include-unreviewed``).
3. Silently skips sources whose path is missing, recording a human-readable
   ``_skip_reason`` per source so the report can explain the drop.

A source that was skipped survives in the registry as a ``ResolvedSource``
with ``available=False``; this keeps the gate-decision logic aware of the
operator's intent even when the fixture dir is incomplete.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# Default source YAML lives next to this module
DEFAULT_SOURCES_YAML = Path(__file__).parent / "config" / "sources.yaml"


# Fallback source list used when sources.yaml is missing. Keeps the harness
# functional in the public repo checkout before the private fixtures land.
_BUILTIN_SOURCES: list[dict[str, object]] = [
    {
        "name": "longmemeval",
        "path": "${NOUS_EVAL_FIXTURES_DIR}/qrels_longmemeval.jsonl",
        "enabled_by_default": True,
        "gate_eligible": True,
        "requires_fixtures_dir": True,
        "description": "Stratified 20-Q LongMemEval_S subset",
    },
    {
        "name": "ai_hand_labeled",
        "path": "${NOUS_EVAL_FIXTURES_DIR}/qrels_ai_hand.jsonl",
        "enabled_by_default": True,
        "gate_eligible": False,
        "requires_fixtures_dir": True,
        "review_filter": "reviewed_by != null",
        "description": "AI-drafted hand-labeled qrels against seeded corpus",
    },
    {
        "name": "probes",
        "path": "tests/fixtures/eval_probes.jsonl",
        "enabled_by_default": True,
        "gate_eligible": True,
        "requires_fixtures_dir": False,
        "description": "Auto-generated deterministic probes",
    },
    {
        "name": "silver_episodes",
        "path": "${NOUS_EVAL_FIXTURES_DIR}/qrels_silver.jsonl",
        "enabled_by_default": True,
        "gate_eligible": False,
        "requires_fixtures_dir": True,
        "description": "Click-model-style silver labels mined from heart.episodes",
    },
    {
        "name": "synthetic_haiku",
        "path": "${NOUS_EVAL_FIXTURES_DIR}/qrels_synthetic.jsonl",
        "enabled_by_default": False,
        "gate_eligible": False,
        "requires_fixtures_dir": True,
        "description": "Haiku-reverse-generated (informational only, circular risk)",
    },
]


class SourceSpec(BaseModel):
    """Declarative source metadata loaded from sources.yaml (or builtin)."""

    name: str
    path: str
    enabled_by_default: bool = True
    gate_eligible: bool = False
    requires_fixtures_dir: bool = False
    review_filter: str | None = None
    description: str = ""

    @property
    def key(self) -> str:
        return self.name


@dataclass(frozen=True)
class ResolvedSource:
    """A source after env-var substitution + existence check.

    ``gate_eligible_effective`` lets the report distinguish sources that are
    structurally gate-eligible from sources that are only gate-eligible
    because ``--include-unreviewed`` promoted them. ``_skip_reason`` is
    populated when the source was resolved but the file isn't present —
    lets the report explain the drop instead of silently omitting it.
    """

    spec: SourceSpec
    resolved_path: Path
    available: bool
    gate_eligible_effective: bool
    include_unreviewed: bool = False
    _skip_reason: str | None = None


@dataclass
class SourceRegistry:
    """Loads source specs, applies CLI filters, and exposes resolved sources."""

    specs: list[SourceSpec] = field(default_factory=list)
    fixtures_dir: Path | None = None

    @classmethod
    def load(
        cls,
        yaml_path: Path | None = None,
        fixtures_dir: Path | None = None,
    ) -> SourceRegistry:
        """Load the source specs from ``sources.yaml`` or the builtin list.

        ``fixtures_dir`` is stashed on the registry so ``resolve()`` can
        substitute ``${NOUS_EVAL_FIXTURES_DIR}``. Pass ``None`` for smoke
        mode — sources requiring fixtures will then resolve to
        ``available=False``.
        """
        raw_sources: list[dict[str, object]] = []
        path = yaml_path or DEFAULT_SOURCES_YAML
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
                raw_sources = list(data.get("sources", []))
                if isinstance(raw_sources, dict):
                    # Allow dict-of-dicts YAML layout: {name: {path: ...}}
                    raw_sources = [
                        {"name": n, **(v if isinstance(v, dict) else {})}
                        for n, v in raw_sources.items()
                    ]
            except Exception:
                logger.exception("Failed to parse %s; using builtin sources", path)
                raw_sources = list(_BUILTIN_SOURCES)
        else:
            logger.debug("sources.yaml not found at %s; using builtin sources", path)
            raw_sources = list(_BUILTIN_SOURCES)

        specs = [SourceSpec.model_validate(r) for r in raw_sources]
        return cls(specs=specs, fixtures_dir=fixtures_dir)

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve(
        self,
        only: list[str] | None = None,
        exclude: list[str] | None = None,
        gate_only: bool = False,
        include_unreviewed: bool = False,
    ) -> list[ResolvedSource]:
        """Resolve specs to ``ResolvedSource`` objects after applying filters.

        Args:
            only: CLI ``--sources`` whitelist; overrides ``enabled_by_default``.
            exclude: CLI ``--exclude`` blacklist; subtracts from the resolved set.
            gate_only: CLI ``--gate-only``; keep only ``gate_eligible: True`` sources.
            include_unreviewed: CLI ``--include-unreviewed``; ignore row-level
                review_filter when scoring.

        Sources requested in ``only`` that aren't declared error out — typos
        shouldn't silently pass.
        """
        spec_by_name = {s.name: s for s in self.specs}

        # Detect unknown names requested via --sources
        if only is not None:
            unknown = [n for n in only if n not in spec_by_name]
            if unknown:
                raise ValueError(
                    f"Unknown source(s): {unknown}. "
                    f"Known: {sorted(spec_by_name)}"
                )

        exclude_set = set(exclude or [])

        selected: list[SourceSpec] = []
        for spec in self.specs:
            if only is not None:
                if spec.name not in only:
                    continue
            else:
                if not spec.enabled_by_default:
                    continue
            if spec.name in exclude_set:
                continue
            if gate_only and not spec.gate_eligible:
                continue
            selected.append(spec)

        resolved: list[ResolvedSource] = []
        for spec in selected:
            path_str = self._substitute_env(spec.path)
            resolved_path = Path(path_str)
            available, skip_reason = self._check_available(spec, resolved_path)

            # gate_eligible_effective: structurally gate-eligible AND either
            # has no review_filter OR operator explicitly bypassed it.
            effective = spec.gate_eligible
            if effective and spec.review_filter and not include_unreviewed:
                # The review_filter applies row-by-row at qrels_loader time;
                # availability at source level is unchanged.
                pass  # Still gate-eligible at source level.
            resolved.append(
                ResolvedSource(
                    spec=spec,
                    resolved_path=resolved_path,
                    available=available,
                    gate_eligible_effective=effective,
                    include_unreviewed=include_unreviewed,
                    _skip_reason=skip_reason,
                )
            )

        return resolved

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _substitute_env(self, template: str) -> str:
        """Replace ``${NOUS_EVAL_FIXTURES_DIR}`` + normal env vars.

        Uses ``self.fixtures_dir`` (passed via load()) as the canonical
        value — falls back to os.environ if not set, to keep legacy callers
        working.
        """
        if self.fixtures_dir is not None:
            template = template.replace(
                "${NOUS_EVAL_FIXTURES_DIR}", str(self.fixtures_dir)
            )
        return os.path.expandvars(template)

    def _check_available(
        self, spec: SourceSpec, resolved_path: Path
    ) -> tuple[bool, str | None]:
        """Return (available, skip_reason).

        Source is *available* iff:
          1. It does not require fixtures_dir, OR fixtures_dir is set, AND
          2. The resolved path exists on disk.
        Otherwise it's silently skipped with an explanation.
        """
        if spec.requires_fixtures_dir and self.fixtures_dir is None:
            return False, "fixtures_dir unset (smoke mode)"
        if not resolved_path.exists():
            return False, f"file not found: {resolved_path}"
        return True, None

    # ------------------------------------------------------------------
    # Summary for reporting
    # ------------------------------------------------------------------

    def summary(self, resolved: list[ResolvedSource]) -> dict[str, object]:
        """Build a serializable summary for the report header."""
        return {
            "total_specs": len(self.specs),
            "resolved": len(resolved),
            "available": sum(1 for r in resolved if r.available),
            "skipped": [
                {"name": r.spec.name, "reason": r._skip_reason}
                for r in resolved
                if not r.available
            ],
            "gate_eligible": [
                r.spec.name for r in resolved if r.gate_eligible_effective and r.available
            ],
        }
