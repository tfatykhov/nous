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
        # F094 chart primitives
        "Sparkline",
        "LineChart",
        "BarChart",
    }
)

# F093 §5.1 / F094 §5 — components that are meaningless without their data:
# a chart with no `path` cannot render. A missing/blank `path` is a hard
# error (unlike a Text with no binding, which is merely lazy).
BINDING_MANDATORY = frozenset({"Sparkline", "LineChart", "BarChart"})

_CHILD_KEYS = ("child", "children", "trigger", "content")


def _children_of(component: dict) -> list[str]:
    ids: list[str] = []
    for key in _CHILD_KEYS:
        value = component.get(key)
        if isinstance(value, str):
            ids.append(value)
        elif isinstance(value, list):
            ids.extend(v for v in value if isinstance(v, str))
        elif isinstance(value, dict) and isinstance(value.get("componentId"), str):
            # F093 §6.2 Repeat: a `{componentId, path}` template child. The
            # renderer (Children.svelte) already expands the referenced
            # component over the bound array with a per-item scope; the
            # template child was INVISIBLE here (the "unreachable prior
            # art"), so the reference check and depth walk never saw it.
            ids.append(value["componentId"])
    for tab in component.get("tabs") or []:
        if isinstance(tab, dict) and isinstance(tab.get("child"), str):
            ids.append(tab["child"])
    return ids


def _template_children(component: dict) -> list[dict]:
    """The `{componentId, path}` template children of a component (F093 §6.2)."""
    out = []
    for key in _CHILD_KEYS:
        value = component.get(key)
        if isinstance(value, dict) and isinstance(value.get("componentId"), str):
            out.append(value)
    return out


# F093 §6.3 — list-heavy archetypes get a larger budget. MAX_DEPTH stays
# 5 for all (depth is a complexity smell, not an expressiveness need).
_ROOMY_ARCHETYPES = frozenset({"ledger", "briefing"})


def caps_for(archetype: str | None) -> tuple[int, int]:
    """(max_components, max_sections) for an archetype. Default 40/5;
    ledger/briefing get 80/8 (a 16-day itinerary or a metrics dashboard
    does not fit 40/5, and Repeat keeps the component count low anyway)."""
    # `in` would hash the value — a non-string archetype (a model emitting an
    # object/array) raises TypeError, which escapes _validate's repair loop and
    # the guaranteed fallback (codex P2). A non-string is simply not roomy.
    if isinstance(archetype, str) and archetype in _ROOMY_ARCHETYPES:
        return 80, 8
    return MAX_COMPONENTS, MAX_SECTIONS


