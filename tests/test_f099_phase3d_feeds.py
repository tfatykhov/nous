"""F099 Phase 3d: the seven model-facing feeds (R6) and the REST list that must stay whole."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from f099_calibration_support import arrival, brain_of, make_decision
from f099_support import CONT, env_factory, make_root  # noqa: F401

pytestmark = pytest.mark.postgres_only
ON = {**CONT, "intention_calibration_enabled": True}
FLAGS = [(ON, False), (CONT, True)]
IDS = ["on", "off-PIN"]


async def _world(env):
    root = await make_root(env)
    linked = (await arrival(env, root, 1)).decision_id
    ordinary = await make_decision(env, session_id="chat-9")
    in_session = await make_decision(env, session_id=f"intent-{root.id}", description="Chose the Friday slot")
    return root, linked, ordinary, in_session


class Recorder:
    """An automatic-review signal that records every decision it is shown and grades none."""

    def __init__(self):
        self.seen: list = []

    async def check(self, decision):
        self.seen.append(decision.id)
        return None


def _reviewer(env):
    from nous.events import EventBus
    from nous.handlers.decision_reviewer import DecisionReviewer

    # DecisionReviewer.__init__ calls bus.on("session_ended", ...) unconditionally, so it needs a real bus
    # (the pattern of tests/test_fix_m_loops_foreign_cancel.py).
    reviewer = DecisionReviewer(brain_of(env), env.settings, EventBus())
    reviewer._signals = [Recorder()]
    return reviewer


def _heart():
    heart = MagicMock()
    heart.censors.list_active = AsyncMock(return_value=[])
    heart.facts.count_stale = AsyncMock(return_value=0)
    heart.procedures.get_low_effectiveness = AsyncMock(return_value=[])
    return heart


# ---- 1. DecisionReviewer.sweep ----------------------------------------------------------------------------------


@pytest.mark.parametrize(("flags", "linked_seen"), FLAGS, ids=IDS)
async def test_the_automatic_reviewers_sweep(env_factory, flags, linked_seen):  # noqa: F811
    env = await env_factory(**flags)
    _root, linked, ordinary, in_session = await _world(env)
    reviewer = _reviewer(env)
    await reviewer.sweep()
    seen = set(reviewer._signals[0].seen)
    assert {ordinary, in_session} <= seen and (linked in seen) is linked_seen


# ---- 2. DecisionReviewer.handle (get_session_decisions) -------------------------------------------------------


@pytest.mark.parametrize(("flags", "linked_seen"), FLAGS, ids=IDS)
async def test_the_session_end_review_skips_the_arrival_decision_but_not_the_sessions_own_row(
    env_factory,  # noqa: F811
    flags,
    linked_seen,
):
    from nous.events import Event

    env = await env_factory(**flags)
    root, linked, _ordinary, in_session = await _world(env)
    reviewer = _reviewer(env)
    reviewer.sweep = AsyncMock(return_value=[])  # handle() sweeps after the session: keep this test to the session
    await reviewer.handle(Event(type="session_ended", agent_id=env.agent, data={"session_id": f"intent-{root.id}"}))
    seen = set(reviewer._signals[0].seen)
    assert in_session in seen and (linked in seen) is linked_seen


# ---- 3. the unreviewed_decisions micro-app source ----------------------------------------------------------------


@pytest.mark.parametrize(("flags", "linked_seen"), FLAGS, ids=IDS)
async def test_the_unreviewed_decisions_source(env_factory, flags, linked_seen):  # noqa: F811
    from nous.a2ui.sources import build_default_registry

    env = await env_factory(**flags)
    _root, linked, ordinary, in_session = await _world(env)
    registry = build_default_registry(brain=brain_of(env), settings=env.settings)
    resolved = await registry.resolve([{"source": "unreviewed_decisions", "key": "d", "params": {}}])
    ids = {row["id"] for row in resolved["d"]}
    assert {str(ordinary), str(in_session)} <= ids and (str(linked) in ids) is linked_seen


# ---- 4. the decision_sweep card's own sourcing -----------------------------------------------------------------


@pytest.mark.parametrize(("flags", "linked_seen"), FLAGS, ids=IDS)
async def test_the_decision_sweep_card(env_factory, flags, linked_seen):  # noqa: F811
    from nous.a2ui.tools import register_a2ui_tools

    env = await env_factory(**flags)
    _root, linked, ordinary, in_session = await _world(env)
    captured: dict = {}

    class Dispatcher:
        def register(self, name, fn, schema):
            captured[name] = fn

    class Service:
        _settings = env.settings

        async def push_built(self, built, **kwargs):
            captured["built"] = built
            return "surface-1"

    register_a2ui_tools(Dispatcher(), Service(), brain=brain_of(env))
    result = await captured["push_surface"](template="decision_sweep", params={})
    assert result["is_error"] is False, result
    ids = set(captured["built"].data_model["decisions"])
    assert {str(ordinary), str(in_session)} <= ids and (str(linked) in ids) is linked_seen


# ---- 5. the heartbeat health check -----------------------------------------------------------------------------


@pytest.mark.parametrize(("flags", "count"), [(ON, 2), (CONT, 3)], ids=IDS)
async def test_the_health_check_counts(env_factory, flags, count):  # noqa: F811
    from nous.heartbeat.checks import HealthCheck

    env = await env_factory(**flags)
    await _world(env)
    result = await HealthCheck(heart=_heart(), brain=brain_of(env), settings=env.settings).run()
    (finding,) = [f for f in result.findings if "pending review" in f.summary]
    assert f"{count} decisions pending review" in finding.summary


# ---- 6. GET /decisions/unreviewed (and GET /decisions, which stays whole) ---------------------------------------


@pytest.fixture
def rest_client_for():
    """``rest_client_for(env)``: an HTTP client on the REST app over a real Brain of ``env`` (the wiring of
    tests/test_rest.py; no auth headers are needed)."""
    from httpx import ASGITransport, AsyncClient
    from test_rest import MockAgentRunner

    from nous.api.rest import create_app
    from nous.cognitive.layer import CognitiveLayer

    def build(env):
        brain = brain_of(env)
        app = create_app(
            MockAgentRunner(),
            brain,
            env.heart,
            CognitiveLayer(brain, env.heart, env.settings, identity_prompt="You are Nous."),
            env.db,
            env.settings,
        )
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    return build


@pytest.mark.parametrize(("flags", "linked_seen"), FLAGS, ids=IDS)
async def test_get_decisions_unreviewed(env_factory, rest_client_for, flags, linked_seen):  # noqa: F811
    env = await env_factory(**flags)
    _root, linked, ordinary, in_session = await _world(env)
    async with rest_client_for(env) as client:
        body = (await client.get("/decisions/unreviewed")).json()
    ids = {d["id"] for d in body["decisions"]}
    assert {str(ordinary), str(in_session)} <= ids and (str(linked) in ids) is linked_seen


async def test_get_decisions_stays_unfiltered_with_the_flag_on(env_factory, rest_client_for):  # noqa: F811
    """The dashboard shows everything (R6)."""
    env = await env_factory(**ON)
    _root, linked, ordinary, in_session = await _world(env)
    async with rest_client_for(env) as client:
        body = (await client.get("/decisions?limit=50")).json()
    assert {str(linked), str(ordinary), str(in_session)} <= {d["id"] for d in body["decisions"]}


# ---- 7. the list_decisions agent tool ---------------------------------------------------------------------------


@pytest.mark.parametrize(("flags", "linked_seen"), FLAGS, ids=IDS)
async def test_the_list_decisions_tool(env_factory, flags, linked_seen):  # noqa: F811
    from nous.api.tools import create_nous_tools

    env = await env_factory(**flags)
    _root, linked, ordinary, in_session = await _world(env)
    tools = create_nous_tools(brain_of(env), env.heart, settings=env.settings)
    text = (await tools["list_decisions"](outcome="pending", limit=50))["content"][0]["text"]
    assert str(ordinary) in text and str(in_session) in text and (str(linked) in text) is linked_seen


# ---- the exact-signature doubles of today still work with the flag off -------------------------------------------


async def test_flag_off_no_feed_passes_the_new_keyword(env_factory):  # noqa: F811
    """PIN. With calibration off every feed calls its reader exactly as before this PR: a double with the old
    signature (tests/test_a2ui_phase2.py, tests/test_fix_m_loops_foreign_cancel.py) is not given an unexpected
    keyword. The double records each call it ACCEPTS, because `handle` and the health check log and swallow a
    TypeError. Mutation: make `arrival_exclusion` return the keyword with the flag off, and `sweep` raises
    TypeError while the swallowed calls go missing from `calls`."""
    from nous.a2ui.sources import build_default_registry
    from nous.events import EventBus
    from nous.handlers.decision_reviewer import DecisionReviewer
    from nous.heartbeat.checks import HealthCheck

    env = await env_factory(**CONT)
    calls: list[str] = []

    class OldBrain:
        async def get_unreviewed(self, max_age_days=30, stakes=None, limit=None):
            calls.append("get_unreviewed")
            return []

        async def get_session_decisions(self, session_id, session=None):
            calls.append("get_session_decisions")
            return []

    reviewer = DecisionReviewer(OldBrain(), env.settings, EventBus())
    await reviewer.sweep()
    await reviewer.handle(SimpleNamespace(data={"session_id": "s"}, session_id="s"))  # the session, then a sweep
    registry = build_default_registry(brain=OldBrain(), settings=env.settings)
    await registry.resolve([{"source": "unreviewed_decisions", "key": "d", "params": {}}])
    await HealthCheck(heart=_heart(), brain=OldBrain(), settings=env.settings).run()
    assert calls == ["get_unreviewed", "get_session_decisions", "get_unreviewed", "get_unreviewed", "get_unreviewed"]
