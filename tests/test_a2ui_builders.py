"""F092: template builders emit valid A2UI, and the right A2UI.

``Surface.build()`` already runs schema + structural validation, so every
builder call here is itself an assertion that the surface validates —
``BuiltSurface.validate()`` is called again explicitly to say so out loud.
That is the point of the design (plan 3.2): an invalid surface fails CI
rather than reaching a renderer that would silently drop the component.

The behavioral assertions cover the two places where a builder decides
something a schema cannot check: which option is recommended, and whether a
Revert button exists at all.
"""

from __future__ import annotations

from typing import Any

import pytest

from nous.a2ui.builders import TEMPLATES, action_review, approval_gate, heartbeat_findings
from nous.a2ui.dsl import BuiltSurface

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _by_id(built: BuiltSurface, component_id: str) -> dict:
    for component in built.components:
        if component["id"] == component_id:
            return component
    raise AssertionError(f"no component {component_id!r} in {[c['id'] for c in built.components]}")


def _action_names(built: BuiltSurface) -> set[str]:
    """Every action name actually wired to a component in the surface."""
    names = set()
    for component in built.components:
        action = component.get("action")
        if isinstance(action, dict):
            name = (action.get("event") or {}).get("name")
            if name:
                names.add(name)
    return names


def _text_of(built: BuiltSurface, component_id: str) -> Any:
    return _by_id(built, component_id)["text"]


TRACE_ID = "3f2a1b4c-5d6e-4f70-8192-a3b4c5d6e7f8"

APPROVAL_PARAMS = {
    "title": "Delete the stale eval database?",
    "summary": "nous-eval-scratch has not been read in 40 days.",
    "risk": "Irreversible. 12 GB of fixtures would need re-ingesting.",
    "options": [
        {"id": "keep", "label": "Keep it"},
        {"id": "drop", "label": "Drop it"},
    ],
    "recommendation": "keep",
}

REVIEW_PARAMS = {
    "title": "Archived 43 resolved findings",
    "did": "Moved 43 findings older than 30 days out of the active list.",
    "why": "They were resolved and were crowding the triage view.",
    "cost": "One sweep, no LLM calls.",
}

FINDINGS_PARAMS = {
    "findings": [
        {
            "fingerprint": "abc123def456",
            "message": "Disk usage on /var is at 91%.",
            "urgency": "high",
            "check": "health",
        },
        {
            "fingerprint": "999888777666",
            "message": "Two subtasks have been running for 6 hours.",
            "urgency": "normal",
            "check": "health",
        },
    ]
}


# ---------------------------------------------------------------------------
# Every builder produces a valid surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("template", "params"),
    [
        ("approval_gate", APPROVAL_PARAMS),
        ("action_review", REVIEW_PARAMS),
        ("heartbeat_findings", FINDINGS_PARAMS),
    ],
)
def test_builder_output_validates(template: str, params: dict) -> None:
    built = TEMPLATES[template](params)

    built.validate()  # raises SurfaceValidationError on any schema/structure error

    assert built.kind == template
    assert built.title
    assert built.allowed_actions
    assert any(c["id"] == "root" for c in built.components)


def test_templates_registry_covers_all_builders() -> None:
    assert set(TEMPLATES) == {
        "approval_gate",
        "action_review",
        "heartbeat_findings",
        # Phase 2
        "decision_sweep",
        "memory_graph",
        "dag_monitor",
    }


# ---------------------------------------------------------------------------
# approval_gate
# ---------------------------------------------------------------------------


def test_approval_gate_marks_the_recommended_option() -> None:
    """The recommended option is the primary button and says so in its label.

    Styling alone would not survive a screen reader, and the label alone
    would not survive a glance — the surface carries both.
    """
    built = approval_gate(APPROVAL_PARAMS)

    # options[0] is "keep", which is also the recommendation.
    assert _by_id(built, "opt_0")["variant"] == "primary"
    assert _text_of(built, "opt_label_0") == "Keep it (recommended)"

    assert _by_id(built, "opt_1")["variant"] == "default"
    assert _text_of(built, "opt_label_1") == "Drop it"