def lint_micro_app(
    components: list[dict[str, Any]], *, archetype: str | None = None
) -> list[str]:
    """Return every grammar violation (empty list = conforming).

    Operates on the raw component array (the shape BuiltSurface carries and
    the composer emits) so both compose output and refine recomposition run
    through the identical check. ``archetype`` selects the component/section
    caps (F093 §6.3).
    """
    errors: list[str] = []
    by_id = {c.get("id"): c for c in components if isinstance(c, dict)}
    max_components, max_sections = caps_for(archetype)

    if len(components) > max_components:
        errors.append(
            f"component budget exceeded: {len(components)} > {max_components} — "
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
        # Issue #620 gap 2 — the grammar is REFERENCE-based: a `children`
        # array holds component ids. When the model emits inline child
        # OBJECTS instead (a very common LLM failure), `_children_of` filtered
        # them out and nothing complained: no dangling ref (the ref never
        # existed), no depth accounting, no repair error. The section the
        # model believed it filled validated clean and rendered EMPTY, and the
        # pipeline reported success with repairs:0. Accept-and-degrade is the
        # exact class the _validate codex guards were written to remove, so
        # this is a hard error like an unknown theme — not a silent drop.
        for key in _CHILD_KEYS:
            value = comp.get(key)
            if not isinstance(value, list):
                continue
            inline = sum(1 for v in value if not isinstance(v, str))
            if inline:
                errors.append(
                    f"{ctype} {comp.get('id')!r} has {inline} inline child "
                    f"object(s) in `{key}` — children must be component ID "
                    "strings; define each child as its own component and "
                    "reference it by id"
                )
        if ctype == "Section":
            title = comp.get("title")
            if not isinstance(title, str) or not title.strip():
                errors.append(f"Section {comp.get('id')!r} has no title — no anonymous blocks")
        if ctype == "AppHeader" and not comp.get("composedAt"):
            errors.append("AppHeader is missing composedAt — the freshness stamp is mandatory")
        if ctype in BINDING_MANDATORY:
            path = comp.get("path")
            if not isinstance(path, str) or not path.strip():
                errors.append(
                    f"{ctype} {comp.get('id')!r} has no `path` — a chart with no "
                    "bound series is meaningless (F094 §5)"
                )
        if ctype == "LineChart":
            # Data-free arity, so the repair loop leads with THIS clean message
            # instead of the schema's maxItems error — which reads "remove path
            # and series" (both valid) because the failed `series` branch marks
            # them unevaluated, guiding the model to make the app worse (rev-be
            # P2). `minItems:1` in the schema still backstops the empty case.
            n_series = len(comp.get("series") or []) if isinstance(comp.get("series"), list) else 0
            if n_series > 4:
                errors.append(
                    f"LineChart {comp.get('id')!r} declares {n_series} series — max 4"
                )
        for tmpl in _template_children(comp):
            # F093 §6.2 Repeat: a template child must name a real component
            # and bind an array path; the referenced component renders ONCE
            # per array item, so it must exist in the flat list.
            if not isinstance(tmpl.get("path"), str) or not tmpl["path"].strip():
                errors.append(
                    f"repeat template in {comp.get('id')!r} has no `path` — it "
                    "must bind the array to expand over"
                )
            if tmpl["componentId"] not in by_id:
                errors.append(
                    f"repeat template in {comp.get('id')!r} references unknown "
                    f"component {tmpl['componentId']!r}"
                )
        if ctype == "StatRow":
            # Type-check the children (codex round 3): the catalog schema
            # only bounds children to <=4 strings, so a Text or Card ref
            # would otherwise render arbitrary content in the summary grid.
            for kid in comp.get("children") or []:
                if not isinstance(kid, str):
                    # An inline child object is already reported above; feeding
                    # the dict to `by_id.get` raises `TypeError: unhashable
                    # type` — and lint runs BEFORE schema validation, so that
                    # escapes _validate entirely, taking the repair loop and
                    # the guaranteed fallback with it (codex P2). A lint pass
                    # must always return errors, never raise.
                    continue
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
    elif not isinstance(root.get("children"), list):
        # The skeleton is a fixed ordered list; a template child at root
        # (F093 §6.2) would make the skeleton unrepresentable. Repeat lives
        # inside a Section, never at root.
        errors.append("root children must be an ordered list, not a repeat template")
    else:
        order = [
            by_id[cid].get("component")
            for cid in root.get("children") or []
            if cid in by_id
        ]
        skeleton_error = _skeleton_error(order, max_sections)
        if skeleton_error:
            errors.append(skeleton_error)

    # --- depth --------------------------------------------------------------
    if root is not None:
        depth = _max_depth(by_id, "root")
        if depth > MAX_DEPTH:
            errors.append(f"nesting depth {depth} exceeds {MAX_DEPTH}")

    return errors


def _skeleton_error(order: list[str | None], max_sections: int = MAX_SECTIONS) -> str | None:
    """AppHeader · StatRow? · Section{1,N} · AppFooter — anything else fails."""
    want = f"AppHeader, optional StatRow, 1-{max_sections} Section, AppFooter"
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
    if sections < 1 or sections > max_sections:
        return f"skeleton: need 1-{max_sections} Section blocks, got {sections} (want {want})"
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
