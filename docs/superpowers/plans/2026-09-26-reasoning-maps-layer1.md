# Reasoning Maps — Layer 1: Strategy Cards (v1.0)

**Goal:** When a decision is resolved (success / partial / failure), distil a *strategy card* from that outcome and store it as a `heart.procedure` with `kind='strategy'`. Cards are recallable through the normal procedure path; a per-turn cap (default 1) prevents noise amplification. Two feature flags default OFF so merging is inert until explicitly enabled.

**Architecture:**
- `decision_reviewed` bus event → `StrategyCardDistiller` handler → `call_background_llm_structured` → `ProcedureManager.store` + `GraphLinker.create_edge`
- `Procedure.kind = 'strategy'` written to a new `kind` column (migration 077); idempotency via `runtime_metadata->>'source_decision_id'` lookup
- Context cap: after relevance filter in `ContextEngine.build`, slice strategy cards to `strategy_cards_max_per_turn` when `strategy_cards_retrieval_enabled`

**Tech stack:** Python 3.12+, SQLAlchemy 2 async, pydantic v2, pytest (`asyncio_mode = "auto"`) with SQLite default / Postgres via `NOUS_TEST_DB=postgres`.

**Scope:** Layer 1 only. Layers 2 and 3 are described below as future phases with entry criteria; they are NOT implemented here.

---

## Global Constraints

- `noise` and `superseded` outcomes produce NO card (skip early in the handler).
- Distillation is off the hot path: the handler is async; the resolve call returns before distillation starts.
- Idempotent per decision: a re-resolve triggers an UPDATE (or re-distil) of the existing card, never a new duplicate.
- Every new setting ships with `False` / `0` / conservative default; nothing activates on deploy.
- Migration 077: `IF NOT EXISTS`, no `BEGIN`/`COMMIT`, full-line `--` comments only, no `;` inside comments.
- Tests run on SQLite by default; Postgres-specific features are gated behind `pytest.mark.postgres_only`.

---

## File Map

| File | Responsibility | Tasks |
|---|---|---|
| `sql/migrations/077_strategy_cards.sql` | Add `kind VARCHAR(100) NULL` to `heart.procedures` | A |
| `nous/storage/models.py` | ORM mirror of migration 077 | A |
| `nous/heart/schemas.py` | `ProcedureInput.kind` + `ProcedureDetail.kind` | A |
| `nous/heart/procedures.py` | Pass `kind` through `_store()` | A |
| `nous/config.py` | Three new settings | B |
| `nous/handlers/strategy_card_distiller.py` | New handler (distillation, idempotency, graph edge) | C |
| `nous/main.py` | Wire handler into bus | D |
| `nous/cognitive/context.py` | Cap strategy cards per turn, log hit rate | E |
| `tests/test_strategy_card_distiller.py` | Focused tests with mutation evidence | F |

---

## Task A — Migration + ORM + schema

### Step A1: Migration file

Create `sql/migrations/077_strategy_cards.sql`:

```sql
-- 077: add kind column to heart.procedures for strategy cards (Reasoning Maps L1)
ALTER TABLE heart.procedures ADD COLUMN IF NOT EXISTS kind VARCHAR(100) NULL;

CREATE INDEX IF NOT EXISTS idx_procedures_kind
    ON heart.procedures (agent_id, kind)
    WHERE kind IS NOT NULL;
```

### Step A2: ORM model

In `nous/storage/models.py`, inside the `Procedure` class after the `tags` column, add:

```python
kind: Mapped[str | None] = mapped_column(String(100), nullable=True)
```

### Step A3: Schemas

In `nous/heart/schemas.py`, `ProcedureInput` (line 285) add:
```python
kind: str | None = None
```

In `ProcedureDetail` (line 305) add:
```python
kind: str | None = None
```

### Step A4: ProcedureManager._store

In `nous/heart/procedures.py`, `_store()` (line 134), pass `kind=input.kind` to the `Procedure(...)` constructor.

---

## Task B — Config

