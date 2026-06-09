"""Re-import skill procedures from source to restore full bodies (lost to the
first-H2-section-only import bug, parser.py).

DRY-RUN (default): classify every active skill-tagged procedure by source
recoverability, re-fetch URL sources, and report the fidelity gain (full body vs
the currently-stored compressed body) — NO writes.

--commit: update recoverable procedures' bodies via heart.update_procedure_body
(preserves learned stats + re-embeds), and ARCHIVE (active=false, reversible) the
non-recoverable STUBS (source gone AND content below --stub-threshold).

Targets the DB in env (DB_*); validate on the eval snapshot before prod.

Run: uv run python scripts/diag/reimport_skills.py            # dry-run
     uv run python scripts/diag/reimport_skills.py --commit --stub-threshold 300
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# env: prod flags/keys but snapshot DB unless DB_* already set by caller
SNAP = Path(".env.prod-snapshot")
if SNAP.exists():
    for raw in SNAP.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        key = k.strip()
        if key.startswith("DB_"):
            continue  # snapshot's prod DB must not shadow the target below
        os.environ.setdefault(key, v.strip().strip('"').strip("'"))
# Target the eval snapshot by default; override by exporting DB_* before running.
os.environ.setdefault("DB_HOST", "127.0.0.1")
os.environ.setdefault("DB_PORT", "5433")
os.environ.setdefault("DB_NAME", "nous_eval_prod")
os.environ.setdefault("DB_USER", "nous")
os.environ.setdefault("DB_PASSWORD", "nous_eval")
os.environ["NOUS_AGENT_ID"] = "nous-default"
for k in ("NOUS_MCP_ENABLED", "NOUS_HEARTBEAT_ENABLED", "NOUS_SCHEDULE_ENABLED",
          "NOUS_EVENT_BUS_ENABLED"):
    os.environ[k] = "false"

from nous.config import Settings  # noqa: E402
from nous.storage.database import Database  # noqa: E402
from nous.brain.embeddings import EmbeddingProvider  # noqa: E402
from nous.heart.heart import Heart  # noqa: E402
from nous.skills.parser import SkillParser  # noqa: E402
from sqlalchemy import text  # noqa: E402


def _instr_len(notes: list[str]) -> int:
    return sum(len(n) for n in (notes or []) if not n.startswith(("source:", "version:")))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--stub-threshold", type=int, default=300,
                    help="source-gone procedures with instructional content below this are archived")
    args = ap.parse_args()

    s = Settings()
    db = Database(s)
    await db.connect()
    emb = EmbeddingProvider(api_key=s.openai_api_key, model=s.embedding_model,
                            dimensions=getattr(s, "embedding_dimensions", 1536))
    heart = Heart(db, s, emb, owns_embeddings=False)
    parser = SkillParser()

    async with db.session() as sess:
        rows = (await sess.execute(text("""
            SELECT id, name, domain, implementation_notes, tags
            FROM heart.procedures
            WHERE agent_id='nous-default' AND active AND 'skill'=ANY(tags)
            ORDER BY name
        """))).mappings().all()

    recover, gone_full, gone_stub = [], [], []
    print(f"{'PROCEDURE':34s} {'source':10s} {'cur':>5s} {'full':>6s}  action")
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
        for r in rows:
            notes = list(r["implementation_notes"] or [])
            src = notes[0].replace("source:", "") if notes and notes[0].startswith("source:") else ""
            cur = _instr_len(notes)
            if src.startswith("http"):
                try:
                    resp = await http.get(src)
                    resp.raise_for_status()
                    manifest = parser.parse(resp.text, source_hint=src)
                    pi = parser.to_procedure_input(manifest)
                    full = _instr_len(pi.implementation_notes)
                    recover.append((r, pi, cur, full))
                    print(f"{r['name'][:34]:34s} {'url':10s} {cur:5d} {full:6d}  RE-IMPORT (+{full-cur})")
                except Exception as e:
                    gone_full.append((r, cur))  # url unreachable -> treat as keep
                    print(f"{r['name'][:34]:34s} {'url-FAIL':10s} {cur:5d} {'?':>6s}  keep (fetch failed: {str(e)[:30]})")
            else:
                kind = "inline" if src == "inline" else ("local" if src in ("", "local") else "path-gone")
                if cur < args.stub_threshold:
                    gone_stub.append((r, cur, kind))
                    print(f"{r['name'][:34]:34s} {kind:10s} {cur:5d} {'—':>6s}  ARCHIVE (stub, source gone)")
                else:
                    gone_full.append((r, cur))
                    print(f"{r['name'][:34]:34s} {kind:10s} {cur:5d} {'—':>6s}  keep (source gone, but has content)")

    print("\n" + "=" * 70)
    print(f"  RECOVERABLE (re-import full body): {len(recover)}")
    print(f"  source-gone, KEEP (functional):    {len(gone_full)}")
    print(f"  NON-RECOVERABLE STUBS (archive):   {len(gone_stub)}")
    if gone_stub:
        print("\n  --- non-recoverable stubs (would be archived) ---")
        for r, cur, kind in gone_stub:
            print(f"    {r['name']}  [{kind}, {cur} ch]")

    if args.commit:
        print("\n[COMMIT] applying...")
        n_re = n_arch = 0
        for r, pi, cur, full in recover:
            await heart.update_procedure_body(r["id"], pi)
            n_re += 1
        async with db.session() as sess:
            for r, cur, kind in gone_stub:
                await sess.execute(text(
                    "UPDATE heart.procedures SET active=false, archived_at=now() WHERE id=:i"
                ), {"i": r["id"]})
            await sess.commit()
            n_arch = len(gone_stub)
        print(f"[COMMIT] re-imported {n_re} bodies, archived {n_arch} stubs.")
    else:
        print("\n  (dry-run — pass --commit to apply)")


if __name__ == "__main__":
    asyncio.run(main())
