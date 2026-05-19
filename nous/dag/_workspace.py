"""F064.3 — DAG workspace safety helpers.

Three pure utilities that backstop the file-system surface every DAG node
touches: path sanitization, deterministic workspace path computation, and
runtime containment assertions.

Design:
- `sanitize_segment` is the STRICT, insert-time gate. It raises ValueError
  for any path segment that doesn't match Symphony §9.1's safe regex
  ``[A-Za-z0-9._-]+`` (plus rejecting empty, ``.``, and ``..``). Called
  from ``DAGNodeSpec.model_validator`` when
  ``NOUS_DAG_WORKSPACE_SAFETY_ENABLED=true`` so callers can't ship a node
  with an unsafe ``name``.
- `compute_workspace_path` is the LENIENT, read-time helper. It applies
  the same regex as a TRANSFORMATION (substitute unsafe chars with ``_``)
  rather than rejecting outright, so pre-flag rows with names like
  ``"step with spaces"`` continue to resolve to a deterministic safe
  path (``"step_with_spaces"``) instead of breaking on read.
- `assert_inside_root` runs UNCONDITIONALLY at every read site. Path
  traversal is a security boundary, not a feature flag — even pre-flag
  rows must not escape the workspace root via symlink or naive ``..``.

Plan §6.2 + the post-review revision on the read-time transformation
posture motivate this two-tier design.
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

# Symphony §9.1: "Derive from issue.identifier by replacing any character
# not in [A-Za-z0-9._-] with _."
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]")

# Reserved special names: "." and ".." are valid w.r.t. the regex but are
# path-traversal primitives. Empty strings collapse to the root.
_RESERVED = frozenset({"", ".", ".."})


def sanitize_segment(s: str) -> str:
    """Strict insert-time validator. Returns the input unchanged when safe;
    raises ValueError otherwise.

    Reject (not rewrite) at insert time. Insert-time rejection is loud,
    surfaces the bad input to the caller immediately, and avoids two
    nodes with names that collide after sanitization producing the same
    workspace path (silent corruption).

    Args:
        s: A single path segment (NOT a multi-segment path; no '/').

    Returns:
        The unchanged segment when safe.

    Raises:
        ValueError: When the segment contains characters outside
            [A-Za-z0-9._-], or is a reserved name (empty, ".", or "..").
    """
    if s in _RESERVED:
        raise ValueError(f"invalid path segment: {s!r}")
    if _UNSAFE_RE.search(s):
        # Identify the offending chars to make the error helpful.
        sanitized = _UNSAFE_RE.sub("_", s)
        raise ValueError(
            f"path segment contains characters outside [A-Za-z0-9._-]: "
            f"{s!r} (would sanitize to {sanitized!r}). Reject at insert time."
        )
    return s


def _sanitize_segment_lenient(s: str) -> str:
    """Read-time transformation. Rewrites unsafe chars to ``_`` rather than
    raising. Pre-flag rows with legacy names use this path so reads of
    historical data keep working when the safety flag is flipped on.

    Reserved names still raise — those aren't transformable to a safe form
    without ambiguity (which of two nodes named "." owns the result file?).
    """
    if s in _RESERVED:
        raise ValueError(f"invalid path segment: {s!r}")
    return _UNSAFE_RE.sub("_", s)


def compute_workspace_path(dag_id: UUID, node_name: str, root: Path) -> Path:
    """Resolve the workspace path for a (dag_id, node_name) pair.

    Uses ``dag_id.hex[:8]`` (hex-only, sanitization-safe) for the DAG
    segment to match orchestrator.py:524's pre-F064.3 convention. The
    ``node_name`` segment passes through the LENIENT transformation, not
    the strict gate — read-time of legacy data must not break.

    Args:
        dag_id: The owning DAG's UUID.
        node_name: The node name as stored on the row. May be a legacy
            unsafe name from before NOUS_DAG_WORKSPACE_SAFETY_ENABLED was
            flipped on; gets transformed to a safe equivalent.
        root: The resolved workspace root (typically
            ``settings.dag_workspace_root``).

    Returns:
        An absolute Path under ``root``. Callers should still call
        ``assert_inside_root`` on the resolved result before using it —
        ``compute_workspace_path`` returns the candidate, the assertion
        enforces the invariant.
    """
    dag_segment = dag_id.hex[:8]
    safe_name = _sanitize_segment_lenient(node_name)
    return root / dag_segment / safe_name


def assert_inside_root(path: Path, root: Path) -> None:
    """Raise ValueError if ``path.resolve()`` is not inside ``root.resolve()``.

    Both arguments are resolved (following symlinks) so a symlink-based
    escape (``workspace_root/foo`` → ``/etc/passwd``) is caught even if
    the textual path looks safe.

    Runs UNCONDITIONALLY at read sites — security boundary, not a feature.
    Cheap (one syscall per resolve), so the cost is negligible in the
    per-tick poll path.
    """
    resolved_path = path.resolve()
    resolved_root = root.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as e:
        raise ValueError(
            f"workspace path {resolved_path} escapes root {resolved_root}"
        ) from e
