"""Tool dispatcher and Nous memory tools for direct Anthropic API integration.

Provides:
- ToolDispatcher: registers tools, dispatches calls, filters by frame
- 4 tool closures that give Claude direct access to Nous memory organs:
  - record_decision: Write decisions to Brain
  - learn_fact: Store facts in Heart
  - recall_deep: Search all memory types (Heart + Brain)
  - create_censor: Add guardrails to Heart

Each tool returns MCP-compliant response format and handles errors gracefully.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from collections.abc import Callable
from functools import partial
from typing import Any
from uuid import UUID

from nous.brain.brain import Brain
from nous.brain.schemas import ReasonInput, RecordInput
from nous.config import Settings
from nous.heart.heart import Heart
from nous.heart.schemas import CensorInput, FactInput, FactRejected, ProcedureInput
from nous.skills.parser import SkillParser

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ToolDispatcher
# ---------------------------------------------------------------------------


class ToolDispatcher:
    """Registers tool handlers and dispatches tool calls from the API.

    Each handler is an async callable that accepts **kwargs and returns
    an MCP-format response: {"content": [{"type": "text", "text": "..."}]}.

    The dispatcher extracts plain text for the Anthropic API tool_result format.
    """

    def __init__(self, *, tool_schema_cache_enabled: bool = True) -> None:
        self._handlers: dict[str, Callable[..., Any]] = {}
        self._schemas: dict[str, dict[str, Any]] = {}  # P0-7 fix
        self._tool_schema_cache: dict[str, list[dict[str, Any]]] = {}  # F036
        self._tool_schema_cache_enabled = tool_schema_cache_enabled  # F036

    def register(self, name: str, handler: Callable[..., Any], schema: dict[str, Any]) -> None:
        """Register a tool handler with its JSON schema."""
        self._handlers[name] = handler
        self._schemas[name] = schema
        self._tool_schema_cache.clear()  # F036: invalidate on registration

    async def dispatch(
        self, name: str, args: dict[str, Any], session_id: str | None = None,
    ) -> tuple[str, bool]:
        """Dispatch a tool call and return (result_text, is_error).

        P0-6 fix: Uses **kwargs unpacking for closures.
        P1-1 fix: Extracts text from MCP-format response.
        """
        handler = self._handlers.get(name)
        if not handler:
            return f"Unknown tool: {name}", True
        try:
            if session_id is not None and name == "spawn_task":
                args = {**args, "_session_id": session_id}
            if session_id is not None and name == "cache_retrieve":
                args = {**args, "session_id": session_id}
            if session_id is not None and name == "run_python":
                args = {**args, "_session_id": session_id}
            if session_id is not None and name == "recall_deep":
                # F051.4 / F055: inject session_id into recall_deep so
                # F055's Cross-Turn Residual Activation can read it via
                # the _session_id kwarg. Until F055 ships, recall_deep
                # silently accepts the kwarg (added by F051.4) and ignores
                # it — fail-open contract.
                args = {**args, "_session_id": session_id}
            result = await handler(**args)  # P0-6: **kwargs unpacking
            # P1-1: Extract text from MCP-format response
            return result["content"][0]["text"], False
        except Exception as e:
            logger.exception("Tool dispatch error for %s", name)
            return f"Tool error: {e}", True

    def tool_definitions(self) -> list[dict[str, Any]]:
        """Return all tool definitions in Anthropic API format."""
        return [
            {
                "name": name,
                "description": schema.get("description", ""),
                "input_schema": schema,
            }
            for name, schema in self._schemas.items()
        ]

    def available_tools(self, frame_id: str) -> list[dict[str, Any]]:
        """Return tool definitions filtered by frame (D5).

        Uses FRAME_TOOLS map from runner module to determine which
        tools are available for a given frame. Wildcard "*" means all tools.

        F036: Results cached per frame_id. Returns deep copy to prevent
        mutation from corrupting the cache.
        """
        # F036: Check cache first (skip if caching disabled)
        if self._tool_schema_cache_enabled and frame_id in self._tool_schema_cache:
            return copy.deepcopy(self._tool_schema_cache[frame_id])

        from nous.api.runner import FRAME_TOOLS

        allowed = FRAME_TOOLS.get(frame_id, [])

        # Wildcard means all tools
        if "*" in allowed:
            result = self.tool_definitions()
        else:
            result = [
                {
                    "name": name,
                    "description": schema.get("description", ""),
                    "input_schema": schema,
                }
                for name, schema in self._schemas.items()
                if name in allowed
            ]

        # F036: Cache and return deep copy
        if self._tool_schema_cache_enabled:
            self._tool_schema_cache[frame_id] = result
            return copy.deepcopy(result)
        return result


# ---------------------------------------------------------------------------
# recall_deep text formatter (F051 refactor)
# ---------------------------------------------------------------------------


def _format_pipeline_text(
    results: "list[Any]",  # list[PipelineResult] — string-quoted to avoid import cycle at module top
    stats: "Any",  # PipelineStats
    search_types: list[str],
) -> str:
    """Format ``run_recall_pipeline`` output into legacy ``recall_deep`` text.

    Byte-identical to the pre-F051 ``recall_deep`` text output for the same
    query + heart/brain state. Section ordering, headings, score precision,
    score-truthy elision (``if dec.score`` not ``is not None``), and contradiction
    warning format are all preserved exactly.
    """
    search_all = "all" in search_types
    results_text: list[str] = []

    # ------------------------------------------------------------------
    # Heart Memory section
    # ------------------------------------------------------------------
    # Heart section is emitted iff Heart sub-search ran (heart_types non-empty).
    # The pre-refactor closure used a local `heart_types` list; we replicate
    # that gate by checking whether any heart-eligible type was in search_types
    # (or 'all' was passed) AND the pipeline produced or attempted Heart results.
    heart_results = [r for r in results if r.source == "heart"]
    heart_section_eligible = search_all or any(
        t in search_types for t in ["episode", "fact", "procedure", "censor"]
    )
    # The original closure ALSO requires that heart_types resolves to a non-empty
    # list — which is always true once heart_section_eligible is True (because the
    # filter expression yields the same membership). So the gate matches.
    if heart_section_eligible:
        if heart_results:
            results_text.append("=== Heart Memory ===")
            for i, result in enumerate(heart_results, 1):
                results_text.append(
                    f"{i}. [{result.type}] {result.description} "
                    f"(id: {result.id}, score: {result.score:.3f})"
                )
        else:
            results_text.append("=== Heart Memory ===\nNo results found.")

    # ------------------------------------------------------------------
    # Graph-Connected Decisions section (F022 Phase 2 cross-type)
    # ------------------------------------------------------------------
    # Pre-refactor: heart_graph_decisions are PipelineResult.source == "graph_expanded"
    # AND derived from Heart seeds. We tag those distinctly: graph-expanded results
    # produced from Heart seeds appear as `source="graph_expanded"` with type=="decision"
    # AND no matching brain-side seed (i.e., emitted in stage 2 not stage 4).
    # Distinguish via a heuristic: stage-2 entries have edge_relation that is NOT
    # "spreading_activation", and they precede the Brain section in the result
    # ordering. We rely on the pipeline's stage ordering: stage-2 results come
    # before any "brain" source result.
    heart_graph: list = []
    seen_brain = False
    for r in results:
        if r.source == "brain":
            seen_brain = True
            continue
        if r.source == "graph_expanded" and not seen_brain and r.type == "decision":
            heart_graph.append(r)

    if heart_graph:
        results_text.append("\n=== Graph-Connected Decisions ===")
        for i, n in enumerate(heart_graph, 1):
            results_text.append(
                f"{i}. [via {n.edge_relation}] {n.description} "
                f"(id: {n.id}, score: {n.score:.3f})"
            )

    # ------------------------------------------------------------------
    # Brain Decisions section
    # ------------------------------------------------------------------
    if search_all or "decision" in search_types:
        decision_results = [r for r in results if r.source == "brain"]
        # Brain-side graph-expanded entries: source in {graph_expanded,
        # spreading_activation} AND appear after the brain block.
        brain_graph: list = []
        seen_brain = False
        for r in results:
            if r.source == "brain":
                seen_brain = True
                continue
            if seen_brain and r.source in ("graph_expanded", "spreading_activation"):
                brain_graph.append(r)

        if decision_results or brain_graph:
            results_text.append("\n=== Brain Decisions ===")
            for i, dec in enumerate(decision_results, 1):
                # Preserve original truthy-elision behavior: ``raw_score``
                # is None or 0.0 -> empty string. Don't use ``dec.score``
                # because it's normalized to 0.0 for None (see _decisions_to_pipeline).
                raw = dec.metadata.get("raw_score")
                score_str = f", score: {raw:.3f}" if raw else ""
                category = dec.metadata.get("category", "")
                stakes = dec.metadata.get("stakes", "")
                confidence = dec.metadata.get("confidence", 0.0)
                results_text.append(
                    f"{i}. {dec.description} | {category} | {stakes} | "
                    f"confidence: {confidence:.2f} "
                    f"(id: {dec.id}{score_str})"
                )
            for j, n in enumerate(brain_graph, len(decision_results) + 1):
                results_text.append(
                    f"{j}. [via graph: {n.edge_relation}] {n.description} "
                    f"(id: {n.id}, score: {n.score:.3f})"
                )
        else:
            results_text.append("\n=== Brain Decisions ===\nNo results found.")

    # ------------------------------------------------------------------
    # Contradiction warnings (F022 Phase 3)
    # ------------------------------------------------------------------
    for src_id, src_type, tgt_id, tgt_type in stats.contradiction_edges:
        results_text.append(
            f"\nWarning: Contradiction detected between "
            f"{src_type}({str(src_id)[:8]}) and "
            f"{tgt_type}({str(tgt_id)[:8]})"
        )

    if not results_text:
        results_text.append("No results found.")

    return "\n".join(results_text)


# ---------------------------------------------------------------------------
# Nous memory tool closures
# ---------------------------------------------------------------------------


def create_nous_tools(brain: Brain, heart: Heart, settings: Settings | None = None) -> dict[str, Any]:
    """Create tool closures with Brain and Heart captured in closure context.

    Returns a dict of async callables suitable for ToolDispatcher registration.
    Each closure takes tool parameters, calls Brain/Heart methods, and returns
    MCP-compliant response: {"content": [{"type": "text", "text": "..."}]}.

    All tools are wrapped in try/except to return error messages as tool results.
    """
    if settings is None:
        settings = Settings()

    async def record_decision(
        description: str,
        confidence: float,
        category: str,
        stakes: str,
        context: str | None = None,
        pattern: str | None = None,
        tags: list[str] | None = None,
        reasons: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Record a decision to the Brain.

        Args:
            description: What was decided
            confidence: 0.0-1.0 confidence level
            category: architecture, process, tooling, security, or integration
            stakes: low, medium, high, or critical
            context: Situation and constraints
            pattern: Abstract pattern this decision represents
            tags: Keywords for filtering
            reasons: List of {type, text} dicts (type: analysis, pattern, empirical, etc.)

        Returns:
            MCP-compliant response with decision ID or error message
        """
        try:
            # Validate and construct input
            reason_inputs = []
            if reasons:
                for r in reasons:
                    if not isinstance(r, dict) or "type" not in r or "text" not in r:
                        return {
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        f"Error: Invalid reason format. Expected dict with 'type' and 'text', got: {r}"
                                    ),
                                }
                            ]
                        }
                    reason_inputs.append(ReasonInput(type=r["type"], text=r["text"]))

            input_data = RecordInput(
                description=description,
                confidence=confidence,
                category=category,  # type: ignore
                stakes=stakes,  # type: ignore
                context=context,
                pattern=pattern,
                tags=tags or [],
                reasons=reason_inputs,
            )

            # Record to Brain
            result = await brain.record(input_data)

            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Decision recorded successfully.\n"
                            f"ID: {result.id}\n"
                            f"Quality score: {result.quality_score:.2f}\n"
                            f"Category: {result.category}\n"
                            f"Stakes: {result.stakes}"
                        ),
                    }
                ]
            }

        except Exception as e:
            logger.exception("record_decision tool failed")
            return {"content": [{"type": "text", "text": f"Error recording decision: {e}"}]}

    async def learn_fact(
        content: str,
        category: str | None = None,
        subject: str | None = None,
        confidence: float = 1.0,
        source: str | None = None,
        source_episode_id: str | None = None,
        source_decision_id: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Store a fact in the Heart.

        Args:
            content: The fact content
            category: preference, technical, person, tool, concept, or rule
            subject: What/who the fact is about
            confidence: 0.0-1.0 confidence level
            source: Where this fact came from
            source_episode_id: Episode UUID if learned during episode
            source_decision_id: Decision UUID if learned during decision
            tags: Keywords for filtering

        Returns:
            MCP-compliant response with fact ID or error message
        """
        try:
            # Parse UUIDs if provided
            episode_uuid = UUID(source_episode_id) if source_episode_id else None
            decision_uuid = UUID(source_decision_id) if source_decision_id else None

            input_data = FactInput(
                content=content,
                category=category,
                subject=subject,
                confidence=confidence,
                source="user_direct",  # F023/F038: +0.15 admission bonus for user tool calls
                source_episode_id=episode_uuid,
                source_decision_id=decision_uuid,
                tags=tags or [],
            )

            # Store to Heart
            result = await heart.learn(input_data)

            # F023/F038: Handle rejected facts — user_direct gets +0.15 bonus (F038-2.4)
            # but very low-quality or short content can still be rejected
            if isinstance(result, FactRejected):
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Fact not stored (admission score {result.composite_score:.2f} "
                                f"< {result.threshold} threshold).\n"
                                f"Scores: {', '.join(f'{k}={v:.2f}' for k, v in result.scores.items())}\n"
                                f"Override with explicit instruction if this should be stored."
                            ),
                        }
                    ]
                }

            warning_msg = ""
            if result.contradiction_warning:
                warning_msg = (
                    f"\nPotential contradiction detected:\n"
                    f"Existing fact: {result.contradiction_warning.existing_content}\n"
                    f"Similarity: {result.contradiction_warning.similarity:.2f}"
                )

            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Fact learned successfully.\n"
                            f"ID: {result.id}\n"
                            f"Category: {result.category or 'none'}\n"
                            f"Subject: {result.subject or 'none'}"
                            f"{warning_msg}"
                        ),
                    }
                ]
            }

        except ValueError as e:
            # UUID parsing error or validation error
            return {"content": [{"type": "text", "text": f"Validation error: {e}"}]}
        except Exception as e:
            logger.exception("learn_fact tool failed")
            return {"content": [{"type": "text", "text": f"Error learning fact: {e}"}]}

    async def recall_deep(  # noqa: C901
        query: str,
        limit: int = 10,
        memory_types: list[str] | None = None,
        _session_id: str | None = None,
    ) -> dict[str, Any]:
        """Search across all memory types in Heart and Brain.

        Thin wrapper around ``run_recall_pipeline`` (F051 refactor): the
        pipeline runs the full retrieval stack and returns structured
        results; this closure formats them into the legacy text shape.

        Args:
            query: Search query string
            limit: Maximum results to return
            memory_types: Types to search (episode, fact, procedure, censor, decision)
                         If None or contains "all", searches everything
            _session_id: F051.4/F055 — session identifier injected by
                ``ToolDispatcher.dispatch``. When F055 (Cross-Turn Residual
                Activation) ships, the residual_activations path reads this
                to bias recall toward recently-surfaced items in the same
                session. Pre-F055 the kwarg is silently accepted and ignored
                (fail-open). Underscore prefix marks it as
                infrastructure-injected, not a tool-schema-declared parameter.

        Returns:
            MCP-compliant response with ranked results or error message
        """
        from nous.api.retrieval_pipeline import run_recall_pipeline

        try:
            search_types = memory_types or ["all"]

            # F055: compute residual activations BEFORE recall (consumed
            # inside Heart._recall via the threaded `residual_activations`
            # kwarg). Skipped when feature flag off, no session_id, or
            # no activator wired (test fixtures, smoke). Fail-open: any
            # exception falls through to cold recall.
            residual_activations: dict[UUID, float] = {}
            current_turn = 0
            if (
                getattr(settings, "residual_activation_enabled", False)
                and _session_id
                and getattr(heart, "_residual_activator", None) is not None
            ):
                try:
                    activator = heart._residual_activator
                    current_turn = await activator.current_turn(brain.agent_id, _session_id)
                    residual_activations = await activator.compute_activations(
                        brain.agent_id, _session_id, current_turn,
                    )
                except Exception:
                    logger.warning(
                        "F055: compute_activations failed for %s/%s, continuing cold",
                        brain.agent_id, _session_id, exc_info=True,
                    )
                    residual_activations = {}

            results, stats = await run_recall_pipeline(
                query=query,
                heart=heart,
                brain=brain,
                settings=settings,
                limit=limit,
                memory_types=memory_types,
                residual_activations=residual_activations or None,  # F055
            )
            text = _format_pipeline_text(results, stats, search_types)

            # F055: fire-and-forget record_surfaced so the request returns
            # immediately. record_surfaced opens its OWN DB session inside
            # the WorkingMemoryManager helper (spec §3 fix #2 — must NOT
            # reuse the caller's AsyncSession across asyncio.create_task).
            if (
                results
                and getattr(settings, "residual_activation_enabled", False)
                and _session_id
                and getattr(heart, "_residual_activator", None) is not None
            ):
                try:
                    import asyncio
                    surfaced = [
                        (r.id, r.type, float(r.score) if r.score is not None else 0.0)
                        for r in results
                    ]
                    asyncio.create_task(
                        heart._residual_activator.record_surfaced(
                            agent_id=brain.agent_id,
                            session_id=_session_id,
                            current_turn=current_turn + 1,  # this turn becomes turn N+1
                            surfaced=surfaced,
                        )
                    )
                except Exception:
                    logger.warning("F055: failed to schedule record_surfaced", exc_info=True)

            return {"content": [{"type": "text", "text": text}]}
        except Exception as e:
            logger.exception("recall_deep tool failed")
            return {"content": [{"type": "text", "text": f"Error searching memory: {e}"}]}

    async def create_censor(
        trigger_pattern: str,
        reason: str,
        action: str = "warn",
        domain: str | None = None,
        learned_from_decision: str | None = None,
        learned_from_episode: str | None = None,
    ) -> dict[str, Any]:
        """Create a guardrail censor in the Heart.

        Args:
            trigger_pattern: Pattern to match (substring or regex)
            reason: Why this censor exists
            action: warn, block, or absolute
            domain: Domain this censor applies to (architecture, debugging, etc.)
            learned_from_decision: Decision UUID that triggered this censor
            learned_from_episode: Episode UUID that triggered this censor

        Returns:
            MCP-compliant response with censor ID or error message
        """
        try:
            # Parse UUIDs if provided
            decision_uuid = UUID(learned_from_decision) if learned_from_decision else None
            episode_uuid = UUID(learned_from_episode) if learned_from_episode else None

            input_data = CensorInput(
                trigger_pattern=trigger_pattern,
                reason=reason,
                action=action,  # type: ignore
                domain=domain,
                learned_from_decision=decision_uuid,
                learned_from_episode=episode_uuid,
            )

            # Create censor
            result = await heart.add_censor(input_data)

            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Censor created successfully.\n"
                            f"ID: {result.id}\n"
                            f"Action: {result.action}\n"
                            f"Domain: {result.domain or 'all'}\n"
                            f"Pattern: {result.trigger_pattern}"
                        ),
                    }
                ]
            }

        except ValueError as e:
            # UUID parsing error or validation error
            return {"content": [{"type": "text", "text": f"Validation error: {e}"}]}
        except Exception as e:
            logger.exception("create_censor tool failed")
            return {"content": [{"type": "text", "text": f"Error creating censor: {e}"}]}

    async def recall_recent(
        hours: int = 48,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Recall recent episodes by time, not topic similarity.

        Use this when the user asks "what did we talk about", "what happened
        recently", or you need a comprehensive overview of recent activity.

        Args:
            hours: Look back this many hours (default 48)
            limit: Maximum episodes to return (default 10)

        Returns:
            MCP-compliant response with time-ordered episode list
        """
        try:
            episodes = await heart.list_episodes(limit=limit, hours=hours)

            if not episodes:
                return {"content": [{"type": "text", "text": f"No episodes found in the last {hours} hours."}]}

            lines = [f"Recent episodes (last {hours}h):"]
            for e in episodes:
                title = e.title or (e.summary[:60] if e.summary else "Untitled")
                time_str = e.started_at.strftime("%b %d %H:%M")
                lines.append(f"- [{time_str}] {title}")
                if e.summary and e.summary != e.title:
                    lines.append(f"  {e.summary[:150]}")

            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        except Exception as e:
            logger.exception("recall_recent tool failed")
            return {"content": [{"type": "text", "text": f"Error fetching recent episodes: {e}"}]}

    # F011: learn_skill tool — register skills from URL, local path, or inline markdown
    _skill_parser = SkillParser()

    async def learn_skill(
        source: str,
        content: str | None = None,
    ) -> dict[str, Any]:
        """Register a skill from a URL, local path, or raw markdown.

        Args:
            source: URL, local file path, or 'inline' for raw content
            content: Raw SKILL.md markdown when source is 'inline'

        Returns:
            MCP-compliant response with skill registration result
        """
        try:
            # 1. Fetch content
            if source == "inline":
                if not content:
                    return {"content": [{"type": "text", "text": "Error: 'content' is required when source is 'inline'"}]}
                markdown = content
            elif source.startswith(("http://", "https://")):
                # Fetch from URL
                import httpx as _httpx
                async with _httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(source)
                    resp.raise_for_status()
                    markdown = resp.text
            else:
                # Local file path
                import os
                workspace = settings.workspace_dir if settings else "."
                path = os.path.join(workspace, source) if not os.path.isabs(source) else source
                if not os.path.exists(path):
                    return {"content": [{"type": "text", "text": f"Error: file not found: {path}"}]}
                with open(path, encoding="utf-8") as f:
                    markdown = f.read()

            # 2. Parse
            manifest = _skill_parser.parse(markdown, source_hint=source)

            # 2b. Check requires (env var validation)
            import os as _os
            missing_requires = [var for var in manifest.requires if not _os.environ.get(var)]
            skill_active = len(missing_requires) == 0

            # 3. Check for existing procedure with same name (dedup)
            existing = await heart.get_procedure_by_name(manifest.name)
            updated = False
            if existing:
                await heart.retire_procedure(existing.id)
                updated = True

            # 4. Convert to ProcedureInput and store
            proc_input = _skill_parser.to_procedure_input(manifest)
            if not skill_active:
                proc_input.active = False
            result = await heart.store_procedure(proc_input)

            action = "updated" if updated else "registered"
            if not skill_active:
                active_str = f"inactive (missing: {', '.join(missing_requires)})"
            else:
                active_str = "active"
            warnings_text = ""
            if manifest.warnings:
                warnings_text = "\nWarnings:\n" + "\n".join(f"  - {w}" for w in manifest.warnings)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Skill {action} successfully.\n"
                            f"Name: {manifest.name}\n"
                            f"ID: {result.id}\n"
                            f"Domain: {manifest.domain}\n"
                            f"Status: {active_str}\n"
                            f"Triggers: {', '.join(manifest.triggers) if manifest.triggers else 'none'}"
                            f"{warnings_text}"
                        ),
                    }
                ]
            }

        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Parse error: {e}"}]}
        except Exception as e:
            logger.exception("learn_skill tool failed")
            return {"content": [{"type": "text", "text": f"Error learning skill: {e}"}]}

    async def get_procedure(
        procedure_id: str,
    ) -> dict[str, Any]:
        """Fetch full procedure/skill details by ID.

        Use after recall_deep returns a procedure result to read the full
        skill body, triggers, tools, and implementation notes.

        Args:
            procedure_id: UUID of the procedure (from recall_deep results)

        Returns:
            MCP-compliant response with full procedure details
        """
        try:
            from uuid import UUID as _UUID
            pid = _UUID(procedure_id)
            detail = await heart.get_procedure(pid)

            lines = [
                f"**{detail.name}** ({detail.domain or 'general'})",
            ]
            if detail.description:
                lines.append(f"Description: {detail.description}")
            if detail.goals:
                lines.append(f"Triggers: {', '.join(detail.goals)}")
            if detail.core_tools:
                lines.append(f"Tools: {', '.join(detail.core_tools)}")
            if detail.implementation_notes:
                lines.append("")
                for note in detail.implementation_notes:
                    lines.append(note)
            lines.append(f"\nActivated: {detail.activation_count}x | Status: {'active' if detail.active else 'inactive'}")
            if detail.effectiveness is not None:
                lines.append(f"Effectiveness: {detail.effectiveness:.0%}")

            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}]}
        except Exception as e:
            logger.exception("get_procedure tool failed")
            return {"content": [{"type": "text", "text": f"Error fetching procedure: {e}"}]}

    return {
        "record_decision": record_decision,
        "learn_fact": learn_fact,
        "recall_deep": recall_deep,
        "create_censor": create_censor,
        "recall_recent": recall_recent,
        "learn_skill": learn_skill,
        "get_procedure": get_procedure,
    }


