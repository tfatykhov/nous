"""F092: heartbeat findings triage surface.

Findings grouped as cards with acknowledge / resolve / dismiss buttons,
delegating to the existing finding lifecycle (F034.1). Near-zero backend
work by design — thin presentation over an API that already exists.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..dsl import Button, Card, Column, Divider, Row, Surface, Text, event

# What each verb does to the F034.1 finding lifecycle, in the user's words.
# Shown once at the top of the card so the three buttons are self-explaining.
LEGEND = (
    "- **Acknowledge** — seen it, keep watching. Stays tracked in the daily "
    "digest and auto-closes once the check stops reporting it. Items Nous "
    "raised itself (check `agent:…`) have no check to clear them, so they "
    "stay until you Resolve or Dismiss.\n"
    "- **Resolve** — handled. Closes it now and tells the heartbeat this was a "
    "useful alert.\n"
    "- **Dismiss** — noise. Closes it now and counts against that check, so "
    "the heartbeat learns to raise alerts like this less often.\n"
    "\nThe card closes by itself once every item is resolved or dismissed; "
    "**Close card** hides it sooner without changing any finding."
)

OPEN_STATUS = "Status: open — waiting for you"

# Status line patched into the card after a button press (see actions.py).
STATUS_AFTER = {
    "acknowledge": "✓ Acknowledged — still tracked, will auto-close when it clears",
    "resolve": "✓ Resolved — closed as handled",
    "dismiss": "✓ Dismissed — closed as noise; counted against this check",
}

# Agent-raised findings (check_name "agent:…", registered by push_surface)
# are never auto-resolved: the heartbeat runner only clears findings whose
# check ran successfully this cycle, and no runner check owns these.
AGENT_CHECK_PREFIX = "agent:"
STATUS_AFTER_AGENT_ACK = "✓ Acknowledged — still tracked; Resolve or Dismiss it when done"


# Verbs that end a finding's life on the card. Acknowledge is NOT terminal:
# an agent-raised item stays open until Resolve/Dismiss (see LEGEND), and the
# build-time "open" value is not a verb at all.
TERMINAL_VERBS = frozenset({"resolve", "dismiss"})


def fully_triaged(findings: Any) -> bool:
    """True when a card's ``/findings`` map is non-empty and all terminal.

    An empty card ("No open findings.") is never fully triaged: nobody
    answered anything on it, so it is left to expiry or Close card.
    """
    return isinstance(findings, dict) and bool(findings) and all(isinstance(v, str) and v in TERMINAL_VERBS for v in findings.values())


def status_after(verb: str, check_name: str | None) -> str:
    """Status line for a finding after ``verb``, honest about auto-close."""
    if verb == "acknowledge" and (check_name or "").startswith(AGENT_CHECK_PREFIX):
        return STATUS_AFTER_AGENT_ACK
    return STATUS_AFTER.get(verb, f"{verb}d")


def heartbeat_findings(params: dict[str, Any]) -> Any:
    findings = params.get("findings", [])
    title = params.get("title") or f"Heartbeat findings ({len(findings)})"

    s = Surface(
        kind="heartbeat_findings",
        origin="heartbeat",
        title=title,
        priority=int(params.get("priority", 1)),
        allowed_actions=[
            "heartbeat.acknowledge",
            "heartbeat.resolve",
            "heartbeat.dismiss",
            "heartbeat.close_card",
        ],
        expires_in=timedelta(hours=float(params.get("expires_hours", 72))),
    )
    s.data(
        {
            # Every finding starts open, whatever the caller passed (codex
            # P1): a caller-supplied resolve/dismiss would let the sweep
            # auto-close a card nobody saw. Only the action handler, after a
            # recorded user press, writes a terminal verb here.
            "findings": {f["fingerprint"]: "open" for f in findings},
            # Human-readable per-finding status the action handler patches,
            # so a button press visibly changes the card.
            "status": {f["fingerprint"]: OPEN_STATUS for f in findings},
        }
    )

    children: list[str] = ["header"]
    components: list[dict] = [Text("header", f"## {title}")]
    if findings:
        children.append("legend")
        components.append(Text("legend", LEGEND, variant="caption"))
    for i, finding in enumerate(findings):
        fp = finding["fingerprint"]
        card_id = f"f{i}"
        children.append(card_id)
        row = [
            Card(card_id, child=f"f{i}_col"),
            Column(
                f"f{i}_col",
                children=[f"f{i}_msg", f"f{i}_meta", f"f{i}_status", f"f{i}_acts"],
            ),
            Text(f"f{i}_msg", finding.get("message", "")),
            Text(
                f"f{i}_meta",
                f"{finding.get('urgency', 'normal')} · {finding.get('check', '')} · {fp[:12]}",
                variant="caption",
            ),
            Text(f"f{i}_status", {"path": f"/status/{_escape_pointer(fp)}"}),
            Row(f"f{i}_acts", children=[f"f{i}_ack", f"f{i}_res", f"f{i}_dis"]),
            Button(
                f"f{i}_ack",
                child=f"f{i}_ack_l",
                action=event("heartbeat.acknowledge", {"fingerprint": fp}),
            ),
            Text(f"f{i}_ack_l", "Acknowledge"),
            Button(
                f"f{i}_res",
                child=f"f{i}_res_l",
                variant="primary",
                action=event("heartbeat.resolve", {"fingerprint": fp}),
            ),
            Text(f"f{i}_res_l", "Resolve"),
            Button(
                f"f{i}_dis",
                child=f"f{i}_dis_l",
                variant="borderless",
                action=event("heartbeat.dismiss", {"fingerprint": fp}),
            ),
            Text(f"f{i}_dis_l", "Dismiss"),
        ]
        components.extend(row)
        if i < len(findings) - 1:
            div = f"f{i}_div"
            children.append(div)
            components.append(Divider(div))

    if not findings:
        children.append("empty")
        components.append(Text("empty", "No open findings."))

    # Hides the card without touching any finding (actions.py).
    children.append("close")
    components.extend(
        [
            Button(
                "close",
                child="close_l",
                variant="borderless",
                action=event("heartbeat.close_card", {}),
            ),
            Text("close_l", "Close card"),
        ]
    )

    s.add(Column("root", children=children, align="stretch"), *components)
    return s.build()


def _escape_pointer(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")
