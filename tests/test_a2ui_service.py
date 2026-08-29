"""F092: SurfaceService — lifecycle, outbox, dedup, sweep, replay.

Postgres-only for everything DB-backed. ``a2ui_surfaces.allowed_actions`` is
``ARRAY(Text)``, which SQLite round-trips as a JSON string that SQLAlchemy
hands back as a list of single CHARACTERS — an allowlist check against it
would pass or fail for reasons unrelated to the code under test. CI runs
``NOUS_TEST_DB=postgres``, so these execute in the real gate.

Isolation: every test mints a unique ``agent_id`` and deletes its own rows
afterwards. SurfaceService opens and COMMITS its own sessions, so conftest's
rollback-per-test ``session`` fixture cannot protect a shared dev database.

Lag window: ``_LAG_WINDOW_SECONDS`` makes freshly committed outbox rows
invisible to ``replay``/``latest_seq`` for 2 seconds — that is deliberate
(BIGSERIAL seq is allocated at INSERT but visible at COMMIT, so a bare
``seq > watermark`` scan can skip a late-committing row forever). Most tests
neutralize it; two pin the behavior itself.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text

from nous.a2ui import service as service_module
from nous.a2ui.builders import approval_gate, heartbeat_findings
from nous.a2ui.dsl import BuiltSurface
from nous.a2ui.service import SurfaceService, _pointer_set
from nous.storage.models import A2uiAction, A2uiOutbox, A2uiSurface

# ---------------------------------------------------------------------------
# _pointer_set — pure, runs on every backend
# ---------------------------------------------------------------------------


def test_pointer_set_writes_a_nested_value() -> None:
    model = {"a": {"b": 1}}

    _pointer_set(model, "/a/b", 2)

    assert model == {"a": {"b": 2}}


def test_pointer_set_creates_missing_objects() -> None:
    model: dict[str, Any] = {}

    _pointer_set(model, "/a/b/c", "deep")

    assert model == {"a": {"b": {"c": "deep"}}}


def test_pointer_set_creates_arrays_for_numeric_tokens() -> None:
    """A numeric next-token means the intermediate is a list, not an object.

    Mirrored by the client's pointer.ts; if the two disagree the server's
    authoritative model and the rendered one silently diverge.
    """
    model: dict[str, Any] = {}

    _pointer_set(model, "/items/1/name", "second")

    assert model == {"items": [None, {"name": "second"}]}


def test_pointer_set_null_deletes_an_object_key() -> None:
    model = {"a": 1, "b": 2}

    _pointer_set(model, "/b", None)

    assert model == {"a": 1}


def test_pointer_set_null_removes_an_array_element() -> None:
    model = {"items": ["x", "y", "z"]}

    _pointer_set(model, "/items/1", None)

    assert model == {"items": ["x", "z"]}


def test_pointer_set_deleting_a_missing_key_is_a_no_op() -> None:
    model = {"a": 1}

    _pointer_set(model, "/nope", None)

    assert model == {"a": 1}


def test_pointer_set_unescapes_tilde_one_before_tilde_zero() -> None:
    """RFC 6901 order: ~1 -> / first, then ~0 -> ~.

    Reversing them would turn "~01" into "/" instead of "~1".
    """
    model: dict[str, Any] = {}

    _pointer_set(model, "/a~1b", "slash")
    _pointer_set(model, "/a~01", "tilde-one")

    assert model == {"a/b": "slash", "a~1": "tilde-one"}


def test_pointer_set_extends_a_short_array() -> None:
    model = {"items": ["x"]}

    _pointer_set(model, "/items/2", "z")

    assert model == {"items": ["x", None, "z"]}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

APPROVAL_PARAMS = {
    "title": "Restart the eval container?",
    "summary": "It has been unresponsive for 20 minutes.",
    "risk": "In-flight eval run would be lost.",
    "options": [{"id": "restart", "label": "Restart"}, {"id": "wait", "label": "Wait"}],
}


@pytest.fixture
def a2ui_agent_id() -> str:
    """A unique agent per test — the dev database is shared and committed to."""
    return f"test-a2ui-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def a2ui_settings(settings, a2ui_agent_id: str):
    """Real Settings with the agent scoped and Telegram notification disabled."""
    return settings.model_copy(
        update={
            "agent_id": a2ui_agent_id,
            "telegram_bot_token": None,
            "telegram_chat_id": None,
        }
    )


@pytest.fixture
def no_lag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make committed outbox rows immediately visible to replay/latest_seq."""
    monkeypatch.setattr(service_module, "_LAG_WINDOW_SECONDS", 0)


