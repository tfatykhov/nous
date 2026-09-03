"""F092.1 Phase 3: ephemeral micro-apps — grammar, composer, service gates,
navigable-readonly functions, compose_surface tool.

Postgres-only for the same reason as the other A2UI suites (ARRAY columns).
The composer's LLM is faked by patching ``call_background_llm`` in the
compose module namespace with a scripted response queue.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

import nous.a2ui.compose as compose_mod
from nous.a2ui.actions import ActionRouter
from nous.a2ui.compose import ComposedApp, SurfaceComposer
from nous.a2ui.dsl import BuiltSurface, SurfaceValidationError
from nous.a2ui.grammar import lint_micro_app
from nous.a2ui.sources import SourceRegistry, UnknownSourceError
from nous.storage.models import A2uiAction, A2uiOutbox, A2uiSurface

pytestmark = pytest.mark.postgres_only

JSON_CT = "application/json"


# ---------------------------------------------------------------------------
# Component fixtures
# ---------------------------------------------------------------------------


def _valid_components() -> list[dict]:
    return [
        {"id": "root", "component": "Column", "children": ["header", "stats", "sec1", "footer"], "align": "stretch"},
        {
            "id": "header",
            "component": "AppHeader",
            "title": "Italy — Sep 5-20",
            "subtitle": "departs in 7 days",
            "composedAt": {"path": "/meta/composedAt"},
            "staleAfterS": 3600,
        },
        {"id": "stats", "component": "StatRow", "children": ["t1"]},
        # Binds the `status` source so the unread-source rule (F093 §1.1) is
        # satisfied — a declared source must be bound by something.
        {"id": "t1", "component": "StatTile", "label": "Days out", "value": {"path": "/status/days_out"}},
        {"id": "sec1", "component": "Section", "title": "Flights", "child": "kv", "provenance": "model"},
        {"id": "kv", "component": "KeyValueTable", "rows": {"path": "/trip/flights"}},
        {"id": "footer", "component": "AppFooter", "refineOptions": [], "showRefresh": True},
    ]


def _llm_response(
    components: list[dict] | None = None,
    data_model: dict | None = None,
    refine_options: list | None = None,
) -> str:
    return json.dumps(
        {
            "title": "Italy — Sep 5-20",
            "archetype": "status",
            "components": components if components is not None else _valid_components(),
            "dataModel": data_model if data_model is not None else {"trip": {"flights": []}},
            "refine_options": refine_options if refine_options is not None else [
                {"id": "blockers", "label": "Just the blockers"}
            ],
        }
    )


class _ScriptedLLM:
    """Patched in place of call_background_llm — pops scripted responses."""

    def __init__(self, responses: list[str | None]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    async def __call__(self, client, model, system_prompt, user_message, max_tokens=800):
        self.calls.append(user_message)
        return self.responses.pop(0) if self.responses else None


@pytest.fixture
def scripted_llm(monkeypatch):
    def _install(responses: list[str | None]) -> _ScriptedLLM:
        fake = _ScriptedLLM(responses)
        monkeypatch.setattr(compose_mod, "call_background_llm", fake)
        return fake

    return _install


@pytest.fixture
def sources() -> SourceRegistry:
    registry = SourceRegistry()

    async def trip_status(params: dict) -> dict:
        return {"days_out": 7, "booked": "5/5"}

    registry.register("trip_status", trip_status)
    return registry


@pytest.fixture
def composer(a2ui_settings, sources: SourceRegistry) -> SurfaceComposer:
    return SurfaceComposer(object(), a2ui_settings, sources)


# ---------------------------------------------------------------------------
# Shared service fixtures (same shape as test_a2ui_phase2.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def a2ui_agent_id() -> str:
    return f"test-a2ui-app-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def a2ui_settings(settings, a2ui_agent_id: str):
    return settings.model_copy(
        update={
            "agent_id": a2ui_agent_id,
            "telegram_bot_token": None,
            "telegram_chat_id": None,
            "a2ui_max_live_apps": 2,
        }
    )


@pytest_asyncio.fixture
async def service(db, a2ui_settings, a2ui_agent_id: str):
    from nous.a2ui.service import SurfaceService

    svc = SurfaceService(db, a2ui_settings)
    yield svc
    async with db.session() as session:
        await session.execute(delete(A2uiAction).where(A2uiAction.agent_id == a2ui_agent_id))
        await session.execute(delete(A2uiOutbox).where(A2uiOutbox.agent_id == a2ui_agent_id))
        await session.execute(delete(A2uiSurface).where(A2uiSurface.agent_id == a2ui_agent_id))
        await session.commit()


async def _surface_row(db, surface_id: str) -> A2uiSurface:
    async with db.session() as session:
        row = await session.get(A2uiSurface, surface_id)
        assert row is not None
        return row


def _micro_app(app_spec: dict | None = None, title: str = "Test app") -> BuiltSurface:
    built = BuiltSurface(
        kind="micro_app",
        origin="chat",
        title=title,
        priority=0,
        allowed_actions=["app.close"],
        components=_valid_components(),
        data_model={"meta": {"composedAt": "2026-08-29T14:00:00+00:00"}, "trip": {"flights": []}},
        expires_in=None,
    )
    built.app_spec = app_spec or {
        "intent": "test",
        "archetype": "status",
        "composed_at": "2026-08-29T14:00:00+00:00",
        "refine_options": [{"id": "blockers", "label": "Just the blockers"}],
        "data_sources": [],
        "provenance": {"trip": "model"},
    }
    return built


# ---------------------------------------------------------------------------
# Grammar
# ---------------------------------------------------------------------------


def test_grammar_accepts_the_conforming_fixture() -> None:
    assert lint_micro_app(_valid_components()) == []


def test_grammar_rejects_banned_input_components() -> None:
    comps = _valid_components()
    comps[5] = {"id": "kv", "component": "TextField", "label": "free text"}

    errors = lint_micro_app(comps)

    assert any("banned" in e for e in errors)


def test_grammar_rejects_untitled_sections_and_missing_stamp() -> None:
    comps = _valid_components()
    comps[4] = {"id": "sec1", "component": "Section", "title": "  ", "child": "kv"}
    comps[1] = {**comps[1]}
    del comps[1]["composedAt"]

    errors = lint_micro_app(comps)

    assert any("no title" in e for e in errors)
    assert any("composedAt" in e for e in errors)


def test_grammar_rejects_broken_skeletons() -> None:
    no_footer = [c for c in _valid_components() if c["id"] != "footer"]
    no_footer[0] = {**no_footer[0], "children": ["header", "stats", "sec1"]}
    assert any("skeleton" in e for e in lint_micro_app(no_footer))

    # Six sections exceed the 1-5 band.
    comps = _valid_components()
    extra_ids = [f"sec{i}" for i in range(2, 8)]
    for sid in extra_ids:
        comps.append({"id": sid, "component": "Section", "title": sid, "child": f"{sid}_b"})
        comps.append({"id": f"{sid}_b", "component": "Text", "text": "x"})
    comps[0] = {**comps[0], "children": ["header", "stats", "sec1", *extra_ids, "footer"]}
    assert any("1-5 Section" in e for e in lint_micro_app(comps))


def test_grammar_rejects_component_budget_and_depth() -> None:
    comps = _valid_components()
    for i in range(40):
        comps.append({"id": f"pad{i}", "component": "Text", "text": "x"})
    assert any("budget" in e for e in lint_micro_app(comps))

    deep = _valid_components()
    # Chain of nested Columns under the section body: sec1 > kv chain.
    deep[5] = {"id": "kv", "component": "Column", "children": ["d1"]}
    for i in range(1, 7):
        nxt = f"d{i + 1}"
        deep.append({"id": f"d{i}", "component": "Column", "children": [nxt]})
    deep.append({"id": "d7", "component": "Text", "text": "bottom"})
    assert any("depth" in e for e in lint_micro_app(deep))


def test_grammar_rejects_duplicate_and_multi_parent_refs() -> None:
    comps = _valid_components()
    comps[2] = {"id": "stats", "component": "StatRow", "children": ["t1", "t1"]}
    errors = lint_micro_app(comps)
    assert any("duplicate child refs" in e for e in errors)

    comps = _valid_components()
    comps[4] = {"id": "sec1", "component": "Section", "title": "Flights", "child": "t1"}
    errors = lint_micro_app(comps)
    assert any("one parent per component" in e for e in errors)


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------


async def test_compose_happy_path_merges_server_data_and_stamps_provenance(
    composer: SurfaceComposer, scripted_llm
) -> None:
    scripted_llm([_llm_response()])

    composed = await composer.compose(
        "show me my vacation plans",
        data_sources=[{"key": "status", "source": "trip_status"}],
    )

    assert not composed.fallback
    built = composed.built
    assert built.kind == "micro_app"
    assert built.allowed_actions == ["app.close"]
    assert built.expires_in is None
    # Server authority: sourced key injected, meta stamped server-side.
    assert built.data_model["status"] == {"days_out": 7, "booked": "5/5"}
    assert built.data_model["meta"]["composedAt"]
    # The model-supplied subtree is recorded, the sourced one is not.
    assert composed.app_spec["provenance"] == {"trip": "model"}
    assert composed.app_spec["refine_options"] == [{"id": "blockers", "label": "Just the blockers"}]
    # Footer renders the SERVER-cleaned options.
    footer = next(c for c in built.components if c["id"] == "footer")
    assert footer["refineOptions"] == [{"id": "blockers", "label": "Just the blockers"}]


async def test_compose_repairs_a_rejected_attempt(
    composer: SurfaceComposer, scripted_llm
) -> None:
    bad = _valid_components()
    bad[1] = {**bad[1], "composedAt": "2026-08-29T14:00:00Z"}  # literal, not binding
    llm = scripted_llm([_llm_response(components=bad), _llm_response()])

    composed = await composer.compose("vacation")

    assert not composed.fallback
    assert composed.repairs == 1
    assert "REJECTED" in llm.calls[1]
    assert "/meta/composedAt" in llm.calls[1]


async def test_compose_falls_back_to_markdown_after_max_repairs(
    composer: SurfaceComposer, scripted_llm
) -> None:
    llm = scripted_llm(["not json", "still not json", "{\"components\": []}"])

    composed = await composer.compose(
        "vacation", data_sources=[{"key": "status", "source": "trip_status"}]
    )

    assert composed.fallback
    assert len(llm.calls) == 3
    built = composed.built
    # The fallback is itself a conforming micro-app — degraded, never broken.
    built.validate()
    assert lint_micro_app(built.components) == []
    assert built.allowed_actions == ["app.close"]
    assert built.data_model["status"] == {"days_out": 7, "booked": "5/5"}


async def test_compose_rejects_a_model_shadowing_sourced_keys(
    composer: SurfaceComposer, scripted_llm
) -> None:
    llm = scripted_llm(
        [
            _llm_response(data_model={"status": {"days_out": 999}}),
            _llm_response(),
        ]
    )

    composed = await composer.compose(
        "vacation", data_sources=[{"key": "status", "source": "trip_status"}]
    )

    assert not composed.fallback
    assert composed.repairs == 1
    assert "shadows a server-resolved source" in llm.calls[1]


async def test_compose_propagates_unknown_sources_to_the_caller(
    composer: SurfaceComposer, scripted_llm
) -> None:
    scripted_llm([_llm_response()])

    with pytest.raises(UnknownSourceError):
        await composer.compose("x", data_sources=[{"key": "a", "source": "nope"}])


async def test_refresh_data_reruns_sources_and_restamps(
    composer: SurfaceComposer,
) -> None:
    patches = await composer.refresh_data(
        {"data_sources": [{"key": "status", "source": "trip_status"}]}
    )

    assert patches["status"] == {"days_out": 7, "booked": "5/5"}
    assert patches["meta"]["composedAt"]


async def test_refresh_refuses_an_unsourced_app(composer: SurfaceComposer) -> None:
    """Codex P2: a restamp with nothing re-read would advance the header
    over content that did not move."""
    with pytest.raises(ValueError, match="nothing to refresh"):
        await composer.refresh_data({"data_sources": []})


async def test_unsourced_compose_withholds_the_refresh_control(
    composer: SurfaceComposer, scripted_llm
) -> None:
    scripted_llm([_llm_response(), _llm_response()])

    unsourced = await composer.compose("vacation")
    sourced = await composer.compose(
        "vacation", data_sources=[{"key": "status", "source": "trip_status"}]
    )

    def footer(c):
        return next(x for x in c.built.components if x["id"] == "footer")

    assert footer(unsourced)["showRefresh"] is False
    assert footer(sourced)["showRefresh"] is True


# ---------------------------------------------------------------------------
# Service gates: fail-closed creation + cap/LRU
# ---------------------------------------------------------------------------


async def test_push_rejects_micro_app_with_extra_actions(service) -> None:
    built = _micro_app()
    built.allowed_actions = ["app.close", "dag.cancel"]

    with pytest.raises(ValueError, match="app.close"):
        await service.push_built(built)


async def test_push_rejects_blocking_priority(service) -> None:
    built = _micro_app()
    built.priority = 2

    with pytest.raises(ValueError, match="never blocking"):
        await service.push_built(built)


async def test_push_rejects_grammar_violations(service) -> None:
    built = _micro_app()
    built.components[5] = {"id": "kv", "component": "TextField", "label": "x"}

    with pytest.raises(SurfaceValidationError):
        await service.push_built(built)


async def test_push_persists_app_spec_and_null_expiry(service, db) -> None:
    surface_id = await service.push_built(_micro_app())

    row = await _surface_row(db, surface_id)
    assert row.kind == "micro_app"
    assert row.expires_at is None
    assert row.app_spec["refine_options"] == [{"id": "blockers", "label": "Just the blockers"}]


async def test_cap_evicts_least_recently_touched(service, db, a2ui_agent_id: str) -> None:
    """Cap is 2 (fixture). Pushing a third evicts the least-recently-updated;
    touching the oldest first must protect it."""
    first = await service.push_built(_micro_app(title="first"))
    second = await service.push_built(_micro_app(title="second"))
    # Touch FIRST so second becomes the LRU victim.
    await service.update_data(first, "/trip/flights", [{"key": "UA", "value": "booked"}])

    third = await service.push_built(_micro_app(title="third"))

    assert (await _surface_row(db, first)).status == "live"
    assert (await _surface_row(db, second)).status == "expired"
    assert (await _surface_row(db, third)).status == "live"
    # Eviction emitted a teardown envelope for the victim.
    async with db.session() as session:
        envs = (
            await session.execute(
                select(A2uiOutbox.envelope).where(
                    A2uiOutbox.agent_id == a2ui_agent_id,
                    A2uiOutbox.surface_id == second,
                )
            )
        ).scalars().all()
    assert any("deleteSurface" in e for e in envs)


async def test_dedup_replacement_updates_the_origin(service, db) -> None:
    """Codex round 4: a background compose replacing a chat-origin app via
    the shared dedup key must persist origin='agent' or the push/pull
    measurement misclassifies it."""
    chat_app = _micro_app(title="chat version")
    surface_id = await service.push_built(chat_app, dedup_key="app:same-intent")
    assert (await _surface_row(db, surface_id)).origin == "chat"

    agent_app = _micro_app(title="agent version")
    agent_app.origin = "agent"
    replaced = await service.push_built(agent_app, dedup_key="app:same-intent")

    assert replaced == surface_id
    assert (await _surface_row(db, surface_id)).origin == "agent"


async def test_dedup_replacement_timestamp_never_moves_backwards(service, db) -> None:
    """Codex round 10: a replacement can wait out a refine on the surface
    lock; stamping the pre-wait clock would move updated_at BACKWARDS past
    that mutation, making the freshly replaced app the LRU victim."""
    from datetime import UTC, datetime, timedelta

    surface_id = await service.push_built(_micro_app(title="v1"), dedup_key="app:ts")
    await service.update_data(surface_id, "/trip", {"flights": ["touched"]})
    touched_at = (await _surface_row(db, surface_id)).updated_at

    stale_now = datetime.now(UTC) - timedelta(minutes=5)
    await service._push_transaction(
        _micro_app(title="v2"),
        dedup_key="app:ts",
        session_id=None,
        notify=False,
        _dedup_retry=False,
        agent_id=service._settings.agent_id,
        now=stale_now,
        expires_at=None,
        _locked_surface_id=surface_id,
    )

    row = await _surface_row(db, surface_id)
    assert row.title == "v2"
    assert row.updated_at >= touched_at, "replacement stamps a fresh clock, never the pre-wait one"


async def test_reconcile_spares_a_victim_touched_after_selection(
    db, a2ui_settings, a2ui_agent_id: str
) -> None:
    """Codex round 10: the victim is re-validated under its lock — a
    refine/refresh landing between selection and lock acquisition makes it
    no longer the LRU, and evicting on the stale ranking would delete an
    actively used app."""
    from contextlib import asynccontextmanager

    from nous.a2ui.service import SurfaceService

    svc = SurfaceService(db, a2ui_settings)
    a = await svc.push_built(_micro_app(title="oldest"))
    b = await svc.push_built(_micro_app(title="middle"))

    orig_lock = svc.surface_lock
    bumped: dict[str, bool] = {}

    @asynccontextmanager
    async def touching_lock(surface_id: str):
        # Simulate the race: the selected victim gets touched right before
        # the reconciler acquires its lock.
        if surface_id == a and not bumped:
            bumped[surface_id] = True
            await svc.update_data(a, "/trip", {"flights": ["still in use"]})
        async with orig_lock(surface_id):
            yield

    svc.surface_lock = touching_lock  # type: ignore[method-assign]
    try:
        # Third push (cap 2) selects `a` as the victim; the hook touches it
        # first, so the under-lock recheck must spare it.
        await svc.push_built(_micro_app(title="newest"))
    finally:
        svc.surface_lock = orig_lock  # type: ignore[method-assign]

    assert (await _surface_row(db, a)).status == "live", "touched victim spared"
    # Under-eviction is the accepted trade: the next push reconciles again.
    async with db.session() as session:
        live = (
            await session.execute(
                select(A2uiSurface).where(
                    A2uiSurface.agent_id == a2ui_agent_id,
                    A2uiSurface.kind == "micro_app",
                    A2uiSurface.status == "live",
                )
            )
        ).scalars().all()
    assert {s.title for s in live} >= {"oldest", "newest"}
    # Cleanup rows created outside the shared `service` fixture.
    async with db.session() as session:
        await session.execute(delete(A2uiOutbox).where(A2uiOutbox.agent_id == a2ui_agent_id))
        await session.execute(delete(A2uiSurface).where(A2uiSurface.agent_id == a2ui_agent_id))
        await session.commit()
    assert b  # silence unused warning


async def test_concurrent_pushes_converge_to_the_cap(
    service, db, a2ui_agent_id: str
) -> None:
    """Codex round 9: a pre-insert cap check was a TOCTOU — concurrent
    pushes admitted against the same snapshot. Post-insert reconciliation
    converges: whichever insert lands last, its reconcile sees the full
    population and evicts down to the cap."""
    import asyncio

    await asyncio.gather(
        *(service.push_built(_micro_app(title=f"app{i}")) for i in range(4))
    )

    async with db.session() as session:
        live = (
            await session.execute(
                select(A2uiSurface).where(
                    A2uiSurface.agent_id == a2ui_agent_id,
                    A2uiSurface.kind == "micro_app",
                    A2uiSurface.status == "live",
                )
            )
        ).scalars().all()
    assert len(live) == 2, "cap holds under concurrency (fixture cap = 2)"


async def test_late_dedup_match_reenters_the_locked_path(service, db) -> None:
    """Codex round 7: when the preliminary lookup misses but the inner one
    hits (two first-time producers raced), the replacement must NOT commit
    on the unlocked branch. Exercised by calling _push_transaction directly
    with _locked=False against an existing live dedup match."""
    from datetime import UTC, datetime

    first = await service.push_built(_micro_app(title="winner"), dedup_key="app:race7")
    old_nonce = (await _surface_row(db, first)).nonce

    loser = _micro_app(title="loser-turned-replacement")
    replaced = await service._push_transaction(
        loser,
        dedup_key="app:race7",
        session_id=None,
        notify=False,
        _dedup_retry=False,
        agent_id=service._settings.agent_id,
        now=datetime.now(UTC),
        expires_at=None,
        _locked_surface_id=None,
    )

    assert replaced == first, "re-entered as a replacement, not a duplicate insert"
    row = await _surface_row(db, first)
    assert row.title == "loser-turned-replacement"
    assert row.nonce != old_nonce, "the locked replacement path rotated the nonce"


async def test_stale_lock_identity_reenters_for_the_current_winner(service, db) -> None:
    """Codex round 8: holding SOME surface lock is not holding the RIGHT
    one. A producer that locked S1 (since closed) must not replace S2 — the
    lookup's row identity is compared against the held lock's."""
    from datetime import UTC, datetime

    current = await service.push_built(_micro_app(title="S2-winner"), dedup_key="app:race8")
    old_nonce = (await _surface_row(db, current)).nonce

    replaced = await service._push_transaction(
        _micro_app(title="replacement-under-right-lock"),
        dedup_key="app:race8",
        session_id=None,
        notify=False,
        _dedup_retry=False,
        agent_id=service._settings.agent_id,
        now=datetime.now(UTC),
        expires_at=None,
        # Simulates having locked a DIFFERENT (since-closed) surface: the
        # identity check must refuse and re-enter for the current winner.
        _locked_surface_id="nous:chat:micro_app:dead01",
    )

    assert replaced == current
    row = await _surface_row(db, current)
    assert row.title == "replacement-under-right-lock"
    assert row.nonce != old_nonce


async def test_fallback_push_is_refused_against_a_live_healthy_app(service, db) -> None:
    """F092.3: the guard runs under the SAME lock/transaction that performs
    the replacement, so a degraded render never lands on a healthy app."""
    from nous.a2ui.service import FallbackOverwriteRefused

    surface_id = await service.push_built(
        _micro_app(title="authored report"), dedup_key="app:guard"
    )
    before = await _surface_row(db, surface_id)
    old_nonce, old_title = before.nonce, before.title

    stub = _micro_app(title="Could not compose")
    stub.app_spec = {**(stub.app_spec or {}), "archetype": "fallback"}
    with pytest.raises(FallbackOverwriteRefused) as excinfo:
        await service.push_built(
            stub, dedup_key="app:guard", refuse_fallback_overwrite=True
        )
    assert "PRESERVED" in str(excinfo.value)

    row = await _surface_row(db, surface_id)
    assert row.title == old_title, "the healthy app survived"
    assert row.nonce == old_nonce, "no replacement happened — nonce not rotated"


async def test_fallback_flag_survives_the_insert_race_retry(service, db) -> None:
    """Codex P1 (F092.3): a fallback and a healthy compose can BOTH observe
    no row for a fresh dedup_key. If the healthy insert wins, the fallback
    hits the IntegrityError retry — which re-enters push_built and lands on
    the update-in-place path. Dropping ``refuse_fallback_overwrite`` on that
    hop would let the degraded stub overwrite the app that just published,
    which is exactly the race the guard exists for.
    """
    from contextlib import asynccontextmanager

    from nous.a2ui.service import FallbackOverwriteRefused

    opened: list[int] = []
    winner: dict[str, str] = {}
    orig_session = service._db.session

    @asynccontextmanager
    async def racing_session():
        async with orig_session() as session:
            opened.append(1)
            # Session 1 is push_built's read-only preliminary dedup lookup;
            # session 2 is the write transaction. Slipping the healthy
            # producer in immediately BEFORE its flush reproduces the real
            # race: a genuine unique-violation IntegrityError, not a faked
            # one — the winner's row is committed while this insert is
            # still unwritten.
            if len(opened) == 2:
                orig_flush = session.flush

                async def flush_after_losing_the_race(*args, **kwargs):
                    if not winner:
                        winner["id"] = await service.push_built(
                            _micro_app(title="healthy winner"),
                            dedup_key="app:race-p1",
                        )
                    return await orig_flush(*args, **kwargs)

                session.flush = flush_after_losing_the_race  # type: ignore[method-assign]
            yield session

    stub = _micro_app(title="Could not compose")
    stub.app_spec = {**(stub.app_spec or {}), "archetype": "fallback"}

    service._db.session = racing_session  # type: ignore[method-assign]
    try:
        with pytest.raises(FallbackOverwriteRefused):
            await service.push_built(
                stub, dedup_key="app:race-p1", refuse_fallback_overwrite=True
            )
    finally:
        service._db.session = orig_session  # type: ignore[method-assign]

    assert winner, "the race hook ran — the retry path was actually exercised"
    row = await _surface_row(db, winner["id"])
    assert row.title == "healthy winner", "the winner survived its loser's retry"
    assert row.status == "live"


async def test_dedup_update_does_not_evict(service, db) -> None:
    first = await service.push_built(_micro_app(title="first"), dedup_key="app:one")
    second = await service.push_built(_micro_app(title="second"), dedup_key="app:two")

    replaced = await service.push_built(_micro_app(title="one again"), dedup_key="app:one")

    assert replaced == first
    assert (await _surface_row(db, first)).status == "live"
    assert (await _surface_row(db, second)).status == "live"


async def test_update_components_replaces_tree_and_app_spec(service, db, a2ui_agent_id) -> None:
    surface_id = await service.push_built(_micro_app())
    before = (await _surface_row(db, surface_id)).updated_at
    new_components = _valid_components()
    new_components[4] = {"id": "sec1", "component": "Section", "title": "Blockers", "child": "kv"}
    new_spec = {"intent": "test", "refine_options": [], "data_sources": [], "provenance": {}}

    await service.update_components(surface_id, new_components, app_spec=new_spec)

    row = await _surface_row(db, surface_id)
    assert row.components[4]["title"] == "Blockers"
    assert row.app_spec == new_spec
    assert row.updated_at >= before
    async with db.session() as session:
        envs = (
            await session.execute(
                select(A2uiOutbox.envelope).where(
                    A2uiOutbox.agent_id == a2ui_agent_id,
                    A2uiOutbox.surface_id == surface_id,
                )
            )
        ).scalars().all()
    assert any("updateComponents" in e for e in envs)


# ---------------------------------------------------------------------------
# app.close / app.refresh / app.refine
# ---------------------------------------------------------------------------


class _FakeComposer:
    def __init__(self) -> None:
        self.refreshes: list[dict] = []
        self.compose_calls: list[str] = []
        # Theme the recomposition claims (a refine that "restyles"); the router
        # must pin it back to the existing surface's theme.
        self.compose_theme: str | None = None

    async def refresh_data(self, app_spec: dict) -> dict:
        self.refreshes.append(app_spec)
        return {"trip": {"flights": ["fresh"]}, "meta": {"composedAt": "2026-08-29T15:00:00+00:00"}}

    async def compose(self, intent: str, **kwargs: Any) -> ComposedApp:
        self.compose_calls.append(intent)
        built = _micro_app(title="refined")
        if self.compose_theme is not None:
            built.app_spec = {**built.app_spec, "theme": self.compose_theme}
        return ComposedApp(built=built, app_spec=built.app_spec, fallback=False, repairs=0)


@pytest.fixture
def fake_composer() -> _FakeComposer:
    return _FakeComposer()


@pytest.fixture
def router(db, a2ui_settings, service, fake_composer: _FakeComposer):
    return ActionRouter(db, a2ui_settings, service, composer=fake_composer)


def _action_body(name: str, surface_id: str, nonce: str, context: dict | None = None) -> dict:
    return {
        "version": "v1.0",
        "action": {
            "name": name,
            "surfaceId": surface_id,
            "context": dict(context or {}),
            "metadata": {"extensions": {"com_nous_nonce": nonce}},
        },
    }


def _call_body(surface_id: str, nonce: str, call: str, args: dict | None = None) -> dict:
    return {
        "version": "v1.0",
        "callAgentFunction": {
            "surfaceId": surface_id,
            "functionCallId": "fc-1",
            "callFunction": {"call": call, "args": dict(args or {})},
        },
        "metadata": {"extensions": {"com_nous_nonce": nonce}},
    }


async def test_app_close_resolves_and_audits(router, service, db, a2ui_agent_id) -> None:
    surface_id = await service.push_built(_micro_app())
    nonce = (await _surface_row(db, surface_id)).nonce

    status, payload = await router.handle(
        _action_body("app.close", surface_id, nonce), content_type=JSON_CT
    )

    assert status == 200
    assert payload["resolved"] is True
    assert (await _surface_row(db, surface_id)).status == "resolved"
    async with db.session() as session:
        audits = (
            await session.execute(
                select(A2uiAction).where(A2uiAction.agent_id == a2ui_agent_id)
            )
        ).scalars().all()
    assert [a.status for a in audits] == ["completed"]


async def test_app_refresh_reruns_sources_and_patches(
    router, service, db, fake_composer: _FakeComposer
) -> None:
    surface_id = await service.push_built(_micro_app())
    nonce = (await _surface_row(db, surface_id)).nonce

    status, payload = await router.handle_call(
        _call_body(surface_id, nonce, "app.refresh"), content_type=JSON_CT
    )

    assert status == 200
    assert payload["agentFunctionResponse"]["value"]["refreshed"] == ["meta", "trip"]
    row = await _surface_row(db, surface_id)
    assert row.data_model["trip"] == {"flights": ["fresh"]}
    assert row.data_model["meta"]["composedAt"] == "2026-08-29T15:00:00+00:00"
    assert fake_composer.refreshes, "refresh must consult the surface's app_spec"


async def test_app_refine_validates_against_app_spec(
    router, service, db, fake_composer: _FakeComposer
) -> None:
    surface_id = await service.push_built(_micro_app())
    nonce = (await _surface_row(db, surface_id)).nonce

    status, payload = await router.handle_call(
        _call_body(surface_id, nonce, "app.refine", {"id": "not-offered"}),
        content_type=JSON_CT,
    )

    assert status == 422
    assert "not offered" in payload["agentFunctionResponse"]["error"]["message"]
    assert fake_composer.compose_calls == []


async def test_app_refine_recomposes_the_same_surface(
    router, service, db, fake_composer: _FakeComposer
) -> None:
    surface_id = await service.push_built(_micro_app())
    nonce = (await _surface_row(db, surface_id)).nonce

    status, payload = await router.handle_call(
        _call_body(surface_id, nonce, "app.refine", {"id": "blockers"}),
        content_type=JSON_CT,
    )

    assert status == 200
    value = payload["agentFunctionResponse"]["value"]
    assert value["refined"] == "blockers"
    assert "Just the blockers" in fake_composer.compose_calls[0]
    row = await _surface_row(db, surface_id)
    assert row.status == "live", "refine recomposes in place, never tears down"
    assert row.nonce == nonce, "no nonce rotation on refine (updateComponents path)"


async def test_app_refine_pins_the_existing_theme(
    router, service, db, fake_composer: _FakeComposer
) -> None:
    # updateComponents carries no theme envelope, so a recomposed theme would
    # only surface on the next reconnect snapshot — an ambush. The refine must
    # pin the stored theme to the existing surface's (codex P2).
    spec = {
        "intent": "test",
        "archetype": "status",
        "composed_at": "2026-08-29T14:00:00+00:00",
        "refine_options": [{"id": "blockers", "label": "Just the blockers"}],
        "data_sources": [],
        "provenance": {"trip": "model"},
        "theme": "harbor",
    }
    surface_id = await service.push_built(_micro_app(app_spec=spec))
    nonce = (await _surface_row(db, surface_id)).nonce
    fake_composer.compose_theme = "signal"  # the recomposition tries to restyle

    status, _ = await router.handle_call(
        _call_body(surface_id, nonce, "app.refine", {"id": "blockers"}),
        content_type=JSON_CT,
    )

    assert status == 200
    row = await _surface_row(db, surface_id)
    assert row.app_spec["theme"] == "harbor", "refine must not change the live theme"


async def test_refine_and_refresh_write_audit_rows(
    router, service, db, a2ui_agent_id: str
) -> None:
    """Mutating functions are audited (rev-arch #2b) — a function that
    rewrites a surface is exactly what the evidence tier exists for."""
    surface_id = await service.push_built(_micro_app())
    nonce = (await _surface_row(db, surface_id)).nonce

    await router.handle_call(
        _call_body(surface_id, nonce, "app.refresh"), content_type=JSON_CT
    )
    await router.handle_call(
        _call_body(surface_id, nonce, "app.refine", {"id": "blockers"}),
        content_type=JSON_CT,
    )
    # Read-only functions stay unaudited.
    await router.handle_call(
        _call_body(surface_id, nonce, "loadDecisionDetail", {"decisionId": "x"}),
        content_type=JSON_CT,
    )

    async with db.session() as session:
        audits = (
            await session.execute(
                select(A2uiAction)
                .where(A2uiAction.agent_id == a2ui_agent_id)
                .order_by(A2uiAction.created_at)
            )
        ).scalars().all()
    assert [(a.action_name, a.status) for a in audits] == [
        ("app.refresh", "completed"),
        ("app.refine", "completed"),
    ]


class _BlockingHeart:
    async def check_censors(self, text: str):
        from types import SimpleNamespace

        return [SimpleNamespace(action="abort", reason="blocked prose", trigger_pattern="x")]


async def test_refine_is_censored_like_the_initial_push(
    router, service, db, a2ui_agent_id: str
) -> None:
    """rev-arch P1: an app censored on turn 1 must not be uncensored on
    every refine after — the recomposed content passes the same gate."""
    surface_id = await service.push_built(_micro_app())
    nonce = (await _surface_row(db, surface_id)).nonce
    service._heart = _BlockingHeart()  # attach AFTER push so creation passes

    status, payload = await router.handle_call(
        _call_body(surface_id, nonce, "app.refine", {"id": "blockers"}),
        content_type=JSON_CT,
    )

    assert status == 422
    assert "censor" in payload["agentFunctionResponse"]["error"]["message"]
    row = await _surface_row(db, surface_id)
    assert row.components[4]["title"] == "Flights", "blocked refine changes nothing"


async def test_refresh_is_censored(router, service, db) -> None:
    surface_id = await service.push_built(_micro_app())
    nonce = (await _surface_row(db, surface_id)).nonce
    service._heart = _BlockingHeart()

    status, payload = await router.handle_call(
        _call_body(surface_id, nonce, "app.refresh"), content_type=JSON_CT
    )

    assert status == 422
    assert "censor" in payload["agentFunctionResponse"]["error"]["message"]


async def test_update_components_lints_micro_apps(service, db) -> None:
    """The service-level guard for future callers — the composer lints its
    own output, but update_components must not trust its caller."""
    surface_id = await service.push_built(_micro_app())
    bad = _valid_components()
    bad[5] = {"id": "kv", "component": "TextField", "label": "x"}

    with pytest.raises(SurfaceValidationError):
        await service.update_components(surface_id, bad)


async def test_outbox_prunes_old_rows_of_live_surfaces(
    service, db, a2ui_agent_id: str
) -> None:
    """rev-arch #3: micro-apps never expire, so the age cutoff must apply
    to live surfaces' outbox rows too or a long-lived app grows the outbox
    without bound. Safe: reconnect is hydration-first."""
    from datetime import UTC, datetime, timedelta

    surface_id = await service.push_built(_micro_app())
    async with db.session() as session:
        from sqlalchemy import update as sa_update

        await session.execute(
            sa_update(A2uiOutbox)
            .where(A2uiOutbox.surface_id == surface_id)
            .values(created_at=datetime.now(UTC) - timedelta(hours=999))
        )
        await session.commit()

    await service.expire_sweep()

    assert (await _surface_row(db, surface_id)).status == "live", "surface untouched"
    async with db.session() as session:
        remaining = (
            await session.execute(
                select(A2uiOutbox).where(A2uiOutbox.surface_id == surface_id)
            )
        ).scalars().all()
    assert remaining == [], "aged rows of a LIVE surface are pruned"


async def test_compose_budget_exhaustion_is_the_callers_error(
    a2ui_settings, sources: SourceRegistry, scripted_llm
) -> None:
    throttled = a2ui_settings.model_copy(update={"a2ui_compose_max_per_hour": 1})
    composer = SurfaceComposer(object(), throttled, sources)
    scripted_llm([_llm_response(), _llm_response()])

    await composer.compose("first app")
    with pytest.raises(ValueError, match="budget exhausted"):
        await composer.compose("second app")


async def test_llm_transport_failure_is_terminal_not_a_repair_round(
    composer: SurfaceComposer, scripted_llm
) -> None:
    """A None return (transport failure/timeout) must fall back immediately
    — retrying an unreachable model burns the repair budget on nothing."""
    llm = scripted_llm([None, _llm_response()])

    composed = await composer.compose("vacation")

    assert composed.fallback
    assert len(llm.calls) == 1, "no repair round after a transport failure"


async def test_compose_repairs_non_object_component_entries(
    composer: SurfaceComposer, scripted_llm
) -> None:
    """Codex P2: a stray null in components must be a REPAIR error — the
    old filter-then-consume-unfiltered path raised AttributeError past both
    the repair loop and the fallback."""
    llm = scripted_llm(
        [
            _llm_response(components=[*_valid_components(), None]),
            _llm_response(),
        ]
    )

    composed = await composer.compose("vacation")

    assert not composed.fallback
    assert composed.repairs == 1
    assert "non-object" in llm.calls[1]


def test_grammar_rejects_non_stattile_statrow_children() -> None:
    """Codex round 3: the catalog only bounds StatRow children to <=4
    strings — a Text ref would render arbitrary content in the grid."""
    comps = _valid_components()
    comps.append({"id": "rogue", "component": "Text", "text": "not a stat"})
    comps[2] = {"id": "stats", "component": "StatRow", "children": ["t1", "rogue"]}

    errors = lint_micro_app(comps)

    assert any("StatTiles only" in e for e in errors)


async def test_background_compose_persists_agent_origin(fake_composer) -> None:
    """Codex round 3: a heartbeat/schedule compose must be origin='agent'
    or push apps are indistinguishable from pull apps."""
    from nous.a2ui.tools import register_a2ui_tools
    from nous.api.tools import ToolDispatcher

    captured: dict[str, str] = {}

    class _OriginComposer(_FakeComposer):
        async def compose(self, intent: str, **kwargs: Any) -> ComposedApp:
            captured["origin"] = kwargs.get("origin", "")
            return await super().compose(intent, **kwargs)

    dispatcher = ToolDispatcher()
    register_a2ui_tools(dispatcher, _CapturingService(), composer=_OriginComposer())

    await dispatcher.dispatch("compose_surface", {"intent": "x"}, is_background=True)
    assert captured["origin"] == "agent"
    await dispatcher.dispatch("compose_surface", {"intent": "x"})
    assert captured["origin"] == "chat"


async def test_source_limits_are_clamped_before_the_query() -> None:
    """Codex P2: the char budget trims AFTER materialization, so a prompted
    limit in the millions must be clamped before it reaches the fetcher."""
    from nous.a2ui.sources import _limit

    assert _limit({"limit": 10_000_000}, 10) == 50
    assert _limit({"limit": 5}, 10) == 5
    assert _limit({"limit": 0}, 10) == 1
    assert _limit({"limit": "junk"}, 10) == 10
    assert _limit({}, 10) == 10


async def test_source_resolve_bounds_oversized_values() -> None:
    registry = SourceRegistry()

    async def huge(params: dict) -> list[dict]:
        return [{"i": i, "text": "x" * 200} for i in range(200)]

    registry.register("huge", huge)

    model = await registry.resolve([{"key": "big", "source": "huge"}])

    rows = model["big"]
    assert rows[-1].get("_truncated") is True, "the cut is explicit, never silent"
    assert rows[-1]["omitted"] > 0
    import json as _json

    assert len(_json.dumps(model, default=str)) < 13_000


async def test_refresh_rejects_a_mid_flight_dedup_replacement(
    db, a2ui_settings, service
) -> None:
    """Codex round 5 TOCTOU: sources resolve against a PRE-lock snapshot;
    a dedup replacement committing in that window rotates the nonce, and
    the stale patches must NOT land on the new app."""

    class _ReplacingComposer(_FakeComposer):
        def __init__(self, svc, dedup_key: str) -> None:
            super().__init__()
            self._svc = svc
            self._dedup_key = dedup_key

        async def refresh_data(self, app_spec: dict) -> dict:
            # Simulate the race deterministically: the replacement commits
            # while the (slow) source fetch is still in flight.
            await self._svc.push_built(_micro_app(title="replacement"), dedup_key=self._dedup_key)
            return await super().refresh_data(app_spec)

    composer = _ReplacingComposer(service, "app:raced")
    router = ActionRouter(db, a2ui_settings, service, composer=composer)
    surface_id = await service.push_built(_micro_app(title="original"), dedup_key="app:raced")
    nonce = (await _surface_row(db, surface_id)).nonce

    status, payload = await router.handle_call(
        _call_body(surface_id, nonce, "app.refresh"), content_type=JSON_CT
    )

    # The call 403s at the nonce gate OR 422s at the epoch check depending
    # on where the replacement lands relative to the gate — either way the
    # stale patches never touch the replacement.
    assert status in (403, 422)
    row = await _surface_row(db, surface_id)
    assert row.title == "replacement"
    assert row.data_model["trip"] == {"flights": []}, "stale patches never landed"


async def test_refresh_rejects_an_overlapping_mutation_same_nonce(
    db, a2ui_settings, service
) -> None:
    """Codex round 6: refine/refresh/patches do NOT rotate the nonce, so a
    nonce-only epoch check let two overlapping calls interleave and the
    slower one overwrite newer work. updated_at is the complete revision."""

    class _MutatingComposer(_FakeComposer):
        def __init__(self, svc) -> None:
            super().__init__()
            self._svc = svc
            self.surface_id = ""

        async def refresh_data(self, app_spec: dict) -> dict:
            # A concurrent mutation commits during the slow fetch — it bumps
            # updated_at but leaves the nonce alone.
            await self._svc.update_data(self.surface_id, "/trip", {"flights": ["newer work"]})
            return await super().refresh_data(app_spec)

    composer = _MutatingComposer(service)
    router = ActionRouter(db, a2ui_settings, service, composer=composer)
    surface_id = await service.push_built(_micro_app(title="original"))
    composer.surface_id = surface_id
    nonce = (await _surface_row(db, surface_id)).nonce

    status, payload = await router.handle_call(
        _call_body(surface_id, nonce, "app.refresh"), content_type=JSON_CT
    )

    assert status == 422
    assert "changed while" in payload["agentFunctionResponse"]["error"]["message"]
    row = await _surface_row(db, surface_id)
    assert row.data_model["trip"] == {"flights": ["newer work"]}, (
        "the newer mutation survives; the stale refresh never lands"
    )


async def test_refine_rejects_a_mid_flight_dedup_replacement(
    db, a2ui_settings, service
) -> None:
    class _ReplacingComposer(_FakeComposer):
        def __init__(self, svc, dedup_key: str) -> None:
            super().__init__()
            self._svc = svc
            self._dedup_key = dedup_key

        async def compose(self, intent: str, **kwargs: Any) -> ComposedApp:
            await self._svc.push_built(_micro_app(title="replacement"), dedup_key=self._dedup_key)
            return await super().compose(intent, **kwargs)

    composer = _ReplacingComposer(service, "app:raced2")
    router = ActionRouter(db, a2ui_settings, service, composer=composer)
    surface_id = await service.push_built(_micro_app(title="original"), dedup_key="app:raced2")
    nonce = (await _surface_row(db, surface_id)).nonce

    status, _ = await router.handle_call(
        _call_body(surface_id, nonce, "app.refine", {"id": "blockers"}),
        content_type=JSON_CT,
    )

    assert status in (403, 422)
    assert (await _surface_row(db, surface_id)).title == "replacement"


async def test_micro_app_functions_report_unavailable_without_composer(
    db, a2ui_settings, service
) -> None:
    router = ActionRouter(db, a2ui_settings, service)  # no composer
    surface_id = await service.push_built(_micro_app())
    nonce = (await _surface_row(db, surface_id)).nonce

    status, payload = await router.handle_call(
        _call_body(surface_id, nonce, "app.refresh"), content_type=JSON_CT
    )

    assert status == 422
    assert "unavailable" in payload["agentFunctionResponse"]["error"]["message"]


# ---------------------------------------------------------------------------
# compose_surface tool
# ---------------------------------------------------------------------------


class _CapturingService:
    def __init__(self) -> None:
        self.pushed: list[tuple[Any, str | None]] = []
        self.calls: list[str] = []
        self.degraded: list[bool] = []

    async def push_built(
        self,
        built,
        dedup_key=None,
        session_id=None,
        notify=None,
        refuse_fallback_overwrite=False,
    ):
        self.calls.append("push_built")
        self.pushed.append((built, dedup_key))
        self.degraded.append(refuse_fallback_overwrite)
        return "nous:chat:micro_app:0001"


class _FallbackComposer(_FakeComposer):
    """Composer whose every compose degrades to the markdown fallback."""

    async def compose(self, intent: str, **kwargs: Any) -> ComposedApp:
        self.compose_calls.append(intent)
        built = _micro_app(title="Could not compose")
        built.app_spec = {**(built.app_spec or {}), "archetype": "fallback"}
        return ComposedApp(built=built, app_spec=built.app_spec, fallback=True, repairs=3)


class _RefusingService(_CapturingService):
    """Stands in for the real service refusing the push under its lock."""

    async def push_built(self, built, **kwargs):
        self.calls.append("push_built")
        self.degraded.append(kwargs.get("refuse_fallback_overwrite", False))
        if kwargs.get("refuse_fallback_overwrite"):
            from nous.a2ui.service import FallbackOverwriteRefused

            raise FallbackOverwriteRefused(
                "composition failed and the existing app was PRESERVED: "
                "'app:italy-vacation' is live with 53 components (archetype "
                "report), and the fallback render would have replaced it "
                "with a degraded stub. Nothing was published. Retry with a "
                "simpler intent or fewer sources, or update it in place: "
                "call P.pub('trip'). Do not report this action as successful."
            )
        return await super().push_built(built, **kwargs)


async def test_fallback_never_overwrites_a_healthy_app() -> None:
    """F092.3: a compose failure must not destroy the app already on
    screen. The real incident: one tap on an agent action recomposed a
    53-component authored app into a 5-component JSON dump."""
    from nous.a2ui.tools import register_a2ui_tools
    from nous.api.tools import ToolDispatcher

    dispatcher = ToolDispatcher()
    service = _RefusingService()
    register_a2ui_tools(dispatcher, service, composer=_FallbackComposer())

    text, is_error = await dispatcher.dispatch(
        "compose_surface", {"intent": "x", "dedup_key": "app:italy-vacation"}
    )

    assert is_error
    assert "PRESERVED" in text and "53 components" in text
    assert "call P.pub('trip')" in text  # the caller is told the right path
    assert "Do not report this action as successful." in text
    # The service's own refusal reaches the caller verbatim — not swallowed
    # into the generic "Failed to push composed app" wrapper.
    assert "Failed to push composed app" not in text
    assert service.pushed == []  # nothing published — the app survives


async def test_degraded_pushes_are_marked_and_healthy_ones_are_not() -> None:
    """Codex P2: the guard cannot be a pre-flight read in the tool — that is
    a TOCTOU. The tool's whole job is to MARK the push degraded; the service
    decides under the same lock that performs the replacement."""
    from nous.a2ui.tools import register_a2ui_tools
    from nous.api.tools import ToolDispatcher

    dispatcher = ToolDispatcher()
    service = _CapturingService()
    register_a2ui_tools(dispatcher, service, composer=_FallbackComposer())
    text, is_error = await dispatcher.dispatch(
        "compose_surface", {"intent": "x", "dedup_key": "app:new"}
    )
    assert not is_error, text
    assert json.loads(text)["fallback"] is True
    assert service.degraded == [True]
    # exactly one service call: no separate probe to race against
    assert service.calls == ["push_built"]


async def test_a_healthy_compose_is_never_marked_degraded(fake_composer) -> None:
    from nous.a2ui.tools import register_a2ui_tools
    from nous.api.tools import ToolDispatcher

    dispatcher = ToolDispatcher()
    service = _CapturingService()
    register_a2ui_tools(dispatcher, service, composer=fake_composer)

    text, is_error = await dispatcher.dispatch("compose_surface", {"intent": "x"})

    assert not is_error, text
    assert service.degraded == [False]


def test_overwrite_refusal_spares_only_apps_that_are_already_stubs() -> None:
    """The helper the service applies to the locked row."""
    from nous.a2ui.service import _overwrite_refusal

    class _Row:
        def __init__(self, components, spec):
            self.components = components
            self.app_spec = spec

    healthy = _Row(
        [{"c": i} for i in range(53)],
        {"archetype": "report", "update_hint": "call P.pub('trip')"},
    )
    msg = _overwrite_refusal(healthy, "app:italy-vacation")
    assert msg and "53 components" in msg and "call P.pub('trip')" in msg
    assert "'app:italy-vacation'" in msg

    # An existing degraded stub may be replaced: a retry can only improve it.
    stub = _Row([{"c": i} for i in range(5)], {"archetype": "fallback"})
    assert _overwrite_refusal(stub, "app:x") is None

    # A fallback ARCHETYPE carrying real content is still worth protecting.
    fat = _Row([{"c": i} for i in range(40)], {"archetype": "fallback"})
    assert _overwrite_refusal(fat, "app:x") is not None

    # No hint declared: still refused, just without the update path.
    bare = _Row([{"c": i} for i in range(20)], {"archetype": "report"})
    bare_msg = _overwrite_refusal(bare, "app:x")
    assert bare_msg and "update it in place" not in bare_msg


async def test_compose_surface_tool_defaults_the_dedup_key(fake_composer) -> None:
    from nous.a2ui.tools import register_a2ui_tools
    from nous.api.tools import ToolDispatcher

    dispatcher = ToolDispatcher()
    service = _CapturingService()
    register_a2ui_tools(dispatcher, service, composer=fake_composer)

    text, is_error = await dispatcher.dispatch(
        "compose_surface", {"intent": "Show me my Vacation Plans!"}
    )

    assert not is_error, text
    _, dedup_key = service.pushed[0]
    assert dedup_key.startswith("app:show-me-my-vacation-plans-")
    assert json.loads(text)["url"].startswith("/companion#/s/")


def test_intent_slug_is_collision_resistant() -> None:
    """Codex P2: 'C++ status' and 'C status' slug identically — without the
    digest, composing the second would replace the first live app."""
    from nous.a2ui.tools import _intent_slug

    assert _intent_slug("C++ status") != _intent_slug("C status")
    assert _intent_slug("x" * 100) != _intent_slug("x" * 100 + "y")
    assert _intent_slug("same intent") == _intent_slug("same intent")


async def test_compose_surface_tool_rejects_blocking_priority(fake_composer) -> None:
    from nous.a2ui.tools import register_a2ui_tools
    from nous.api.tools import ToolDispatcher

    dispatcher = ToolDispatcher()
    service = _CapturingService()
    register_a2ui_tools(dispatcher, service, composer=fake_composer)

    _, is_error = await dispatcher.dispatch(
        "compose_surface", {"intent": "x", "priority": 2}
    )

    assert is_error is True
    assert service.pushed == []


async def test_compose_surface_tool_is_absent_without_composer() -> None:
    from nous.a2ui.tools import register_a2ui_tools
    from nous.api.tools import ToolDispatcher

    dispatcher = ToolDispatcher()
    register_a2ui_tools(dispatcher, _CapturingService())  # composer None (flag off)

    text, is_error = await dispatcher.dispatch("compose_surface", {"intent": "x"})

    assert is_error is True
    assert "Unknown tool" in text


# ---------------------------------------------------------------------------
# Catalog property summary (compose prompt)
# ---------------------------------------------------------------------------


def test_catalog_summary_covers_every_allowed_component() -> None:
    from nous.a2ui.catalog_summary import catalog_property_summary
    from nous.a2ui.grammar import ALLOWED_COMPONENTS, BANNED_COMPONENTS

    summary = catalog_property_summary()

    for name in ALLOWED_COMPONENTS:
        assert f"- {name}: required " in summary, f"{name} missing from summary"
    for name in BANNED_COMPONENTS:
        assert f"- {name}:" not in summary, f"{name} is banned but summarized"


def test_catalog_summary_names_the_properties_that_broke_in_production() -> None:
    """The 2026-08-30 compose instrumentation: 4 of 6 repair rounds failed on
    props the model was never shown. ``DecisionCard.decisionId`` was reported
    verbatim; the "'items' is a required property" message came from the
    Timeline branch of the anyComponent oneOf (``List`` requires ``children``,
    not ``items`` — both are now stated)."""
    from nous.a2ui.catalog_summary import catalog_property_summary

    summary = catalog_property_summary()

    assert "- DecisionCard: required decisionId, description" in summary
    assert "- Timeline: required items" in summary
    assert "- List: required children" in summary
    # The three "unexpected property" rejections were subtitle/title on
    # components that have no such prop — the preamble says so explicitly.
    assert "DOES NOT EXIST" in summary
    assert "- AppHeader: required title, composedAt; optional subtitle" in summary


def test_catalog_summary_is_cached_and_within_budget(monkeypatch) -> None:
    from nous.a2ui import catalog_summary as summary_mod

    summary_mod.catalog_property_summary.cache_clear()
    first = summary_mod.catalog_property_summary()

    # Second call must not touch the 69 KB of catalog JSON again.
    def _boom() -> dict:
        raise AssertionError("catalog re-read on a cached call")

    monkeypatch.setattr(summary_mod, "_component_schemas", _boom)
    second = summary_mod.catalog_property_summary()

    assert second is first
    assert len(first) <= summary_mod._TOKEN_BUDGET * summary_mod._CHARS_PER_TOKEN


def test_build_prompt_carries_the_catalog_summary(composer: SurfaceComposer) -> None:
    from nous.a2ui.catalog_summary import catalog_property_summary

    prompt = composer._build_prompt("show me my vacation plans", None, {})

    summary = catalog_property_summary()
    assert summary in prompt
    # Ordered after the grammar rules (which now cross-reference it) and
    # before the source-data block.
    assert prompt.index("Hard rules") < prompt.index(summary)
    assert prompt.index(summary) < prompt.index("Server-resolved data")


# ---------------------------------------------------------------------------
# heartbeat_findings fingerprint validation (Sep 1 2026 field bug)
# ---------------------------------------------------------------------------


class _FakeFindingStore:
    """Mirrors the real store: ingest() derives the fingerprint from the
    finding's own text — the caller never chooses it."""

    def __init__(self, fingerprints: list[str]) -> None:
        self._fps = list(fingerprints)
        self.ingested: list[Any] = []

    def to_list(self) -> list[dict[str, Any]]:
        return [{"fingerprint": fp, "summary": "x"} for fp in self._fps]

    def ingest(self, finding: Any) -> str:
        self.ingested.append(finding)
        fp = finding.fingerprint()
        if fp not in self._fps:
            self._fps.append(fp)
        return "TRIAGE"


class _FakeHeartbeatRunner:
    def __init__(self, store: Any) -> None:
        self.finding_store = store


class _PushBuiltService:
    def __init__(self) -> None:
        self.pushed: list[Any] = []

    async def push_built(self, built: Any, **kwargs: Any) -> str:
        self.pushed.append(built)
        return "surface-123"


def _findings_dispatcher(store: Any):
    from nous.a2ui.tools import register_a2ui_tools
    from nous.api.tools import ToolDispatcher

    dispatcher = ToolDispatcher()
    service = _PushBuiltService()
    runner = _FakeHeartbeatRunner(store) if store is not None else None
    register_a2ui_tools(dispatcher, service, heartbeat_runner=runner)
    return dispatcher, service


async def _push_findings(dispatcher: Any, findings: list[dict[str, Any]]):
    """dispatch returns (content, is_error)."""
    return await dispatcher.dispatch(
        "push_surface",
        {"template": "heartbeat_findings", "params": {"findings": findings}},
    )


def _rendered_fingerprints(built: Any) -> set[str]:
    """Fingerprints the user can actually act on. actions.py gates on the
    data model AND dispatches on the button's context, so both must agree —
    assert on the intersection so a mismatch cannot pass."""
    offered = set((built.data_model or {}).get("findings", {}))
    on_buttons = {
        c["action"]["event"]["context"]["fingerprint"]
        for c in built.components
        if c.get("component") == "Button" and "fingerprint" in c.get("action", {}).get("event", {}).get("context", {})
    }
    assert offered == on_buttons, f"data model {offered} != buttons {on_buttons}"
    return offered


async def test_heartbeat_findings_registers_invented_fingerprint() -> None:
    """The Sep 1 2026 field bug: a card was pushed with the made-up
    fingerprint 'pr-568-open'. It rendered normally and every button
    dead-ended at "finding 'pr-568-open' not found" on the user's click.
    Rejecting the push killed the dead button but dropped the item; now the
    item is REGISTERED and rendered under its real derived fingerprint."""
    store = _FakeFindingStore(["f41f9f0af9e54af0"])
    dispatcher, service = _findings_dispatcher(store)

    content, is_error = await _push_findings(
        dispatcher, [{"fingerprint": "pr-568-open", "message": "PR #568 open"}]
    )

    assert not is_error, content
    assert service.pushed, "the item must still reach the user"
    assert len(store.ingested) == 1, "unknown item must be registered, not dropped"
    rendered = _rendered_fingerprints(service.pushed[0])
    assert "pr-568-open" not in rendered, "the invented slug must never be rendered"
    derived = store.ingested[0].fingerprint()
    assert rendered == {derived}
    assert derived in {f["fingerprint"] for f in store.to_list()}, "button must resolve"


async def test_heartbeat_findings_registers_item_with_no_fingerprint() -> None:
    """The caller should not have to supply an id at all."""
    store = _FakeFindingStore([])
    dispatcher, service = _findings_dispatcher(store)

    content, is_error = await _push_findings(dispatcher, [{"message": "disk 91% full"}])

    assert not is_error, content
    assert len(store.ingested) == 1
    assert _rendered_fingerprints(service.pushed[0]) == {store.ingested[0].fingerprint()}


async def test_heartbeat_findings_registration_is_idempotent() -> None:
    """A recurring card re-pushing the same text must not stack duplicates."""
    store = _FakeFindingStore([])
    dispatcher, _service = _findings_dispatcher(store)

    await _push_findings(dispatcher, [{"message": "disk 91% full"}])
    await _push_findings(dispatcher, [{"message": "disk 91% full"}])

    assert len({f.fingerprint() for f in store.ingested}) == 1
    assert len(store.to_list()) == 1


async def test_heartbeat_findings_maps_unsupported_urgency() -> None:
    """Finding.urgency is a 3-value Literal; the card showed 'medium'."""
    store = _FakeFindingStore([])
    dispatcher, _service = _findings_dispatcher(store)

    _content, is_error = await _push_findings(
        dispatcher, [{"message": "PR #568 open", "urgency": "medium"}]
    )

    assert not is_error
    assert store.ingested[0].urgency == "normal"


async def test_heartbeat_findings_keeps_agent_items_out_of_check_buckets() -> None:
    """check_name keys escalation and tuner reputation — agent-authored
    items must not pollute a real check's bucket. The name now includes a
    per-message digest suffix to prevent digit-variant collisions, so we
    assert the 'agent:<check>:' prefix rather than an exact match."""
    store = _FakeFindingStore([])
    dispatcher, _service = _findings_dispatcher(store)

    await _push_findings(dispatcher, [{"message": "PR #568 open", "check": "facts"}])

    assert store.ingested[0].check_name.startswith("agent:facts:")


async def test_heartbeat_findings_rejects_blank_message() -> None:
    """An item registered by its own text needs text."""
    store = _FakeFindingStore([])
    dispatcher, service = _findings_dispatcher(store)

    _content, is_error = await _push_findings(dispatcher, [{"fingerprint": "made-up"}])

    assert is_error
    assert not service.pushed


async def test_heartbeat_findings_accepts_real_fingerprint() -> None:
    """A genuine heartbeat finding must pass through untouched."""
    store = _FakeFindingStore(["f41f9f0af9e54af0"])
    dispatcher, service = _findings_dispatcher(store)

    content, is_error = await _push_findings(
        dispatcher, [{"fingerprint": "f41f9f0af9e54af0", "message": "PR #568 open"}]
    )

    assert not is_error, f"real fingerprint must pass, got {content}"
    assert service.pushed
    assert not store.ingested, "an existing finding must not be re-registered"
    assert _rendered_fingerprints(service.pushed[0]) == {"f41f9f0af9e54af0"}


async def test_heartbeat_findings_rejected_when_store_missing() -> None:
    """No finding store == every button fails; the card is dead on arrival."""
    dispatcher, service = _findings_dispatcher(None)

    _content, is_error = await _push_findings(
        dispatcher, [{"fingerprint": "f41f9f0af9e54af0", "message": "x"}]
    )

    assert is_error
    assert not service.pushed


# ---------------------------------------------------------------------------
# Item 2 regression: digit-variant fingerprint collision
# ---------------------------------------------------------------------------


async def test_heartbeat_findings_digit_variants_get_distinct_fingerprints() -> None:
    """Two findings whose text differs only in a digit must not share a
    fingerprint. Finding.fingerprint() normalises digit runs to 'N', so
    'PR #568 open' and 'PR #569 open' would collide under the old
    check_name — the second button silently resolved the first finding."""
    store = _FakeFindingStore([])
    dispatcher, service = _findings_dispatcher(store)

    _content, is_error = await _push_findings(
        dispatcher,
        [
            {"message": "PR #568 open", "check": "prs"},
            {"message": "PR #569 open", "check": "prs"},
        ],
    )

    assert not is_error
    fps = {f.fingerprint() for f in store.ingested}
    assert len(fps) == 2, "digit variants must produce distinct fingerprints"
    assert len(_rendered_fingerprints(service.pushed[0])) == 2, "both must render"


async def test_heartbeat_findings_same_message_is_idempotent_across_pushes() -> None:
    """The identical message re-pushed must yield the same fingerprint and
    not grow the store — the second push adopts the already-registered fp."""
    store = _FakeFindingStore([])
    dispatcher, _service = _findings_dispatcher(store)

    await _push_findings(dispatcher, [{"message": "PR #568 open"}])
    await _push_findings(dispatcher, [{"message": "PR #568 open"}])

    fps = {f.fingerprint() for f in store.ingested}
    assert len(fps) == 1, "same message must produce one tracked finding"
    assert len(store.to_list()) == 1


# ---------------------------------------------------------------------------
# Item 3 regression: findings must not be ingested when the push is blocked
# ---------------------------------------------------------------------------


async def test_heartbeat_findings_not_ingested_when_push_is_blocked() -> None:
    """If push_built raises (censor block or other failure), the finding
    store must be left unchanged — rejected prose must not appear in
    GET /heartbeat/findings."""
    store = _FakeFindingStore([])

    class _BlockingService:
        def __init__(self) -> None:
            self.pushed: list[Any] = []

        async def push_built(self, built: Any, **kwargs: Any) -> str:
            raise PermissionError("censor blocked")

    from nous.a2ui.tools import register_a2ui_tools
    from nous.api.tools import ToolDispatcher

    dispatcher = ToolDispatcher()
    service = _BlockingService()
    register_a2ui_tools(dispatcher, service, heartbeat_runner=_FakeHeartbeatRunner(store))

    _content, is_error = await dispatcher.dispatch(
        "push_surface",
        {"template": "heartbeat_findings", "params": {"findings": [{"message": "disk 91% full"}]}},
    )

    assert is_error
    assert not store.ingested, "finding must not enter the store when the push is blocked"
    assert not service.pushed
