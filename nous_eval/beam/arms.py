"""Settings for one harness run: the scratch DB, the per-conversation agent, and
the arm's flag overrides.

An arm is a set of ``NOUS_*`` environment overrides (``--set NAME=VALUE``), parsed
by ``Settings`` exactly as the deployed environment is. A base config can come from
a dotenv file of flags (``--flags-file``, e.g. a prod env snapshot): only its
``NOUS_*`` flag names are read, never credentials. Credentials come from the
process environment / ``.env`` as for every eval script.

Compare arms that differ in one override. A read-path flag (e.g.
``NOUS_CONTEXT_QUERY_MODE``) is compared on one ingest; a write-path flag needs
its own ingest under its own ``--agent-tag``.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Never taken from an override: credentials, and what the harness sets itself.
_SECRET_RE = re.compile(r"KEY|TOKEN|PASSWORD|SECRET|CREDENTIAL", re.IGNORECASE)
_HARNESS_OWNED = {"DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME", "NOUS_AGENT_ID"}

# Recorded with every run (plus each overridden field). Names only: never a dump.
SNAPSHOT_FIELDS = (
    "model",
    "background_model",
    "contradiction_model",
    "api_backend",
    "embedding_model",
    "embedding_dimensions",
    "context_query_mode",
    "context_query_short_chars",
    "context_skip_unchanged_query",
    "query_expansion_enabled",
    "episode_chunks_enabled",
    "temporal_extraction_enabled",
    "admission_control_enabled",
    "cross_type_linking_enabled",
    "graph_backfill_enabled",
    "residual_activation_enabled",
    "epistemic_gate_enabled",
    "transcript_message_max_chars",
    "sleep_reflection_hours",
    "stale_scan_age_days",
)


def env_name_to_field() -> dict[str, str]:
    """``NOUS_CONTEXT_QUERY_MODE`` -> ``context_query_mode``, aliases included."""
    from nous.config import Settings

    prefix = Settings.model_config.get("env_prefix", "")
    out: dict[str, str] = {}
    for name, info in Settings.model_fields.items():
        alias = info.validation_alias
        env = alias if isinstance(alias, str) else f"{prefix}{name}"
        out[env.upper()] = name
    return out


def parse_overrides(pairs: list[str]) -> dict[str, str]:
    """``["NOUS_X=1", ...]`` -> ``{"NOUS_X": "1"}``. An unknown or credential name is an error."""
    known = env_name_to_field()
    out: dict[str, str] = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        name = name.strip().upper()
        if not sep or not name:
            raise SystemExit(f"--set expects NAME=VALUE, got {pair!r}")
        if name in _HARNESS_OWNED or _SECRET_RE.search(name):
            raise SystemExit(f"--set {name}: credentials and DB/agent settings are not arm variables")
        if name not in known:
            raise SystemExit(f"--set {name}: no Settings field reads this name in this code")
        out[name] = value
    return out


def load_flags_file(path: Path) -> tuple[dict[str, str], list[str]]:
    """``NOUS_*`` flags from a dotenv file; returns (flags, names skipped as unknown)."""
    known = env_name_to_field()
    flags: dict[str, str] = {}
    skipped: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not m:
            continue
        name = m.group(1).upper()
        if not name.startswith("NOUS_") or name in _HARNESS_OWNED or _SECRET_RE.search(name):
            continue
        if name not in known:
            skipped.append(name)
            continue
        value = m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        flags[name] = value
    return flags, skipped


@contextmanager
def _environ(values: dict[str, str]) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def build_settings(db_url: str, agent_id: str, overrides: dict[str, str]) -> Any:
    """Settings parsed with ``overrides`` in the environment, bound to ``db_url`` and ``agent_id``."""
    from nous.config import Settings

    p = urlparse(db_url.replace("postgresql+asyncpg://", "postgresql://"))
    if not (p.hostname and p.username and p.path.strip("/")):
        raise SystemExit("--db-url must look like postgresql+asyncpg://user:password@host:port/dbname")
    with _environ(overrides):
        base = Settings()
    return base.model_copy(
        update={
            "db_host": p.hostname,
            "db_port": p.port or 5432,
            "db_user": p.username,
            "db_password": p.password or "",
            "db_name": p.path.strip("/"),
            "agent_id": agent_id,
        }
    )


def settings_snapshot(settings: Any, overrides: dict[str, str]) -> dict[str, Any]:
    """Allowlisted settings values; a name this code lacks is recorded as ``"absent"``."""
    fields = type(settings).model_fields
    by_env = env_name_to_field()
    names = list(SNAPSHOT_FIELDS) + [by_env[n] for n in overrides if by_env[n] not in SNAPSHOT_FIELDS]
    return {n: (getattr(settings, n) if n in fields else "absent") for n in names if not _SECRET_RE.search(n)}


def code_revision() -> dict[str, Any]:
    """Git revision of the imported ``nous`` package (the code under test)."""
    import nous

    root = Path(nous.__file__).resolve().parent.parent

    def git(*args: str) -> str:
        try:
            return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True).stdout.strip()
        except OSError:
            return ""

    dirty = git("status", "--porcelain", "--", "nous", "nous_eval")
    return {
        "sha": git("rev-parse", "HEAD") or "unknown",
        "branch": git("rev-parse", "--abbrev-ref", "HEAD") or "unknown",
        "dirty_files": len(dirty.splitlines()) if dirty else 0,
    }


def check_out_dir(path: Path) -> None:
    """Refuse an output directory that git would track: eval outputs never enter the repo."""
    probe = path.resolve()
    while not probe.exists():
        probe = probe.parent
    top = subprocess.run(["git", "-C", str(probe), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode != 0:
        return  # not inside a git work tree
    ignored = subprocess.run(["git", "-C", top.stdout.strip(), "check-ignore", "-q", str(path.resolve())])
    if ignored.returncode != 0:
        raise SystemExit(f"--out-dir {path} is inside a git work tree and not ignored; use a path outside the repo")
