"""REST API for Nous agent.

Endpoints:
  POST /chat              - Send message, get response
  DELETE /chat/{session}  - End conversation
  GET  /status            - Agent status + memory stats + calibration
  GET  /decisions         - List recent decisions (Brain)
  GET  /decisions/unreviewed - Unreviewed decisions for external agents
  POST /decisions/{id}/review - External decision review
  GET  /decisions/{id}    - Get decision detail
  GET  /episodes          - List recent episodes (Heart)
  GET  /facts             - Search facts (Heart)
  GET  /censors           - Active censors (Heart)
  GET  /frames            - Available frames
  GET  /calibration       - Calibration report (Brain)
  GET  /identity          - Get current agent identity
  PUT  /identity/{section} - Update an identity section
  POST /reinitiate        - Reset identity and re-run initiation
  GET  /subtasks          - List subtasks (Heart)
  GET  /subtasks/{id}     - Subtask detail
  DELETE /subtasks/{id}   - Cancel a pending subtask
  GET  /schedules         - List schedules (Heart)
  POST /schedules         - Create a schedule externally
  DELETE /schedules/{id}  - Deactivate a schedule
  GET  /health            - Health check (DB connectivity)
  GET  /procedures        - List procedures (Heart)
  GET  /dashboard/graph   - Graph data for D3 visualization (F021)
  GET  /dashboard/calibration - Decision intelligence analytics (F021)
  GET  /dashboard/activity - System activity timeline (F021)
  GET  /dashboard/health  - Graph health trends (F021)
  GET  /dashboard/admission - Admission control analytics (F021.1)
  GET  /dashboard/admission/rejected - Paginated rejected facts (F021.1)
  GET  /dashboard         - Redirects to /dashboard/v2/ (Svelte SPA, Phase 4 cutover)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from typing import Any
from uuid import UUID, uuid4

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from nous.api.models import Attachment
from nous.api.runner import AgentRunner
from nous.brain import Brain
from nous.cognitive import CognitiveLayer
from nous.cognitive.context import PROFILE_CORE_TAG, TIER1_FACT_CATEGORIES
from nous.config import Settings
from nous.events import Event, EventBus
from nous.heart import Heart
from nous.observability.retrieval_logger import RETRIEVAL_PATHS as _RETRIEVAL_PATHS
from nous.storage.database import Database

logger = logging.getLogger(__name__)


def create_app(
    runner: AgentRunner,
    brain: Brain,
    heart: Heart,
    cognitive: CognitiveLayer,
    database: Database,
    settings: Settings,
    lifespan: Any | None = None,
    identity_manager: Any | None = None,
    bus: EventBus | None = None,
    sleep_handler: Any | None = None,
    rubric_manager: Any | None = None,
    rubric_evolver: Any | None = None,
    heartbeat_runner: Any | None = None,
    session_monitor: Any | None = None,
    context_logger: Any | None = None,
    surface_service: Any | None = None,
    action_router: Any | None = None,
) -> Starlette:
    """Create the Starlette ASGI app with all routes."""

    def _parse_attachments(body: dict) -> list[Attachment]:
        from nous.api.attachments import classify_attachment, sanitize_filename, validate_base64_size
        out: list[Attachment] = []
        raw = body.get("attachments") if isinstance(body, dict) else None
        for a in (raw or [])[: settings.attachments_max_per_message]:
            if not isinstance(a, dict):
                continue  # ignore malformed entries rather than 500
            data_b64 = a.get("data_base64", "")
            filename = sanitize_filename(a.get("filename", "unnamed"))
            media_type = a.get("media_type", "application/octet-stream")
            out.append(Attachment(
                filename=filename, media_type=media_type, data_base64=data_b64,
                size_bytes=validate_base64_size(data_b64),  # actual, not client-declared
                source="rest",
                content_type=classify_attachment(filename, media_type),
            ))
        return out

    def _body_too_large(request) -> bool:
        max_body = int(settings.attachments_max_per_message * 32 * 1024 * 1024 * 1.4) + 1_000_000
        clen = request.headers.get("content-length")
        return bool(clen and clen.isdigit() and int(clen) > max_body)

    async def chat(request: Request) -> JSONResponse:
        """POST /chat - Send a message, get a response."""
        if settings.attachments_enabled and _body_too_large(request):
            return JSONResponse({"error": "Request too large"}, status_code=413)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        message = body.get("message")
        attachments = _parse_attachments(body) if settings.attachments_enabled else []
        if not message and not attachments:
            return JSONResponse({"error": "Missing required field: message"}, status_code=400)
        if not message:
            message = settings.attachments_default_prompt

        session_id = body.get("session_id") or str(uuid4())

        try:
            debug = body.get("debug", False)
            platform = body.get("platform")
            # 007.4: Extract optional user identity
            user_id = body.get("user_id")
            user_display_name = body.get("user_display_name")
            response_text, turn_context, usage = await runner.run_turn(
                session_id, message, platform=platform,
                user_id=user_id, user_display_name=user_display_name,
                attachments=attachments or None,
            )
            result: dict[str, Any] = {
                "response": response_text,
                "session_id": session_id,
                "frame": turn_context.frame.frame_id,
                "decision_id": turn_context.decision_id,
                "usage": usage,
            }
            if debug:
                result["debug"] = {
                    "system_prompt": turn_context.system_prompt,
                    "frame_confidence": turn_context.frame.confidence,
                    "active_censors": len(turn_context.active_censors),
                    "related_decisions": len(turn_context.recalled_decision_ids),
                    "related_facts": len(turn_context.recalled_fact_ids),
                    "related_episodes": len(turn_context.recalled_episode_ids),
                    "context_tokens": turn_context.context_token_estimate,
                    # Actual IDs injected into context this turn — lets an eval attribute
                    # mechanism (pre_turn context-injection vs agentic tool retrieval), which
                    # usage.tool_calls cannot see.
                    "recalled_fact_ids": list(turn_context.recalled_fact_ids),
                    "recalled_decision_ids": list(turn_context.recalled_decision_ids),
                    "recalled_episode_ids": list(turn_context.recalled_episode_ids),
                    "recalled_procedure_ids": list(turn_context.recalled_procedure_ids),
                }
            return JSONResponse(result)
        except Exception as e:
            logger.error("Chat error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def chat_stream(request: Request) -> StreamingResponse:
        """POST /chat/stream - SSE streaming chat."""
        if settings.attachments_enabled and _body_too_large(request):
            return JSONResponse({"error": "Request too large"}, status_code=413)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        message = body.get("message")
        attachments = _parse_attachments(body) if settings.attachments_enabled else []
        if not message and not attachments:
            return JSONResponse({"error": "Missing required field: message"}, status_code=400)
        if not message:
            message = settings.attachments_default_prompt

        session_id = body.get("session_id") or str(uuid4())
        platform = body.get("platform")
        # 007.4: Extract optional user identity
        user_id = body.get("user_id")
        user_display_name = body.get("user_display_name")

        async def event_generator():
            stream = runner.stream_chat(
                session_id, message, platform=platform,
                user_id=user_id, user_display_name=user_display_name,
                attachments=attachments or None,
            )
            aiter = stream.__aiter__()
            ping_interval = settings.sse_ping_interval
            pending: asyncio.Task | None = None
            try:
                while True:
                    if pending is None:
                        pending = asyncio.create_task(aiter.__anext__())

                    # Race the next event against the ping interval. We use
                    # asyncio.wait (not wait_for) so that on timeout the task
                    # is NOT cancelled — it stays pending and we resume waiting
                    # on the next iteration. This keeps bytes flowing to the
                    # client during stalls in pre_turn, compaction, or any
                    # other non-streaming phase of stream_chat.
                    done, _ = await asyncio.wait(
                        {pending}, timeout=ping_interval
                    )
                    if not done:
                        # No event in window — emit SSE comment-line ping.
                        # Comment lines (starting with `:`) are ignored by
                        # spec-compliant SSE clients but reset their socket
                        # read timer. See WHATWG EventSource spec.
                        yield ": keepalive\n\n"
                        continue

                    try:
                        event = pending.result()
                    except StopAsyncIteration:
                        pending = None
                        break
                    pending = None

                    event_data: dict[str, Any] = {
                        "type": event.type,
                        "text": event.text,
                        "tool_name": event.tool_name,
                        "stop_reason": event.stop_reason,
                    }
                    if event.usage:
                        event_data["usage"] = event.usage
                    data = json.dumps(event_data)
                    yield f"data: {data}\n\n"
            except Exception as e:
                logger.error("Stream error: %s", e)
                error_data = json.dumps({"type": "error", "text": str(e)})
                yield f"data: {error_data}\n\n"
            finally:
                # Cancel any in-flight __anext__ task before closing the
                # generator to avoid "Task was destroyed but it is pending"
                # warnings on client disconnect.
                if pending is not None and not pending.done():
                    pending.cancel()
                    with contextlib.suppress(BaseException):
                        await pending
                # Ensure stream_chat generator is closed on client disconnect
                # so its finally block (post_turn cleanup) runs deterministically.
                await stream.aclose()

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    async def end_chat(request: Request) -> JSONResponse:
        """DELETE /chat/{session_id} - End a conversation."""
        session_id = request.path_params["session_id"]
        try:
            await runner.end_conversation(session_id)
            return JSONResponse({"status": "ended", "session_id": session_id})
        except Exception as e:
            logger.error("End chat error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def status(request: Request) -> JSONResponse:
        """GET /status - Agent status overview."""
        try:
            calibration = await brain.get_calibration()

            # Raw SQL COUNT queries (F23)
            from sqlalchemy import text

            async with database.session() as session:
                counts = {}
                for table, key in [
                    ("brain.decisions", "total_decisions"),
                    ("heart.facts", "total_facts"),
                    ("heart.episodes", "total_episodes"),
                    ("heart.procedures", "total_procedures"),
                    # F067: surface chunked transcripts on /status so the
                    # Overview tab has a parity stat card next to facts/
                    # episodes/decisions/procedures.
                    ("heart.episode_chunks", "total_chunks"),
                ]:
                    result = await session.execute(
                        text(f"SELECT COUNT(*) FROM {table} WHERE agent_id = :agent_id"),
                        {"agent_id": settings.agent_id},
                    )
                    counts[key] = result.scalar() or 0

                # Active censors count
                result = await session.execute(
                    text("SELECT COUNT(*) FROM heart.censors WHERE agent_id = :agent_id AND active = true"),
                    {"agent_id": settings.agent_id},
                )
                counts["active_censors"] = result.scalar() or 0

                # Working memory sessions
                result = await session.execute(
                    text("SELECT session_id FROM heart.working_memory WHERE agent_id = :agent_id"),
                    {"agent_id": settings.agent_id},
                )
                wm_session_ids = [row[0] for row in result]

                # Fetch working memory details for each session
                working_memory_sessions = []
                for wm_sid in wm_session_ids:
                    wm_state = await heart.get_working_memory(wm_sid, session=session)
                    if wm_state:
                        working_memory_sessions.append({
                            "session_id": wm_sid,
                            "current_task": wm_state.current_task,
                            "current_frame": wm_state.current_frame,
                            "item_count": wm_state.item_count,
                            "items": [
                                {"type": it.type, "summary": it.summary, "relevance": it.relevance}
                                for it in wm_state.items
                            ],
                            "open_threads": [
                                {"description": t.description, "priority": t.priority}
                                for t in wm_state.open_threads
                            ],
                        })

            result_data: dict[str, Any] = {
                    "agent_id": settings.agent_id,
                    "agent_name": settings.agent_name,
                    "model": settings.model,
                    "calibration": {
                        "brier_score": calibration.brier_score,
                        "accuracy": calibration.accuracy,
                        "total_decisions": calibration.total_decisions,
                        "reviewed_decisions": calibration.reviewed_decisions,
                    },
                    "memory": {
                        "active_conversations": len(runner._conversations),
                        "active_censors": counts["active_censors"],
                        "total_decisions": counts["total_decisions"],
                        "total_facts": counts["total_facts"],
                        "total_episodes": counts["total_episodes"],
                        "total_procedures": counts["total_procedures"],
                        "total_chunks": counts["total_chunks"],
                    },
                    "working_memory": working_memory_sessions,
                    "execution_integrity": {
                        **_build_integrity_config(),
                        "active_ledgers": len(runner._ledgers),
                        "sessions": {
                            sid: {
                                "total_actions": len(ledger.actions),
                                "blocked_actions": sum(
                                    1 for a in ledger.actions if a.status == "blocked"
                                ),
                                "current_turn": ledger._current_turn,
                                "summary": ledger.one_line_summary(),
                            }
                            for sid, ledger in runner._ledgers.items()
                        },
                        "pending_corrections": {
                            sid: len(corrections)
                            for sid, corrections in runner._pending_corrections.items()
                            if corrections
                        },
                    },
                }

            # F021: Dashboard extension
            if request.query_params.get("dashboard") == "true":
                from nous.api.dashboard_queries import get_dashboard_stats

                async with database.session() as dash_session:
                    result_data["dashboard"] = await get_dashboard_stats(
                        dash_session, settings.agent_id
                    )

            return JSONResponse(result_data)
        except Exception as e:
            logger.error("Status error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def list_decisions(request: Request) -> JSONResponse:
        """GET /decisions?limit=20&offset=0 - List decisions with filters."""
        try:
            limit = int(request.query_params.get("limit", "20"))
            offset = int(request.query_params.get("offset", "0"))
        except ValueError:
            return JSONResponse({"error": "limit and offset must be integers"}, status_code=400)

        category = request.query_params.get("category")
        stakes = request.query_params.get("stakes")
        outcome = request.query_params.get("outcome")
        confidence_min_str = request.query_params.get("confidence_min")
        confidence_min = float(confidence_min_str) if confidence_min_str else None
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        reviewed_param = request.query_params.get("reviewed")
        reviewed = {"true": True, "false": False}.get(reviewed_param) if reviewed_param else None
        sort = request.query_params.get("sort", "created_at")
        order = request.query_params.get("order", "desc")

        try:
            decisions, total = await brain.list_decisions(
                limit=limit, offset=offset, category=category, stakes=stakes,
                outcome=outcome, confidence_min=confidence_min,
                date_from=date_from, date_to=date_to, reviewed=reviewed,
                sort=sort, order=order,
            )
            return JSONResponse({
                "decisions": [d.model_dump(mode="json") for d in decisions],
                "total": total,
                "limit": limit,
                "offset": offset,
            })
        except Exception as e:
            logger.error("List decisions error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def get_decision(request: Request) -> JSONResponse:
        """GET /decisions/{id} - Decision detail."""
        decision_id_str = request.path_params["id"]
        try:
            decision_id = UUID(decision_id_str)
        except ValueError:
            return JSONResponse({"error": "Invalid decision ID"}, status_code=400)

        try:
            detail = await brain.get(decision_id)
            if detail is None:
                return JSONResponse({"error": "Decision not found"}, status_code=404)
            return JSONResponse(detail.model_dump(mode="json"))
        except Exception as e:
            logger.error("Get decision error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def list_episodes(request: Request) -> JSONResponse:
        """GET /episodes?limit=20&offset=0 - List episodes with filters."""
        try:
            limit = int(request.query_params.get("limit", "20"))
            offset = int(request.query_params.get("offset", "0"))
        except ValueError:
            return JSONResponse({"error": "limit and offset must be integers"}, status_code=400)

        outcome = request.query_params.get("outcome")
        frame = request.query_params.get("frame")
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        sort = request.query_params.get("sort", "started_at")
        order = request.query_params.get("order", "desc")

        try:
            episodes, total = await heart.list_episodes_paginated(
                limit=limit, offset=offset, outcome=outcome, frame=frame,
                date_from=date_from, date_to=date_to, sort=sort, order=order,
            )
            return JSONResponse({
                "episodes": [e.model_dump(mode="json") for e in episodes],
                "total": total,
                "limit": limit,
                "offset": offset,
            })
        except Exception as e:
            logger.error("List episodes error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def search_facts(request: Request) -> JSONResponse:
        """GET /facts?q=query&limit=20 - Search or browse facts."""
        q = request.query_params.get("q")

        try:
            limit = int(request.query_params.get("limit", "20"))
            offset = int(request.query_params.get("offset", "0"))
        except ValueError:
            return JSONResponse({"error": "limit and offset must be integers"}, status_code=400)

        try:
            if q:
                # Existing search behavior
                category = request.query_params.get("category")
                facts = await heart.search_facts(q, limit=limit, category=category)
                return JSONResponse({
                    "facts": [f.model_dump(mode="json") for f in facts],
                    "total": len(facts),
                })
            else:
                # Browse mode (F021)
                category = request.query_params.get("category")
                active_param = request.query_params.get("active")
                active_only = active_param != "false" if active_param else True
                confidence_min_str = request.query_params.get("confidence_min")
                confidence_min = float(confidence_min_str) if confidence_min_str else None
                date_from = request.query_params.get("date_from")
                date_to = request.query_params.get("date_to")
                sort = request.query_params.get("sort", "created_at")
                order = request.query_params.get("order", "desc")

                facts, total = await heart.list_facts(
                    limit=limit, offset=offset, category=category,
                    active_only=active_only, confidence_min=confidence_min,
                    date_from=date_from, date_to=date_to, sort=sort, order=order,
                )
                return JSONResponse({
                    "facts": [f.model_dump(mode="json") for f in facts],
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                })
        except Exception as e:
            logger.error("Search/browse facts error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def list_profile_facts(request: Request) -> JSONResponse:
        """GET /profile/facts — Tier-1 user-profile facts (preference/person/rule).

        Same accessor + ordering (confidence DESC, learned_at DESC) as the
        system-prompt User Profile section, so the dashboard shows exactly
        what the agent can draw from.
        """
        try:
            limit = int(request.query_params.get("limit", "100"))
            if limit < 1:
                raise ValueError
        except ValueError:
            return JSONResponse({"error": "invalid limit"}, status_code=400)
        active_only = request.query_params.get("active", "true").lower() != "false"
        # ?core=true audits the curated core set (facts tagged PROFILE_CORE_TAG).
        require_tag = (
            PROFILE_CORE_TAG
            if request.query_params.get("core", "").lower() == "true"
            else None
        )
        try:
            facts = await heart.list_facts_by_category(
                categories=TIER1_FACT_CATEGORIES,
                active_only=active_only,
                limit=limit,
                # Mirror the prompt path (docstring promise: "exactly what the
                # agent can draw from") — 2026-07-24 pollution fix.
                exclude_sources=settings.profile_exclude_sources or None,
                require_tag=require_tag,
            )
            return JSONResponse(
                {"facts": [f.model_dump(mode="json") for f in facts], "total": len(facts)}
            )
        except Exception as e:
            logger.error("list_profile_facts failed: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def update_fact(request: Request) -> JSONResponse:
        """PUT /facts/{fact_id} — edit a Tier-1 fact's content via supersession.

        Nous never edits fact content in place: the old fact is deactivated
        with superseded_by set, the replacement is re-embedded. If the new
        content dedups into a DIFFERENT existing fact (native cosine gate),
        heart confirms that fact instead of storing the edit — we report that
        honestly as merged_into_existing (never a silent false success).
        """
        from uuid import UUID as _UUID

        from sqlalchemy import text as sa_text

        from nous.heart import FactInput

        try:
            fact_id = _UUID(request.path_params["fact_id"])
        except ValueError:
            return JSONResponse({"error": "invalid fact id"}, status_code=400)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        content = (body.get("content") or "").strip()
        if not content:
            return JSONResponse({"error": "content is required"}, status_code=400)
        min_chars = settings.fact_min_content_chars
        if min_chars and len(content) < min_chars:
            return JSONResponse(
                {"error": f"content too short (min {min_chars} chars)"}, status_code=400
            )
        body_category = (body.get("category") or "").strip() or None
        if body_category is not None and body_category not in TIER1_FACT_CATEGORIES:
            return JSONResponse(
                {"error": f"category must be one of {TIER1_FACT_CATEGORIES}"}, status_code=400
            )
        try:
            # One session for the whole check-then-write: a per-fact advisory
            # xact lock serializes concurrent edits of the same fact so the
            # loser re-reads under the lock and hits the advertised 409
            # (codex r1: both requests could pass the current-id check before
            # either committed). Lock is transaction-scoped — released on
            # commit/rollback with the session.
            async with database.session() as session:
                await session.execute(
                    sa_text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": f"nous_fact_edit:{fact_id}"},
                )
                try:
                    target = await heart.get_current_fact(fact_id, session=session)
                except ValueError:
                    return JSONResponse({"error": "fact not found"}, status_code=404)
                if str(target.id) != str(fact_id):
                    return JSONResponse(
                        {"error": "already_superseded", "current_fact_id": str(target.id)},
                        status_code=409,
                    )
                if target.category not in TIER1_FACT_CATEGORIES:
                    return JSONResponse({"error": "not_profile_fact"}, status_code=409)
                # Exact-content pre-check (codex r1): an edit that exactly
                # matches ANOTHER active fact dedup-confirms it, so the
                # content-inequality signal below cannot see the merge —
                # detect it up front.
                exact_row = await session.execute(
                    sa_text(
                        "SELECT id FROM heart.facts "
                        "WHERE agent_id = :a AND content = :c AND active AND id != :fid "
                        "LIMIT 1"
                    ),
                    {"a": settings.agent_id, "c": content, "fid": str(fact_id)},
                )
                exact_dupe_id = exact_row.scalar_one_or_none()
                new_input = FactInput(
                    content=content,
                    category=body_category or target.category,
                    subject=body.get("subject") if "subject" in body else target.subject,
                    confidence=body.get("confidence") if body.get("confidence") is not None else target.confidence,
                    # codex #574 r4: supersession must not strip curation — an
                    # edited core fact previously lost profile_core and vanished
                    # from the curated User Profile. Inherit ALL tags.
                    tags=list(target.tags or []),
                )
                try:
                    new_fact = await heart.supersede_fact(fact_id, new_input, session=session)
                except ValueError:
                    return JSONResponse({"error": "fact not found"}, status_code=404)
                # codex #574 r5: on a dedup-merge, supersede returns a
                # pre-existing THIRD fact — FactInput.tags never lands there.
                # Curation must follow the merge: if the edited fact was core
                # and the surviving fact isn't, tag it.
                from nous.cognitive.context import PROFILE_CORE_TAG as _CORE_TAG
                if _CORE_TAG in (target.tags or []) and _CORE_TAG not in (new_fact.tags or []):
                    await heart.set_fact_tag(new_fact.id, _CORE_TAG, True, session=session)
                await session.commit()
            # Injected-session supersede skips its own post-commit vocab
            # invalidation (facts.py contract: commit owner invalidates).
            heart.facts.invalidate_entity_vocab()
        except Exception as e:
            logger.error("update_fact failed: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)
        merged = (new_fact.content or "").strip() != content or (
            exact_dupe_id is not None and str(new_fact.id) == str(exact_dupe_id)
        )
        if merged:
            return JSONResponse({
                "status": "merged_into_existing",
                "new_fact_id": str(new_fact.id),
                "stored_content": new_fact.content,
            })
        return JSONResponse({"status": "superseded", "new_fact_id": str(new_fact.id)})

    async def delete_fact(request: Request) -> JSONResponse:
        """DELETE /facts/{fact_id} — soft-delete (deactivate) a Tier-1 fact."""
        from uuid import UUID as _UUID

        from sqlalchemy import text as sa_text

        try:
            fact_id = _UUID(request.path_params["fact_id"])
        except ValueError:
            return JSONResponse({"error": "invalid fact id"}, status_code=400)
        try:
            # Same per-fact advisory xact lock + single session as update_fact
            # so a DELETE racing a PUT on the same fact serializes and the
            # loser sees the already_superseded/missing state (codex r1).
            async with database.session() as session:
                await session.execute(
                    sa_text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": f"nous_fact_edit:{fact_id}"},
                )
                try:
                    target = await heart.get_current_fact(fact_id, session=session)
                except ValueError:
                    return JSONResponse({"error": "fact not found"}, status_code=404)
                if str(target.id) != str(fact_id):
                    return JSONResponse(
                        {"error": "already_superseded", "current_fact_id": str(target.id)},
                        status_code=409,
                    )
                if target.category not in TIER1_FACT_CATEGORIES:
                    return JSONResponse({"error": "not_profile_fact"}, status_code=409)
                try:
                    await heart.deactivate_fact(fact_id, session=session)
                except ValueError:
                    return JSONResponse({"error": "fact not found"}, status_code=404)
                await session.commit()
        except Exception as e:
            logger.error("delete_fact failed: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse({"status": "deactivated"})

    async def set_fact_core(request: Request) -> JSONResponse:
        """POST /facts/{fact_id}/core — toggle the profile_core curation tag.

        Adds/removes PROFILE_CORE_TAG on a Tier-1 profile fact so the User
        Profile core-selection path renders it. Guard chain mirrors update_fact:
        400 bad id -> 404 missing -> 409 already_superseded -> 409 not_profile_fact.
        """
        from uuid import UUID as _UUID

        try:
            fact_id = _UUID(request.path_params["fact_id"])
        except ValueError:
            return JSONResponse({"error": "invalid fact id"}, status_code=400)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        core = body.get("core")
        if not isinstance(core, bool):
            return JSONResponse({"error": "core (bool) is required"}, status_code=400)
        try:
            async with database.session() as session:
                # Same per-fact advisory xact lock as update_fact/delete_fact:
                # serializes against a concurrent supersede/deactivate so the
                # tag is never applied to a row that just became superseded
                # (branch-review P2 — consistency with the file's convention).
                from sqlalchemy import text as sa_text
                await session.execute(
                    sa_text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": f"nous_fact_edit:{fact_id}"},
                )
                try:
                    target = await heart.get_current_fact(fact_id, session=session)
                except ValueError:
                    return JSONResponse({"error": "fact not found"}, status_code=404)
                if str(target.id) != str(fact_id):
                    return JSONResponse(
                        {"error": "already_superseded", "current_fact_id": str(target.id)},
                        status_code=409,
                    )
                if target.category not in TIER1_FACT_CATEGORIES:
                    return JSONResponse({"error": "not_profile_fact"}, status_code=409)
                await heart.set_fact_tag(fact_id, PROFILE_CORE_TAG, core, session=session)
                await session.commit()
        except Exception as e:
            logger.error("set_fact_core failed: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse({"status": "core_set" if core else "core_unset"})

    async def list_chunks(request: Request) -> JSONResponse:
        """GET /chunks?q=&episode_id=&limit=&offset= — browse F067 episode chunks.

        Parity with /facts browse mode but read-only and direct-SQL (no
        Heart accessor — chunk rows aren't part of the recall surface).
        """
        try:
            limit = int(request.query_params.get("limit", "50"))
            offset = int(request.query_params.get("offset", "0"))
        except ValueError:
            return JSONResponse({"error": "limit and offset must be integers"}, status_code=400)

        q = request.query_params.get("q")
        episode_id_raw = request.query_params.get("episode_id")
        # Validate episode_id is a real UUID before it reaches Postgres —
        # otherwise the type-cast error surfaces as a 500 via the generic
        # except below. Codex P2 (2026-05-26).
        episode_id: str | None = None
        if episode_id_raw:
            import uuid as _uuid
            try:
                episode_id = str(_uuid.UUID(episode_id_raw))
            except ValueError:
                return JSONResponse(
                    {"error": "episode_id must be a UUID"},
                    status_code=400,
                )

        from sqlalchemy import text as _sql_text
        params: dict[str, Any] = {"agent_id": settings.agent_id, "limit": limit, "offset": offset}
        where = ["agent_id = :agent_id"]
        if q:
            where.append("content ILIKE :q")
            params["q"] = f"%{q}%"
        if episode_id:
            where.append("episode_id = :episode_id")
            params["episode_id"] = episode_id
        where_sql = " AND ".join(where)

        try:
            async with database.session() as session:
                rows = await session.execute(
                    _sql_text(f"""
                        SELECT id::text, episode_id::text, chunk_index, content, created_at
                        FROM heart.episode_chunks
                        WHERE {where_sql}
                        ORDER BY created_at DESC, chunk_index ASC
                        LIMIT :limit OFFSET :offset
                    """),
                    params,
                )
                items = [
                    {
                        "id": r.id,
                        "episode_id": r.episode_id,
                        "chunk_index": r.chunk_index,
                        "content": r.content,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                    }
                    for r in rows
                ]
                # Total count under the same filters (drop limit/offset).
                count_params = {k: v for k, v in params.items() if k not in ("limit", "offset")}
                total_row = await session.execute(
                    _sql_text(f"SELECT COUNT(*) AS cnt FROM heart.episode_chunks WHERE {where_sql}"),
                    count_params,
                )
                total = total_row.scalar() or 0

            return JSONResponse({
                "chunks": items,
                "total": int(total),
                "limit": limit,
                "offset": offset,
            })
        except Exception as e:
            logger.error("List chunks error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def list_censors(request: Request) -> JSONResponse:
        """GET /censors - List censors with filters."""
        try:
            limit = int(request.query_params.get("limit", "50"))
            offset = int(request.query_params.get("offset", "0"))
        except ValueError:
            return JSONResponse({"error": "limit and offset must be integers"}, status_code=400)

        action = request.query_params.get("action")
        active_param = request.query_params.get("active")
        active_only = active_param != "false" if active_param else True
        domain = request.query_params.get("domain")

        try:
            censors, total = await heart.list_censors_paginated(
                limit=limit, offset=offset, action=action,
                active_only=active_only, domain=domain,
            )
            return JSONResponse({
                "censors": [c.model_dump(mode="json") for c in censors],
                "total": total,
                "limit": limit,
                "offset": offset,
            })
        except Exception as e:
            logger.error("List censors error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def update_censor(request: Request) -> JSONResponse:
        """PUT /censors/{id} - Update censor fields (F031).

        F078: ``action`` (steer|refuse|abort) and ``active`` (bool) are the UI
        severity-control path — operator=human, so any valid tier is allowed.
        """
        censor_id = request.path_params["id"]
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        update_fields = {}
        for field in (
            "trigger_action", "action_instruction", "unblock_pattern",
            "reason", "domain", "action", "active",
        ):
            if field in body:
                update_fields[field] = body[field]

        if not update_fields:
            return JSONResponse({"error": "No fields to update"}, status_code=400)

        # F078: validate action vocabulary (UI severity control).
        if "action" in update_fields and update_fields["action"] not in ("steer", "refuse", "abort"):
            return JSONResponse(
                {"error": "action must be one of: steer, refuse, abort"},
                status_code=400,
            )
        # F078: validate active is a bool.
        if "active" in update_fields and not isinstance(update_fields["active"], bool):
            return JSONResponse({"error": "active must be a boolean"}, status_code=400)

        # F031: Validate trigger_action structure if provided
        ta = update_fields.get("trigger_action")
        if ta is not None:
            if not isinstance(ta, dict):
                return JSONResponse({"error": "trigger_action must be a JSON object or null"}, status_code=400)
            from nous.heart.censor_actions import ALLOWED_TOOLS
            tool = ta.get("tool")
            if not tool or tool not in ALLOWED_TOOLS:
                return JSONResponse(
                    {"error": f"trigger_action.tool must be one of: {', '.join(sorted(ALLOWED_TOOLS))}"},
                    status_code=400,
                )

        try:
            from uuid import UUID
            detail = await heart.update_censor(UUID(censor_id), **update_fields)
            return JSONResponse(detail.model_dump(mode="json"))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except Exception as e:
            logger.error("Update censor error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def list_procedures(request: Request) -> JSONResponse:
        """GET /procedures - List procedures with filters."""
        try:
            limit = int(request.query_params.get("limit", "50"))
            offset = int(request.query_params.get("offset", "0"))
        except ValueError:
            return JSONResponse({"error": "limit and offset must be integers"}, status_code=400)
        domain = request.query_params.get("domain")
        active_param = request.query_params.get("active")
        active_only = active_param != "false" if active_param else True
        try:
            procs, total = await heart.list_procedures(limit=limit, offset=offset, domain=domain, active_only=active_only)
            return JSONResponse({"procedures": [p.model_dump(mode="json") for p in procs], "total": total, "limit": limit, "offset": offset})
        except Exception as e:
            logger.error("List procedures error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def list_frames(request: Request) -> JSONResponse:
        """GET /frames - Available cognitive frames."""
        try:
            frames = await cognitive.list_frames(settings.agent_id)
            return JSONResponse(
                {
                    "frames": [f.model_dump(mode="json") for f in frames],
                }
            )
        except Exception as e:
            logger.error("List frames error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def calibration(request: Request) -> JSONResponse:
        """GET /calibration - Full calibration report."""
        try:
            report = await brain.get_calibration()
            return JSONResponse(report.model_dump(mode="json"))
        except Exception as e:
            logger.error("Calibration error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def health(request: Request) -> JSONResponse:
        """GET /health - Health check."""
        try:
            from sqlalchemy import text

            async with database.session() as session:
                await session.execute(text("SELECT 1"))
            return JSONResponse({"status": "healthy"})
        except Exception as e:
            return JSONResponse({"status": "unhealthy", "error": str(e)}, status_code=503)

    # ------------------------------------------------------------------
    # 008: Identity endpoints
    # ------------------------------------------------------------------

    async def get_identity(request: Request) -> JSONResponse:
        """GET /identity - Get current agent identity sections."""
        if identity_manager is None:
            return JSONResponse({"error": "Identity manager not initialized"}, status_code=503)
        try:
            sections = await identity_manager.get_current()
            is_initiated = await identity_manager.is_initiated()
            return JSONResponse({
                "agent_id": identity_manager.agent_id,
                "is_initiated": is_initiated,
                "sections": sections,
            })
        except Exception as e:
            logger.error("GET /identity failed: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def update_identity_section(request: Request) -> JSONResponse:
        """PUT /identity/{section} - Update an identity section."""
        if identity_manager is None:
            return JSONResponse({"error": "Identity manager not initialized"}, status_code=503)

        section = request.path_params["section"]
        from nous.identity.manager import VALID_SECTIONS
        if section not in VALID_SECTIONS:
            return JSONResponse(
                {"error": f"Invalid section '{section}'. Valid: {', '.join(sorted(VALID_SECTIONS))}"},
                status_code=400,
            )

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        content = body.get("content")
        if not content or not isinstance(content, str):
            return JSONResponse({"error": "Missing or invalid 'content' field"}, status_code=400)

        updated_by = body.get("updated_by", "api")
        try:
            await identity_manager.update_section(section, content, updated_by=updated_by)
            return JSONResponse({"status": "updated", "section": section})
        except Exception as e:
            logger.error("PUT /identity/%s failed: %s", section, e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def reinitiate(request: Request) -> JSONResponse:
        """POST /reinitiate - Reset identity and re-run initiation protocol."""
        if identity_manager is None:
            return JSONResponse({"error": "Identity manager not initialized"}, status_code=503)
        try:
            await identity_manager.reset_identity()
            return JSONResponse({"status": "reset", "message": "Identity cleared. Next conversation will trigger initiation."})
        except Exception as e:
            logger.error("POST /reinitiate failed: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    # ------------------------------------------------------------------
    # 008.5: Decision Review Loop endpoints
    # ------------------------------------------------------------------

    async def review_decision(request: Request) -> JSONResponse:
        """POST /decisions/{id}/review — external review endpoint."""
        decision_id = request.path_params["id"]
        body = await request.json()

        outcome = body.get("outcome")
        result_text = body.get("result")
        reviewer = body.get("reviewer", "external")
        # codex #577 r3: this endpoint never read superseded_by, so external
        # supersessions landed with NULL lineage. Now that ReviewInput enforces
        # the invariant, NOT forwarding it would reject every REST supersession
        # outright — parse and pass it through.
        superseded_by_raw = body.get("superseded_by")

        if not outcome:
            return JSONResponse({"error": "outcome is required"}, status_code=400)

        superseded_by: UUID | None = None
        if superseded_by_raw:
            try:
                superseded_by = UUID(str(superseded_by_raw))
            except ValueError:
                return JSONResponse(
                    {"error": "superseded_by must be a UUID"}, status_code=400
                )

        try:
            detail = await brain.review(
                UUID(decision_id),
                outcome=outcome,
                result=result_text,
                reviewer=reviewer,
                superseded_by=superseded_by,
            )
            return JSONResponse(detail.model_dump(mode="json"))
        except ValueError as e:
            # A missing-lineage rejection is a client error, not a 404.
            status = 400 if "superseded_by is required" in str(e) else 404
            return JSONResponse({"error": str(e)}, status_code=status)

    async def list_unreviewed(request: Request) -> JSONResponse:
        """GET /decisions/unreviewed — unreviewed decisions for external agents."""
        stakes = request.query_params.get("stakes")
        max_age_days = int(request.query_params.get("max_age_days", "30"))
        limit = int(request.query_params.get("limit", "20"))

        decisions = await brain.get_unreviewed(
            max_age_days=max_age_days,
            stakes=stakes,
        )
        decisions = decisions[:limit]
        return JSONResponse({
            "decisions": [d.model_dump(mode="json") for d in decisions],
            "total": len(decisions),
        })

    # ------------------------------------------------------------------
    # 011.1: Subtask & Schedule endpoints
    # ------------------------------------------------------------------

    async def list_subtasks(request: Request) -> JSONResponse:
        """GET /subtasks?status=pending&limit=20 — list subtasks."""
        try:
            status_filter = request.query_params.get("status")
            limit = int(request.query_params.get("limit", "20"))
        except ValueError:
            return JSONResponse({"error": "limit must be an integer"}, status_code=400)

        try:
            subtasks = await heart.subtasks.list(status=status_filter, limit=limit)
            return JSONResponse({
                "subtasks": [
                    {
                        "id": str(st.id),
                        "task": st.task,
                        "status": st.status,
                        "priority": st.priority,
                        "result": st.result,
                        "error": st.error,
                        "worker_id": st.worker_id,
                        "notify": st.notify,
                        "timeout_seconds": st.timeout_seconds,
                        "created_at": st.created_at.isoformat() if st.created_at else None,
                        "started_at": st.started_at.isoformat() if st.started_at else None,
                        "completed_at": st.completed_at.isoformat() if st.completed_at else None,
                    }
                    for st in subtasks
                ],
            })
        except Exception as e:
            logger.error("List subtasks error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def get_subtask(request: Request) -> JSONResponse:
        """GET /subtasks/{id} — subtask detail."""
        subtask_id_str = request.path_params["id"]
        try:
            subtask_id = UUID(subtask_id_str)
        except ValueError:
            return JSONResponse({"error": "Invalid subtask ID"}, status_code=400)

        try:
            st = await heart.subtasks.get(subtask_id)
            if st is None:
                return JSONResponse({"error": "Subtask not found"}, status_code=404)
            return JSONResponse({
                "id": str(st.id),
                "task": st.task,
                "status": st.status,
                "priority": st.priority,
                "result": st.result,
                "error": st.error,
                "worker_id": st.worker_id,
                "notify": st.notify,
                "timeout_seconds": st.timeout_seconds,
                "created_at": st.created_at.isoformat() if st.created_at else None,
                "started_at": st.started_at.isoformat() if st.started_at else None,
                "completed_at": st.completed_at.isoformat() if st.completed_at else None,
                "metadata": st.metadata_,
            })
        except Exception as e:
            logger.error("Get subtask error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def cancel_subtask(request: Request) -> JSONResponse:
        """DELETE /subtasks/{id} — cancel a pending subtask."""
        subtask_id_str = request.path_params["id"]
        try:
            subtask_id = UUID(subtask_id_str)
        except ValueError:
            return JSONResponse({"error": "Invalid subtask ID"}, status_code=400)

        try:
            cancelled = await heart.subtasks.cancel(subtask_id)
            if not cancelled:
                return JSONResponse(
                    {"error": "Subtask not found or not in pending status"},
                    status_code=404,
                )
            return JSONResponse({"status": "cancelled", "id": subtask_id_str})
        except Exception as e:
            logger.error("Cancel subtask error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def list_schedules(request: Request) -> JSONResponse:
        """GET /schedules?active_only=true&limit=20 — list schedules."""
        try:
            active_only = request.query_params.get("active_only", "true").lower() != "false"
            limit = int(request.query_params.get("limit", "20"))
        except ValueError:
            return JSONResponse({"error": "limit must be an integer"}, status_code=400)

        try:
            schedules = await heart.schedules.list(active_only=active_only, limit=limit)
            return JSONResponse({
                "schedules": [
                    {
                        "id": str(sc.id),
                        "task": sc.task,
                        "schedule_type": sc.schedule_type,
                        "active": sc.active,
                        "cron_expr": sc.cron_expr,
                        "interval_seconds": sc.interval_seconds,
                        "next_fire_at": sc.next_fire_at.isoformat() if sc.next_fire_at else None,
                        "last_fired_at": sc.last_fired_at.isoformat() if sc.last_fired_at else None,
                        "fire_count": sc.fire_count,
                        "max_fires": sc.max_fires,
                        "notify": sc.notify,
                        "created_at": sc.created_at.isoformat() if sc.created_at else None,
                    }
                    for sc in schedules
                ],
            })
        except Exception as e:
            logger.error("List schedules error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def create_schedule(request: Request) -> JSONResponse:
        """POST /schedules — create a schedule externally."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        task = body.get("task")
        if not task:
            return JSONResponse({"error": "Missing required field: task"}, status_code=400)

        when = body.get("when")
        every = body.get("every")

        if bool(when) == bool(every):
            return JSONResponse(
                {"error": "Exactly one of 'when' or 'every' must be provided"},
                status_code=400,
            )

        try:
            from nous.handlers.time_parser import parse_every, parse_when

            notify = body.get("notify", True)
            timeout = body.get("timeout", 120)

            if when:
                fire_at = parse_when(when)
                schedule = await heart.schedules.create(
                    task=task,
                    schedule_type="once",
                    fire_at=fire_at,
                    notify=notify,
                    timeout=timeout,
                )
            else:
                interval_seconds, cron_expr = parse_every(every)
                schedule = await heart.schedules.create(
                    task=task,
                    schedule_type="recurring",
                    interval_seconds=interval_seconds,
                    cron_expr=cron_expr,
                    notify=notify,
                    timeout=timeout,
                )

            return JSONResponse({
                "id": str(schedule.id),
                "schedule_type": schedule.schedule_type,
                "next_fire_at": schedule.next_fire_at.isoformat() if schedule.next_fire_at else None,
                "active": schedule.active,
            })
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            logger.error("Create schedule error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def deactivate_schedule(request: Request) -> JSONResponse:
        """DELETE /schedules/{id} — deactivate a schedule."""
        schedule_id_str = request.path_params["id"]
        try:
            schedule_id = UUID(schedule_id_str)
        except ValueError:
            return JSONResponse({"error": "Invalid schedule ID"}, status_code=400)

        try:
            schedule = await heart.schedules.get(schedule_id)
            if schedule is None:
                return JSONResponse({"error": "Schedule not found"}, status_code=404)
            await heart.schedules.deactivate(schedule_id)
            return JSONResponse({"status": "deactivated", "id": schedule_id_str})
        except Exception as e:
            logger.error("Deactivate schedule error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    # ------------------------------------------------------------------
    # Sleep trigger endpoint (#173)
    # ------------------------------------------------------------------

    async def trigger_sleep(request: Request) -> JSONResponse:
        """POST /sleep/trigger - Manually trigger a sleep cycle."""
        try:
            if sleep_handler.is_sleeping:
                return JSONResponse(
                    {"error": "Sleep cycle already in progress"},
                    status_code=409,
                )
        except (RuntimeError, AttributeError):
            return JSONResponse(
                {"error": "Sleep handler not configured"},
                status_code=503,
            )
        try:
            _ = bus.emit  # Verify bus is available
        except (RuntimeError, AttributeError):
            return JSONResponse(
                {"error": "Event bus not configured"},
                status_code=503,
            )
        await bus.emit(Event(
            type="sleep_started",
            agent_id=settings.agent_id,
            data={"manual": True},
        ))
        return JSONResponse({"status": "started", "message": "Sleep cycle triggered"})

    # ------------------------------------------------------------------
    # Admin: runtime config endpoints (F025 prep)
    # ------------------------------------------------------------------

    async def get_search_weights(request: Request) -> JSONResponse:
        """GET /admin/search-weights — current vector/keyword weight + rrf_k + source."""
        from nous.runtime_config import RuntimeConfig

        rc = RuntimeConfig.get()
        vw = rc.get_vector_weight(settings)
        source = rc.get_vector_weight_source(settings)
        return JSONResponse({
            "vector_weight": vw,
            "keyword_weight": round(1.0 - vw, 4),
            "rrf_k": rc.get_rrf_k(settings),
            "source": source,
        })

    async def set_search_weights(request: Request) -> JSONResponse:
        """POST /admin/search-weights — update vector weight and/or rrf_k at runtime."""
        from nous.runtime_config import RuntimeConfig

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        rc = RuntimeConfig.get()

        # Optional vector_weight update
        raw = body.get("vector_weight")
        if raw is not None:
            try:
                vw = float(raw)
            except (TypeError, ValueError):
                return JSONResponse({"error": "vector_weight must be a number"}, status_code=400)
            if not (0.0 <= vw <= 1.0):
                return JSONResponse(
                    {"error": "vector_weight must be between 0.0 and 1.0"},
                    status_code=400,
                )
            old_vw = rc.get_vector_weight(settings)
            old_source = rc.get_vector_weight_source(settings)
            rc.set_vector_weight(vw)
            try:
                async with database.session() as session:
                    await rc.persist_to_db(session, "vector_weight", vw)
            except Exception as e:
                logger.error("Failed to persist vector_weight: %s", e)
            logger.info(
                "vector_weight updated to %.4f (was %.4f, source: %s)",
                vw, old_vw, old_source,
            )

        # Optional rrf_k update
        raw_k = body.get("rrf_k")
        if raw_k is not None:
            try:
                rrf_k_val = int(raw_k)
            except (TypeError, ValueError):
                return JSONResponse({"error": "rrf_k must be an integer"}, status_code=400)
            if rrf_k_val < 1:
                return JSONResponse({"error": "rrf_k must be >= 1"}, status_code=400)
            rc.set_rrf_k(rrf_k_val)
            try:
                async with database.session() as session:
                    await rc.persist_to_db(session, "rrf_k", rrf_k_val)
            except Exception as e:
                logger.error("Failed to persist rrf_k: %s", e)

        if raw is None and raw_k is None:
            return JSONResponse(
                {"error": "Must provide at least one of: vector_weight, rrf_k"},
                status_code=400,
            )

        vw_now = rc.get_vector_weight(settings)
        return JSONResponse({
            "vector_weight": vw_now,
            "keyword_weight": round(1.0 - vw_now, 4),
            "rrf_k": rc.get_rrf_k(settings),
            "source": rc.get_vector_weight_source(settings),
        })

    # ------------------------------------------------------------------
    # F021: Dashboard endpoints (route registration in Task 10)
    # ------------------------------------------------------------------

    async def dashboard_graph(request: Request) -> JSONResponse:
        """GET /dashboard/graph - Graph visualization data."""
        try:
            limit = min(int(request.query_params.get("limit", "200")), 2000)
        except ValueError:
            return JSONResponse({"error": "limit must be an integer"}, status_code=400)

        try:
            from nous.api.dashboard_queries import get_graph_data

            async with database.session() as session:
                data = await get_graph_data(session, settings.agent_id, limit=limit)
            return JSONResponse(data)
        except Exception as e:
            logger.error("Dashboard graph error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def dashboard_graph_node(request: Request) -> JSONResponse:
        """GET /dashboard/graph/node/{node_id}?type= - Single node detail + connections."""
        node_id = request.path_params["node_id"]
        node_type = request.query_params.get("type", "")
        try:
            UUID(node_id)
        except (ValueError, TypeError):
            return JSONResponse({"error": "node_id must be a UUID"}, status_code=400)
        try:
            from nous.api.dashboard_queries import (
                _NODE_DETAIL_SOURCES,
                get_node_detail,
            )

            if node_type not in _NODE_DETAIL_SOURCES:
                valid = ", ".join(sorted(_NODE_DETAIL_SOURCES))
                return JSONResponse(
                    {"error": f"type must be one of: {valid}"}, status_code=400
                )

            async with database.session() as session:
                data = await get_node_detail(
                    session, settings.agent_id, node_id, node_type
                )
            if not data.get("found"):
                return JSONResponse(data, status_code=404)
            return JSONResponse(data)
        except Exception as e:
            logger.error("Dashboard graph node error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def dashboard_calibration(request: Request) -> JSONResponse:
        """GET /dashboard/calibration - Calibration dashboard data."""
        try:
            from nous.api.dashboard_queries import get_calibration_data

            async with database.session() as session:
                data = await get_calibration_data(session, settings.agent_id)
            return JSONResponse(data)
        except Exception as e:
            logger.error("Dashboard calibration error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def dashboard_activity(request: Request) -> JSONResponse:
        """GET /dashboard/activity - Activity timeline data."""
        try:
            hours = int(request.query_params.get("hours", "168"))
        except ValueError:
            return JSONResponse({"error": "hours must be an integer"}, status_code=400)
        hours = max(1, min(hours, 720))  # Cap 1h to 30d
        try:
            from nous.api.dashboard_queries import get_activity_data

            async with database.session() as session:
                data = await get_activity_data(session, settings.agent_id, hours=hours)
            return JSONResponse(data)
        except Exception as e:
            logger.error("Dashboard activity error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def dashboard_health(request: Request) -> JSONResponse:
        """GET /dashboard/health - Graph health metrics."""
        try:
            from nous.api.dashboard_queries import get_health_data

            async with database.session() as session:
                data = await get_health_data(session, settings.agent_id)
            return JSONResponse(data)
        except Exception as e:
            logger.error("Dashboard health error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def dashboard_rubric(request: Request) -> JSONResponse:
        """GET /dashboard/rubric - Rubric analytics for dashboard."""
        try:
            from nous.api.dashboard_queries import get_rubric_dashboard_data

            async with database.session() as session:
                data = await get_rubric_dashboard_data(session, settings.agent_id, settings)
            return JSONResponse(data)
        except Exception as e:
            logger.error("Dashboard rubric error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def dashboard_admission(request: Request) -> JSONResponse:
        """GET /dashboard/admission - Admission control analytics."""
        try:
            days = int(request.query_params.get("days", "30"))
        except ValueError:
            return JSONResponse({"error": "days must be an integer"}, status_code=400)

        source_filter = request.query_params.get("source")
        category_filter = request.query_params.get("category")

        try:
            from nous.api.dashboard_queries import get_admission_data

            async with database.session() as session:
                data = await get_admission_data(
                    session, settings.agent_id,
                    days=days,
                    threshold=settings.admission_threshold,
                    source=source_filter,
                    category=category_filter,
                )
            # Prepend config block
            data["config"] = {
                "enabled": settings.admission_control_enabled,
                "shadow_mode": settings.admission_shadow_mode,
                "threshold": settings.admission_threshold,
                "weights": {
                    "utility": settings.admission_w_utility,
                    "confidence": settings.admission_w_confidence,
                    "novelty": settings.admission_w_novelty,
                    "recency": settings.admission_w_recency,
                    "type_prior": settings.admission_w_type_prior,
                },
            }
            return JSONResponse(data)
        except Exception as e:
            logger.error("Dashboard admission error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def dashboard_admission_rejected(request: Request) -> JSONResponse:
        """GET /dashboard/admission/rejected - Paginated rejected facts."""
        try:
            limit = min(int(request.query_params.get("limit", "50")), 200)
            offset = int(request.query_params.get("offset", "0"))
            days = int(request.query_params.get("days", "30"))
        except ValueError:
            return JSONResponse({"error": "limit, offset, days must be integers"}, status_code=400)

        sort = request.query_params.get("sort", "admission_score")
        order = request.query_params.get("order", "asc")

        try:
            from nous.api.dashboard_queries import get_admission_rejected

            async with database.session() as session:
                data = await get_admission_rejected(
                    session, settings.agent_id,
                    threshold=settings.admission_threshold,
                    days=days, limit=limit, offset=offset,
                    sort=sort, order=order,
                )
            return JSONResponse(data)
        except Exception as e:
            logger.error("Dashboard admission rejected error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    # --- F032: Execution Ledger Dashboard ---

    def _build_integrity_config() -> dict[str, Any]:
        """Shared helper for execution integrity config (used by /status and /dashboard/ledger)."""
        return {
            "enabled": {
                "ledger": settings.execution_ledger_enabled,
                "claim_verification": settings.claim_verification_enabled,
                "action_gating": settings.action_gating_enabled,
            },
            "modes": {
                "claim_verification": settings.claim_verification_mode,
                "action_gating": settings.action_gating_mode,
            },
        }

    async def dashboard_ledger(request: Request) -> JSONResponse:
        """GET /dashboard/ledger - Execution ledger detail for dashboard."""
        from collections import Counter

        from nous.cognitive.execution_ledger import redact_key_args

        try:
            action_limit = max(1, min(int(request.query_params.get("action_limit", "50")), 200))
        except ValueError:
            action_limit = 50

        try:
            # Snapshot ledgers to avoid concurrent-mutation issues
            ledger_snapshot = list(runner._ledgers.items())

            sessions = []
            for sid, ledger in ledger_snapshot:
                actions_snapshot = list(ledger.actions)
                status_counts: Counter[str] = Counter(a.status for a in actions_snapshot)

                # Serialize actions (most recent first, capped by limit)
                truncated = len(actions_snapshot) > action_limit
                display_actions = actions_snapshot[-action_limit:] if truncated else actions_snapshot

                serialized_actions = []
                for a in display_actions:
                    serialized_actions.append({
                        "turn": a.turn,
                        "tool_name": a.tool_name,
                        "key_args": redact_key_args(a.tool_name, a.key_args),
                        "status": a.status,
                        "timestamp": a.timestamp.isoformat(),
                        "result_summary": a.result_summary,
                        "side_effect_type": a.side_effect_type,
                    })

                sessions.append({
                    "session_id": sid,
                    "current_turn": ledger.current_turn,
                    "total_actions": len(actions_snapshot),
                    "success_actions": status_counts.get("success", 0),
                    "blocked_actions": status_counts.get("blocked", 0),
                    "error_actions": status_counts.get("error", 0),
                    "timeout_actions": status_counts.get("timeout", 0),
                    "summary": ledger.one_line_summary(),
                    "actions": serialized_actions,
                    "actions_truncated": truncated,
                })

            result = _build_integrity_config()
            result["sessions"] = sessions
            return JSONResponse(result)
        except Exception as e:
            logger.error("Dashboard ledger error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    # --- F034: Heartbeat dashboard ---

    async def dashboard_heartbeat(request: Request) -> JSONResponse:
        """GET /dashboard/heartbeat - Heartbeat overview for dashboard."""
        try:
            _ = heartbeat_runner.registry  # trigger proxy resolution
        except (RuntimeError, AttributeError):
            return JSONResponse({"error": "Heartbeat not enabled"}, status_code=503)

        try:
            hours = max(1, min(int(request.query_params.get("hours", "24")), 168))
        except ValueError:
            return JSONResponse({"error": "hours must be an integer"}, status_code=400)

        try:
            from nous.api.dashboard_queries import get_heartbeat_dashboard_data

            async with database.session() as session:
                data = await get_heartbeat_dashboard_data(
                    session, settings.agent_id, hours=hours,
                )

            # Merge in-memory state from heartbeat_runner
            budget_used = heartbeat_runner.tokens_used_today
            budget_limit = settings.heartbeat_daily_token_budget
            data["status"] = {
                "enabled": settings.heartbeat_enabled,
                "is_running": heartbeat_runner.is_running,
                "last_tick": heartbeat_runner.last_tick.isoformat() if heartbeat_runner.last_tick else None,
                "tick_interval": settings.heartbeat_tick_interval,
            }
            checks_dict = heartbeat_runner.registry.get_status()
            data["checks"] = [{"name": k, **v} for k, v in checks_dict.items()]
            data["budget"] = {
                "used": budget_used,
                "limit": budget_limit,
                "percentage": round(budget_used / budget_limit * 100, 1) if budget_limit > 0 else 0,
            }
            data["quiet_hours"] = {
                "start": settings.heartbeat_quiet_start,
                "end": settings.heartbeat_quiet_end,
                "active": heartbeat_runner.is_quiet,
            }

            # F034.1: Finding lifecycle stats
            store = heartbeat_runner.finding_store
            if store is not None:
                data["finding_lifecycle"] = {
                    "stats": store.stats(),
                    "findings": store.to_list(),
                    "escalation_policy": {
                        "low_to_normal_hours": store._escalation.low_to_normal_hours,
                        "normal_to_high_hours": store._escalation.normal_to_high_hours,
                        "high_realert_hours": store._escalation.high_realert_hours,
                        "accumulation_threshold": store._escalation.accumulation_threshold,
                    },
                }
            else:
                data["finding_lifecycle"] = None

            # F034.3: Tuning status
            tuner = heartbeat_runner.tuner
            last_report = tuner.last_report
            data["tuning"] = {
                "enabled": settings.heartbeat_tuning_enabled,
                "last_report": {
                    "adjustments": len(last_report.adjustments),
                    "skipped_checks": last_report.skipped_checks,
                    "timestamp": last_report.timestamp.isoformat() if last_report.timestamp else None,
                    "summary": tuner.generate_report_text(last_report),
                } if last_report else None,
            }

            return JSONResponse(data)
        except Exception as e:
            logger.error("Dashboard heartbeat error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    # --- F035: Observability dashboard ---

    async def dashboard_observability(request: Request) -> JSONResponse:
        """GET /dashboard/observability - Aggregated observability data."""
        from datetime import UTC, datetime, timedelta

        result = {}

        # 1. Event bus stats (in-memory, instant)
        if bus is not None:
            stats = bus.stats.to_dict()
            stats["queue_depth"] = bus.pending
            result["event_bus"] = stats
        else:
            result["event_bus"] = {"total_processed": 0, "total_dropped": 0, "handlers": {}, "event_counts": {}}

        # 2. Recent traces (last 10)
        try:
            async with database.session() as session:
                from sqlalchemy import text
                tr = await session.execute(text("""
                    WITH trace_stats AS (
                        SELECT trace_id,
                               COUNT(*) AS event_count,
                               BOOL_OR(data->>'modifies' IS NOT NULL) AS has_modifications
                        FROM nous_system.events
                        WHERE trace_id IS NOT NULL
                        AND agent_id = :aid
                        GROUP BY trace_id
                    ),
                    roots AS (
                        SELECT event_id, event_type, trace_id, created_at
                        FROM nous_system.events
                        WHERE trace_id IS NOT NULL AND caused_by IS NULL
                        AND agent_id = :aid
                    )
                    SELECT r.trace_id, r.event_type AS root_type, r.created_at,
                           ts.event_count, ts.has_modifications
                    FROM roots r
                    JOIN trace_stats ts ON ts.trace_id = r.trace_id
                    ORDER BY r.created_at DESC
                    LIMIT 10
                """), {"aid": settings.agent_id})
                rows = tr.fetchall()
            result["recent_traces"] = [{
                "trace_id": r.trace_id, "root_type": r.root_type,
                "timestamp": r.created_at.isoformat() if r.created_at else None,
                "event_count": r.event_count, "has_modifications": bool(r.has_modifications),
            } for r in rows]
        except Exception:
            logger.debug("dashboard_observability: recent_traces failed", exc_info=True)
            result["recent_traces"] = []

        # 3. Recent modifications (last 24h)
        try:
            async with database.session() as session:
                from sqlalchemy import text
                mr = await session.execute(text("""
                    SELECT event_id, event_type, trace_id, data, created_at
                    FROM nous_system.events
                    WHERE data->>'modifies' IS NOT NULL
                    AND agent_id = :aid
                    AND created_at > NOW() - INTERVAL '24 hours'
                    ORDER BY created_at DESC LIMIT 20
                """), {"aid": settings.agent_id})
                rows = mr.fetchall()
            result["recent_modifications"] = [{
                "event_id": r.event_id, "type": r.event_type, "trace_id": r.trace_id,
                "modifies": (r.data or {}).get("modifies"),
                "timestamp": r.created_at.isoformat() if r.created_at else None,
            } for r in rows]
        except Exception:
            logger.debug("dashboard_observability: recent_modifications failed", exc_info=True)
            result["recent_modifications"] = []

        # 4. Drift snapshot (latest + anomalies)
        try:
            async with database.session() as session:
                from sqlalchemy import text
                sr = await session.execute(text(
                    "SELECT timestamp, metrics, anomalies FROM nous_system.behavior_snapshots "
                    "WHERE agent_id = :aid ORDER BY timestamp DESC LIMIT 1"
                ), {"aid": settings.agent_id})
                row = sr.fetchone()
            if row:
                result["drift"] = {
                    "timestamp": row.timestamp.isoformat(),
                    "metrics": row.metrics,
                    "anomalies": row.anomalies or [],
                }
            else:
                result["drift"] = None
        except Exception:
            logger.debug("dashboard_observability: drift snapshot failed", exc_info=True)
            result["drift"] = None

        # 5. Drift trends (last 7 days — key metrics only)
        try:
            async with database.session() as session:
                from sqlalchemy import text
                cutoff = datetime.now(UTC) - timedelta(days=7)
                tr2 = await session.execute(text(
                    "SELECT timestamp, metrics FROM nous_system.behavior_snapshots "
                    "WHERE agent_id = :aid AND timestamp > :cutoff ORDER BY timestamp"
                ), {"aid": settings.agent_id, "cutoff": cutoff})
                rows = tr2.fetchall()
            trend_metrics = ["fact_count_delta", "handler_error_rate"]
            trends = {m: [] for m in trend_metrics}
            for row in rows:
                metrics = row.metrics if isinstance(row.metrics, dict) else {}
                ts = row.timestamp.isoformat()
                for m in trend_metrics:
                    trends[m].append({"t": ts, "v": metrics.get(m, 0)})
            result["drift_trends"] = trends
        except Exception:
            logger.debug("dashboard_observability: drift_trends failed", exc_info=True)
            result["drift_trends"] = {}

        # 6. Context log (last 10 calls)
        if context_logger is not None:
            entries = context_logger.get_recent(limit=10)
            result["context_log"] = [e.to_dict() for e in entries]
        else:
            result["context_log"] = []

        return JSONResponse(result)

    async def dashboard_cache(request: Request) -> JSONResponse:
        """GET /dashboard/cache - Prompt cache efficiency metrics (F036.1)."""
        if context_logger is None:
            return JSONResponse({"error": "Context logging not enabled"}, status_code=503)

        entries = context_logger.get_recent(limit=200)

        # Filter to entries that have actual token data (API has responded)
        calls = [e for e in entries if e.input_tokens_actual is not None]

        total_calls = len(calls)
        # Anthropic API reports input_tokens as non-cached only.
        # Total input = input_tokens + cache_creation + cache_read.
        total_cache_read = sum(e.cache_read_tokens or 0 for e in calls)
        total_cache_created = sum(e.cache_creation_tokens or 0 for e in calls)
        total_input = (
            sum(e.input_tokens_actual or 0 for e in calls)
            + total_cache_read + total_cache_created
        )
        total_breaks = sum(1 for e in calls if e.cache_break)
        total_break_tokens = sum(e.cache_break_tokens_lost for e in calls if e.cache_break)

        # Per-session aggregation
        sessions: dict[str, dict] = {}
        for e in calls:
            sid = e.session_id
            if sid not in sessions:
                sessions[sid] = {"calls": 0, "input": 0, "cache_read": 0, "cache_created": 0, "breaks": 0}
            s = sessions[sid]
            s["calls"] += 1
            s["cache_read"] += e.cache_read_tokens or 0
            s["cache_created"] += e.cache_creation_tokens or 0
            # Total input = non-cached + cache_creation + cache_read
            s["input"] += (
                (e.input_tokens_actual or 0)
                + (e.cache_read_tokens or 0)
                + (e.cache_creation_tokens or 0)
            )
            if e.cache_break:
                s["breaks"] += 1

        # Break component distribution
        component_counts: dict[str, int] = {}
        for e in calls:
            if e.cache_break:
                for comp in e.cache_break_components:
                    component_counts[comp] = component_counts.get(comp, 0) + 1

        # Per-call timeline (newest first, last 50)
        timeline = []
        for e in calls[:50]:
            cache_read = e.cache_read_tokens or 0
            cache_created = e.cache_creation_tokens or 0
            # Total input = non-cached + cache_creation + cache_read
            input_tok = (e.input_tokens_actual or 0) + cache_read + cache_created
            hit_rate = round(cache_read / input_tok * 100, 1) if input_tok > 0 else 0
            timeline.append({
                "timestamp": e.timestamp,
                "session_id": e.session_id,
                "turn": e.turn_number,
                "model": e.model,
                "input_tokens": input_tok,
                "cache_read": cache_read,
                "cache_created": cache_created,
                "hit_rate": hit_rate,
                "cache_break": e.cache_break,
                "break_components": e.cache_break_components if e.cache_break else [],
            })

        # Session list sorted by calls descending
        session_list = []
        for sid, s in sessions.items():
            hit_rate = round(s["cache_read"] / s["input"] * 100, 1) if s["input"] > 0 else 0
            session_list.append({
                "session_id": sid,
                "calls": s["calls"],
                "input_tokens": s["input"],
                "cache_read": s["cache_read"],
                "cache_created": s["cache_created"],
                "hit_rate": hit_rate,
                "breaks": s["breaks"],
            })
        session_list.sort(key=lambda x: x["calls"], reverse=True)

        return JSONResponse({
            "summary": {
                "total_calls": total_calls,
                "total_input_tokens": total_input,
                "total_cache_read": total_cache_read,
                "total_cache_created": total_cache_created,
                "overall_hit_rate": round(total_cache_read / total_input * 100, 1) if total_input > 0 else 0,
                "total_breaks": total_breaks,
                "break_rate": round(total_breaks / total_calls * 100, 1) if total_calls > 0 else 0,
                "tokens_lost_to_breaks": total_break_tokens,
            },
            "break_components": component_counts,
            "sessions": session_list,
            "timeline": timeline,
        })

    # --- F038: DAG Orchestration dashboard ---

    async def dashboard_dag(request: Request) -> JSONResponse:
        """GET /dashboard/dag - DAG orchestration overview for dashboard."""
        try:
            from nous.api.dashboard_queries import get_dag_dashboard_data

            async with database.session() as session:
                data = await get_dag_dashboard_data(session, settings.agent_id)
            return JSONResponse(data)
        except Exception as e:
            logger.error("Dashboard DAG error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    # --- F040: Graph density dashboard ---

    async def dashboard_density(request: Request) -> JSONResponse:
        """GET /dashboard/density - Graph density metrics for dashboard."""
        try:
            from nous.api.dashboard_queries import get_density_data

            async with database.session() as session:
                data = await get_density_data(session, settings.agent_id)
            return JSONResponse(data)
        except Exception as e:
            logger.error("Dashboard density error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    # --- F091: Retrieval telemetry dashboard ---

    async def dashboard_retrieval(request: Request) -> JSONResponse:
        """GET /dashboard/retrieval - Recent retrievals + aggregate dispositions."""
        try:
            limit = max(1, min(int(request.query_params.get("limit", "50")), 200))
        except ValueError:
            return JSONResponse({"error": "limit must be an integer"}, status_code=400)
        path_filter = request.query_params.get("path")
        # "script" is run_python's in-script recall_deep. It is a THIRD path, not
        # a flavour of "pipeline": both run run_recall_pipeline, but only this one
        # can fire several times inside a single tool call, so blending them would
        # make per-turn retrieval counts silently wrong.
        if path_filter and path_filter not in _RETRIEVAL_PATHS:
            return JSONResponse(
                {"error": f"path must be one of {', '.join(sorted(_RETRIEVAL_PATHS))}"},
                status_code=400,
            )

        try:
            from nous.api.dashboard_queries import get_retrieval_data

            async with database.session() as session:
                data = await get_retrieval_data(
                    session, settings.agent_id, limit=limit, path=path_filter,
                )
            return JSONResponse(data)
        except Exception as e:
            logger.error("Dashboard retrieval error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def dashboard_retrieval_detail(request: Request) -> JSONResponse:
        """GET /dashboard/retrieval/{entry_id} - One retrieval's candidates + expansions."""
        entry_id = request.path_params["entry_id"]
        # Ids are uuid4().hex[:16]. `isalnum()` is Unicode-aware, so it let
        # non-hex through while reading like a validation control; match the
        # actual shape instead.
        if not re.fullmatch(r"[0-9a-f]{16}", entry_id or ""):
            return JSONResponse({"error": "invalid entry_id"}, status_code=400)

        try:
            from nous.api.dashboard_queries import get_retrieval_detail

            async with database.session() as session:
                data = await get_retrieval_detail(session, settings.agent_id, entry_id)
            if data is None:
                return JSONResponse({"error": "not found"}, status_code=404)
            return JSONResponse(data)
        except Exception as e:
            logger.error("Dashboard retrieval detail error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    # --- F035.6: Consolidation audit diff dashboard ---

    async def dashboard_consolidation(request: Request) -> JSONResponse:
        """GET /dashboard/consolidation - Recent consolidation cycles."""
        try:
            limit = max(1, min(int(request.query_params.get("limit", "30")), 200))
        except ValueError:
            return JSONResponse({"error": "limit must be an integer"}, status_code=400)

        try:
            from nous.api.dashboard_queries import get_consolidation_data

            async with database.session() as session:
                data = await get_consolidation_data(
                    session, settings.agent_id, limit=limit,
                )
            return JSONResponse(data)
        except Exception as e:
            logger.error("Dashboard consolidation error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def dashboard_consolidation_detail(request: Request) -> JSONResponse:
        """GET /dashboard/consolidation/{cycle_id} - One cycle's action diffs."""
        cycle_id = request.path_params["cycle_id"]
        try:
            UUID(cycle_id)
        except (ValueError, TypeError):
            return JSONResponse({"error": "cycle_id must be a UUID"}, status_code=400)

        try:
            from nous.api.dashboard_queries import get_consolidation_cycle_detail

            async with database.session() as session:
                data = await get_consolidation_cycle_detail(
                    session, settings.agent_id, cycle_id,
                )
            if data.get("cycle") is None:
                return JSONResponse(data, status_code=404)
            return JSONResponse(data)
        except Exception as e:
            logger.error("Dashboard consolidation detail error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    # --- F061: Subtask hardening dashboard ---

    async def dashboard_subtasks(request: Request) -> JSONResponse:
        """GET /dashboard/subtasks - Subtask outcome metrics for dashboard.

        Query params:
          - hours: window length in hours (default 24, max 168 = 7 days).
        """
        try:
            hours = max(1, min(int(request.query_params.get("hours", "24")), 168))
        except ValueError:
            return JSONResponse({"error": "hours must be an integer"}, status_code=400)

        try:
            from nous.api.dashboard_queries import get_subtask_dashboard_data

            async with database.session() as session:
                data = await get_subtask_dashboard_data(
                    session, settings.agent_id, hours=hours,
                )
            return JSONResponse(data)
        except Exception as e:
            logger.exception("Dashboard subtasks error")
            return JSONResponse({"error": str(e)}, status_code=500)

    # --- F024 Phase 3b: Rubric endpoints ---

    async def get_rubric(request: Request) -> JSONResponse:
        """GET /rubric — current active rubric version."""
        try:
            _ = rubric_manager.get_active  # trigger proxy resolution
        except (RuntimeError, AttributeError):
            return JSONResponse({"error": "Rubric system not enabled"}, status_code=503)
        active = await rubric_manager.get_active()
        if not active:
            return JSONResponse({"error": "No active rubric"}, status_code=404)
        detail = rubric_manager.to_detail(active)
        return JSONResponse(detail.model_dump(mode="json"))

    async def get_rubric_history(request: Request) -> JSONResponse:
        """GET /rubric/history — rubric version history."""
        try:
            _ = rubric_manager.get_history  # trigger proxy resolution
        except (RuntimeError, AttributeError):
            return JSONResponse({"error": "Rubric system not enabled"}, status_code=503)
        limit = int(request.query_params.get("limit", "20"))
        history = await rubric_manager.get_history(limit=limit)
        return JSONResponse([h.model_dump(mode="json") for h in history])

    async def get_outcome_signals(request: Request) -> JSONResponse:
        """GET /rubric/signals — outcome signals with optional episode filter."""
        try:
            _ = rubric_manager.get_signals
        except (RuntimeError, AttributeError):
            return JSONResponse({"error": "Rubric system not enabled"}, status_code=503)
        episode_id = request.query_params.get("episode_id")
        signals = await rubric_manager.get_signals(episode_id=episode_id)
        return JSONResponse(signals)

    async def trigger_evolution(request: Request) -> JSONResponse:
        """POST /rubric/evolve — manually trigger a rubric evolution cycle."""
        try:
            _ = rubric_evolver.run_evolution_cycle  # trigger proxy resolution
        except (RuntimeError, AttributeError):
            return JSONResponse({"error": "Rubric evolution not enabled"}, status_code=503)
        if not rubric_evolver:
            return JSONResponse({"error": "Rubric evolution not enabled"}, status_code=503)
        try:
            report = await rubric_evolver.run_evolution_cycle()
            if report:
                return JSONResponse(report.model_dump(mode="json"))
            return JSONResponse({"status": "no_change", "message": "Insufficient data or no weight changes needed"})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def propose_dimension(request: Request) -> JSONResponse:
        """POST /rubric/propose-dimension — propose a new rubric dimension (Tim approval required)."""
        try:
            _ = rubric_manager.get_active
        except (RuntimeError, AttributeError):
            return JSONResponse({"error": "Rubric system not enabled"}, status_code=503)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        required = ["name", "description", "scoring_criteria", "gap_analysis"]
        missing = [k for k in required if k not in body]
        if missing:
            return JSONResponse({"error": f"Missing fields: {missing}"}, status_code=400)

        # Store as a pending proposal fact for Tim's review
        from nous.heart.schemas import FactInput
        fact = FactInput(
            content=f"[RUBRIC PROPOSAL] New dimension: {body['name']}\n\n"
                    f"Description: {body['description']}\n"
                    f"Scoring: {body['scoring_criteria']}\n"
                    f"Evidence: {body['gap_analysis']}\n"
                    f"Suggested weight: {body.get('suggested_weight', 0.15)}",
            category="technical",
            subject="rubric_dimension_proposal",
            source="f024_phase3b",
            tags=["rubric", "proposal", "pending_approval"],
        )
        result = await heart.learn(fact)

        return JSONResponse({
            "status": "pending_approval",
            "fact_id": str(result.id) if result else None,
            "message": "Dimension proposal stored. Requires Tim's approval to activate.",
        }, status_code=201)

    async def list_proposals(request: Request) -> JSONResponse:
        """GET /rubric/proposals — list pending dimension proposals."""
        try:
            _ = heart.search_facts
        except (RuntimeError, AttributeError):
            return JSONResponse({"error": "System not available"}, status_code=503)

        results = await heart.search_facts(
            query="rubric_dimension_proposal",
            limit=20,
            category="technical",
        )
        return JSONResponse([
            {
                "id": str(f.id),
                "content": f.content,
                "created_at": f.created_at.isoformat() if hasattr(f, "created_at") and f.created_at else None,
            }
            for f in results
        ])

    async def approve_proposal(request: Request) -> JSONResponse:
        """POST /rubric/proposals/{id}/approve — approve a proposed dimension."""
        try:
            _ = rubric_manager.get_active
        except (RuntimeError, AttributeError):
            return JSONResponse({"error": "Rubric system not enabled"}, status_code=503)

        try:
            body = await request.json()
        except Exception:
            body = {}

        name = body.get("name")
        description = body.get("description")
        scoring_criteria = body.get("scoring_criteria", "1-10 scale")
        weight = float(body.get("weight", 0.15))

        if not name or not description:
            return JSONResponse({"error": "Approval must include 'name' and 'description'"}, status_code=400)

        active = await rubric_manager.get_active()
        if not active:
            return JSONResponse({"error": "No active rubric"}, status_code=404)

        new_dims = list(active.dimensions) + [{
            "name": name,
            "weight": weight,
            "description": description,
            "scoring_criteria": scoring_criteria,
            "min_weight": 0.10,
            "max_weight": 0.40,
        }]

        # Normalize weights
        from nous.cognitive.correlation import _normalize_weights
        norm = _normalize_weights({d["name"]: d["weight"] for d in new_dims})
        for d in new_dims:
            d["weight"] = norm[d["name"]]

        base_version = active.version.split("-")[0]
        parts = base_version.split(".")
        new_version = f"{int(parts[0]) + 1}.0.0"

        try:
            await rubric_manager.create_version(
                new_version=new_version,
                dimensions=new_dims,
                change_reason=f"Phase 3: Added '{name}' dimension (Tim approved)",
            )
            return JSONResponse({"status": "approved", "new_version": new_version})
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    async def rollback_rubric(request: Request) -> JSONResponse:
        """POST /rubric/rollback — rollback to a previous version."""
        try:
            _ = rubric_manager.rollback
        except (RuntimeError, AttributeError):
            return JSONResponse({"error": "Rubric system not enabled"}, status_code=503)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        target = body.get("target_version")
        if not target:
            return JSONResponse({"error": "Missing target_version"}, status_code=400)
        result = await rubric_manager.rollback(target)
        if result:
            return JSONResponse({"status": "rolled_back", "new_version": result.version})
        return JSONResponse({"error": "Target version not found"}, status_code=404)

    # ------------------------------------------------------------------
    # Heartbeat (F034)
    # ------------------------------------------------------------------

    async def heartbeat_status(request: Request) -> JSONResponse:
        """GET /heartbeat/status — check statuses, budget, last run."""
        try:
            _ = heartbeat_runner.registry
        except (RuntimeError, AttributeError):
            return JSONResponse({"error": "Heartbeat not enabled"}, status_code=503)

        return JSONResponse({
            "checks": heartbeat_runner.registry.get_status(),
            "tokens_used_today": heartbeat_runner.tokens_used_today,
            "daily_budget": settings.heartbeat_daily_token_budget,
            "last_tick": heartbeat_runner.last_tick.isoformat() if heartbeat_runner.last_tick else None,
        })

    async def heartbeat_trigger(request: Request) -> JSONResponse:
        """POST /heartbeat/trigger — force immediate tick."""
        try:
            _ = heartbeat_runner.registry
        except (RuntimeError, AttributeError):
            return JSONResponse({"error": "Heartbeat not enabled"}, status_code=503)

        findings = await heartbeat_runner.trigger_tick()
        return JSONResponse({
            "status": "triggered",
            "findings_count": len(findings),
            "findings": [{"source": f.source, "summary": f.summary, "urgency": f.urgency} for f in findings],
        })

    async def heartbeat_config(request: Request) -> JSONResponse:
        """PUT /heartbeat/config — update intervals, quiet hours, budget at runtime."""
        try:
            _ = heartbeat_runner.registry
        except (RuntimeError, AttributeError):
            return JSONResponse({"error": "Heartbeat not enabled"}, status_code=503)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        updated = []
        # Map of config field -> check name (for propagating interval changes)
        interval_to_check = {
            "heartbeat_health_interval": "health",
            "heartbeat_self_initiated_interval": "self_initiated",
        }

        for field_name in ("heartbeat_tick_interval", "heartbeat_quiet_start",
                           "heartbeat_quiet_end", "heartbeat_daily_token_budget",
                           "heartbeat_health_interval", "heartbeat_self_initiated_interval"):
            short = field_name.replace("heartbeat_", "")
            if short in body:
                val = body[short]
                if isinstance(val, int) and val >= 0:
                    object.__setattr__(settings, field_name, val)
                    updated.append(short)
                    # Propagate interval changes to running checks
                    check_name = interval_to_check.get(field_name)
                    if check_name:
                        check = heartbeat_runner.registry.get_check(check_name)
                        if check:
                            check.interval = val

        return JSONResponse({"status": "updated", "fields": updated})

    async def heartbeat_check_trigger(request: Request) -> JSONResponse:
        """POST /heartbeat/check/{name}/trigger — force specific check."""
        try:
            _ = heartbeat_runner.registry
        except (RuntimeError, AttributeError):
            return JSONResponse({"error": "Heartbeat not enabled"}, status_code=503)

        name = request.path_params["name"]
        check = heartbeat_runner.registry.get_check(name)
        if check is None:
            return JSONResponse({"error": f"Check '{name}' not found"}, status_code=404)

        try:
            result = await heartbeat_runner.trigger_check(name)
            return JSONResponse({
                "status": "triggered",
                "has_updates": result.has_updates if result else False,
                "findings": [
                    {"source": f.source, "summary": f.summary, "urgency": f.urgency}
                    for f in (result.findings if result else [])
                ],
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def heartbeat_check_reset(request: Request) -> JSONResponse:
        """POST /heartbeat/check/{name}/reset — reset circuit breaker."""
        try:
            _ = heartbeat_runner.registry
        except (RuntimeError, AttributeError):
            return JSONResponse({"error": "Heartbeat not enabled"}, status_code=503)

        name = request.path_params["name"]
        check = heartbeat_runner.registry.get_check(name)
        if check is None:
            return JSONResponse({"error": f"Check '{name}' not found"}, status_code=404)

        check.reset_circuit_breaker()
        return JSONResponse({"status": "reset", "check": name})

    # ------------------------------------------------------------------
    # Heartbeat Finding Lifecycle (F034.1)
    # ------------------------------------------------------------------

    async def heartbeat_findings(request: Request) -> JSONResponse:
        """GET /heartbeat/findings — All tracked findings with state/age."""
        try:
            _ = heartbeat_runner.registry  # trigger proxy
        except (RuntimeError, AttributeError):
            return JSONResponse({"error": "Heartbeat not available"}, status_code=503)
        store = heartbeat_runner.finding_store
        if store is None:
            return JSONResponse({"findings": [], "stats": {}})
        return JSONResponse({"findings": store.to_list(), "stats": store.stats()})

    async def heartbeat_findings_acknowledge(request: Request) -> JSONResponse:
        """POST /heartbeat/findings/{fingerprint}/acknowledge"""
        fp = request.path_params["fingerprint"]
        store = heartbeat_runner.finding_store
        if store is None:
            return JSONResponse({"error": "Finding store not available"}, status_code=503)
        ok = store.acknowledge(fp)
        if not ok:
            return JSONResponse({"error": "Finding not found"}, status_code=404)
        return JSONResponse({"acknowledged": fp})

    async def heartbeat_findings_resolve(request: Request) -> JSONResponse:
        """POST /heartbeat/findings/{fingerprint}/resolve"""
        from nous.heartbeat.schemas import OutcomeSignal

        fp = request.path_params["fingerprint"]
        store = heartbeat_runner.finding_store
        if store is None:
            return JSONResponse({"error": "Finding store not available"}, status_code=503)
        ok = store.resolve(fp)
        if not ok:
            return JSONResponse({"error": "Finding not found"}, status_code=404)
        # Record positive outcome (user cared enough to resolve)
        store.record_outcome(fp, OutcomeSignal.POSITIVE)
        return JSONResponse({"resolved": fp})

    async def heartbeat_findings_dismiss(request: Request) -> JSONResponse:
        """POST /heartbeat/findings/{fingerprint}/dismiss"""
        fp = request.path_params["fingerprint"]
        store = heartbeat_runner.finding_store
        if store is None:
            return JSONResponse({"error": "Finding store not available"}, status_code=503)
        ok = store.dismiss(fp)
        if not ok:
            return JSONResponse({"error": "Finding not found"}, status_code=404)
        return JSONResponse({"dismissed": fp})

    async def heartbeat_escalation_policy(request: Request) -> JSONResponse:
        """PUT /heartbeat/escalation-policy — Update escalation thresholds."""
        store = heartbeat_runner.finding_store
        if store is None:
            return JSONResponse({"error": "Finding store not available"}, status_code=503)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        cfg = store._escalation
        minimums = {
            "low_to_normal_hours": 1,
            "normal_to_high_hours": 1,
            "high_realert_hours": 1,
            "accumulation_threshold": 2,
        }
        for field_name, min_val in minimums.items():
            if field_name in body:
                val = int(body[field_name])
                if val < min_val:
                    return JSONResponse(
                        {"error": f"{field_name} must be >= {min_val}"}, status_code=400,
                    )
                setattr(cfg, field_name, val)
        return JSONResponse({
            "low_to_normal_hours": cfg.low_to_normal_hours,
            "normal_to_high_hours": cfg.normal_to_high_hours,
            "high_realert_hours": cfg.high_realert_hours,
            "accumulation_threshold": cfg.accumulation_threshold,
        })

    # ------------------------------------------------------------------
    # Heartbeat Tuning (F034.3)
    # ------------------------------------------------------------------

    async def heartbeat_tuning_report(request: Request) -> JSONResponse:
        """GET /heartbeat/tuning-report — Latest tuning report."""
        try:
            _ = heartbeat_runner.registry
        except Exception:
            return JSONResponse({"error": "Heartbeat not available"}, status_code=503)
        tuner = heartbeat_runner.tuner
        report = tuner.last_report
        report_data = None
        if report is not None:
            report_data = {
                "adjustments": [
                    {
                        "check_name": a.check_name,
                        "param_name": a.param_name,
                        "old_value": a.old_value,
                        "new_value": a.new_value,
                        "direction": a.direction,
                        "sample_count": a.sample_count,
                    }
                    for a in report.adjustments
                ],
                "skipped_checks": report.skipped_checks,
                "timestamp": report.timestamp.isoformat() if report.timestamp else None,
                "report_text": tuner.generate_report_text(report),
            }
        return JSONResponse({
            "report": report_data,
            "tuning_enabled": settings.heartbeat_tuning_enabled,
        })

    async def heartbeat_tune(request: Request) -> JSONResponse:
        """POST /heartbeat/tune — Force tuning pass."""
        if not settings.heartbeat_tuning_enabled:
            return JSONResponse({"error": "Tuning not enabled"}, status_code=400)
        store = heartbeat_runner.finding_store
        if store is None:
            return JSONResponse({"error": "Finding store not available"}, status_code=503)
        tuner = heartbeat_runner.tuner
        report = await tuner.tune(store, heartbeat_runner.registry)
        return JSONResponse({
            "adjustments": len(report.adjustments),
            "skipped": report.skipped_checks,
            "report_text": tuner.generate_report_text(report),
        })

    # ------------------------------------------------------------------
    # F034.5: Dynamic heartbeat check endpoints
    # ------------------------------------------------------------------

    async def dynamic_checks_list(request: Request) -> JSONResponse:
        """GET /heartbeat/checks/dynamic — list all dynamic checks."""
        try:
            loader = heartbeat_runner.dynamic_loader
        except (RuntimeError, AttributeError):
            return JSONResponse({"error": "Heartbeat not enabled"}, status_code=503)
        if loader is None:
            return JSONResponse({"error": "Dynamic checks not enabled"}, status_code=503)
        result = await loader.manage_check(action="list")
        return JSONResponse(result)

    async def dynamic_checks_create(request: Request) -> JSONResponse:
        """POST /heartbeat/checks/dynamic — create a new dynamic check."""
        try:
            loader = heartbeat_runner.dynamic_loader
        except (RuntimeError, AttributeError):
            return JSONResponse({"error": "Heartbeat not enabled"}, status_code=503)
        if loader is None:
            return JSONResponse({"error": "Dynamic checks not enabled"}, status_code=503)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        try:
            result = await loader.create_check(
                name=body.get("name", ""),
                description=body.get("description", ""),
                prompt=body.get("prompt", ""),
                tools=body.get("tools"),
                interval_seconds=body.get("interval_seconds", 3600),
                cron_expr=body.get("cron_expr"),
                timeout_seconds=body.get("timeout_seconds"),
                urgent=body.get("urgent", False),
                on_complete_prompt=body.get("on_complete_prompt"),
                on_complete_tools=body.get("on_complete_tools"),
            )
            return JSONResponse(result, status_code=201)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def dynamic_checks_update(request: Request) -> JSONResponse:
        """PATCH /heartbeat/checks/dynamic/{name} — update a dynamic check."""
        try:
            loader = heartbeat_runner.dynamic_loader
        except (RuntimeError, AttributeError):
            return JSONResponse({"error": "Heartbeat not enabled"}, status_code=503)
        if loader is None:
            return JSONResponse({"error": "Dynamic checks not enabled"}, status_code=503)
        name = request.path_params["name"]
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        try:
            result = await loader.manage_check(action="update", name=name, updates=body)
            return JSONResponse(result)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def dynamic_checks_delete(request: Request) -> JSONResponse:
        """DELETE /heartbeat/checks/dynamic/{name} — delete a dynamic check."""
        try:
            loader = heartbeat_runner.dynamic_loader
        except (RuntimeError, AttributeError):
            return JSONResponse({"error": "Heartbeat not enabled"}, status_code=503)
        if loader is None:
            return JSONResponse({"error": "Dynamic checks not enabled"}, status_code=503)
        name = request.path_params["name"]
        try:
            result = await loader.manage_check(action="delete", name=name)
            return JSONResponse(result)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def dynamic_checks_trigger(request: Request) -> JSONResponse:
        """POST /heartbeat/checks/dynamic/{name}/trigger — force-run a dynamic check."""
        try:
            loader = heartbeat_runner.dynamic_loader
        except (RuntimeError, AttributeError):
            return JSONResponse({"error": "Heartbeat not enabled"}, status_code=503)
        if loader is None:
            return JSONResponse({"error": "Dynamic checks not enabled"}, status_code=503)
        name = request.path_params["name"]
        check = heartbeat_runner.registry.get_check(name)
        if check is None:
            return JSONResponse({"error": f"Check '{name}' not found"}, status_code=404)
        try:
            result = await heartbeat_runner.trigger_check(name)
            return JSONResponse({
                "status": "triggered",
                "has_updates": result.has_updates if result else False,
                "findings": [
                    {"source": f.source, "summary": f.summary, "urgency": f.urgency}
                    for f in (result.findings if result else [])
                ],
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    # ------------------------------------------------------------------
    # F035.1: Event bus observability
    # ------------------------------------------------------------------

    async def events_stats(request: Request) -> JSONResponse:
        """GET /events/stats — Event bus statistics (F035.1)."""
        if bus is None:
            return JSONResponse({"total_processed": 0, "total_dropped": 0, "handlers": {}, "event_counts": {}})
        data = bus.stats.to_dict()
        data["queue_depth"] = bus.pending
        component_stats = {}
        if session_monitor and hasattr(session_monitor, "get_stats"):
            component_stats["session_monitor"] = session_monitor.get_stats()
        if sleep_handler and hasattr(sleep_handler, "get_stats"):
            component_stats["sleep_handler"] = sleep_handler.get_stats()
        if heartbeat_runner and hasattr(heartbeat_runner, "get_stats"):
            component_stats["heartbeat_runner"] = heartbeat_runner.get_stats()
        if component_stats:
            data["component_stats"] = component_stats
        return JSONResponse(data)

    async def events_recent(request: Request) -> JSONResponse:
        """GET /events/recent — Recent events ring buffer (F035.1)."""
        limit = int(request.query_params.get("limit", "20"))
        if bus is None:
            return JSONResponse({"events": [], "source": "memory", "count": 0})
        events = bus.stats.recent_events(limit=limit)
        return JSONResponse({
            "events": [{"type": e.type, "timestamp": e.timestamp, "handlers_invoked": e.handlers_invoked, "handlers_failed": e.handlers_failed, "duration_ms": round(e.duration_ms, 2), "session_id": e.session_id} for e in events],
            "source": "memory", "count": len(events),
        })

    # ------------------------------------------------------------------
    # F035.2: Causal chain tracing endpoints
    # ------------------------------------------------------------------

    async def events_trace(request: Request) -> JSONResponse:
        """GET /events/trace/{trace_id} — All events in a causal chain."""
        trace_id = request.path_params["trace_id"]
        async with database.session() as session:
            from sqlalchemy import text
            result = await session.execute(text("""
                SELECT event_id, event_type, session_id, data, created_at, trace_id, caused_by
                FROM nous_system.events
                WHERE trace_id = :tid
                ORDER BY created_at ASC
            """), {"tid": trace_id})
            rows = result.fetchall()

        events = []
        for r in rows:
            events.append({
                "event_id": r.event_id,
                "type": r.event_type,
                "session_id": r.session_id,
                "data": r.data if isinstance(r.data, dict) else {},
                "timestamp": r.created_at.isoformat() if r.created_at else None,
                "trace_id": r.trace_id,
                "caused_by": r.caused_by,
            })

        root_event = next((e for e in events if not e["caused_by"]), None)
        return JSONResponse({
            "trace_id": trace_id,
            "root_event": root_event["type"] if root_event else None,
            "depth": len(events),
            "events": events,
            "duration_ms": None,  # Could compute from first/last timestamps
        })

    async def events_recent_traces(request: Request) -> JSONResponse:
        """GET /events/recent-traces — Recent trace roots with stats."""
        limit = int(request.query_params.get("limit", "20"))
        async with database.session() as session:
            from sqlalchemy import text
            result = await session.execute(text("""
                WITH trace_stats AS (
                    SELECT trace_id,
                           COUNT(*) AS event_count,
                           BOOL_OR(data->>'modifies' IS NOT NULL) AS has_modifications
                    FROM nous_system.events
                    WHERE trace_id IS NOT NULL
                    GROUP BY trace_id
                ),
                roots AS (
                    SELECT event_id, event_type, trace_id, created_at
                    FROM nous_system.events
                    WHERE trace_id IS NOT NULL AND caused_by IS NULL
                )
                SELECT r.trace_id, r.event_type AS root_type, r.created_at,
                       ts.event_count, ts.has_modifications
                FROM roots r
                JOIN trace_stats ts ON ts.trace_id = r.trace_id
                ORDER BY r.created_at DESC
                LIMIT :lim
            """), {"lim": limit})
            rows = result.fetchall()

        traces = [{"trace_id": r.trace_id, "root_type": r.root_type,
                   "timestamp": r.created_at.isoformat() if r.created_at else None,
                   "event_count": r.event_count, "has_modifications": bool(r.has_modifications)}
                  for r in rows]
        return JSONResponse({"traces": traces})

    async def events_modifications(request: Request) -> JSONResponse:
        """GET /events/modifications — Events that modify state."""
        hours = int(request.query_params.get("hours", "24"))
        async with database.session() as session:
            from sqlalchemy import text
            result = await session.execute(text("""
                SELECT event_id, event_type, session_id, data, created_at, trace_id, caused_by
                FROM nous_system.events
                WHERE data->>'modifies' IS NOT NULL
                  AND created_at > NOW() - INTERVAL '1 hour' * :hours
                ORDER BY created_at DESC
            """), {"hours": hours})
            rows = result.fetchall()

        events = [{"event_id": r.event_id, "type": r.event_type, "session_id": r.session_id,
                   "modifies": (r.data or {}).get("modifies"),
                   "timestamp": r.created_at.isoformat() if r.created_at else None,
                   "trace_id": r.trace_id}
                  for r in rows]
        return JSONResponse({"events": events, "hours": hours, "count": len(events)})

    # ------------------------------------------------------------------
    # F035.4: Context visibility endpoints
    # ------------------------------------------------------------------

    async def context_log_list(request: Request) -> JSONResponse:
        session_id = request.query_params.get("session_id")
        limit = int(request.query_params.get("limit", "20"))
        if context_logger is None:
            return JSONResponse({"entries": []})
        entries = context_logger.get_recent(session_id=session_id, limit=limit)
        return JSONResponse({"entries": [e.to_dict() for e in entries]})

    async def context_log_detail(request: Request) -> JSONResponse:
        entry_id = request.path_params["id"]
        if context_logger is None:
            return JSONResponse({"error": "Not enabled"}, status_code=404)
        entry = context_logger.get_entry(entry_id)
        if not entry:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return JSONResponse(entry.to_dict())

    async def context_log_payload(request: Request) -> JSONResponse:
        entry_id = request.path_params["id"]
        if context_logger is None:
            return JSONResponse({"error": "Not enabled"}, status_code=404)
        payload = context_logger.get_payload(entry_id)
        if not payload:
            return JSONResponse({"error": "Not captured"}, status_code=404)
        return JSONResponse(payload)

    async def context_log_sections(request: Request) -> JSONResponse:
        entry_id = request.path_params["id"]
        if context_logger is None:
            return JSONResponse({"error": "Not enabled"}, status_code=404)
        entry = context_logger.get_entry(entry_id)
        if not entry:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return JSONResponse({
            "sections": entry.token_breakdown,
            "sections_text": entry.sections_text,
            "total_tokens_est": entry.total_tokens_est,
            "sections_present": entry.sections_present,
        })

    async def context_diff(request: Request) -> JSONResponse:
        a_id = request.query_params.get("a")
        b_id = request.query_params.get("b")
        if not context_logger or not a_id or not b_id:
            return JSONResponse({"error": "Missing parameters"}, status_code=400)
        a = context_logger.get_entry(a_id)
        b = context_logger.get_entry(b_id)
        if not a or not b:
            return JSONResponse({"error": "Not found"}, status_code=404)
        token_delta = {}
        for s in set(a.token_breakdown) | set(b.token_breakdown):
            d = b.token_breakdown.get(s, 0) - a.token_breakdown.get(s, 0)
            if d != 0:
                token_delta[s] = d
        return JSONResponse({
            "a": a_id, "b": b_id,
            "token_delta": {
                "total": b.total_tokens_est - a.total_tokens_est,
                "by_section": token_delta,
            },
            "sections_added": [s for s in b.sections_present if s not in a.sections_present],
            "sections_removed": [s for s in a.sections_present if s not in b.sections_present],
            "tools_added": [t for t in b.tool_names if t not in a.tool_names],
            "tools_removed": [t for t in a.tool_names if t not in b.tool_names],
            "messages_delta": b.messages_count - a.messages_count,
        })

    # ------------------------------------------------------------------
    # F035.3: Behavioral drift detection endpoints
    # ------------------------------------------------------------------

    async def behavior_snapshot_latest(request: Request) -> JSONResponse:
        async with database.session() as session:
            from sqlalchemy import text
            result = await session.execute(text(
                "SELECT timestamp, metrics, anomalies FROM nous_system.behavior_snapshots "
                "WHERE agent_id = :aid ORDER BY timestamp DESC LIMIT 1"
            ), {"aid": settings.agent_id})
            row = result.fetchone()
        if not row:
            return JSONResponse({"snapshot": None})
        return JSONResponse({"snapshot": {"timestamp": row.timestamp.isoformat(), "metrics": row.metrics, "anomalies": row.anomalies or []}})

    async def behavior_trends(request: Request) -> JSONResponse:
        import statistics as st
        from datetime import UTC, datetime, timedelta
        metric = request.query_params.get("metric", "fact_count_delta")
        hours = int(request.query_params.get("hours", "168"))
        async with database.session() as session:
            from sqlalchemy import text
            cutoff = datetime.now(UTC) - timedelta(hours=hours)
            result = await session.execute(text(
                "SELECT timestamp, metrics FROM nous_system.behavior_snapshots "
                "WHERE agent_id = :aid AND timestamp > :cutoff ORDER BY timestamp"
            ), {"aid": settings.agent_id, "cutoff": cutoff})
            rows = result.fetchall()
        points = []
        values = []
        for row in rows:
            metrics = row.metrics if isinstance(row.metrics, dict) else {}
            val = metrics.get(metric, 0)
            points.append({"timestamp": row.timestamp.isoformat(), "value": val})
            values.append(float(val))
        stats = {}
        if values:
            stats = {"mean": round(st.mean(values), 2), "min": min(values), "max": max(values)}
            if len(values) > 1:
                stats["stddev"] = round(st.stdev(values), 2)
        return JSONResponse({"metric": metric, "hours": hours, "points": points, "stats": stats})

    async def behavior_anomalies(request: Request) -> JSONResponse:
        from datetime import UTC, datetime, timedelta
        hours = int(request.query_params.get("hours", "168"))
        async with database.session() as session:
            from sqlalchemy import text
            cutoff = datetime.now(UTC) - timedelta(hours=hours)
            result = await session.execute(text(
                "SELECT timestamp, anomalies FROM nous_system.behavior_snapshots "
                "WHERE agent_id = :aid AND anomalies != '[]'::jsonb AND timestamp > :cutoff ORDER BY timestamp DESC"
            ), {"aid": settings.agent_id, "cutoff": cutoff})
            rows = result.fetchall()
        anomalies = []
        for row in rows:
            for a in (row.anomalies or []):
                a["timestamp"] = row.timestamp.isoformat()
                anomalies.append(a)
        return JSONResponse({"anomalies": anomalies, "hours": hours})

    async def behavior_drift_report(request: Request) -> JSONResponse:
        """GET /behavior/drift-report - Human-readable drift summary."""
        async with database.session() as session:
            from sqlalchemy import text
            result = await session.execute(text(
                "SELECT timestamp, metrics, anomalies FROM nous_system.behavior_snapshots "
                "WHERE agent_id = :aid ORDER BY timestamp DESC LIMIT 1"
            ), {"aid": settings.agent_id})
            row = result.fetchone()
        if not row:
            return JSONResponse({"report": "No snapshots yet.", "anomalies": []})
        anomalies = row.anomalies or []
        metrics = row.metrics if isinstance(row.metrics, dict) else {}
        if not anomalies:
            report = f"System behavior is within normal parameters. Last snapshot: {row.timestamp.isoformat()}"
        else:
            lines = [f"Drift detected at {row.timestamp.isoformat()}:"]
            for a in anomalies:
                lines.append(f"  - {a.get('metric', '?')}: {a.get('current', '?')} ({a.get('direction', '?')} from baseline)")
            report = "\n".join(lines)
        return JSONResponse({"report": report, "anomalies": anomalies, "snapshot_time": row.timestamp.isoformat()})

    async def _dashboard_redirect(request: Request) -> RedirectResponse:
        """Redirect bare /dashboard and /dashboard/ to the Svelte v2 app."""
        return RedirectResponse(url="/dashboard/v2/")

    async def _companion_redirect(request: Request) -> RedirectResponse:
        """Redirect /companion to the built companion entry.

        The Location header carries NO fragment, so a deep link like
        /companion#/s/{id} keeps its fragment client-side across the redirect
        (standard browser behavior) and the companion router reads it on mount.
        """
        return RedirectResponse(url="/dashboard/v2/companion.html")

    # ------------------------------------------------------------ F092 A2UI

    def _a2ui_services() -> tuple[Any, Any] | None:
        """Resolve the A2UI services or None if unavailable/disabled.

        Mirrors the heartbeat routes' probe: a lazy proxy raises RuntimeError
        before lifespan init, and tests construct create_app without the
        kwargs — both cases must 503, not crash.
        """
        try:
            # Truthiness, not `is None`: a _LazyProxy over a missing/None
            # component is falsy (main.py __bool__ override) but never None.
            if not surface_service or not action_router:
                return None
            if not settings.a2ui_enabled:
                return None
            return surface_service, action_router
        except (RuntimeError, AttributeError):
            return None

    async def a2ui_stream(request: Request) -> Response:
        """GET /a2ui/stream — SSE surface envelopes, resumable by seq."""
        resolved = _a2ui_services()
        if resolved is None:
            return JSONResponse({"error": "A2UI not available"}, status_code=503)
        service, _ = resolved
        # Last-Event-ID WINS over ?since=: a browser auto-reconnect reuses the
        # original URL (stale ?since) but sends the header from the last id.
        raw = request.headers.get("last-event-id") or request.query_params.get("since")
        try:
            since = int(raw) if raw is not None else None
        except ValueError:
            return JSONResponse({"error": "since must be an integer"}, status_code=400)
        if since is None:
            since = await service.latest_seq()

        from nous.a2ui.transport import stream_events

        async def generate():
            async for frame in stream_events(
                service, since=since, ping_interval=settings.sse_ping_interval
            ):
                yield frame

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def a2ui_surfaces_index(request: Request) -> JSONResponse:
        """GET /a2ui/surfaces — live-surface index for hydration (no nonces)."""
        resolved = _a2ui_services()
        if resolved is None:
            return JSONResponse({"error": "A2UI not available"}, status_code=503)
        service, _ = resolved
        return JSONResponse(await service.live_index())

    async def a2ui_surface_snapshot(request: Request) -> JSONResponse:
        """GET /a2ui/surfaces/{surface_id} — full createSurface snapshot."""
        resolved = _a2ui_services()
        if resolved is None:
            return JSONResponse({"error": "A2UI not available"}, status_code=503)
        service, _ = resolved
        snapshot = await service.snapshot(request.path_params["surface_id"])
        if snapshot is None:
            return JSONResponse({"error": "surface not found or not live"}, status_code=404)
        envelope, upto_seq = snapshot
        # Per-surface watermark: everything at/below this seq is already
        # reflected in the returned state (read in the same statement).
        return JSONResponse(envelope, headers={"X-A2UI-Upto-Seq": str(upto_seq)})

    async def a2ui_action(request: Request) -> JSONResponse:
        """POST /a2ui/action — renderer->agent user action."""
        resolved = _a2ui_services()
        if resolved is None:
            return JSONResponse({"error": "A2UI not available"}, status_code=503)
        _, router = resolved
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        # Forwarded identity is trusted ONLY when configured (codex P2): on a
        # directly reachable port any caller can set these headers, and a
        # forged actor in the audit is worse than 'unattributed'. The audit
        # row records who acted — never implied consent.
        actor = "unattributed"
        if settings.a2ui_trust_forwarded_identity:
            actor = (
                request.headers.get("x-forwarded-user")
                or request.headers.get("x-forwarded-email")
                or "unattributed"
            )
        status_code, payload = await router.handle(
            body, content_type=request.headers.get("content-type", ""), actor=actor
        )
        return JSONResponse(payload, status_code=status_code)

    async def a2ui_catalog(request: Request) -> JSONResponse:
        """GET /a2ui/catalog/{name} — serve a vendored catalog by short name."""
        from nous.a2ui.validator import load_catalog

        catalog = load_catalog(request.path_params["name"])
        if catalog is None:
            return JSONResponse({"error": "unknown catalog"}, status_code=404)
        return JSONResponse(catalog)

    routes = [
        Route("/chat", chat, methods=["POST"]),
        Route("/chat/stream", chat_stream, methods=["POST"]),
        Route("/chat/{session_id}", end_chat, methods=["DELETE"]),
        Route("/status", status),
        Route("/decisions", list_decisions),
        Route("/decisions/unreviewed", list_unreviewed),
        Route("/decisions/{id}/review", review_decision, methods=["POST"]),
        Route("/decisions/{id}", get_decision),
        Route("/episodes", list_episodes),
        Route("/profile/facts", list_profile_facts),
        Route("/facts/{fact_id}/core", set_fact_core, methods=["POST"]),
        Route("/facts/{fact_id}", update_fact, methods=["PUT"]),
        Route("/facts/{fact_id}", delete_fact, methods=["DELETE"]),
        Route("/facts", search_facts),
        Route("/chunks", list_chunks),
        Route("/censors/{id}", update_censor, methods=["PUT"]),
        Route("/censors", list_censors),
        Route("/procedures", list_procedures),
        Route("/frames", list_frames),
        Route("/calibration", calibration),
        Route("/identity", get_identity),
        Route("/identity/{section}", update_identity_section, methods=["PUT"]),
        Route("/reinitiate", reinitiate, methods=["POST"]),
        Route("/subtasks", list_subtasks),
        Route("/subtasks/{id}", get_subtask),
        Route("/subtasks/{id}", cancel_subtask, methods=["DELETE"]),
        Route("/schedules", list_schedules),
        Route("/schedules", create_schedule, methods=["POST"]),
        Route("/schedules/{id}", deactivate_schedule, methods=["DELETE"]),
        Route("/health", health),
        # F092: A2UI companion — detail before list (Starlette top-down matching)
        Route("/a2ui/stream", a2ui_stream),
        Route("/a2ui/surfaces/{surface_id}", a2ui_surface_snapshot),
        Route("/a2ui/surfaces", a2ui_surfaces_index),
        Route("/a2ui/action", a2ui_action, methods=["POST"]),
        Route("/a2ui/catalog/{name}", a2ui_catalog),
        # F035.1: Event bus observability
        Route("/events/stats", events_stats),
        Route("/events/recent", events_recent),
        # F035.2: Causal chain tracing
        Route("/events/trace/{trace_id}", events_trace, methods=["GET"]),
        Route("/events/recent-traces", events_recent_traces, methods=["GET"]),
        Route("/events/modifications", events_modifications, methods=["GET"]),
        # F034: Heartbeat endpoints
        Route("/heartbeat/status", heartbeat_status),
        Route("/heartbeat/trigger", heartbeat_trigger, methods=["POST"]),
        Route("/heartbeat/config", heartbeat_config, methods=["PUT"]),
        # F034.1: Finding lifecycle endpoints
        Route("/heartbeat/findings/{fingerprint}/acknowledge", heartbeat_findings_acknowledge, methods=["POST"]),
        Route("/heartbeat/findings/{fingerprint}/resolve", heartbeat_findings_resolve, methods=["POST"]),
        Route("/heartbeat/findings/{fingerprint}/dismiss", heartbeat_findings_dismiss, methods=["POST"]),
        Route("/heartbeat/findings", heartbeat_findings),
        Route("/heartbeat/escalation-policy", heartbeat_escalation_policy, methods=["PUT"]),
        # F034.3: Tuning endpoints
        Route("/heartbeat/tuning-report", heartbeat_tuning_report),
        Route("/heartbeat/tune", heartbeat_tune, methods=["POST"]),
        # F034: Check-level endpoints
        Route("/heartbeat/check/{name}/trigger", heartbeat_check_trigger, methods=["POST"]),
        Route("/heartbeat/check/{name}/reset", heartbeat_check_reset, methods=["POST"]),
        # F034.5: Dynamic check endpoints
        Route("/heartbeat/checks/dynamic/{name}/trigger", dynamic_checks_trigger, methods=["POST"]),
        Route("/heartbeat/checks/dynamic/{name}", dynamic_checks_update, methods=["PATCH"]),
        Route("/heartbeat/checks/dynamic/{name}", dynamic_checks_delete, methods=["DELETE"]),
        Route("/heartbeat/checks/dynamic", dynamic_checks_list),
        Route("/heartbeat/checks/dynamic", dynamic_checks_create, methods=["POST"]),
        Route("/sleep/trigger", trigger_sleep, methods=["POST"]),
        # Admin API endpoints (F025 prep)
        Route("/admin/search-weights", get_search_weights),
        Route("/admin/search-weights", set_search_weights, methods=["POST"]),
        # F024 Phase 3b: Rubric endpoints
        Route("/rubric/propose-dimension", propose_dimension, methods=["POST"]),
        Route("/rubric/proposals/{id}/approve", approve_proposal, methods=["POST"]),
        Route("/rubric/proposals", list_proposals),
        Route("/rubric/rollback", rollback_rubric, methods=["POST"]),
        Route("/rubric/history", get_rubric_history),
        Route("/rubric/signals", get_outcome_signals),
        Route("/rubric/evolve", trigger_evolution, methods=["POST"]),
        Route("/rubric", get_rubric),
        # Dashboard API endpoints (F021) — MUST be before static Mount
        Route("/dashboard/graph", dashboard_graph),
        Route("/dashboard/graph/node/{node_id}", dashboard_graph_node),
        Route("/dashboard/calibration", dashboard_calibration),
        Route("/dashboard/activity", dashboard_activity),
        Route("/dashboard/health", dashboard_health),
        Route("/dashboard/rubric", dashboard_rubric),
        # F021.1: Admission dashboard — rejected MUST be before admission (Starlette top-down matching)
        Route("/dashboard/admission/rejected", dashboard_admission_rejected),
        Route("/dashboard/admission", dashboard_admission),
        # F032: Execution ledger dashboard
        Route("/dashboard/ledger", dashboard_ledger),
        # F034: Heartbeat dashboard
        Route("/dashboard/heartbeat", dashboard_heartbeat),
        # F035: Observability dashboard
        Route("/dashboard/observability", dashboard_observability),
        # F091: Retrieval telemetry dashboard
        Route("/dashboard/retrieval", dashboard_retrieval),
        Route("/dashboard/retrieval/{entry_id}", dashboard_retrieval_detail),
        # F036.1: Cache dashboard
        Route("/dashboard/cache", dashboard_cache),
        # F038: DAG orchestration dashboard
        Route("/dashboard/dag", dashboard_dag),
        # F040: Graph density dashboard
        Route("/dashboard/density", dashboard_density),
        # F035.6: Consolidation audit diff dashboard — detail (path param) MUST
        # be before the list route for Starlette top-down matching.
        Route("/dashboard/consolidation/{cycle_id}", dashboard_consolidation_detail),
        Route("/dashboard/consolidation", dashboard_consolidation),
        Route("/dashboard/subtasks", dashboard_subtasks),
        # NOTE: the bare /dashboard -> /dashboard/v2/ redirect is registered
        # below, gated on the v2 build existing (so a source/pip install without
        # `npm run build` does not redirect to a 404).
        # F035.4: Context visibility
        Route("/context/log", context_log_list, methods=["GET"]),
        Route("/context/log/{id}", context_log_detail, methods=["GET"]),
        Route("/context/log/{id}/payload", context_log_payload, methods=["GET"]),
        Route("/context/log/{id}/sections", context_log_sections, methods=["GET"]),
        Route("/context/diff", context_diff, methods=["GET"]),
        # F035.3: Behavioral drift detection
        Route("/behavior/snapshot/latest", behavior_snapshot_latest, methods=["GET"]),
        Route("/behavior/trends", behavior_trends, methods=["GET"]),
        Route("/behavior/anomalies", behavior_anomalies, methods=["GET"]),
        Route("/behavior/drift-report", behavior_drift_report, methods=["GET"]),
    ]

    # Hoisted unconditionally so the v2 static mount can use it even if
    # the built directory does not yet exist at runtime.
    # Correct MIME types for assets the browser is strict about. On Windows the
    # stdlib `mimetypes` maps `.js` -> `text/plain` (registry-driven), and a
    # browser REFUSES to execute an ES module (`<script type="module">`) unless
    # it is served with a JavaScript MIME type. That silently breaks the Svelte
    # v2 SPA (blank page, no console error). Linux prod maps `.js` correctly, but
    # we force it here so serving is robust cross-platform.
    _STATIC_MIME_OVERRIDES = {
        ".js": "text/javascript; charset=utf-8",
        ".mjs": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".svg": "image/svg+xml",
        ".json": "application/json",
        ".map": "application/json",
    }

    class _NoCacheStaticFiles(StaticFiles):
        """StaticFiles wrapper that (1) forces ETag re-validation on every
        request and (2) corrects the Content-Type for module-script-critical
        asset types. Every dashboard PR was hitting stale-bundle issues
        because the default StaticFiles response has no Cache-Control
        header — browsers cache aggressively via heuristic freshness.
        ETag is already set, so re-validation is cheap (304 when
        unchanged); we just need to tell the browser to revalidate."""

        async def get_response(self, path: str, scope: Scope) -> Response:
            response = await super().get_response(path, scope)
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
            _ext = os.path.splitext(path)[1].lower()
            _mime = _STATIC_MIME_OVERRIDES.get(_ext)
            if _mime is not None:
                response.headers["Content-Type"] = _mime
            return response

    # Dashboard v2 (Svelte) — appended after the exact-match Route entries above.
    dashboard_v2_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "static", "dashboard-v2", "dist",
    )
    if os.path.isdir(dashboard_v2_dir):
        routes.append(
            Mount("/dashboard/v2", app=_NoCacheStaticFiles(directory=dashboard_v2_dir, html=True)),
        )
        # Redirect bare /dashboard and /dashboard/ to the Svelte v2 app — only
        # when the build exists (otherwise /dashboard would redirect to a 404).
        # Exact-match Routes, so they do NOT shadow /dashboard/graph etc.; both
        # variants listed explicitly to avoid auto-slash-redirect ambiguity.
        routes.append(Route("/dashboard", _dashboard_redirect))
        routes.append(Route("/dashboard/", _dashboard_redirect))
        # F092: companion entry — both slash variants, exact-match Routes
        # (same rationale as the /dashboard pair above).
        routes.append(Route("/companion", _companion_redirect))
        routes.append(Route("/companion/", _companion_redirect))

    kwargs: dict[str, Any] = {"routes": routes}
    if lifespan is not None:
        kwargs["lifespan"] = lifespan
    return Starlette(**kwargs)
