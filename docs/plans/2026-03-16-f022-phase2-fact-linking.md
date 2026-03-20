# F022 Phase 2 Gap Fix: Fact-to-Decision Auto-Linking

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the existing `GraphLinker.link_fact_to_decisions()` method so it actually runs when facts are created, completing F022 Phase 2's cross-type graph linking.

**Architecture:** The `fact_learned` event currently only writes to the DB audit table — it never reaches the in-process EventBus. We add a `bus.emit()` call in `Heart.learn()` and create a `FactGraphLinker` handler that subscribes to it, calling the existing `GraphLinker.link_fact_to_decisions()` in an isolated DB session. This mirrors the `EpisodeSummarizer` → `GraphLinker.link_episode_deterministic()` pattern exactly.

**Tech Stack:** Python 3.12+, asyncio EventBus, SQLAlchemy async, pgvector, pytest + pytest-asyncio

**Decision ID:** `3b63a909`

---

## Background: Why This Is Needed

F022 Phase 2 (Cross-Type Edges) is ~90% implemented. The schema, indexes, relation types, `neighbors()`, `recall_deep` expansion, and episode linking all work. But `GraphLinker.link_fact_to_decisions()` (`nous/brain/graph_linker.py:47`) is never called — no handler subscribes to fact creation events for graph linking.

**Key discovery:** The `fact_learned` event is emitted via `FactManager._emit_event()` (`nous/heart/facts.py:193`), which inserts a row into the `nous_system.events` DB table. This is an **audit log**, not the in-process `EventBus`. The `EventBus` (`nous/events.py`) is fed exclusively via `bus.emit()` calls. There is no DB→bus bridge. A handler subscribing to `"fact_learned"` on the bus would never fire.

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `nous/heart/heart.py` | Modify | Add `_bus` attribute, emit `fact_learned` on EventBus after fact creation |
| `nous/handlers/fact_graph_linker.py` | Create | New handler: subscribe to `fact_learned`, call `GraphLinker.link_fact_to_decisions()` |
| `nous/main.py` | Modify | Wire handler, inject bus into Heart, hoist GraphLinker construction |
| `tests/test_fact_graph_linker.py` | Create | Unit + integration tests for the new handler and bus emission |

---

## Chunk 1: Implementation

### Task 1: Add EventBus Emission to Heart.learn()

**Files:**
- Modify: `nous/heart/heart.py:61-82` (add `_bus` attribute in `__init__`)
- Modify: `nous/heart/heart.py:197-217` (add bus emit after `self.facts.learn()`)
- Test: `tests/test_fact_graph_linker.py`

- [ ] **Step 1: Write the failing test for bus emission**

Create `tests/test_fact_graph_linker.py`:

