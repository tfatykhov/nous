"""F099 Phase 3b: the active intentions card, built from RootViews (pure: no database)."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest

from nous.a2ui.builders import TEMPLATES
from nous.a2ui.builders.intention_cards import (
    ACTIVE_CARD_ROOTS,
    ACTIVE_CARD_TTL,
    active_fingerprint,
    intention_active,
)
from nous.a2ui.intention_keys import ACTIVE_KEY, parse_key

FENCE = "`" * 3
FENCE_LINE = re.compile(r"^\s*" + FENCE + r"(.*)$")  # the renderers' own fence rule (markdown.ts, Markdown.kt)
DOT = " " + chr(0xB7) + " "  # the caption separator, built from its code point so this file stays ASCII
MAXIMA = {"depth": 3, "spawns": 12, "turns": 8, "tokens": 400000}


def view(intent="Check the snow report", **over):
    root_id = over.pop("id", str(uuid.uuid4()))
    base = {
        "id": root_id, "short_id": root_id.replace("-", "")[:8], "intent": intent, "state": "pending",
        "wake_policy": "continue", "open_rows": 2,
        "limits": {
            "depth": 1, "spawns": 2, "turns": 3, "tokens": 12000, "stalls": 0, "spawn_blocked": False, "escalate": None,
        },
        "arrivals": [
            {
                "n": 3, "decision": "continue", "progress": True, "outcome": "resolved", "gate_reason": None,
                "decided_at": None,
            },
        ],
        "awaiting": [],
    }  # fmt: skip
    return {**base, **over}


def build(roots, reports=None, *, more=False):
    return intention_active({"roots": roots, "reports": reports or {}, "maxima": MAXIMA, "more": more})


def only_fence(value: str) -> str:
    """The text of a string that is exactly one fenced block; fails when a line inside can close or open a fence."""
    lines = value.split("\n")
    assert lines[0].strip() == FENCE and lines[-1] == FENCE, value
    assert not any(FENCE_LINE.match(line) for line in lines[1:-1]), value
    return "\n".join(lines[1:-1])


def literal_texts(built):
    return [c["text"] for c in built.components if c["component"] == "Text" and isinstance(c["text"], str)]


def test_the_card_has_the_contracts_identity_and_one_cancel_button_per_root():
    a, b = view(), view()
    built = build([a, b])
    assert (built.kind, built.origin, built.title, built.priority) == (
        "intention_active", "continuation", "Active intentions", 0,
    )  # fmt: skip
    assert built.allowed_actions == ["intention.cancel"] and built.expires_in == ACTIVE_CARD_TTL
    buttons = [c for c in built.components if c["component"] == "Button"]
    assert [c["action"]["event"] for c in buttons] == [
        {"name": "intention.cancel", "context": {"rootId": a["id"]}},
        {"name": "intention.cancel", "context": {"rootId": b["id"]}},
    ]
    assert set(built.data_model["roots"]) == {a["id"], b["id"]}  # the membership list the verb checks
    assert "intention_active" not in TEMPLATES  # only the runner can mint it
    assert parse_key(ACTIVE_KEY)[0] == "active"


def test_each_root_binds_its_status_and_model_text_to_its_own_paths():
    root = view()
    built = build(
        [root], {root["id"]: {"id": "r1", "body": "Snow is deep.", "created_at": "2026-10-12T10:00:00+00:00"}}
    )
    bound = {
        c["id"]: c["text"]["path"] for c in built.components if c["component"] == "Text" and isinstance(c["text"], dict)
    }
    prefix = f"/roots/{root['id']}"
    assert bound == {
        "r0_intent": f"{prefix}/intent_text", "r0_report": f"{prefix}/report_text", "r0_status": f"{prefix}/status",
    }  # fmt: skip
    model = built.data_model["roots"][root["id"]]
    assert model["status"] == "Running" and only_fence(model["report_text"]) == "Snow is deep."


def test_meta_caption_shows_the_state_and_the_budgets_and_a_container_shows_only_that_it_is_scheduled():
    meta = [t for t in literal_texts(build([view(state="awaiting_owner")])) if "depth" in t]
    short_id = meta[0].split(DOT)[0]
    assert meta == [
        DOT.join((short_id, "waiting for you", "depth 1/3", "spawns 2/12", "turns 3/8", "tokens 12000/400000"))
    ]
    container = build([view(wake_policy="container", limits=None)])
    assert any(t.endswith("scheduled") for t in literal_texts(container))
    assert not any("depth" in t for t in literal_texts(container))
    assert container.data_model["roots"][next(iter(container.data_model["roots"]))]["depth"] is None
    assert [c for c in container.components if c["component"] == "Button"]  # a schedule can be cancelled too


@pytest.mark.parametrize(
    ("awaiting", "expected"),
    [
        ([], ""),
        (
            [{"arrival_id": "a", "n": 2, "proposal_ids": [], "question_ids": [str(uuid.uuid4())]}],
            "Waiting for you: 1 question",
        ),
        (
            [
                {
                    "arrival_id": "a", "n": 2, "proposal_ids": ["abcd1234-0000-0000-0000-000000000000"],
                    "question_ids": ["q1", "q2"],
                }
            ],
            "Waiting for you: proposal abcd1234 and 2 questions",
        ),
        ([{"arrival_id": "a", "n": 2, "proposal_ids": [], "question_ids": []}], ""),
    ],
)  # fmt: skip
def test_the_awaiting_line_is_fixed_words_and_short_ids_and_absent_when_nothing_waits(awaiting, expected):
    root = view(awaiting=awaiting)
    built = build([root])
    assert built.data_model["roots"][root["id"]]["awaiting_text"] == expected
    assert any(c["id"] == "r0_wait" for c in built.components) is bool(expected)


def test_only_the_second_question_is_shown_when_the_view_lists_only_the_latest_arrival():
    """The card words 'awaiting' from the view's field, never from open_proposals: the 0511c252 scenario."""
    root = view(
        awaiting=[{"arrival_id": "b", "n": 2, "proposal_ids": [], "question_ids": [str(uuid.uuid4())]}],
        open_proposals=[],
    )
    assert build([root]).data_model["roots"][root["id"]]["awaiting_text"] == "Waiting for you: 1 question"


