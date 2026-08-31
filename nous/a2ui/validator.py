"""F092: A2UI envelope validation against the vendored v1.0 schemas.

Two deliberate transformations happen at schema LOAD time (the vendored
files under catalogs/ stay byte-identical — see catalogs/VENDORED.md):

1. ``\\p{...}`` rewrite. common_types.json constrains Extensions keys with a
   UAX #31 pattern using Unicode property escapes, which Python's ``re``
   cannot compile — jsonschema RAISES instead of reporting invalid, and every
   Nous surface carries ``metadata.extensions.com_nous_nonce``. The pattern is
   rewritten to the close approximation ``^[^\\W\\d][\\w]*$``.

2. Merged catalog. The envelope schema references a single placeholder
   ``catalog.json`` — the $ref is static, so a surface mixing basic and
   nous-core components (the Action Review shape) cannot validate against
   either catalog alone. We register a merged document (union of components/
   functions and the anyComponent/anyFunction branch lists) at the
   placeholder URI.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

CATALOGS_DIR = Path(__file__).parent / "catalogs"
SPEC_BASE = "https://a2ui.org/specification/v1_0/"

BASIC_CATALOG_ID = "https://a2ui.org/specification/v1_0/catalogs/basic/catalog.json"
NOUS_CORE_CATALOG_ID = "https://nous.fatykhov.us/a2ui/v1.0/nous-core/catalog.json"

# Python re has no \p{XID_Start}/\p{XID_Continue}. [^\W\d] = word char that is
# not a digit (letters + underscore), [\w]* = XID_Continue approximation.
_UAX31_APPROX = "^[^\\W\\d][\\w]*$"


def _rewrite_unicode_patterns(node: Any) -> Any:
    """Recursively replace \\p{...} regex patterns with a Python-safe form."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key in ("pattern",) and isinstance(value, str) and "\\p{" in value:
                out[key] = _UAX31_APPROX
            elif key == "patternProperties" and isinstance(value, dict):
                out[key] = {
                    (_UAX31_APPROX if "\\p{" in pat else pat): _rewrite_unicode_patterns(sub)
                    for pat, sub in value.items()
                }
            else:
                out[key] = _rewrite_unicode_patterns(value)
        return out
    if isinstance(node, list):
        return [_rewrite_unicode_patterns(item) for item in node]
    return node


def _load(path: Path) -> dict:
    return _rewrite_unicode_patterns(json.loads(path.read_text(encoding="utf-8")))


def _merge_catalogs(basic: dict, nous_core: dict) -> dict:
    """Union two catalogs into one validation document.

    Key collisions are a hard error: a component name resolving to two
    definitions would validate against the wrong schema silently.
    """
    merged = json.loads(json.dumps(basic))
    for section in ("components", "functions"):
        ours = nous_core.get(section, {})
        collisions = set(merged.get(section, {})) & set(ours)
        if collisions:
            raise ValueError(f"catalog {section} name collision: {sorted(collisions)}")
        merged.setdefault(section, {}).update(ours)
    for defs_key in ("anyComponent", "anyFunction"):
        extra = nous_core.get("$defs", {}).get(defs_key, {}).get("oneOf", [])
        # Relative "#/components/X" refs resolve within the merged doc, which
        # now contains both catalogs' definitions — no rewriting needed.
        merged["$defs"][defs_key].setdefault("oneOf", []).extend(extra)
    # The merged doc must own the placeholder identity: it inherits basic's
    # $id, and fragment refs resolve against $id through the registry — if the
    # basic URI still mapped to the PRISTINE basic, "#/components/ApprovalPanel"
    # would dangle. So the merged doc is registered under every catalog URI.
    merged["$id"] = SPEC_BASE + "catalog.json"
    return merged


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    json_dir = CATALOGS_DIR / "json"
    envelope = _load(json_dir / "agent_to_renderer.json")
    common = _load(json_dir / "common_types.json")
    renderer_to_agent = _load(json_dir / "renderer_to_agent.json")
    basic = _load(CATALOGS_DIR / "basic" / "catalog.json")
    nous_core = _load(CATALOGS_DIR / "nous_core" / "catalog.json")
    merged = _merge_catalogs(basic, nous_core)

    registry = Registry().with_resources(
        [
            (SPEC_BASE + "common_types.json", Resource.from_contents(common)),
            (SPEC_BASE + "catalog.json", Resource.from_contents(merged)),
            (SPEC_BASE + "agent_to_renderer.json", Resource.from_contents(envelope)),
            (SPEC_BASE + "renderer_to_agent.json", Resource.from_contents(renderer_to_agent)),
            (BASIC_CATALOG_ID, Resource.from_contents(merged)),
            (NOUS_CORE_CATALOG_ID, Resource.from_contents(merged)),
        ]
    )
    return Draft202012Validator(envelope, registry=registry)


def validate_envelope(envelope: dict) -> list[dict[str, Any]]:
    """Validate one agent->renderer envelope. Returns spec-shaped errors.

    Errors are DESCENDED before reporting (F092.1): the envelope schema is a
    oneOf over message types, and a single bad component prop used to
    surface as one root-level oneOf error whose message was the entire
    instance dump — which made the compose repair loop feed the model 300
    chars of its own output as "the problem". jsonschema's best_match walks
    into the failing branch; we additionally descend its context to the
    deepest failures so a Section that wrote ``children`` instead of
    ``child`` reports at ``.../components/6`` with the actual missing-prop
    message.
    """
    surface_id = ""
    for key in ("createSurface", "updateComponents", "updateDataModel", "deleteSurface"):
        if isinstance(envelope.get(key), dict):
            surface_id = envelope[key].get("surfaceId", "")
            break
    errors = []
    for err in _validator().iter_errors(envelope):
        leaves = _descend(err)
        primary = leaves[0]
        messages: list[str] = []
        for leaf in leaves[:5]:
            msg = leaf.message[:200]
            if msg not in messages:
                messages.append(msg)
        errors.append(
            {
                "code": "VALIDATION_FAILED",
                "surfaceId": surface_id,
                "path": "/" + "/".join(str(p) for p in primary.absolute_path),
                "message": "; ".join(messages)[:300],
            }
        )
    return errors


def _descend(err: Any, depth: int = 0) -> list[Any]:
    """Follow oneOf/anyOf context down to the most specific failures.

    Two heuristics, both about which oneOf BRANCH the instance was actually
    aiming for (context errors carry their branch index as the head of
    ``schema_path``):

    - Discriminator: inside an anyComponent oneOf, every wrong branch fails
      on its ``component`` const ("'Text' was expected" × 20 — noise). Any
      branch containing such a failure is dropped; the branch whose const
      matched fails on its real problem ("'child' is a required property").
    - Depth: among surviving branches, keep those whose errors reach the
      deepest instance path — at the envelope level the createSurface
      branch fails deep inside components while the updateDataModel branch
      fails at the root on a missing key nobody intended to send.

    An instance matching NO branch (unknown component) keeps the oneOf
    error itself, at its own path rather than the envelope root.
    """
    if not err.context or depth >= 8:
        return [err]
    by_branch: dict[Any, list[Any]] = {}
    for child in err.context:
        schema_path = list(child.schema_path)
        by_branch.setdefault(schema_path[0] if schema_path else -1, []).append(child)
    survivors = {
        branch: errs
        for branch, errs in by_branch.items()
        if not any(
            e.validator == "const" and (list(e.absolute_path) or [None])[-1] == "component"
            for e in errs
        )
    }
    if not survivors:
        return [err]

    def _branch_depth(errs: list[Any]) -> int:
        return max(len(list(e.absolute_path)) for e in errs)

    deepest = max(_branch_depth(errs) for errs in survivors.values())
    picked: list[Any] = []
    seen: set[tuple[str, str]] = set()
    for errs in survivors.values():
        if _branch_depth(errs) != deepest:
            continue
        for child in errs:
            for leaf in _descend(child, depth + 1):
                key = ("/".join(str(p) for p in leaf.absolute_path), leaf.message[:120])
                if key not in seen:
                    seen.add(key)
                    picked.append(leaf)
        if len(picked) >= 8:
            break
    return picked or [err]


def validate_structure(
    components: list[dict],
    allowed_actions: list[str],
    surface_id: str = "",
) -> list[dict[str, Any]]:
    """Structural checks past JSON Schema, applied to a full component list.

    Builders must satisfy all of these; the LLM path (compose_surface, later
    phase) repairs against them. Checks: exactly one root, no dangling child
    refs, no cycles, every action event name in the surface allowlist.
    """
    errors: list[dict[str, Any]] = []

    def err(path: str, message: str, code: str = "VALIDATION_FAILED") -> None:
        errors.append({"code": code, "surfaceId": surface_id, "path": path, "message": message})

    by_id: dict[str, dict] = {}
    for i, comp in enumerate(components):
        cid = comp.get("id")
        if not cid:
            err(f"/components/{i}", "component missing id")
            continue
        if cid in by_id:
            err(f"/components/{i}", f"duplicate component id {cid!r}")
        by_id[cid] = comp

    if "root" not in by_id:
        err("/components", "no component with id 'root'")

    def child_ids(comp: dict) -> list[str]:
        ids: list[str] = []
        for key, value in comp.items():
            if key == "child" and isinstance(value, str):
                ids.append(value)
            elif key == "children":
                if isinstance(value, list):
                    ids.extend(v for v in value if isinstance(v, str))
                elif isinstance(value, dict) and isinstance(value.get("componentId"), str):
                    ids.append(value["componentId"])
            elif key in ("trigger", "content") and isinstance(value, str):
                ids.append(value)
            elif key == "tabs" and isinstance(value, list):
                ids.extend(t["child"] for t in value if isinstance(t, dict) and isinstance(t.get("child"), str))
        return ids

    for cid, comp in by_id.items():
        for ref in child_ids(comp):
            if ref not in by_id:
                err(f"/components/{cid}", f"dangling child ref {ref!r}")

    # Cycle detection over the child graph (iterative DFS, 3-color).
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(by_id, WHITE)
    for start in by_id:
        if color[start] != WHITE:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        color[start] = GRAY
        while stack:
            node, idx = stack[-1]
            kids = [k for k in child_ids(by_id[node]) if k in by_id]
            if idx < len(kids):
                stack[-1] = (node, idx + 1)
                kid = kids[idx]
                if color[kid] == GRAY:
                    err(f"/components/{kid}", f"cycle through {kid!r}")
                    color[kid] = BLACK
                elif color[kid] == WHITE:
                    color[kid] = GRAY
                    stack.append((kid, 0))
            else:
                color[node] = BLACK
                stack.pop()

    allowed = set(allowed_actions)
    for cid, comp in by_id.items():
        action = comp.get("action")
        if isinstance(action, dict):
            # The canonical shape is {"event": {"name": ...}} (dsl.py). A
            # string event — a plausible malformed LLM output — used to raise
            # AttributeError here, escaping both the compose repair loop and
            # the fallback. Malformed shapes must be validation ERRORS.
            event = action.get("event")
            name = event.get("name") if isinstance(event, dict) else None
            if not name:
                err(
                    f"/components/{cid}",
                    'action must be shaped {"event": {"name": ...}}',
                )
            elif name not in allowed:
                err(
                    f"/components/{cid}",
                    f"action {name!r} not in surface allowed_actions",
                )

    return errors


def load_catalog(name: str) -> dict | None:
    """Serve a vendored catalog by short name (route: /a2ui/catalog/{name})."""
    paths = {
        "basic": CATALOGS_DIR / "basic" / "catalog.json",
        "nous-core": CATALOGS_DIR / "nous_core" / "catalog.json",
    }
    path = paths.get(name)
    if path is None or not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
