"""F078 gated triage — restore the few hard-tier censors after migration 056.

Migration 056 maps every existing censor to advisory `steer` (functionally INERT,
so a deploy never auto-blocks). This operator-run script restores the genuinely
prohibitive tiers and retires obvious dead/junk censors. It is the "gated triage
mechanism" referenced by F078 / docs/reviews/censor-triage-2026-06-05.md.

  PROMOTE -> abort   : destructive (rm -rf)
  PROMOTE -> refuse  : trading safety (never lower exit_threshold to flush an
                       underwater position, never sell/liquidate underwater) +
                       the autopilot record_decision noise filter
  RETIRE  (active=false): never-activated auto-prose censors + test rows

DRY-RUN BY DEFAULT. Reversible: retire sets active=false (never deletes); tier
changes are plain UPDATEs. Idempotent: only rows not already at target change.

Run against prod by pointing DB_* env at the prod DB, then:
  uv run python scripts/migrate_censors_f078.py --agent nous-default            # dry-run
  uv run python scripts/migrate_censors_f078.py --agent nous-default --commit   # apply

Email censors are intentionally LEFT as steer (BC invariant — daily email
senders must keep working; recipient-allowlist hardening is F078.1).
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from nous.config import Settings  # noqa: E402
from nous.storage.database import Database  # noqa: E402

# Operator-reviewed promotions, matched case-insensitively against trigger_pattern.
# abort wins over refuse if both match (more restrictive).
PROMOTE_ABORT = [r"rm\s+-rf"]
PROMOTE_REFUSE = [
    r"exit_threshold",
    r"sell.*underwater",
    r"liquidate.*underwater",
    r"SOL.*HOLD",          # autopilot record_decision noise filter
    r"autopilot.*tick.*triage",
]


def _matches(patterns: list[str], trigger: str) -> bool:
    return any(re.search(p, trigger or "", re.IGNORECASE) for p in patterns)


def _target(action: str, trigger: str, reason: str, activation_count: int,
            provenance: str) -> tuple[str | None, bool, str]:
    """Return (new_action_or_None, retire, why). new_action None = keep current."""
    # Retire obvious junk first.
    if (reason or "").strip().lower() == "test" or (trigger or "").strip() == "test pattern":
        return None, True, "test row"
    if activation_count == 0 and provenance == "auto":
        return None, True, "never-activated auto-prose"
    # Promotions.
    if _matches(PROMOTE_ABORT, trigger):
        return ("abort" if action != "abort" else None), False, "destructive -> abort"
    if _matches(PROMOTE_REFUSE, trigger):
        return ("refuse" if action != "refuse" else None), False, "prohibitive -> refuse"
    return None, False, "keep steer (advisory)"


async def main() -> None:
    ap = argparse.ArgumentParser(description="F078 gated censor triage")
    ap.add_argument("--agent", default="nous-default")
    ap.add_argument("--commit", action="store_true", help="apply changes (default: dry-run)")
    args = ap.parse_args()

    db = Database(Settings())
    await db.connect()
    try:
        async with db.session() as s:
            rows = (await s.execute(text(
                "SELECT id::text, action, trigger_pattern, coalesce(reason,'') AS reason, "
                "coalesce(activation_count,0) AS ac, coalesce(provenance,'human') AS prov "
                "FROM heart.censors WHERE agent_id=:a AND active ORDER BY ac DESC"
            ), {"a": args.agent})).mappings().all()

            promote, retire, keep = [], [], 0
            for r in rows:
                new_action, do_retire, why = _target(
                    r["action"], r["trigger_pattern"], r["reason"], r["ac"], r["prov"])
                if do_retire:
                    retire.append((r, why))
                elif new_action:
                    promote.append((r, new_action, why))
                else:
                    keep += 1

            mode = "COMMIT" if args.commit else "DRY-RUN"
            print(f"\n=== F078 triage [{mode}] agent={args.agent}  active={len(rows)} ===")
            print(f"\nPROMOTE ({len(promote)}):")
            for r, na, why in promote:
                print(f"  {r['action']:6} -> {na:6}  [{why}]  {r['trigger_pattern'][:46]}")
            print(f"\nRETIRE ({len(retire)}):")
            for r, why in retire:
                print(f"  active=false  [{why}]  {r['trigger_pattern'][:46]}")
            print(f"\nKEEP as steer (advisory): {keep}")

            if not args.commit:
                print("\n(dry-run — re-run with --commit to apply; reversible)")
                return

            for r, na, _why in promote:
                await s.execute(text("UPDATE heart.censors SET action=:na, updated_at=now() WHERE id=:id"),
                                {"na": na, "id": r["id"]})
            for r, _why in retire:
                await s.execute(text("UPDATE heart.censors SET active=false, updated_at=now() WHERE id=:id"),
                                {"id": r["id"]})
            await s.commit()
            print(f"\nAPPLIED: promoted {len(promote)}, retired {len(retire)}.")
    finally:
        await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