```python
"""Tests for F022 Phase 2 gap fix: fact-to-decision auto-linking."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nous.events import Event, EventBus
from nous.heart.schemas import FactDetail, FactInput


def _fake_fact_detail(**overrides):
    """Create a FactDetail with all required fields populated."""
    defaults = dict(
        id=uuid4(),
        agent_id="test-agent",
        content="PostgreSQL uses MVCC",
        category="technical",
        subject="PostgreSQL",
        confidence=0.9,
        source="test",
        source_episode_id=None,
        source_decision_id=None,
        learned_at=datetime.now(UTC),
        last_confirmed=None,
        confirmation_count=0,
        superseded_by=None,
        contradiction_of=None,
        active=True,
        tags=[],
        created_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return FactDetail(**defaults)


class TestHeartBusEmission:
    """Verify Heart.learn() emits fact_learned on the EventBus."""

    @pytest.mark.asyncio
    async def test_learn_emits_fact_learned_on_bus(self):
        """When Heart._bus is set, learn() should emit fact_learned with content."""
        from nous.heart.heart import Heart

        heart = MagicMock(spec=Heart)
        heart._bus = MagicMock(spec=EventBus)
        heart._bus.emit = AsyncMock()

        fake_detail = _fake_fact_detail(
            content="PostgreSQL uses MVCC",
            subject="PostgreSQL",
        )
        heart.facts = AsyncMock()
        heart.facts.learn = AsyncMock(return_value=fake_detail)
        heart.agent_id = "test-agent"

        # Call the real learn method with mocked internals
        result = await Heart.learn(heart, FactInput(content="PostgreSQL uses MVCC", subject="PostgreSQL"))

        assert result == fake_detail
        heart._bus.emit.assert_called_once()
        emitted = heart._bus.emit.call_args[0][0]
        assert emitted.type == "fact_learned"
        assert emitted.data["fact_id"] == str(fake_detail.id)
        assert emitted.data["content"] == "PostgreSQL uses MVCC"
        assert emitted.data["category"] == "technical"
        assert emitted.data["subject"] == "PostgreSQL"

    @pytest.mark.asyncio
    async def test_learn_works_without_bus(self):
        """When Heart._bus is None, learn() should still work (no emission)."""
        from nous.heart.heart import Heart

        heart = MagicMock(spec=Heart)
        heart._bus = None

        fake_detail = _fake_fact_detail(content="test fact", subject=None)
        heart.facts = AsyncMock()
        heart.facts.learn = AsyncMock(return_value=fake_detail)

        result = await Heart.learn(heart, FactInput(content="test fact"))
        assert result == fake_detail
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_fact_graph_linker.py::TestHeartBusEmission -v`
Expected: FAIL — `Heart` has no `_bus` attribute, `learn()` doesn't emit

- [ ] **Step 3: Add `_bus` attribute to Heart.__init__**

In `nous/heart/heart.py`, after line 81 (`self.schedules = ScheduleManager(...)`), add:

```python
        # F022 Phase 2: Optional EventBus for fact_learned emission.
        # Injected post-construction in main.py (not a constructor param
        # to keep Heart's interface stable).
        self._bus: EventBus | None = None
```

Add the import at top of file (after `from nous.storage.database import Database`):

```python
from nous.events import Event, EventBus
```

- [ ] **Step 4: Modify Heart.learn() to emit on bus**

Replace `nous/heart/heart.py` lines 197-217:

```python
    async def learn(
        self,
        input: FactInput,
        session: AsyncSession | None = None,
        encoded_frame: str | None = None,
        encoded_censors: list[str] | None = None,
    ) -> FactDetail:
        """Store a new fact with deduplication.

        Args:
            input: Fact data.
            session: Optional DB session.
            encoded_frame: Active frame when fact was learned (003.2).
            encoded_censors: Active censors when fact was learned (003.2).
        """
        result = await self.facts.learn(
            input,
            session=session,
            encoded_frame=encoded_frame,
            encoded_censors=encoded_censors,
        )

        # F022 Phase 2: Emit on in-process EventBus for cross-type graph linking.
        # The DB audit event (via FactManager._emit_event) does NOT reach the bus.
        if self._bus is not None:
            await self._bus.emit(Event(
                type="fact_learned",
                agent_id=self.agent_id,
                data={
                    "fact_id": str(result.id),
                    "content": result.content,
                    "category": result.category,
                    "subject": result.subject,
                },
            ))

        return result
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_fact_graph_linker.py::TestHeartBusEmission -v`
Expected: PASS (both tests)

- [ ] **Step 6: Commit**

```bash
git add nous/heart/heart.py tests/test_fact_graph_linker.py
git commit -m "feat(f022): emit fact_learned on EventBus from Heart.learn()

The fact_learned event was only written to the DB audit table via
FactManager._emit_event(), never reaching the in-process EventBus.
Add bus emission in Heart.learn() so handlers can subscribe to it.
The _bus attribute is injected post-construction to keep the
Heart constructor interface stable."
```

---

### Task 2: Create FactGraphLinker Handler

**Files:**
- Create: `nous/handlers/fact_graph_linker.py`
- Test: `tests/test_fact_graph_linker.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fact_graph_linker.py`:

