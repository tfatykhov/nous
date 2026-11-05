"""The memory write-health card on the companion (precision plan P4).

Built by the server from the ``memory_write_health`` check's own rule states, NEVER from a model's call: this builder
is not in ``TEMPLATES``, so ``push_surface`` cannot produce it. It lists the tripped rules with their summaries
(fixed text, numbers and source labels; no memory text) and one Acknowledge button per rule, which calls
``memory_write_health.acknowledge`` (``a2ui/actions.py``). The data model is
``{"day": "<UTC day>", "rules": {rule_id: "tripped" | "acknowledged"}}``: the handler takes the rule and the day from
the card, never from the tap. The check pushes it under ``DEDUP_KEY`` with priority 0 and ``notify=False``, so it
replaces itself in place and never pings the phone or Telegram (owner: these alerts are for the dashboard and the
companion only).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

from ..dsl import BuiltSurface, Button, Column, Divider, Row, Surface, Text, event
from .heartbeat_findings import _escape_pointer

KIND = "memory_write_health"
ORIGIN = "heartbeat"
DEDUP_KEY = "memory_write_health"
ACK_ACTION = "memory_write_health.acknowledge"
EXPIRES_IN = timedelta(days=3)
TRIPPED = "tripped"
ACKNOWLEDGED = "acknowledged"
STATUS_TEXT = {TRIPPED: "Tripped: waiting for you", ACKNOWLEDGED: "Acknowledged: its baseline starts again from here"}


def tripped_rules(states: Iterable[Any]) -> dict[str, list[str]]:
    """Each tripped rule id, in rule order, with its summaries (F1 can trip once per source label)."""
    out: dict[str, list[str]] = {}
    for s in states:
        if s.state == TRIPPED:
            out.setdefault(s.id, []).append(s.summary)
    return out


def memory_write_health_card(states: Iterable[Any], day: date) -> BuiltSurface:
    """The card for the tripped rules among ``states`` on UTC ``day``. Raises ``ValueError`` when none tripped."""
    rules = tripped_rules(states)
    if not rules:
        raise ValueError("a memory write-health card needs at least one tripped rule")
    title = f"Memory write health: {len(rules)} rule{'s' if len(rules) != 1 else ''} tripped"
    s = Surface(
        kind=KIND,
        origin=ORIGIN,
        title=title,
        priority=0,
        allowed_actions=[ACK_ACTION],
        expires_in=EXPIRES_IN,
    )
    s.data(
        {
            "day": day.isoformat(),
            "rules": dict.fromkeys(rules, TRIPPED),
            "status": dict.fromkeys(rules, STATUS_TEXT[TRIPPED]),
        }
    )
    children: list[str] = ["header", "intro"]
    components: list[dict] = [
        Text("header", f"## {title}"),
        Text(
            "intro",
            "The memory write-health check found these. The owner decides: nothing was changed. If a restore or a "
            "delete was deliberate, Acknowledge resets that rule's baseline. The dashboard's Memory write health "
            "page has the detail.",
            variant="caption",
        ),
    ]
    for i, (rule, summaries) in enumerate(rules.items()):
        base = f"r{i}"
        pointer = _escape_pointer(rule)
        children.append(base)
        components += [
            Column(base, children=[f"{base}_h", f"{base}_s", f"{base}_status", f"{base}_acts"]),
            Text(f"{base}_h", f"**{rule}**"),
            Text(f"{base}_s", "\n".join(f"- {line}" for line in summaries)),
            Text(f"{base}_status", {"path": f"/status/{pointer}"}, variant="caption"),
            Row(f"{base}_acts", children=[f"{base}_ack"]),
            Button(f"{base}_ack", child=f"{base}_ack_l", variant="primary", action=event(ACK_ACTION, {"rule": rule})),
            Text(f"{base}_ack_l", "Acknowledge"),
        ]
        if i < len(rules) - 1:
            children.append(f"{base}_div")
            components.append(Divider(f"{base}_div"))
    s.add(Column("root", children=children, align="stretch"), *components)
    return s.build()


def signature(states: Iterable[Any]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """What the card shows, for "has it changed since the last push"."""
    return tuple((rule, tuple(lines)) for rule, lines in tripped_rules(states).items())
