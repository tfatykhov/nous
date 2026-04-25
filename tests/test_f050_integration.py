"""F050 — Integration tests: ``Heart.recall`` + sub-manager ``variant_pairs`` flow.

Covers the wire-in points from plan v2 §"Files":
- ``Heart._recall`` constructs ``variant_pairs`` from QueryExpander + embeddings
- Sub-managers' ``search(..., variant_pairs=...)`` route to ``hybrid_search_multi``
- ``variant_pairs=None`` path is byte-identical to today's single-query path

These tests will fail with ImportError or AttributeError until the
Integration agent lands the wire-in. Until then, individual tests skip
cleanly when the wire-in attribute (``Heart.set_query_expander``,
``FactManager._search`` ``variant_pairs`` kwarg, ``hybrid_search_multi``)
is missing.
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

# ---------------------------------------------------------------------------
# Defensive imports — Integration agent's edits may not be landed
# ---------------------------------------------------------------------------

try:
    from nous.heart.heart import Heart
    from nous.heart.facts import FactManager
except ImportError:
    pytest.skip(
        "Heart / FactManager import failed — integration agent in flight",
        allow_module_level=True,
    )


def _has_variant_pairs_kwarg(method: Any) -> bool:
    """True if the method accepts ``variant_pairs`` (plan v2 wire-in)."""
    try:
        sig = inspect.signature(method)
        return "variant_pairs" in sig.parameters
    except (TypeError, ValueError):
        return False


def _heart_has_set_query_expander() -> bool:
    return hasattr(Heart, "set_query_expander")


# ---------------------------------------------------------------------------
# Heart._recall — flag off (no expander) is unchanged path
# ---------------------------------------------------------------------------


def _stub_settings(**overrides: Any) -> MagicMock:
    """Settings-like stub. Avoids ``pydantic-settings`` env-var bleed from
    a developer's local ``.env`` (e.g. extra ``gdrive_*`` keys) which would
    otherwise raise ``extra_forbidden`` on ``Settings()``."""
    s = MagicMock()
    s.query_expansion_enabled = False
    s.agent_id = "nous-test"
    s.mmr_enabled = False
    s.cross_encoder_enabled = False
    s.cross_encoder_max_candidates = 30
    s.cross_encoder_model = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    s.cross_encoder_text_limit = 512
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


class TestHeartRecallNoExpander:
    """When ``query_expansion_enabled=False`` (default), Heart._recall MUST
    take the exact code path it takes today — no QueryExpander attached,
    sub-managers called WITHOUT variant_pairs."""

    @pytest.mark.asyncio
    async def test_heart_recall_no_expander_unchanged_path(self) -> None:
        """Flag off → sub-managers called without variant_pairs (or with =None).

        We call _recall directly, mock all sub-managers, and verify each
        ``.search()`` invocation either omits variant_pairs or passes None.
        """
        # Build a Heart with mocked managers; flag stays off.
        settings = _stub_settings(query_expansion_enabled=False)

        heart = Heart.__new__(Heart)
        heart.settings = settings
        heart.agent_id = "nous-test"
        heart._embeddings = None
        heart._owns_embeddings = False
        heart._bus = None
        # Plan v2: Heart constructor MUST initialize _query_expander = None
        # so flag-off + no-wiring works for tests.
        heart._query_expander = None  # type: ignore[attr-defined]

        # Mock sub-managers.
        heart.episodes = MagicMock()
        heart.episodes.search = AsyncMock(return_value=[])
        heart.facts = MagicMock()
        heart.facts.search = AsyncMock(return_value=[])
        heart.procedures = MagicMock()
        heart.procedures.search = AsyncMock(return_value=[])
        heart.censors = MagicMock()
        heart.censors.search = AsyncMock(return_value=[])

        session = AsyncMock()
        # Call private _recall directly to bypass session-context plumbing.
        await heart._recall("three word query", limit=10, types=None, session=session)

        # facts.search must NOT receive a non-None variant_pairs.
        for mock in (heart.facts.search, heart.episodes.search, heart.procedures.search):
            for call in mock.call_args_list:
                vp = call.kwargs.get("variant_pairs")
                assert vp is None, (
                    f"Flag-off path passed variant_pairs={vp!r} — "
                    "regression to today's behavior broken"
                )


# ---------------------------------------------------------------------------
# Heart._recall — flag on routes through expander
# ---------------------------------------------------------------------------


class TestHeartRecallWithExpander:
    @pytest.mark.asyncio
    async def test_heart_recall_with_expander_routes_through_hybrid_search_multi(
        self,
    ) -> None:
        """Flag on + expander wired → sub-managers called with variant_pairs."""
        if not _heart_has_set_query_expander():
            pytest.skip(
                "Heart.set_query_expander not yet landed — Integration agent in flight"
            )

        settings = _stub_settings(query_expansion_enabled=True)

        # Mock embedding provider returns a deterministic vector per text.
        embeddings = MagicMock()
        embeddings.embed = AsyncMock(return_value=[0.1] * 4)
        embeddings.embed_batch = AsyncMock(
            return_value=[[0.1] * 4, [0.2] * 4, [0.3] * 4]
        )

        # Mock expander returns three variants: original + 2 alts.
        expander = MagicMock()
        expander.expand = AsyncMock(
            return_value=["three word query", "alt phrasing", "alt jargon"]
        )

        heart = Heart.__new__(Heart)
        heart.settings = settings
        heart.agent_id = "nous-test"
        heart._embeddings = embeddings
        heart._owns_embeddings = False
        heart._bus = None
        heart._query_expander = expander  # type: ignore[attr-defined]

        heart.episodes = MagicMock()
        heart.episodes.search = AsyncMock(return_value=[])
        heart.facts = MagicMock()
        heart.facts.search = AsyncMock(return_value=[])
        heart.procedures = MagicMock()
        heart.procedures.search = AsyncMock(return_value=[])
        heart.censors = MagicMock()
        heart.censors.search = AsyncMock(return_value=[])

        session = AsyncMock()
        await heart._recall("three word query", limit=10, types=None, session=session)

        # Expander was called once with the original query.
        expander.expand.assert_awaited_once()
        # embed_batch was called for the variant list.
        embeddings.embed_batch.assert_awaited_once()

        # Sub-managers received variant_pairs (a non-None list of (text, emb) pairs).
        for mock in (heart.facts.search, heart.episodes.search, heart.procedures.search):
            assert mock.call_args is not None, "Sub-manager search must be called"
            vp = mock.call_args.kwargs.get("variant_pairs")
            assert vp is not None, (
                f"Flag-on path must pass variant_pairs to {mock} (got None)"
            )
            assert len(vp) == 3, (
                f"Expected 3 variant pairs (original + 2 alts), got {len(vp)}"
            )

    @pytest.mark.asyncio
    async def test_embed_batch_failure_falls_back_to_single_query(self) -> None:
        """Plan §10 silent-failure-surface row 10: ``embed_batch`` raising
        triggers fallback to single-query path with variant_pairs=None."""
        if not _heart_has_set_query_expander():
            pytest.skip("Integration not yet landed")

        settings = _stub_settings(query_expansion_enabled=True)

        embeddings = MagicMock()
        embeddings.embed = AsyncMock(return_value=[0.1] * 4)
        embeddings.embed_batch = AsyncMock(side_effect=RuntimeError("openai down"))

        expander = MagicMock()
        expander.expand = AsyncMock(
            return_value=["three word query", "alt phrasing"]
        )

        heart = Heart.__new__(Heart)
        heart.settings = settings
        heart.agent_id = "nous-test"
        heart._embeddings = embeddings
        heart._owns_embeddings = False
        heart._bus = None
        heart._query_expander = expander  # type: ignore[attr-defined]

        heart.episodes = MagicMock()
        heart.episodes.search = AsyncMock(return_value=[])
        heart.facts = MagicMock()
        heart.facts.search = AsyncMock(return_value=[])
        heart.procedures = MagicMock()
        heart.procedures.search = AsyncMock(return_value=[])
        heart.censors = MagicMock()
        heart.censors.search = AsyncMock(return_value=[])

        session = AsyncMock()
        # Must NOT raise — silent fallback to variant_pairs=None.
        await heart._recall("three word query", limit=10, types=None, session=session)

        # Sub-managers must have been called (the fallback path didn't bail).
        assert heart.facts.search.call_count >= 1


# ---------------------------------------------------------------------------
# FactManager._search — variant_pairs routing
# ---------------------------------------------------------------------------


class TestFactSearchVariantPairsRouting:
    @pytest.mark.asyncio
    async def test_facts_search_variant_pairs_routes_to_multi(self) -> None:
        """FactManager._search with variant_pairs (len > 1) routes to
        hybrid_search_multi instead of hybrid_search."""
        if not _has_variant_pairs_kwarg(FactManager.search):
            pytest.skip("FactManager.search lacks variant_pairs kwarg yet")

        # Build a bare FactManager instance — bypass __init__ DB plumbing.
        fm = FactManager.__new__(FactManager)
        fm.agent_id = "nous-test"
        fm.embeddings = MagicMock()
        fm.embeddings.embed = AsyncMock(return_value=[0.1] * 4)
        fm._admission_controller = None
        fm.db = MagicMock()
        # F027 supersession filter is a no-op when applied to empty list,
        # but stub it just in case the implementation calls it pre-emptively.
        fm.apply_supersession_filter = lambda summaries: summaries
        fm._fire_track_access = lambda ids: None

        session = AsyncMock()
        # session.execute returns scalars().all() → empty list of facts
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=[])
        result_proxy = MagicMock()
        result_proxy.scalars = MagicMock(return_value=scalars)
        session.execute = AsyncMock(return_value=result_proxy)

        variant_pairs = [
            ("three word query", [0.1] * 4),
            ("alt phrasing", [0.2] * 4),
        ]

        with (
            patch(
                "nous.heart.facts.hybrid_search_multi",
                new=AsyncMock(return_value=[]),
            ) as mock_multi,
            patch(
                "nous.heart.facts.hybrid_search",
                new=AsyncMock(return_value=[]),
            ) as mock_single,
        ):
            await fm.search(
                "three word query",
                limit=10,
                session=session,
                variant_pairs=variant_pairs,
            )

        # Multi path taken; single-query path NOT taken.
        assert mock_multi.await_count == 1, (
            "variant_pairs (len > 1) must route to hybrid_search_multi"
        )
        assert mock_single.await_count == 0, (
            "Single-query hybrid_search must NOT fire when multi path is selected"
        )

    @pytest.mark.asyncio
    async def test_facts_search_variant_pairs_none_unchanged_path(self) -> None:
        """variant_pairs=None (or absent) → single-query path (regression).

        This is the byte-identical guarantee — the only callers in the
        codebase today pass nothing, so behavior must be unchanged.
        """
        fm = FactManager.__new__(FactManager)
        fm.agent_id = "nous-test"
        fm.embeddings = MagicMock()
        fm.embeddings.embed = AsyncMock(return_value=[0.1] * 4)
        fm._admission_controller = None
        fm.db = MagicMock()
        fm.apply_supersession_filter = lambda summaries: summaries
        fm._fire_track_access = lambda ids: None

        session = AsyncMock()
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=[])
        result_proxy = MagicMock()
        result_proxy.scalars = MagicMock(return_value=scalars)
        session.execute = AsyncMock(return_value=result_proxy)

        with patch(
            "nous.heart.facts.hybrid_search",
            new=AsyncMock(return_value=[]),
        ) as mock_single:
            # Try both call shapes the API may offer.
            if _has_variant_pairs_kwarg(FactManager.search):
                await fm.search(
                    "three word query",
                    limit=10,
                    session=session,
                    variant_pairs=None,
                )
            else:
                await fm.search("three word query", limit=10, session=session)

        # Single-query path fired exactly once.
        assert mock_single.await_count == 1


# ---------------------------------------------------------------------------
# active_only=False bypass — plan v2 arch P1-2 documentation test
# ---------------------------------------------------------------------------


class TestActiveOnlyFalseBypass:
    """Plan v2 arch P1-2: ``FactManager._search`` routes ``active_only=False``
    through ``_search_all`` which does NOT call ``hybrid_search`` — so any
    variant_pairs passed on this path is silently ignored.

    Currently safe: the only public caller (Heart._recall) always passes
    active_only=True. This test documents the silent-skip so a future change
    surfaces the expected behavior.
    """

    @pytest.mark.asyncio
    async def test_active_only_false_path_silently_skips_variants(self) -> None:
        """variant_pairs is ignored on the active_only=False bypass path.

        This is documented (plan §"facts.py::_search") behavior — when a UI
        ever surfaces inactive facts, the variant routing won't fire there.
        We assert _search_all is taken and hybrid_search/_multi are NOT
        called from the public path.
        """
        fm = FactManager.__new__(FactManager)
        fm.agent_id = "nous-test"
        fm.embeddings = MagicMock()
        fm.embeddings.embed = AsyncMock(return_value=[0.1] * 4)
        fm._admission_controller = None
        fm.db = MagicMock()
        fm.apply_supersession_filter = lambda summaries: summaries
        fm._fire_track_access = lambda ids: None
        # Stub _search_all so we can assert it was the chosen path.
        fm._search_all = AsyncMock(return_value=[])  # type: ignore[assignment]

        session = AsyncMock()

        with (
            patch(
                "nous.heart.facts.hybrid_search",
                new=AsyncMock(return_value=[]),
            ) as mock_single,
        ):
            kwargs: dict = {
                "limit": 10,
                "session": session,
                "active_only": False,
            }
            if _has_variant_pairs_kwarg(FactManager.search):
                kwargs["variant_pairs"] = [
                    ("three word query", [0.1] * 4),
                    ("alt phrasing", [0.2] * 4),
                ]
            await fm.search("three word query", **kwargs)

        # _search_all was called; hybrid_search was NOT.
        assert fm._search_all.await_count == 1, (
            "active_only=False must route through _search_all"
        )
        assert mock_single.await_count == 0, (
            "active_only=False bypass must skip hybrid_search entirely"
        )
        # If the multi-flavour exists, it must also be untouched on this path.
        try:
            from nous.heart.facts import hybrid_search_multi  # noqa: F401

            with patch(
                "nous.heart.facts.hybrid_search_multi",
                new=AsyncMock(return_value=[]),
            ) as mock_multi:
                await fm.search("three word query", **kwargs)
            assert mock_multi.await_count == 0, (
                "active_only=False bypass must skip hybrid_search_multi too"
            )
        except ImportError:
            pass