# ---------------------------------------------------------------------------
# Tool schema definitions (Anthropic API format)
# ---------------------------------------------------------------------------

# JSON Schema definitions for each tool's input parameters.
# Field names match the closure parameter names exactly.

_RECORD_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Record a decision to the Brain (decision intelligence organ)",
    "properties": {
        "description": {"type": "string", "description": "What was decided"},
        "confidence": {
            "type": "number",
            "description": "0.0-1.0 confidence level",
            "minimum": 0.0,
            "maximum": 1.0,
        },
        "category": {
            "type": "string",
            "description": "Decision category",
            "enum": ["architecture", "process", "tooling", "security", "integration"],
        },
        "stakes": {
            "type": "string",
            "description": "Stakes level",
            "enum": ["low", "medium", "high", "critical"],
        },
        "context": {"type": "string", "description": "Situation and constraints"},
        "pattern": {"type": "string", "description": "Abstract pattern this decision represents"},
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Keywords for filtering",
        },
        "reasons": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [
                            "analysis",
                            "pattern",
                            "empirical",
                            "authority",
                            "analogy",
                            "intuition",
                            "elimination",
                            "constraint",
                        ],
                    },
                    "text": {"type": "string"},
                },
                "required": ["type", "text"],
            },
            "description": "Supporting reasons",
        },
    },
    "required": ["description", "confidence", "category", "stakes"],
}