@pytest_asyncio.fixture
async def service(db, a2ui_settings, a2ui_agent_id: str):
    svc = SurfaceService(db, a2ui_settings)
    yield svc
    async with db.session() as session:
        # a2ui_actions has no FK to surfaces, so it does not cascade.
        await session.execute(delete(A2uiAction).where(A2uiAction.agent_id == a2ui_agent_id))
        await session.execute(delete(A2uiSurface).where(A2uiSurface.agent_id == a2ui_agent_id))
        await session.commit()


async def _surfaces(db, agent_id: str) -> list[A2uiSurface]:
    async with db.session() as session:
        result = await session.execute(
            select(A2uiSurface)
            .where(A2uiSurface.agent_id == agent_id)
            .order_by(A2uiSurface.created_at)
        )
        return list(result.scalars().all())


async def _outbox(db, agent_id: str) -> list[A2uiOutbox]:
    async with db.session() as session:
        result = await session.execute(
            select(A2uiOutbox).where(A2uiOutbox.agent_id == agent_id).order_by(A2uiOutbox.seq)
        )
        return list(result.scalars().all())


async def _actions(db, agent_id: str) -> list[A2uiAction]:
    async with db.session() as session:
        result = await session.execute(
            select(A2uiAction)
            .where(A2uiAction.agent_id == agent_id)
            .order_by(A2uiAction.created_at)
        )
        return list(result.scalars().all())


async def _backdate_outbox(db, agent_id: str, *, seconds: float = 0, hours: float = 0) -> None:
    """Age this agent's outbox rows so lag-window/retention logic can see them."""
    async with db.session() as session:
        await session.execute(
            text(
                "UPDATE nous_system.a2ui_outbox SET created_at = created_at - "
                "make_interval(secs => :secs) WHERE agent_id = :agent"
            ),
            {"secs": seconds + hours * 3600, "agent": agent_id},
        )
        await session.commit()


