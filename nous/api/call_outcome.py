"""What a tool call learned about its own effect (harness Phase 2b).

The runner creates a CallOutcome per call and passes it to
ToolDispatcher.dispatch(), which exposes it to the handler through a context
variable set and reset INSIDE dispatch -- a single task. It never spans a
generator yield (stream_chat is resumed chunk by chunk in new tasks).
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


@dataclass
class CallOutcome:
    external_ref: str | None = None  # provider id: SMTP Message-ID, Telegram message_id
    uncertain: bool = False  # the provider may have acted although the call failed
    # Phase 2.8: resolve_decision's review state before/after its write, read
    # inside the resolving transaction under a row lock (Brain.review capture=).
    review_capture: dict | None = None
    # Phase 2.8: a heartbeat_check_manage disable's prior state and the
    # state token it wrote (DynamicCheckLoader.manage_check capture=).
    check_capture: dict | None = None
    # Phase 2.8: set by the runner for a disable in an undoable context: the
    # disable is refused (nothing changed) if it would cancel an active run.
    check_refuse_if_running: bool = False
    # Phase 2.8: ``async persist(session, capture)`` writing the capture into
    # this call's compensation snapshot on the MUTATION's own session, before
    # its commit -- so the change and its revert record commit together. Set
    # by the runner once the pre-dispatch snapshot exists; the handler hands
    # it to the mutation as ``capture["persist"]``, which sets
    # ``capture["persisted"]``.
    persist_written: Any = None
    # Phase 2.8: the resolved path a write_file snapshot was captured for.
    # write_file refuses to write anywhere else, so a symlink retargeted
    # between the snapshot and the write cannot mutate an unsnapshotted file.
    write_target: str | None = None
    # Phase 2.8: the state that snapshot recorded there (a sha256 hex, or
    # builtin_tools.ABSENT); write_file refuses to replace anything else.
    write_expected: str | None = None
    # Phase 2.8: this call's builtin_tools.WriteFence, so a revert can stop a
    # write orphaned by a cancelled call from landing after it.
    write_fence: Any = None
    # Phase 2.8: the per-path lock the runner holds for this write_file.
    write_lock: Any = None
    # Phase 2.8: the asyncio task running write_file's worker thread. A
    # cancelled call returns before the thread finishes; the runner releases
    # write_lock only once this task is done.
    write_worker: Any = None


_current: ContextVar[CallOutcome | None] = ContextVar("tool_call_outcome", default=None)


def current_outcome() -> CallOutcome | None:
    """The outcome of the call being dispatched, or None outside dispatch()."""
    return _current.get()
