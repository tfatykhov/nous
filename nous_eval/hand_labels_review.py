"""Interactive review tool for hand-label drafts.

Walks through ``qrels_hand_labels.jsonl`` one row at a time, runs
``hybrid_search`` over the scratch eval DB to produce candidate gold IDs,
lets the operator pick a subset by number, and writes the reviewed JSONL
back atomically.

Usage:
    NOUS_EVAL_FIXTURES_DIR=E:/Projects/nous-eval-fixtures/v2026-Q2 \
    DB_HOST=192.168.1.141 DB_USER=nous DB_PASSWORD=nous_dev_password \
    NOUS_EVAL_DB_HOST=127.0.0.1 NOUS_EVAL_DB_NAME=nous_eval_scratch \
    OPENAI_API_KEY=... \
    uv run python -m nous_eval.hand_labels_review

Resume-able: skips rows with non-null ``reviewed_by``. Re-running picks up
where you left off. Atomic write via .tmp + rename so an interrupted
session never leaves a half-written file.

NOT a prod-runtime tool — lives in ``nous_eval/`` and never ships in the
``nous/`` Docker image.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# Per-table search candidate count. 5 each * 5 types = 25 candidates max,
# trimmed to TOP_N_DISPLAY for the operator's screen.
PER_TABLE_LIMIT = 8
TOP_N_DISPLAY = 20
SNIPPET_LEN = 90

_TABLES = (
    ("heart.facts", "fact", "content"),
    ("brain.decisions", "decision", "description"),
    ("heart.episodes", "episode", "summary"),
    ("heart.procedures", "procedure", "name"),
    ("heart.censors", "censor", "pattern"),
)


@dataclass
class Candidate:
    """One row surfaced by hybrid_search for the reviewer to consider."""

    id: UUID
    type: str
    snippet: str
    score: float


def _detect_reviewer() -> str:
    """Default reviewer name from ``git config user.name`` so the operator
    doesn't type it on every row. Falls back to ``$USER`` then ``unknown``."""
    try:
        result = subprocess.run(
            ["git", "config", "user.name"],
            capture_output=True, text=True, check=True, timeout=2,
        )
        name = result.stdout.strip()
        if name:
            return name
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"


def _truncate(text: str | None, n: int = SNIPPET_LEN) -> str:
    if not text:
        return "<empty>"
    text = " ".join(text.split())  # collapse whitespace
    return text if len(text) <= n else text[: n - 1] + "…"


async def _search_candidates(
    query: str, embedding: list[float] | None, conn: Any, agent_id: str
) -> list[Candidate]:
    """Cross-table hybrid search. Returns up to ``5 * PER_TABLE_LIMIT`` rows.

    Uses keyword + vector together via the existing ``hybrid_search`` from
    ``nous/heart/search.py``. Each row gets its native ``score``; we merge
    by score DESC across tables and trim to TOP_N_DISPLAY before display.
    """
    from sqlalchemy import text as sql_text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    # Build a tiny SQLAlchemy async engine pointing at the same eval DB we
    # already opened via asyncpg. hybrid_search requires an AsyncSession.
    eval_settings_dsn = (
        f"postgresql+asyncpg://"
        f"{os.environ.get('NOUS_EVAL_DB_USER', 'nous')}:"
        f"{os.environ.get('NOUS_EVAL_DB_PASSWORD', 'nous_eval')}@"
        f"{os.environ.get('NOUS_EVAL_DB_HOST', '127.0.0.1')}:"
        f"{os.environ.get('NOUS_EVAL_DB_PORT', '5433')}/"
        f"{os.environ.get('NOUS_EVAL_DB_NAME', 'nous_eval_scratch')}"
    )
    engine = create_async_engine(eval_settings_dsn, pool_pre_ping=True)
    try:
        from nous.heart.search import hybrid_search

        candidates: list[Candidate] = []
        async with AsyncSession(engine) as session:
            for table, type_label, snippet_col in _TABLES:
                try:
                    rows = await hybrid_search(
                        session=session,
                        table=table,
                        embedding=embedding,
                        query_text=query,
                        agent_id=agent_id,
                        limit=PER_TABLE_LIMIT,
                    )
                except Exception as exc:
                    logger.debug("hybrid_search on %s failed: %s", table, exc)
                    continue
                if not rows:
                    continue
                # Fetch snippet content in a single query per table
                ids = [r[0] for r in rows]
                score_by_id = {r[0]: float(r[1]) for r in rows}
                snip_rows = await session.execute(
                    sql_text(
                        f"SELECT id, {snippet_col} FROM {table} "
                        f"WHERE id = ANY(:ids)"
                    ),
                    {"ids": ids},
                )
                for rid, snip in snip_rows.all():
                    candidates.append(Candidate(
                        id=rid,
                        type=type_label,
                        snippet=_truncate(snip),
                        score=score_by_id.get(rid, 0.0),
                    ))
        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates[:TOP_N_DISPLAY]
    finally:
        await engine.dispose()


