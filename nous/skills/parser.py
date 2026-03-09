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


# Regex for YAML frontmatter block
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

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


def _parse_frontmatter(text: str) -> dict[str, str | list[str]]:
    """Parse YAML frontmatter into a flat dict.

    Handles:
    - key: value (scalar)
    - key: [a, b, c] (inline list)
    - key:\\n  - a\\n  - b (block list)
    """
    result: dict[str, str | list[str]] = {}
    lines = text.split("\n")
    current_key: str | None = None
    current_list: list[str] | None = None

    for line in lines:
        # Block list continuation: "  - item"
        if current_key and current_list is not None and re.match(r"^\s+-\s+", line):
            item = re.sub(r"^\s+-\s+", "", line).strip()
            current_list.append(_unquote(item))
            continue

        # If we were building a list, save it
        if current_key and current_list is not None:
            result[current_key] = current_list
            current_key = None
            current_list = None

        # Key: value line
        match = re.match(r"^(\w[\w_]*)\s*:\s*(.*)", line)
        if match:
            key = match.group(1)
            value = match.group(2).strip()

            if not value:
                # Start of block list
                current_key = key
                current_list = []
            else:
                parsed = _parse_yaml_value(value)
                result[key] = parsed
                current_key = None
                current_list = None

    # Flush trailing list
    if current_key and current_list is not None:
        result[current_key] = current_list

    return result


def _ensure_list(val: str | list[str] | None) -> list[str]:
    """Coerce a value to a list of strings."""
    if val is None:
        return []
    if isinstance(val, str):
        return [val] if val else []
    return val


class SkillParser:
    """Parses SKILL.md markdown into SkillManifest."""

    def parse(self, markdown: str, source_hint: str | None = None) -> SkillManifest:
        """Parse SKILL.md markdown into SkillManifest.

        Args:
            markdown: Full SKILL.md content
            source_hint: Optional source URL or path for attribution

        Raises:
            ValueError: If frontmatter is missing or required fields absent
        """
        fm_match = _FRONTMATTER_RE.match(markdown)
        if not fm_match:
            raise ValueError("SKILL.md must start with YAML frontmatter (--- ... ---)")

        fm_text = fm_match.group(1)
        body = markdown[fm_match.end():]

        data = _parse_frontmatter(fm_text)

        # Required fields
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
        )

    def to_procedure_input(self, manifest: SkillManifest) -> ProcedureInput:
        """Convert SkillManifest to ProcedureInput for heart.store_procedure()."""
        impl_notes = []
        if manifest.source_url:
            impl_notes.append(f"source:{manifest.source_url}")
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
        else:
            tags.append("local")

        return ProcedureInput(
            name=manifest.name,
            domain=manifest.domain,
            description=manifest.description,
            goals=manifest.triggers,
            core_tools=manifest.tools,
            core_patterns=manifest.triggers,
            core_concepts=[manifest.domain] + manifest.requires,
            implementation_notes=impl_notes,
            tags=tags,
        )
