"""Backfill descriptions for procedures broken by the pre-fix SkillParser.

The old _parse_frontmatter captured the YAML block-scalar indicator ("|"/">") as the
literal description and dropped the body (audit goal 1b). These rows are heavily used
(deep-researcher act 656, investigate act 244) but show a useless one-char description in
the F079 catalog. The parser is fixed going forward; these existing rows need a one-time
data fix (re-import isn't possible — sources are gstack-ported / inline). Descriptions are
synthesized from each row's own goals / core_patterns / implementation_notes.

Idempotent: only updates rows whose description is still a block-scalar marker, so a re-run
(or a later real description) is untouched. Connects via DB_* env vars.
Modes: --dry-run (default) / --commit.
"""
from __future__ import annotations

import argparse
import asyncio
import os

from sqlalchemy import text

from nous.config import Settings
from nous.storage.database import Database

AGENT = os.environ.get("PROC_CONSOLIDATE_AGENT", "nous-default")

# id -> (name, synthesized description). Descriptions distilled from the live bodies
# (scripts/diag/_proc_bodies.json), matching each skill's goals/patterns.
BACKFILL: dict[str, tuple[str, str]] = {
    "f6515b17-5a8a-4696-a425-3714dddcffdc": (
        "deep-researcher",
        "Conduct rigorous multi-source research: evaluate source credibility, triangulate "
        "claims across 3+ independent sources, separate established fact from speculation, "
        "and deliver structured reports with explicit confidence ratings.",
    ),
    "84a25199-a70f-4a26-a022-57f670e1feed": (
        "investigate",
        "Root-cause debugging: investigate errors, failures, and regressions to find the "
        "underlying cause before applying any fix — no symptom patching.",
    ),
    "de9ff940-6e8c-4ffa-b292-3f519ca3ded2": (
        "cso",
        "Security audit and threat modeling: OWASP review, vulnerability and supply-chain "
        "scanning with confidence-gated findings (daily vs comprehensive scope modes).",
    ),
    "87dceb55-8d5d-406f-ae94-0103899ed2d3": (
        "office-hours",
        "Brainstorming / product 'office hours': think through whether and how to build an "
        "idea, choosing startup vs builder mode from the stated goal.",
    ),
}

# Block-scalar markers the broken parser left behind.
_MARKERS = ("|", ">", "|-", ">-", "|+", ">+")


async def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill broken procedure descriptions")
    ap.add_argument("--commit", action="store_true", help="COMMIT (default dry-run)")
    args = ap.parse_args()
    mode = "COMMIT" if args.commit else "DRY-RUN"

    db = Database(Settings())
    await db.connect()
    try:
        async with db.engine.connect() as conn:
            trans = await conn.begin()
            updated = 0
            for pid, (name, desc) in BACKFILL.items():
                row = (
                    await conn.execute(
                        text("SELECT name, description FROM heart.procedures WHERE id=:i AND agent_id=:a"),
                        {"i": pid, "a": AGENT},
                    )
                ).mappings().first()
                if row is None:
                    print(f"  SKIP {name}: not found")
                    continue
                cur = (row["description"] or "").strip()
                if cur not in _MARKERS:
                    print(f"  SKIP {name}: description not a block-scalar marker ({cur!r:.40})")
                    continue
                print(f"  FIX  {name}: {cur!r} -> {desc[:60]!r}…")
                res = await conn.execute(
                    text(
                        "UPDATE heart.procedures SET description=:d, updated_at=now() "
                        "WHERE id=:i AND agent_id=:a AND trim(description) = ANY(:markers)"
                    ),
                    {"d": desc, "i": pid, "a": AGENT, "markers": list(_MARKERS)},
                )
                updated += res.rowcount or 0
            print(f"[{mode}] {updated} row(s) would be updated" if not args.commit else f"[COMMIT] {updated} row(s) updated")
            if args.commit:
                await trans.commit()
            else:
                await trans.rollback()
    finally:
        await db.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