```python
from nous.brain.graph_linker import GraphLinker
from nous.brain.schemas import GraphEdgeInfo
from nous.config import Settings


def _mock_settings(**overrides):
    """Create a mock Settings with cross_type_linking_enabled=True."""
    s = MagicMock(spec=Settings)
    s.cross_type_linking_enabled = overrides.get("cross_type_linking_enabled", True)
    return s


def _make_event(fact_id=None, content="test fact", category="technical", subject="test"):
    """Create a fact_learned Event."""
    return Event(
        type="fact_learned",
        agent_id="test-agent",
        data={
            "fact_id": str(fact_id or uuid4()),
            "content": content,
            "category": category,
            "subject": subject,
        },
    )


class TestFactGraphLinker:
    """Tests for the FactGraphLinker handler."""

    def _make_handler(self, graph_linker=None, settings=None, bus=None):
        from nous.handlers.fact_graph_linker import FactGraphLinker

        graph_linker = graph_linker or AsyncMock(spec=GraphLinker)
        settings = settings or _mock_settings()
        bus = bus or MagicMock(spec=EventBus)
        bus.on = MagicMock()

        handler = FactGraphLinker(graph_linker, settings, bus)
        return handler, graph_linker, bus

    def test_registers_on_fact_learned(self):
        """Handler should register on the fact_learned event."""
        _, _, bus = self._make_handler()
        bus.on.assert_called_once()
        assert bus.on.call_args[0][0] == "fact_learned"

    @pytest.mark.asyncio
    async def test_skips_when_linking_disabled(self):
        """Should return early when cross_type_linking_enabled=False."""
        settings = _mock_settings(cross_type_linking_enabled=False)
        handler, graph_linker, _ = self._make_handler(settings=settings)

        await handler.handle(_make_event())

        # GraphLinker should not be called at all
        graph_linker.db.session.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_content_missing(self):
        """Should return early when event has no content."""
        handler, graph_linker, _ = self._make_handler()
        event = Event(
            type="fact_learned",
            agent_id="test-agent",
            data={"fact_id": str(uuid4()), "content": ""},
        )

        await handler.handle(event)
        graph_linker.db.session.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_fact_id_missing(self):
        """Should return early when event has no fact_id."""
        handler, graph_linker, _ = self._make_handler()
        event = Event(
            type="fact_learned",
            agent_id="test-agent",
            data={"content": "some fact"},
        )

        await handler.handle(event)
        graph_linker.db.session.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_fact_id_invalid(self):
        """Should return early when fact_id is not a valid UUID."""
        handler, graph_linker, _ = self._make_handler()
        event = Event(
            type="fact_learned",
            agent_id="test-agent",
            data={"fact_id": "not-a-uuid", "content": "some fact"},
        )

        await handler.handle(event)
        graph_linker.db.session.assert_not_called()

    @pytest.mark.asyncio
    async def test_calls_link_fact_to_decisions(self):
        """Should call graph_linker.link_fact_to_decisions with correct args."""
        graph_linker = AsyncMock(spec=GraphLinker)
        mock_session = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        graph_linker.db.session.return_value = mock_cm

        fact_id = uuid4()
        edge = GraphEdgeInfo(
            source_id=fact_id,
            target_id=uuid4(),
            source_type="fact",
            target_type="decision",
            relation="evidence_for",
            weight=0.85,
            auto_linked=True,
        )
        graph_linker.link_fact_to_decisions = AsyncMock(return_value=[edge])

        handler, _, _ = self._make_handler(graph_linker=graph_linker)
        event = _make_event(fact_id=fact_id, content="PostgreSQL uses MVCC")

        await handler.handle(event)

        graph_linker.link_fact_to_decisions.assert_called_once_with(
            fact_id=fact_id,
            fact_content="PostgreSQL uses MVCC",
            session=mock_session,
        )
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_commit_when_no_edges(self):
        """Should not commit when link_fact_to_decisions returns empty list."""
        graph_linker = AsyncMock(spec=GraphLinker)
        mock_session = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        graph_linker.db.session.return_value = mock_cm
        graph_linker.link_fact_to_decisions = AsyncMock(return_value=[])

        handler, _, _ = self._make_handler(graph_linker=graph_linker)
        await handler.handle(_make_event())

        mock_session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_error_isolation(self):
        """GraphLinker failures should not propagate."""
        graph_linker = AsyncMock(spec=GraphLinker)
        mock_session = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        graph_linker.db.session.return_value = mock_cm
        graph_linker.link_fact_to_decisions = AsyncMock(
            side_effect=Exception("embedding service down")
        )

        handler, _, _ = self._make_handler(graph_linker=graph_linker)

        # Should NOT raise
        await handler.handle(_make_event())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fact_graph_linker.py::TestFactGraphLinker -v`