def _envelope_kind(envelope: dict) -> str:
    for key in ("createSurface", "updateComponents", "updateDataModel", "deleteSurface"):
        if key in envelope:
            return key
    raise AssertionError(f"unrecognized envelope: {envelope}")


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
class TestPush:
    async def test_push_creates_the_surface_and_one_outbox_row(
        self, service: SurfaceService, db, a2ui_agent_id: str
    ) -> None:
        built = approval_gate(APPROVAL_PARAMS)

        surface_id = await service.push_built(built)

        surfaces = await _surfaces(db, a2ui_agent_id)
        assert len(surfaces) == 1
        row = surfaces[0]
        assert row.surface_id == surface_id
        assert row.status == "live"
        assert row.kind == "approval_gate"
        assert row.origin == "escalation"
        assert row.priority == 2
        assert row.title == APPROVAL_PARAMS["title"]
        assert row.allowed_actions == ["approval.choose", "approval.defer"]
        assert row.nonce, "every surface must carry a nonce"
        assert row.components == built.components
        assert row.data_model == built.data_model

        outbox = await _outbox(db, a2ui_agent_id)
        assert len(outbox) == 1
        assert _envelope_kind(outbox[0].envelope) == "createSurface"

    async def test_create_envelope_carries_nonce_and_priority_extensions(
        self, service: SurfaceService, db, a2ui_agent_id: str
    ) -> None:
        """The nonce reaches the client on the stream — it is the action key.

        It is omitted from the LIST endpoint only; without it here the
        renderer could never POST an accepted action.
        """
        await service.push_built(approval_gate(APPROVAL_PARAMS))

        envelope = (await _outbox(db, a2ui_agent_id))[0].envelope
        create = envelope["createSurface"]
        extensions = create["metadata"]["extensions"]
        surface = (await _surfaces(db, a2ui_agent_id))[0]

        assert envelope["version"] == "v1.0"
        assert extensions["com_nous_nonce"] == surface.nonce
        assert extensions["com_nous_priority"] == 2
        assert create["sendDataModel"] is True
        assert create["components"] == surface.components

    async def test_push_sets_expiry_from_the_builder(
        self, service: SurfaceService, db, a2ui_agent_id: str
    ) -> None:
        await service.push_built(approval_gate(APPROVAL_PARAMS))

        surface = (await _surfaces(db, a2ui_agent_id))[0]

        assert surface.expires_at is not None
        assert surface.expires_at > datetime.now(UTC)

    async def test_dedup_key_updates_the_surface_in_place(
        self, service: SurfaceService, db, a2ui_agent_id: str
    ) -> None:
        """A recurring producer updates its card instead of stacking cards.

        This is the whole reason dedup_key exists: an hourly heartbeat check
        would otherwise leave 24 findings surfaces in the feed per day.
        """
        first_id = await service.push_built(
            heartbeat_findings({"findings": [{"fingerprint": "aaa111", "message": "one"}]}),
            dedup_key="heartbeat:findings",
        )

        second_id = await service.push_built(
            heartbeat_findings(
                {
                    "findings": [
                        {"fingerprint": "aaa111", "message": "one"},
                        {"fingerprint": "bbb222", "message": "two"},
                    ]
                }
            ),
            dedup_key="heartbeat:findings",
        )

        assert second_id == first_id
        surfaces = await _surfaces(db, a2ui_agent_id)
        assert len(surfaces) == 1, "dedup must not create a second surface"
        assert surfaces[0].title == "Heartbeat findings (2)"
        assert set(surfaces[0].data_model["findings"]) == {"aaa111", "bbb222"}

        kinds = [_envelope_kind(row.envelope) for row in await _outbox(db, a2ui_agent_id)]
        assert kinds == ["createSurface", "updateComponents", "updateDataModel"]

    async def test_different_dedup_keys_create_separate_surfaces(
        self, service: SurfaceService, db, a2ui_agent_id: str
    ) -> None:
        await service.push_built(approval_gate(APPROVAL_PARAMS), dedup_key="one")
        await service.push_built(approval_gate(APPROVAL_PARAMS), dedup_key="two")

        assert len(await _surfaces(db, a2ui_agent_id)) == 2

    async def test_dedup_does_not_match_a_resolved_surface(
        self, service: SurfaceService, db, a2ui_agent_id: str
    ) -> None:
        """Only LIVE surfaces dedup; a resolved card is gone, not reusable."""
        first_id = await service.push_built(approval_gate(APPROVAL_PARAMS), dedup_key="k")
        await service.resolve(first_id)

        second_id = await service.push_built(approval_gate(APPROVAL_PARAMS), dedup_key="k")

        assert second_id != first_id
        assert len(await _surfaces(db, a2ui_agent_id)) == 2

    async def test_push_blocked_by_a_censor_raises_and_writes_nothing(
        self, db, a2ui_settings, a2ui_agent_id: str
    ) -> None:
        """An aborting censor stops the push before any row is written."""

        class _AbortingHeart:
            async def check_censors(self, text: str) -> list[Any]:
                from types import SimpleNamespace

                return [
                    SimpleNamespace(
                        action="abort", reason="no restarts", trigger_pattern="restart"
                    )
                ]

        svc = SurfaceService(db, a2ui_settings, heart=_AbortingHeart())

        with pytest.raises(PermissionError, match="no restarts"):
            await svc.push_built(approval_gate(APPROVAL_PARAMS))

        assert await _surfaces(db, a2ui_agent_id) == []

    async def test_push_broadcasts_to_live_subscribers(
        self, service: SurfaceService
    ) -> None:
        queue = service.subscribe()

        surface_id = await service.push_built(approval_gate(APPROVAL_PARAMS))

        seq, envelope = queue.get_nowait()
        assert seq > 0
        assert envelope["createSurface"]["surfaceId"] == surface_id
        service.unsubscribe(queue)


