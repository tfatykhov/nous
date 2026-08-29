"""F092.1 §4.1: the micro-app grammar — machine-checked, not documented.

Fresh composition without constraints produces a different-looking app every
Friday, which is unusable as a habit. Consistency comes from an enforced
grammar, not from a recipe cache (Q8): every micro-app surface must satisfy
these rules IN ADDITION to schema + structural validation, and a violation
is a compose-repair input or a push rejection — never a rendered surface.

Rules (each yields its own error string, so the repair loop can fix
precisely what broke):

- Fixed skeleton: root is a Column whose children are, in order,
  one AppHeader, zero-or-one StatRow, one to five Section, one AppFooter.
- Depth <= 5.
- Component budget <= 40 nodes.
- Catalog subset: nous-core + basic DISPLAY components only. Input
  components are banned — free text and forms live in the Conversation
  surface, and this lint is what mechanically enforces Q7 (read-only).
- Every Section carries a non-empty title.
- Freshness stamp mandatory: AppHeader.composedAt present (as a data-model
  binding, so app.refresh can update it without touching components).
- No duplicate refs within one children array, and no component referenced
  by two different parents — the renderer's keyed each treats a repeated
  ref as a duplicate key, which is a Svelte crash that takes down the
  whole surface (rev-ui #4; the composer is an LLM).
"""

from __future__ import annotations

from typing import Any

MAX_DEPTH = 5
MAX_COMPONENTS = 40
MAX_SECTIONS = 5

# Q7's lint rule: these exist for the Conversation and form surfaces, never
# inside a micro-app. A refine button is not a free-form prompt in disguise.
BANNED_COMPONENTS = frozenset(
    {"TextField", "Slider", "DateTimeInput", "CheckBox", "ChoicePicker"}
)

# nous-core + the basic DISPLAY subset the companion renders (Video and
# AudioPlayer are deliberately unimplemented in the renderer).
ALLOWED_COMPONENTS = frozenset(
    {
        # basic display
        "Text",
        "Image",
        "Icon",
        "Row",
        "Column",
        "List",
        "Card",
        "Tabs",
        "Modal",
        "Divider",
        "Button",
        # nous-core
        "ApprovalPanel",
        "ActionReviewCard",
        "StatTile",
        "KeyValueTable",
        "DecisionCard",
        "ConfidenceMeter",
        "MemoryGraph",
        "DagGraph",
        # F092.1 Phase 3
        "AppHeader",
        "AppFooter",
        "Section",
        "StatRow",
        "Timeline",
    }
)

_CHILD_KEYS = ("child", "children", "trigger", "content")


def _children_of(component: dict) -> list[str]:
    ids: list[str] = []
    for key in _CHILD_KEYS:
        value = component.get(key)
        if isinstance(value, str):
            ids.append(value)
        elif isinstance(value, list):
            ids.extend(v for v in value if isinstance(v, str))
    for tab in component.get("tabs") or []:
        if isinstance(tab, dict) and isinstance(tab.get("child"), str):
            ids.append(tab["child"])
    return ids


