"""F092: template builders — one per surface kind.

Every builder returns a validated ``BuiltSurface``; a builder that emits
invalid A2UI raises in its unit test, not in production. Registered in
``TEMPLATES`` for the ``push_surface`` tool.
"""

from .action_review import action_review
from .approval import approval_gate
from .dag_monitor import dag_monitor
from .decision_sweep import decision_sweep
from .heartbeat_findings import heartbeat_findings
from .memory_graph import memory_graph

TEMPLATES = {
    "approval_gate": approval_gate,
    "action_review": action_review,
    "heartbeat_findings": heartbeat_findings,
    "decision_sweep": decision_sweep,
    "memory_graph": memory_graph,
    "dag_monitor": dag_monitor,
}

__all__ = [
    "TEMPLATES",
    "approval_gate",
    "action_review",
    "heartbeat_findings",
    "decision_sweep",
    "memory_graph",
    "dag_monitor",
]