def test_approval_gate_defaults_the_recommendation_to_the_first_option() -> None:
    params = {k: v for k, v in APPROVAL_PARAMS.items() if k != "recommendation"}

    built = approval_gate(params)

    assert built.data_model["recommendation"] == "keep"
    assert _by_id(built, "opt_0")["variant"] == "primary"


def test_approval_gate_allowed_actions_are_exact() -> None:
    built = approval_gate(APPROVAL_PARAMS)

    assert built.allowed_actions == ["approval.choose", "approval.defer"]
    assert _action_names(built) == {"approval.choose", "approval.defer"}


def test_approval_gate_is_top_priority() -> None:
    """It blocks an irreversible action, so it pages (priority >= 1 notifies)."""
    assert approval_gate(APPROVAL_PARAMS).priority == 2


def test_approval_gate_carries_the_trace_id_into_action_context() -> None:
    built = approval_gate({**APPROVAL_PARAMS, "trace_id": TRACE_ID})

    context = _by_id(built, "opt_0")["action"]["event"]["context"]

    assert context == {"optionId": "keep", "traceId": TRACE_ID}
    assert built.trace_id == TRACE_ID


@pytest.mark.parametrize("builder", [approval_gate, action_review])
def test_builders_refuse_a_non_uuid_trace_id(builder: Any) -> None:
    """A malformed trace_id would make review.course_correct no-op silently.

    brain.review() takes a UUID; a bad one is caught and logged inside the
    handler, so the user would see "correction recorded" against nothing.
    Refusing at build time puts the error where the producer can act on it.
    """
    params = APPROVAL_PARAMS if builder is approval_gate else REVIEW_PARAMS

    with pytest.raises(ValueError, match="trace_id must be a UUID"):
        builder({**params, "trace_id": "trace-7"})


@pytest.mark.parametrize("empty", [None, ""])
def test_builders_treat_an_absent_trace_id_as_none(empty: Any) -> None:
    assert approval_gate({**APPROVAL_PARAMS, "trace_id": empty}).trace_id is None
    assert (
        "traceId"
        not in _by_id(approval_gate({**APPROVAL_PARAMS, "trace_id": empty}), "opt_0")["action"]["event"]["context"]
    )


def test_approval_gate_rejects_missing_required_params() -> None:
    with pytest.raises(KeyError):
        approval_gate({"options": APPROVAL_PARAMS["options"]})  # no title

    with pytest.raises(KeyError):
        approval_gate({"title": "T"})  # no options


def test_approval_gate_rejects_an_empty_option_list() -> None:
    """A decision surface with nothing to decide is a dead end, not a card."""
    with pytest.raises(ValueError, match="at least one option"):
        approval_gate({"title": "T", "options": []})


# ---------------------------------------------------------------------------
# action_review
# ---------------------------------------------------------------------------


def test_action_review_omits_revert_when_not_revertible() -> None:
    """No Revert affordance unless something can actually revert it.

    A Revert button that silently fails is worse than no button, so the
    allowlist and the component tree must agree — checking only the allowlist
    would miss a stranded button, and vice versa.
    """
    built = action_review({**REVIEW_PARAMS, "compensation": {"revertible": False}})

    assert "review.revert" not in built.allowed_actions
    assert "review.revert" not in _action_names(built)
    assert all(c["id"] != "revert" for c in built.components)


def test_action_review_offers_revert_when_revertible_and_handler() -> None:
    """Phase 2.8: Revert is offered when the action is compensable and a handler is set."""
    built = action_review(
        {
            **REVIEW_PARAMS,
            "trace_id": "0b7e1c8a-3f7a-4c55-9a55-2f1f6b1d9e10",
            "compensation": {
                "revertible": True,
                "handler": "write_file",
                "note": "The prior file content was snapshotted.",
            },
        }
    )

    assert "review.revert" in built.allowed_actions
    assert any(c["id"] == "revert" for c in built.components)
    assert built.data_model["compensation"]["revertible"] is True


def test_action_review_defaults_to_not_revertible() -> None:
    """Absent compensation means unknown, and unknown must not offer Revert."""
    built = action_review(REVIEW_PARAMS)

    assert built.allowed_actions == [
        "review.acknowledge",
        "review.course_correct",
        "review.make_rule",
    ]