_LEARN_FACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Store a fact in the Heart (memory system)",
    "properties": {
        "content": {"type": "string", "description": "The fact content"},
        "category": {
            "type": "string",
            "description": "Fact category",
            "enum": ["preference", "technical", "person", "tool", "concept", "rule"],
        },
        "subject": {"type": "string", "description": "What/who the fact is about"},
        "confidence": {
            "type": "number",
            "description": "0.0-1.0 confidence level",
            "minimum": 0.0,
            "maximum": 1.0,
            "default": 1.0,
        },
        "source": {"type": "string", "description": "Where this fact came from"},
        "source_episode_id": {"type": "string", "description": "Episode UUID"},
        "source_decision_id": {"type": "string", "description": "Decision UUID"},
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Keywords for filtering",
        },
    },
    "required": ["content"],
}

_RECALL_DEEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Search across all memory types in Heart and Brain",
    "properties": {
        "query": {"type": "string", "description": "Search query string"},
        "limit": {
            "type": "integer",
            "description": "Maximum results to return",
            "default": 10,
            "minimum": 1,
            "maximum": 50,
        },
        "memory_types": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["all", "episode", "fact", "procedure", "censor", "decision"],
            },
            "description": "Types to search. If omitted or contains 'all', searches everything.",
        },
    },
    "required": ["query"],
}

_CREATE_CENSOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Create a guardrail censor in the Heart",
    "properties": {
        "trigger_pattern": {
            "type": "string",
            "description": "Pattern to match (substring or regex)",
        },
        "reason": {"type": "string", "description": "Why this censor exists"},
        "action": {
            "type": "string",
            "description": "Censor action",
            "enum": ["warn", "block", "absolute"],
            "default": "warn",
        },
        "domain": {"type": "string", "description": "Domain this censor applies to"},
        "learned_from_decision": {"type": "string", "description": "Decision UUID"},
        "learned_from_episode": {"type": "string", "description": "Episode UUID"},
    },
    "required": ["trigger_pattern", "reason"],
}

_RECALL_RECENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Recall recent episodes by time (not topic similarity). Use when the user asks what you discussed recently or you need a temporal overview.",
    "properties": {
        "hours": {
            "type": "integer",
            "description": "Look back this many hours (default 48)",
            "default": 48,
        },
        "limit": {
            "type": "integer",
            "description": "Maximum episodes to return (default 10)",
            "default": 10,
        },
    },
    "required": [],
}


_LEARN_SKILL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Register a skill from a URL, local file path, or raw markdown. "
        "Skills are stored as procedures and auto-activate during recall when relevant to the task."
    ),
    "properties": {
        "source": {
            "type": "string",
            "description": (
                "URL (https://...), local file path relative to workspace, "
                "or 'inline' for raw content"
            ),
        },
        "content": {
            "type": "string",
            "description": "Raw SKILL.md markdown when source is 'inline'",
        },
    },
    "required": ["source"],
}

_GET_PROCEDURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Fetch full procedure/skill details by ID. Use after recall_deep returns a "
        "procedure result to read the full skill body, triggers, tools, and instructions."
    ),
    "properties": {
        "procedure_id": {
            "type": "string",
            "description": "UUID of the procedure (from recall_deep results metadata)",
        },
    },
    "required": ["procedure_id"],
}


def register_nous_tools(dispatcher: ToolDispatcher, brain: Brain, heart: Heart, settings: Settings | None = None) -> None:
    """Create Nous memory tools and register them with the dispatcher.

    This is the main wiring function called at startup to register
    all memory tools with their schemas.
    """
    closures = create_nous_tools(brain, heart, settings=settings)

    dispatcher.register("record_decision", closures["record_decision"], _RECORD_DECISION_SCHEMA)
    dispatcher.register("learn_fact", closures["learn_fact"], _LEARN_FACT_SCHEMA)
    dispatcher.register("recall_deep", closures["recall_deep"], _RECALL_DEEP_SCHEMA)
    dispatcher.register("create_censor", closures["create_censor"], _CREATE_CENSOR_SCHEMA)
    dispatcher.register("recall_recent", closures["recall_recent"], _RECALL_RECENT_SCHEMA)
    dispatcher.register("learn_skill", closures["learn_skill"], _LEARN_SKILL_SCHEMA)
    dispatcher.register("get_procedure", closures["get_procedure"], _GET_PROCEDURE_SCHEMA)


# ---------------------------------------------------------------------------
# F020: cache_retrieve tool
# ---------------------------------------------------------------------------

_CACHE_RETRIEVE_SCHEMA = {
    "description": "Retrieve original content from a previously compressed search or fetch result. Use when you see a [SmartCompressed] marker and need more detail.",
    "type": "object",
    "properties": {
        "hash_key": {
            "type": "string",
            "description": "The hash key from the [SmartCompressed] marker (e.g., 'abc123de01234567')",
        },
        "query": {
            "type": "string",
            "description": "Optional: return only items matching this query instead of everything.",
        },
    },
    "required": ["hash_key"],
}

CACHE_RETRIEVE_TOOL_DEF = {
    "name": "cache_retrieve",
    "description": _CACHE_RETRIEVE_SCHEMA["description"],
    "input_schema": _CACHE_RETRIEVE_SCHEMA,
}


def register_cache_retrieve_tool(
    dispatcher: ToolDispatcher,
    db_session_factory,
) -> None:
    """Register the cache_retrieve tool (F020)."""
    from nous.api.tool_cache import retrieve_cached_result

    async def _cache_retrieve(
        hash_key: str, query: str | None = None, session_id: str | None = None, **kwargs,
    ) -> dict:
        if not session_id:
            return {"content": [{"type": "text", "text": "Error: no active session for cache lookup."}]}
        try:
            async with db_session_factory() as db_sess:
                result = await retrieve_cached_result(db_sess, session_id, hash_key, query)
                if result is None:
                    text = f"No cached result found for hash key '{hash_key}'. It may have expired or the key may be incorrect."
                else:
                    text = result
                return {"content": [{"type": "text", "text": text}]}
        except Exception as e:
            logger.exception("cache_retrieve error")
            return {"content": [{"type": "text", "text": f"Error retrieving cached result: {e}"}]}

    dispatcher.register("cache_retrieve", _cache_retrieve, _CACHE_RETRIEVE_SCHEMA)


