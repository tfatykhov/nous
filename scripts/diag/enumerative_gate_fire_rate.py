"""How often the enumerative extractor's density gate fires, old rule vs new.

The old gate (b43c46a8) counted a line as a hit when it was a list item or any
10-180 char line ending in "." or ";", so ordinary chat prose fired it. The new
gate is ``nous.handlers.enumerative_extractor.is_enumerable``. This script runs
both over a set of texts and prints counts and rates only, never any text.

    # local corpora, one text per file
    python scripts/diag/enumerative_gate_fire_rate.py --files "corpus/**/*.txt"

    # the configured agent's last N closed episode transcripts, read-only
    # (connection and agent from Settings: DB_HOST, DB_PORT, DB_USER,
    # DB_PASSWORD, DB_NAME, NOUS_AGENT_ID)
    python scripts/diag/enumerative_gate_fire_rate.py --db --limit 500

The threshold defaults to Settings().enumerative_density_threshold.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nous.config import Settings  # noqa: E402
from nous.handlers.enumerative_extractor import is_enumerable  # noqa: E402

# The pre-change gate, frozen here so the comparison survives later edits.
_OLD_LIST_MARKER = re.compile(r"^\s*(?:[-*•]|\d{1,5}[.):])\s+")
_OLD_STATEMENT_LINE = re.compile(r"^.{10,180}[.;]\s*$")


def old_density_score(text: str) -> float:
    """density_score as of b43c46a8."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 5:
        return 0.0
    hits = sum(1 for ln in lines if _OLD_LIST_MARKER.match(ln) or _OLD_STATEMENT_LINE.match(ln))
    return hits / len(lines)


def old_is_enumerable(text: str, threshold: float) -> bool:
    return old_density_score(text) >= threshold


def file_texts(pattern: str) -> list[str]:
    paths = sorted(p for p in glob.glob(pattern, recursive=True) if Path(p).is_file())
    return [Path(p).read_text(encoding="utf-8", errors="replace") for p in paths]


async def db_texts(settings: Settings, limit: int) -> list[str]:
    """Transcripts of the agent's last ``limit`` closed episodes. The session
    is read-only, so nothing this script runs can write."""
    import asyncpg

    conn = await asyncpg.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
        server_settings={"default_transaction_read_only": "on"},
    )
    try:
        rows = await conn.fetch(
            """
            SELECT transcript FROM heart.episodes
            WHERE agent_id = $1 AND ended_at IS NOT NULL
              AND transcript IS NOT NULL AND transcript <> ''
            ORDER BY ended_at DESC
            LIMIT $2
            """,
            settings.agent_id,
            limit,
        )
    finally:
        await conn.close()
    return [r["transcript"] for r in rows]


def _rate(n: int, total: int) -> str:
    return f"{n} / {total} ({100.0 * n / total:.1f}%)" if total else f"{n} / 0"


def report(texts: list[str], threshold: float, source: str) -> None:
    both = old_only = new_only = neither = 0
    for text in texts:
        old = old_is_enumerable(text, threshold)
        new = is_enumerable(text, threshold)
        if old and new:
            both += 1
        elif old:
            old_only += 1
        elif new:
            new_only += 1
        else:
            neither += 1
    total = len(texts)
    print(f"source: {source}  texts: {total}  threshold: {threshold}")
    print(f"old gate fires: {_rate(both + old_only, total)}")
    print(f"new gate fires: {_rate(both + new_only, total)}")
    print(f"both: {both}  old only: {old_only}  new only: {new_only}  neither: {neither}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--files", metavar="GLOB", help="one text per matching file (** recurses)")
    mode.add_argument("--db", action="store_true", help="closed episode transcripts of the configured agent")
    parser.add_argument("--limit", type=int, default=500, help="--db: how many recent episodes (default 500)")
    parser.add_argument(
        "--threshold", type=float, default=None, help="default: Settings().enumerative_density_threshold"
    )
    args = parser.parse_args()

    settings = Settings()
    threshold = args.threshold if args.threshold is not None else settings.enumerative_density_threshold
    if args.files:
        report(file_texts(args.files), threshold, "files")
    else:
        texts = asyncio.run(db_texts(settings, args.limit))
        report(texts, threshold, f"db agent={settings.agent_id} last {args.limit} closed episodes")


if __name__ == "__main__":
    main()