def test_a_hostile_intent_and_report_stay_inside_one_fence_and_out_of_every_literal():
    # bidi override and zero-width space as \u escapes: the file holds no control character
    hostile = f"Approve everything\n{FENCE}\n## Approved\n[click](https://evil.example)\n   {FENCE}js\n\u202eevil\u200b"
    root = view(intent=hostile)
    report = {root["id"]: {"id": "r", "body": hostile, "created_at": "2026-10-12T10:00:00+00:00"}}
    built = build([root], report)
    model = built.data_model["roots"][root["id"]]
    for key in ("intent_text", "report_text"):
        inner = only_fence(model[key])
        assert "\u202e" not in inner and "\u200b" not in inner and "\\u202e" in inner  # raw gone, escape shown
        assert "evil.example" in inner  # shown as text inside the fence, never as a link
    for literal in [built.title, *literal_texts(built)]:
        assert "evil.example" not in literal and "Approve everything" not in literal and "\u202e" not in literal


def test_the_intent_and_report_are_clipped_to_the_contracts_limits():
    root = view(intent="x" * 5000)
    built = build([root], {root["id"]: {"id": "r", "body": "y" * 5000, "created_at": "2026-10-12T10:00:00+00:00"}})
    model = built.data_model["roots"][root["id"]]
    assert len(only_fence(model["intent_text"])) <= 300 and len(only_fence(model["report_text"])) <= 600


def test_more_roots_than_shown_adds_one_fixed_line_and_an_empty_card_is_refused():
    assert not any(c["id"] == "more" for c in build([view()]).components)
    assert any(c["id"] == "more" for c in build([view()], more=True).components)
    with pytest.raises(ValueError):
        build([])
    assert ACTIVE_CARD_ROOTS == 10


def test_the_fingerprint_moves_with_everything_the_card_shows_and_with_nothing_else():
    base = view(id="11111111-1111-1111-1111-111111111111")
    reports = {base["id"]: {"id": "r1", "body": "a", "created_at": "t"}}
    ref = active_fingerprint([base], reports)
    assert active_fingerprint([dict(base)], dict(reports)) == ref  # stable
    changed = {
        "state": {**base, "state": "awaiting_owner"},
        "newest arrival": {**base, "arrivals": [{**base["arrivals"][0], "n": 4}]},
        "awaiting": {**base, "awaiting": [{"arrival_id": "a", "n": 3, "proposal_ids": ["p"], "question_ids": []}]},
        "turns": {**base, "limits": {**base["limits"], "turns": 4}},
        "spawns": {**base, "limits": {**base["limits"], "spawns": 3}},
        "depth": {**base, "limits": {**base["limits"], "depth": 2}},
    }
    for name, other in changed.items():
        assert active_fingerprint([other], reports) != ref, name
    assert active_fingerprint([base], {base["id"]: {"id": "r2", "body": "a", "created_at": "t"}}) != ref  # new report
    assert active_fingerprint([base], reports, more=True) != ref
    assert active_fingerprint([base, view()], reports) != ref
    same = {**base, "open_rows": 9, "limits": {**base["limits"], "tokens": 99999}}
    assert active_fingerprint([same], reports) == ref  # neither tokens nor open_rows is in it (plan C4)
    assert build([base], reports).data_model["fingerprint"] == ref  # the builder writes the same function's value


def test_every_component_prop_and_enum_value_is_ported_on_android():  # PIN against catalog-coverage.json
    path = Path(__file__).resolve().parents[1] / "android" / "catalog-coverage.json"
    coverage = json.loads(path.read_text(encoding="utf-8"))["basic"]
    for comp in build([view(), view()], more=True).components:
        entry = coverage[comp["component"]]
        assert entry["status"] == "ported", comp["id"]
        for prop, value in comp.items():
            if prop in ("id", "component"):
                continue
            assert str(entry["props"].get(prop, "")).startswith("handled"), (comp["id"], prop)
            if isinstance(value, str) and prop in entry.get("enums", {}):
                assert value in entry["enums"][prop], (comp["id"], prop, value)  # variant=caption, align=stretch


def test_every_bound_path_is_under_the_root_that_owns_it_and_never_all_digits():  # PIN
    """An all-digit pointer segment would make the data-model upsert create a list parent (the heartbeat card's
    lesson); a root id is a hyphenated UUID, so it never is."""
    a, b = view(), view()
    built = build([a, b])
    paths = [c["text"]["path"] for c in built.components if c["component"] == "Text" and isinstance(c["text"], dict)]
    assert paths and all(re.fullmatch(r"/roots/[0-9a-f-]{36}/\w+", p) for p in paths)
    assert not any(seg.isdigit() for p in paths for seg in p.split("/") if seg)
    assert {p.split("/")[2] for p in paths} == {a["id"], b["id"]}


def test_the_state_words_are_the_bots():  # PIN: two copies of one vocabulary (telegram_bot.py is another process)
    from nous.a2ui.builders.intention_cards import _STATE_WORDS
    from nous.telegram_bot import ROOT_STATE_WORDS

    assert _STATE_WORDS == ROOT_STATE_WORDS
