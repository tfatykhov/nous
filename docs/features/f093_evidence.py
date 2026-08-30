#!/usr/bin/env python3
"""F093 evidence generator.

Every number in F093 §1 is produced HERE, not hand-typed into the prose.
Four adversarial review rounds produced twelve P1 defects; every single one was a
stale or mis-scoped count in a hand-maintained table, not an error of argument.
The table is output, not prose.

Usage:  python3 docs/features/f093_evidence.py            # markdown table
        python3 docs/features/f093_evidence.py --json     # raw numbers
Run from the repo root.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPANION = ROOT / "dashboard-app/src/companion"
CATALOG = COMPANION / "catalog"
GRAMMAR = ROOT / "nous/a2ui/grammar.py"
CSS = COMPANION / "companion.css"

# Canonical token-reference pattern. Deliberately does NOT require a closing
# paren: `var(--mono, monospace)` (DecisionCardView.svelte:101) is a real
# reference and the `var\(--[a-z0-9-]*\)` form silently drops it. That single
# character of pattern is the difference between 188 and 189 and cost one
# review round.
VAR_REF = re.compile(r"var\(--")


def views() -> list[Path]:
    """Catalog *views* — .svelte only. Tests and index.ts are not vocabulary."""
    return sorted(CATALOG.glob("*.svelte"))


def _grammar_list(name: str) -> list[str]:
    """Read a frozenset/set literal out of grammar.py without importing it."""
    src = GRAMMAR.read_text()
    m = re.search(rf"^{name}\s*(?::[^=]*)?=\s*(?:frozenset\(\s*)?\{{(.*?)\}}", src, re.S | re.M)
    if not m:
        raise SystemExit(f"could not locate {name} in {GRAMMAR}")
    return re.findall(r"[\"']([A-Za-z][A-Za-z0-9_]*)[\"']", m.group(1))


def _lineno(path: Path, needle: str) -> int | None:
    for i, line in enumerate(path.read_text().splitlines(), 1):
        if needle in line:
            return i
    return None


def _block_extent(path: Path, start_needle: str) -> tuple[int, int] | None:
    """First line of a `$derived.by(() => {` block through its closing `});`,
    matched by brace depth. Hand-typed ranges truncated mid-function twice."""
    lines = path.read_text().splitlines()
    start = next((i for i, l in enumerate(lines) if start_needle in l), None)
    if start is None:
        return None
    depth = 0
    for i in range(start, len(lines)):
        depth += lines[i].count("{") - lines[i].count("}")
        if depth <= 0 and i > start:
            return start + 1, i + 1
    return None


def root_tokens() -> list[str]:
    src = CSS.read_text()
    block = re.search(r":root\s*\{(.*?)\}", src, re.S)
    if not block:
        raise SystemExit("no :root block in companion.css")
    return re.findall(r"^\s*(--[a-z0-9-]+)\s*:", block.group(1), re.M)


def collect() -> dict:
    v = views()
    refs = {p: len(VAR_REF.findall(p.read_text())) for p in v}
    view_refs = sum(refs.values())
    view_files = sum(1 for n in refs.values() if n)

    pkg_refs = sum(
        len(VAR_REF.findall(p.read_text()))
        for p in COMPANION.rglob("*")
        if p.is_file() and p.suffix in {".svelte", ".css", ".ts"}
    )

    defined = set(root_tokens())
    used = {
        m
        for p in COMPANION.rglob("*")
        if p.is_file() and p.suffix in {".svelte", ".css", ".ts"}
        for m in re.findall(r"var\((--[a-z0-9-]+)", p.read_text())
    }

    # Colour literals that bypass the token layer. Excludes Svelte block
    # syntax ({#each, {#if ...) — the naive hex grep matches those and 18 of
    # its 20 hits were false. Any hex or rgb()/rgba() outside companion.css.
    literals = []
    for p in sorted(COMPANION.rglob("*")):
        if not p.is_file() or p.suffix not in {".svelte", ".ts"} or ".test." in p.name:
            continue
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if re.search(r"\{[#/:]", line):
                continue
            if re.search(r"#[0-9a-fA-F]{3,8}\b", line) or re.search(r"\brgba?\(", line):
                literals.append(f"{p.relative_to(ROOT)}:{i}  {line.strip()}")

    sev = {
        t: sum(len(re.findall(rf"var\(--{t}\)", p.read_text())) for p in v)
        for t in ("red", "yellow", "green")
    }

    caps = {
        k: int(m.group(1))
        for k in ("MAX_COMPONENTS", "MAX_SECTIONS", "MAX_DEPTH")
        if (m := re.search(rf"^{k}\s*=\s*(\d+)", GRAMMAR.read_text(), re.M))
    }
    cap_lines = sorted(filter(None, (_lineno(GRAMMAR, f"{k} =") for k in caps)))

    return {
        "allowed_components": sorted(_grammar_list("ALLOWED_COMPONENTS")),
        "banned_components": sorted(_grammar_list("BANNED_COMPONENTS")),
        "view_count": len(v),
        "view_refs": view_refs,
        "view_files_with_refs": view_files,
        "pkg_refs": pkg_refs,
        "root_tokens": sorted(defined),
        "undefined_tokens": sorted(used - defined),
        "colour_literals": literals,
        "severity_call_sites": sev,
        "severity_total": sum(sev.values()),
        "caps": caps,
        "cap_lines": f"{cap_lines[0]}-{cap_lines[-1]}" if cap_lines else "?",
        "accent": next(
            (l.split(":")[1].strip(" ;") for l in CSS.read_text().splitlines() if "--accent:" in l),
            "?",
        ),
        "escape_hatches": subprocess.run(
            ["grep", "-rIl", "-E", r"dangerouslySetInnerHTML|iframe|rawHtml|customCss",
             str(COMPANION), str(ROOT / "nous/a2ui")],
            capture_output=True, text=True,
        ).stdout.split(),
        "extents": {
            "DagGraphView": _block_extent(CATALOG / "DagGraphView.svelte", "const layout = $derived.by"),
            "MemoryGraphView": _block_extent(CATALOG / "MemoryGraphView.svelte", "const positions = $derived.by"),
            "ConfidenceMeterView": _block_extent(CATALOG / "ConfidenceMeterView.svelte", "$derived"),
        },
        "sha": subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True
        ).stdout.strip(),
    }


def main() -> None:
    d = collect()
    if "--json" in sys.argv:
        print(json.dumps(d, indent=2))
        return
    ex = d["extents"]
    dg = "%d-%d" % ex["DagGraphView"] if ex["DagGraphView"] else "?"
    mg = "%d-%d" % ex["MemoryGraphView"] if ex["MemoryGraphView"] else "?"
    print(f"<!-- generated by f093_evidence.py against {d['sha']} — do not hand-edit -->")
    print("| Observation | Measurement | Source |")
    print("|---|---|---|")
    print(
        f"| Component vocabulary | **{len(d['allowed_components'])} names**, display primitives only. "
        f"(A {len(d['banned_components'])}-name `BANNED_COMPONENTS` set of *input* widgets is rejected "
        f"outright, so it is not vocabulary.) | `grammar.py` |"
    )
    print(
        f"| Escape hatch for custom styling | **{'none' if not d['escape_hatches'] else d['escape_hatches']}** "
        f"— 0 files match `dangerouslySetInnerHTML\\|iframe\\|rawHtml\\|customCss` | `grep` across "
        f"`nous/a2ui/`, `dashboard-app/src/companion/` |"
    )
    print(f"| Theme tokens | **{len(d['root_tokens'])} CSS variables**, single `:root` block | `companion.css` |")
    print(
        f"| View references to those tokens | **{d['view_refs']}** `var(--…)` across "
        f"**{d['view_files_with_refs']} of {d['view_count']}** catalog views; **{d['pkg_refs']}** package-wide "
        f"| `grep -c 'var(--'` |"
    )
    print(
        f"| Colour literals bypassing the token layer | **{len(d['colour_literals'])}** | "
        + "; ".join(l.split("  ")[0] for l in d["colour_literals"])
        + " |"
    )
    print(
        f"| Severity-colour call sites (migration surface) | **{d['severity_total']}** across catalog views "
        f"(`--red` {d['severity_call_sites']['red']}, `--yellow` {d['severity_call_sites']['yellow']}, "
        f"`--green` {d['severity_call_sites']['green']}) | `grep -o 'var(--red)'` etc. |"
    )
    print(
        "| Structural caps | "
        + ", ".join(f"`{k} = {v}`" for k, v in d["caps"].items())
        + f" | `grammar.py:{d['cap_lines']}` |"
    )
    print(
        f"| Renderer-owned computation | `ConfidenceMeterView`, `AppHeaderView` (display value); "
        f"`DagGraphView` (`:{dg}`), `MemoryGraphView` (`:{mg}`) (geometry) | see §1 prose |"
    )
    if d["undefined_tokens"]:
        print(
            f"| **Referenced but undefined tokens** | {', '.join('`%s`' % t for t in d['undefined_tokens'])} "
            f"— resolve to nothing under the default theme | `:root` in `companion.css` |"
        )


if __name__ == "__main__":
    main()
