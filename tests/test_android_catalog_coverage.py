"""F097 §8.3 — the native renderer's prop-level coverage ratchet.

Runs in the always-on CI, so a catalog change turns required CI red until
``android/catalog-coverage.json`` acknowledges it. A name-only check would
have missed F096, which added props to components that were already
registered; this walks ``properties`` / ``allOf`` / ``anyOf`` / ``oneOf``
and resolves ``$ref`` into ``common_types.json`` so ``checks`` and
``accessibility`` (mixed in through ``Checkable`` / ``AccessibilityAttributes``)
count as props too.

The JUnit side (``CoverageManifestTest``) asserts the Kotlin registry agrees
with this file, so the two checks together close the loop.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CATALOGS = ROOT / "nous" / "a2ui" / "catalogs"
MANIFEST = ROOT / "android" / "catalog-coverage.json"

_CATALOG_NAMES = {"basic": "basic", "nous_core": "nous_core"}


def _common_defs() -> dict:
    return json.loads((CATALOGS / "json" / "common_types.json").read_text(encoding="utf-8")).get("$defs", {})


def _resolve(schema: dict, defs: dict, seen: set[str]) -> dict:
    """Follow one level of $ref into common_types (cycle-safe)."""
    ref = schema.get("$ref")
    if not ref or "#/$defs/" not in ref:
        return schema
    name = ref.split("#/$defs/")[-1]
    if name in seen:
        return {}
    seen.add(name)
    return defs.get(name, {})


def component_props(schema: dict, defs: dict, seen: set[str] | None = None) -> dict[str, dict]:
    """Every prop name → its schema, across properties/allOf/anyOf/oneOf/$ref."""
    seen = set() if seen is None else seen
    schema = _resolve(schema, defs, seen)
    out: dict[str, dict] = {}
    for name, sub in (schema.get("properties") or {}).items():
        out.setdefault(name, sub)
    for key in ("allOf", "anyOf", "oneOf"):
        for sub in schema.get(key) or []:
            for name, s in component_props(sub, defs, set(seen)).items():
                out.setdefault(name, s)
    return out


def enum_values(prop_schema: dict, defs: dict) -> list[str]:
    s = _resolve(prop_schema, defs, set())
    if "enum" in s:
        return list(s["enum"])
    for key in ("anyOf", "oneOf"):
        for sub in s.get(key) or []:
            vals = enum_values(sub, defs)
            if vals:
                return vals
    return []


def load_catalog(name: str) -> dict:
    return json.loads((CATALOGS / name / "catalog.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


@pytest.mark.parametrize("catalog", sorted(_CATALOG_NAMES))
def test_every_component_and_prop_is_covered(catalog: str, manifest: dict) -> None:
    defs = _common_defs()
    cat = load_catalog(catalog)
    section = manifest[catalog]
    missing: list[str] = []
    for comp_name, schema in cat["components"].items():
        entry = section.get(comp_name)
        if entry is None:
            missing.append(f"{catalog}.{comp_name}: component absent from manifest")
            continue
        assert entry["status"] in ("ported", "unsupported"), f"{comp_name}: bad status {entry['status']!r}"
        props = component_props(schema, defs)
        for prop in props:
            if prop == "component":
                continue
            verdict = entry.get("props", {}).get(prop)
            if verdict is None:
                missing.append(f"{catalog}.{comp_name}.{prop}: prop absent from manifest")
            elif not (verdict == "handled" or verdict.startswith("handled:") or verdict.startswith("ignored:")):
                missing.append(f"{catalog}.{comp_name}.{prop}: bad verdict {verdict!r}")
        # A ported component must declare every enum value it handles.
        if entry["status"] == "ported":
            for prop, pschema in props.items():
                vals = enum_values(pschema, defs)
                if vals and entry.get("props", {}).get(prop) == "handled":
                    handled = set(entry.get("enums", {}).get(prop, []))
                    extra = set(vals) - handled
                    if extra:
                        missing.append(f"{catalog}.{comp_name}.{prop}: enum values not acknowledged: {sorted(extra)}")
    # Nothing in the manifest may name a component the catalog no longer has.
    for comp_name in section:
        if comp_name not in cat["components"]:
            missing.append(f"{catalog}.{comp_name}: in manifest but not in catalog")
    assert not missing, "\n".join(missing)


def test_every_basic_function_is_covered(manifest: dict) -> None:
    fns = set(load_catalog("basic").get("functions", {}).keys())
    covered = set(manifest["functions"].keys())
    assert fns == covered, f"missing={sorted(fns - covered)} stale={sorted(covered - fns)}"
