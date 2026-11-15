"""F114 goal routes: the owner's changes go through the real store, as the owner, and a refusal reaches the caller in
the store's own words; on Postgres."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from goal_routes_support import (
    CHAN,
    OWNER,
    OWNER_VALUE,
    PROXY,
    active,
    add_session,
    db_now,
    digest,
    draft,
    journal_rows,
    make_env,
    paused,
    reread,
    send,
    set_values,
)

from nous.goals.store import GoalRefused, describe, format_short_id

pytestmark = pytest.mark.postgres_only

NEW = {
    "title": "Sort the photos",
    "objective": "Put the trip photos in folders by day",
    "done_when": "Every photo is in a folder",
}


async def _refusal_of(call) -> str:
    with pytest.raises(GoalRefused) as refused:
        await call
    return str(refused.value)


async def test_create_makes_a_draft_proposed_by_the_owner_with_a_journal_row_by_the_owner(db) -> None:
    env = make_env(db)
    response = await env.call("POST", "/goals", json={**NEW, "origin_channel": CHAN})
    assert response.status_code == 201
    body = response.json()
    assert (body["state"], body["proposed_by"], body["preset"]) == ("draft", "owner", "small")
    assert body["title"] == NEW["title"] and body["origin_channel"] == "telegram:****"
    assert body["actions"] == ["approve", "budget", "deadline", "note", "cancel"]
    goal = await env.store.get(body["id"])
    (row,) = await journal_rows(env.store, goal)
    assert (row.kind, row.actor) == ("state", "owner")


async def test_create_through_the_proxy_with_preset_and_no_channel(db) -> None:
    env = make_env(db)
    response = await env.call(
        "POST", "/goals", json={**NEW, "preset": "large"}, headers={**PROXY, "content-type": "application/json"}
    )
    assert response.status_code == 201
    assert (response.json()["preset"], response.json()["origin_channel"]) == ("large", None)
    assert response.json()["actions"] == ["budget", "deadline", "note", "cancel"]  # nowhere to ask: no approve


async def test_create_with_limits_makes_a_custom_goal_and_a_bad_limit_is_409_with_the_envelope_sentence(db) -> None:
    env = make_env(db)
    limits = {"total_budget": 3_000_000, "quiet_hours_ok": True}
    custom = await env.call("POST", "/goals", json={**NEW, "limits": limits})
    assert custom.status_code == 201
    body = custom.json()
    assert (body["preset"], body["total_budget"], body["day_budget"], body["quiet_hours_ok"]) == (
        "custom", 3_000_000, 500_000, True,
    )
    bad = await env.call("POST", "/goals", json={**NEW, "limits": {"day_budget": 3_000_000}})
    assert (bad.status_code, bad.json()) == (409, {"error": "the day budget cannot be larger than the total budget"})
    unknown = await env.call("POST", "/goals", json={**NEW, "preset": "huge"})
    assert (unknown.status_code, unknown.json()) == (
        409, {"error": "unknown preset 'huge': use small, medium, large or custom"},
    )
    assert len((await env.call("GET", "/goals")).json()["goals"]) == 1


async def test_create_with_a_bad_origin_channel_is_refused_in_the_stores_words(db) -> None:
    env = make_env(db)
    response = await env.call("POST", "/goals", json={**NEW, "origin_channel": "email:me"})
    assert response.status_code == 409
    expected = await _refusal_of(env.store.create(**NEW, proposed_by="owner", origin_channel="email:me"))
    assert response.json() == {"error": expected}
    assert (await env.call("GET", "/goals")).json()["goals"] == []


async def test_approve_sets_active_and_the_deadline(db) -> None:
    env = make_env(db)
    goal = await draft(env.store)
    response = await env.call("POST", f"/goals/{goal.id}/approve", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "active" and body["approved_at"] is not None and body["deadline_at"] is not None
    assert body["journal"][0]["actor"] == "owner" and body["journal"][0]["text"].startswith("Approved")


async def test_approve_without_a_channel_is_refused_in_words(db) -> None:
    env = make_env(db)
    goal = await draft(env.store, origin_channel=None)
    response = await env.call("POST", f"/goals/{goal.id}/approve")
    assert response.status_code == 409
    assert response.json()["error"] == (
        f"{describe(goal)} has nowhere to ask the owner: give it an origin channel or set NOUS_TELEGRAM_CHAT_ID"
    )
    assert (await reread(env.store, goal)).state == "draft"


async def test_pause_resume_stop_cancel_confirm_done_run_now_follow_the_store(db) -> None:
    env = make_env(db)
    goal = await active(env.store)
    run = await env.call("POST", f"/goals/{goal.id}/run-now")
    assert (run.status_code, run.json()["pending_request"]) == (200, "run_now")
    pause = await env.call("POST", f"/goals/{goal.id}/pause", json={})
    assert (pause.json()["state"], pause.json()["pause_reason"], pause.json()["pending_request"]) == (
        "paused", "owner", None,
    )
    resume = await env.call("POST", f"/goals/{goal.id}/resume", json={})
    assert (resume.status_code, resume.json()["state"]) == (200, "active")
    stop = await env.call("POST", f"/goals/{goal.id}/stop", json={})
    assert (stop.json()["state"], stop.json()["pause_reason"]) == ("paused", "owner")
    waiting = await paused(env.store, "waiting_for_owner")
    done = await env.call("POST", f"/goals/{waiting.id}/confirm-done", json={})
    assert (done.status_code, done.json()["state"], done.json()["actions"]) == (200, "done", [])
    cancel = await env.call("POST", f"/goals/{goal.id}/cancel", json={})
    assert (cancel.status_code, cancel.json()["state"]) == (200, "cancelled")


@pytest.mark.parametrize(
    ("action", "store_call"),
    [
        ("pause", lambda s, g: s.pause(g.id, actor="owner")),
        ("resume", lambda s, g: s.resume(g.id, actor="owner")),
        ("stop", lambda s, g: s.stop(g.id, actor="owner")),
        ("confirm-done", lambda s, g: s.confirm_done(g.id, actor="owner")),
        ("run-now", lambda s, g: s.request_run_now(g.id, actor="owner")),
        ("approve", lambda s, g: s.approve(g.id, actor="owner")),
    ],
)
async def test_a_wrong_state_call_is_409_with_the_stores_sentence_and_the_row_unchanged(db, action, store_call) -> None:
    env = make_env(db)
    goal = await (active(env.store) if action == "approve" else draft(env.store))
    before = await reread(env.store, goal)
    response = await env.call("POST", f"/goals/{format_short_id(goal.short_id)}/{action}", json={})
    assert response.status_code == 409
    assert response.json() == {"error": await _refusal_of(store_call(env.store, goal))}
    after = await reread(env.store, goal)
    assert (after.state, after.pause_reason, after.pending_request, after.updated_at) == (
        before.state, before.pause_reason, before.pending_request, before.updated_at,
    )


async def test_finished_goals_refuse_cancel_note_budget_and_deadline_in_words(db) -> None:
    env = make_env(db)
    goal = await draft(env.store)
    await env.store.cancel(goal.id, actor="owner")
    for action, body in (("cancel", {}), ("note", {"text": "x"}), ("budget", {"total": 5}), ("deadline", {"days": 1})):
        response = await env.call("POST", f"/goals/{goal.id}/{action}", json=body)
        assert response.status_code == 409, action
        assert response.json()["error"].startswith(f"{describe(await reread(env.store, goal))}: "), action


async def test_budget_raise_and_a_lowering_below_spend_is_409(db) -> None:
    env = make_env(db)
    goal = await active(env.store)
    await add_session(env.store, goal, n=1, spent=1_500_000)
    raised = await env.call("POST", f"/goals/{goal.id}/budget", json={"total": 4_000_000, "day": 800_000})
    assert (raised.status_code, raised.json()["total_budget"], raised.json()["day_budget"]) == (200, 4_000_000, 800_000)
    lowered = await env.call("POST", f"/goals/{goal.id}/budget", json={"total": 1_000_000, "day": 100_000})
    assert lowered.status_code == 409
    assert lowered.json()["error"] == (
        f"{describe(goal)}: 1500000 tokens are already spent; the total cannot be lowered below that"
    )
    nothing = await env.call("POST", f"/goals/{goal.id}/budget", json={})
    assert nothing.status_code == 409
    assert nothing.json() == {"error": "give a new total budget, a new day budget, or both"}
    assert (await reread(env.store, goal)).total_budget == 4_000_000


async def test_deadline_extension_and_a_91_day_request_is_409(db) -> None:
    env = make_env(db)
    goal = await active(env.store)
    extended = await env.call("POST", f"/goals/{goal.id}/deadline", json={"days": 5})
    assert extended.status_code == 200
    moved = (await reread(env.store, goal)).deadline_at
    assert moved - goal.deadline_at == timedelta(days=5)
    too_far = await env.call("POST", f"/goals/{goal.id}/deadline", json={"days": 91})
    assert too_far.status_code == 409
    assert too_far.json() == {"error": "the extension must be a whole number of days, 1 to 90"}
    a_draft = await draft(env.store)
    longer = await env.call("POST", f"/goals/{a_draft.id}/deadline", json={"days": 4})
    assert (longer.status_code, longer.json()["deadline_days"]) == (200, 7)


async def test_note_adds_an_owner_note_and_wakes_an_active_goal_but_never_resumes_a_paused_one(db) -> None:
    env = make_env(db)
    goal = await active(env.store)
    later = await db_now(env.store) + timedelta(hours=3)
    await set_values(env.store, goal, next_wake_at=later)
    noted = await env.call("POST", f"/goals/{goal.id}/note", json={"text": "  Use the 2024 folder.  "})
    assert noted.status_code == 200
    row = noted.json()["journal"][0]
    assert (row["kind"], row["actor"], row["text"]) == ("owner_note", "owner", "Use the 2024 folder.")
    assert (await reread(env.store, goal)).next_wake_at < later
    held = await paused(env.store, "owner")
    kept = await env.call("POST", f"/goals/{held.id}/note", json={"text": "Wait for me."})
    assert (kept.status_code, kept.json()["state"]) == (200, "paused")
    blank = await env.call("POST", f"/goals/{goal.id}/note", json={"text": "   "})
    assert (blank.status_code, blank.json()) == (409, {"error": "a note needs some text"})


async def test_every_action_is_journalled_as_the_owner(db) -> None:
    env = make_env(db)
    proxy = {**PROXY, "content-type": "application/json"}
    goal = await draft(env.store)
    steps = [
        ("note", {"text": "First."}, None),
        ("budget", {"total": 3_000_000}, proxy),
        ("deadline", {"days": 2}, None),
        ("approve", {}, proxy),
        ("run-now", {}, None),
        ("pause", {}, proxy),
        ("resume", {}, None),
        ("stop", {}, proxy),
        ("cancel", {}, None),
    ]
    for action, body, headers in steps:
        response = await env.call("POST", f"/goals/{goal.id}/{action}", json=body, headers=headers)
        assert response.status_code == 200, (action, response.text)
        assert (await journal_rows(env.store, goal))[-1].actor == "owner", action
    waiting = await paused(env.store, "waiting_for_owner")
    assert (await env.call("POST", f"/goals/{waiting.id}/confirm-done", headers=proxy)).status_code == 200
    assert (await journal_rows(env.store, waiting))[-1].actor == "owner"
    assert {row.actor for row in await journal_rows(env.store, goal)} == {"owner"}


@pytest.mark.parametrize("action", ["approve", "pause", "note", "budget", "deadline", "cancel"])
async def test_a_body_that_names_an_actor_is_400(db, action: str) -> None:
    env = make_env(db)
    goal = await draft(env.store)
    valid = {"note": {"text": "x"}, "budget": {"total": 3_000_000}, "deadline": {"days": 1}}.get(action, {})
    body = {"actor": "host", **valid}
    response = await env.call("POST", f"/goals/{goal.id}/{action}", json=body)
    assert (response.status_code, response.json()) == (400, {"error": "actor: Extra inputs are not permitted"})
    assert len(await journal_rows(env.store, goal)) == 1
    created = await env.call("POST", "/goals", json={**NEW, "actor": "host", "proposed_by": "agent"})
    assert created.status_code == 400
    assert "actor: Extra inputs are not permitted" in created.json()["error"]
    assert "proposed_by: Extra inputs are not permitted" in created.json()["error"]


@pytest.mark.parametrize(
    ("path", "body", "field"),
    [
        ("budget", {"total": "5000000"}, "total"),
        ("budget", {"day": True}, "day"),
        ("deadline", {"days": 1.5}, "days"),
        ("deadline", {}, "days"),
        ("note", {"text": "x" * 4001}, "text"),
        ("note", {"text": 5}, "text"),
        ("create", {**NEW, "title": "t" * 201}, "title"),
        ("create", {**NEW, "objective": "o" * 4001}, "objective"),
        ("create", {**NEW, "done_when": "d" * 2001}, "done_when"),
        ("create", {**NEW, "limits": {"total_budget": True}}, "limits.total_budget"),
        ("create", {**NEW, "limits": {"mode": "full_autonomy"}}, "limits.mode"),
        ("create", {**NEW, "origin_channel": 42}, "origin_channel"),
        ("create", {"title": "only a title"}, "objective"),
        ("pause", {"extra": 1}, "extra"),
    ],
)
async def test_bodies_are_validated(db, path: str, body: dict, field: str) -> None:
    env = make_env(db)
    goal = await draft(env.store)
    url = "/goals" if path == "create" else f"/goals/{goal.id}/{path}"
    response = await env.call("POST", url, json=body)
    assert response.status_code == 400
    assert response.json()["error"].startswith(f"{field}: ") or f"; {field}: " in response.json()["error"]
    assert len(await journal_rows(env.store, goal)) == 1
    assert len((await env.call("GET", "/goals")).json()["goals"]) == 1


async def test_a_body_that_is_not_a_json_object_is_400(db) -> None:
    env = make_env(db)
    goal = await draft(env.store)
    for content, error in ((b"[1]", "the body must be a JSON object"), (b"{not json", "the body must be JSON")):
        response = await env.call(
            "POST", f"/goals/{goal.id}/pause", content=content, headers={**PROXY, "content-type": "application/json"}
        )
        assert (response.status_code, response.json()) == (400, {"error": error})


async def test_a_race_is_a_409_not_a_500(db) -> None:
    env = make_env(db)
    goal = await draft(env.store)
    first, second = await asyncio.gather(
        env.call("POST", f"/goals/{goal.id}/approve"), env.call("POST", f"/goals/{goal.id}/approve")
    )
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    refused = first if first.status_code == 409 else second
    error = refused.json()["error"]
    assert error.endswith("only a draft can be approved") or "changed meanwhile" in error
    approvals = [row for row in await journal_rows(env.store, goal) if row.text.startswith("Approved")]
    assert len(approvals) == 1


async def test_an_unexpected_error_is_500_with_no_detail_and_logs_the_class_only(db, monkeypatch, caplog) -> None:
    env = make_env(db)
    goal = await active(env.store)

    async def broken(*_args, **_kwargs):
        raise RuntimeError("secret text")

    monkeypatch.setattr(env.store, "pause", broken)
    caplog.set_level(logging.DEBUG)
    response = await env.call("POST", f"/goals/{goal.id}/pause")
    assert (response.status_code, response.json()) == (500, {"error": "the request could not be processed"})
    assert any("RuntimeError" in record.getMessage() for record in caplog.records)
    assert not any("secret text" in record.getMessage() + repr(record.args) for record in caplog.records)
    assert "secret text" not in response.text


async def test_an_unknown_goal_is_404_before_the_store_is_asked(db, monkeypatch) -> None:
    env = make_env(db)
    called: list[str] = []

    async def pause(*args, **kwargs):
        called.append("pause")

    monkeypatch.setattr(env.store, "pause", pause)
    response = await env.call("POST", "/goals/not-a-goal/pause")
    assert (response.status_code, response.json(), called) == (404, {"error": "no goal not-a-goal"}, [])


async def test_the_app_mounts_the_goal_routes_over_the_store_it_builds_when_goals_are_on(db, tmp_path) -> None:
    """Through ``create_app``, as the server mounts them: the store rest.py builds writes and reads real rows."""
    from nous.api.rest import create_app
    from nous.config import Settings

    settings = Settings(
        _env_file=None,
        agent_id=f"goal-app-{uuid.uuid4().hex[:8]}",
        workspace_dir=str(tmp_path),
        owner_route_secret_sha256=digest(OWNER_VALUE),
        telegram_chat_id="",
        goals_enabled=True,
        result_inbox_enabled=True,
        intentions_enabled=True,
        continuation_enabled=True,
        subtask_enabled=True,
    )
    assert settings.goals_enabled is True
    app = create_app(
        SimpleNamespace(), SimpleNamespace(), SimpleNamespace(schedules=None), SimpleNamespace(), db, settings,
        heartbeat_runner=SimpleNamespace(finding_store=None),
    )
    made = await send(app, "POST", "/goals", json={**NEW, "origin_channel": CHAN}, headers=OWNER)
    assert made.status_code == 201, made.text
    listed = await send(app, "GET", "/goals", headers=OWNER)
    assert [row["id"] for row in listed.json()["goals"]] == [made.json()["id"]]
    approved = await send(app, "POST", f"/goals/{made.json()['ref']}/approve", headers=OWNER)
    assert (approved.status_code, approved.json()["state"]) == (200, "active")


@pytest.mark.parametrize("path", ["pause", "create"])
async def test_a_change_that_was_made_is_200_with_reload_when_reading_it_back_fails(
    db, monkeypatch, caplog, path: str
) -> None:
    """The change committed, then reading the goal back failed: the caller is told what the store returned and to
    reload, never a 500 for a change that was made."""
    env = make_env(db)
    goal = await active(env.store)

    async def broken(*_args, **_kwargs):
        raise RuntimeError("secret text")

    monkeypatch.setattr(env.store, "sessions", broken)
    caplog.set_level(logging.DEBUG)
    if path == "create":
        response = await env.call("POST", "/goals", json={**NEW, "origin_channel": CHAN})
        assert response.status_code == 201
        made = await env.store.get(response.json()["id"])
        assert made is not None and made.state == "draft"
        assert response.json() == {
            "enabled": True, "reload": True, "id": str(made.id), "ref": format_short_id(made.short_id),
            "title": NEW["title"], "state": "draft", "pause_reason": None, "pending_request": None,
        }
    else:
        response = await env.call("POST", f"/goals/{goal.id}/pause")
        assert response.status_code == 200
        assert response.json()["reload"] is True
        assert (response.json()["state"], response.json()["pause_reason"]) == ("paused", "owner")
        assert (await reread(env.store, goal)).state == "paused"
    assert any("reading the goal back failed (RuntimeError)" in record.getMessage() for record in caplog.records)
    assert not any("secret text" in record.getMessage() for record in caplog.records)
    assert "secret text" not in response.text