# ---------------------------------------------------------------------------
# update_data / resolve
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
class TestMutations:
    async def test_update_data_patches_the_row_and_appends_an_envelope(
        self, service: SurfaceService, db, a2ui_agent_id: str
    ) -> None:
        """Authoritative state AND the delta log — a client that hydrates from

        the snapshot and one that follows the stream must end up identical.
        """
        surface_id = await service.push_built(approval_gate(APPROVAL_PARAMS))

        await service.update_data(surface_id, "/summary", "Now unresponsive for 40 minutes.")

        surface = (await _surfaces(db, a2ui_agent_id))[0]
        assert surface.data_model["summary"] == "Now unresponsive for 40 minutes."
        assert surface.data_model["risk"] == APPROVAL_PARAMS["risk"], "untouched keys survive"

        envelope = (await _outbox(db, a2ui_agent_id))[-1].envelope
        assert envelope["updateDataModel"] == {
            "surfaceId": surface_id,
            "value": "Now unresponsive for 40 minutes.",
            "path": "/summary",
        }

    async def test_nested_patches_reach_the_authoritative_row(
        self, service: SurfaceService, db, a2ui_agent_id: str
    ) -> None:
        """REGRESSION: a nested pointer write must persist, not just broadcast.

        update_data copied the model with dict(), which shares nested objects
        with SQLAlchemy's committed-state snapshot; an in-place _pointer_set
        mutated both, the flush compared new == old and emitted no UPDATE.
        The outbox envelope still shipped, so a CONNECTED client showed the
        new value while the authoritative row kept the old one — the next
        reload silently reverted it. Every heartbeat.* action patches a
        nested path, so every triage verb was lost on reload; top-level
        paths worked, which is what hid it.

        Asserted through snapshot() as well as the row: hydration is the
        path that was actually broken for the user.
        """
        surface_id = await service.push_built(
            heartbeat_findings(
                {
                    "findings": [
                        {"fingerprint": "fp-one", "message": "one"},
                        {"fingerprint": "fp-two", "message": "two"},
                    ]
                }
            )
        )

        await service.update_data(surface_id, "/findings/fp-one", "resolve")

        surface = (await _surfaces(db, a2ui_agent_id))[0]
        assert surface.data_model["findings"] == {"fp-one": "resolve", "fp-two": "open"}

        snapshot = await service.snapshot(surface_id)
        assert snapshot is not None
        assert snapshot["createSurface"]["dataModel"]["findings"]["fp-one"] == "resolve"

    async def test_update_data_without_a_path_replaces_the_whole_model(
        self, service: SurfaceService, db, a2ui_agent_id: str
    ) -> None:
        surface_id = await service.push_built(approval_gate(APPROVAL_PARAMS))

        await service.update_data(surface_id, None, {"summary": "fresh"})

        surface = (await _surfaces(db, a2ui_agent_id))[0]
        assert surface.data_model == {"summary": "fresh"}

        envelope = (await _outbox(db, a2ui_agent_id))[-1].envelope
        assert "path" not in envelope["updateDataModel"]

    async def test_whole_model_replace_requires_an_object(
        self, service: SurfaceService
    ) -> None:
        surface_id = await service.push_built(approval_gate(APPROVAL_PARAMS))

        with pytest.raises(ValueError, match="whole-model replace"):
            await service.update_data(surface_id, "/", "not an object")

    async def test_update_data_on_a_resolved_surface_raises(
        self, service: SurfaceService
    ) -> None:
        surface_id = await service.push_built(approval_gate(APPROVAL_PARAMS))
        await service.resolve(surface_id)

        with pytest.raises(KeyError):
            await service.update_data(surface_id, "/summary", "too late")

    async def test_resolve_marks_the_surface_and_emits_the_teardown(
        self, service: SurfaceService, db, a2ui_agent_id: str
    ) -> None:
        surface_id = await service.push_built(approval_gate(APPROVAL_PARAMS))

        await service.resolve(surface_id)

        surface = (await _surfaces(db, a2ui_agent_id))[0]
        assert surface.status == "resolved"
        assert surface.resolved_at is not None

        last = (await _outbox(db, a2ui_agent_id))[-1].envelope
        assert last["deleteSurface"] == {"surfaceId": surface_id}

    async def test_resolve_is_idempotent(
        self, service: SurfaceService, db, a2ui_agent_id: str
    ) -> None:
        """A second resolve must not emit a second teardown.

        Action handlers and the expiry sweep can both reach a surface; a
        duplicate deleteSurface would be harmless on the client but would
        make the outbox lie about what happened.
        """
        surface_id = await service.push_built(approval_gate(APPROVAL_PARAMS))
        await service.resolve(surface_id)
        before = len(await _outbox(db, a2ui_agent_id))

        await service.resolve(surface_id)

        assert len(await _outbox(db, a2ui_agent_id)) == before

    async def test_resolve_unknown_surface_raises(self, service: SurfaceService) -> None:
        with pytest.raises(KeyError):
            await service.resolve("nous:nope:nope:000000")


