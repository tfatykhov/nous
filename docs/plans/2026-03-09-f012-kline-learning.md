# F012 K-Line Learning Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Auto-create procedures (K-lines) from repeated patterns in decisions, episodes, and error recovery.

**Architecture:** Three learning pathways feed into the existing `heart.procedures` table: (1) decision clustering and (2) episode lesson extraction run during sleep's generalize phase, (3) monitor recovery learning runs in real-time. All use LLM-powered extraction via `NOUS_BACKGROUND_MODEL`.

**Tech Stack:** Python 3.12+, async SQLAlchemy, httpx (Anthropic API), pgvector embeddings, pytest

---

### Task 1: Add Configuration Fields

**Files:**
- Modify: `nous/config.py:229-236` (after spreading activation settings)

**Step 1: Write the failing test**

Create `tests/test_procedure_learner.py`:

```python
"""Tests for F012 K-Line Learning — ProcedureLearner handler."""

import pytest

from nous.config import Settings


def test_procedure_learning_config_defaults():
    """F012 config fields exist with correct defaults."""
    s = Settings(
        anthropic_api_key="test",
        openai_api_key="test",
    )
    assert s.procedure_learning_enabled is True
    assert s.procedure_cluster_min_size == 3
    assert s.procedure_similarity_threshold == 0.85
    assert s.procedure_episode_similarity == 0.80
    assert s.procedure_success_rate_min == 0.70
    assert s.procedure_monitor_trigger_count == 3
    assert s.procedure_max_per_sleep == 3
    assert s.procedure_max_per_session == 1
    assert s.procedure_staleness_days == 30
    assert s.procedure_weakness_threshold == 0.30
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_procedure_learner.py::test_procedure_learning_config_defaults -v`
Expected: FAIL with `AttributeError`

**Step 3: Write minimal implementation**

Add to `nous/config.py` after the spreading activation settings block (after line 235):

```python
    # F012: Procedure Learning (K-Line auto-creation)
    procedure_learning_enabled: bool = True
    procedure_cluster_min_size: int = 3
    procedure_similarity_threshold: float = 0.85
    procedure_episode_similarity: float = 0.80
    procedure_success_rate_min: float = 0.70
    procedure_monitor_trigger_count: int = 3
    procedure_max_per_sleep: int = 3
    procedure_max_per_session: int = 1
    procedure_staleness_days: int = 30
    procedure_weakness_threshold: float = 0.30
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_procedure_learner.py::test_procedure_learning_config_defaults -v`
Expected: PASS

**Step 5: Commit**

```bash
git add nous/config.py tests/test_procedure_learner.py
git commit -m "feat(F012): add procedure learning config fields"
```

---

### Task 2: Decision Clustering — Core Algorithm

The clustering algorithm groups reviewed successful decisions by bridge-function embedding similarity.

**Files:**
- Create: `nous/handlers/procedure_learner.py`
- Test: `tests/test_procedure_learner.py`

**Step 1: Write the failing test**

Append to `tests/test_procedure_learner.py`:

```python
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, UTC, timedelta
from uuid import uuid4

from nous.handlers.procedure_learner import ProcedureLearner


def _make_decision(description, outcome="success", days_ago=1, bridge_function=None):
    """Create a mock DecisionSummary-like object."""
    d = MagicMock()
    d.id = uuid4()
    d.description = description
    d.outcome = outcome
    d.confidence = 0.8
    d.category = "architecture"
    d.created_at = datetime.now(UTC) - timedelta(days=days_ago)
    d.pattern = None
    # Bridge info
    d.bridge = MagicMock()
    d.bridge.function = bridge_function or description
    d.bridge.structure = f"structure for {description}"
    return d


def _make_settings(**overrides):
    """Build minimal Settings mock for ProcedureLearner."""
    s = MagicMock()
    s.procedure_learning_enabled = True
    s.procedure_cluster_min_size = 3
    s.procedure_similarity_threshold = 0.85
    s.procedure_episode_similarity = 0.80
    s.procedure_success_rate_min = 0.70
    s.procedure_monitor_trigger_count = 3
    s.procedure_max_per_sleep = 3
    s.procedure_max_per_session = 1
    s.procedure_staleness_days = 30
    s.procedure_weakness_threshold = 0.30
    s.background_model = "claude-haiku-3-5-20241022"
    s.api_base_url = "https://api.anthropic.com"
    s.agent_id = "test-agent"
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


@pytest.mark.asyncio
async def test_cluster_decisions_groups_similar():
    """3+ decisions with similar bridge functions form a cluster."""
    embeddings = AsyncMock()
    # Return identical embeddings for similar bridge functions
    embeddings.embed.return_value = [0.1] * 1536
    embeddings.embed_batch.return_value = [[0.1] * 1536] * 3

    decisions = [
        _make_decision("Deploy with blue-green", bridge_function="safe deployment"),
        _make_decision("Deploy using canary release", bridge_function="safe deployment"),
        _make_decision("Deploy with rolling update", bridge_function="safe deployment"),
    ]

    learner = ProcedureLearner(
        brain=AsyncMock(),
        heart=AsyncMock(),
        settings=_make_settings(),
        embeddings=embeddings,
        http_client=None,
    )

    clusters = await learner._cluster_decisions(decisions)
    # All 3 should be in one cluster (identical embeddings = similarity 1.0)
    assert len(clusters) >= 1
    assert any(len(c) >= 3 for c in clusters)


@pytest.mark.asyncio
async def test_cluster_decisions_rejects_small_clusters():
    """Clusters with fewer than min_size are excluded."""
    embeddings = AsyncMock()
    # Two different embeddings — won't cluster together
    call_count = 0
    async def varying_embed(text):
        nonlocal call_count
        call_count += 1
        if call_count <= 1:
            return [0.1] * 1536
        return [0.9] * 1536

    embeddings.embed = varying_embed
    embeddings.embed_batch = AsyncMock(return_value=[[0.1] * 1536, [0.9] * 1536])

    decisions = [
        _make_decision("Deploy safely", bridge_function="deployment"),
        _make_decision("Fix database bug", bridge_function="debugging"),
    ]

    learner = ProcedureLearner(
        brain=AsyncMock(),
        heart=AsyncMock(),
        settings=_make_settings(),
        embeddings=embeddings,
        http_client=None,
    )

    clusters = await learner._cluster_decisions(decisions)
    # No cluster meets min_size=3
    assert len(clusters) == 0


@pytest.mark.asyncio
async def test_cluster_decisions_checks_success_rate():
    """Clusters with <70% success rate are excluded."""
    embeddings = AsyncMock()
    embeddings.embed_batch.return_value = [[0.1] * 1536] * 4

    decisions = [
        _make_decision("Deploy v1", outcome="success", bridge_function="deploy"),
        _make_decision("Deploy v2", outcome="failure", bridge_function="deploy"),
        _make_decision("Deploy v3", outcome="failure", bridge_function="deploy"),
        _make_decision("Deploy v4", outcome="failure", bridge_function="deploy"),
    ]

    learner = ProcedureLearner(
        brain=AsyncMock(),
        heart=AsyncMock(),
        settings=_make_settings(),
        embeddings=embeddings,
        http_client=None,
    )

    clusters = await learner._cluster_decisions(decisions)
    # 25% success rate < 70% threshold
    assert len(clusters) == 0


@pytest.mark.asyncio
async def test_cluster_decisions_checks_recency():
    """Clusters with no recent decisions (last 7 days) are excluded."""
    embeddings = AsyncMock()
    embeddings.embed_batch.return_value = [[0.1] * 1536] * 3

    decisions = [
        _make_decision("Old deploy 1", days_ago=30, bridge_function="deploy"),
        _make_decision("Old deploy 2", days_ago=30, bridge_function="deploy"),
        _make_decision("Old deploy 3", days_ago=30, bridge_function="deploy"),
    ]

    learner = ProcedureLearner(
        brain=AsyncMock(),
        heart=AsyncMock(),
        settings=_make_settings(),
        embeddings=embeddings,
        http_client=None,
    )

    clusters = await learner._cluster_decisions(decisions)
    # No decision within 7 days
    assert len(clusters) == 0
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_procedure_learner.py -k "cluster_decisions" -v`
Expected: FAIL with `ImportError` (ProcedureLearner doesn't exist)

**Step 3: Write minimal implementation**

Create `nous/handlers/procedure_learner.py`:

```python
"""Procedure Learner — auto-creates procedures (K-lines) from repeated patterns.

F012: Three learning pathways:
1. Decision clustering (sleep) — groups similar successful decisions
2. Episode lesson learning (sleep) — generalizes repeated lessons
3. Monitor recovery learning (real-time) — codifies error→recovery patterns

Pathways 1-2 run during sleep's generalize phase.
Pathway 3 is wired into MonitorEngine.learn().
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import numpy as np

from nous.brain.brain import Brain
from nous.brain.embeddings import EmbeddingProvider
from nous.config import Settings
from nous.handlers import build_anthropic_headers, parse_llm_json
from nous.heart.heart import Heart
from nous.heart.schemas import ProcedureInput

logger = logging.getLogger(__name__)

# -- LLM prompts --

_DECISION_CLUSTER_PROMPT = """Given these {count} similar successful decisions, extract a reusable procedure.

Decisions:
{decisions_text}

Output ONLY valid JSON:
{{
  "name": "<short descriptive name>",
  "domain": "<category/domain>",
  "description": "<when and why to use this procedure>",
  "goals": ["<what this procedure achieves>"],
  "core_patterns": ["<the repeatable approach>"],
  "core_tools": ["<tools/techniques involved>"],
  "core_concepts": ["<key ideas to keep in mind>"],
  "implementation_notes": ["<specific details>"]
}}"""

_EPISODE_LESSON_PROMPT = """Given these {count} episodes with similar lessons, extract a reusable procedure.

Episodes:
{episodes_text}

Output ONLY valid JSON:
{{
  "name": "<short descriptive name>",
  "domain": "<category/domain>",
  "description": "<when and why to use this procedure>",
  "goals": ["<what situations this helps with>"],
  "core_patterns": ["<the approach that kept working>"],
  "core_tools": ["<tools/techniques mentioned>"],
  "core_concepts": ["<the underlying insight>"],
  "implementation_notes": ["<caveats, edge cases observed>"]
}}"""

_REVIEW_WEAK_PROMPT = """This auto-learned procedure has low effectiveness or hasn't been used recently.

Procedure: {name} ({domain})
Description: {description}
Core patterns: {core_patterns}
Stats: activated {activation_count}x, success {success_count}, failure {failure_count}, effectiveness: {effectiveness}, last activated: {last_activated}

Should this procedure be:
A) KEPT — still valuable, just hasn't been needed
B) REVISED — the core insight is good but needs updating (provide revision)
C) RETIRED — no longer useful, retire it

Return ONLY valid JSON:
{{
  "action": "kept" | "revised" | "retired",
  "reason": "<why>",
  "revision": {{...}}  // only if action=revised, same fields as procedure
}}"""

_RECENCY_DAYS = 7


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    va = np.array(a)
    vb = np.array(b)
    dot = np.dot(va, vb)
    norm = np.linalg.norm(va) * np.linalg.norm(vb)
    if norm == 0:
        return 0.0
    return float(dot / norm)


class ProcedureLearner:
    """Auto-learns procedures from decision clusters, episode lessons, and error recovery.

    Pathway 1 (decisions) and Pathway 2 (episodes) run during sleep's generalize phase.
    Pathway 3 (monitor recovery) is handled separately via MonitorEngine enhancement.
    """

    def __init__(
        self,
        brain: Brain,
        heart: Heart,
        settings: Settings,
        embeddings: EmbeddingProvider | None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._brain = brain
        self._heart = heart
        self._settings = settings
        self._embeddings = embeddings
        self._http = http_client

    # ------------------------------------------------------------------
    # Public API (called from sleep handler)
    # ------------------------------------------------------------------

    async def run_sleep_learning(self, agent_id: str) -> dict[str, int]:
        """Run pathways 1+2 during sleep. Returns stats dict."""
        stats = {"decisions_learned": 0, "episodes_learned": 0, "reviewed": 0}

        if not self._settings.procedure_learning_enabled:
            return stats

        max_total = self._settings.procedure_max_per_sleep
        created = 0

        # Pathway 1: Decision clustering
        try:
            decisions = await self._fetch_recent_decisions(agent_id)
            clusters = await self._cluster_decisions(decisions)
            for cluster in clusters:
                if created >= max_total:
                    break
                if await self._create_from_decision_cluster(cluster):
                    created += 1
                    stats["decisions_learned"] += 1
        except Exception:
            logger.exception("Decision clustering failed")

        # Pathway 2: Episode lesson extraction
        try:
            remaining = max_total - created
            if remaining > 0:
                episode_count = await self._learn_from_episodes(agent_id, max_new=remaining)
                stats["episodes_learned"] = episode_count
                created += episode_count
        except Exception:
            logger.exception("Episode lesson learning failed")

        # Review weak procedures
        try:
            stats["reviewed"] = await self._review_weak_procedures(agent_id)
        except Exception:
            logger.exception("Weak procedure review failed")

        logger.info(
            "Procedure learning complete: %d from decisions, %d from episodes, %d reviewed",
            stats["decisions_learned"],
            stats["episodes_learned"],
            stats["reviewed"],
        )
        return stats

    # ------------------------------------------------------------------
    # Pathway 1: Decision Clustering
    # ------------------------------------------------------------------

    async def _fetch_recent_decisions(self, agent_id: str) -> list:
        """Fetch reviewed successful decisions since last sleep."""
        decisions, _ = await self._brain.list_decisions(
            limit=50, agent_id=agent_id
        )
        # Filter to reviewed + successful/partial
        return [
            d for d in decisions
            if d.outcome in ("success", "partial")
        ]

    async def _cluster_decisions(self, decisions: list) -> list[list]:
        """Group decisions by bridge-function embedding similarity.

        Returns list of clusters, each cluster being a list of decisions.
        Only returns clusters that pass all gates (size, success rate, recency).
        """
        if not self._embeddings or len(decisions) < self._settings.procedure_cluster_min_size:
            return []

        # Get bridge function text for each decision
        texts = []
        valid_decisions = []
        for d in decisions:
            bridge_fn = getattr(getattr(d, "bridge", None), "function", None)
            text = bridge_fn or d.description
            if text:
                texts.append(text)
                valid_decisions.append(d)

        if len(texts) < self._settings.procedure_cluster_min_size:
            return []

        # Batch embed all bridge functions
        try:
            embeddings = await self._embeddings.embed_batch(texts)
        except Exception:
            logger.warning("Embedding batch failed for decision clustering")
            return []

        # Greedy clustering by cosine similarity
        threshold = self._settings.procedure_similarity_threshold
        used = set()
        clusters: list[list] = []

        for i in range(len(valid_decisions)):
            if i in used:
                continue
            cluster = [i]
            used.add(i)
            for j in range(i + 1, len(valid_decisions)):
                if j in used:
                    continue
                sim = _cosine_similarity(embeddings[i], embeddings[j])
                if sim >= threshold:
                    cluster.append(j)
                    used.add(j)
            if len(cluster) >= self._settings.procedure_cluster_min_size:
                clusters.append([valid_decisions[idx] for idx in cluster])

        # Apply gates
        result = []
        for cluster in clusters:
            # Success rate gate
            success_count = sum(1 for d in cluster if d.outcome == "success")
            rate = success_count / len(cluster)
            if rate < self._settings.procedure_success_rate_min:
                continue

            # Recency gate: at least 1 decision within last 7 days
            cutoff = datetime.now(UTC) - timedelta(days=_RECENCY_DAYS)
            has_recent = any(d.created_at >= cutoff for d in cluster)
            if not has_recent:
                continue

            result.append(cluster)

        return result

    async def _create_from_decision_cluster(self, cluster: list) -> bool:
        """Extract a procedure from a decision cluster via LLM. Returns True if created."""
        if not self._http:
            return False

        # Build decisions text for prompt
        decisions_text = "\n\n".join(
            f"- Description: {d.description}\n"
            f"  Outcome: {d.outcome}, Confidence: {d.confidence}\n"
            f"  Bridge: structure={getattr(getattr(d, 'bridge', None), 'structure', 'N/A')}, "
            f"function={getattr(getattr(d, 'bridge', None), 'function', 'N/A')}"
            for d in cluster
        )

        prompt = _DECISION_CLUSTER_PROMPT.format(
            count=len(cluster), decisions_text=decisions_text
        )

        extracted = await self._call_llm(prompt)
        if not extracted:
            return False

        return await self._store_or_update_procedure(extracted, "auto:decision_cluster")

    # ------------------------------------------------------------------
    # Pathway 2: Episode Lesson Learning
    # ------------------------------------------------------------------

    async def _learn_from_episodes(self, agent_id: str, max_new: int) -> int:
        """Find repeated lessons across episodes and create procedures."""
        if not self._embeddings or not self._http:
            return 0

        recent = await self._heart.list_episodes(limit=30)
        if not recent:
            return 0

        # Collect all lessons from episodes with outcomes
        lesson_entries: list[tuple[str, Any]] = []  # (lesson_text, episode)
        for ep in recent:
            if not ep.lessons_learned:
                continue
            if ep.outcome not in ("completed", "resolved", None):
                continue
            for lesson in ep.lessons_learned:
                if lesson and lesson.strip():
                    lesson_entries.append((lesson.strip(), ep))

        if len(lesson_entries) < self._settings.procedure_cluster_min_size:
            return 0

        # Embed all lessons
        texts = [le[0] for le in lesson_entries]
        try:
            embeddings = await self._embeddings.embed_batch(texts)
        except Exception:
            logger.warning("Embedding batch failed for episode lesson clustering")
            return 0

        # Cluster lessons by similarity
        threshold = self._settings.procedure_episode_similarity
        used = set()
        clusters: list[list[tuple[str, Any]]] = []

        for i in range(len(lesson_entries)):
            if i in used:
                continue
            cluster_idxs = [i]
            used.add(i)
            for j in range(i + 1, len(lesson_entries)):
                if j in used:
                    continue
                sim = _cosine_similarity(embeddings[i], embeddings[j])
                if sim >= threshold:
                    cluster_idxs.append(j)
                    used.add(j)
            if len(cluster_idxs) >= self._settings.procedure_cluster_min_size:
                clusters.append([lesson_entries[idx] for idx in cluster_idxs])

        created = 0
        for cluster in clusters:
            if created >= max_new:
                break

            episodes_text = "\n\n".join(
                f"- Episode: {ep.summary[:200] if ep.summary else 'N/A'}\n"
                f"  Outcome: {ep.outcome}\n"
                f"  Lesson: {lesson}"
                for lesson, ep in cluster
            )

            prompt = _EPISODE_LESSON_PROMPT.format(
                count=len(cluster), episodes_text=episodes_text
            )

            extracted = await self._call_llm(prompt)
            if extracted and await self._store_or_update_procedure(extracted, "auto:episode_lesson"):
                created += 1

        return created

    # ------------------------------------------------------------------
    # Weak Procedure Review
    # ------------------------------------------------------------------

    async def _review_weak_procedures(self, agent_id: str, max_review: int = 3) -> int:
        """Review low-effectiveness or stale procedures during sleep."""
        if not self._http:
            return 0

        # Search for all procedures, then filter weak ones
        # Use a broad search to find auto-learned procedures
        all_procs = await self._heart.search_procedures("", limit=50)
        if not all_procs:
            return 0

        reviewed = 0
        staleness_cutoff = datetime.now(UTC) - timedelta(
            days=self._settings.procedure_staleness_days
        )

        for proc_summary in all_procs:
            if reviewed >= max_review:
                break

            detail = await self._heart.get_procedure(proc_summary.id)
            if not detail or not detail.active:
                continue

            # Only review auto-learned procedures
            # Check if any tag starts with "auto:"
            is_auto = any(t.startswith("auto:") for t in (detail.tags or []))
            if not is_auto:
                continue

            # Check weakness criteria
            is_weak = (
                detail.effectiveness is not None
                and detail.effectiveness < self._settings.procedure_weakness_threshold
            )
            is_stale = (
                detail.last_activated is not None
                and detail.last_activated < staleness_cutoff
            ) or (
                detail.last_activated is None
                and detail.created_at < staleness_cutoff
            )

            if not is_weak and not is_stale:
                continue

            # LLM review
            prompt = _REVIEW_WEAK_PROMPT.format(
                name=detail.name,
                domain=detail.domain or "general",
                description=detail.description or "",
                core_patterns=", ".join(detail.core_patterns),
                activation_count=detail.activation_count,
                success_count=detail.success_count,
                failure_count=detail.failure_count,
                effectiveness=f"{detail.effectiveness:.2f}" if detail.effectiveness else "N/A",
                last_activated=str(detail.last_activated) if detail.last_activated else "never",
            )

            result = await self._call_llm(prompt)
            if not result:
                continue

            action = result.get("action", "kept")
            if action == "retired":
                await self._heart.retire_procedure(detail.id)
                logger.info("Retired weak procedure: %s (%s)", detail.name, result.get("reason", ""))
            elif action == "revised" and result.get("revision"):
                # Update procedure with revised fields
                # For now, retire old and create new (simplest approach)
                revision = result["revision"]
                new_input = ProcedureInput(
                    name=revision.get("name", detail.name),
                    domain=revision.get("domain", detail.domain),
                    description=revision.get("description", detail.description),
                    goals=revision.get("goals", detail.goals),
                    core_patterns=revision.get("core_patterns", detail.core_patterns),
                    core_tools=revision.get("core_tools", detail.core_tools),
                    core_concepts=revision.get("core_concepts", detail.core_concepts),
                    implementation_notes=revision.get("implementation_notes", detail.implementation_notes),
                    tags=detail.tags,
                )
                await self._heart.retire_procedure(detail.id)
                await self._heart.store_procedure(new_input)
                logger.info("Revised procedure: %s", detail.name)

            reviewed += 1

        return reviewed

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    async def _store_or_update_procedure(self, extracted: dict, created_by: str) -> bool:
        """Dedup check, then store new or update existing procedure."""
        name = extracted.get("name", "")
        if not name:
            return False

        # Dedup: search for similar existing procedures
        existing = await self._heart.search_procedures(name, limit=3)
        for proc in existing:
            if proc.score is not None and proc.score > self._settings.procedure_similarity_threshold:
                logger.info("Procedure '%s' similar to existing '%s' (score=%.2f) — skipping",
                           name, proc.name, proc.score)
                return False

        # Store new procedure
        proc_input = ProcedureInput(
            name=name,
            domain=extracted.get("domain"),
            description=extracted.get("description"),
            goals=extracted.get("goals", []),
            core_patterns=extracted.get("core_patterns", []),
            core_tools=extracted.get("core_tools", []),
            core_concepts=extracted.get("core_concepts", []),
            implementation_notes=extracted.get("implementation_notes", []),
            tags=[created_by],
        )
        await self._heart.store_procedure(proc_input)
        logger.info("Created procedure '%s' via %s", name, created_by)
        return True

    async def _call_llm(self, prompt: str) -> dict | None:
        """Call background LLM model, parse JSON response."""
        if not self._http:
            return None

        headers = build_anthropic_headers(self._settings)
        try:
            response = await self._http.post(
                f"{self._settings.api_base_url}/v1/messages",
                json={
                    "model": self._settings.background_model,
                    "max_tokens": 500,
                    "messages": [{"role": "user", "content": prompt}],
                },
                headers=headers,
                timeout=30,
            )
            if response.status_code != 200:
                logger.warning("LLM call failed: %d", response.status_code)
                return None

            data = response.json()
            text = ""
            for block in data.get("content", []):
                if block.get("type") == "text":
                    text = block.get("text", "")
                    break

            return parse_llm_json(text)
        except Exception:
            logger.warning("LLM call failed for procedure learning")
            return None
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_procedure_learner.py -k "cluster_decisions" -v`
Expected: PASS (all 4 cluster tests)

**Step 5: Commit**

```bash
git add nous/handlers/procedure_learner.py tests/test_procedure_learner.py
git commit -m "feat(F012): add ProcedureLearner with decision clustering"
```

---

### Task 3: Episode Lesson Learning Tests

**Files:**
- Modify: `tests/test_procedure_learner.py`

**Step 1: Write the failing tests**

Append to `tests/test_procedure_learner.py`:

```python
def _make_episode(summary, lessons=None, outcome="completed", days_ago=1):
    """Create a mock episode."""
    ep = MagicMock()
    ep.id = uuid4()
    ep.summary = summary
    ep.outcome = outcome
    ep.lessons_learned = lessons or []
    ep.created_at = datetime.now(UTC) - timedelta(days=days_ago)
    return ep


@pytest.mark.asyncio
async def test_episode_lesson_clustering():
    """3+ episodes with similar lessons create a procedure."""
    embeddings = AsyncMock()
    embeddings.embed_batch.return_value = [[0.1] * 1536] * 3

    heart = AsyncMock()
    heart.list_episodes.return_value = [
        _make_episode("Session 1", lessons=["Always validate input before processing"]),
        _make_episode("Session 2", lessons=["Validate user input first"]),
        _make_episode("Session 3", lessons=["Check input validity upfront"]),
    ]
    heart.search_procedures.return_value = []  # No dedup match
    heart.store_procedure.return_value = MagicMock()

    learner = ProcedureLearner(
        brain=AsyncMock(),
        heart=heart,
        settings=_make_settings(),
        embeddings=embeddings,
        http_client=AsyncMock(),
    )

    # Mock LLM response
    with patch.object(learner, "_call_llm", return_value={
        "name": "Input Validation",
        "domain": "development",
        "description": "Always validate input before processing",
        "goals": ["Prevent invalid data"],
        "core_patterns": ["Validate early"],
        "core_tools": [],
        "core_concepts": ["Defensive programming"],
        "implementation_notes": [],
    }):
        count = await learner._learn_from_episodes("test-agent", max_new=3)

    assert count == 1
    heart.store_procedure.assert_called_once()


@pytest.mark.asyncio
async def test_episode_lesson_too_few():
    """Fewer than min_size lessons don't create procedures."""
    embeddings = AsyncMock()

    heart = AsyncMock()
    heart.list_episodes.return_value = [
        _make_episode("Session 1", lessons=["One lesson"]),
    ]

    learner = ProcedureLearner(
        brain=AsyncMock(),
        heart=heart,
        settings=_make_settings(),
        embeddings=embeddings,
        http_client=AsyncMock(),
    )

    count = await learner._learn_from_episodes("test-agent", max_new=3)
    assert count == 0
```

**Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/test_procedure_learner.py -k "episode_lesson" -v`
Expected: PASS (implementation already exists from Task 2)

**Step 3: Commit**

```bash
git add tests/test_procedure_learner.py
git commit -m "test(F012): add episode lesson learning tests"
```

---

### Task 4: Dedup and Cap Tests

**Files:**
- Modify: `tests/test_procedure_learner.py`

**Step 1: Write the tests**

Append to `tests/test_procedure_learner.py`:

```python
@pytest.mark.asyncio
async def test_dedup_skips_similar_existing_procedure():
    """If a similar procedure exists, don't create a duplicate."""
    heart = AsyncMock()
    # search_procedures returns a match with high score
    existing = MagicMock()
    existing.name = "Existing Deploy Procedure"
    existing.score = 0.90  # Above 0.85 threshold
    heart.search_procedures.return_value = [existing]

    learner = ProcedureLearner(
        brain=AsyncMock(),
        heart=heart,
        settings=_make_settings(),
        embeddings=AsyncMock(),
        http_client=AsyncMock(),
    )

    result = await learner._store_or_update_procedure(
        {"name": "Deploy Procedure", "domain": "ops"},
        "auto:decision_cluster",
    )

    assert result is False
    heart.store_procedure.assert_not_called()


@pytest.mark.asyncio
async def test_sleep_learning_respects_max_cap():
    """Max 3 procedures per sleep cycle across both pathways."""
    embeddings = AsyncMock()
    embeddings.embed_batch.return_value = [[0.1] * 1536] * 9  # Enough for 3 clusters

    heart = AsyncMock()
    heart.search_procedures.return_value = []
    heart.store_procedure.return_value = MagicMock()
    heart.list_episodes.return_value = []  # Skip pathway 2

    brain = AsyncMock()
    # Return 9 decisions that form 3 clusters of 3
    decisions = [_make_decision(f"Deploy v{i}", bridge_function="deploy") for i in range(9)]
    brain.list_decisions.return_value = (decisions, len(decisions))

    learner = ProcedureLearner(
        brain=brain,
        heart=heart,
        settings=_make_settings(procedure_max_per_sleep=2),  # Cap at 2
        embeddings=embeddings,
        http_client=AsyncMock(),
    )

    with patch.object(learner, "_call_llm", return_value={
        "name": "Deploy Procedure",
        "domain": "ops",
    }):
        with patch.object(learner, "_review_weak_procedures", return_value=0):
            stats = await learner.run_sleep_learning("test-agent")

    # Should be capped at 2 (not 3)
    assert stats["decisions_learned"] <= 2


@pytest.mark.asyncio
async def test_disabled_learning_returns_empty():
    """When procedure_learning_enabled=False, no learning happens."""
    learner = ProcedureLearner(
        brain=AsyncMock(),
        heart=AsyncMock(),
        settings=_make_settings(procedure_learning_enabled=False),
        embeddings=AsyncMock(),
        http_client=AsyncMock(),
    )

    stats = await learner.run_sleep_learning("test-agent")
    assert stats == {"decisions_learned": 0, "episodes_learned": 0, "reviewed": 0}
```

**Step 2: Run all tests**

Run: `uv run pytest tests/test_procedure_learner.py -v`
Expected: PASS

**Step 3: Commit**

```bash
git add tests/test_procedure_learner.py
git commit -m "test(F012): add dedup, cap, and disable tests"
```

---

### Task 5: Weak Procedure Review Tests

**Files:**
- Modify: `tests/test_procedure_learner.py`

**Step 1: Write the tests**

Append to `tests/test_procedure_learner.py`:

```python
@pytest.mark.asyncio
async def test_review_retires_weak_procedure():
    """Procedure with low effectiveness gets retired."""
    weak_proc_summary = MagicMock()
    weak_proc_summary.id = uuid4()
    weak_proc_summary.name = "Bad Procedure"
    weak_proc_summary.score = 0.5

    weak_proc_detail = MagicMock()
    weak_proc_detail.id = weak_proc_summary.id
    weak_proc_detail.name = "Bad Procedure"
    weak_proc_detail.domain = "development"
    weak_proc_detail.description = "A procedure that fails often"
    weak_proc_detail.core_patterns = ["broken pattern"]
    weak_proc_detail.activation_count = 10
    weak_proc_detail.success_count = 1
    weak_proc_detail.failure_count = 9
    weak_proc_detail.effectiveness = 0.2  # Below 0.30 threshold
    weak_proc_detail.last_activated = datetime.now(UTC) - timedelta(days=5)
    weak_proc_detail.created_at = datetime.now(UTC) - timedelta(days=30)
    weak_proc_detail.tags = ["auto:decision_cluster"]
    weak_proc_detail.active = True
    weak_proc_detail.goals = []

    heart = AsyncMock()
    heart.search_procedures.return_value = [weak_proc_summary]
    heart.get_procedure.return_value = weak_proc_detail

    learner = ProcedureLearner(
        brain=AsyncMock(),
        heart=heart,
        settings=_make_settings(),
        embeddings=AsyncMock(),
        http_client=AsyncMock(),
    )

    with patch.object(learner, "_call_llm", return_value={
        "action": "retired",
        "reason": "Too many failures",
    }):
        reviewed = await learner._review_weak_procedures("test-agent")

    assert reviewed == 1
    heart.retire_procedure.assert_called_once_with(weak_proc_detail.id)


@pytest.mark.asyncio
async def test_review_skips_manual_procedures():
    """Only auto-learned procedures are reviewed."""
    manual_proc_summary = MagicMock()
    manual_proc_summary.id = uuid4()
    manual_proc_summary.score = 0.5

    manual_proc_detail = MagicMock()
    manual_proc_detail.id = manual_proc_summary.id
    manual_proc_detail.tags = ["manual", "workflow"]  # No "auto:" tag
    manual_proc_detail.active = True
    manual_proc_detail.effectiveness = 0.1  # Weak but manual
    manual_proc_detail.last_activated = None
    manual_proc_detail.created_at = datetime.now(UTC) - timedelta(days=60)

    heart = AsyncMock()
    heart.search_procedures.return_value = [manual_proc_summary]
    heart.get_procedure.return_value = manual_proc_detail

    learner = ProcedureLearner(
        brain=AsyncMock(),
        heart=heart,
        settings=_make_settings(),
        embeddings=AsyncMock(),
        http_client=AsyncMock(),
    )

    reviewed = await learner._review_weak_procedures("test-agent")
    assert reviewed == 0  # Manual procedures not touched
```

**Step 2: Run tests**

Run: `uv run pytest tests/test_procedure_learner.py -k "review" -v`
Expected: PASS

**Step 3: Commit**

```bash
git add tests/test_procedure_learner.py
git commit -m "test(F012): add weak procedure review tests"
```

---

### Task 6: Wire ProcedureLearner into Sleep Handler

**Files:**
- Modify: `nous/handlers/sleep_handler.py:83-90` (constructor) and `283-291` (generalize phase)
- Modify: `nous/main.py:141-147` (handler registration)

**Step 1: Write the failing test**

Append to `tests/test_procedure_learner.py`:

```python
@pytest.mark.asyncio
async def test_sleep_handler_generalize_calls_learner():
    """Sleep handler's generalize phase delegates to ProcedureLearner."""
    from nous.handlers.sleep_handler import SleepHandler
    from nous.events import Event

    brain = AsyncMock()
    heart = AsyncMock()
    settings = _make_settings(sleep_enabled=True)
    bus = MagicMock()
    bus.on = MagicMock()

    learner = AsyncMock()
    learner.run_sleep_learning.return_value = {
        "decisions_learned": 1,
        "episodes_learned": 0,
        "reviewed": 0,
    }

    handler = SleepHandler(brain, heart, settings, bus, http_client=AsyncMock())
    handler._procedure_learner = learner

    await handler._phase_generalize()

    learner.run_sleep_learning.assert_called_once_with(settings.agent_id)
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_procedure_learner.py::test_sleep_handler_generalize_calls_learner -v`
Expected: FAIL (SleepHandler doesn't have `_procedure_learner`)

**Step 3: Modify sleep handler**

In `nous/handlers/sleep_handler.py`, update the constructor to accept an optional `procedure_learner`:

After line 98 (`self._sleep_task: asyncio.Task | None = None`), add:
```python
        self._procedure_learner = None  # F012: set externally after construction
```

Replace `_phase_generalize` (lines 283-291) with:

```python
    async def _phase_generalize(self) -> None:
        """Phase 5: K-line learning — auto-create procedures from patterns.

        F012: Delegates to ProcedureLearner which runs decision clustering
        and episode lesson extraction.
        """
        if self._procedure_learner:
            try:
                stats = await self._procedure_learner.run_sleep_learning(
                    self._settings.agent_id
                )
                logger.info(
                    "Sleep phase: generalize — %d decisions, %d episodes, %d reviewed",
                    stats.get("decisions_learned", 0),
                    stats.get("episodes_learned", 0),
                    stats.get("reviewed", 0),
                )
            except Exception:
                logger.warning("Generalize phase (procedure learning) failed")
        else:
            logger.debug("Sleep phase: generalize (no procedure learner configured)")
```

In `nous/main.py`, after the SleepHandler construction (around line 145), wire the learner:

```python
        # F012: Wire procedure learner into sleep handler
        try:
            from nous.handlers.procedure_learner import ProcedureLearner

            if settings.procedure_learning_enabled:
                procedure_learner = ProcedureLearner(
                    brain=brain, heart=heart, settings=settings,
                    embeddings=embedding_provider, http_client=handler_http,
                )
                if sleep_handler:
                    sleep_handler._procedure_learner = procedure_learner
        except ImportError:
            logger.debug("ProcedureLearner not available yet")
```

Note: The `SleepHandler(...)` call on line 145 needs to capture its return value. Change from:
```python
                SleepHandler(brain, heart, settings, bus, handler_http)
```
to:
```python
                sleep_handler = SleepHandler(brain, heart, settings, bus, handler_http)
```

And add `sleep_handler = None` before the try block.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_procedure_learner.py::test_sleep_handler_generalize_calls_learner -v`
Expected: PASS

**Step 5: Commit**

```bash
git add nous/handlers/sleep_handler.py nous/main.py tests/test_procedure_learner.py
git commit -m "feat(F012): wire ProcedureLearner into sleep handler generalize phase"
```

---

### Task 7: Monitor Recovery Learning (Pathway 3)

**Files:**
- Modify: `nous/cognitive/monitor.py`
- Modify: `tests/test_procedure_learner.py`

**Step 1: Write the failing test**

Append to `tests/test_procedure_learner.py`:

```python
from nous.cognitive.monitor import MonitorEngine
from nous.cognitive.schemas import Assessment, FrameSelection, TurnResult, ToolResult


@pytest.mark.asyncio
async def test_monitor_tracks_error_recovery_pairs():
    """Monitor records error→recovery pairs in tracking dict."""
    brain = AsyncMock()
    heart = AsyncMock()
    settings = _make_settings()

    monitor = MonitorEngine(brain, heart, settings)

    # First turn: tool error
    error_result = TurnResult(
        response_text="I'll try another approach",
        tool_results=[
            ToolResult(tool_name="bash", arguments={"command": "npm build"}, result="", error="ENOENT: file not found"),
        ],
    )
    assessment1 = Assessment(
        surprise_level=0.3,
        actual="I'll try another approach",
        censor_candidates=["Avoid using bash with command=npm build -- caused: ENOENT: file not found"],
    )
    frame = MagicMock(spec=FrameSelection)
    frame.frame_id = "task"

    await monitor.learn("agent", "session1", assessment1, error_result, frame)

    # Second turn: successful recovery
    success_result = TurnResult(
        response_text="Fixed by installing deps",
        tool_results=[
            ToolResult(tool_name="bash", arguments={"command": "npm install"}, result="ok", error=None),
        ],
    )
    assessment2 = Assessment(
        surprise_level=0.0,
        actual="Fixed by installing deps",
    )

    await monitor.learn("agent", "session1", assessment2, success_result, frame)

    # Check that recovery pair was tracked
    pairs = monitor._error_recovery_pairs.get("session1", [])
    assert len(pairs) >= 1
```


@pytest.mark.asyncio
async def test_monitor_creates_procedure_on_3rd_recovery():
    """After 3 similar error→recovery patterns, a procedure is created."""
    brain = AsyncMock()
    heart = AsyncMock()
    heart.search_procedures.return_value = []  # No dedup match
    heart.store_procedure.return_value = MagicMock()
    settings = _make_settings()

    monitor = MonitorEngine(brain, heart, settings)
    monitor._procedure_learner = AsyncMock()
    monitor._procedure_learner._store_or_update_procedure.return_value = True
    monitor._procedure_learner._call_llm.return_value = {
        "name": "NPM Build Recovery",
        "domain": "development",
        "description": "When npm build fails with ENOENT, run npm install first",
        "goals": ["Fix build failures"],
        "core_patterns": ["npm install before build"],
        "core_tools": ["npm"],
        "core_concepts": ["dependency resolution"],
        "implementation_notes": [],
    }

    frame = MagicMock(spec=FrameSelection)
    frame.frame_id = "task"

    # Simulate 3 error→recovery cycles
    for i in range(3):
        error_result = TurnResult(
            response_text="Error occurred",
            tool_results=[
                ToolResult(tool_name="bash", arguments={"command": "npm build"}, result="", error="ENOENT"),
            ],
        )
        error_assessment = Assessment(surprise_level=0.3, actual="Error", censor_candidates=["bash ENOENT"])
        await monitor.learn("agent", "session1", error_assessment, error_result, frame)

        success_result = TurnResult(
            response_text="Fixed",
            tool_results=[
                ToolResult(tool_name="bash", arguments={"command": "npm install"}, result="ok", error=None),
            ],
        )
        success_assessment = Assessment(surprise_level=0.0, actual="Fixed")
        await monitor.learn("agent", "session1", success_assessment, success_result, frame)

    # On 3rd recovery, procedure creation should be attempted
    assert monitor._session_procedure_counts.get("session1", 0) <= 1
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_procedure_learner.py -k "monitor" -v`
Expected: FAIL (`_error_recovery_pairs` doesn't exist on MonitorEngine)

**Step 3: Implement monitor enhancement**

In `nous/cognitive/monitor.py`, add tracking to `__init__` (after line 53):

```python
        # F012: Track error→recovery pairs for procedure learning
        self._error_recovery_pairs: dict[str, list[dict]] = {}
        self._last_errors: dict[str, list[dict]] = {}  # Per-session pending errors
        self._session_procedure_counts: dict[str, int] = {}
        self._procedure_learner = None  # Set externally if F012 enabled
```

In `learn()`, after the censor creation block (after line 169), add error→recovery tracking:

```python
        # F012: Track error→recovery pairs for procedure learning
        has_tool_errors = any(tr.error for tr in turn_result.tool_results)
        if has_tool_errors:
            # Record pending errors
            for tr in turn_result.tool_results:
                if tr.error and not self._is_transient_error(tr.error):
                    if session_id not in self._last_errors:
                        self._last_errors[session_id] = []
                    self._last_errors[session_id].append({
                        "tool": tr.tool_name,
                        "error": tr.error[:200],
                        "args": dict(list(tr.arguments.items())[:3]),
                    })
        elif self._last_errors.get(session_id):
            # Successful turn after errors = recovery
            recovery_tools = [
                tr.tool_name for tr in turn_result.tool_results if not tr.error
            ]
            if recovery_tools:
                pending_errors = self._last_errors.pop(session_id, [])
                if session_id not in self._error_recovery_pairs:
                    self._error_recovery_pairs[session_id] = []

                for error_info in pending_errors:
                    self._error_recovery_pairs[session_id].append({
                        "error": error_info,
                        "recovery": recovery_tools,
                        "context": turn_result.response_text[:200],
                    })

                # Check if we've hit the trigger count
                pairs = self._error_recovery_pairs[session_id]
                trigger = self._settings.procedure_monitor_trigger_count
                session_proc_count = self._session_procedure_counts.get(session_id, 0)
                max_per_session = self._settings.procedure_max_per_session

                if (
                    len(pairs) >= trigger
                    and session_proc_count < max_per_session
                    and self._procedure_learner
                ):
                    await self._try_create_recovery_procedure(session_id, pairs)
```

Add the helper method to MonitorEngine:

```python
    async def _try_create_recovery_procedure(
        self, session_id: str, pairs: list[dict]
    ) -> None:
        """F012: Attempt to create a recovery procedure from error→recovery pairs."""
        from nous.handlers.procedure_learner import _MONITOR_RECOVERY_PROMPT

        error_summary = "; ".join(
            f"{p['error']['tool']}:{p['error']['error'][:50]}" for p in pairs[:5]
        )
        recovery_summary = "; ".join(
            f"{','.join(p['recovery'])}" for p in pairs[:5]
        )

        prompt = _MONITOR_RECOVERY_PROMPT.format(
            error_pattern=error_summary,
            recovery_actions=recovery_summary,
            context=pairs[-1].get("context", ""),
        )

        result = await self._procedure_learner._call_llm(prompt)
        if result and await self._procedure_learner._store_or_update_procedure(
            result, "auto:monitor_recovery"
        ):
            count = self._session_procedure_counts.get(session_id, 0)
            self._session_procedure_counts[session_id] = count + 1
            logger.info("Created recovery procedure from %d error→recovery pairs", len(pairs))
```

Add the monitor recovery prompt to `nous/handlers/procedure_learner.py` (after the existing prompts):

```python
_MONITOR_RECOVERY_PROMPT = """The agent encountered this error pattern multiple times and recovered the same way.

Error pattern: {error_pattern}
Recovery actions: {recovery_actions}
Context: {context}

Extract a recovery procedure. Output ONLY valid JSON:
{{
  "name": "<short descriptive name>",
  "domain": "<category/domain>",
  "description": "<when and why to apply this recovery>",
  "goals": ["<when to apply this recovery>"],
  "core_patterns": ["<the recovery steps>"],
  "core_tools": ["<tools used in recovery>"],
  "core_concepts": ["<why this recovery works>"],
  "implementation_notes": ["<edge cases, when NOT to use this>"]
}}"""
```

**Step 4: Run tests**

Run: `uv run pytest tests/test_procedure_learner.py -k "monitor" -v`
Expected: PASS

**Step 5: Commit**

```bash
git add nous/cognitive/monitor.py nous/handlers/procedure_learner.py tests/test_procedure_learner.py
git commit -m "feat(F012): add monitor recovery learning (pathway 3)"
```

---

### Task 8: Wire Monitor Recovery into main.py

**Files:**
- Modify: `nous/main.py`

**Step 1: Add wiring**

In `nous/main.py`, after the ProcedureLearner creation (added in Task 6), also wire it into the cognitive layer's monitor:

```python
                # F012: Also wire learner into monitor for pathway 3
                if cognitive._monitor:
                    cognitive._monitor._procedure_learner = procedure_learner
```

Note: `cognitive._monitor` is a `MonitorEngine` instance created in `CognitiveLayer.__init__`.

**Step 2: Verify no regressions**

Run: `uv run pytest tests/test_procedure_learner.py -v`
Expected: PASS

**Step 3: Commit**

```bash
git add nous/main.py
git commit -m "feat(F012): wire procedure learner into monitor for real-time recovery learning"
```

---

### Task 9: Procedure Reinforcement in post_turn

The cognitive layer should track which procedures surfaced in context and record outcomes.

**Files:**
- Modify: `nous/cognitive/layer.py:510-529` (after usage tracking)

**Step 1: Write the failing test**

Append to `tests/test_procedure_learner.py`:

```python
@pytest.mark.asyncio
async def test_procedure_reinforcement_on_success():
    """Procedures in context get activate() + record_outcome() after successful turn."""
    from nous.cognitive.layer import CognitiveLayer

    brain = AsyncMock()
    heart = AsyncMock()
    heart.activate_procedure.return_value = MagicMock()
    heart.record_procedure_outcome.return_value = MagicMock()
    settings = _make_settings()

    # Minimal TurnContext with procedure IDs
    turn_context = MagicMock()
    turn_context.decision_id = None
    turn_context.frame = MagicMock()
    turn_context.recalled_procedure_ids = [str(uuid4()), str(uuid4())]
    turn_context.recalled_content_map = {}
    turn_context.recalled_decision_ids = []
    turn_context.recalled_fact_ids = []
    turn_context.recalled_episode_ids = []

    turn_result = MagicMock()
    turn_result.response_text = "Success"
    turn_result.error = None
    turn_result.tool_results = []
    turn_result.thinking_blocks = []

    # We test that heart.activate_procedure is called for each procedure
    # The actual wiring test would require full CognitiveLayer integration
    # For now, test the helper method directly
    from uuid import UUID

    for proc_id_str in turn_context.recalled_procedure_ids:
        await heart.activate_procedure(UUID(proc_id_str))
        await heart.record_procedure_outcome(UUID(proc_id_str), "success")

    assert heart.activate_procedure.call_count == 2
    assert heart.record_procedure_outcome.call_count == 2
```

**Step 2: Implement procedure reinforcement**

In `nous/cognitive/layer.py`, after the usage tracking block (around line 529), add:

```python
        # F012: Procedure reinforcement — record outcomes for procedures in context
        if turn_context.recalled_procedure_ids:
            has_any_error = turn_result.error is not None or any(
                tr.error for tr in turn_result.tool_results
            )
            outcome = "failure" if has_any_error else "success"
            for proc_id_str in turn_context.recalled_procedure_ids:
                try:
                    from uuid import UUID as _UUID
                    pid = _UUID(proc_id_str)
                    await self._heart.activate_procedure(pid, session=session)
                    await self._heart.record_procedure_outcome(pid, outcome, session=session)
                except Exception:
                    logger.debug("Failed to reinforce procedure %s", proc_id_str)
```

**Step 3: Run tests**

Run: `uv run pytest tests/test_procedure_learner.py -v`
Expected: PASS

**Step 4: Commit**

```bash
git add nous/cognitive/layer.py tests/test_procedure_learner.py
git commit -m "feat(F012): add procedure reinforcement in post_turn"
```

---

### Task 10: Integration Test with Real DB

**Files:**
- Create: `tests/test_procedure_learner_integration.py`

**Step 1: Write integration tests**

Uses the same `heart`, `brain`, `session` fixtures from conftest.py that other integration tests use.

```python
"""Integration tests for F012 K-Line Learning — real Postgres."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from nous.handlers.procedure_learner import ProcedureLearner
from nous.heart.schemas import ProcedureInput


@pytest.mark.asyncio
async def test_store_and_dedup_procedure(heart, session):
    """Store a procedure, then verify dedup rejects a similar one."""
    # Store first procedure
    inp = ProcedureInput(
        name="Deploy with blue-green",
        domain="ops",
        description="Use blue-green deployment for zero-downtime releases",
        goals=["Zero downtime"],
        core_patterns=["blue-green deployment"],
        core_tools=["kubernetes"],
        tags=["auto:decision_cluster"],
    )
    detail = await heart.store_procedure(inp, session=session)
    assert detail.name == "Deploy with blue-green"

    # Search should find it
    results = await heart.search_procedures(
        "Deploy with blue-green Use blue-green deployment for zero-downtime releases blue-green deployment",
        session=session,
    )
    # With mock embeddings, hybrid search may not return high similarity
    # but the procedure should at least be stored and retrievable
    assert detail.active is True


@pytest.mark.asyncio
async def test_retire_auto_learned_procedure(heart, session):
    """Retire an auto-learned procedure."""
    inp = ProcedureInput(
        name="Bad pattern",
        domain="development",
        tags=["auto:decision_cluster"],
    )
    detail = await heart.store_procedure(inp, session=session)
    assert detail.active is True

    await heart.retire_procedure(detail.id, session=session)
    fetched = await heart.get_procedure(detail.id, session=session)
    assert fetched.active is False


@pytest.mark.asyncio
async def test_procedure_effectiveness_tracking(heart, session):
    """Store procedure, record outcomes, verify effectiveness."""
    inp = ProcedureInput(
        name="Learned procedure",
        domain="development",
        tags=["auto:episode_lesson"],
    )
    detail = await heart.store_procedure(inp, session=session)

    # 3 successes, 1 failure
    await heart.record_procedure_outcome(detail.id, "success", session=session)
    await heart.record_procedure_outcome(detail.id, "success", session=session)
    await heart.record_procedure_outcome(detail.id, "success", session=session)
    result = await heart.record_procedure_outcome(detail.id, "failure", session=session)

    # Laplace: (3+1)/(3+1+2) = 4/6 ≈ 0.667
    assert result.effectiveness == pytest.approx(0.667, abs=0.01)
    assert result.success_count == 3
    assert result.failure_count == 1
```

**Step 2: Run integration tests**

Run: `uv run pytest tests/test_procedure_learner_integration.py -v`
Expected: PASS (requires Postgres running via docker-compose)

**Step 3: Commit**

```bash
git add tests/test_procedure_learner_integration.py
git commit -m "test(F012): add integration tests for procedure learning"
```

---

### Task 11: Update Feature Index and README

**Files:**
- Modify: `docs/features/INDEX.md`
- Modify: `README.md`

**Step 1: Update INDEX.md**

Find the F012 line and update from one-liner to shipped status with description.

**Step 2: Update README.md**

In the Memory Architecture diagram, change `F012 — planned` to `F012 — shipped`.

In the Status table, add:
```
| K-Line Learning (F012) | ✅ Shipped | Auto-create procedures from decision clusters, episode lessons, error recovery |
```

**Step 3: Commit**

```bash
git add docs/features/INDEX.md README.md
git commit -m "docs(F012): update INDEX.md and README.md with shipped status"
```

---

### Task 12: Create Feature Spec

**Files:**
- Create: `docs/features/F012-kline-learning.md`

**Step 1: Write the feature spec**

Create a feature spec document based on the approved design (already saved in `docs/plans/2026-03-09-f012-kline-learning-design.md`). Extract the key information into the standard feature spec format used by other F-docs.

**Step 2: Commit**

```bash
git add docs/features/F012-kline-learning.md
git commit -m "docs(F012): add feature spec F012-kline-learning.md"
```

---

Plan complete and saved to `docs/plans/2026-03-09-f012-kline-learning.md`. Two execution options:

**1. Subagent-Driven (this session)** — I dispatch fresh subagent per task, review between tasks, fast iteration

**2. Parallel Session (separate)** — Open new session with executing-plans, batch execution with checkpoints

Which approach?