# ---------------------------------------------------------------------------
# Subtask prefix builder (012.2)
# ---------------------------------------------------------------------------


_F061_DEFAULT_SUCCESS_CRITERIA = (
    "The summary directly addresses the task and is internally consistent."
)
_F061_DEFAULT_BOUNDARIES = (
    "Do not spawn further subtasks. Do not modify files unless the task "
    "explicitly requires it. Cap tool calls per the runner limit."
)
_F061_FRAME_OUTPUT_FORMATS = {
    "task":         "Concise summary of what was done + verification of success.",
    "research":     "Synthesis of findings, with key facts in `findings[]` and sources in `evidence_refs[]`.",
    "decision":     "Decision recommendation + reasoning + confidence; record the decision via record_decision and reference its ID in `evidence_refs[]`.",
    "debug":        "Root cause + fix suggestion + verification steps.",
    "conversation": "Direct natural-language answer to the question.",
}


def _f061_default_output_format(frame_type: str | None) -> str:
    return _F061_FRAME_OUTPUT_FORMATS.get(
        frame_type or "", "Free-form summary appropriate to the task."
    )


def build_subtask_prefix(
    task: str,
    frame_type: str | None = None,
    *,
    output_format: str | None = None,
    success_criteria: str | None = None,
    boundaries: str | None = None,
    hardening_enabled: bool = False,
    payload_schema: dict | None = None,
) -> str:
    """Build a system prompt prefix for subtask execution.

    Used by both inline (await_result) and background worker subtask execution
    to ensure consistent frame-aware context assembly.

    When ``hardening_enabled=False`` (default), emits the legacy text —
    byte-identical to pre-F061. PR-6 will remove this branch once the flag
    is locked on. When ``hardening_enabled=True``, emits the F061 four-part
    brief + termination contract (objective + output_format + success_criteria
    + boundaries) and instructs the agent to terminate via the
    ``submit_final_report`` tool.

    F062: when ``payload_schema`` is non-None (and hardening_enabled is true),
    appends a "Result schema (REQUIRED)" block instructing the model to
    populate ``submit_final_report``'s ``payload`` field with data that
    validates against the supplied JSON Schema. Ignored when hardening is off
    — the legacy text path does not register ``submit_final_report`` at all.
    """
    from nous.api.runner import FRAME_TOOLS

    if not hardening_enabled:
        # LEGACY — DO NOT MODIFY. PR-6 deletes this branch.
        base = (
            "You are executing a background subtask.\n"
            "Deliver a clear, complete result. Do not ask questions."
        )
        frame_instruction = ""
        if frame_type and frame_type in FRAME_TOOLS:
            frame_instruction = (
                f"\n\nFrame: {frame_type} — apply {frame_type}-appropriate "
                "reasoning and tool usage."
            )
        return f"{base}{frame_instruction}\n\nTask: {task}"

    of = output_format or _f061_default_output_format(frame_type)
    sc = success_criteria or _F061_DEFAULT_SUCCESS_CRITERIA
    bd = boundaries or _F061_DEFAULT_BOUNDARIES
    # Render the Frame block for any non-empty frame name (including
    # 'research' which is not in FRAME_TOOLS). The Frame block is
    # informational; FRAME_TOOLS gates only tool availability.
    frame_block = ""
    if frame_type:
        frame_block = (
            f"\n# Frame\n{frame_type} — apply {frame_type}-appropriate "
            "reasoning and tool usage.\n"
        )
    # F062: when a payload schema was supplied, append a result-schema block
    # that the model populates via submit_final_report's optional `payload`
    # field. Compact JSON to keep prompt token spend bounded.
    payload_schema_block = ""
    if payload_schema is not None:
        import json as _json
        compact_schema = _json.dumps(payload_schema, separators=(",", ":"))
        payload_schema_block = (
            "\n# Result schema (REQUIRED)\n"
            "When you call submit_final_report, the `payload` field MUST be "
            "a JSON value that validates against this schema. Use the "
            "schema's property names exactly; do not invent keys.\n\n"
            "<schema>\n"
            f"{compact_schema}\n"
            "</schema>\n"
        )

    return (
        "You are a Nous subtask agent. Your ONLY way to terminate is to call "
        "the submit_final_report tool with a schema-valid payload.\n\n"
        f"# Objective\n{task}\n\n"
        f"# Output format\n{of}\n\n"
        f"# Success criteria\n{sc}\n\n"
        f"# Boundaries\n{bd}\n"
        f"{frame_block}"
        f"{payload_schema_block}\n"
        "# Termination\n"
        "When you are done — and ONLY when done — call submit_final_report. "
        "Do not produce a final text-only response; the parent agent will "
        "read only the report payload. If you genuinely cannot complete the "
        "task, call submit_final_report with incomplete=true and a specific "
        "blocked_reason."
    )


async def _persist_and_emit_inline_outcome(
    *,
    heart: Heart,
    bus: object,
    settings: "Settings",
    subtask: Any,
    final_outcome: str,
    error_msg: str,
    state: Any,
) -> None:
    """F061 round 4: inline path's outer timeout/exception handler must
    persist accurate attempts + token counts AND emit a subtask_outcome
    event. Mirrors ``SubtaskWorkerPool._emit_outcome_from_outer`` for the
    inline ``await_result=true`` path.
    """
    attempts = max(1, getattr(state, "attempts", 0) or 0)
    tokens_in = getattr(state, "tokens_in", 0)
    tokens_out = getattr(state, "tokens_out", 0)
    tool_calls_made = getattr(state, "tool_calls_made", 0)

    await heart.subtasks.fail(
        subtask.id, error_msg,
        final_outcome=final_outcome,
        attempts=attempts,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tool_calls_made=tool_calls_made,
    )

    if bus is None or not settings.subtask_outcome_persistence_enabled:
        return
    try:
        from nous.events import Event
        from nous.handlers.subtask_executor import SUBTASK_OUTCOME_EVENT_TYPE

        data: dict[str, Any] = {
            "subtask_id": str(subtask.id),
            "agent_id": getattr(subtask, "agent_id", None),
            "frame_type": getattr(subtask, "frame_type", None),
            "final_outcome": final_outcome,
            "ok": False,
            "validator_reason": error_msg,
            "attempts": attempts,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tool_calls_made": tool_calls_made,
            "duration_ms": None,
            "dag_node_id": (
                str(subtask.dag_node_id)
                if getattr(subtask, "dag_node_id", None) is not None
                else None
            ),
        }
        await bus.emit(Event(
            type=SUBTASK_OUTCOME_EVENT_TYPE,
            agent_id=getattr(subtask, "agent_id", "") or settings.agent_id,
            session_id=f"subtask-{subtask.id.hex[:8]}",
            data=data,
        ))
    except Exception:
        logger.exception(
            "Failed to emit subtask_outcome from inline outer handler for %s",
            subtask.id.hex[:8],
        )


# ---------------------------------------------------------------------------
# Subtask & Schedule tool closures (011.1)
# ---------------------------------------------------------------------------