# ---------------------------------------------------------------------------
# expire_sweep
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
class TestExpirySweep:
    async def test_sweep_records_no_objection_before_expiring(
        self, service: SurfaceService, db, a2ui_agent_id: str
    ) -> None:
        """Spec 6.2 "silence counts": the evidence is written BEFORE the flip.

        If the order were reversed, a crash between the two writes would
        expire an escalation with no record that nobody objected — the
        surface would be gone and the reason it was gone unrecoverable.
        """
        surface_id = await service.push_built(approval_gate(APPROVAL_PARAMS))
        async with db.session() as session:
            await session.execute(
                text(
                    "UPDATE nous_system.a2ui_surfaces SET expires_at = now() - "
                    "interval '1 hour' WHERE surface_id = :sid"
                ),
                {"sid": surface_id},
            )
            await session.commit()

        expired = await service.expire_sweep()

        assert expired == 1
        surface = (await _surfaces(db, a2ui_agent_id))[0]
        assert surface.status == "expired"

        actions = await _actions(db, a2ui_agent_id)
        assert len(actions) == 1
        assert actions[0].action_name == "no_objection"
        assert actions[0].status == "completed"
        assert actions[0].actor == "system:expiry", "a sweep is not a human consenting"
        assert actions[0].completed_at <= surface.resolved_at

        assert _envelope_kind((await _outbox(db, a2ui_agent_id))[-1].envelope) == "deleteSurface"

    async def test_sweep_leaves_surfaces_that_are_not_due(
        self, service: SurfaceService, db, a2ui_agent_id: str
    ) -> None:
        await service.push_built(approval_gate(APPROVAL_PARAMS))

        assert await service.expire_sweep() == 0
        assert (await _surfaces(db, a2ui_agent_id))[0].status == "live"
        assert await _actions(db, a2ui_agent_id) == []

    async def test_sweep_prunes_undeliverable_outbox_rows(
        self, service: SurfaceService, db, a2ui_agent_id: str
    ) -> None:
        """Outbox rows for non-live surfaces are undeliverable, so they go.

        Reconnect is hydration-first and replay only covers live surfaces,
        so nothing can ever read these again.
        """
        surface_id = await service.push_built(approval_gate(APPROVAL_PARAMS))
        await service.resolve(surface_id)
        assert len(await _outbox(db, a2ui_agent_id)) == 2

        await _backdate_outbox(db, a2ui_agent_id, hours=48)
        await service.expire_sweep()

        assert await _outbox(db, a2ui_agent_id) == []

    async def test_sweep_keeps_outbox_rows_for_live_surfaces(
        self, service: SurfaceService, db, a2ui_agent_id: str
    ) -> None:
        """Age alone is not a reason to prune — a live surface can be old."""
        await service.push_built(approval_gate(APPROVAL_PARAMS))
        await _backdate_outbox(db, a2ui_agent_id, hours=48)

        await service.expire_sweep()

        assert len(await _outbox(db, a2ui_agent_id)) == 1


