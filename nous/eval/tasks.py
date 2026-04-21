"""F051 eval-harness operator task runner.

One-stop CLI dispatcher for the offline fixture pipeline:

    python -m nous.eval.tasks <subcommand> [args...]

Subcommands (all wrap subprocess calls with ``check=True`` so errors propagate
as non-zero exit codes, and ``capture_output=True`` so stdout/stderr surface in
test harnesses):

    build-image          Build the Dockerfile.eval-db image (needs staging dir).
    push-image           Push the built image to GHCR.
    serve-eval-db        docker compose up -d nous-eval-db (profile: eval).
    stop-eval-db         docker compose stop nous-eval-db.
    rebuild              Targeted volume purge + restart (v2.1: no -v on down).
    ingest               Replay prod corpus into scratch eval DB + dump JSONL.
    probe-gen            Deterministic qrels from INDEX.md + `git log`.
    hand-labels-draft    AI-drafted qrels (unreviewed; human must approve).
    longmemeval-subset   Download + stratify LongMemEval_S into N=20 qrels.

All subcommands are intentionally thin — they shell out to ``docker``, ``git``,
``uv``, or a sibling Python module. The delegated module is the source of truth
for behaviour; this file is the dispatch glue.
"""

from __future__ import annotations

import argparse
import logging
import shlex
import subprocess
import sys
from collections.abc import Sequence

logger = logging.getLogger(__name__)

# Kept in one place so every subcommand resolves the same default.
DEFAULT_FIXTURE_VERSION = "v2026-Q2"
DEFAULT_IMAGE_NAME = "ghcr.io/tfatykhov/nous-eval-db"
DEFAULT_VOLUME_NAME = "nous_eval_db_data"
COMPOSE_SERVICE_NAME = "nous-eval-db"
COMPOSE_PROFILE = "eval"


# ---------------------------------------------------------------------------
# subprocess helpers
# ---------------------------------------------------------------------------