Expected: FAIL — `nous.handlers.fact_graph_linker` module not found

- [ ] **Step 3: Create the handler**

Create `nous/handlers/fact_graph_linker.py`:

```python
"""Fact Graph Linker — cross-type linking on fact creation.

Listens to: fact_learned (in-process EventBus)
Calls: GraphLinker.link_fact_to_decisions()

F022 Phase 2: Wires the missing fact->decision cross-type edge creation.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from nous.brain.graph_linker import GraphLinker
from nous.config import Settings
from nous.events import Event, EventBus

logger = logging.getLogger(__name__)


class FactGraphLinker:
    """Links newly learned facts to related decisions via embedding similarity.

    Subscribes to fact_learned events on the in-process EventBus.
    Calls GraphLinker.link_fact_to_decisions() in an isolated DB session
    so failures never affect the originating fact creation transaction.
    """

    def __init__(
        self,
        graph_linker: GraphLinker,
        settings: Settings,
        bus: EventBus,
    ) -> None:
        self._graph_linker = graph_linker
        self._settings = settings
        bus.on("fact_learned", self.handle)

    async def handle(self, event: Event) -> None:
        """Handle fact_learned — link fact to related decisions."""
        if not self._settings.cross_type_linking_enabled:
            return

        fact_id_str = event.data.get("fact_id")
        fact_content = event.data.get("content", "")
        if not fact_id_str or not fact_content:
            return

        try:
            fact_id = UUID(fact_id_str)
        except ValueError:
            logger.debug("F022: Invalid fact_id in fact_learned event: %s", fact_id_str)
            return

        try:
            async with self._graph_linker.db.session() as link_session:
                edges = await self._graph_linker.link_fact_to_decisions(
                    fact_id=fact_id,
                    fact_content=fact_content,
                    session=link_session,
                )
                if edges:
                    await link_session.commit()
                    logger.debug(
                        "F022: Linked fact %s to %d decisions via cross-type embedding",
                        fact_id, len(edges),
                    )
        except asyncio.CancelledError:
            raise  # Let the EventBus handle cancellation for clean shutdown
        except Exception:
            logger.debug("F022 fact->decision graph linking failed for fact %s", fact_id_str)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_fact_graph_linker.py::TestFactGraphLinker -v`
Expected: PASS (all 8 tests)

- [ ] **Step 5: Commit**

```bash
git add nous/handlers/fact_graph_linker.py tests/test_fact_graph_linker.py
git commit -m "feat(f022): add FactGraphLinker handler for fact-to-decision linking

New handler subscribes to fact_learned on the EventBus and calls
GraphLinker.link_fact_to_decisions() in an isolated DB session.
Error-isolated: graph linking failures never affect fact creation.
Self-gated by settings.cross_type_linking_enabled."
```

---

### Task 3: Wire Handler in main.py

**Files:**
- Modify: `nous/main.py:104-125` (hoist GraphLinker, add FactGraphLinker, inject bus into Heart)

- [ ] **Step 1: Modify main.py to wire the handler**

In `nous/main.py`, replace the EpisodeSummarizer block (lines 105-116) and FactExtractor block (lines 118-124). **Keep the KnowledgeExtractor block (lines 126-132) unchanged.** Replace with:

```python
        # F022: Create GraphLinker (shared between episode and fact linking)
        graph_linker = None
        try:
            from nous.brain.graph_linker import GraphLinker

            if settings.cross_type_linking_enabled or settings.episode_summary_enabled:
                graph_linker = GraphLinker(
                    db=database, embedder=embedding_provider,
                    settings=settings, agent_id=settings.agent_id,
                )
        except ImportError:
            logger.debug("GraphLinker not available yet")

        try:
            from nous.handlers.episode_summarizer import EpisodeSummarizer

            if settings.episode_summary_enabled:
                EpisodeSummarizer(heart, brain, settings, bus, handler_http, graph_linker=graph_linker)
        except ImportError:
            logger.debug("EpisodeSummarizer not available yet")

        try:
            from nous.handlers.fact_extractor import FactExtractor

            if settings.fact_extraction_enabled:
                FactExtractor(heart, settings, bus, handler_http)
        except ImportError:
            logger.debug("FactExtractor not available yet")

        # F022 Phase 2: Wire fact->decision graph linking
        try:
            from nous.handlers.fact_graph_linker import FactGraphLinker

            if graph_linker is not None and settings.cross_type_linking_enabled:
                heart._bus = bus  # Inject bus for fact_learned emission
                FactGraphLinker(graph_linker, settings, bus)
                logger.debug("F022: FactGraphLinker wired — fact->decision linking enabled")
        except ImportError:
            logger.debug("FactGraphLinker not available yet")
```

**Key changes:**
1. `GraphLinker` construction hoisted above `EpisodeSummarizer` (was inside the episode block)
2. Construction condition: `cross_type_linking_enabled OR episode_summary_enabled` (covers both use cases)
3. `heart._bus = bus` injection gated on `cross_type_linking_enabled` specifically (not just graph_linker existence)
4. `FactGraphLinker` registered with shared `graph_linker` instance
5. **KnowledgeExtractor block (lines 126-132) remains unchanged** — do not touch it

- [ ] **Step 2: Run full test suite to verify no regressions**

Run: `uv run pytest tests/test_fact_graph_linker.py -v`
Expected: PASS (all tests)

Run: `uv run pytest tests/test_event_bus.py -v`
Expected: PASS (existing handler tests unaffected)

- [ ] **Step 3: Commit**

```bash
git add nous/main.py
git commit -m "feat(f022): wire FactGraphLinker in main.py

Hoist GraphLinker construction above EpisodeSummarizer so it can be
shared with FactGraphLinker. Inject EventBus into Heart._bus for
fact_learned emission. Register FactGraphLinker handler when
cross_type_linking_enabled or episode_summary_enabled."
```

---

### Task 4: Verify End-to-End (Manual Smoke Test)

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest tests/ -v --tb=short -q`
Expected: All existing tests pass, no regressions

- [ ] **Step 2: Verify the data flow conceptually**

The complete flow is now:

```
Heart.learn(FactInput)
  → FactManager._learn() — creates fact row, emits DB audit event
  → Heart.learn() emits bus Event("fact_learned", {fact_id, content, ...})
  → [async] EventBus dispatches to FactGraphLinker.handle()
    → GraphLinker.link_fact_to_decisions(fact_id, content, session)
      → embeds "fact: {content}" via common template
      → queries decisions by cosine similarity
      → re-embeds candidates with common template for fair comparison
      → creates "evidence_for" edges in brain.graph_edges
    → commit (only if edges found)
```

- [ ] **Step 3: Final commit with all files**

Verify all changes are committed:

```bash
git log --oneline -5
git status
```

---

## Summary

| Change | File | LOC |
|--------|------|-----|
| Add `_bus` attribute + bus emission | `nous/heart/heart.py` | ~15 |
| New FactGraphLinker handler | `nous/handlers/fact_graph_linker.py` | ~55 |
| Wire handler in main.py | `nous/main.py` | ~20 (net) |
| Tests | `tests/test_fact_graph_linker.py` | ~220 |
| **Total** | **4 files** | **~290** |

**No schema changes, no new config, no new dependencies.** Uses existing `GraphLinker.link_fact_to_decisions()`, existing `settings.cross_type_linking_enabled` flag, and existing `EventBus` infrastructure.