def test_action_review_binds_the_correction_field_to_the_data_model() -> None:
    """Course-correct sends whatever the user typed, via a pointer binding."""
    built = action_review(REVIEW_PARAMS)

    assert _by_id(built, "correction_field")["value"] == {"path": "/correction"}
    assert built.data_model["correction"] == ""

    context = _by_id(built, "correct")["action"]["event"]["context"]
    assert context["correction"] == {"path": "/correction"}


def test_action_review_rejects_missing_title() -> None:
    with pytest.raises(KeyError):
        action_review({"did": "something"})


# ---------------------------------------------------------------------------
# heartbeat_findings
# ---------------------------------------------------------------------------


def test_heartbeat_findings_renders_a_card_per_finding() -> None:
    built = heartbeat_findings(FINDINGS_PARAMS)

    assert _text_of(built, "f0_msg") == "Disk usage on /var is at 91%."
    assert _text_of(built, "f1_msg") == "Two subtasks have been running for 6 hours."
    assert built.data_model["findings"] == {"abc123def456": "open", "999888777666": "open"}


def test_heartbeat_findings_explains_its_buttons_and_binds_a_status_line() -> None:
    """Tim, 2026-10-05: pressing a button showed nothing, and the three verbs
    were unexplained. The card now carries a legend and a per-finding status
    line bound to /status/<fp>, which the action handler patches."""
    built = heartbeat_findings(FINDINGS_PARAMS)
    built.validate()

    legend = _text_of(built, "legend")
    for verb in ("Acknowledge", "Resolve", "Dismiss"):
        assert verb in legend
    assert _by_id(built, "f0_status")["text"] == {"path": "/status/abc123def456"}
    assert built.data_model["status"]["abc123def456"].startswith("Status: open")
    assert "f0_status" in _by_id(built, "f0_col")["children"]


def test_heartbeat_findings_wires_every_verb_to_each_finding() -> None:
    built = heartbeat_findings(FINDINGS_PARAMS)

    assert built.allowed_actions == [
        "heartbeat.acknowledge",
        "heartbeat.resolve",
        "heartbeat.dismiss",
    ]
    assert _action_names(built) == set(built.allowed_actions)

    ack_context = _by_id(built, "f0_ack")["action"]["event"]["context"]
    assert ack_context == {"fingerprint": "abc123def456"}


def test_heartbeat_findings_renders_an_empty_state() -> None:
    """Zero findings still has to render: the surface may be updated in place.

    A dedup_key'd triage card that empties out must say it is empty rather
    than become a surface with no body.
    """
    built = heartbeat_findings({"findings": []})

    built.validate()

    assert _text_of(built, "empty") == "No open findings."
    assert _by_id(built, "root")["children"] == ["header", "empty"]  # no legend when empty


def test_heartbeat_findings_defaults_its_title_to_the_count() -> None:
    assert heartbeat_findings({"findings": []}).title == "Heartbeat findings (0)"
    assert heartbeat_findings(FINDINGS_PARAMS).title == "Heartbeat findings (2)"


def test_heartbeat_findings_requires_a_fingerprint_per_finding() -> None:
    """The fingerprint is the handle every action verb needs; no default."""
    with pytest.raises(KeyError):
        heartbeat_findings({"findings": [{"message": "no fingerprint here"}]})


def test_approval_gate_dag_options():
    import json

    from nous.a2ui.builders import approval_gate

    built = approval_gate(
        {
            "title": "t",
            "options": [
                {"id": "send", "label": "Send it — continues", "outcome": "proceed"},
                {"id": "hold", "label": "Don't send — stops here", "outcome": "stop"},
            ],
            "recommend_first": False,
            "defer_label": "Decide later",
        }
    )
    assert built.data_model["recommendation"] == ""
    assert [o["outcome"] for o in built.data_model["options"]] == ["proceed", "stop"]
    rendered = json.dumps(built.components)
    assert "Decide later" in rendered
    assert "(recommended)" not in rendered


