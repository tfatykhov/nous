"""Harness Phase 3 — approval nodes: the pure pieces (spec §3.4-§3.12).

A leaf module with no a2ui imports, so nous/a2ui can import the reserved
dedup prefix without a cycle. Card text, answer texts, refusal messages, the
stopped-at-approval predicate and the delivery lines live here so the
orchestrator, the action handler, the F087 template and dag_manage render the
same words from one definition.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

DEDUP_PREFIX = "dag-approval:"
UNATTRIBUTED = "unattributed"
DEADLINE_ACTOR = "system:deadline"
DEFER_LABEL = "Decide later"
BLOCKED_BY_APPROVAL = "Blocked: an approval was declined or not answered"
SUMMARY_MAX_CHARS = 4000
NOTIFY_QUESTION_CHARS = 200

AnswerOutcome = Literal[
    "recorded", "closed", "not_open", "dag_ended", "stray_card", "not_linked", "invalid_option"
]


def approval_dedup_key(node_id: UUID) -> str:
    return f"{DEDUP_PREFIX}{node_id}"


def node_id_from_dedup_key(key: str | None) -> UUID | None:
    if not key or not key.startswith(DEDUP_PREFIX):
        return None
    try:
        return UUID(key[len(DEDUP_PREFIX):])
    except ValueError:
        return None


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite returns stored timestamps naive; they are UTC."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def fmt_time(value: datetime | None) -> str:
    moment = as_utc(value)
    return moment.strftime("%Y-%m-%d %H:%M UTC") if moment else "an unknown time"


def option_by_id(spec: dict[str, Any] | None, option_id: str | None) -> dict[str, Any] | None:
    for option in (spec or {}).get("options", []):
        if option.get("id") == option_id:
            return option
    return None


def label_of(spec: dict[str, Any] | None, option_id: str | None) -> str:
    option = option_by_id(spec, option_id)
    return option["label"] if option else (option_id or "?")


def card_shown_chars(
    question: str, results: list[tuple[str, str]], max_chars: int = SUMMARY_MAX_CHARS
) -> list[int]:
    """How many chars of each result the card shows — the ONE truncation rule,
    shared by the card and by the acting node's label (§3.5), so a node is
    never told that text the person did not see was approved."""
    if not results:
        return []
    per_result = max(max_chars - len(question.strip()), 0) // len(results)
    shown: list[int] = []
    for name, text in results:
        room = max(per_result - len(f"From '{name}':\n"), 0)
        marker = f"\n[truncated, {len(text)} chars]"
        shown.append(len(text) if len(text) <= room else max(room - len(marker), 0))
    return shown


def build_card_summary(
    question: str, results: list[tuple[str, str]], max_chars: int = SUMMARY_MAX_CHARS
) -> str:
    """The question first and never cut; each predecessor result cut on its
    own with a visible marker, so a long draft cannot push the question off."""
    head = question.strip()
    if not results:
        return head
    blocks: list[str] = []
    for (name, text), n in zip(
        results, card_shown_chars(question, results, max_chars), strict=True
    ):
        if n < len(text):
            text = text[:n] + f"\n[truncated, {len(text)} chars]"
        blocks.append(f"From '{name}':\n" + text)
    return head + "\n\n" + "\n\n".join(blocks)


def risk_line(deadline: datetime | None, default_label: str) -> str:
    return f"If nobody answers by {fmt_time(deadline)}, '{default_label}' applies."


def button_label(label: str, outcome: str) -> str:
    return f"{label} — continues" if outcome == "proceed" else f"{label} — stops here"


def notify_text(title: str, question: str, deadline: datetime | None, default_label: str) -> str:
    """The Telegram ping body before the link (the service appends it). The
    ping is the only notice, and in prod its link is not tappable."""
    first_line = (question.strip().splitlines() or [""])[0][:NOTIFY_QUESTION_CHARS]
    return f"{title}\n{first_line}\nNo answer by {fmt_time(deadline)} → '{default_label}'."


def _by(actor: str | None) -> str:
    return "" if not actor or actor == UNATTRIBUTED else f" by {actor}"


def answer_values(
    spec: dict[str, Any],
    option_id: str,
    *,
    source: str,
    actor: str | None,
    at: datetime,
    deadline: datetime | None,
) -> dict[str, Any]:
    """Status and text columns the conditional answer write sets (§3.5)."""
    option = option_by_id(spec, option_id) or {}
    label, outcome = option.get("label", option_id), option.get("outcome")
    if source == "deadline":
        text = f"no answer by {fmt_time(deadline)}; default '{label}' ({option_id}) applied"
        if outcome == "proceed":  # unreachable in v1: the validator requires a stop default
            return {"status": "completed", "error": None, "result": text[0].upper() + text[1:]}
        return {"status": "failed", "error": text}
    when = f"at {fmt_time(at)}{_by(actor)}"
    if outcome == "proceed":
        return {
            "status": "completed",
            "error": None,
            "result": f"Answered in the companion: '{label}' ({option_id}) {when}",
        }
    return {"status": "failed", "error": f"declined in the companion: '{label}' ({option_id}) {when}"}


@dataclass(frozen=True)
class AnswerResult:
    """What an answer attempt did (§3.5). For `closed`, the label/source/time
    describe the answer that is actually recorded, not the one attempted."""

    outcome: AnswerOutcome
    node_id: UUID | None = None
    dag_id: UUID | None = None
    option_label: str | None = None
    option_outcome: str | None = None
    node_status: str | None = None
    answer_source: str | None = None
    answered_by: str | None = None
    answered_at: datetime | None = None


def refusal_message(result: AnswerResult, option_id: str) -> str:
    """The text a refused tap shows on the card (the companion shows a
    message only when the action fails)."""
    if result.outcome == "closed":
        if result.node_status == "cancelled":
            return "this DAG step was cancelled"
        if result.answer_source == "deadline":
            return (
                f"no answer by the deadline — '{result.option_label}' was applied at "
                f"{fmt_time(result.answered_at)}"
            )
        return f"already answered '{result.option_label}' at {fmt_time(result.answered_at)}"
    if result.outcome == "not_open":
        return "this question will be asked again on a new card"
    if result.outcome == "dag_ended":
        return "this DAG has already ended"
    if result.outcome == "stray_card":
        return "this card is out of date — answer the current one"
    if result.outcome == "invalid_option":
        return f"option {option_id!r} was not offered by this card"
    return "this DAG step no longer exists"


def is_answered_approval(node: Any) -> bool:
    return getattr(node, "node_type", None) == "approval" and bool(
        getattr(node, "answer_source", None)
    )


def stopped_at_approval(nodes: list[Any]) -> bool:
    """Every failed node is an answered approval — the ONE predicate behind
    the completion summary, the template verb and the blocked text (§3.12)."""
    failed = [n for n in nodes if n.status == "failed"]
    return bool(failed) and all(is_answered_approval(n) for n in failed)


def stopped_summary(nodes: list[Any]) -> str:
    stops = [n for n in nodes if n.status == "failed" and is_answered_approval(n)]
    parts = ", ".join(f"'{n.name}': '{label_of(n.approval_spec, n.answer)}'" for n in stops)
    not_run = sum(1 for n in nodes if n.status == "blocked")
    return f"Stopped at approval {parts}; {not_run} step{'s' if not_run != 1 else ''} not run"


def approval_line(node: Any) -> str:
    spec = node.approval_spec or {}
    label = label_of(spec, node.answer)
    if node.answer_source == "companion":
        outcome = (option_by_id(spec, node.answer) or {}).get("outcome")
        verdict = "approved" if outcome == "proceed" else "declined"
        return (
            f"{node.name}: {verdict} — '{label}' in the companion at "
            f"{fmt_time(node.answered_at)}"
        )
    if node.answer_source == "deadline":
        return f"{node.name}: no answer by {fmt_time(node.answer_deadline)}; default '{label}' applied"
    if node.status == "awaiting_input":
        default = label_of(spec, spec.get("default_option"))
        return (
            f"{node.name}: waiting for an answer until {fmt_time(node.answer_deadline)} "
            f"(default '{default}')"
        )
    if node.status == "cancelled":
        return f"{node.name}: not answered (cancelled)"
    return f"{node.name}: {node.status}"


def card_link(surface_id: str, base_url: str | None) -> str:
    """Same shape as SurfaceService._notify_telegram's link."""
    return f"{(base_url or '').rstrip('/')}/companion#/s/{surface_id}"


def declined_retry_refusal(node_name: str) -> str:
    return (
        f"'{node_name}' was declined in the companion; the agent cannot re-ask it. "
        "If the person wants to reconsider, push a dag_monitor card for this DAG "
        "(push_surface template='dag_monitor') — its Retry button re-asks the question."
    )


def history_entry(node: Any) -> dict[str, Any] | None:
    """The previous attempt's answer, archived by the park write (§3.4)."""
    if not node.answer_source:
        return None
    option = option_by_id(node.approval_spec, node.answer) or {}
    answered_at = as_utc(node.answered_at)
    return {
        "answer": node.answer,
        "label": option.get("label"),
        "outcome": option.get("outcome"),
        "answer_source": node.answer_source,
        "answered_by": node.answered_by,
        "answered_at": answered_at.isoformat() if answered_at else None,
    }
