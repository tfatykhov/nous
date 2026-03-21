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
  GET  /dashboard         - Static dashboard SPA (F021)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from uuid import UUID, uuid4

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from nous.api.runner import AgentRunner
from nous.brain import Brain
from nous.cognitive import CognitiveLayer
from nous.config import Settings
from nous.events import Event, EventBus
from nous.heart import Heart
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
) -> Starlette:
    """Create the Starlette ASGI app with all routes."""

    async def chat(request: Request) -> JSONResponse:
        """POST /chat - Send a message, get a response."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        message = body.get("message")
        if not message:
            return JSONResponse({"error": "Missing required field: message"}, status_code=400)

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
                }
            return JSONResponse(result)
        except Exception as e:
            logger.error("Chat error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def chat_stream(request: Request) -> StreamingResponse:
        """POST /chat/stream - SSE streaming chat."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        message = body.get("message")
        if not message:
            return JSONResponse({"error": "Missing required field: message"}, status_code=400)

        session_id = body.get("session_id") or str(uuid4())
        platform = body.get("platform")
        # 007.4: Extract optional user identity
        user_id = body.get("user_id")
        user_display_name = body.get("user_display_name")

        async def event_generator():
            try:
                async for event in runner.stream_chat(
                    session_id, message, platform=platform,
                    user_id=user_id, user_display_name=user_display_name,
                ):
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
                    },
                    "working_memory": working_memory_sessions,
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

        if not outcome:
            return JSONResponse({"error": "outcome is required"}, status_code=400)

        try:
            detail = await brain.review(
                UUID(decision_id),
                outcome=outcome,
                result=result_text,
                reviewer=reviewer,
            )
            return JSONResponse(detail.model_dump(mode="json"))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=404)

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
        Route("/facts", search_facts),
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
        Route("/sleep/trigger", trigger_sleep, methods=["POST"]),
        # Admin API endpoints (F025 prep)
        Route("/admin/search-weights", get_search_weights),
        Route("/admin/search-weights", set_search_weights, methods=["POST"]),
        # Dashboard API endpoints (F021) — MUST be before static Mount
        Route("/dashboard/graph", dashboard_graph),
        Route("/dashboard/calibration", dashboard_calibration),
        Route("/dashboard/activity", dashboard_activity),
        Route("/dashboard/health", dashboard_health),
        # F021.1: Admission dashboard — rejected MUST be before admission (Starlette top-down matching)
        Route("/dashboard/admission/rejected", dashboard_admission_rejected),
        Route("/dashboard/admission", dashboard_admission),
    ]

    # Static dashboard mount — only add if directory exists (avoids crash during tests)
    # MUST be LAST in routes list (catch-all for /dashboard/*)
    dashboard_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "static", "dashboard",
    )
    if os.path.isdir(dashboard_dir):
        routes.append(
            Mount("/dashboard", app=StaticFiles(directory=dashboard_dir, html=True)),
        )

    kwargs: dict[str, Any] = {"routes": routes}
    if lifespan is not None:
        kwargs["lifespan"] = lifespan
    return Starlette(**kwargs)
