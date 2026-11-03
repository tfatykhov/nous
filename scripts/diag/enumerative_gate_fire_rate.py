"""How often the enumerative extractor's density gate fires, old rule vs new.

The old gate (b43c46a8) counted a line as a hit when it was a list item or any
10-180 char line ending in "." or ";", so ordinary chat prose fired it. The new
gate counts list items, "key: value" lines and 10-80 char statements outside
Assistant turns, never a speaker-labelled line, and needs five hits. This
script runs both over a set of texts and prints counts and rates only, never
any text.

It is self-contained so it can run inside an image that still has the old
gate: from nous it imports only Settings, for the database connection.

    # the configured agent's last N closed episode transcripts, read-only
    # (connection and agent from Settings: DB_HOST, DB_PORT, DB_USER,
    # DB_PASSWORD, DB_NAME, NOUS_AGENT_ID). This is the default mode.
    python scripts/diag/enumerative_gate_fire_rate.py --limit 500
    docker exec -i nous-agent python - --limit 500 < scripts/diag/enumerative_gate_fire_rate.py

    # local corpora, one text per file
    python scripts/diag/enumerative_gate_fire_rate.py --files "corpus/**/*.txt"

The threshold defaults to Settings().enumerative_density_threshold.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import re
import sys
from pathlib import Path

# Run from a checkout, put its root on the path. Piped to `python -`, __file__
# is "<stdin>" and the installed nous package is used.
_HERE = Path(globals().get("__file__") or "<stdin>")
if _HERE.is_file():
    sys.path.insert(0, str(_HERE.resolve().parents[2]))

from nous.config import Settings  # noqa: E402

# ---------------------------------------------------------------------------
# Measurement copies of the two gate decisions. They are kept in sync with
# nous/handlers/enumerative_extractor.py by hand, so this script measures the
# same rules in an image that does not have the new gate yet. Delete this
# script once the new gate is rolled out.
# ---------------------------------------------------------------------------

# Old gate: density_score / is_enumerable as of b43c46a8.
_OLD_LIST_MARKER = re.compile(r"^\s*(?:[-*•]|\d{1,5}[.):])\s+")
_OLD_STATEMENT_LINE = re.compile(r"^.{10,180}[.;]\s*$")


def _old_density_score(text: str) -> float:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 5:
        return 0.0
    hits = sum(1 for ln in lines if _OLD_LIST_MARKER.match(ln) or _OLD_STATEMENT_LINE.match(ln))
    return hits / len(lines)


def _old_gate(text: str, threshold: float) -> bool:
    return _old_density_score(text) >= threshold


# New gate: density_score / is_enumerable on fix/profile-learning.
_LIST_MARKER = re.compile(r"^\s*(?:[-*•]|\d{1,5}[.):])\s+")
_KEY_VALUE = re.compile(r"^[^:]{1,40}:\s+\S")
_SHORT_STATEMENT = re.compile(r"^.{10,80}[.;]\s*$")
_SPEAKER_LABEL = re.compile(r"^(user|assistant|system|human):", re.IGNORECASE)
_MIN_HITS = 5


def _is_list_line(line: str) -> bool:
    if _SPEAKER_LABEL.match(line):
        return False
    return bool(_LIST_MARKER.match(line) or _KEY_VALUE.match(line) or _SHORT_STATEMENT.match(line))


def _lines_outside_assistant_turns(text: str) -> list[str]:
    kept: list[str] = []
    in_assistant = False
    turn_may_start = True
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            turn_may_start = True
            continue
        if turn_may_start:
            label = _SPEAKER_LABEL.match(line)
            if label:
                in_assistant = label.group(1).lower() == "assistant"
            turn_may_start = False
        if not in_assistant:
            kept.append(line)
    return kept


def _new_density_score(text: str) -> float:
    lines = _lines_outside_assistant_turns(text)
    hits = sum(1 for ln in lines if _is_list_line(ln))
    if hits < _MIN_HITS:
        return 0.0
    return hits / len(lines)


def _new_gate(text: str, threshold: float) -> bool:
    return _new_density_score(text) >= threshold


# ---------------------------------------------------------------------------


def file_texts(pattern: str) -> list[str]:
    paths = sorted(p for p in glob.glob(pattern, recursive=True) if Path(p).is_file())
    return [Path(p).read_text(encoding="utf-8", errors="replace") for p in paths]


async def db_texts(settings: Settings, limit: int) -> list[str]:
    """Transcripts of the agent's last ``limit`` closed episodes, read in a
    read-only session, so nothing this script runs can write."""
    import asyncpg

    conn = await asyncpg.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
    )
    try:
        await conn.execute("SET default_transaction_read_only = on")
        if await conn.fetchval("SHOW default_transaction_read_only") != "on":
            raise SystemExit("could not make the session read-only; nothing was read")
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
        old = _old_gate(text, threshold)
        new = _new_gate(text, threshold)
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
    parser.add_argument("--files", metavar="GLOB", help="one text per matching file (** recurses); default: --db")
    parser.add_argument("--db", action="store_true", help="closed episode transcripts of the configured agent")
    parser.add_argument("--limit", type=int, default=500, help="--db: how many recent episodes (default 500)")
    parser.add_argument(
        "--threshold", type=float, default=None, help="default: Settings().enumerative_density_threshold"
    )
    args = parser.parse_args()
    if args.files and args.db:
        parser.error("--files and --db are alternatives")

    settings = Settings()
    threshold = args.threshold if args.threshold is not None else settings.enumerative_density_threshold
    if args.files:
        report(file_texts(args.files), threshold, "files")
    else:
        texts = asyncio.run(db_texts(settings, args.limit))
        report(texts, threshold, f"db agent={settings.agent_id} last {args.limit} closed episodes")


if __name__ == "__main__":
    main()