# ---------------------------------------------------------------------------
# replay / latest_seq — the lag window
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
class TestReplay:
    async def test_fresh_rows_are_invisible_to_replay_but_counted_by_latest_seq(
        self, service: SurfaceService
    ) -> None:
        """A just-committed row is deliberately NOT replayed yet — but

        ``latest_seq`` DOES count it (codex P2): hydration snapshots read
        current committed state, so the watermark handed to a fresh client
        must cover everything the snapshots already contain, or the young
        row gets REPLAYED once it ages and a redelivered createSurface
        clobbers input typed in that window. The lag window guards only
        replay's reads (BIGSERIAL commit-visibility skew).
        """
        await service.push_built(approval_gate(APPROVAL_PARAMS))

        assert await service.replay(0) == []
        assert await service.latest_seq() > 0

    async def test_rows_replay_once_past_the_lag_window(
        self, service: SurfaceService, db, a2ui_agent_id: str
    ) -> None:
        surface_id = await service.push_built(approval_gate(APPROVAL_PARAMS))
        await _backdate_outbox(db, a2ui_agent_id, seconds=5)

        replayed = await service.replay(0)

        assert replayed is not None
        assert len(replayed) == 1
        seq, envelope = replayed[0]
        assert seq == await service.latest_seq()
        assert envelope["createSurface"]["surfaceId"] == surface_id

    async def test_replay_excludes_rows_at_or_below_since(
        self, service: SurfaceService, db, a2ui_agent_id: str, no_lag: None
    ) -> None:
        first = await service.push_built(approval_gate(APPROVAL_PARAMS))
        boundary = await service.latest_seq()
        second = await service.push_built(approval_gate(APPROVAL_PARAMS))

        replayed = await service.replay(boundary)

        assert replayed is not None
        surface_ids = [env["createSurface"]["surfaceId"] for _, env in replayed]
        assert surface_ids == [second]
        assert first not in surface_ids

    async def test_replay_skips_non_live_content_but_replays_teardowns(
        self, service: SurfaceService, db, a2ui_agent_id: str, no_lag: None
    ) -> None:
        """Content deltas replay live-only, but deleteSurface replays

        REGARDLESS of status (codex P2): a surface can resolve between the
        client's snapshot fetch and its SSE subscribe, and the teardown row
        is then the only event that removes the stale card. Hydration-first
        covers reconnects; this covers the subscribe race. Applying a
        teardown for an unknown surface is a client-side no-op.
        """
        resolved_id = await service.push_built(approval_gate(APPROVAL_PARAMS))
        await service.resolve(resolved_id)
        live_id = await service.push_built(approval_gate(APPROVAL_PARAMS))

        replayed = await service.replay(0)

        assert replayed is not None
        creates = [
            env["createSurface"]["surfaceId"] for _, env in replayed if "createSurface" in env
        ]
        deletes = [
            env["deleteSurface"]["surfaceId"] for _, env in replayed if "deleteSurface" in env
        ]
        assert creates == [live_id]
        assert deletes == [resolved_id]

    async def test_replay_returns_none_when_the_gap_exceeds_the_window(
        self, db, settings, a2ui_agent_id: str, no_lag: None
    ) -> None:
        """Too far behind to catch up: the caller must tell the client to

        resync rather than stream a partial history it would render as truth.
        """
        narrow = settings.model_copy(
            update={
                "agent_id": a2ui_agent_id,
                "telegram_bot_token": None,
                "telegram_chat_id": None,
                "a2ui_outbox_replay_window": 1,
            }
        )
        svc = SurfaceService(db, narrow)
        try:
            await svc.push_built(approval_gate(APPROVAL_PARAMS), dedup_key="k")
            await svc.push_built(approval_gate(APPROVAL_PARAMS), dedup_key="k")

            assert await svc.replay(0) is None
        finally:
            async with db.session() as session:
                await session.execute(
                    delete(A2uiAction).where(A2uiAction.agent_id == a2ui_agent_id)
                )
                await session.execute(
                    delete(A2uiSurface).where(A2uiSurface.agent_id == a2ui_agent_id)
                )
                await session.commit()


# ---------------------------------------------------------------------------
# reads: live_index / snapshot
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
class TestReads:
    async def test_live_index_never_leaks_the_nonce(
        self, service: SurfaceService, no_lag: None
    ) -> None:
        """The nonce is the action credential; the list endpoint is the one

        place it is withheld (the stream and the snapshot must carry it).
        """
        await service.push_built(approval_gate(APPROVAL_PARAMS))

        index = await service.live_index()

        assert len(index["surfaces"]) == 1
        entry = index["surfaces"][0]
        assert "nonce" not in entry
        assert set(entry) == {
            "surface_id",
            "kind",
            "origin",
            "title",
            "priority",
            "created_at",
            "updated_at",
        }
        assert index["latest_seq"] > 0

    async def test_live_index_excludes_non_live_surfaces(
        self, service: SurfaceService
    ) -> None:
        resolved_id = await service.push_built(approval_gate(APPROVAL_PARAMS))
        await service.resolve(resolved_id)
        live_id = await service.push_built(approval_gate(APPROVAL_PARAMS))

        index = await service.live_index()

        assert [s["surface_id"] for s in index["surfaces"]] == [live_id]

    async def test_live_index_orders_by_priority_then_recency(
        self, service: SurfaceService
    ) -> None:
        low_id = await service.push_built(
            heartbeat_findings({"findings": [], "priority": 0})
        )
        high_id = await service.push_built(approval_gate(APPROVAL_PARAMS))

        index = await service.live_index()

        assert [s["surface_id"] for s in index["surfaces"]] == [high_id, low_id]

    async def test_snapshot_returns_a_create_envelope_with_the_nonce(
        self, service: SurfaceService, db, a2ui_agent_id: str
    ) -> None:
        surface_id = await service.push_built(approval_gate(APPROVAL_PARAMS))
        surface = (await _surfaces(db, a2ui_agent_id))[0]

        snapshot = await service.snapshot(surface_id)

        assert snapshot is not None
        create = snapshot["createSurface"]
        assert create["surfaceId"] == surface_id
        assert create["metadata"]["extensions"]["com_nous_nonce"] == surface.nonce
        assert create["components"] == surface.components

    async def test_snapshot_is_none_for_a_resolved_surface(
        self, service: SurfaceService
    ) -> None:
        """Hydration must not resurrect a surface the user already dismissed."""
        surface_id = await service.push_built(approval_gate(APPROVAL_PARAMS))
        await service.resolve(surface_id)

        assert await service.snapshot(surface_id) is None

    async def test_snapshot_is_none_for_an_unknown_surface(
        self, service: SurfaceService
    ) -> None:
        assert await service.snapshot("nous:nope:nope:000000") is None


