"""F092: push_surface agent tool.

Lives here (not nous/api/tools.py) deliberately: the AST ratchet in
tests/test_tool_arg_salvage.py counts unflagged MCP-shaped returns in that
file with an exact `==` baseline, and a handler module outside it keeps the
ratchet untouched. The only nous/api/tools.py edit for F092 is the
``_session_id`` injection branch in ``dispatch()``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from nous.api.tools import ToolDispatcher, _tool_error

from .builders import TEMPLATES
from .dsl import SurfaceValidationError

logger = logging.getLogger(__name__)

_PUSH_SURFACE_SCHEMA = {
    "type": "object",
    "description": (
        "Push a structured, interactive surface to the companion app "
        "(A2UI). Use for things that are bad as prose and good as an "
        "interface: escalations needing a decision, reviews of actions you "
        "took, batch triage. The user sees it at /companion and their "
        "button presses come back as recorded actions."
    ),
    "properties": {
        "template": {
            "type": "string",
            "enum": sorted(TEMPLATES),
            "description": (
                "approval_gate: pre-execution escalation with options "
                "(params: title, summary, risk, options=[{id,label}], "
                "recommendation, trace_id). v1 RECORDS the user's choice as "
                "a durable audited action and resolves the card — it does "
                "NOT resume a blocked operation for you; hold the operation "
                "yourself and check the recorded choice (executor callbacks "
                "ship with the escalation integration). "
                "action_review: post-hoc review of an action already taken "
                "(params: title, did, why, cost, compensation={revertible,"
                "handler,note}, trace_id). "
                "heartbeat_findings: triage list (params: findings=[{"
                "fingerprint,message,urgency,check}], title). "
                "decision_sweep: unreviewed-decision review cards, ALWAYS "
                "self-sourced from the brain — decision rows in params are "
                "ignored (optional: max_age_days, max_decisions, title). "
                "memory_graph: interactive graph explorer seeded on one "
                "node (params: node_id UUID, node_type, label; tap-to-"
                "expand happens client-side). "
                "dag_monitor: DAG node/status graph with retry+cancel; "
                "params={dag_id} — nodes/edges/name/status are ALWAYS "
                "fetched from the store, supplied values are ignored."
            ),
        },
        "params": {
            "type": "object",
            "description": "Template parameters (see template description).",
        },
        "dedup_key": {
            "type": "string",
            "description": (
                "Stable key for update-in-place: a recurring producer (e.g. "
                "an hourly check) MUST pass one so it updates its existing "
                "card instead of stacking new ones."
            ),
        },
        "notify": {
            "type": "boolean",
            "description": ("Override the Telegram ping (default: priority >= 1 pings)."),
        },
    },
    "required": ["template", "params"],
}


def register_a2ui_tools(
    dispatcher: ToolDispatcher,
    surface_service: Any,
    brain: Any = None,
    dag_store: Any = None,
) -> None:
    """Register the push_surface tool against a live SurfaceService.

    ``brain`` and ``dag_store`` let self-sourcing templates fetch their own
    data (decision_sweep from get_unreviewed, dag_monitor from the store),
    so the model can push them with minimal params.
    """

    async def push_surface(**kwargs) -> dict:
        template = kwargs.get("template", "")
        builder = TEMPLATES.get(template)
        if builder is None:
            return _tool_error(f"Unknown template {template!r}. Available: {sorted(TEMPLATES)}")
        params = dict(kwargs.get("params") or {})
        dedup_key = kwargs.get("dedup_key")
        if template == "heartbeat_findings" and not dedup_key:
            # Recurring producers MUST dedup (codex P2): a scheduled check
            # calling without a key would stack a new card every run —
            # exactly the spam dedup exists to prevent. Derive the stable
            # default rather than bouncing the model.
            dedup_key = "heartbeat:findings"

        # Self-sourcing templates — ALWAYS, not as a fallback (codex P1 x2):
        # the DB is the only source of truth for actionable rows. A caller-
        # supplied list was previously honored when present, which let the
        # model render fabricated text over a REAL decision/DAG id — the user
        # reads the fabrication, but their click records against the id.
        # Caller-supplied rows are therefore overwritten unconditionally;
        # callers control filters and display options only.
        if template == "decision_sweep":
            if brain is None:
                return _tool_error("decision_sweep needs the brain wired to self-source")
            try:
                unreviewed = await brain.get_unreviewed(
                    max_age_days=int(params.get("max_age_days", 30))
                )
            except Exception as exc:
                return _tool_error(f"Could not fetch unreviewed decisions: {exc}")
            params["decisions"] = [
                {
                    "id": str(d.id),
                    "description": d.description,
                    "confidence": d.confidence,
                    "stakes": d.stakes,
                    "category": d.category,
                }
                for d in unreviewed[: int(params.get("max_decisions", 15))]
            ]
            dedup_key = dedup_key or "sweep:decisions"
        if template == "dag_monitor":
            if dag_store is None:
                return _tool_error("dag_monitor needs the dag_store wired to self-source")
            from uuid import UUID as _UUID

            try:
                dag = await dag_store.get_dag(_UUID(str(params.get("dag_id", ""))))
            except ValueError:
                return _tool_error("dag_monitor params require a dag_id UUID")
            except Exception as exc:
                return _tool_error(f"Could not fetch DAG: {exc}")
            if dag is None:
                return _tool_error("DAG not found")
            # Distinct (from, to) name pairs only: DAGEdge uniqueness includes
            # edge_type, so a stored DAG can carry parallel edges that project
            # to the same pair — and the renderer keys edges by that pair.
            name_by_id = {n.id: n.name for n in dag.nodes}
            seen_pairs: set[tuple[str, str]] = set()
            edges = []
            for e in dag.edges:
                pair = (name_by_id.get(e.from_node_id, ""), name_by_id.get(e.to_node_id, ""))
                if not pair[0] or not pair[1] or pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                edges.append({"from": pair[0], "to": pair[1]})
            params.update(
                {
                    "dag_id": str(dag.id),
                    "name": dag.name,
                    "status": dag.status,
                    "nodes": [
                        {"name": n.name, "status": n.status, "node_type": n.node_type}
                        for n in dag.nodes
                    ],
                    "edges": edges,
                }
            )
            dedup_key = dedup_key or f"dag:{dag.id}"
        try:
            built = builder(params)
        except SurfaceValidationError as exc:
            return _tool_error(f"Surface failed validation: {exc.errors[:2]}")
        except (KeyError, ValueError, TypeError) as exc:
            return _tool_error(f"Bad params for {template}: {exc}")
        try:
            surface_id = await surface_service.push_built(
                built,
                dedup_key=dedup_key,
                session_id=kwargs.get("_session_id"),
                notify=kwargs.get("notify"),
            )
        except PermissionError as exc:
            return _tool_error(f"Surface blocked: {exc}")
        except Exception as exc:
            logger.exception("push_surface failed")
            return _tool_error(f"Failed to push surface: {exc}")
        return {
            "is_error": False,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "surface_id": surface_id,
                            "url": f"/companion#/s/{surface_id}",
                            "priority": built.priority,
                        }
                    ),
                }
            ],
        }

    dispatcher.register("push_surface", push_surface, _PUSH_SURFACE_SCHEMA)
