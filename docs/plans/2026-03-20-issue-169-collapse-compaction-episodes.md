# Issue #169: Collapse Compaction Episodes Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate graph pollution by keeping the existing episode open during compaction instead of creating a new one.

**Architecture:** Replace the end-episode + start-episode cycle in `pre_compaction()` with a no-op on the episode boundary. The existing episode stays open and continues accumulating decisions/facts. A `compaction_count` field on the episode tracks how many compactions occurred during its lifetime, exposed via `EpisodeDetail` for observability.

**Tech Stack:** Python, SQLAlchemy async, PostgreSQL, pytest

**Review notes:**
- Embedding staleness: episode embedding reflects initial summary until `end_session` triggers `EpisodeSummarizer`. Acceptable — `update_summary()` handles refresh at session end. Documented in docstring.
- `episode_completed` event no longer fires at compaction boundaries. `KnowledgeExtractor` already subscribes to `conversation_compacting` (which still fires). `EpisodeSummarizer` only runs at session end. No handler impact.
- `list_recent()` filters `ended_at IS NOT NULL` — active episodes were always hidden. Not a regression.
- Existing compaction edges cleaned up in migration.

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `nous/cognitive/layer.py:993-1046` | Replace end+start episode cycle with compaction_count bump |
| Modify | `nous/heart/episodes.py` | Add `bump_compaction_count()` method |
| Modify | `nous/heart/heart.py` | Expose `bump_episode_compaction_count()` |
| Modify | `nous/heart/schemas.py:38-61` | Add `compaction_count` to `EpisodeDetail` |
| Modify | `nous/storage/models.py:306-336` | Add `compaction_count` column to Episode ORM |
| Create | `sql/migrations/017_episode_compaction_count.sql` | Migration: new column + cleanup old edges |
| Modify | `tests/test_compaction_phase3.py:686-835` | Update 5 episode boundary tests |
| Create | `tests/test_episode_compaction_collapse.py` | New tests for collapse behavior |

## Chunk 1: Database + Episode Manager

### Task 1: Add `compaction_count` column to Episode ORM

**Files:**
- Modify: `nous/storage/models.py:306-336`
- Create: `sql/migrations/017_episode_compaction_count.sql`

- [ ] **Step 1: Write migration SQL**

```sql
-- 017_episode_compaction_count.sql
-- Issue #169: Track compaction count per episode instead of creating new episodes

-- 1. Add compaction_count column
ALTER TABLE heart.episodes ADD COLUMN IF NOT EXISTS compaction_count INTEGER NOT NULL DEFAULT 0;
COMMENT ON COLUMN heart.episodes.compaction_count IS 'Number of conversation compactions during this episode lifetime';

-- 2. Clean up existing compaction pollution edges.
-- These are episode→episode edges auto-created by cross-type linking between
-- compaction stub episodes (all had identical "Continuation after conversation
-- compaction" summaries, so cross-type linking connected them all).
DELETE FROM brain.memory_edges
WHERE source_type = 'episode'
  AND target_type = 'episode'
  AND source_id IN (
    SELECT id FROM heart.episodes WHERE trigger = 'compaction'
  );

-- 3. Deactivate orphaned compaction stub episodes (they have no decisions/facts)
UPDATE heart.episodes
SET active = false
WHERE trigger = 'compaction'
  AND summary = 'Continuation after conversation compaction'
  AND id NOT IN (SELECT episode_id FROM heart.episode_decisions);
```

- [ ] **Step 2: Add column to ORM model**

In `nous/storage/models.py`, add after the `compression_tier` line (~336):

```python
    compaction_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
```

- [ ] **Step 3: Commit**

```bash
git add sql/migrations/017_episode_compaction_count.sql nous/storage/models.py
git commit -m "fix(models): add compaction_count column to episodes (#169)"
```

### Task 2: Add `bump_compaction_count()` to EpisodeManager and expose in schemas

**Files:**
- Modify: `nous/heart/episodes.py`
- Modify: `nous/heart/heart.py`
- Modify: `nous/heart/schemas.py:38-61`
- Create: `tests/test_episode_compaction_collapse.py`

- [ ] **Step 1: Write failing test for bump_compaction_count**

Create `tests/test_episode_compaction_collapse.py`:

```python
"""Tests for issue #169: Collapse compaction episodes."""

import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from nous.heart.episodes import EpisodeManager


@pytest.mark.asyncio
async def test_bump_compaction_count_increments():
    """bump_compaction_count increments the counter on the episode."""
    db = MagicMock()
    episode_id = uuid4()

    # Mock the episode ORM object
    mock_episode = MagicMock()
    mock_episode.compaction_count = 0

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_episode

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.flush = AsyncMock()

    db.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    db.session.return_value.__aexit__ = AsyncMock(return_value=False)

    manager = EpisodeManager(db=db, embeddings=None, agent_id="test")

    await manager.bump_compaction_count(episode_id)

    assert mock_episode.compaction_count == 1
    mock_session.flush.assert_called_once()


@pytest.mark.asyncio
async def test_bump_compaction_count_increments_existing():
    """bump_compaction_count increments from existing count."""
    db = MagicMock()
    episode_id = uuid4()

    mock_episode = MagicMock()
    mock_episode.compaction_count = 3

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_episode

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.flush = AsyncMock()

    db.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    db.session.return_value.__aexit__ = AsyncMock(return_value=False)

    manager = EpisodeManager(db=db, embeddings=None, agent_id="test")

    await manager.bump_compaction_count(episode_id)

    assert mock_episode.compaction_count == 4


@pytest.mark.asyncio
async def test_bump_compaction_count_missing_episode():
    """bump_compaction_count raises ValueError for missing episode."""
    db = MagicMock()
    episode_id = uuid4()

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    db.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    db.session.return_value.__aexit__ = AsyncMock(return_value=False)

    manager = EpisodeManager(db=db, embeddings=None, agent_id="test")

    with pytest.raises(ValueError, match="not found"):
        await manager.bump_compaction_count(episode_id)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_episode_compaction_collapse.py -v`
Expected: FAIL with `AttributeError: 'EpisodeManager' object has no attribute 'bump_compaction_count'`

- [ ] **Step 3: Implement bump_compaction_count in EpisodeManager**

In `nous/heart/episodes.py`, add after the `update_summary` methods (~line 160):

```python
    # ------------------------------------------------------------------
    # bump_compaction_count()
    # ------------------------------------------------------------------

    async def bump_compaction_count(
        self, episode_id: UUID, session: AsyncSession | None = None
    ) -> None:
        """Increment compaction counter on an active episode.

        Note: Episode embedding is NOT refreshed here — it still reflects the
        initial session summary. EpisodeSummarizer refreshes it at session end
        via update_summary(). If the session ends abnormally, the embedding
        may be stale.
        """
        if session is None:
            async with self.db.session() as session:
                await self._bump_compaction_count(episode_id, session)
                await session.commit()
                return
        await self._bump_compaction_count(episode_id, session)

    async def _bump_compaction_count(
        self, episode_id: UUID, session: AsyncSession
    ) -> None:
        stmt = select(Episode).where(Episode.id == episode_id)
        result = await session.execute(stmt)
        episode = result.scalar_one_or_none()
        if episode is None:
            raise ValueError(f"Episode {episode_id} not found")
        episode.compaction_count = (episode.compaction_count or 0) + 1
        await session.flush()
```

- [ ] **Step 4: Add `compaction_count` to EpisodeDetail schema**

In `nous/heart/schemas.py`, add to `EpisodeDetail` class after `created_at`:

```python
    compaction_count: int = 0
```

- [ ] **Step 5: Add `compaction_count` to `_to_detail()` mapping**

In `nous/heart/episodes.py`, update `_to_detail()` (~line 573-598) to include:

```python
            compaction_count=episode.compaction_count or 0,
```

after the `created_at=episode.created_at,` line.

- [ ] **Step 6: Expose via Heart facade**

In `nous/heart/heart.py`, add after `update_episode_summary` method:

```python
    async def bump_episode_compaction_count(
        self, episode_id: UUID, session: AsyncSession | None = None
    ) -> None:
        """Increment compaction counter on episode."""
        await self.episodes.bump_compaction_count(episode_id, session=session)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_episode_compaction_collapse.py -v`
Expected: All 3 PASS

- [ ] **Step 8: Commit**

```bash
git add nous/heart/episodes.py nous/heart/heart.py nous/heart/schemas.py tests/test_episode_compaction_collapse.py
git commit -m "feat(heart): add bump_compaction_count for episode compaction collapse (#169)"
```

## Chunk 2: Cognitive Layer — Replace Episode Boundary

### Task 3: Modify `pre_compaction()` to keep episode open

