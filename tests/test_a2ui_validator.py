"""F092: A2UI envelope + structural validation.

Two of these tests pin probe-proven P1 regressions from the plan review
(docs/plans/2026-08-29-f092-a2ui-companion-phase1.md 3.9). Both crashed or
rejected valid surfaces before the fix, and both are silent-failure shaped:

1. ``\\p{XID_Start}`` in common_types.json raises ``re.PatternError`` inside
   jsonschema rather than reporting an error, and EVERY Nous surface carries
   ``metadata.extensions.com_nous_nonce`` — so the crash was on the only path
   that matters.
2. The envelope's catalog ``$ref`` is static, so a surface mixing basic and
   nous-core components (the flagship Action Review shape) validated against
   neither catalog alone.
"""

from __future__ import annotations

from typing import Any

import pytest

from nous.a2ui.validator import (
    BASIC_CATALOG_ID,
    NOUS_CORE_CATALOG_ID,
    load_catalog,
    validate_envelope,
    validate_structure,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _envelope(
    components: list[dict],
    *,
    catalog_id: str = BASIC_CATALOG_ID,
    extensions: dict | None = None,
) -> dict:
    """A minimal createSurface envelope around ``components``."""
    create: dict[str, Any] = {
        "surfaceId": "s1",
        "catalogId": catalog_id,
        "components": components,
    }
    if extensions is not None:
        create["metadata"] = {"extensions": extensions}
    return {"version": "v1.0", "createSurface": create}


_TEXT_ROOT = [{"id": "root", "component": "Text", "text": "hello"}]


# ---------------------------------------------------------------------------
# P1 regression: \p{...} pattern rewrite
# ---------------------------------------------------------------------------


def test_extension_keys_validate_without_pattern_crash() -> None:
    """The UAX #31 pattern rewrite: valid identifier keys pass, no crash.

    Before the rewrite this raised re.PatternError out of jsonschema instead
    of returning errors, on the one path every Nous surface takes.
    """
    envelope = _envelope(
        _TEXT_ROOT, extensions={"com_nous_nonce": "tok", "a2ui_x": 1}
    )

    assert validate_envelope(envelope) == []


@pytest.mark.parametrize("bad_key", ["com-nous-bad", "1abc"])
def test_non_identifier_extension_keys_are_rejected(bad_key: str) -> None:
    """A hyphen or a leading digit is REJECTED — reported, never raised."""
    errors = validate_envelope(_envelope(_TEXT_ROOT, extensions={bad_key: "x"}))

    assert len(errors) == 1
    assert errors[0]["code"] == "VALIDATION_FAILED"
    assert errors[0]["surfaceId"] == "s1"


# ---------------------------------------------------------------------------
# P1 regression: merged catalog
# ---------------------------------------------------------------------------


def test_mixed_catalog_surface_validates() -> None:
    """basic (Column/Text/Button) + nous-core (ApprovalPanel) in one surface."""
    components = [
        {"id": "root", "component": "Column", "children": ["panel", "note", "go"]},
        {
            "id": "panel",
            "component": "ApprovalPanel",
            "title": "Deploy to prod?",
            "summary": "Ships 4 commits.",
        },
        {"id": "note", "component": "Text", "text": "Reviewed by two people."},
        {
            "id": "go",
            "component": "Button",
            "child": "note",
            "action": {"event": {"name": "approval.choose", "context": {}}},
        },
    ]

    errors = validate_envelope(
        _envelope(components, catalog_id=NOUS_CORE_CATALOG_ID)
    )

    assert errors == []


def test_basic_only_envelope_validates() -> None:
    errors = validate_envelope(_envelope(_TEXT_ROOT))

    assert errors == []


def test_unknown_component_is_rejected() -> None:
    errors = validate_envelope(
        _envelope([{"id": "root", "component": "NotAComponent", "text": "x"}])
    )

    assert len(errors) == 1
    assert errors[0]["code"] == "VALIDATION_FAILED"


def test_button_variant_must_be_a_literal_enum() -> None:
    """A data binding in ``variant`` is invalid — the catalog wants a literal.

    The F092 spec's own Appendix A example binds this field; the catalog it
    cites does not allow it, which is why builders emit per-option buttons
    with literal variants instead.
    """
    components = [
        {"id": "root", "component": "Column", "children": ["btn"]},
        {
            "id": "btn",
            "component": "Button",
            "child": "label",
            "variant": {"path": "/variant"},
            "action": {"event": {"name": "approval.choose", "context": {}}},
        },
        {"id": "label", "component": "Text", "text": "Go"},
    ]

    errors = validate_envelope(_envelope(components))

    assert len(errors) == 1


# ---------------------------------------------------------------------------
# validate_structure
# ---------------------------------------------------------------------------


def test_structure_accepts_a_well_formed_tree() -> None:
    components = [
        {"id": "root", "component": "Column", "children": ["btn", "label"]},
        {
            "id": "btn",
            "component": "Button",
            "child": "label",
            "action": {"event": {"name": "approval.defer", "context": {}}},
        },
        {"id": "label", "component": "Text", "text": "Later"},
    ]

    assert validate_structure(components, ["approval.defer"]) == []


def test_structure_accepts_the_function_call_action_branch() -> None:
    # Action is a oneOf in common_types.json: event OR functionCall. The
    # allowlist governs agent-event names only — a functionCall action is
    # dispatched through the callAgentFunction trust pipeline and must NOT
    # be reported as malformed (codex P2 on #626).
    components = [
        {"id": "root", "component": "Column", "children": ["btn"]},
        {
            "id": "btn",
            "component": "Button",
            "child": "label",
            "action": {"functionCall": {"call": "expandNode", "args": {}}},
        },
        {"id": "label", "component": "Text", "text": "More"},
    ]

    assert validate_structure(components, []) == []


def test_structure_rejects_malformed_action_shapes() -> None:
    # A string event — a plausible malformed LLM output — used to raise
    # AttributeError, escaping both the compose repair loop and the
    # fallback. It must be a validation error instead.
    for bad_action in ({"event": "app.close"}, {}, {"event": None}):
        components = [
            {"id": "root", "component": "Column", "children": ["btn"]},
            {"id": "btn", "component": "Button", "child": "label", "action": bad_action},
            {"id": "label", "component": "Text", "text": "x"},
        ]

        errors = validate_structure(components, ["app.close"])

        assert len(errors) == 1, bad_action
        assert "action must carry" in errors[0]["message"]


def test_structure_requires_a_root_component() -> None:
    errors = validate_structure([{"id": "body", "component": "Text", "text": "x"}], [])

    assert len(errors) == 1
    assert "root" in errors[0]["message"]


def test_structure_rejects_duplicate_ids() -> None:
    components = [
        {"id": "root", "component": "Text", "text": "one"},
        {"id": "root", "component": "Text", "text": "two"},
    ]

    errors = validate_structure(components, [])

    assert [e["message"] for e in errors] == ["duplicate component id 'root'"]


def test_structure_rejects_dangling_child_refs() -> None:
    components = [{"id": "root", "component": "Column", "children": ["ghost"]}]

    errors = validate_structure(components, [])

    assert len(errors) == 1
    assert "dangling child ref 'ghost'" in errors[0]["message"]


def test_structure_detects_cycles() -> None:
    components = [
        {"id": "root", "component": "Card", "child": "b"},
        {"id": "b", "component": "Card", "child": "root"},
    ]

    errors = validate_structure(components, [])

    assert len(errors) == 1
    assert "cycle" in errors[0]["message"]


def test_structure_rejects_actions_outside_the_allowlist() -> None:
    """A button naming an action the surface does not offer is rejected here.

    allowed_actions is the server-side gate, so such a button would fail at
    POST time with no way for the user to tell why — it fails at build instead.
    """
    components = [
        {"id": "root", "component": "Column", "children": ["btn", "label"]},
        {
            "id": "btn",
            "component": "Button",
            "child": "label",
            "action": {"event": {"name": "review.revert", "context": {}}},
        },
        {"id": "label", "component": "Text", "text": "Revert"},
    ]

    errors = validate_structure(components, ["review.acknowledge"])

    assert len(errors) == 1
    assert "review.revert" in errors[0]["message"]


def test_structure_follows_modal_trigger_and_content_refs() -> None:
    """Modal names its children ``trigger``/``content``, not ``child(ren)``."""
    components = [
        {
            "id": "root",
            "component": "Modal",
            "trigger": "missing_trigger",
            "content": "missing_content",
        }
    ]

    messages = [e["message"] for e in validate_structure(components, [])]

    assert "dangling child ref 'missing_trigger'" in messages
    assert "dangling child ref 'missing_content'" in messages


def test_structure_follows_tab_child_refs() -> None:
    """Tabs nest their child ids one level down, inside ``tabs[].child``."""
    components = [
        {
            "id": "root",
            "component": "Tabs",
            "tabs": [{"title": "One", "child": "panel_one"}],
        }
    ]

    errors = validate_structure(components, [])

    assert len(errors) == 1
    assert "dangling child ref 'panel_one'" in errors[0]["message"]


# ---------------------------------------------------------------------------
# load_catalog (backs GET /a2ui/catalog/{name})
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["basic", "nous-core"])
def test_load_catalog_returns_a_document(name: str) -> None:
    catalog = load_catalog(name)

    assert catalog is not None
    assert "components" in catalog
    assert catalog.get("$id")


def test_load_catalog_returns_none_for_an_unknown_name() -> None:
    assert load_catalog("no-such-catalog") is None


def test_served_catalog_ids_match_what_surfaces_declare() -> None:
    """The renderer maps a surface's catalogId to a local fetch path, so the

    served ``$id`` must equal the id builders stamp on their surfaces.
    """
    assert load_catalog("basic")["$id"] == BASIC_CATALOG_ID
    assert load_catalog("nous-core")["$id"] == NOUS_CORE_CATALOG_ID
