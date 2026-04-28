"""F056: generic JSONL fixture loader for handler evals.

Each handler ships its own pydantic row model; this helper reads JSONL
+ pydantic-validates each row + returns a typed list. Per F056 spec
§"_jsonl raises-on-error policy", schema violations RAISE — silent
skip-with-warn is the F051.5 999/1000 admission bug pattern.

`reviewed_by` filtering is a separate post-load gate (per-handler), not
a parse-time concern. The loader treats `reviewed_by=None` rows as valid;
callers apply the `--include-unreviewed` filter before metric computation.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def load_jsonl(path: Path, model_cls: type[T]) -> list[T]:
    """Load a JSONL file into a list of validated pydantic models.

    Lines that are blank or start with `#` are skipped (project-specific
    extension to standard JSONL — useful for fixture commentary).

    Raises:
        FileNotFoundError: when `path` does not exist.
        pydantic.ValidationError: on the FIRST row that fails schema validation
            (re-raised as-is; the wrapping `ValueError` adds file/line context
            and chains to the original via `__cause__`).
            Caller should treat this as a hard precondition violation — do not
            silently shrink the corpus.
        json.JSONDecodeError: on malformed JSON.
    """
    if not path.exists():
        raise FileNotFoundError(f"Fixture not found: {path}")
    rows: list[T] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_num, raw in enumerate(fh, start=1):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise json.JSONDecodeError(
                    f"{path}:{line_num}: {exc.msg}", exc.doc, exc.pos,
                ) from exc
            try:
                rows.append(model_cls.model_validate(payload))
            except ValidationError:
                # Re-raise the original — pydantic's ValidationError formatting
                # already includes field/error detail. We don't reconstruct via
                # from_exception_data because its `line_errors` parameter
                # expects InitErrorDetails (internal API), not ErrorDetails
                # (what .errors() returns) — using the wrong shape crashes with
                # TypeError instead of cleanly propagating ValidationError.
                logger.error(
                    "load_jsonl: schema validation failed at %s:%d", path, line_num,
                )
                raise
    return rows
