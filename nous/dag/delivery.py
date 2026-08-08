"""F087: DAG result delivery — get a finished DAG's outcome out to the user.

Before this module a DAG reaching a terminal status wrote ``result_summary``
to a row and stopped. No event, no notification, no memory write: a multi-hour
DAG completed in silence and the only way to learn the outcome was to poll
``dag_manage`` or open the dashboard.

Three legs carry the result out. Each is independently flagged and
independently guarded, so one failing never suppresses the others:

* **bus**      — ``dag.completed`` / ``dag.failed`` for downstream handlers.
* **summary**  — an agent-authored prose summary (costs an LLM turn, opt-in).
* **telegram** — the push the user actually sees.

Durability does NOT live here. ``EventBus.emit`` drops on ``QueueFull`` and
never blocks, and an HTTP push can fail, so the retry state lives on
``execution_dags`` and the orchestrator's sweep re-invokes this module until
a delivery succeeds or the attempt cap is reached. This module's only
contract is to report honestly which legs landed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from nous.config import Settings
from nous.events import Event
from nous.storage.models import ExecutionDAG

if TYPE_CHECKING:  # pragma: no cover - typing only
    from nous.events import EventBus

logger = logging.getLogger(__name__)

# Telegram rejects messages over 4096 chars.
_TELEGRAM_MAX_CHARS = 3900
# Per-node lines in the deterministic template. A 50-node DAG must not
# produce a 50-line push notification.
_TEMPLATE_MAX_NODE_LINES = 12
_NODE_ERROR_CHARS = 160
_SUMMARY_RESULT_CHARS = 600


@dataclass(frozen=True, slots=True)
class LegResult:
    """Outcome of one delivery leg."""

    name: str
    ok: bool
    required: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    """Aggregate result of a delivery attempt.

    ``delivered`` is true when every REQUIRED leg succeeded. Best-effort legs
    are reported but never block the DAG from being marked delivered — a
    down event bus must not wedge the user's Telegram notification, and a
    missing Telegram config must not produce an infinite retry loop.
    """

    delivered: bool
    legs: tuple[LegResult, ...]
    summary: str

    @property
    def failure_detail(self) -> str:
        """Human-readable reason the attempt failed, for delivery_error."""
        failed = [
            f"{leg.name}: {leg.detail or 'failed'}"
            for leg in self.legs
            if leg.required and not leg.ok
        ]
        return "; ".join(failed) or "no required leg succeeded"


class DAGResultDelivery:
    """Carries a terminal DAG's outcome to the bus, an LLM summary, Telegram.

    Collaborators are optional so the orchestrator stays unit-testable and so
    a deployment missing any one of them degrades to the legs it can run.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        agent_id: str,
        bus: EventBus | None = None,
        runner: Any | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._agent_id = agent_id
        self._bus = bus
        self._runner = runner
        self._http = http

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def deliver(self, dag: ExecutionDAG) -> DeliveryOutcome:
        """Run every enabled leg for a terminal DAG.

        Never raises: a leg that throws is recorded as a failed LegResult so
        the orchestrator can decide between retry and give-up from data
        rather than from an exception type.
        """
        legs: list[LegResult] = []

        summary = self.build_template(dag)
        if self._settings.dag_delivery_agent_summary_enabled:
            leg, authored = await self._leg_agent_summary(dag, summary)
            legs.append(leg)
            if authored:
                summary = authored

        if self._settings.dag_delivery_bus_enabled:
            legs.append(await self._leg_bus(dag, summary))

        if self._settings.dag_delivery_telegram_enabled:
            legs.append(await self._leg_telegram(dag, summary))

        delivered = all(leg.ok for leg in legs if leg.required)
        return DeliveryOutcome(
            delivered=delivered, legs=tuple(legs), summary=summary
        )

    def build_template(self, dag: ExecutionDAG) -> str:
        """Deterministic outcome text. Always available, never fails.

        This is what ships when the agent-summary leg is off, times out, or
        errors — delivery must not depend on an LLM being reachable.
        """
        verb = {
            "completed": "completed",
            "failed": "FAILED",
            "partial": "partially completed",
            "cancelled": "was cancelled",
        }.get(dag.status, dag.status)

        nodes = list(dag.nodes or [])
        tally: dict[str, int] = {}
        for node in nodes:
            tally[node.status] = tally.get(node.status, 0) + 1
        tally_text = ", ".join(
            f"{count} {status}" for status, count in sorted(tally.items())
        )

        lines = [
            f"DAG '{dag.name}' {verb} ({str(dag.id)[:8]})",
            f"Nodes: {len(nodes)}" + (f" — {tally_text}" if tally_text else ""),
        ]
        if dag.token_budget:
            lines.append(
                f"Tokens: {dag.tokens_consumed}/{dag.token_budget}"
            )
        elif dag.tokens_consumed:
            lines.append(f"Tokens: {dag.tokens_consumed}")
        if dag.result_summary:
            lines.append(f"Summary: {dag.result_summary}")

        # Failures are what the user needs to see, so they lead. Successful
        # nodes fill the remaining lines only if there is room.
        failed = [n for n in nodes if n.status in ("failed", "blocked", "cancelled")]
        if failed:
            lines.append("")
            lines.append("Problems:")
            for node in failed[:_TEMPLATE_MAX_NODE_LINES]:
                detail = (node.error or "").strip().replace("\n", " ")
                suffix = f" — {detail[:_NODE_ERROR_CHARS]}" if detail else ""
                lines.append(f"  [{node.status}] {node.name}{suffix}")
            if len(failed) > _TEMPLATE_MAX_NODE_LINES:
                lines.append(
                    f"  ... and {len(failed) - _TEMPLATE_MAX_NODE_LINES} more"
                )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Legs
    # ------------------------------------------------------------------

    async def _leg_bus(self, dag: ExecutionDAG, summary: str) -> LegResult:
        """Emit dag.completed / dag.failed. Best-effort by construction.

        Not required: the bus drops on QueueFull by design, so making
        delivery contingent on it would retry a notification the user
        already received.
        """
        if self._bus is None:
            return LegResult("bus", ok=True, required=False, detail="no bus wired")
        event_type = "dag.completed" if dag.status == "completed" else "dag.failed"
        try:
            await self._bus.emit(
                Event(
                    type=event_type,
                    agent_id=self._agent_id,
                    data={
                        "dag_id": str(dag.id),
                        "name": dag.name,
                        "status": dag.status,
                        "source": dag.source,
                        "result_summary": dag.result_summary,
                        "summary": summary,
                        "tokens_consumed": dag.tokens_consumed,
                        "token_budget": dag.token_budget,
                        "nodes": [
                            {
                                "name": n.name,
                                "type": n.node_type,
                                "status": n.status,
                                "error": n.error,
                            }
                            for n in (dag.nodes or [])
                        ],
                    },
                )
            )
            return LegResult("bus", ok=True, required=False)
        except Exception as exc:
            logger.warning(
                "F087: bus emit failed for DAG %s: %s", str(dag.id)[:8], exc
            )
            return LegResult("bus", ok=False, required=False, detail=str(exc))

    async def _leg_agent_summary(
        self, dag: ExecutionDAG, template: str
    ) -> tuple[LegResult, str | None]:
        """Ask the agent to write prose about the finished DAG.

        Best-effort and bounded: on timeout or error the caller keeps the
        deterministic template, so an unreachable LLM degrades the message
        rather than blocking the notification.

        Runs as a normal cognitive turn, so its episode lands in Heart on its
        own — which is how a finished DAG reaches the next conversation's
        context without a separate memory-write leg.
        """
        if self._runner is None:
            return (
                LegResult("summary", ok=True, required=False, detail="no runner"),
                None,
            )

        prompt = self._build_summary_prompt(dag, template)
        try:
            result = await asyncio.wait_for(
                self._runner.run_turn(
                    session_id=f"dag-summary-{dag.id.hex[:8]}",
                    user_message=prompt,
                    is_background=True,
                ),
                timeout=self._settings.dag_delivery_agent_summary_timeout_seconds,
            )
        except TimeoutError:
            logger.warning(
                "F087: agent summary timed out for DAG %s — using template",
                str(dag.id)[:8],
            )
            return (
                LegResult("summary", ok=False, required=False, detail="timeout"),
                None,
            )
        except Exception as exc:
            logger.warning(
                "F087: agent summary failed for DAG %s (%s) — using template",
                str(dag.id)[:8], type(exc).__name__,
            )
            return (
                LegResult("summary", ok=False, required=False, detail=str(exc)),
                None,
            )

        text = self._extract_text(result)
        if not text:
            return (
                LegResult("summary", ok=False, required=False, detail="empty"),
                None,
            )
        return LegResult("summary", ok=True, required=False), text

    async def _leg_telegram(self, dag: ExecutionDAG, summary: str) -> LegResult:
        """Push the outcome to Telegram.

        REQUIRED when a bot token and chat id are both configured — this is
        the leg the user actually sees, so a transient HTTP failure should
        bring the sweep back. Not required when Telegram is unconfigured,
        because retrying against a channel that does not exist would loop
        until the attempt cap for every DAG.
        """
        token = self._settings.telegram_bot_token
        chat_id = self._settings.telegram_chat_id
        if not token or not chat_id:
            return LegResult(
                "telegram", ok=True, required=False, detail="not configured"
            )

        text = summary[:_TELEGRAM_MAX_CHARS]
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        client = self._http or httpx.AsyncClient()
        try:
            response = await client.post(
                url, json={"chat_id": chat_id, "text": text}, timeout=10,
            )
            if response.status_code >= 400:
                detail = f"HTTP {response.status_code}"
                logger.warning(
                    "F087: Telegram push failed for DAG %s: %s",
                    str(dag.id)[:8], detail,
                )
                return LegResult("telegram", ok=False, required=True, detail=detail)
            return LegResult("telegram", ok=True, required=True)
        except Exception as exc:
            logger.warning(
                "F087: Telegram push errored for DAG %s: %s",
                str(dag.id)[:8], exc,
            )
            return LegResult("telegram", ok=False, required=True, detail=str(exc))
        finally:
            if self._http is None:
                await client.aclose()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_summary_prompt(self, dag: ExecutionDAG, template: str) -> str:
        """Prompt for the agent-authored summary.

        Node results are included but truncated — the point is a readable
        notification, not a transcript.
        """
        parts = [
            "A background execution DAG you were running has finished. "
            "Write a short summary for the user: what was accomplished, what "
            "failed and why, and what (if anything) needs their attention. "
            "Be concrete and lead with the outcome. No preamble.",
            "",
            "=== DAG outcome ===",
            template,
        ]
        results = [
            (n.name, n.result) for n in (dag.nodes or []) if n.result
        ]
        if results:
            parts.append("")
            parts.append("=== Node results ===")
            for name, result in results:
                parts.append(f"[{name}]: {result[:_SUMMARY_RESULT_CHARS]}")
        return "\n".join(parts)

    @staticmethod
    def _extract_text(result: Any) -> str:
        """Pull the assistant text out of run_turn's return value.

        ``run_turn`` returns ``(response_text, TurnContext, usage)``. A bare
        string is also accepted so tests can stub the runner without building
        a TurnContext.
        """
        if isinstance(result, str):
            return result.strip()
        if isinstance(result, tuple) and result and isinstance(result[0], str):
            return result[0].strip()
        return ""
