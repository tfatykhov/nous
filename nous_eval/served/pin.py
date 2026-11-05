"""Which code a run measures, and how to run a pinned commit.

The served package runs from the same root as the ``nous`` it measures, so the stack wiring
(which mirrors nous/main.py) travels with the code under test. ``--code`` names that root: a
directory, or a commit, which gets a detached worktree. The CLI then re-runs itself inside that
root (``python -m nous_eval.served`` with PYTHONPATH set to it). Every run records the HEAD and
dirty-file count of the root it imported, and a hash of the served package files, so ``compare``
can tell a code difference and an instrument difference apart.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

# Prefixes of the process environment a child never inherits: the code under test reads them.
_HIDDEN_PREFIXES = ("NOUS_", "DB_")


def _git(root: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


def imported_roots() -> tuple[Path, Path]:
    """The roots the running ``nous`` and ``nous_eval`` packages were imported from."""
    import nous
    import nous_eval

    return Path(nous.__file__).resolve().parent.parent, Path(nous_eval.__file__).resolve().parent.parent


# The nous_eval modules a run imports besides the served package: part of the instrument.
_INSTRUMENT_DEPS = (
    "beam/harness.py",
    "beam/arms.py",
    "retrieval_runner.py",
    "env_pin.py",
    "_oat_preamble.py",
    "schema_preflight.py",
    "config.py",
)


def served_hash() -> str:
    """sha256 over the instrument's files (relative name and bytes, sorted): the served package's
    .py files and the nous_eval modules it imports."""
    pkg = Path(__file__).resolve().parent
    files = sorted(pkg.glob("*.py")) + [pkg.parent / d for d in _INSTRUMENT_DEPS]
    h = hashlib.sha256()
    for p in sorted(files, key=lambda f: f.relative_to(pkg.parent).as_posix()):
        h.update(p.relative_to(pkg.parent).as_posix().encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


def code_info() -> dict[str, Any]:
    """HEAD, branch and dirty-file count (nous/, nous_eval/, sql/, pyproject.toml, uv.lock) of the
    imported root."""
    nous_root, eval_root = imported_roots()
    dirty = _git(nous_root, "status", "--porcelain", "--", "nous", "nous_eval", "sql", "pyproject.toml", "uv.lock")
    return {
        "root": str(nous_root),
        "nous_eval_root": str(eval_root),
        "head": _git(nous_root, "rev-parse", "HEAD") or "unknown",
        "branch": _git(nous_root, "rev-parse", "--abbrev-ref", "HEAD") or "unknown",
        "dirty_files": len(dirty.splitlines()) if dirty else 0,
        "served_sha256": served_hash(),
    }


def check_code(info: dict[str, Any], *, expected_root: str | None, allow_dirty: bool) -> None:
    """Refuse two copies of the code (nous and nous_eval from different roots), a root other than
    the pinned one, and uncommitted changes unless allowed."""
    if Path(info["root"]) != Path(info["nous_eval_root"]):
        raise SystemExit(f"nous ({info['root']}) and nous_eval ({info['nous_eval_root']}) come from different roots")
    if expected_root and Path(info["root"]) != Path(expected_root).resolve():
        raise SystemExit(f"imported nous from {info['root']}, expected the pinned root {expected_root}")
    if info["dirty_files"] and not allow_dirty:
        raise SystemExit(
            f"{info['dirty_files']} uncommitted file(s) under nous/, nous_eval/, sql/, pyproject.toml or uv.lock "
            f"in {info['root']}; "
            "commit, pin a commit with --code, or pass --allow-dirty"
        )


def resolve_code_root(spec: str, *, repo: Path, worktree_base: Path) -> Path:
    """A directory is used as given; anything else is a commit, checked out (once) in a detached
    worktree ``<worktree_base>/<sha12>``."""
    p = Path(spec)
    if p.is_dir():
        return p.resolve()
    sha = _git(repo, "rev-parse", "--verify", f"{spec}^{{commit}}")
    if not sha:
        raise SystemExit(f"--code {spec!r}: neither a directory nor a commit of {repo}")
    wt = (worktree_base / sha[:12]).resolve()
    if not wt.exists():
        worktree_base.mkdir(parents=True, exist_ok=True)
        out = subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "--detach", str(wt), sha], capture_output=True, text=True
        )
        if out.returncode != 0:
            raise SystemExit(f"git worktree add failed: {out.stderr.strip()[:300]}")
    if _git(wt, "rev-parse", "HEAD") != sha:
        raise SystemExit(f"worktree {wt} is not at {sha}")
    return wt


def child_env(code_root: Path) -> dict[str, str]:
    """The parent's environment minus NOUS_*/DB_*, with PYTHONPATH = the pinned root only and a
    fixed hash seed: the pre-turn build orders some items by set iteration, which varies with the
    per-process string hash seed (an A/A pair rendered the same sets in a different tail order)."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(_HIDDEN_PREFIXES)}
    env["PYTHONPATH"] = str(code_root)
    env["PYTHONHASHSEED"] = "0"
    return env


def reexec(code_root: Path, argv: list[str], *, cwd: Path) -> int:
    """Run ``python -P -m nous_eval.served <argv> --pinned-root <code_root>`` from ``cwd``: ``-P``
    keeps the working directory off ``sys.path``, so only PYTHONPATH (the pinned root) supplies
    ``nous`` and ``nous_eval`` whatever directory the command was started from."""
    cmd = [sys.executable, "-P", "-m", "nous_eval.served", *argv, "--pinned-root", str(code_root)]
    return subprocess.run(cmd, cwd=str(cwd), env=child_env(code_root)).returncode
