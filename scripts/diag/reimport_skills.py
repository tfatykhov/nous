"""Re-import skill procedures from source to restore full bodies (lost to the
first-H2-section-only import bug, parser.py).

DRY-RUN (default): classify every active skill-tagged procedure by source
recoverability, re-fetch URL sources, and report the fidelity gain (full body vs
the currently-stored compressed body) — NO writes.

--commit: update recoverable procedures' bodies via heart.update_procedure_body
(preserves learned stats + re-embeds) ONLY when the re-fetched body is LONGER
than the stored one (never overwrite a good body with a shorter/stale fetch), and
ARCHIVE (active=false, reversible) source-gone STUBS whose total instructional
content (description + body) is below --stub-threshold.

Each skill is isolated (one failure doesn't abort the run); a summary always prints.
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


def _content_len(description: str | None, notes: list[str] | None) -> int:
    """Total instructional content = description + body (notes minus metadata)."""
    body = sum(len(n) for n in (notes or []) if not str(n).startswith(("source:", "version:")))
    return len(description or "") + body


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--stub-threshold", type=int, default=300,
                    help="source-gone procedures with total content below this are archived")
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
            SELECT id, name, domain, description, implementation_notes, tags
            FROM heart.procedures
            WHERE agent_id='nous-default' AND active AND 'skill'=ANY(tags)
            ORDER BY name
        """))).mappings().all()

    recover, gone_full, gone_stub, url_fail = [], [], [], []
    print(f"{'PROCEDURE':34s} {'source':12s} {'cur':>5s} {'full':>6s}  action")
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
        for r in rows:
            notes = list(r["implementation_notes"] or [])
            src = notes[0][len("source:"):] if notes and notes[0].startswith("source:") else ""
            cur = _content_len(r["description"], notes)
            if src.startswith("http"):
                try:
                    resp = await http.get(src)
                    if resp.status_code == 404:
                        bucket = gone_stub if cur < args.stub_threshold else gone_full
                        bucket.append((r, cur, "url-404"))
                        print(f"{r['name'][:34]:34s} {'url-404':12s} {cur:5d} {'—':>6s}  "
                              f"source gone (404) -> {'ARCHIVE' if cur<args.stub_threshold else 'keep'}")
                        continue
                    resp.raise_for_status()
                    manifest = parser.parse(resp.text, source_hint=src)
                    pi = parser.to_procedure_input(manifest)
                    full = _content_len(manifest.description, pi.implementation_notes)
                    if full > cur:
                        recover.append((r, pi, cur, full))
                        print(f"{r['name'][:34]:34s} {'url':12s} {cur:5d} {full:6d}  RE-IMPORT (+{full-cur})")
                    else:
                        print(f"{r['name'][:34]:34s} {'url':12s} {cur:5d} {full:6d}  skip (no gain)")
                except (httpx.TransportError, httpx.HTTPStatusError) as e:
                    url_fail.append((r, cur, str(e)[:40]))
                    print(f"{r['name'][:34]:34s} {'url-FAIL':12s} {cur:5d} {'?':>6s}  KEEP (transient: {str(e)[:24]})")
                except Exception as e:  # parse / unexpected
                    url_fail.append((r, cur, str(e)[:40]))
                    print(f"{r['name'][:34]:34s} {'url-ERR':12s} {cur:5d} {'?':>6s}  KEEP (parse err: {str(e)[:24]})")
            else:
                # Resolve relative non-HTTP sources against the configured workspace
                # (matching learn_skill's settings.workspace_dir), then try the
                # filesystem before declaring gone — a present local SKILL.md is
                # re-importable; archiving it would lose learned stats on the next
                # bootstrap re-create (codex P1).
                fpath = None
                if src and src not in ("inline", "local", ""):
                    p = Path(src)
                    if not p.is_absolute():
                        p = Path(getattr(s, "workspace_dir", ".") or ".") / src
                    fpath = p
                if fpath is not None and fpath.is_file():
                    try:
                        manifest = parser.parse(fpath.read_text(encoding="utf-8"), source_hint=src)
                        pi = parser.to_procedure_input(manifest)
                        full = _content_len(manifest.description, pi.implementation_notes)
                        if full > cur:
                            recover.append((r, pi, cur, full))
                            print(f"{r['name'][:34]:34s} {'local-file':12s} {cur:5d} {full:6d}  RE-IMPORT (+{full-cur})")
                        else:
                            print(f"{r['name'][:34]:34s} {'local-file':12s} {cur:5d} {full:6d}  skip (no gain)")
                    except Exception as e:
                        # Readable source but read/parse failed (transient/encoding/
                        # mid-edit) — KEEP, don't archive a recoverable skill (codex P2).
                        url_fail.append((r, cur, str(e)[:40]))
                        print(f"{r['name'][:34]:34s} {'local-FAIL':12s} {cur:5d} {'?':>6s}  KEEP (read/parse err)")
                    continue
                # inline, or a path that resolves to a missing file -> source gone
                kind = "inline" if src == "inline" else "path-gone"
                if cur < args.stub_threshold:
                    gone_stub.append((r, cur, kind))
                    print(f"{r['name'][:34]:34s} {kind:12s} {cur:5d} {'—':>6s}  ARCHIVE (stub, source gone)")
                else:
                    gone_full.append((r, cur))
                    print(f"{r['name'][:34]:34s} {kind:12s} {cur:5d} {'—':>6s}  keep (source gone, has content)")

    print("\n" + "=" * 72)
    print(f"  RECOVERABLE (re-import full body): {len(recover)}")
    print(f"  source-gone, KEEP (functional):    {len(gone_full)}")
    print(f"  url-FAIL (transient, KEEP):        {len(url_fail)}")
    print(f"  NON-RECOVERABLE STUBS (archive):   {len(gone_stub)}")
    if gone_stub:
        print("\n  --- non-recoverable stubs (would be archived) ---")
        for r, cur, kind in gone_stub:
            print(f"    {r['name']}  [{kind}, {cur} ch total]")

    if not args.commit:
        print("\n  (dry-run — pass --commit to apply)")
        return

    print("\n[COMMIT] applying...")
    n_re = n_null = n_fail = n_arch = 0
    try:
        for r, pi, cur, full in recover:
            try:
                detail = await heart.update_procedure_body(r["id"], pi)
                n_re += 1
                if getattr(detail, "embedding", "x") is None:
                    n_null += 1
                    print(f"  WARN: {r['name']} re-imported but embedding is NULL "
                          f"(body too large for embed?) — search-invisible")
            except Exception as e:
                n_fail += 1
                print(f"  FAIL re-import {r['name']}: {e}")
        # archive runs independently of re-import outcome
        async with db.session() as sess:
            for r, cur, kind in gone_stub:
                await sess.execute(text(
                    "UPDATE heart.procedures SET active=false, archived_at=now() WHERE id=:i"
                ), {"i": r["id"]})
            await sess.commit()
            n_arch = len(gone_stub)
    finally:
        print(f"[COMMIT] re-imported {n_re} (null-embed {n_null}, failed {n_fail}), "
              f"archived {n_arch} stubs.")


if __name__ == "__main__":
    asyncio.run(main())
