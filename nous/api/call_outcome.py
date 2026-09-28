"""What a tool call learned about its own effect (harness Phase 2b).

The runner creates a CallOutcome per call and passes it to
ToolDispatcher.dispatch(), which exposes it to the handler through a context
variable set and reset INSIDE dispatch -- a single task. It never spans a
generator yield (stream_chat is resumed chunk by chunk in new tasks).
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class CallOutcome:
    external_ref: str | None = None   # provider id: SMTP Message-ID, Telegram message_id
    uncertain: bool = False           # the provider may have acted although the call failed
    # Phase 2.8: resolve_decision's review state before/after its write, read
    # inside the resolving transaction under a row lock (Brain.review capture=).
    review_capture: dict | None = None
    # Phase 2.8: a heartbeat_check_manage disable's prior state and the
    # state token it wrote (DynamicCheckLoader.manage_check capture=).
    check_capture: dict | None = None


_current: ContextVar[CallOutcome | None] = ContextVar("tool_call_outcome", default=None)


def current_outcome() -> CallOutcome | None:
    """The outcome of the call being dispatched, or None outside dispatch()."""
    return _current.get()