async def _embed_query(query: str) -> list[float] | None:
    """Embed via the same OpenAI client Nous uses. Returns None on failure
    (caller falls back to keyword-only ``hybrid_search``)."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        from nous.brain.embeddings import EmbeddingProvider

        provider = EmbeddingProvider(api_key=api_key)
        try:
            return await provider.embed(query)
        finally:
            await provider.close()
    except Exception as exc:
        logger.warning("Embedding failed (falling back to keyword-only): %s", exc)
        return None


def _atomic_write_jsonl(path: Path, rows: list[dict]) -> None:
    """Write JSONL via .tmp + rename so an interrupted run never produces
    a half-written file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _print_candidates(query: str, rationale: str, cands: list[Candidate]) -> None:
    print()
    print("=" * 78)
    print(f"QUERY:     {query}")
    if rationale:
        print(f"RATIONALE: {_truncate(rationale, 150)}")
    print(f"\nTop {len(cands)} candidates from hybrid_search:")
    if not cands:
        print("  <no results>")
        return
    for i, c in enumerate(cands, start=1):
        print(f"  [{i:>2}] {c.type:<10} {str(c.id)[:8]} (score={c.score:.3f}) — {c.snippet}")


def _parse_picks(raw: str, n_cands: int) -> list[int] | str:
    """Parse 'gold picks' input.

    Returns:
      - list[int] of 1-based indices the operator marked as gold
      - 's' / 'r' / 'q' / 'k' as a control char string
      - empty list [] for "approve with zero gold" (rare but valid)

    Raises ValueError on a malformed numeric pick (out of range / non-int).
    """
    raw = raw.strip().lower()
    if raw in ("s", "r", "q", "k", ""):
        return raw or "k"  # bare enter = keep going with no gold
    indices = []
    for tok in raw.replace(" ", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if not tok.isdigit():
            raise ValueError(f"not a number: {tok!r}")
        i = int(tok)
        if not 1 <= i <= n_cands:
            raise ValueError(f"index {i} out of range [1, {n_cands}]")
        indices.append(i)
    return indices


async def _review_one(
    row: dict,
    reviewer: str,
    agent_id: str,
) -> dict | None:
    """Interactive review of a single draft row.

    Returns:
      - The updated row (with gold_ids + reviewed_by populated) on approve
      - None on skip (row is preserved as-is)
    Raises KeyboardInterrupt if operator hits Ctrl-C OR types 'q' to quit.
    """
    query = row["query"]
    rationale = ""
    notes = row.get("notes")
    if isinstance(notes, dict):
        rationale = notes.get("rationale", "")
    elif isinstance(notes, str):
        try:
            rationale = json.loads(notes).get("rationale", "")
        except (json.JSONDecodeError, AttributeError):
            rationale = notes

    embedding = await _embed_query(query)
    cands = await _search_candidates(query, embedding, None, agent_id)
    _print_candidates(query, rationale, cands)

    while True:
        try:
            raw = input(
                f"\n  Pick gold (1,3,5  |  s=skip  |  r=rewrite  |  q=quit  |  k=keep no-gold): "
            )
        except EOFError:
            raise KeyboardInterrupt
        try:
            picks = _parse_picks(raw, len(cands))
        except ValueError as exc:
            print(f"  ! {exc}; try again.")
            continue

        if picks == "q":
            raise KeyboardInterrupt
        if picks == "s":
            print("  → skipping (row left unreviewed)")
            return None
        if picks == "r":
            new_q = input("  New query text: ").strip()
            if not new_q:
                print("  → empty; canceling rewrite, try again.")
                continue
            row["query"] = new_q
            print("  → rewritten; re-running search…")
            return await _review_one(row, reviewer, agent_id)
        if picks == "k":
            print("  → keeping with no gold (still marked reviewed)")
            row["gold_ids"] = []
            row["reviewed_by"] = reviewer
            return row

        # numeric picks
        chosen_ids = [str(cands[i - 1].id) for i in picks]
        row["gold_ids"] = chosen_ids
        row["reviewed_by"] = reviewer
        print(f"  ✓ marked {len(chosen_ids)} gold; reviewed_by={reviewer}")
        return row


async def _async_main(args: Any) -> int:
    fixtures_dir = (
        args.fixtures_dir
        or os.environ.get("NOUS_EVAL_FIXTURES_DIR")
    )
    if not fixtures_dir:
        print(
            "ERROR: --fixtures-dir or NOUS_EVAL_FIXTURES_DIR must be set",
            file=sys.stderr,
        )
        return 2
    in_path = Path(fixtures_dir) / "qrels_hand_labels.jsonl"
    if not in_path.exists():
        print(f"ERROR: {in_path} not found", file=sys.stderr)
        return 2

    rows = [
        json.loads(line)
        for line in in_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    n_total = len(rows)
    n_reviewed_before = sum(1 for r in rows if r.get("reviewed_by"))
    n_unreviewed = n_total - n_reviewed_before
    if n_unreviewed == 0:
        print(f"All {n_total} rows already reviewed. Nothing to do.")
        return 0

    reviewer = args.reviewer or _detect_reviewer()
    agent_id = args.agent_id or os.environ.get("NOUS_EVAL_AGENT_ID", "nous-eval-corpus")
    print(
        f"\nHand-label review — {n_unreviewed}/{n_total} rows pending\n"
        f"  reviewer:  {reviewer}\n"
        f"  agent_id:  {agent_id}\n"
        f"  file:      {in_path}\n"
        f"\nControls: pick numbers (1,3,5) | s=skip | r=rewrite query | q=quit | k=keep no-gold"
    )

    n_done = n_reviewed_before
    n_with_gold = sum(1 for r in rows if r.get("gold_ids"))
    try:
        for i, row in enumerate(rows, start=1):
            if row.get("reviewed_by"):
                continue  # resume: skip already-reviewed
            print(f"\n\n>>> Reviewing {i}/{n_total} <<<")
            updated = await _review_one(row, reviewer, agent_id)
            if updated is not None:
                rows[i - 1] = updated
                n_done += 1
                if updated["gold_ids"]:
                    n_with_gold += 1
            # Persist after every row so a crash doesn't lose work
            _atomic_write_jsonl(in_path, rows)
    except KeyboardInterrupt:
        print("\n\nInterrupted — progress saved.")

    print(
        f"\n=== Review session complete ===\n"
        f"  reviewed:    {n_done}/{n_total}\n"
        f"  with gold:   {n_with_gold}\n"
        f"  file:        {in_path}\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(prog="python -m nous_eval.hand_labels_review")
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=None,
        help="Override NOUS_EVAL_FIXTURES_DIR.",
    )
    parser.add_argument(
        "--reviewer",
        type=str,
        default=None,
        help="Reviewer name (defaults to git config user.name).",
    )
    parser.add_argument(
        "--agent-id",
        type=str,
        default=None,
        help="agent_id of the corpus loaded in the eval DB (default nous-eval-corpus).",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