def create_subtask_tools(
    heart: Heart,
    settings: "Settings",
    runner: object = None,
    bus: object = None,
) -> dict[str, Any]:
    """Create subtask/schedule tool closures with Heart captured in closure context.

    Returns a dict of async callables suitable for ToolDispatcher registration.
    The optional ``runner`` param enables inline (await_result) subtask execution.
    The optional ``bus`` param (F061 PR-3) is the EventBus the inline hardened
    path uses to emit ``subtask_outcome`` telemetry; ``None`` disables that
    emission for inline subtasks (background subtasks emit via the worker pool).
    """
    from nous.config import Settings as _Settings  # noqa: F811 — deferred to avoid circular

    # F061 PR-3 silent-failure review P2.1: warn loudly when inline telemetry
    # is silently disabled because no bus was wired through. Operator who
    # forgets to pass bus= would otherwise see dashboard rows for inline
    # subtasks without subtask_outcome events for them.
    if (
        bus is None
        and getattr(settings, "subtask_outcome_persistence_enabled", True)
        and getattr(settings, "subtask_hardening_enabled", False)
    ):
        logger.warning(
            "F061 PR-3: inline subtask telemetry disabled (bus=None passed to "
            "create_subtask_tools). subtask_outcome events from "
            "await_result=True path will be lost."
        )

    async def spawn_task(
        task: str,
        priority: str = "normal",
        timeout: int | None = None,
        notify: bool = False,
        frame_type: str | None = None,
        await_result: bool = False,
        model: str | None = None,
        # F061: 2 of 4 brief fields are persisted. ``boundaries`` is
        # intentionally NOT a parameter — it has no DB column and would
        # silently drop. Fold boundary constraints into ``task`` until a
        # follow-up PR adds storage. Worker / executor synthesize frame-
        # derived defaults at execute time when these are None.
        output_format: str | None = None,
        success_criteria: str | None = None,
        # F062: optional JSON Schema for the result payload. Only honored
        # when settings.subtask_payload_schema_enabled is True; otherwise
        # silently dropped (the property is also gated out of
        # _SPAWN_TASK_SCHEMA so the model can't see it). Persisted on the
        # subtask row even when dropped at execute time — operators can
        # inspect via /dashboard/subtasks.
        payload_schema: dict | None = None,
        _session_id: str | None = None,
    ) -> dict[str, Any]:
        """Spawn a subtask, optionally waiting for its result inline.

        Args:
            task: Natural-language instruction for the subtask
            priority: urgent, normal, or low
            timeout: Max seconds (clamped to settings.subtask_max_timeout)
            notify: Whether to notify on completion
            frame_type: Cognitive frame for the subtask
            await_result: If true, execute inline and return result
            model: Model override for this subtask

        Returns:
            MCP-compliant response with subtask ID or inline result
        """
        try:
            # 012.2: Apply frame-default model mapping
            effective_model = model
            if not effective_model and frame_type:
                effective_model = settings.frame_default_models.get(frame_type)
            if not effective_model:
                effective_model = settings.background_model

            # 012.2: Differentiate timeout defaults
            if await_result:
                effective_timeout = min(
                    timeout or settings.inline_subtask_timeout,
                    settings.subtask_max_timeout,
                )
            else:
                effective_timeout = min(
                    timeout or settings.subtask_default_timeout,
                    settings.subtask_max_timeout,
                )

            # F031: Check censors on subtask task text at creation time.
            # Subtasks are non-interactive so censor checks are skipped during
            # execution (pre_turn). We check here instead for immediate feedback.
            try:
                from nous.heart.censor_actions import CensorActionExecutor
                matches = await heart.check_censors(task)
                for match in matches:
                    if match.action == "block":
                        # F031: Check unblock condition before rejecting
                        unblocked = False
                        if match.trigger_action and match.unblock_pattern:
                            executor = CensorActionExecutor(heart)
                            action_result = await executor.execute(match.trigger_action)
                            if action_result:
                                import re
                                try:
                                    if re.search(match.unblock_pattern, action_result, re.IGNORECASE):
                                        unblocked = True
                                except re.error:
                                    pass
                        if not unblocked:
                            reason = match.reason or match.trigger_pattern
                            msg = f"Subtask rejected by censor: {reason}"
                            if match.action_instruction:
                                msg += f"\n{match.action_instruction}"
                            return {"content": [{"type": "text", "text": msg}]}
                    elif match.action == "warn":
                        logger.info("Censor WARN on subtask creation: %s", match.trigger_pattern)
            except Exception:
                logger.debug("Censor check failed during spawn_task, proceeding")

            # F062: only persist payload_schema when the flag is on.
            # Operators flipping the flag off should not see stale schemas
            # take effect on new rows even if a caller passes one through
            # the (un-exposed) kwarg.
            effective_payload_schema = (
                payload_schema if settings.subtask_payload_schema_enabled else None
            )
            subtask = await heart.subtasks.create(
                task=task,
                priority=priority,
                timeout=effective_timeout,
                notify=notify,
                parent_session_id=_session_id,
                frame_type=frame_type,
                model=effective_model,
                # F061: persist the four-part brief for the executor.
                output_format=output_format,
                success_criteria=success_criteria,
                # F062: caller-supplied JSON Schema for the result payload.
                payload_schema=effective_payload_schema,
            )

            if not await_result:
                # Fire-and-forget (existing behavior)
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Subtask spawned.\n"
                                f"ID: {subtask.id}\n"
                                f"Priority: {priority}\n"
                                f"Timeout: {effective_timeout}s"
                            ),
                        }
                    ]
                }

            # 012.2: Synchronous inline execution
            if runner is None:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": "Cannot execute inline subtask: runner not available. Use await_result=false.",
                        }
                    ]
                }

            import asyncio as _asyncio

            subtask_session_id = f"subtask-{subtask.id.hex[:8]}"

            # F061: route inline path through the SAME hardened executor as
            # the worker pool when the flag is on. Closes the silent-failure
            # gap (P1.1 from spec review): otherwise the new prompt would
            # mention submit_final_report on a path where that tool isn't
            # registered, relocating the exact failure F061 fixes.
            if settings.subtask_hardening_enabled:
                # Late import: nous.handlers.subtask_executor imports
                # build_subtask_prefix from THIS module — top-level import
                # would deadlock at startup. (functools.partial is stdlib
                # and circular-safe; imported at module top.)
                from nous.handlers.subtask_executor import (
                    HardenedRunState,
                    emit_outcome_event,
                    execute_hardened,
                )

                # F061 PR-3: pass an emit_event callback so inline subtasks
                # also produce subtask_outcome telemetry. ``bus`` is captured
                # by the outer create_subtask_tools closure when available.
                _outcome_emitter = (
                    partial(emit_outcome_event, bus, settings=settings)
                    if bus is not None else None
                )

                # F061 round 4: HardenedRunState side channel so the timeout
                # / exception handlers below can read accurate attempts +
                # token counts, persist them on the row, AND emit a
                # subtask_outcome event (execute_hardened skips persist+emit
                # on cancel — outer handler is the authoritative classifier).
                state = HardenedRunState()

                # `executed` flag prevents double-persist on programming-
                # error in the response-formatting code below: if any
                # AttributeError leaks from the body builder after
                # execute_hardened has already persisted via _persist_outcome,
                # the outer `except Exception` below would overwrite the row
                # with status='failed' even though it was already 'completed'.
                executed = False
                try:
                    final_text, _result = await _asyncio.wait_for(
                        execute_hardened(
                            subtask, subtask_session_id,
                            runner=runner, heart=heart, settings=settings,
                            emit_event=_outcome_emitter,
                            state=state,
                        ),
                        timeout=effective_timeout,
                    )
                    executed = True
                    if _result.ok:
                        body = (
                            f"[Subtask {subtask.id.hex[:8]} completed]\n\n{final_text}"
                        )
                    elif _result.outcome == "incomplete_blocked":
                        body = (
                            f"[Subtask {subtask.id.hex[:8]} blocked: {_result.reason}]"
                        )
                    else:
                        body = (
                            f"[Subtask {subtask.id.hex[:8]} {_result.outcome}: "
                            f"{_result.reason}]"
                        )
                    return {"content": [{"type": "text", "text": body}]}
                except _asyncio.TimeoutError:
                    if not executed:
                        await _persist_and_emit_inline_outcome(
                            heart=heart, bus=bus, settings=settings,
                            subtask=subtask,
                            final_outcome="timed_out",
                            error_msg=f"Timeout after {effective_timeout}s",
                            state=state,
                        )
                    return {
                        "content": [
                            {
                                "type": "text",
                                "text": f"[Subtask {subtask.id.hex[:8]} timed out after {effective_timeout}s]",
                            }
                        ]
                    }
                except Exception as e:
                    if not executed:
                        await _persist_and_emit_inline_outcome(
                            heart=heart, bus=bus, settings=settings,
                            subtask=subtask,
                            final_outcome="errored",
                            error_msg=str(e),
                            state=state,
                        )
                    return {
                        "content": [
                            {
                                "type": "text",
                                "text": f"[Subtask {subtask.id.hex[:8]} failed: {e}]",
                            }
                        ]
                    }

            # Legacy inline path — bytewise unchanged from pre-F061.
            system_prefix = build_subtask_prefix(task, frame_type)

            try:
                response_text, _ctx, _usage = await _asyncio.wait_for(
                    runner.run_turn(
                        session_id=subtask_session_id,
                        user_message=task,
                        agent_id=settings.agent_id,
                        system_prompt_prefix=system_prefix,
                        skip_episode=True,
                        is_subtask=True,
                        max_tool_calls=settings.subtask_tool_call_limit,
                        model_override=effective_model,
                        is_background=True,
                    ),
                    timeout=effective_timeout,
                )

                # F061 PR-1: record outcome on legacy path so dashboard rows
                # are never NULL between PR-1 ship and PR-2 hardened-executor ship.
                await heart.subtasks.complete(
                    subtask.id, response_text, final_outcome="completed",
                )
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"[Subtask {subtask.id.hex[:8]} completed]\n\n{response_text}",
                        }
                    ]
                }

            except _asyncio.TimeoutError:
                # F061 PR-3 Codex review: attempts=1 because one execution
                # attempt definitely happened before the timeout.
                await heart.subtasks.fail(
                    subtask.id, f"Timeout after {effective_timeout}s",
                    final_outcome="timed_out", attempts=1,
                )
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"[Subtask {subtask.id.hex[:8]} timed out after {effective_timeout}s]",
                        }
                    ]
                }
            except Exception as e:
                await heart.subtasks.fail(
                    subtask.id, str(e), final_outcome="errored", attempts=1,
                )
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"[Subtask {subtask.id.hex[:8]} failed: {e}]",
                        }
                    ]
                }

        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Cannot spawn subtask: {e}"}]}
        except Exception as e:
            logger.exception("spawn_task tool failed")
            return {"content": [{"type": "text", "text": f"Error spawning subtask: {e}"}]}

    async def schedule_task(
        task: str,
        when: str | None = None,
        every: str | None = None,
        notify: bool = False,
        model: str | None = None,
        frame_type: str | None = None,
    ) -> dict[str, Any]:
        """Schedule a task for later or recurring execution.

        Exactly one of ``when`` or ``every`` must be provided.

        Args:
            task: Natural-language instruction
            when: One-shot time (e.g. "in 2 hours", ISO 8601)
            every: Recurring pattern (e.g. "daily at 8am", "30 minutes")
            notify: Whether to notify on each fire

        Returns:
            MCP-compliant response with schedule ID and next fire time
        """
        try:
            if bool(when) == bool(every):
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": "Exactly one of 'when' or 'every' must be provided.",
                        }
                    ]
                }

            from nous.handlers.time_parser import parse_every, parse_when

            if when:
                fire_at = parse_when(when)
                schedule = await heart.schedules.create(
                    task=task,
                    schedule_type="once",
                    fire_at=fire_at,
                    notify=notify,
                    timeout=settings.subtask_default_timeout,
                    model=model,
                    frame_type=frame_type,
                )
            else:
                interval_seconds, cron_expr = parse_every(every)  # type: ignore[arg-type]
                schedule = await heart.schedules.create(
                    task=task,
                    schedule_type="recurring",
                    interval_seconds=interval_seconds,
                    cron_expr=cron_expr,
                    notify=notify,
                    timeout=settings.subtask_default_timeout,
                    model=model,
                    frame_type=frame_type,
                )

            next_fire = (
                schedule.next_fire_at.isoformat() if schedule.next_fire_at else "N/A"
            )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Schedule created.\n"
                            f"ID: {schedule.id}\n"
                            f"Type: {schedule.schedule_type}\n"
                            f"Next fire: {next_fire}"
                        ),
                    }
                ]
            }
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Schedule error: {e}"}]}
        except Exception as e:
            logger.exception("schedule_task tool failed")
            return {"content": [{"type": "text", "text": f"Error scheduling task: {e}"}]}

    async def list_tasks(
        status: str | None = None,
    ) -> dict[str, Any]:
        """List subtasks and schedules.

        Args:
            status: Filter subtasks by status (pending, running, completed, failed, cancelled)

        Returns:
            MCP-compliant response with formatted task list
        """
        try:
            subtasks = await heart.subtasks.list(status=status, limit=20)
            schedules = await heart.schedules.list(active_only=True, limit=20)

            lines: list[str] = []

            if subtasks:
                lines.append("=== Subtasks ===")
                for st in subtasks:
                    line = f"- [{st.status}] {st.id} | {st.task[:80]}"
                    if st.status == "completed" and st.result:
                        line += f"\n  Result: {st.result}"
                    elif st.status == "failed" and st.error:
                        line += f"\n  Error: {st.error}"
                    lines.append(line)
            else:
                lines.append("=== Subtasks ===\nNo subtasks found.")

            if schedules:
                lines.append("\n=== Schedules ===")
                for sc in schedules:
                    next_fire = (
                        sc.next_fire_at.strftime("%Y-%m-%d %H:%M UTC")
                        if sc.next_fire_at
                        else "N/A"
                    )
                    lines.append(
                        f"- [{sc.schedule_type}] {sc.id} | {sc.task[:80]} (next: {next_fire})"
                    )
            else:
                lines.append("\n=== Schedules ===\nNo active schedules.")

            return {"content": [{"type": "text", "text": "\n".join(lines)}]}
        except Exception as e:
            logger.exception("list_tasks tool failed")
            return {"content": [{"type": "text", "text": f"Error listing tasks: {e}"}]}

    async def cancel_task(
        task_id: str,
    ) -> dict[str, Any]:
        """Cancel a subtask or deactivate a schedule by ID.

        Args:
            task_id: UUID of the subtask or schedule to cancel

        Returns:
            MCP-compliant response confirming cancellation or error
        """
        try:
            from uuid import UUID as _UUID

            uid = _UUID(task_id)

            # Try subtask cancel first
            cancelled = await heart.subtasks.cancel(uid)
            if cancelled:
                return {
                    "content": [
                        {"type": "text", "text": f"Subtask {task_id} cancelled."}
                    ]
                }

            # Try schedule deactivation
            schedule = await heart.schedules.get(uid)
            if schedule:
                await heart.schedules.deactivate(uid)
                return {
                    "content": [
                        {"type": "text", "text": f"Schedule {task_id} deactivated."}
                    ]
                }

            return {
                "content": [
                    {"type": "text", "text": f"No pending subtask or active schedule found for {task_id}."}
                ]
            }
        except ValueError:
            return {"content": [{"type": "text", "text": f"Invalid task ID: {task_id}"}]}
        except Exception as e:
            logger.exception("cancel_task tool failed")
            return {"content": [{"type": "text", "text": f"Error cancelling task: {e}"}]}

    # F062: spawn_sync — typed counterpart to spawn_task(await_result=True).
    # Returns SubtaskResult.to_dict() as a JSON blob in the content. The
    # spawn_task fire-and-forget contract and legacy string return stay
    # untouched; spawn_sync is the new entry-point for callers that want
    # a structured, schema-validated payload.
    async def spawn_sync(
        task: str,
        payload_schema: dict | None = None,
        frame_type: str | None = None,
        timeout_seconds: int | None = None,
        model: str | None = None,
        success_criteria: str | None = None,
        _session_id: str | None = None,
    ) -> dict[str, Any]:
        import json as _json
        import uuid as _uuid

        from nous.api.models import SubtaskResult

        # Generate a unique per-spawn_sync session id so the row lookup
        # below can find the subtask by parent_session_id — race-free
        # regardless of how many other subtasks land between create and
        # lookup. (heart.subtasks.list is fine but bounded; this is
        # unbounded-correct under any concurrency.)
        sync_session_id = f"spawn_sync-{_uuid.uuid4().hex[:12]}"

        # spawn_sync always blocks inline. Reuse spawn_task's plumbing so
        # the censor / hardened-executor / inline-outcome paths all stay
        # in one place. spawn_task already enforces effective_payload_schema
        # gating on settings.subtask_payload_schema_enabled.
        raw = await spawn_task(
            task=task,
            priority="normal",
            timeout=timeout_seconds,
            notify=False,
            frame_type=frame_type,
            await_result=True,
            model=model,
            output_format=None,
            success_criteria=success_criteria,
            payload_schema=payload_schema,
            _session_id=sync_session_id,
        )

        # spawn_task always returns {"content": [{"type":"text","text":...}]}.
        text = ""
        try:
            text = raw["content"][0]["text"]
        except (KeyError, IndexError, TypeError):
            text = ""

        # Race-free lookup by exact parent_session_id (Codex round-5 P1).
        # The earlier scan-with-limit was fragile under load — a burst of
        # concurrent subtasks could push the row out of the recency window
        # and silently misreport the result as "errored". Direct query is
        # both race-free and unbounded.
        match = await heart.subtasks.get_by_parent_session(sync_session_id)
        if match is None:
            # Censor-rejection path or runner-unavailable path — no row
            # was created at all. Return a degraded but typed result.
            result = SubtaskResult(
                task_id="",
                status="errored",
                payload={},
                raw_text=text,
                confidence=None,
                elapsed_seconds=0.0,
                validator_reason="spawn_sync: no subtask row created (censor or runner unavailable)",
            )
            return {
                "content": [
                    {"type": "text", "text": _json.dumps(result.to_dict(), indent=2)}
                ]
            }

        report = match.report_jsonb or {}
        elapsed = 0.0
        if match.completed_at and match.started_at:
            elapsed = (match.completed_at - match.started_at).total_seconds()

        # status mirrors final_outcome (Codex round-2 P1 invariant). Fall back
        # to legacy `status` only if final_outcome is somehow NULL — that
        # signals a pre-F061 row, which spawn_sync should never hit but we
        # surface as "errored" for safety rather than guessing.
        if match.final_outcome:
            status: Any = match.final_outcome
        else:
            status = "errored"

        # Payload contract:
        #   status == "completed"        → payload = the validated value from
        #                                  the report. Preserves explicit
        #                                  None (e.g., schema {"type":"null"})
        #                                  vs. unset (no payload key in
        #                                  report) — the former is a valid
        #                                  schema-typed result and must NOT
        #                                  be coerced to {} (Codex round-4
        #                                  P2). Only fall back to {} when
        #                                  the `payload` key was never set.
        #   any other terminal outcome   → payload = {} (do NOT surface a
        #                                  schema-invalid payload that a
        #                                  caller might accidentally consume
        #                                  thinking it validated).
        # raw_text + validator_reason carry the diagnostic info if the
        # caller actually wants to inspect the failed payload.
        _UNSET = object()
        if status == "completed":
            payload_or_unset: Any = (
                report.get("payload", _UNSET)
                if isinstance(report, dict)
                else _UNSET
            )
            if payload_or_unset is _UNSET:
                payload = {}
            else:
                payload = payload_or_unset
        else:
            payload = {}

        confidence = None
        if isinstance(report, dict):
            c = report.get("confidence")
            if isinstance(c, (int, float)):
                confidence = float(c)

        validator_reason = match.error if match.error else None

        result = SubtaskResult(
            task_id=str(match.id),
            status=status,
            payload=payload,
            raw_text=text,
            confidence=confidence,
            elapsed_seconds=elapsed,
            validator_reason=validator_reason,
        )
        return {
            "content": [
                {"type": "text", "text": _json.dumps(result.to_dict(), indent=2)}
            ]
        }

    return {
        "spawn_task": spawn_task,
        "schedule_task": schedule_task,
        "list_tasks": list_tasks,
        "cancel_task": cancel_task,
        "spawn_sync": spawn_sync,
    }