**Files:**
- Modify: `nous/cognitive/layer.py:993-1046`
- Modify: `tests/test_compaction_phase3.py:686-835`
- Modify: `tests/test_episode_compaction_collapse.py`

- [ ] **Step 1: Write failing test for new pre_compaction behavior**

Add to `tests/test_episode_compaction_collapse.py`:

```python
from nous.cognitive.layer import CognitiveLayer
from nous.heart.schemas import EpisodeInput


def _mock_settings():
    """Create minimal mock settings for CognitiveLayer."""
    s = MagicMock()
    s.NOUS_AGENT_ID = "test-agent"
    s.NOUS_AGENT_NAME = "Test"
    s.NOUS_MODEL = "claude-sonnet-4-6"
    s.NOUS_CONTEXT_WINDOW = 0
    s.NOUS_ANTI_HALLUCINATION_PROMPT = False
    s.NOUS_RELEVANCE_FLOOR_ENABLED = True
    s.NOUS_RELEVANCE_DROP_RATIO = 0.6
    s.NOUS_BUDGET_SCALE_ENABLED = True
    s.NOUS_CONTEXT_BUDGET_OVERRIDES = {}
    s.NOUS_STALENESS_PENALTY_ENABLED = False
    s.NOUS_STALENESS_HALF_LIFE_DAYS = 14
    s.NOUS_GRAPH_RECALL_ENABLED = False
    s.NOUS_SPREADING_ACTIVATION_ENABLED = "false"
    s.NOUS_CROSS_TYPE_LINKING_ENABLED = False
    s.NOUS_TOOL_PRUNING_ENABLED = False
    s.NOUS_COMPACTION_ENABLED = True
    s.NOUS_COMPACTION_THRESHOLD = 50000
    s.NOUS_KEEP_RECENT_TOKENS = 20000
    return s


TEST_AGENT = "test-agent"
TEST_SESSION = "test-session"


@pytest.mark.asyncio
async def test_pre_compaction_keeps_episode_open():
    """pre_compaction does NOT end the episode — it bumps compaction count."""
    brain = MagicMock()
    brain.db = MagicMock()
    brain.embeddings = MagicMock()
    heart = MagicMock()
    heart.end_episode = AsyncMock()
    heart.start_episode = AsyncMock()
    heart.bump_episode_compaction_count = AsyncMock()
    settings = _mock_settings()

    cognitive = CognitiveLayer(brain, heart, settings, bus=None)
    old_episode_id = str(uuid4())
    cognitive._active_episodes[TEST_SESSION] = old_episode_id

    await cognitive.pre_compaction(
        agent_id=TEST_AGENT,
        session_id=TEST_SESSION,
        message_snapshot=[{"role": "user", "content": "test"}],
    )

    # Episode stays open — NOT ended, NOT replaced
    heart.end_episode.assert_not_called()
    heart.start_episode.assert_not_called()

    # Compaction count bumped
    heart.bump_episode_compaction_count.assert_called_once()

    # Same episode ID retained
    assert cognitive._active_episodes[TEST_SESSION] == old_episode_id


@pytest.mark.asyncio
async def test_pre_compaction_no_episode_no_error():
    """pre_compaction with no active episode doesn't error or create one."""
    brain = MagicMock()
    brain.db = MagicMock()
    brain.embeddings = MagicMock()
    heart = MagicMock()
    heart.bump_episode_compaction_count = AsyncMock()
    settings = _mock_settings()

    cognitive = CognitiveLayer(brain, heart, settings, bus=None)

    await cognitive.pre_compaction(
        agent_id=TEST_AGENT,
        session_id=TEST_SESSION,
        message_snapshot=[{"role": "user", "content": "test"}],
    )

    heart.bump_episode_compaction_count.assert_not_called()


@pytest.mark.asyncio
async def test_pre_compaction_bump_failure_non_fatal():
    """If bump_compaction_count fails, episode stays active and no error raised."""
    brain = MagicMock()
    brain.db = MagicMock()
    brain.embeddings = MagicMock()
    heart = MagicMock()
    heart.bump_episode_compaction_count = AsyncMock(side_effect=RuntimeError("DB error"))
    settings = _mock_settings()

    cognitive = CognitiveLayer(brain, heart, settings, bus=None)
    old_episode_id = str(uuid4())
    cognitive._active_episodes[TEST_SESSION] = old_episode_id

    # Should not raise
    await cognitive.pre_compaction(
        agent_id=TEST_AGENT,
        session_id=TEST_SESSION,
        message_snapshot=[],
    )

    # Episode stays open even on failure
    assert cognitive._active_episodes[TEST_SESSION] == old_episode_id


@pytest.mark.asyncio
async def test_pre_compaction_still_emits_event():
    """pre_compaction still emits conversation_compacting event."""
    brain = MagicMock()
    brain.db = MagicMock()
    brain.embeddings = MagicMock()
    heart = MagicMock()
    heart.bump_episode_compaction_count = AsyncMock()
    settings = _mock_settings()
    bus = MagicMock()
    bus.emit = AsyncMock()

    cognitive = CognitiveLayer(brain, heart, settings, bus=bus)
    cognitive._active_episodes[TEST_SESSION] = str(uuid4())

    snapshot = [{"role": "user", "content": "test"}]
    await cognitive.pre_compaction(
        agent_id=TEST_AGENT,
        session_id=TEST_SESSION,
        message_snapshot=snapshot,
    )

    bus.emit.assert_called_once()
    event = bus.emit.call_args[0][0]
    assert event.type == "conversation_compacting"
    assert event.data["message_snapshot"] == snapshot
```

