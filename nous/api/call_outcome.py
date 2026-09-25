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


_current: ContextVar[CallOutcome | None] = ContextVar("tool_call_outcome", default=None)


def current_outcome() -> CallOutcome | None:
    """The outcome of the call being dispatched, or None outside dispatch()."""
    return _current.get()
