"""F092: typed builder DSL for A2UI surfaces.

Thin helpers that accumulate the flat component list; the enforcement is
``BuiltSurface.build()`` running the schema + structural validators, so a
builder that emits invalid A2UI fails its unit test, not production.

Surface ids and nonces are minted by the SurfaceService at persist time —
builders describe content only. Component helpers return plain dicts; only
properties actually used by the v1 builders are modeled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from .validator import (
    NOUS_CORE_CATALOG_ID,
    validate_envelope,
    validate_structure,
)


class SurfaceValidationError(ValueError):
    """Raised when a builder emits an invalid surface."""

    def __init__(self, errors: list[dict[str, Any]]):
        self.errors = errors
        super().__init__(f"invalid A2UI surface: {errors[:3]}")


@dataclass
class BuiltSurface:
    """Everything a surface is, minus the identity the service mints."""

    kind: str
    origin: str
    title: str
    catalog_id: str = NOUS_CORE_CATALOG_ID
    priority: int = 0
    allowed_actions: list[str] = field(default_factory=list)
    components: list[dict] = field(default_factory=list)
    data_model: dict = field(default_factory=dict)
    trace_id: str | None = None
    expires_in: timedelta | None = None

    def validate(self) -> None:
        """Schema + structural validation; raises SurfaceValidationError."""
        probe = {
            "version": "v1.0",
            "createSurface": {
                "surfaceId": "nous:probe:probe:000000",
                "catalogId": self.catalog_id,
                "components": self.components,
                "dataModel": self.data_model,
            },
        }
        errors = validate_envelope(probe)
        errors += validate_structure(self.components, self.allowed_actions)
        if errors:
            raise SurfaceValidationError(errors)


class Surface:
    """Accumulator used by builders: ``s = Surface(...); s.data(...); s.add(...)``."""

    def __init__(
        self,
        *,
        kind: str,
        origin: str,
        title: str,
        catalog_id: str = NOUS_CORE_CATALOG_ID,
        priority: int = 0,
        allowed_actions: list[str] | None = None,
        trace_id: str | None = None,
        expires_in: timedelta | None = None,
    ):
        self._built = BuiltSurface(
            kind=kind,
            origin=origin,
            title=title,
            catalog_id=catalog_id,
            priority=priority,
            allowed_actions=list(allowed_actions or []),
            trace_id=trace_id,
            expires_in=expires_in,
        )

    def data(self, model: dict) -> Surface:
        self._built.data_model = model
        return self

    def add(self, *components: dict) -> Surface:
        self._built.components.extend(components)
        return self

    def build(self) -> BuiltSurface:
        self._built.validate()
        return self._built


def _clean(props: dict) -> dict:
    return {k: v for k, v in props.items() if v is not None}


def event(name: str, context: dict | None = None) -> dict:
    return {"event": _clean({"name": name, "context": context or {}})}


def Text(id: str, text: Any, *, variant: str | None = None) -> dict:
    return _clean({"id": id, "component": "Text", "text": text, "variant": variant})


def Column(id: str, *, children: Any, justify: str | None = None, align: str | None = None) -> dict:
    return _clean({"id": id, "component": "Column", "children": children, "justify": justify, "align": align})


def Row(id: str, *, children: Any, justify: str | None = None, align: str | None = None) -> dict:
    return _clean({"id": id, "component": "Row", "children": children, "justify": justify, "align": align})


def List_(id: str, *, children: Any, direction: str | None = None) -> dict:
    return _clean({"id": id, "component": "List", "children": children, "direction": direction})


def Card(id: str, *, child: str) -> dict:
    return {"id": id, "component": "Card", "child": child}


def Divider(id: str, *, axis: str | None = None) -> dict:
    return _clean({"id": id, "component": "Divider", "axis": axis})


def Button(id: str, *, child: str, action: dict, variant: str | None = None) -> dict:
    # variant must be a LITERAL enum string: the basic catalog does not accept
    # a data binding here (the F092 spec's Appendix A example does, and is
    # invalid against the catalog it cites — builders know their options at
    # build time, so per-option buttons carry literal variants instead).
    return _clean({"id": id, "component": "Button", "child": child, "action": action, "variant": variant})


def TextField(id: str, *, label: Any, value: Any = None, variant: str | None = None) -> dict:
    return _clean({"id": id, "component": "TextField", "label": label, "value": value, "variant": variant})


def CheckBox(id: str, *, label: Any, value: Any) -> dict:
    return {"id": id, "component": "CheckBox", "label": label, "value": value}


def ApprovalPanel(
    id: str,
    *,
    title: Any,
    summary: Any = None,
    risk: Any = None,
    recommendation: Any = None,
) -> dict:
    return _clean(
        {
            "id": id,
            "component": "ApprovalPanel",
            "title": title,
            "summary": summary,
            "risk": risk,
            "recommendation": recommendation,
        }
    )


def ActionReviewCard(
    id: str,
    *,
    title: Any,
    did: Any,
    why: Any = None,
    cost: Any = None,
    compensation: Any = None,
) -> dict:
    return _clean(
        {
            "id": id,
            "component": "ActionReviewCard",
            "title": title,
            "did": did,
            "why": why,
            "cost": cost,
            "compensation": compensation,
        }
    )


def StatTile(id: str, *, label: Any, value: Any, delta: Any = None, intent: str | None = None) -> dict:
    return _clean(
        {
            "id": id,
            "component": "StatTile",
            "label": label,
            "value": value,
            "delta": delta,
            "intent": intent,
        }
    )


def KeyValueTable(id: str, *, rows: Any) -> dict:
    return {"id": id, "component": "KeyValueTable", "rows": rows}


def DecisionCard(
    id: str,
    *,
    decisionId: Any,
    description: Any,
    confidence: Any = None,
    stakes: Any = None,
    category: Any = None,
    outcome: Any = None,
) -> dict:
    return _clean(
        {
            "id": id,
            "component": "DecisionCard",
            "decisionId": decisionId,
            "description": description,
            "confidence": confidence,
            "stakes": stakes,
            "category": category,
            "outcome": outcome,
        }
    )


def ConfidenceMeter(id: str, *, value: Any) -> dict:
    return {"id": id, "component": "ConfidenceMeter", "value": value}


def MemoryGraph(id: str, *, nodes: Any, edges: Any, focusNodeId: Any = None) -> dict:
    return _clean(
        {"id": id, "component": "MemoryGraph", "nodes": nodes, "edges": edges, "focusNodeId": focusNodeId}
    )


def DagGraph(id: str, *, nodes: Any, edges: Any) -> dict:
    return _clean({"id": id, "component": "DagGraph", "nodes": nodes, "edges": edges})
