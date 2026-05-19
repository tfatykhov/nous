"""SKILL.md parser — extracts frontmatter into SkillManifest and converts to ProcedureInput.

F011 v2: Single module, no scanner, no file watcher. Just parsing logic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from nous.heart.schemas import ProcedureInput


@dataclass
class SkillManifest:
    """Parsed representation of a SKILL.md file."""

    name: str
    description: str
    domain: str
    triggers: list[str] = field(default_factory=list)
    frames: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    source_url: str | None = None
    version: str | None = None
    raw_content: str = ""
    first_section: str = ""
    warnings: list[str] = field(default_factory=list)
    # F064.4 — workflow-as-code runtime hints. v1 ships manifest+
    # persistence only; the orchestrator consumer is deferred to
    # F064.4-v2 and gated by NOUS_SKILL_RUNTIME_METADATA_ENABLED.
    # Always parsed (no flag gate at write time) so a skill author
    # never has their declaration silently dropped.
    concurrency_cap: int | None = None
    timeout_override_seconds: int | None = None
    hooks: dict[str, str] = field(default_factory=dict)
    requires_human_review: bool = False


# Regex for YAML frontmatter block
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

# Lenient: fenced yaml block
_FENCED_YAML_RE = re.compile(r"^```ya?ml\s*\n(.*?)\n```\s*\n?", re.DOTALL)

# Lenient: frontmatter with missing closing --- (end at first ## or EOF)
_OPEN_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)(?=\n##\s+|\Z)", re.DOTALL)

# Regex for first H2 section
_FIRST_H2_RE = re.compile(r"^##\s+.+?\n(.*?)(?=\n##\s+|\Z)", re.MULTILINE | re.DOTALL)


def _parse_yaml_value(value: str) -> str | list[str]:
    """Minimal YAML value parser — handles strings, quoted strings, and inline lists."""
    value = value.strip()

    # Inline list: [a, b, c]
    if value.startswith("[") and value.endswith("]"):
        items = value[1:-1].split(",")
        return [_unquote(item.strip()) for item in items if item.strip()]

    return _unquote(value)


def _unquote(s: str) -> str:
    """Remove surrounding quotes."""
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


def _parse_frontmatter(text: str) -> dict[str, str | list[str] | dict[str, str]]:
    """Parse YAML frontmatter into a flat dict.

    Handles:
    - key: value (scalar)
    - key: [a, b, c] (inline list)
    - key:\\n  - a\\n  - b (block list)
    - key:\\n  sub: val (block dict — added in F064.4 for `hooks:` etc.)
    """
    result: dict[str, str | list[str] | dict[str, str]] = {}
    lines = text.split("\n")
    current_key: str | None = None
    current_list: list[str] | None = None
    current_dict: dict[str, str] | None = None
    # `current_pending` is True after we see `key:` with empty value but
    # before we know if it's a list (`  - item`) or dict (`  sub: val`).
    current_pending: bool = False

    def _flush_block() -> None:
        nonlocal current_key, current_list, current_dict, current_pending
        if current_key is None:
            return
        if current_list is not None:
            result[current_key] = current_list
        elif current_dict is not None:
            result[current_key] = current_dict
        elif current_pending:
            # Empty block — treat as empty list (most common YAML quirk).
            result[current_key] = []
        current_key = None
        current_list = None
        current_dict = None
        current_pending = False

    for line in lines:
        # Block list continuation: "  - item"
        list_match = re.match(r"^\s+-\s+(.*)", line)
        if current_key and (current_list is not None or current_pending) and list_match:
            if current_list is None:
                current_list = []
                current_pending = False
            current_list.append(_unquote(list_match.group(1).strip()))
            continue

        # Block dict continuation: "  subkey: subvalue"
        sub_match = re.match(r"^(\s+)(\w[\w_]*)\s*:\s*(.+)", line)
        if current_key and (current_dict is not None or current_pending) and sub_match:
            if current_dict is None:
                current_dict = {}
                current_pending = False
            current_dict[sub_match.group(2)] = _unquote(sub_match.group(3).strip())
            continue

        # End of block — flush and reset state before reading new top-level key.
        _flush_block()

        # Top-level key: value line (no leading whitespace)
        match = re.match(r"^(\w[\w_]*)\s*:\s*(.*)", line)
        if match:
            key = match.group(1)
            value = match.group(2).strip()

            if not value:
                # Start of a block — kind (list vs dict) determined by next line.
                current_key = key
                current_pending = True
            else:
                parsed = _parse_yaml_value(value)
                result[key] = parsed

    # Flush trailing block
    _flush_block()

    return result


def _ensure_list(val: str | list[str] | None) -> list[str]:
    """Coerce a value to a list of strings."""
    if val is None:
        return []
    if isinstance(val, str):
        return [val] if val else []
    return val


def _parse_optional_positive_int(val: object, *, field_name: str) -> int | None:
    """F064.4: parse a manifest int field that must be > 0 when present.

    Returns None when the field is absent or explicitly null. Raises
    ValueError on a 0 / negative / non-integer value — failing loudly is
    the always-persist counterpart: never silently coerce a malformed
    declaration to the "absent" default.
    """
    if val is None:
        return None
    if isinstance(val, str):
        try:
            parsed = int(val)
        except ValueError as e:
            raise ValueError(
                f"'{field_name}' must be a positive integer, got {val!r}"
            ) from e
    elif isinstance(val, int) and not isinstance(val, bool):
        parsed = val
    else:
        raise ValueError(
            f"'{field_name}' must be a positive integer, got {type(val).__name__}"
        )
    if parsed < 1:
        raise ValueError(
            f"'{field_name}' must be >= 1, got {parsed}"
        )
    return parsed


class SkillParser:
    """Parses SKILL.md markdown into SkillManifest."""

    def parse(self, markdown: str, source_hint: str | None = None) -> SkillManifest:
        """Parse SKILL.md markdown into SkillManifest.

        Tries strict frontmatter parsing first (--- ... ---).
        Falls back to lenient modes: strips whitespace, accepts ```yaml blocks,
        handles missing closing ---.

        Args:
            markdown: Full SKILL.md content
            source_hint: Optional source URL or path for attribution

        Raises:
            ValueError: If frontmatter cannot be extracted or required fields missing
        """
        warnings: list[str] = []
        text = markdown

        # Strict parse first
        fm_match = _FRONTMATTER_RE.match(text)

        if not fm_match:
            # Lenient: strip leading whitespace/blank lines
            stripped = text.lstrip()
            if stripped != text:
                fm_match = _FRONTMATTER_RE.match(stripped)
                if fm_match:
                    text = stripped
                    warnings.append("Auto-corrected: stripped leading whitespace before frontmatter")

        if not fm_match:
            # Lenient: fenced yaml block
            stripped = text.lstrip()
            fm_match = _FENCED_YAML_RE.match(stripped)
            if fm_match:
                text = stripped
                warnings.append("Auto-corrected: parsed ```yaml fenced block as frontmatter")

        if not fm_match:
            # Lenient: missing closing ---
            stripped = text.lstrip()
            fm_match = _OPEN_FRONTMATTER_RE.match(stripped)
            if fm_match:
                text = stripped
                warnings.append("Auto-corrected: missing closing --- delimiter")

        if not fm_match:
            raise ValueError(
                "SKILL.md must start with YAML frontmatter (--- ... ---). "
                "Also accepts ```yaml fenced blocks. "
                "Ensure the first non-blank line is --- or ```yaml."
            )

        fm_text = fm_match.group(1)
        body = text[fm_match.end():]

        data = _parse_frontmatter(fm_text)

        # Required fields — specific error messages
        name = data.get("name")
        if not name or not isinstance(name, str):
            raise ValueError("Frontmatter must include 'name' (string)")

        description = data.get("description")
        if not description or not isinstance(description, str):
            raise ValueError("Frontmatter must include 'description' (string)")

        domain = data.get("domain")
        if not domain or not isinstance(domain, str):
            domain = "general"

        # Extract first H2 section from body
        first_section = ""
        h2_match = _FIRST_H2_RE.search(body)
        if h2_match:
            first_section = h2_match.group(1).strip()

        source_url = data.get("source_url")
        if isinstance(source_url, list):
            source_url = source_url[0] if source_url else None

        version = data.get("version")
        if isinstance(version, list):
            version = version[0] if version else None

        # F064.4: parse new runtime hints. Each field has a defined default
        # equivalent to "absent" so a manifest that doesn't declare them
        # produces SkillManifest(...) with the default value. Parse errors
        # raise ValueError to surface a malformed manifest to the caller
        # (no silent drop — matches the always-persist semantic).
        concurrency_cap = _parse_optional_positive_int(
            data.get("concurrency_cap"), field_name="concurrency_cap"
        )
        timeout_override_seconds = _parse_optional_positive_int(
            data.get("timeout_override_seconds"),
            field_name="timeout_override_seconds",
        )
        raw_hooks = data.get("hooks", {})
        if raw_hooks and not isinstance(raw_hooks, dict):
            raise ValueError(
                f"'hooks' must be a dict[str, str], got {type(raw_hooks).__name__}"
            )
        hooks: dict[str, str] = {}
        if isinstance(raw_hooks, dict):
            for k, v in raw_hooks.items():
                if not isinstance(v, str):
                    raise ValueError(
                        f"'hooks.{k}' must be a string, got {type(v).__name__}"
                    )
                hooks[k] = v
        requires_human_review_raw = data.get("requires_human_review", False)
        if isinstance(requires_human_review_raw, str):
            requires_human_review = requires_human_review_raw.lower() in {"true", "yes", "1"}
        else:
            requires_human_review = bool(requires_human_review_raw)

        return SkillManifest(
            name=name,
            description=description,
            domain=domain,
            triggers=_ensure_list(data.get("triggers")),
            frames=_ensure_list(data.get("frames")),
            tools=_ensure_list(data.get("tools")),
            requires=_ensure_list(data.get("requires")),
            source_url=source_url if isinstance(source_url, str) else source_hint,
            version=version if isinstance(version, str) else None,
            raw_content=body.strip(),
            first_section=first_section,
            warnings=warnings,
            concurrency_cap=concurrency_cap,
            timeout_override_seconds=timeout_override_seconds,
            hooks=hooks,
            requires_human_review=requires_human_review,
        )

    def to_procedure_input(self, manifest: SkillManifest) -> ProcedureInput:
        """Convert SkillManifest to ProcedureInput for heart.store_procedure()."""
        impl_notes = []
        if manifest.source_url and manifest.source_url != "inline":
            impl_notes.append(f"source:{manifest.source_url}")
        elif manifest.source_url == "inline":
            impl_notes.append("source:inline")
        else:
            impl_notes.append("source:local")
        if manifest.version:
            impl_notes.append(f"version:{manifest.version}")
        if manifest.first_section:
            impl_notes.append(manifest.first_section)

        # Tags: "skill" + frame tags + source tag
        tags = ["skill"]
        tags.extend(manifest.frames)
        if manifest.source_url and ("clawhub" in manifest.source_url or "marketplace" in manifest.source_url):
            tags.append("marketplace")
        elif manifest.source_url == "inline":
            tags.append("inline")
        else:
            tags.append("local")

        # F064.4: build runtime_metadata UNCONDITIONALLY when ANY of the new
        # fields is non-default. The skill_runtime_metadata_enabled flag
        # only gates the deferred-to-v2 orchestrator consumer; the parser
        # never silently drops a declared field. `schema_version` lets v2
        # detect drift if we ever change the dict shape.
        runtime_metadata: dict | None = None
        if (
            manifest.concurrency_cap is not None
            or manifest.timeout_override_seconds is not None
            or manifest.hooks
            or manifest.requires_human_review
        ):
            runtime_metadata = {
                "schema_version": 1,
                "concurrency_cap": manifest.concurrency_cap,
                "timeout_override_seconds": manifest.timeout_override_seconds,
                "hooks": dict(manifest.hooks),
                "requires_human_review": manifest.requires_human_review,
            }

        return ProcedureInput(
            name=manifest.name,
            domain=manifest.domain,
            description=manifest.description,
            goals=manifest.triggers,
            core_tools=manifest.tools,
            core_patterns=manifest.triggers,
            core_concepts=[manifest.domain] + [f"requires:{r}" for r in manifest.requires],
            implementation_notes=impl_notes,
            tags=tags,
            runtime_metadata=runtime_metadata,
        )
