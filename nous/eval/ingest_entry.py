"""``python -m nous.eval.ingest_entry`` — operator-run corpus ingest dispatcher.

Thin wrapper around :func:`nous.eval.ingest.run`. The actual ingest logic
lives in ``ingest.py`` (owned by the Infra subagent and invoked quarterly
against the prod Nous DB via SSH tunnel).
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Delegate to :func:`nous.eval.ingest.main`.

    The actual ingest pipeline (``ingest.run``) is async and takes an
    ``IngestConfig``. ``ingest.main`` is the synchronous CLI wrapper that
    parses argv into a config and runs the coroutine — that's what we
    delegate to here.
    """
    try:
        from nous.eval.ingest import main as ingest_main
    except ImportError as exc:
        print(
            f"ERROR: nous.eval.ingest not available ({exc}). "
            f"Infra subagent must land ingest.py before this entry works.",
            file=sys.stderr,
        )
        return 1
    return ingest_main(argv or [])


if __name__ == "__main__":
    raise SystemExit(main())
