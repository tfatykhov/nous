"""F099 Phase 2d: the wire format of an owner action's button, and the owner's refusal vocabulary.

The server builds the callback data of a proposal's Approve and Reject buttons and the Telegram bot parses it,
so the one definition lives here, and this module imports nothing but the standard library: the bot process
loads it. A button names a proposal and an action, never a model, and the data is a closed grammar: anything
else is not a button of ours.

The refusal sentences live here for the same reason: the REST routes, the bot and (Phase 3) the A2UI cards say
the same fixed sentence for the same refusal. The keys are the store's refusal codes
(``continuation.REFUSE_*``, pinned equal by a test: the store cannot be imported here).
"""

from __future__ import annotations

import re
from uuid import UUID

CALLBACK_NAMESPACE = "f099"
KIND_PROPOSAL = "p"
ACTION_APPROVE, ACTION_REJECT = "a", "r"
CALLBACK_DATA_MAX_BYTES = 64  # Telegram's limit on a button's callback data
_CALLBACK_RE = re.compile(rf"{CALLBACK_NAMESPACE}:({KIND_PROPOSAL}):([0-9a-f]{{32}}):([ar])")

# Fixed, server-authored vocabulary: what the owner reads when a decision or an answer is refused. Never a
# model's text. `ended` also covers a question whose arrival has already moved on (its intentions were woken or
# closed): nothing is waiting for the answer.
DECISION_REFUSALS = {
    "expired": "That proposal expired before it was decided, so it did not run.",
    "ended": "That work has already ended, so nothing ran.",
    "not_pending": "That proposal was already decided the other way.",
}
# 2e: the one refusal of a cancel. A root with nothing running is not marked: a marker would only silence a
# later result.
CANCEL_REFUSALS = {
    "finished": "That work has already finished, so there was nothing to cancel.",
}
ANSWER_REFUSALS = {
    "answered": "That question was already answered.",
    "expired": "That question expired before it was answered.",
    "ended": "That work has already ended, so your answer was not recorded.",
}


def callback_data(proposal_id: UUID, action: str) -> str:
    """``f099:p:<proposal id, 32 hex>:a`` (approve) or ``:r`` (reject)."""
    if action not in (ACTION_APPROVE, ACTION_REJECT):
        raise ValueError(f"unknown owner action {action!r}")
    data = f"{CALLBACK_NAMESPACE}:{KIND_PROPOSAL}:{proposal_id.hex}:{action}"
    if len(data.encode()) > CALLBACK_DATA_MAX_BYTES:  # unreachable with a UUID: the bound is part of the contract
        raise ValueError("callback data is too long")
    return data


def parse_callback(data: object) -> tuple[str, str, str] | None:
    """``(kind, proposal id hex, action)`` of one of our buttons, or None for anything else (no partial match)."""
    if not isinstance(data, str):
        return None
    match = _CALLBACK_RE.fullmatch(data)
    return (match.group(1), match.group(2), match.group(3)) if match else None
