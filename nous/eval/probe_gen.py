"""F051 deterministic probe / qrels generator.

Parses ``docs/features/INDEX.md`` + recent ``git log --grep=feat`` history to
produce **deterministic** qrels — no LLM in the loop, so the same inputs yield
byte-identical output. Used as one of three qrels sources in the harness:

  - ``probes`` (this file)         — feature-ID lookups, deterministic
  - ``hand-labels`` (hand_labels_draft.py) — Sonnet-drafted, human-reviewed
  - ``longmemeval`` (ingest_longmemeval.py) — academic benchmark subset

Output JSONL schema (one row per probe):

    {
        "query": "What does F049 ship?",
        "gold_ids": [],                 # filled in by hand-labels pass
        "memory_types": ["fact", "decision"],
        "source": "probes",
        "notes": {
            "feature_id": "F049",
            "title": "Session & Memory Lifecycle Hygiene",
            "commit_sha": "8665adc",
            "doc_path": "docs/features/F049-session-lifecycle-hygiene.md"
        },
        "reviewed_by": "auto"           # gate-eligible source
    }
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

INDEX_MD_DEFAULT = Path("docs/features/INDEX.md")
OUT_DEFAULT = Path("tests/fixtures/probes_qrels.jsonl")

# Match feature ID + title in INDEX.md — covers `| F049 | ...` table rows AND
# the heading-style entries seen in earlier revisions.
FEATURE_ROW_RE = re.compile(
    r"\bF(\d{2,3})\b[^A-Za-z0-9]*([^|\n]{2,120}?)(?=\s*\||$)",
    re.MULTILINE,
)
# `feat(F049): ...` extraction from `git log --grep`.
FEAT_COMMIT_RE = re.compile(r"^([0-9a-f]{7,40})\s+feat\(F(\d{2,3})\):\s+(.+)$", re.MULTILINE)


@dataclass
class Probe:
    feature_id: str
    title: str
    commit_sha: str | None
    doc_path: str | None

    def to_jsonl_row(self) -> str:
        return json.dumps(
            {
                "query": f"What does {self.feature_id} ship?",
                "gold_ids": [],
                "memory_types": ["fact", "decision"],
                "source": "probes",
                "notes": {
                    "feature_id": self.feature_id,
                    "title": self.title.strip(),
                    "commit_sha": self.commit_sha,
                    "doc_path": self.doc_path,
                },
                "reviewed_by": "auto",
            }
        )


# ---------------------------------------------------------------------------
# Source extraction
# ---------------------------------------------------------------------------


def _parse_index(index_md: Path) -> dict[str, str]:
    """Map ``F049`` -> ``"Session & Memory Lifecycle Hygiene"`` from INDEX.md.

    Returns ``{}`` if the file is missing — caller logs a WARN and continues.
    """
    if not index_md.is_file():
        logger.warning("[eval.probe_gen] %s missing — skipping INDEX parse", index_md)
        return {}
    text = index_md.read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for m in FEATURE_ROW_RE.finditer(text):
        fid = f"F{int(m.group(1)):03d}"
        title = re.sub(r"\s+", " ", m.group(2)).strip(" -:|*")
        # Last-write-wins so a feature with multiple INDEX hits gets the last
        # (typically most descriptive) title.
        out[fid] = title
    return out


def _parse_git_log(limit: int = 200) -> list[tuple[str, str, str]]:
    """Return ``[(sha, feature_id, subject), ...]`` from ``git log --grep=^feat``."""
    try:
        result = subprocess.run(
            [
                "git",
                "log",
                "--no-merges",
                "--pretty=format:%h %s",
                f"-n{limit}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        logger.warning("[eval.probe_gen] git log unavailable — skipping commit parse")
        return []
    matches: list[tuple[str, str, str]] = []
    for line in result.stdout.splitlines():
        m = FEAT_COMMIT_RE.match(line)
        if m:
            matches.append((m.group(1), f"F{int(m.group(2)):03d}", m.group(3).strip()))
    return matches


def _doc_path_for(fid: str, root: Path) -> str | None:
    """Best-effort lookup of ``docs/features/F049-*.md``."""
    feat_dir = root / "docs" / "features"
    if not feat_dir.is_dir():
        return None
    for p in sorted(feat_dir.glob(f"{fid}-*.md")):
        return p.relative_to(root).as_posix()
    return None


# ---------------------------------------------------------------------------
# Merge + emit
# ---------------------------------------------------------------------------


def build_probes(index_md: Path, root: Path) -> list[Probe]:
    """Combine INDEX titles + git commit subjects into a deduped probe list.

    INDEX is the source of truth for the title; git provides the commit_sha.
    Features that appear only in git but not INDEX still produce a probe (the
    title falls back to the commit subject).
    """
    index = _parse_index(index_md)
    commits = _parse_git_log()

    # commit_sha lookup — first commit found wins (most recent in git log order).
    commit_for_fid: dict[str, str] = {}
    for sha, fid, _ in commits:
        commit_for_fid.setdefault(fid, sha)

    out: dict[str, Probe] = {}
    for fid, title in index.items():
        out[fid] = Probe(
            feature_id=fid,
            title=title,
            commit_sha=commit_for_fid.get(fid),
            doc_path=_doc_path_for(fid, root),
        )
    for sha, fid, subject in commits:
        if fid not in out:
            out[fid] = Probe(
                feature_id=fid,
                title=subject,
                commit_sha=sha,
                doc_path=_doc_path_for(fid, root),
            )
    return sorted(out.values(), key=lambda p: p.feature_id)


def write_probes(probes: list[Probe], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for p in probes:
            fh.write(p.to_jsonl_row() + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(prog="python -m nous.eval.probe_gen")
    parser.add_argument("--index", type=Path, default=INDEX_MD_DEFAULT)
    parser.add_argument("--out", type=Path, default=OUT_DEFAULT)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="Repo root (used to resolve docs/features/ paths).",
    )
    args = parser.parse_args(argv)
    probes = build_probes(args.index, args.root)
    write_probes(probes, args.out)
    logger.info("[eval.probe_gen] wrote %d probes -> %s", len(probes), args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
