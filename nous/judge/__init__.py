"""F118 outside judge: a decision the agent resolved is judged later, by harness code, from its record and the tool
calls that were run (``slot``, ``instructions``, ``model_judge``, ``evidence``, ``store``, ``service``; ``jev_judge`` is
a second judge that can run beside the built-in one).

This module holds only the process-wide accessor for the running service: ``nous.api.tools`` (a resolved decision)
and ``nous.handlers.subtask_executor`` (a finished task's report) import it, and nothing else of the package, so a
build with the judge off loads no judge code. ``set_current`` is called by ``main.py`` alone.
"""

from __future__ import annotations

from typing import Any

_current: Any = None


def set_current(service: Any) -> None:
    """Install the running ``OutsideJudge`` (or None to remove it)."""
    global _current
    _current = service


def current() -> Any:
    """The running ``OutsideJudge``, or None when the judge is off or not started."""
    return _current