In `nous/config.py`, add three settings to the `Settings` class:

```python
# Reasoning Maps L1 — strategy card distillation
strategy_cards_enabled: bool = False

# Reasoning Maps L1 — retrieval cap (False = no cap applied; see strategy_cards_max_per_turn)
strategy_cards_retrieval_enabled: bool = False

# Reasoning Maps L1 — max strategy cards injected per turn (0 = no limit when enabled)
strategy_cards_max_per_turn: int = 1
```

---

## Task C — Handler

Create `nous/handlers/strategy_card_distiller.py`:

```python
"""Strategy Card Distiller — Reasoning Maps Layer 1.

Listens to decision_reviewed events and distils a strategy card
(kind='strategy' procedure) for graded outcomes (success/partial/failure).
Skips noise and superseded. Idempotent per decision_id.

Flags:
  NOUS_STRATEGY_CARDS_ENABLED=false (distillation off by default)
"""

import asyncio
import logging
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from nous.brain.brain import Brain
    from nous.config import Settings
    from nous.events import EventBus
    from nous.heart.heart import Heart

from nous.brain.schemas import GRADED_OUTCOMES
from nous.handlers import LLMClient, call_background_llm_structured
from nous.heart.schemas import ProcedureInput

logger = logging.getLogger(__name__)

_CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Short title for the strategy card (≤80 chars)"},
        "description": {"type": "string", "description": "One-sentence summary of the lesson"},
        "lesson": {"type": "string", "description": "The full strategy or guardrail in 2-4 sentences: when X, do/avoid Y, because Z"},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "2-4 lowercase tags"},
    },
    "required": ["name", "description", "lesson"],
}

_SYSTEM_PROMPT = """You extract concise strategy cards from decision outcomes.

A strategy card captures a transferable lesson from a past decision outcome:
- success → validated strategy: "when X, doing Y works because Z"
- partial → qualified lesson: "when X, Y partially works but note Z"
- failure → guardrail: "when X, avoid Y because Z"

Keep the lesson actionable, context-free (no proper names), and ≤4 sentences."""


class StrategyCardDistiller:
    """Distils strategy cards from resolved decisions (Reasoning Maps L1)."""

    def __init__(
        self,
        brain: "Brain",
        heart: "Heart",
        settings: "Settings",
        bus: "EventBus | None",
        llm_client: "LLMClient | None" = None,
        graph_linker=None,
    ) -> None:
        self._brain = brain
        self._heart = heart
        self._settings = settings
        self._llm = llm_client
        self._graph_linker = graph_linker
        if bus is not None:
            bus.on("decision_reviewed", self._on_decision_reviewed)

    # ------------------------------------------------------------------
    # Event entry-point
    # ------------------------------------------------------------------

    def _on_decision_reviewed(self, event) -> None:
        """Sync shim: schedule the async handler as a fire-and-forget task."""
        if not getattr(self._settings, "strategy_cards_enabled", False):
            return
        outcome = event.get("outcome") if isinstance(event, dict) else getattr(event, "outcome", None)
        if outcome not in GRADED_OUTCOMES:
            return
        decision_id_raw = event.get("decision_id") if isinstance(event, dict) else getattr(event, "decision_id", None)
        if not decision_id_raw:
            return
        try:
            decision_id = UUID(str(decision_id_raw))
        except (ValueError, AttributeError):
            logger.warning("StrategyCardDistiller: invalid decision_id %r, skipping", decision_id_raw)
            return
        asyncio.create_task(self._distil(decision_id, outcome))

    # ------------------------------------------------------------------
    # Core distillation
    # ------------------------------------------------------------------

    async def _distil(self, decision_id: UUID, outcome: str) -> None:
        """Fetch decision, distil card, store, link. All errors are swallowed."""
        try:
            await self._do_distil(decision_id, outcome)
        except Exception:
            logger.warning(
                "StrategyCardDistiller: distillation failed for decision %s", decision_id,
                exc_info=True,
            )

    async def _do_distil(self, decision_id: UUID, outcome: str) -> None:
        if not self._llm:
            logger.debug("StrategyCardDistiller: no LLM client, skipping %s", decision_id)
            return

        decision = await self._brain.get(decision_id)
        if decision is None:
            logger.warning("StrategyCardDistiller: decision %s not found", decision_id)
            return

        # Idempotency: find existing card for this decision
        existing_id = await self._find_existing_card(decision_id)

        # Build LLM prompt
        user_msg = (
            f"Decision: {decision.description}\n"
            f"Context: {decision.context or '(none)'}\n"
            f"Outcome: {outcome}\n"
            f"Result notes: {decision.outcome_result or '(none)'}\n\n"
            f"Extract a strategy card for this {outcome} outcome."
        )

        card = await call_background_llm_structured(
            client=self._llm,
            model=getattr(self._settings, "background_model", "claude-haiku-4-5-20251001"),
            system_prompt=_SYSTEM_PROMPT,
            user_message=user_msg,
            tool_name="emit_strategy_card",
            tool_description="Emit the distilled strategy card",
            output_schema=_CARD_SCHEMA,
            max_tokens=512,
        )
        if not card:
            logger.warning("StrategyCardDistiller: LLM returned no card for decision %s", decision_id)
            return

        name = (card.get("name") or "")[:500].strip()
        description = (card.get("description") or "")[:1000].strip()
        lesson = (card.get("lesson") or "")[:2000].strip()
        tags = card.get("tags") or []
        if isinstance(tags, list):
            tags = [str(t)[:100] for t in tags[:6]]

        if not name or not lesson:
            logger.warning("StrategyCardDistiller: incomplete card for decision %s, skipping", decision_id)
            return

        # Deactivate old card before creating new one (update-not-duplicate)
        if existing_id is not None:
            try:
                await self._heart.procedures._deactivate(existing_id)
            except Exception:
                logger.warning("StrategyCardDistiller: failed to deactivate old card %s", existing_id, exc_info=True)

        inp = ProcedureInput(
            name=name,
            domain="strategy",
            description=description,
            implementation_notes=[lesson],
            tags=tags,
            kind="strategy",
            runtime_metadata={"source_decision_id": str(decision_id), "outcome": outcome},
        )

        async with self._heart.db.session() as session:
            detail = await self._heart.procedures.store(inp, session=session)
            # Create graph edge: strategy card extracted_from decision
            if self._graph_linker is not None:
                try:
                    await self._graph_linker.create_edge(
                        source_id=detail.id,
                        source_type="procedure",
                        target_id=decision_id,
                        target_type="decision",
                        relation="extracted_from",
                        weight=1.0,
                        session=session,
                        provenance_source="strategy_card_distiller",
                    )
                except Exception:
                    logger.warning("StrategyCardDistiller: edge creation failed for card %s", detail.id, exc_info=True)
            await session.commit()

        logger.info(
            "StrategyCardDistiller: distilled %s card for decision %s → procedure %s",
            outcome, decision_id, detail.id,
        )

    # ------------------------------------------------------------------
    # Idempotency helpers
    # ------------------------------------------------------------------

    async def _find_existing_card(self, decision_id: UUID) -> UUID | None:
        """Return the id of an existing active strategy card for this decision, or None."""
        from sqlalchemy import select, text as sa_text
        from nous.storage.models import Procedure

        async with self._heart.db.session() as session:
            result = await session.execute(
                select(Procedure.id)
                .where(Procedure.agent_id == self._brain.agent_id)
                .where(Procedure.kind == "strategy")
                .where(Procedure.active.is_(True))
                .where(
                    Procedure.runtime_metadata["source_decision_id"].astext == str(decision_id)
                )
                .limit(1)
            )
            row = result.scalar_one_or_none()
            return row

    async def _deactivate_procedure(self, procedure_id: UUID) -> None:
        """Soft-delete a procedure by id."""
        from sqlalchemy import update
        from nous.storage.models import Procedure

        async with self._heart.db.session() as session:
            await session.execute(
                update(Procedure)
                .where(Procedure.id == procedure_id)
                .where(Procedure.agent_id == self._brain.agent_id)
                .values(active=False)
            )
            await session.commit()
```

