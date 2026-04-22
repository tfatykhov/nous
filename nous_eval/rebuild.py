"""``python -m nous_eval.rebuild`` — drop the eval-DB volume and rebuild.

Thin dispatcher into :func:`nous_eval.tasks._rebuild`. The actual command
logic lives in ``tasks.py`` (owned by the Infra subagent). This module
exists so operators can do ``python -m nous_eval.rebuild`` without
remembering the longer ``python -m nous_eval.tasks rebuild`` form.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Delegate to ``nous_eval.tasks main`` with the ``rebuild`` subcommand.

    Imported lazily so this module loads cleanly even before the Infra
    subagent's ``tasks.py`` lands — a missing tasks module produces a
    clear error rather than a top-level ImportError.

    ``tasks.main`` builds the argparse subparser and dispatches to
    ``_rebuild`` with a properly-shaped ``argparse.Namespace``.
    """
    try:
        from nous_eval.tasks import main as tasks_main
    except ImportError as exc:
        print(
            f"ERROR: nous_eval.tasks not available ({exc}). "
            f"Infra subagent must land tasks.py before rebuild works.",
            file=sys.stderr,
        )
        return 1
    return tasks_main(["rebuild", *(argv or [])])


if __name__ == "__main__":
    raise SystemExit(main())