# ---------------------------------------------------------------------------
# broadcast hub
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
class TestBroadcastHub:
    async def test_a_full_subscriber_is_dropped_and_told_to_resync(
        self, service: SurfaceService
    ) -> None:
        """Drop-on-full by design (F087 precedent): a slow consumer must never

        backpressure a producer. The sentinel is how its stream learns to
        re-hydrate instead of silently missing envelopes.
        """
        queue = service.subscribe()
        while not queue.full():
            queue.put_nowait((0, {"filler": True}))

        service._broadcast(99, {"deleteSurface": {"surfaceId": "x"}})

        assert queue not in service._subscribers
        assert queue.full()

    async def test_unsubscribe_stops_delivery(self, service: SurfaceService) -> None:
        queue = service.subscribe()
        service.unsubscribe(queue)

        await service.push_built(approval_gate(APPROVAL_PARAMS))

        assert queue.empty()


# ---------------------------------------------------------------------------
# BuiltSurface contract
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_push_validates_before_writing(service: SurfaceService, db, a2ui_agent_id: str) -> None:
    """An invalid surface never reaches the database.

    push_built validates first, so a hand-rolled BuiltSurface (the LLM
    compose path in a later phase) cannot persist something no renderer
    could draw.
    """
    from nous.a2ui.dsl import SurfaceValidationError

    broken = BuiltSurface(
        kind="broken",
        origin="test",
        title="Broken",
        components=[{"id": "root", "component": "NotAComponent"}],
    )

    with pytest.raises(SurfaceValidationError):
        await service.push_built(broken)

    assert await _surfaces(db, a2ui_agent_id) == []


# ---------------------------------------------------------------------------
# Codex round-2/3 regressions (postgres_only: DB-backed like the classes above)
# ---------------------------------------------------------------------------

FINDINGS_LOW = {
    "findings": [{"fingerprint": "fp-restart-1", "message": "Disk at 91%.", "urgency": "high"}],
    "priority": 1,
}


@pytest.mark.postgres_only
async def test_snapshot_is_agent_scoped(
    service: SurfaceService, db, a2ui_settings, a2ui_agent_id: str
) -> None:
    """Another agent's surface id must not disclose its snapshot — the

    envelope carries the nonce, so a cross-agent read would also hand over
    action authorization (codex P1).
    """
    surface_id = await service.push_built(approval_gate(APPROVAL_PARAMS))

    stranger = SurfaceService(
        db, a2ui_settings.model_copy(update={"agent_id": f"other-{a2ui_agent_id}"})
    )
    assert await stranger.snapshot(surface_id) is None
    assert await service.snapshot(surface_id) is not None