> **Note on deactivation**: `ProcedureManager` doesn't expose a direct `_deactivate()` - the handler does it inline via SQLAlchemy update. This is simpler than adding a new public method just for this use-case.

---

## Task D — Wiring in main.py

After the `decision_reviewer` block (around line 441), add:

```python
try:
    from nous.handlers.strategy_card_distiller import StrategyCardDistiller
    if settings.strategy_cards_enabled:
        strategy_card_distiller = StrategyCardDistiller(
            brain=brain,
            heart=heart,
            settings=settings,
            bus=bus,
            llm_client=api_client,
            graph_linker=graph_linker,
        )
    else:
        strategy_card_distiller = None
except ImportError:
    strategy_card_distiller = None
    logger.debug("StrategyCardDistiller not available yet")
```

Wire `strategy_card_distiller` into `components` dict and the shutdown path.

---

## Task E — Context cap + instrumentation

In `nous/cognitive/context.py`, after the `embedding_slot_limit` slice (around line 1378), when `strategy_cards_retrieval_enabled`:

```python
# Reasoning Maps L1: cap strategy cards per turn
if getattr(self._settings, "strategy_cards_retrieval_enabled", False):
    max_sc = getattr(self._settings, "strategy_cards_max_per_turn", 1)
    strategy_hits = [p for p in embedding_procedures if getattr(p, "kind", None) == "strategy"]
    non_strategy = [p for p in embedding_procedures if getattr(p, "kind", None) != "strategy"]
    strategy_served = strategy_hits[:max_sc]
    embedding_procedures = non_strategy + strategy_served
    if strategy_hits:
        logger.debug(
            "StrategyCards: retrieved=%d served=%d (cap=%d)",
            len(strategy_hits), len(strategy_served), max_sc,
        )
```