# ---------------------------------------------------------------------------
# Subtask & Schedule tool schemas (Anthropic API format)
# ---------------------------------------------------------------------------

_SPAWN_TASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Spawn a subtask. Use await_result=true to wait for the result inline, or leave false for fire-and-forget background execution.",
    "properties": {
        "task": {
            "type": "string",
            "description": "Natural-language instruction for the subtask",
        },
        "priority": {
            "type": "string",
            "description": "Task priority",
            "enum": ["urgent", "normal", "low"],
            "default": "normal",
        },
        "timeout": {
            "type": "integer",
            "description": "Max execution seconds (clamped to server max)",
            "minimum": 10,
        },
        "notify": {
            "type": "boolean",
            "description": "Notify on completion",
            "default": False,
        },
        "frame_type": {
            "type": "string",
            "description": "Cognitive frame for the subtask. If omitted, auto-detected.",
            "enum": ["task", "research", "conversation", "decision", "debug"],
        },
        "await_result": {
            "type": "boolean",
            "description": "If true, wait for subtask completion and return result inline. Default false (fire-and-forget).",
            "default": False,
        },
        "model": {
            "type": "string",
            "description": "Model to use for this subtask. If omitted, uses default background model. Use a smaller model for fast lookup/summarization tasks.",
        },
        # F061: 2 of 4 brief fields exposed via schema (output_format,
        # success_criteria). The 4th field "boundaries" is NOT exposed yet
        # — it has no persistence path so would silently drop. Fold
        # boundary constraints into the ``task`` string until a follow-up
        # PR adds a column.
        "output_format": {
            "type": "string",
            "description": "How the subtask should structure its final report. Optional; sensible default applied per frame_type.",
        },
        "success_criteria": {
            "type": "string",
            "description": "What 'done' looks like. Optional; default 'summary directly addresses the task and is internally consistent'.",
        },
    },
    "required": ["task"],
}

_SCHEDULE_TASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Schedule a task for later or recurring execution. Provide exactly one of 'when' or 'every'.",
    "properties": {
        "task": {
            "type": "string",
            "description": "Natural-language instruction for the task",
        },
        "when": {
            "type": "string",
            "description": "One-shot time: 'in 2 hours', ISO 8601, or natural language",
        },
        "every": {
            "type": "string",
            "description": "Recurring pattern: 'daily at 8am', '30 minutes', 'every monday at 10am'",
        },
        "notify": {
            "type": "boolean",
            "description": "Notify on each fire",
            "default": False,
        },
        "model": {
            "type": "string",
            "description": "Model to use for this scheduled task. If omitted, uses default background model.",
        },
        "frame_type": {
            "type": "string",
            "description": "Cognitive frame for the task (e.g. 'research', 'task')",
            "enum": ["task", "research", "conversation", "decision", "debug"],
        },
    },
    "required": ["task"],
}

_LIST_TASKS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "List current subtasks and active schedules",
    "properties": {
        "status": {
            "type": "string",
            "description": "Filter subtasks by status",
            "enum": ["pending", "running", "completed", "failed", "cancelled"],
        },
    },
    "required": [],
}

_CANCEL_TASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Cancel a subtask or deactivate a schedule by ID",
    "properties": {
        "task_id": {
            "type": "string",
            "description": "UUID of the subtask or schedule to cancel",
        },
    },
    "required": ["task_id"],
}

# F062: spawn_sync tool schema. Typed counterpart to spawn_task(await_result=True);
# returns a SubtaskResult JSON blob with status/payload/confidence/etc. Only
# registered when settings.subtask_payload_schema_enabled is True.
_SPAWN_SYNC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Run a subtask synchronously (inline) and return a typed SubtaskResult "
        "containing status, structured payload, raw text, and confidence. "
        "Pass payload_schema to require the subtask's report.payload to "
        "validate against a JSON Schema; on mismatch the SubtaskResult will "
        "have status='validation_failed'. Use spawn_task for fire-and-forget."
    ),
    "properties": {
        "task": {
            "type": "string",
            "description": "Natural-language instruction for the subtask",
        },
        "payload_schema": {
            "type": "object",
            "description": (
                "Optional JSON Schema (draft 2020-12 compatible). When set, "
                "the subtask is instructed to populate submit_final_report's "
                "`payload` field with data matching this schema; the executor "
                "validates the result post-hoc."
            ),
        },
        "frame_type": {
            "type": "string",
            "description": "Cognitive frame for the subtask. Defaults to current.",
            "enum": ["task", "research", "conversation", "decision", "debug"],
        },
        "timeout_seconds": {
            "type": "integer",
            "description": "Max execution seconds (clamped to server max).",
            "minimum": 10,
        },
        "model": {
            "type": "string",
            "description": "Model to use for this subtask. Defaults to background model.",
        },
        "success_criteria": {
            "type": "string",
            "description": (
                "What 'done' looks like. Optional; defaults to a generic check."
            ),
        },
    },
    "required": ["task"],
}


def _build_spawn_task_schema(payload_schema_enabled: bool) -> dict[str, Any]:
    """Return a copy of _SPAWN_TASK_SCHEMA with the F062 payload_schema
    property conditionally injected.

    Plan §3 Commit B step 3 — we don't mutate the module-level constant
    because that would (a) leak the property on subsequent registrations
    after a settings flip in tests and (b) cross-contaminate dispatchers
    that share the import.
    """
    import copy
    schema = copy.deepcopy(_SPAWN_TASK_SCHEMA)
    if payload_schema_enabled:
        schema["properties"]["payload_schema"] = {
            "type": "object",
            "description": (
                "F062: optional JSON Schema (draft 2020-12 compatible) for "
                "the subtask's result payload. When supplied, the executor "
                "validates submit_final_report.payload against this schema; "
                "on mismatch the subtask retries (per F061) and ultimately "
                "persists final_outcome='validation_failed'."
            ),
        }
    return schema


