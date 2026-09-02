"""F092.1: the compact catalog property summary fed to the compose prompt.

The compose prompt used to name the allowed components and nothing else — no
property was ever shown to the model, so every component's schema was guessed.
Measured 2026-08-30 over 6 live repair rounds: 4 failed on property errors the
model could not have avoided ("'decisionId' is a required property",
"Unevaluated properties are not allowed ('subtitle' was unexpected)").

This derives that missing summary from the vendored catalogs — names only, no
types, no descriptions, no nested schemas. F092 Phase 3 sized it at ~800 tokens
"not full JSON Schema"; the two catalogs together are 69 KB, so the summary is
built ONCE and cached (``lru_cache``) rather than re-derived per compose call.
"""

from __future__ import annotations

import json
from functools import lru_cache

from .grammar import ALLOWED_COMPONENTS
from .validator import CATALOGS_DIR

# Rough char-per-token estimate — the summary is ASCII identifier soup, so 4
# chars/token is close enough to keep the budget honest without a tokenizer.
_CHARS_PER_TOKEN = 4
_TOKEN_BUDGET = 1200

# Carried by every component and therefore listed once in the preamble instead
# of 22 times in the body: "id"/"component" are the envelope's own contract,
# "weight" is ComponentCommon.
_UNIVERSAL_PROPS = frozenset({"component", "weight"})

# Most-used first. The budget trim drops optional-property names from the TAIL,
# so this order is what "least-used components" means; required properties are
# never dropped, since those are what actually breaks composition.
_COMPONENT_ORDER = (
    # skeleton — every micro-app has these
    "AppHeader",
    "Section",
    "AppFooter",
    "StatRow",
    "StatTile",
    # F096 report vocabulary — what a report app is made of
    "MetricCard",
    "ScoreCard",
    "DeltaList",
    "DataTable",
    "ChipRow",
    # layout + text
    "Column",
    "Row",
    "Text",
    "List",
    "Card",
    "Divider",
    # structured data display
    "KeyValueTable",
    "Timeline",
    "DecisionCard",
    "ConfidenceMeter",
    # rarer
    "Tabs",
    "Modal",
    "Icon",
    "Image",
    "Button",
    "MemoryGraph",
    "DagGraph",
    "ApprovalPanel",
    "ActionReviewCard",
)

_PREAMBLE = """\
Component properties (use ONLY these; required ones are mandatory):
Every component object also carries "id" and "component", and accepts an
optional numeric "weight" — neither is repeated below. A property that is not
listed for a component DOES NOT EXIST on it: do not invent "title",
"subtitle" or "description" on components that have no such property, and do
not omit a required one.
"""


def _component_schemas() -> dict[str, dict]:
    """Component name -> schema, merged across the two vendored catalogs.

    ``allOf`` composition (Button) is flattened by merging the inline
    subschemas; ``$ref`` branches into common_types.json are skipped — they
    contribute renderer-side plumbing (``checks``), not props the composer
    should be spending prompt tokens on.
    """
    schemas: dict[str, dict] = {}
    for path in (
        CATALOGS_DIR / "nous_core" / "catalog.json",
        CATALOGS_DIR / "basic" / "catalog.json",
    ):
        catalog = json.loads(path.read_text(encoding="utf-8"))
        for name, schema in (catalog.get("components") or {}).items():
            schemas.setdefault(name, _flatten(schema))
    return schemas


def _flatten(schema: dict) -> dict:
    properties: dict = dict(schema.get("properties") or {})
    required: list[str] = list(schema.get("required") or [])
    for branch in schema.get("allOf") or []:
        if not isinstance(branch, dict):
            continue
        properties.update(branch.get("properties") or {})
        required += [r for r in (branch.get("required") or []) if r not in required]
    return {"properties": properties, "required": required}


def _line(name: str, schema: dict, *, with_optional: bool) -> str:
    props = list(schema["properties"])
    required = [p for p in schema["required"] if p not in _UNIVERSAL_PROPS]
    optional = [p for p in props if p not in _UNIVERSAL_PROPS and p not in schema["required"]]
    parts = [f"- {name}: required " + (", ".join(required) if required else "(none)")]
    if with_optional and optional:
        parts.append("optional " + ", ".join(optional))
    return "; ".join(parts)


@lru_cache(maxsize=1)
def catalog_property_summary() -> str:
    """The prompt block. Cached — the catalogs are read once per process."""
    schemas = _component_schemas()
    # Ordered allowlist, with any allowed component missing from the curated
    # order appended (so adding one to ALLOWED_COMPONENTS can never silently
    # drop it out of the prompt).
    names = [n for n in _COMPONENT_ORDER if n in ALLOWED_COMPONENTS]
    names += sorted(ALLOWED_COMPONENTS - set(names))
    names = [n for n in names if n in schemas]

    with_optional = set(names)
    while True:
        body = "\n".join(_line(n, schemas[n], with_optional=n in with_optional) for n in names)
        text = _PREAMBLE + body + "\n"
        if len(text) <= _TOKEN_BUDGET * _CHARS_PER_TOKEN:
            return text
        # Over budget: drop optional names from the least-used component that
        # still lists them. Required props are never dropped.
        trimmable = [n for n in reversed(names) if n in with_optional]
        if not trimmable:
            return text
        with_optional.discard(trimmable[0])


__all__ = ["catalog_property_summary"]