---

## Task F — Tests

Create `tests/test_strategy_card_distiller.py` with:

1. `test_skip_noise_outcome` — handler ignores noise; mutation: remove noise guard → assert fails
2. `test_skip_superseded_outcome` — handler ignores superseded
3. `test_no_llm_client_skips` — no LLM wired → no procedure created
4. `test_distil_success_outcome` — success outcome → card stored with kind='strategy', runtime_metadata includes source_decision_id
5. `test_distil_failure_outcome` — failure → card stored
6. `test_idempotency_deactivates_old_card` — re-resolving a decision deactivates old card, creates new one
7. `test_context_cap_strategy_cards` — when cap=1, only 1 strategy card passes the filter even if 3 retrieved
8. `test_kind_field_stored_on_procedure` — `ProcedureInput(kind='strategy')` → `ProcedureDetail.kind == 'strategy'`

All tests use async mock for the LLM client; no real DB required for the logic tests.

---

## Layer 2 Plan (not implemented — entry criterion: L1 hit-rate > 5% over 2 weeks)

**Logical edges** (`enables`, `prevents`, `motivates`, `part_of`, `precedes`) added to `brain.graph_edges` by a sleep-cycle pass that links:
- decision_reasons → evidence facts/episodes
- strategy card procedures → related procedures (precedes)

Migration 078: no new table, only new values for the `relation` enum CHECK.

**Entry criterion:** strategy card retrieval hit-rate (served/turn) > 5% averaged over 2 weeks of production, verified via the F091 retrieval telemetry dashboard.

---

## Layer 3 Plan (not implemented — entry criterion: L2 edge density > 0.3)

**`recall_reasoned` tool** — opt-in, separate from `recall_deep`, flagged OFF.  
Given a query, traverses the logical-edge graph to find chains of supporting/blocking evidence; returns a reasoning trace alongside the memory items.

**Entry criterion:** L2 edge density (average logical edges per decision) > 0.3, ensuring the traversal finds non-trivial paths. Never becomes the default path.