def lint_micro_app(components: list[dict[str, Any]]) -> list[str]:
    """Return every grammar violation (empty list = conforming).

    Operates on the raw component array (the shape BuiltSurface carries and
    the composer emits) so both compose output and refine recomposition run
    through the identical check.
    """
    errors: list[str] = []
    by_id = {c.get("id"): c for c in components if isinstance(c, dict)}

    if len(components) > MAX_COMPONENTS:
        errors.append(
            f"component budget exceeded: {len(components)} > {MAX_COMPONENTS} — "
            "summarize and offer an app.refine drill-down instead"
        )

    for comp in components:
        ctype = comp.get("component", "")
        if ctype in BANNED_COMPONENTS:
            errors.append(
                f"input component {ctype!r} ({comp.get('id')}) is banned in "
                "micro-apps — free text and forms belong to the Conversation surface"
            )
        elif ctype not in ALLOWED_COMPONENTS:
            errors.append(
                f"component {ctype!r} ({comp.get('id')}) is outside the "
                "micro-app catalog subset"
            )
        if ctype == "Section":
            title = comp.get("title")
            if not isinstance(title, str) or not title.strip():
                errors.append(f"Section {comp.get('id')!r} has no title — no anonymous blocks")
        if ctype == "AppHeader" and not comp.get("composedAt"):
            errors.append("AppHeader is missing composedAt — the freshness stamp is mandatory")
        if ctype == "StatRow":
            # Type-check the children (codex round 3): the catalog schema
            # only bounds children to <=4 strings, so a Text or Card ref
            # would otherwise render arbitrary content in the summary grid.
            for kid in comp.get("children") or []:
                kid_type = (by_id.get(kid) or {}).get("component")
                if kid_type is not None and kid_type != "StatTile":
                    errors.append(
                        f"StatRow {comp.get('id')!r} child {kid!r} is a "
                        f"{kid_type} — StatRow holds StatTiles only"
                    )

    # --- duplicate refs (rev-ui #4) ----------------------------------------
    # A repeated id inside one children array, or one component referenced by
    # two parents, becomes a duplicate key in the renderer's keyed each — a
    # crash, not a glitch. The structural validator catches duplicate ids and
    # dangling refs but not repeated refs.
    referenced_by: dict[str, str] = {}
    for comp in components:
        cid = str(comp.get("id"))
        kids = _children_of(comp)
        dupes = {k for k in kids if kids.count(k) > 1}
        if dupes:
            errors.append(f"duplicate child refs in {cid!r}: {sorted(dupes)}")
        for kid in set(kids):
            if kid in referenced_by and referenced_by[kid] != cid:
                errors.append(
                    f"component {kid!r} is referenced by both "
                    f"{referenced_by[kid]!r} and {cid!r} — one parent per component"
                )
            referenced_by.setdefault(kid, cid)

    # --- fixed skeleton -----------------------------------------------------
    root = by_id.get("root")
    if root is None or root.get("component") != "Column":
        errors.append("root must be a Column")
    else:
        order = [
            by_id[cid].get("component")
            for cid in root.get("children") or []
            if cid in by_id
        ]
        skeleton_error = _skeleton_error(order)
        if skeleton_error:
            errors.append(skeleton_error)

    # --- depth --------------------------------------------------------------
    if root is not None:
        depth = _max_depth(by_id, "root")
        if depth > MAX_DEPTH:
            errors.append(f"nesting depth {depth} exceeds {MAX_DEPTH}")

    return errors


def _skeleton_error(order: list[str | None]) -> str | None:
    """AppHeader · StatRow? · Section{1,5} · AppFooter — anything else fails."""
    want = "AppHeader, optional StatRow, 1-5 Section, AppFooter"
    i = 0
    if i >= len(order) or order[i] != "AppHeader":
        return f"skeleton: first top-level child must be AppHeader (want {want})"
    i += 1
    if i < len(order) and order[i] == "StatRow":
        i += 1
    sections = 0
    while i < len(order) and order[i] == "Section":
        sections += 1
        i += 1
    if sections < 1 or sections > MAX_SECTIONS:
        return f"skeleton: need 1-{MAX_SECTIONS} Section blocks, got {sections} (want {want})"
    if i >= len(order) or order[i] != "AppFooter":
        return f"skeleton: last top-level child must be AppFooter (want {want})"
    if i + 1 != len(order):
        return f"skeleton: unexpected components after AppFooter (want {want})"
    return None


def _max_depth(by_id: dict, root_id: str) -> int:
    """Iterative DFS with a visited set — a cycle must not hang the linter
    (the structural validator rejects cycles separately)."""
    best = 0
    stack: list[tuple[str, int]] = [(root_id, 1)]
    seen: set[str] = set()
    while stack:
        cid, depth = stack.pop()
        if cid in seen or cid not in by_id:
            continue
        seen.add(cid)
        best = max(best, depth)
        if depth > MAX_DEPTH:
            # Deep enough to fail — no need to walk further down this arm.
            continue
        for child in _children_of(by_id[cid]):
            stack.append((child, depth + 1))
    return best