def _run(argv: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` with capture + text mode.

    Prints the command before running (to stderr) so the operator can copy-paste
    it if the subcommand explodes. Returns the completed process so callers can
    grep stdout if needed.
    """
    pretty = " ".join(shlex.quote(a) for a in argv)
    print(f"[eval.tasks] $ {pretty}", file=sys.stderr, flush=True)
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        check=check,
    )


def _run_passthrough(argv: Sequence[str]) -> int:
    """Variant of ``_run`` that streams output directly (no capture).

    Used for long-running commands (`docker build`, `docker compose up`) where
    capturing stdout would leave the operator staring at a blank terminal for
    minutes. Returns the child's exit code unmodified.
    """
    pretty = " ".join(shlex.quote(a) for a in argv)
    print(f"[eval.tasks] $ {pretty}", file=sys.stderr, flush=True)
    return subprocess.run(list(argv), check=False).returncode


# ---------------------------------------------------------------------------
# subcommand implementations
# ---------------------------------------------------------------------------


def _build_image(args: argparse.Namespace) -> int:
    """Build Dockerfile.eval-db -> ``<image>:<version>``.

    Requires ``nous-eval-fixtures-staging/`` to already exist (populated by an
    earlier ``ingest`` run). Fails cleanly with exit 2 if that dir is missing.
    """
    from pathlib import Path

    staging = Path("nous-eval-fixtures-staging")
    if not staging.is_dir():
        print(
            f"[eval.tasks] ERROR: {staging}/ missing. "
            "Run `python -m nous.eval.tasks ingest` first.",
            file=sys.stderr,
        )
        return 2

    tag = f"{args.image}:{args.version}"
    return _run_passthrough(
        [
            "docker",
            "build",
            "-f",
            "Dockerfile.eval-db",
            "--build-arg",
            f"FIXTURE_VERSION={args.version}",
            "-t",
            tag,
            ".",
        ]
    )


def _push_image(args: argparse.Namespace) -> int:
    """Push ``<image>:<version>`` to the configured registry.

    Assumes the operator has already run ``docker login`` for the registry.
    """
    tag = f"{args.image}:{args.version}"
    return _run_passthrough(["docker", "push", tag])


def _serve_eval_db(args: argparse.Namespace) -> int:
    """``docker compose --profile eval up -d nous-eval-db``.

    Side effect: pulls the image if not cached locally.
    """
    return _run_passthrough(
        [
            "docker",
            "compose",
            "--profile",
            COMPOSE_PROFILE,
            "up",
            "-d",
            COMPOSE_SERVICE_NAME,
        ]
    )


def _stop_eval_db(args: argparse.Namespace) -> int:
    """``docker compose stop nous-eval-db`` — leaves the volume + image intact."""
    return _run_passthrough(
        [
            "docker",
            "compose",
            "--profile",
            COMPOSE_PROFILE,
            "stop",
            COMPOSE_SERVICE_NAME,
        ]
    )


def _rebuild(args: argparse.Namespace) -> int:
    """Targeted volume + container purge, then re-up.

    Plan v2.1 finding #1: MUST NOT use ``docker compose down -v`` — on some
    Compose versions that would sweep sibling volumes (``pgdata``,
    ``huggingface_cache``). We stop + rm the specific service, then
    ``docker volume rm -f`` only the eval volume by name.

    Each step uses ``check=False`` so a missing container / volume (normal on
    first run) does not abort the pipeline; only the final ``up -d`` is
    ``check=True`` to surface real failures.
    """
    subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            COMPOSE_PROFILE,
            "stop",
            COMPOSE_SERVICE_NAME,
        ],
        check=False,
    )
    subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            COMPOSE_PROFILE,
            "rm",
            "-f",
            COMPOSE_SERVICE_NAME,
        ],
        check=False,
    )
    subprocess.run(
        ["docker", "volume", "rm", "-f", DEFAULT_VOLUME_NAME],
        check=False,
    )
    subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            COMPOSE_PROFILE,
            "up",
            "-d",
            COMPOSE_SERVICE_NAME,
        ],
        check=True,
    )
    return 0


def _ingest(args: argparse.Namespace) -> int:
    """Delegate to :mod:`nous.eval.ingest` (operator-run).

    Kept as a subprocess call rather than an in-process import so the ingest
    pipeline can set its own environment (e.g. ``NOUS_EVENT_BUS_ENABLED=false``)
    without leaking into the tasks.py process.
    """
    return _run_passthrough([sys.executable, "-m", "nous.eval.ingest", *args.extra])


def _probe_gen(args: argparse.Namespace) -> int:
    """Delegate to :mod:`nous.eval.probe_gen`."""
    return _run_passthrough([sys.executable, "-m", "nous.eval.probe_gen", *args.extra])


def _hand_labels_draft(args: argparse.Namespace) -> int:
    """Delegate to :mod:`nous.eval.hand_labels_draft`."""
    return _run_passthrough(
        [sys.executable, "-m", "nous.eval.hand_labels_draft", *args.extra]
    )


def _longmemeval_subset(args: argparse.Namespace) -> int:
    """Delegate to :mod:`nous.eval.ingest_longmemeval`."""
    return _run_passthrough(
        [sys.executable, "-m", "nous.eval.ingest_longmemeval", *args.extra]
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m nous.eval.tasks",
        description="F051 retrieval-eval harness operator task runner",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # build-image
    p_build = sub.add_parser("build-image", help="Build Dockerfile.eval-db")
    p_build.add_argument("--image", default=DEFAULT_IMAGE_NAME)
    p_build.add_argument("--version", default=DEFAULT_FIXTURE_VERSION)
    p_build.set_defaults(func=_build_image)

    # push-image
    p_push = sub.add_parser("push-image", help="docker push <image>:<version>")
    p_push.add_argument("--image", default=DEFAULT_IMAGE_NAME)
    p_push.add_argument("--version", default=DEFAULT_FIXTURE_VERSION)
    p_push.set_defaults(func=_push_image)

    # serve-eval-db / stop-eval-db / rebuild
    sub.add_parser("serve-eval-db", help="docker compose up -d nous-eval-db").set_defaults(
        func=_serve_eval_db
    )
    sub.add_parser("stop-eval-db", help="docker compose stop nous-eval-db").set_defaults(
        func=_stop_eval_db
    )
    sub.add_parser(
        "rebuild",
        help="Purge eval volume + restart service (targeted, no -v on down)",
    ).set_defaults(func=_rebuild)

    # Delegated subcommands — everything after the subcommand name is
    # forwarded verbatim to the delegated module via ``args.extra``.
    for name, func, helptext in [
        ("ingest", _ingest, "Replay prod corpus -> JSONL staging dir"),
        ("probe-gen", _probe_gen, "Generate deterministic qrels from INDEX.md + git log"),
        (
            "hand-labels-draft",
            _hand_labels_draft,
            "AI-draft qrels for human review (reviewed_by: null)",
        ),
        (
            "longmemeval-subset",
            _longmemeval_subset,
            "Download + stratify LongMemEval_S (N=20, 6 reasoning types)",
        ),
    ]:
        p = sub.add_parser(name, help=helptext)
        p.add_argument("extra", nargs=argparse.REMAINDER, help="passed through")
        p.set_defaults(func=func)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
