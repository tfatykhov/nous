"""F055 — Cross-Turn Residual Activation.

Per-session, decaying activation boost on memories surfaced in recent recalls.
Implements the "train of thought" property that current memoryless recall lacks.

Two mechanisms (both flag-gated):

  A. Seed injection: top-N residually-activated nodes added to F022's
     spreading activation seed list at the recall_deep call-site.
  B. Post-fusion boost: additive bounded boost on RRF-normalized scores
     applied AFTER RRF/graph merge but BEFORE F042 cross-encoder rerank
     (per spec §B — boost biases CE's head-cut input rather than overriding
     CE's sigmoid-normalized output).

State is session-scoped:
  - ``ConversationState.turn_count`` (already populated by conversation pipeline)
    serves as the time variable.
  - ``WorkingMemory.items`` JSONB is extended in-place with two keys per item:
    ``activation`` (float 0-1) and ``last_surfaced_turn`` (int).

Fail-open: every method catches Exception and degrades to a no-op (returns
unchanged data). Pipeline runs unmodified when residual activation fails.

Production wiring:
  - ``main.py`` constructs a ResidualActivator after Heart is built and
    sets it via ``Heart.set_residual_activator(...)``.
  - ``recall_deep`` reads ``_session_id`` (injected by F051.4 dispatcher),
    calls ``compute_activations`` (``tools.py:1374``), and passes
    ``residual_activations`` through ``run_recall_pipeline`` into
    ``Heart.recall``.
  - ``seed_for_spreading`` has NO caller. It shapes residual activations into
    F022 spreading seeds, but ``run_recall_pipeline``'s Stage 4 builds its seed
    list only from the current query's decisions + top-3 facts, so cross-turn
    context never reaches the graph walk. (This docstring previously claimed
    ``recall_deep`` consumed it; it does not, and never has.) Wiring it is
    gated on the activation floor, not on the score path: a residual seed
    enters at ``activation * residual_seed_weight`` (<= 0.3), which needs an
    edge weight > 0.67 to clear the default 0.1 floor at depth 1 — against a
    dominant-relation average of 0.410. It would die inside the CTE.
  - ``Heart._recall`` calls ``boost_scores`` on merged candidates BEFORE
    F042 CE rerank (preserves CE sigmoid scoring on its own output).
  - After recall, ``recall_deep`` fires ``record_surfaced`` as
    ``asyncio.create_task`` so the request returns without waiting for
    the WM write.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from nous.config import Settings
    from nous.heart.schemas import RecallResult
    from nous.heart.working_memory import WorkingMemoryManager
    from nous.storage.database import Database

logger = logging.getLogger(__name__)


@dataclass
class _ItemActivation:
    """In-memory representation of a residual-activation entry."""

    node_id: UUID
    node_type: str
    activation: float
    last_surfaced_turn: int


class ResidualActivator:
    """Compute cross-turn residual activation and apply it to recall scoring.

    Stateless across sessions — reads/writes WorkingMemory.items + ConversationState
    via the injected Heart's persistence layer. Every method is fail-open.
    """

    def __init__(
        self,
        settings: "Settings",
        wm: "WorkingMemoryManager",
        db: "Database",
    ) -> None:
        self._settings = settings
        self._wm = wm
        self._db = db

    async def current_turn(self, agent_id: str, session_id: str) -> int:
        """Read ConversationState.turn_count. Returns 0 if no row or on error."""
        try:
            from sqlalchemy import select
            from nous.storage.models import ConversationState

            async with self._db.session() as sa_session:
                result = await sa_session.execute(
                    select(ConversationState.turn_count).where(
                        ConversationState.agent_id == agent_id,
                        ConversationState.session_id == session_id,
                    )
                )
                row = result.scalar_one_or_none()
                return int(row) if row is not None else 0
        except Exception:
            logger.warning("F055: current_turn raised for %s/%s", agent_id, session_id)
            return 0

    def _decay_factor(self, turns_since: int) -> float:
        """Apply geometric or power-law decay based on settings.

        Geometric: decay^turns_since
        Power-law (ACT-R): (turns_since + 1)^(-alpha)
        """
        if turns_since < 0:
            return 0.0
        mode = getattr(self._settings, "residual_decay_mode", "geometric")
        if mode == "power_law":
            alpha = float(getattr(self._settings, "residual_power_law_alpha", 0.5))
            return math.pow(turns_since + 1, -alpha)
        # geometric (default)
        decay = float(getattr(self._settings, "residual_decay_per_turn", 0.5))
        return math.pow(decay, turns_since)

    async def compute_activations(
        self,
        agent_id: str,
        session_id: str,
        current_turn: int,
    ) -> dict[UUID, float]:
        """Return {node_id: activation} for items still above the floor.

        Reads raw WorkingMemory.items rows pre-pydantic-parse so the extra
        residual JSONB keys (``activation``, ``last_surfaced_turn``) are
        accessible. Items missing those keys are skipped (haven't been
        through record_surfaced yet — pre-F055 sessions).

        Returns an empty dict on any error (fail-open).
        """
        try:
            raw_items = await self._wm.list_raw_items(agent_id, session_id)
        except Exception:
            logger.warning("F055: list_raw_items raised for %s/%s", agent_id, session_id)
            return {}

        floor = float(getattr(self._settings, "residual_activation_floor", 0.05))
        top_k = int(getattr(self._settings, "residual_top_k_carried", 20))

        scored: list[tuple[UUID, float]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            ref_id = item.get("ref_id")
            base_activation = item.get("activation")
            last_turn = item.get("last_surfaced_turn")
            if ref_id is None or base_activation is None or last_turn is None:
                continue
            try:
                node_id = UUID(str(ref_id))
                base = float(base_activation)
                last = int(last_turn)
            except (ValueError, TypeError):
                continue
            decayed = base * self._decay_factor(current_turn - last)
            if decayed >= floor:
                scored.append((node_id, decayed))

        # Top-K bound + sort descending.
        scored.sort(key=lambda x: x[1], reverse=True)
        return dict(scored[:top_k])

    def seed_for_spreading(
        self,
        activations: dict[UUID, float],
    ) -> list[tuple[UUID, str, float]]:
        """Top-N seeds for F022 spreading_activation_search.

        F022's seeds are typed (id, type, score). We don't track per-item
        type in the activations dict, so default to "fact" — the spreading
        CTE handles unknown types as opaque starting points.

        Empty dict → empty list (no extra seeds, F022 uses query-only seeds).
        """
        if not activations:
            return []
        top_n = int(getattr(self._settings, "residual_top_n_seeds", 5))
        seed_weight = float(getattr(self._settings, "residual_seed_weight", 0.3))
        sorted_items = sorted(activations.items(), key=lambda x: x[1], reverse=True)[:top_n]
        return [(node_id, "fact", a * seed_weight) for node_id, a in sorted_items]

    def boost_scores(
        self,
        candidates: list["RecallResult"],
        activations: dict[UUID, float],
    ) -> list["RecallResult"]:
        """Apply additive bounded boost on post-RRF scores BEFORE CE rerank.

        Spec §B: position is between RRF/graph merge and F042 CE — the boost
        biases CE's head-cut input rather than overriding CE's sigmoid output.

        Mutates ``score`` in place (matches F042 reranker pattern).
        Score clamped to [0, 1].
        """
        if not activations or not candidates:
            return candidates
        boost_weight = float(getattr(self._settings, "residual_boost_weight", 0.15))
        if boost_weight <= 0:
            return candidates
        boosted = 0
        for r in candidates:
            a = activations.get(r.id)
            if a is None or a <= 0:
                continue
            try:
                base = float(r.score) if r.score is not None else 0.0
                r.score = min(1.0, base + a * boost_weight)
                boosted += 1
            except (TypeError, ValueError):
                continue
        if boosted:
            logger.info(
                "F055: boosted %d/%d candidates (weight=%.3f)",
                boosted, len(candidates), boost_weight,
            )
        return candidates

    async def record_surfaced(
        self,
        agent_id: str,
        session_id: str,
        current_turn: int,
        surfaced: list[tuple],
    ) -> None:
        """Write surfaced items back to WM.items with residual JSONB keys.

        CRITICAL (spec §3 fix #2): opens its OWN DB session via the injected
        WorkingMemoryManager.  This method runs as ``asyncio.create_task``
        from the recall_deep tool, so it outlives the request context.
        Reusing the caller's AsyncSession would corrupt connection state.
        """
        if not surfaced:
            return
        try:
            top_k = int(getattr(self._settings, "residual_top_k_carried", 20))
            # Rank-normalize surfaced scores so activation lives in [0, 1].
            # Tuples are (id, type, score) or (id, type, score, snippet) —
            # the 4th element (audit E2) carries real content so WM entries
            # render meaningfully instead of as "residual fact" stubs.
            max_score = max((entry[2] for entry in surfaced), default=0.0)
            if max_score <= 0:
                return
            # ``loaded_at`` is a required ``datetime`` on WorkingMemoryItem
            # (heart/schemas.py:305). Previously this was written as ``None``,
            # which the JSONB roundtrip via ``_to_state`` rejected with a
            # pydantic ValidationError, breaking /status?dashboard=true and
            # the pre_turn working-memory init in prod. Use the surface time
            # as the load time — semantically correct since residual surfacing
            # IS a load event into WM.
            now_iso = datetime.now(UTC).isoformat()
            entries: list[dict] = []
            for entry in surfaced[:top_k]:
                node_id, node_type, score = entry[0], entry[1], entry[2]
                snippet = str(entry[3]).strip() if len(entry) > 3 and entry[3] else ""
                entries.append({
                    "type": node_type,
                    "ref_id": str(node_id),
                    "summary": snippet[:160] or f"residual {node_type}",
                    "relevance": float(score) / max_score,
                    "loaded_at": now_iso,
                    "activation": float(score) / max_score,
                    "last_surfaced_turn": current_turn,
                })
            await self._wm.upsert_residual_items(
                agent_id=agent_id,
                session_id=session_id,
                items=entries,
                max_residual_items=top_k,
                # codex P2: rank carried entries by their CURRENT decayed
                # activation so stale high-activation entries can't starve
                # fresh surfaces out of the cap. The activator owns the
                # decay model; the manager just applies it.
                current_turn=current_turn,
                decay_fn=self._decay_factor,
            )
        except Exception:
            logger.warning(
                "F055: record_surfaced failed for %s/%s (turn=%d, n=%d)",
                agent_id, session_id, current_turn, len(surfaced),
                exc_info=True,  # review P3: the WHY must reach the log
            )
