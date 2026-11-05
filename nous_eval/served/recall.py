"""recall_deep entry point: each input goes through the real ``recall_deep`` tool closure
(``create_nous_tools``) with ``limit=10`` and no memory-type filter, as the agent calls it; what
it rendered is read from the F091 retrieval trace (candidates with disposition ``rendered``,
in final-rank order, each with its entry leg)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("nous_eval.served")
SNIPPET_CHARS = 1200


async def one_call(stack: Any, query: str) -> dict[str, Any]:
    rl = stack.recorder
    rl._entries.clear()
    stack.ce_calls.clear()
    if stack.date_parser is not None:
        stack.date_parser.last = None
    t0 = time.perf_counter()
    res = await stack.tools["recall_deep"](query=query, limit=10, memory_types=None)
    wall = (time.perf_counter() - t0) * 1000
    text_out = "".join(b.get("text", "") for b in res.get("content", []))
    tr = rl._entries[-1] if rl._entries else None
    if tr is None:
        return {"error": "no retrieval trace", "is_error": bool(res.get("is_error"))}
    rendered = [c for c in (tr.get("candidates") or []) if c.get("disposition") == "rendered"]
    rendered.sort(key=lambda c: (c.get("final_rank") is None, c.get("final_rank") or 0))
    legs = {
        lg["name"]: {k: lg.get(k) for k in ("attempted", "n_returned", "n_dropped", "skip_reason", "error")}
        for lg in tr.get("legs", [])
    }
    return {
        "is_error": bool(res.get("is_error")),
        "wall_ms": round(wall, 1),
        "trace_ms": tr.get("duration_ms"),
        "chars": len(text_out),
        "text_sha": hashlib.sha1(text_out.encode("utf-8")).hexdigest()[:16],
        "truncated": tr.get("truncated"),
        "n_candidates": tr.get("n_candidates"),
        "rendered": [
            {
                "k": f"{c['type']}:{c['id']}",
                "leg": c.get("entry_leg"),
                "rank": c.get("final_rank"),
                "score": c.get("entry_score"),
            }
            for c in rendered
        ],
        "contents": {f"{c['type']}:{c['id']}": (c.get("snippet") or "") for c in rendered},
        "legs": legs,
        "leg_errors": {n: lg["error"] for n, lg in legs.items() if lg.get("error")},
        "date": stack.date_parser.last if stack.date_parser is not None else None,
        "ce": list(stack.ce_calls),
    }


async def run(stack: Any, items: list[dict], rows_path: Path, *, warm: int) -> dict[str, Any]:
    """Fill the date-window store for every input, warm up on the first ``warm`` inputs (results
    discarded: buffers, embedding cache, the cross-encoder load), then one call per input; one
    row per input in ``rows_path``."""
    prefill = await stack.date_parser.prefill([it["text"] for it in items]) if stack.date_parser else None
    for it in items[:warm]:
        try:
            await one_call(stack, it["text"])
        except Exception:  # noqa: BLE001 - a warm-up failure shows up again in the measured call
            pass
    n_err = 0
    with rows_path.open("w", encoding="utf-8") as f:
        for i, it in enumerate(items):
            try:
                r = await one_call(stack, it["text"])
            except Exception as exc:  # noqa: BLE001 - recorded; the score counts error rows
                r = {"error": f"{type(exc).__name__}: {exc}"[:300]}
            n_err += 1 if r.get("error") else 0
            row = {"key": it["key"], "set": it["set"], "text": it["text"], **r}
            if it.get("gold"):
                row["gold"] = it["gold"]
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            if (i + 1) % 25 == 0:
                f.flush()
                logger.info("recall %d/%d", i + 1, len(items))
    return {"n": len(items), "errors": n_err, "date_prefill": prefill}


def _kind(session_id: str) -> str:
    if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-", session_id or ""):
        return "chat"
    m = re.match(r"[A-Za-z]+", session_id or "")
    return m.group(0).lower() if m else "none"


async def extract_queries(db: Any, agent: str, days: int) -> list[dict[str, Any]]:
    """Distinct recall_deep queries (retrieval_log path 'pipeline') of the copy's last ``days``
    days, read-only; the latest row of each normalised query wins. Keys Q000.. in a hash order,
    so a key does not depend on the log's row order."""
    from sqlalchemy import text

    async with db.engine.connect() as c:
        await c.execute(text("SET TRANSACTION READ ONLY"))
        rows = (
            await c.execute(
                text(
                    "SELECT session_id, timestamp, query FROM nous_system.retrieval_log "
                    "WHERE agent_id = :a AND path = 'pipeline' "
                    "AND timestamp > (SELECT max(timestamp) FROM nous_system.retrieval_log) "
                    "- make_interval(days => :d) "
                    "ORDER BY timestamp"
                ),
                {"a": agent, "d": days},
            )
        ).all()
    latest: dict[str, dict] = {}
    for r in rows:
        if (r.query or "").strip():
            nq = re.sub(r"\s+", " ", r.query.strip().lower())
            latest[nq] = {"query": r.query, "logged_at": r.timestamp.isoformat(), "kind": _kind(r.session_id or "")}
    vals = sorted(latest.items(), key=lambda kv: hashlib.sha1(kv[0].encode()).hexdigest())
    return [{"key": f"Q{i:03d}", **v} for i, (_, v) in enumerate(vals)]