def register_subtask_tools(
    dispatcher: ToolDispatcher,
    heart: Heart,
    settings: "Settings",
    runner: object = None,
    bus: object = None,
) -> None:
    """Create subtask/schedule tools and register them with the dispatcher.

    Called at startup when subtask_enabled is True.
    The optional ``runner`` enables inline (await_result) subtask execution.
    The optional ``bus`` (F061 PR-3) lets inline hardened subtasks emit
    ``subtask_outcome`` telemetry via the EventBus.

    F062: when ``settings.subtask_payload_schema_enabled`` is True, the
    payload_schema property is added to spawn_task's schema AND spawn_sync
    is registered as a separate tool. When False, neither is exposed —
    F062 is entirely dormant.
    """
    closures = create_subtask_tools(heart, settings, runner, bus=bus)

    spawn_task_schema = _build_spawn_task_schema(
        settings.subtask_payload_schema_enabled
    )
    dispatcher.register("spawn_task", closures["spawn_task"], spawn_task_schema)
    dispatcher.register("schedule_task", closures["schedule_task"], _SCHEDULE_TASK_SCHEMA)
    dispatcher.register("list_tasks", closures["list_tasks"], _LIST_TASKS_SCHEMA)
    dispatcher.register("cancel_task", closures["cancel_task"], _CANCEL_TASK_SCHEMA)
    # F062 requires F061's hardened executor — without subtask_hardening_enabled
    # the legacy inline path runs, which never calls execute_hardened, never
    # validates the payload, and would silently break F062's typed contract.
    # Gate registration on BOTH flags so the operator can't accidentally flip
    # only payload_schema_enabled and end up with broken validation guarantees.
    if (
        settings.subtask_payload_schema_enabled
        and settings.subtask_hardening_enabled
    ):
        dispatcher.register("spawn_sync", closures["spawn_sync"], _SPAWN_SYNC_SCHEMA)
    elif settings.subtask_payload_schema_enabled:
        logger.warning(
            "F062: NOUS_SUBTASK_PAYLOAD_SCHEMA_ENABLED=true but "
            "NOUS_SUBTASK_HARDENING_ENABLED=false; spawn_sync NOT registered. "
            "F062 requires F061's hardened executor — enable both flags to use it."
        )


# ---------------------------------------------------------------------------
# Programmatic tool calling (012.3)
# ---------------------------------------------------------------------------

SAFE_BUILTINS = {
    "len", "list", "dict", "set", "str", "int", "float", "bool",
    "print", "range", "enumerate", "zip", "sorted", "filter",
    "map", "max", "min", "sum", "any", "all", "isinstance",
    "repr", "round", "abs", "type", "tuple",
}

_MAX_WRITES = 5


def create_programmatic_tools(
    brain: Brain,
    heart: Heart,
    settings: Settings,
    episode_id_resolver: "Callable[[str], str | None] | None" = None,
) -> dict[str, Any]:
    """Create run_python tool closure for client-side programmatic execution.

    Returns a dict with a single "run_python" async callable.
    The closure captures heart (for memory wrappers) and settings (for timeout).

    episode_id_resolver: optional callable(session_id) -> episode_id_str | None.
    When provided, run_python captures the active episode at call time and
    injects it into the _learn_fact closure so script-learned facts get
    fact→episode edges (F022 P2-1 fix).
    """
    import asyncio
    import builtins
    import collections
    import concurrent.futures
    import datetime
    import functools
    import io
    import itertools
    import json
    import math
    import re
    import statistics

    async def run_python(code: str, _session_id: str | None = None) -> dict[str, Any]:
        """Execute Python code with Nous memory functions in scope."""
        write_count = {"n": 0}
        output_buf = io.StringIO()
        # F022 P2-1: Capture active episode at call time so _learn_fact can
        # link script-learned facts to the current episode without the model
        # needing to pass the UUID.
        _active_episode_id: str | None = None
        if _session_id and episode_id_resolver is not None:
            _active_episode_id = episode_id_resolver(_session_id)

        # Get the running event loop so threads can schedule DB coroutines back on it.
        # Using run_coroutine_threadsafe instead of asyncio.run() prevents deadlocks:
        # asyncio.run() creates a NEW loop in the thread, which can't access the
        # connection pool belonging to the main loop. run_coroutine_threadsafe schedules
        # the coroutine on the MAIN loop while it's awaiting the executor — no deadlock.
        loop = asyncio.get_running_loop()
        timeout = settings.programmatic_tools_timeout
        deadline = time.monotonic() + timeout

        def _schedule(coro):
            """Schedule a coroutine on the main loop and block the thread until done.

            Uses deadline-relative timeout so per-call budget tracks remaining
            wall time — prevents thread from outliving the main asyncio.wait_for.
            """
            remaining = max(0.1, deadline - time.monotonic())
            return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=remaining)

        def _recall_deep(query: str, limit: int = 5) -> list[dict]:
            results = _schedule(heart.search_facts(query, limit=limit))
            return [r.model_dump() if hasattr(r, "model_dump") else r for r in results]

        def _recall_recent(hours: int = 24, limit: int = 5) -> list[dict]:
            results = _schedule(heart.list_episodes(limit=limit, hours=hours))
            return [r.model_dump() if hasattr(r, "model_dump") else r for r in results]

        def _list_tasks(status: str | None = None) -> list[dict]:
            results = _schedule(heart.subtasks.list(status=status))
            return [r.model_dump() if hasattr(r, "model_dump") else r for r in results]

        def _learn_fact(
            content: str,
            category: str = "technical",
            subject: str | None = None,
            confidence: float = 1.0,
        ) -> str:
            if write_count["n"] >= _MAX_WRITES:
                raise RuntimeError(f"learn_fact write cap ({_MAX_WRITES}) exceeded")
            write_count["n"] += 1
            from uuid import UUID as _UUID
            ep_uuid = _UUID(_active_episode_id) if _active_episode_id else None
            result = _schedule(heart.learn(FactInput(
                content=content, category=category,
                subject=subject, confidence=confidence,
                source="user_direct",  # F023/F038: +0.15 admission bonus
                source_episode_id=ep_uuid,
            )))
            # F023/F038: Handle FactRejected (user_direct gets +0.15 bonus
            # but very low-quality facts can still be rejected)
            if hasattr(result, "admitted") and not result.admitted:
                return f"rejected: {content[:60]} (score={result.composite_score:.2f})"
            return f"stored: {content[:60]}"

        def _print(*args: object) -> None:
            output_buf.write(" ".join(str(a) for a in args) + "\n")

        safe_builtins = {k: getattr(builtins, k) for k in SAFE_BUILTINS if hasattr(builtins, k)}

        namespace: dict[str, Any] = {
            "__builtins__": safe_builtins,
            # Nous memory functions
            "recall_deep": _recall_deep,
            "recall_recent": _recall_recent,
            "list_tasks": _list_tasks,
            "learn_fact": _learn_fact,
            "print": _print,
            "result": None,
            # Safe stdlib modules (pre-injected — import statement is disabled)
            "json": json,
            "re": re,
            "math": math,
            "datetime": datetime,
            "collections": collections,
            "itertools": itertools,
            "functools": functools,
            "statistics": statistics,
        }

        def _run() -> None:
            exec(compile(code, "<nous_script>", "exec"), namespace)

        logger.info("run_python | %d chars\n%s", len(code), code)
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            # await run_in_executor releases the event loop so DB coroutines
            # scheduled via run_coroutine_threadsafe can actually execute.
            await asyncio.wait_for(loop.run_in_executor(executor, _run), timeout=timeout)
        except asyncio.TimeoutError:
            return {"content": [{"type": "text", "text": f"Error: execution timed out ({timeout}s)"}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Error: {type(e).__name__}: {e}"}]}
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        output = output_buf.getvalue()
        result = namespace.get("result")
        text = output or (str(result) if result is not None else "OK")
        return {"content": [{"type": "text", "text": text}]}

    return {"run_python": run_python}


_RUN_PYTHON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Execute Python code with Nous memory functions and safe stdlib in scope. "
        "Memory functions: recall_deep(query, limit=5), recall_recent(hours=24, limit=5), "
        "list_tasks(status=None), learn_fact(content, category, subject, confidence). "
        "Stdlib available: json, re, math, datetime, collections, itertools, functools, statistics. "
        "Use this to batch multiple memory lookups, filter results, and return only what's needed — "
        "reducing token usage compared to separate tool calls. "
        "Set result = <value> to return structured data. Use print() to emit text output. "
        "Max runtime is configurable (default 10s). Max 5 learn_fact calls per execution."
    ),
    "properties": {
        "code": {
            "type": "string",
            "description": "Python code to execute",
        }
    },
    "required": ["code"],
}


def register_programmatic_tools(
    dispatcher: ToolDispatcher,
    brain: Brain,
    heart: Heart,
    settings: Settings,
    cognitive: "Any | None" = None,
) -> None:
    """Register run_python tool if programmatic tools are enabled.

    cognitive: optional CognitiveLayer; when provided, run_python gains
    access to the active episode ID so script-learned facts get fact→episode
    edges (F022 P2-1).
    """
    if not settings.programmatic_tools_enabled:
        return
    resolver = cognitive.get_active_episode_id if cognitive is not None else None
    closures = create_programmatic_tools(brain, heart, settings, episode_id_resolver=resolver)
    dispatcher.register("run_python", closures["run_python"], _RUN_PYTHON_SCHEMA)


# ---------------------------------------------------------------------------
# Heartbeat dynamic check tools (F034.5)
# ---------------------------------------------------------------------------