def test_approval_gate_defaults_are_unchanged():
    import json

    from nous.a2ui.builders import approval_gate

    built = approval_gate({"title": "t", "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]})
    assert built.data_model["recommendation"] == "a"
    assert built.data_model["options"] == [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]
    assert "Ask me later" in json.dumps(built.components)


# ---------------------------------------------------------------------------
# push_surface derives revert eligibility from server state (Finding #2 fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_surface_ignores_caller_revertible_without_server_snapshot() -> None:
    """A model-authored push_surface with revertible=True and a handler must NOT
    offer Revert when the server has no matching snapshot.

    Before the fix the builder trusted caller-provided fields; any agent could
    force a Revert button for an irreversible or untracked action.
    """
    from unittest.mock import AsyncMock, MagicMock

    from nous.a2ui.tools import register_a2ui_tools
    from nous.api.compensation import CompensationRegistry

    # snapshot_store reports no snapshot for any ledger entry
    snap_store = MagicMock()
    snap_store.get_by_ledger_entry = AsyncMock(return_value=None)

    registry = CompensationRegistry()

    captured: dict = {}

    class _CapturingDispatcher:
        def register(self, name, fn, schema):
            captured[name] = fn

    class _FakeService:
        async def push_built(self, built, **kwargs):
            captured["_last_built"] = built
            return "surf-1"

    _disp = _CapturingDispatcher()
    register_a2ui_tools(
        _disp,
        _FakeService(),
        snapshot_store=snap_store,
        compensation_registry=registry,
    )
    push_surface = captured["push_surface"]

    TRACE_ID = "3f2a1b4c-5d6e-4f70-8192-a3b4c5d6e7f8"
    result = await push_surface(
        template="action_review",
        params={
            "title": "Deleted logs",
            "did": "Ran rm -rf /var/log/stale",
            "compensation": {"revertible": True, "handler": "restore_logs", "note": "malicious"},
            "trace_id": TRACE_ID,
        },
    )

    built = captured.get("_last_built")
    assert built is not None, f"push failed: {result}"
    assert "review.revert" not in built.allowed_actions, "Revert must not be offered when server has no snapshot"


@pytest.mark.asyncio
async def test_push_surface_offers_revert_when_snapshot_and_compensator_exist() -> None:
    """Revert IS offered when snapshot_store has a live snapshot AND the
    compensation_registry has a compensator for its tool.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock
    from uuid import uuid4

    from nous.a2ui.tools import register_a2ui_tools
    from nous.api.compensation import CompensationRegistry, CompensationResult

    registry = CompensationRegistry()

    async def _ok(eid, snap_data, deps):
        return CompensationResult(success=True, message="done")

    registry.register("write_file", _ok)

    TRACE_ID = "3f2a1b4c-5d6e-4f70-8192-a3b4c5d6e7f8"
    fake_snap = SimpleNamespace(
        id=uuid4(),
        tool_name="write_file",
        # What write_file records before dispatch: the revert's guard needs it.
        snapshot_data={"written_content_hash": "0" * 64, "written_size": 1, "workspace_root": "/ws"},
        reverted_at=None,
    )

    snap_store = MagicMock()
    snap_store.get_by_ledger_entry = AsyncMock(return_value=fake_snap)

    captured: dict = {}

    class _CapturingDispatcher:
        def register(self, name, fn, schema):
            captured[name] = fn

    class _FakeService:
        async def push_built(self, built, **kwargs):
            captured["_last_built"] = built
            return "surf-1"

    _disp = _CapturingDispatcher()
    register_a2ui_tools(
        _disp,
        _FakeService(),
        snapshot_store=snap_store,
        compensation_registry=registry,
    )
    push_surface = captured["push_surface"]

    result = await push_surface(
        template="action_review",
        params={
            "title": "Wrote config",
            "did": "Updated /etc/app.conf",
            "compensation": {"revertible": False},
            "trace_id": TRACE_ID,
        },
    )

    built = captured.get("_last_built")
    assert built is not None, f"push failed: {result}"
    assert "review.revert" in built.allowed_actions, "Revert must be offered when snapshot and compensator both exist"
