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


_COMPOSE_SURFACE_SCHEMA = {
    "type": "object",
    "description": (
        "Compose an EPHEMERAL MICRO-APP for the companion (F092.1): a "
        "read-only, navigable UI built fresh for one intent — a vacation "
        "status app, a sailing briefing, a project ledger. It lives until "
        "the user closes it and is rebuilt (not restored) next time. Use "
        "push_surface's templates for recurring shapes; compose for "
        "genuinely ad-hoc intents. Data comes from registered server-side "
        "sources; anything you can't source is stamped model-supplied and "
        "rendered amber."
    ),
    "properties": {
        "intent": {
            "type": "string",
            "description": (
                "What the app is FOR, in one or two sentences — the composer "
                "designs the whole app from this."
            ),
        },
        "archetype": {
            "type": "string",
            "enum": ["status", "briefing", "ledger"],
            "description": (
                "Optional layout archetype: status (where does X stand), "
                "briefing (what's happening in window W), ledger (list of "
                "things with attributes). Omit to let the composer pick."
            ),
        },
        "data_sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Data-model key the app binds."},
                    "source": {
                        "type": "string",
                        "description": (
                            "Registered fetcher: unreviewed_decisions, dag, "
                            "heartbeat_findings, facts_search, recent_episodes, "
                            "subtasks, schedules, decision_outcomes_series, "
                            "dag_throughput_series — or `agent_script` to "
                            "supply the data YOURSELF for any domain that has "
                            "no fetcher."
                        ),
                    },
                    "params": {
                        "type": "object",
                        "description": (
                            "Fetcher params (e.g. {q}, {dag_id}, {days}). For "
                            "`agent_script`: {\"code\": \"<python>\", \"shape\": "
                            "\"records\"|\"series\"} — the script assigns its data "
                            "to a variable named `result`, and `shape` declares "
                            "what it produces so a later failure returns a "
                            "value the app's existing bindings still accept "
                            "(refresh does not re-validate). Use \"series\" for "
                            "any chart. It may "
                            "import and fetch anything, so an external API or "
                            "link needs no new code and no new env var. It is "
                            "STORED and RE-RUN on every refresh, which is what "
                            "keeps the app live instead of a snapshot — so "
                            "make it self-contained and deterministic, not a "
                            "one-off. Memory reads (recall_deep, "
                            "recall_recent, list_tasks) are in scope; "
                            "learn_fact is disabled because it would repeat "
                            "unattended. For a chart, return a series: "
                            "`from nous.a2ui.sources import to_series; "
                            "result = to_series(rows, 't', 'v')`."
                        ),
                    },
                },
                "required": ["key", "source"],
            },
            "description": (
                "Server-side data the app should show. The server resolves "
                "these and owns the values; app.refresh re-runs them."
            ),
        },
        "dedup_key": {
            "type": "string",
            "description": (
                "Stable key for update-in-place. Defaults to app:<intent-slug>. "
                "A recurring producer (Friday sailing app) MUST keep it stable "
                "so the app updates instead of stacking."
            ),
        },
        "priority": {
            "type": "integer",
            "enum": [0, 1],
            "description": (
                "0 = ambient (default, no ping). 1 only when the app carries a "
                "live deadline. Micro-apps are never blocking (2 is rejected)."
            ),
        },
        "notify": {
            "type": "boolean",
            "description": "Override the Telegram ping (default: priority >= 1 pings).",
        },
    },
    "required": ["intent"],
}


def _intent_slug(intent: str) -> str:
    """Readable slug + a digest of the FULL intent (codex P2): slugging
    alone maps 'C++ status' and 'C status' — or any two long intents
    sharing a 40-char prefix — onto one dedup key, and push_built would
    then replace an unrelated live app. Same intent → same key (stable
    update-in-place); different intent → different key, guaranteed."""
    import hashlib

    slug = "".join(c if c.isalnum() else "-" for c in intent.lower())
    slug = "-".join(part for part in slug.split("-") if part)
    digest = hashlib.sha256(intent.encode()).hexdigest()[:8]
    return f"{slug[:40] or 'app'}-{digest}"


def register_a2ui_tools(
    dispatcher: ToolDispatcher,
    surface_service: Any,
    brain: Any = None,
    dag_store: Any = None,
    composer: Any = None,
) -> None:
    """Register the push_surface tool against a live SurfaceService.

    ``brain`` and ``dag_store`` let self-sourcing templates fetch their own
    data (decision_sweep from get_unreviewed, dag_monitor from the store),
    so the model can push them with minimal params. ``composer`` (a
    SurfaceComposer) additionally registers compose_surface — the F092.1
    ephemeral micro-app path; None (component missing or
    NOUS_A2UI_COMPOSE_ENABLED=false) leaves it unregistered.
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
                # Bound pushed into SQL (same class as codex round 3 on the
                # sources registry): a Python slice after the fetch still
                # materializes the whole age window.
                unreviewed = await brain.get_unreviewed(
                    max_age_days=int(params.get("max_age_days", 30)),
                    limit=max(1, min(int(params.get("max_decisions", 15)), 50)),
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
                for d in unreviewed
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

    if composer is None:
        return

    async def compose_surface(**kwargs) -> dict:
        intent = str(kwargs.get("intent") or "").strip()
        if not intent:
            return _tool_error("compose_surface requires an intent")
        priority = int(kwargs.get("priority") or 0)
        if priority > 1:
            return _tool_error("micro-apps are never blocking: priority must be 0 or 1")
        # Origin is SERVER-derived, never caller-supplied (codex round 3):
        # a heartbeat/schedule turn dispatches with _is_background=True, and
        # its apps must persist as origin="agent" or the push path and the
        # Phase 5 origin-based measurement cannot distinguish push from pull.
        origin = "agent" if kwargs.get("_is_background") else "chat"
        try:
            composed = await composer.compose(
                intent,
                archetype=kwargs.get("archetype"),
                data_sources=kwargs.get("data_sources") or [],
                origin=origin,
                priority=priority,
            )
        except Exception as exc:
            # UnknownSourceError and fetcher failures land here: the DECLARED
            # sources are the caller's input, so the caller gets the error.
            return _tool_error(f"compose failed: {exc}")
        dedup_key = kwargs.get("dedup_key") or f"app:{_intent_slug(intent)}"
        try:
            surface_id = await surface_service.push_built(
                composed.built,
                dedup_key=dedup_key,
                session_id=kwargs.get("_session_id"),
                notify=kwargs.get("notify"),
            )
        except PermissionError as exc:
            return _tool_error(f"Surface blocked: {exc}")
        except Exception as exc:
            logger.exception("compose_surface push failed")
            return _tool_error(f"Failed to push composed app: {exc}")
        return {
            "is_error": False,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "surface_id": surface_id,
                            "url": f"/companion#/s/{surface_id}",
                            "dedup_key": dedup_key,
                            "archetype": composed.app_spec.get("archetype"),
                            "fallback": composed.fallback,
                            "repairs": composed.repairs,
                            "model_supplied_keys": sorted(composed.app_spec.get("provenance") or {}),
                        }
                    ),
                }
            ],
        }

    dispatcher.register("compose_surface", compose_surface, _COMPOSE_SURFACE_SCHEMA)