def register_heartbeat_tools(dispatcher: ToolDispatcher, loader: "Any") -> None:
    """F034.5: Register dynamic heartbeat check management tools."""

    async def heartbeat_check_create(**kwargs) -> dict:
        try:
            result = await loader.create_check(
                name=kwargs["name"],
                description=kwargs["description"],
                prompt=kwargs["prompt"],
                tools=kwargs.get("tools"),
                interval_seconds=kwargs.get("interval_seconds", 3600),
                cron_expr=kwargs.get("cron_expr"),
                timeout_seconds=kwargs.get("timeout_seconds"),
                urgent=kwargs.get("urgent", False),
                on_complete_prompt=kwargs.get("on_complete_prompt"),
                on_complete_tools=kwargs.get("on_complete_tools"),
            )
            return {"content": [{"type": "text", "text": f"Created dynamic check: {json.dumps(result)}"}]}
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Failed to create check: {e}"}]}

    async def heartbeat_check_manage(**kwargs) -> dict:
        try:
            result = await loader.manage_check(
                action=kwargs["action"],
                name=kwargs.get("name"),
                updates=kwargs.get("updates"),
            )
            return {"content": [{"type": "text", "text": json.dumps(result)}]}
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Failed: {e}"}]}

    dispatcher.register("heartbeat_check_create", heartbeat_check_create, {
        "type": "object",
        "description": "Create a new dynamic heartbeat check that runs on a schedule",
        "properties": {
            "name": {"type": "string", "description": "Unique check name (slug, e.g. 'arxiv-agent-papers')"},
            "description": {"type": "string", "description": "Human-readable description of what this check monitors"},
            "prompt": {"type": "string", "description": "Instruction for what to check and report on"},
            "tools": {"type": "array", "items": {"type": "string"}, "description": "Tools the check can use (allowed: web_search, web_fetch, recall_deep, recall_recent, bash, read_file, heartbeat_check_create, heartbeat_check_manage)"},
            "interval_seconds": {"type": "integer", "description": "Seconds between runs (min 300, default 3600)"},
            "cron_expr": {"type": "string", "description": "Cron expression for scheduling (overrides interval_seconds)"},
            "timeout_seconds": {"type": "integer", "description": "Max seconds per run (default from NOUS_HEARTBEAT_DEFAULT_CHECK_TIMEOUT)"},
            "urgent": {"type": "boolean", "description": "If true, runs during quiet hours too"},
            "on_complete_prompt": {"type": "string", "description": "Prompt to execute when check self-disables (callback)"},
            "on_complete_tools": {"type": "array", "items": {"type": "string"}, "description": "Tools for callback (must be subset of check tools)"},
        },
        "required": ["name", "description", "prompt"],
    })

    dispatcher.register("heartbeat_check_manage", heartbeat_check_manage, {
        "type": "object",
        "description": "List, enable, disable, delete, or update dynamic heartbeat checks",
        "properties": {
            "action": {"type": "string", "enum": ["list", "enable", "disable", "delete", "update"], "description": "Action to perform"},
            "name": {"type": "string", "description": "Check name (required for enable/disable/delete/update)"},
            "updates": {"type": "object", "description": "Fields to update when action=update (allowed: description, prompt, tools, interval_seconds, cron_expr, timeout_seconds, urgent, on_complete_prompt, on_complete_tools)"},
        },
        "required": ["action"],
    })


# ---------------------------------------------------------------------------
# DAG orchestration tools (F038)
# ---------------------------------------------------------------------------


def register_dag_tools(
    dispatcher: ToolDispatcher,
    store: "Any",
    orchestrator: "Any",
) -> None:
    """F038: Register DAG orchestration tools."""
    from nous.dag.schemas import DAGCreateRequest, DAGEdgeSpec, DAGNodeSpec, DAGNodeType

    async def dag_create(**kwargs: Any) -> dict:
        """Create a DAG with dependency-tracked nodes."""
        try:
            # Parse nodes
            node_specs: list[DAGNodeSpec] = []
            for n in kwargs.get("nodes", []):
                node_specs.append(DAGNodeSpec(
                    name=n["name"],
                    type=DAGNodeType(n["type"]),
                    instructions=n.get("instructions", ""),
                    description=n.get("description", ""),
                    tools=n.get("tools"),
                    frame_type=n.get("frame_type"),
                    model=n.get("model"),
                    timeout_seconds=n.get("timeout_seconds"),
                    completion_condition=n.get("completion_condition"),
                    completion_check=n.get("completion_check"),
                    completion_check_interval=n.get("completion_check_interval"),
                    max_check_attempts=n.get("max_check_attempts"),
                ))

            # Parse edges
            edge_specs: list[DAGEdgeSpec] = []
            for e in kwargs.get("edges", []):
                edge_specs.append(DAGEdgeSpec(
                    from_node=e["from_node"],
                    to_node=e["to_node"],
                    edge_type=e.get("edge_type", "dependency"),
                ))

            request = DAGCreateRequest(
                name=kwargs["name"],
                description=kwargs.get("description", ""),
                source=kwargs.get("source", "conversation"),
                token_budget=kwargs.get("token_budget"),
                nodes=node_specs,
                edges=edge_specs,
            )

            dag = await store.create(request)
            await orchestrator.start_dag(dag.id)

            # Re-fetch to get actual status
            started_dag = await store.get_dag(dag.id)
            actual_status = started_dag.status if started_dag else "unknown"

            # Compute wave summary
            waves = request.compute_waves()
            wave_groups: dict[int, list[str]] = {}
            for name, wave in waves.items():
                wave_groups.setdefault(wave, []).append(name)

            lines = [f"Created DAG '{dag.name}' ({str(dag.id)[:8]})"]
            lines.append(f"  {len(dag.nodes)} nodes, {len(dag.edges)} edges")
            for w in sorted(wave_groups):
                lines.append(f"  Wave {w}: {', '.join(wave_groups[w])}")
            lines.append(f"Status: {actual_status}")

            return {"content": [{"type": "text", "text": "\n".join(lines)}]}
        except Exception as e:
            logger.exception("dag_create failed")
            return {"content": [{"type": "text", "text": f"Error creating DAG: {e}"}]}

    async def dag_manage(**kwargs: Any) -> dict:
        """List, inspect, cancel, or retry nodes in DAGs."""
        action = kwargs["action"]
        dag_id_str = kwargs.get("dag_id")

        try:
            if action == "list":
                dags = await store.get_active_dags()
                if not dags:
                    return {"content": [{"type": "text", "text": "No active DAGs."}]}
                lines = [f"Active DAGs ({len(dags)}):"]
                for d in dags:
                    completed = sum(1 for n in d.nodes if n.status == "completed")
                    total = len(d.nodes)
                    lines.append(f"  {str(d.id)[:8]} | {d.name} | {d.status} | {completed}/{total} nodes done")
                return {"content": [{"type": "text", "text": "\n".join(lines)}]}

            if not dag_id_str:
                return {"content": [{"type": "text", "text": "Error: dag_id required for this action"}]}

            # Support 8-char prefix lookup
            dag = await _resolve_dag(store, dag_id_str)
            if dag is None:
                return {"content": [{"type": "text", "text": f"Error: DAG '{dag_id_str}' not found"}]}

            if action == "status":
                status_icons = {
                    "completed": "+", "failed": "X", "running": ">",
                    "ready": "~", "pending": ".", "blocked": "!", "cancelled": "-",
                    "awaiting_check": "*",
                }
                lines = [
                    f"DAG: {dag.name} ({str(dag.id)[:8]})",
                    f"Status: {dag.status}",
                    f"Nodes ({len(dag.nodes)}):",
                ]
                for node in sorted(dag.nodes, key=lambda n: (n.wave or 0, n.name)):
                    icon = status_icons.get(node.status, "?")
                    wave_str = f"w{node.wave}" if node.wave is not None else "w?"
                    line = f"  [{icon}] {node.name} ({node.node_type}, {wave_str}) — {node.status}"
                    if node.status == "awaiting_check" and hasattr(node, 'check_attempts'):
                        line += f" | polls: {node.check_attempts}"
                    if node.error:
                        line += f" | error: {node.error[:80]}"
                    lines.append(line)
                return {"content": [{"type": "text", "text": "\n".join(lines)}]}

            elif action == "cancel":
                await orchestrator.cancel_dag(dag.id, reason="cancelled by user")
                return {"content": [{"type": "text", "text": f"Cancelled DAG '{dag.name}' ({str(dag.id)[:8]})"}]}

            elif action == "retry_node":
                node_name = kwargs.get("node_name")
                if not node_name:
                    return {"content": [{"type": "text", "text": "Error: node_name required for retry_node"}]}
                await orchestrator.retry_node(dag.id, node_name)
                return {"content": [{"type": "text", "text": f"Reset node '{node_name}' to pending for retry"}]}

            else:
                return {"content": [{"type": "text", "text": f"Error: unknown action '{action}'"}]}

        except Exception as e:
            logger.exception("dag_manage failed")
            return {"content": [{"type": "text", "text": f"Error: {e}"}]}

    dispatcher.register("dag_create", dag_create, {
        "type": "object",
        "description": "Create a DAG to orchestrate subtasks and checks with dependency tracking.",
        "properties": {
            "name": {"type": "string", "description": "DAG name"},
            "description": {"type": "string"},
            "nodes": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "type": {"type": "string", "enum": ["subtask", "check", "gate", "callback"]}, "instructions": {"type": "string"}, "tools": {"type": "array", "items": {"type": "string"}}, "frame_type": {"type": "string"}, "model": {"type": "string"}, "timeout_seconds": {"type": "integer", "minimum": 1, "description": "Execution timeout in seconds (default: NOUS_DAG_NODE_DEFAULT_TIMEOUT, ceiling: NOUS_DAG_NODE_MAX_TIMEOUT)"}, "completion_condition": {"type": "string"}, "completion_check": {"type": "string", "description": "Shell command polled each tick. Exit 0 = success, 1 = failed, 2 = still running."}, "completion_check_interval": {"type": "integer", "description": "Seconds between completion check polls (default: every tick)"}, "max_check_attempts": {"type": "integer", "description": "Max poll attempts before node fails"}}, "required": ["name", "type", "instructions"]}},
            "edges": {"type": "array", "items": {"type": "object", "properties": {"from_node": {"type": "string"}, "to_node": {"type": "string"}, "edge_type": {"type": "string", "enum": ["dependency", "cancel_cascade", "context_flow"]}}, "required": ["from_node", "to_node"]}},
            "source": {"type": "string"},
            "token_budget": {"type": "integer"},
        },
        "required": ["name", "nodes", "edges"],
    })

    dispatcher.register("dag_manage", dag_manage, {
        "type": "object",
        "description": "List, inspect, cancel, or retry nodes in DAGs.",
        "properties": {
            "action": {"type": "string", "enum": ["list", "status", "cancel", "retry_node"]},
            "dag_id": {"type": "string"},
            "node_name": {"type": "string"},
        },
        "required": ["action"],
    })

    logger.info("F038: Registered dag_create and dag_manage tools")


async def _resolve_dag(store: "Any", dag_id_str: str) -> "Any | None":
    """Resolve a DAG by full UUID or 8-char prefix.

    Raises ValueError if prefix matches multiple DAGs.
    Returns None if no match found.
    """
    from uuid import UUID as _UUID

    # Try full UUID first
    try:
        dag_id = _UUID(dag_id_str)
        return await store.get_dag(dag_id)
    except ValueError:
        pass

    # Try prefix match against active DAGs
    dags = await store.get_active_dags()
    matches = [d for d in dags if str(d.id).startswith(dag_id_str)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        ids = ", ".join(str(d.id)[:8] for d in matches)
        raise ValueError(f"Prefix '{dag_id_str}' is ambiguous, matches: {ids}")

    # Also check recent DAGs for status/retry on completed/failed
    recent = await store.get_recent_dags(limit=20)
    finished = [d for d in recent if d.status not in ("pending", "running")]
    matches = [d for d in finished if str(d.id).startswith(dag_id_str)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        ids = ", ".join(str(d.id)[:8] for d in matches)
        raise ValueError(f"Prefix '{dag_id_str}' is ambiguous, matches: {ids}")

    return None