- [ ] **Step 2: Run new tests to verify they fail**

Run: `uv run pytest tests/test_episode_compaction_collapse.py::test_pre_compaction_keeps_episode_open -v`
Expected: FAIL (end_episode still called in current code)

- [ ] **Step 3: Replace pre_compaction episode boundary logic**

In `nous/cognitive/layer.py`, replace the `pre_compaction` method (lines 993-1046) with:

```python
    async def pre_compaction(
        self,
        agent_id: str,
        session_id: str,
        message_snapshot: list[dict[str, Any]],
    ) -> None:
        """Emit pre-compaction event and bump episode compaction count.

        Called by runner BEFORE compact() mutates the conversation.
        The message_snapshot is a copy of messages[:cut_point], decoupled
        from mutation timing so handlers can safely process it.

        Issue #169: Instead of ending the current episode and starting a new
        one (which polluted the graph with generic edges), we keep the
        episode open and increment its compaction_count.
        """
        # 1. Episode — keep open, bump compaction count
        episode_id = self._active_episodes.get(session_id)
        if episode_id:
            try:
                await self._heart.bump_episode_compaction_count(UUID(episode_id))
                logger.debug("Bumped compaction count on episode %s", episode_id)
            except Exception:
                logger.warning("Failed to bump compaction count on episode %s", episode_id)

        # 2. Emit event — handlers get the snapshot, not live state
        if self._bus:
            await self._bus.emit(Event(
                type="conversation_compacting",
                agent_id=agent_id,
                session_id=session_id,
                data={"message_snapshot": message_snapshot},
            ))
```

- [ ] **Step 4: Run all new tests**

Run: `uv run pytest tests/test_episode_compaction_collapse.py -v`
Expected: All 7 PASS

- [ ] **Step 5: Update old compaction tests**

In `tests/test_compaction_phase3.py`, update the 5 episode boundary tests (lines 686-835) to match new behavior:

- `test_episode_ended_on_compaction` → rename to `test_episode_kept_open_on_compaction`: verify end_episode NOT called, bump_episode_compaction_count IS called
- `test_new_episode_started_after_compaction` → rename to `test_no_new_episode_on_compaction`: verify start_episode NOT called, _active_episodes unchanged
- `test_no_active_episode_no_error` → keep as-is (behavior unchanged), add `heart.bump_episode_compaction_count = AsyncMock()` to mock setup
- `test_active_episodes_dict_updated` → rename to `test_active_episodes_dict_unchanged`: verify same episode ID retained
- `test_end_episode_failure_does_not_block_start` → rename to `test_bump_failure_non_fatal`: verify episode stays active

Updated tests:

```python
    @pytest.mark.asyncio
    async def test_episode_kept_open_on_compaction(self):
        """Active episode stays open when pre_compaction is called (#169)."""
        from nous.cognitive.layer import CognitiveLayer

        brain = MagicMock()
        brain.db = MagicMock()
        brain.embeddings = MagicMock()
        heart = MagicMock()
        heart.end_episode = AsyncMock()
        heart.start_episode = AsyncMock()
        heart.bump_episode_compaction_count = AsyncMock()
        settings = _mock_settings()

        cognitive = CognitiveLayer(brain, heart, settings, bus=None)
        old_episode_id = str(uuid4())
        cognitive._active_episodes[TEST_SESSION] = old_episode_id

        await cognitive.pre_compaction(
            agent_id=TEST_AGENT,
            session_id=TEST_SESSION,
            message_snapshot=[{"role": "user", "content": "test"}],
        )

        # Episode stays open
        heart.end_episode.assert_not_called()
        heart.start_episode.assert_not_called()
        heart.bump_episode_compaction_count.assert_called_once()
        assert cognitive._active_episodes[TEST_SESSION] == old_episode_id

    @pytest.mark.asyncio
    async def test_no_new_episode_on_compaction(self):
        """No new episode created after compaction (#169)."""
        from nous.cognitive.layer import CognitiveLayer

        brain = MagicMock()
        brain.db = MagicMock()
        brain.embeddings = MagicMock()
        heart = MagicMock()
        heart.end_episode = AsyncMock()
        heart.start_episode = AsyncMock()
        heart.bump_episode_compaction_count = AsyncMock()
        settings = _mock_settings()

        cognitive = CognitiveLayer(brain, heart, settings, bus=None)
        cognitive._active_episodes[TEST_SESSION] = str(uuid4())

        await cognitive.pre_compaction(
            agent_id=TEST_AGENT,
            session_id=TEST_SESSION,
            message_snapshot=[{"role": "user", "content": "test"}],
        )

        heart.start_episode.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_active_episode_no_error(self):
        """pre_compaction with no active episode doesn't error."""
        from nous.cognitive.layer import CognitiveLayer

        brain = MagicMock()
        brain.db = MagicMock()
        brain.embeddings = MagicMock()
        heart = MagicMock()
        heart.bump_episode_compaction_count = AsyncMock()
        settings = _mock_settings()

        cognitive = CognitiveLayer(brain, heart, settings, bus=None)

        await cognitive.pre_compaction(
            agent_id=TEST_AGENT,
            session_id=TEST_SESSION,
            message_snapshot=[{"role": "user", "content": "test"}],
        )

        heart.bump_episode_compaction_count.assert_not_called()

    @pytest.mark.asyncio
    async def test_active_episodes_dict_unchanged(self):
        """_active_episodes keeps the same episode ID after compaction (#169)."""
        from nous.cognitive.layer import CognitiveLayer

        brain = MagicMock()
        brain.db = MagicMock()
        brain.embeddings = MagicMock()
        heart = MagicMock()
        heart.bump_episode_compaction_count = AsyncMock()
        settings = _mock_settings()

        cognitive = CognitiveLayer(brain, heart, settings, bus=None)
        old_id = uuid4()
        cognitive._active_episodes[TEST_SESSION] = str(old_id)

        await cognitive.pre_compaction(
            agent_id=TEST_AGENT,
            session_id=TEST_SESSION,
            message_snapshot=[],
        )

        assert cognitive._active_episodes[TEST_SESSION] == str(old_id)

    @pytest.mark.asyncio
    async def test_bump_failure_non_fatal(self):
        """If bump_compaction_count fails, episode stays active."""
        from nous.cognitive.layer import CognitiveLayer

        brain = MagicMock()
        brain.db = MagicMock()
        brain.embeddings = MagicMock()
        heart = MagicMock()
        heart.bump_episode_compaction_count = AsyncMock(side_effect=RuntimeError("DB error"))
        settings = _mock_settings()

        cognitive = CognitiveLayer(brain, heart, settings, bus=None)
        old_id = str(uuid4())
        cognitive._active_episodes[TEST_SESSION] = old_id

        await cognitive.pre_compaction(
            agent_id=TEST_AGENT,
            session_id=TEST_SESSION,
            message_snapshot=[],
        )

        assert cognitive._active_episodes[TEST_SESSION] == old_id
```

- [ ] **Step 6: Run all affected tests**

Run: `uv run pytest tests/test_compaction_phase3.py tests/test_episode_compaction_collapse.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add nous/cognitive/layer.py tests/test_compaction_phase3.py tests/test_episode_compaction_collapse.py
git commit -m "fix(cognitive): collapse compaction episodes instead of creating new ones (#169)

Replaces the end+start episode cycle in pre_compaction() with a
compaction_count bump. The existing episode stays open, eliminating
generic 'Continuation after conversation compaction' edges that
polluted the knowledge graph."
```

### Task 4: Run full test suite

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest tests/ -v --timeout=60`
Expected: All tests pass. Watch for any tests that depend on the old end+start behavior.

- [ ] **Step 2: Commit if any fixups needed**