@pytest.mark.postgres_only
async def test_expire_sweep_claims_atomically_never_after_an_action(
    service: SurfaceService, db, a2ui_agent_id: str
) -> None:
    """A surface resolved by a user action before the sweep runs must NOT

    get a no_objection row: the sweep's UPDATE-claim only flips still-live
    rows, and evidence is written in the same transaction (codex P1).
    """
    from datetime import timedelta as _td

    built = approval_gate({**APPROVAL_PARAMS, "expires_hours": 0.000001})
    built.expires_in = _td(seconds=-5)
    surface_id = await service.push_built(built)
    await service.resolve(surface_id)

    expired = await service.expire_sweep()

    assert expired == 0
    async with db.session() as session:
        rows = (
            await session.execute(
                select(A2uiAction).where(
                    A2uiAction.agent_id == a2ui_agent_id,
                    A2uiAction.action_name == "no_objection",
                )
            )
        ).scalars().all()
    assert rows == []


@pytest.mark.postgres_only
async def test_concurrent_same_dedup_key_pushes_yield_one_surface(
    service: SurfaceService, db, a2ui_agent_id: str
) -> None:
    """The partial UNIQUE index serializes racing producers; the loser

    retries down the update path instead of stacking a second live card
    (codex P2).
    """
    import asyncio as _asyncio

    results = await _asyncio.gather(
        service.push_built(approval_gate(APPROVAL_PARAMS), dedup_key="race:key"),
        service.push_built(approval_gate(APPROVAL_PARAMS), dedup_key="race:key"),
    )

    live = [s for s in await _surfaces(db, a2ui_agent_id) if s.status == "live"]
    assert len(live) == 1
    assert set(results) == {live[0].surface_id}


@pytest.mark.postgres_only
async def test_dedup_priority_change_reissues_create_surface(
    service: SurfaceService, db, a2ui_agent_id: str, no_lag: None
) -> None:
    """Priority lives in createSurface metadata and the client reads it only

    there — a dedup update that changes priority must re-deliver it via
    deleteSurface + createSurface, keeping the nonce stable (codex P2).
    """
    await service.push_built(heartbeat_findings(FINDINGS_LOW), dedup_key="hb:demo")
    first_nonce = (await _surfaces(db, a2ui_agent_id))[0].nonce

    await service.push_built(
        heartbeat_findings({**FINDINGS_LOW, "priority": 2}), dedup_key="hb:demo"
    )

    kinds = [_envelope_kind(row.envelope) for row in await _outbox(db, a2ui_agent_id)]
    assert kinds == ["createSurface", "deleteSurface", "createSurface"]
    last_create = (await _outbox(db, a2ui_agent_id))[-1].envelope["createSurface"]
    ext = last_create["metadata"]["extensions"]
    assert ext["com_nous_priority"] == 2
    assert ext["com_nous_nonce"] == first_nonce


@pytest.mark.postgres_only
async def test_startup_invalidation_expires_only_heartbeat_surfaces(
    service: SurfaceService, db, a2ui_agent_id: str
) -> None:
    """After a restart the in-memory finding store is empty, so every live

    heartbeat surface is provably dead — expired with an `invalidated`
    audit row (NOT no_objection: process loss is not user silence). Other
    origins stay live (codex P2).
    """
    hb_id = await service.push_built(heartbeat_findings(FINDINGS_LOW))
    approval_id = await service.push_built(approval_gate(APPROVAL_PARAMS))

    stale = await service.invalidate_heartbeat_surfaces()

    assert stale == 1
    by_id = {s.surface_id: s.status for s in await _surfaces(db, a2ui_agent_id)}
    assert by_id[hb_id] == "expired"
    assert by_id[approval_id] == "live"
    async with db.session() as session:
        audit = (
            await session.execute(
                select(A2uiAction).where(
                    A2uiAction.agent_id == a2ui_agent_id,
                    A2uiAction.surface_id == hb_id,
                )
            )
        ).scalars().all()
    assert [a.action_name for a in audit] == ["invalidated"]
    assert audit[0].actor == "system:restart"


@pytest.mark.postgres_only
async def test_overflowed_subscriber_still_receives_the_resync_sentinel(
    service: SurfaceService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full queue must make ROOM for the sentinel (codex P2): put_nowait on

    a full queue raises, so without evicting one item the dropped stream
    would drain its stale buffer and then wait forever, silently missing
    every later update instead of resyncing.
    """
    monkeypatch.setattr(service_module, "_SUBSCRIBER_QUEUE_SIZE", 1)
    queue = service.subscribe()
    service._broadcast(1, {"a": 1})
    service._broadcast(2, {"b": 2})

    assert queue not in service._subscribers
    drained = []
    while not queue.empty():
        drained.append(queue.get_nowait())
    assert drained[-1] is None
