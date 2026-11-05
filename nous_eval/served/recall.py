"""recall_deep entry point: each input goes through the real ``recall_deep`` tool closure
(``create_nous_tools``) with ``limit=10`` and no memory-type filter, as the agent calls it.

What it rendered is read from the F091 retrieval trace (candidates with disposition
``rendered``, each with its entry leg), in the order the model READS them: the order the
formatter reported emitting (``stack.render_order``), which follows its sections and its session
grouping and date ordering, not the pipeline's ``final_rank`` (kept as its own field). Every call
also checks that order against the ids the output text prints. A rendered chunk carries the id
of its episode, so episode gold can count a chunk of the gold episode."""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("nous_eval.served")
SNIPPET_CHARS = 1200
_ID_RE = re.compile(r"\(id: ([0-9a-fA-F-]{36})")


def order_rendered(cands: list[dict], read_order: list[tuple[str, str]]) -> tuple[list[dict], int]:
    """Rendered candidates in read order, ``rank`` = 1-based read position. A candidate the
    formatter did not report (none expected) goes last, in final-rank order, and is counted."""
    pos = {k: i for i, k in enumerate(read_order)}
    placed = sorted(
        (c for c in cands if (c["type"], str(c["id"])) in pos), key=lambda c: pos[(c["type"], str(c["id"]))]
    )
    unplaced = sorted(
        (c for c in cands if (c["type"], str(c["id"])) not in pos),
        key=lambda c: (c.get("final_rank") is None, c.get("final_rank") or 0),
    )
    out = [
        {
            "k": f"{c['type']}:{c['id']}",
            "leg": c.get("entry_leg"),
            "rank": i + 1,
            "final_rank": c.get("final_rank"),
            "score": c.get("entry_score"),
        }
        for i, c in enumerate(placed + unplaced)
    ]
    return out, len(unplaced)


def text_order_agrees(text: str, read_order: list[tuple[str, str]]) -> bool | None:
    """The ids the output prints as ``(id: ...)``, in text order, follow the read order; None when
    the output printed none of them (nothing to check, e.g. an error row)."""
    in_order = [i for _, i in read_order]
    printed = [i for i in dict.fromkeys(_ID_RE.findall(text)) if i in set(in_order)]
    if not printed:
        return None
    return printed == [i for i in dict.fromkeys(in_order) if i in set(printed)]


async def chunk_episodes(db: Any, chunk_ids: list[str]) -> dict[str, str]:
    """chunk id -> its episode id (heart.episode_chunks)."""
    if not chunk_ids:
        return {}
    from sqlalchemy import text

    async with db.session() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT id::text AS cid, episode_id::text AS eid FROM heart.episode_chunks "
                    "WHERE id::text = ANY(:ids)"
                ),
                {"ids": chunk_ids},
            )
        ).all()
    return {r.cid: r.eid for r in rows}


# Rank, score and provenance furniture around an item's own text: the list number (an exemplar's
# similarity with it) and the [via ...] tag of the leg or edge that found it.
_NUMBER = re.compile(r"\d+\.(?: \[sim -?\d+\.\d+\])? ")
_VIA = re.compile(r"\[via [^\]\n]*\] ")


def _own_text(element: str, rid: str, rtype: str, exemplar: bool) -> str | None:
    """An emitted element without its furniture; None when it does not have the expected shape."""
    m = _NUMBER.match(element)
    if m is None:
        return None
    body = element[m.end() :]
    if exemplar:
        return body
    if body.startswith(f"[{rtype}] "):  # the Heart Memory type tag (the judge gets the type apart)
        body = body[len(rtype) + 3 :]
    v = _VIA.match(body)
    if v is not None:
        body = body[v.end() :]
    cut = body.rfind(f" (id: {rid}")
    if cut < 0 or "\n" in body[cut:] or not body.endswith(")"):
        return None
    return body[:cut]


def item_texts(cap: dict, text_out: str) -> tuple[dict[str, str], dict[str, Any]]:
    """Each served item's text as the formatter wrote it (``cap`` from stack._capture_render_order):
    its element plus the merge-history lines under it, without the furniture that changes with
    rank, score or the finding leg (list number, exemplar similarity, type tag, [via ...], the
    (id: ..., score ...) trailer), so the same item shown elsewhere keeps its label. A parent
    episode's text is its summary, checked present in the output. A row emitted twice (a decision
    in two sections) keeps its first, where it is read. Checks: ``text_unmatched`` (an item with no
    text: an element of an unexpected shape, a parent line not found, or no elements captured) and
    ``output_matches_capture`` (the elements joined, plus the tool's own changed-tools note when it
    adds one, are the tool's output; None when not captured)."""
    lines, at = cap["lines"], cap["at"]
    if lines is None:
        n = len(dict.fromkeys(cap["emissions"])) + len(cap["parents"])
        return {}, {"text_unmatched": n, "output_matches_capture": None}
    starts = set(at)
    texts: dict[str, str] = {}
    unmatched = 0
    for (rid, rtype), i in zip(cap["emissions"], at, strict=True):
        key = f"{rtype}:{rid}"
        if key in texts:
            continue
        own = _own_text(lines[i], rid, rtype, (rid, rtype) in cap["exemplar"])
        if own is None:
            unmatched += 1
            continue
        j = i + 1
        while j < len(lines) and j not in starts and lines[j].startswith("   "):  # merge history
            j += 1
        texts[key] = "\n".join([own, *lines[i + 1 : j]])
    for ep_id, summary in cap["parents"]:
        key = f"episode:{ep_id}"
        if key in texts:
            continue
        if f"- ({ep_id[:8]}) {summary}" in lines:
            texts[key] = summary
        else:
            unmatched += 1
    return texts, {"text_unmatched": unmatched, "output_matches_capture": _tool_text("\n".join(lines)) == text_out}


def _tool_text(formatted: str) -> str:
    """recall_deep's output for a query-only call with this formatter text: the text, then the
    tool's changed-tools note when the text names a changed tool (nous.tool_names)."""
    try:
        from nous.tool_names import changed_tools_note
    except ImportError:  # code that predates the note
        return formatted
    note = changed_tools_note(formatted)
    return f"{formatted}\n\n{note}" if note else formatted


def input_day(it: dict) -> datetime.date | None:
    """The date an input's relative date phrases resolve against (its ``as_of``)."""
    return datetime.date.fromisoformat(it["as_of"]) if it.get("as_of") else None


async def one_call(stack: Any, query: str, day: datetime.date | None = None) -> dict[str, Any]:
    rl = stack.recorder
    rl._entries.clear()
    stack.ce_calls.clear()
    stack.render_orders.clear()
    stack.render_captures.clear()
    if stack.date_parser is not None:
        stack.date_parser.last = None
        stack.date_parser.day = day
    t0 = time.perf_counter()
    res = await stack.tools["recall_deep"](query=query, limit=10, memory_types=None)
    wall = (time.perf_counter() - t0) * 1000
    text_out = "".join(b.get("text", "") for b in res.get("content", []))
    tr = rl._entries[-1] if rl._entries else None
    if tr is None:
        return {"error": "no retrieval trace", "is_error": bool(res.get("is_error"))}
    read_order = stack.render_orders[-1] if stack.render_orders else []
    texts, text_checks = (
        item_texts(stack.render_captures[-1], text_out)
        if stack.render_captures
        else ({}, {"text_unmatched": None, "output_matches_capture": None})
    )
    cands = [c for c in (tr.get("candidates") or []) if c.get("disposition") == "rendered"]
    rendered, unplaced = order_rendered(cands, read_order)
    episodes = await chunk_episodes(stack.db, [str(c["id"]) for c in cands if c["type"] == "chunk"])
    for it in rendered:
        if it["k"].startswith("chunk:"):
            it["episode"] = episodes.get(it["k"].split(":", 1)[1])
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
        "rendered": rendered,
        "unplaced": unplaced,
        "read_order_matches_text": text_order_agrees(text_out, read_order),
        "contents": {k: texts[k] for k in (f"{c['type']}:{c['id']}" for c in cands) if k in texts},
        "text_unmatched": sum(f"{c['type']}:{c['id']}" not in texts for c in cands),
        "output_matches_capture": text_checks["output_matches_capture"],
        "legs": legs,
        "leg_errors": {n: lg["error"] for n, lg in legs.items() if lg.get("error")},
        "date": stack.date_parser.last if stack.date_parser is not None else None,
        "ce": list(stack.ce_calls),
    }


async def run(stack: Any, items: list[dict], rows_path: Path, *, warm: int) -> dict[str, Any]:
    """Fill the date-window store for every input, then a warm pass (results discarded) over the
    first ``warm`` inputs, every input when ``warm`` < 0: it embeds every query and loads the
    cross-encoder, so no measured call pays for a cache miss whichever arm ran first. Then one
    measured call per input, one row per input in ``rows_path``."""
    from nous_eval.served.stack import embedding_cache_size

    prefill = (
        await stack.date_parser.prefill([(it["text"], input_day(it)) for it in items]) if stack.date_parser else None
    )
    for it in items if warm < 0 else items[:warm]:
        try:
            await one_call(stack, it["text"], input_day(it))
        except Exception:  # noqa: BLE001 - a warm-up failure shows up again in the measured call
            pass
    n_err = n_mismatch = n_unchecked = n_unplaced = n_text_unmatched = n_output_mismatch = 0
    before = embedding_cache_size(stack.embeddings)
    with rows_path.open("w", encoding="utf-8") as f:
        for i, it in enumerate(items):
            try:
                r = await one_call(stack, it["text"], input_day(it))
            except Exception as exc:  # noqa: BLE001 - recorded; the score counts error rows
                r = {"error": f"{type(exc).__name__}: {exc}"[:300]}
            n_err += 1 if r.get("error") else 0
            n_mismatch += r.get("read_order_matches_text") is False
            n_unchecked += not r.get("error") and r.get("read_order_matches_text") is None
            n_unplaced += r.get("unplaced") or 0
            n_text_unmatched += r.get("text_unmatched") or 0
            n_output_mismatch += not r.get("error") and r.get("output_matches_capture") is not True
            row = {"key": it["key"], "set": it["set"], "text": it["text"], **r}
            if it.get("gold"):
                row["gold"] = it["gold"]
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            if (i + 1) % 25 == 0:
                f.flush()
                logger.info("recall %d/%d", i + 1, len(items))
    after = embedding_cache_size(stack.embeddings)
    return {
        "n": len(items),
        "errors": n_err,
        "date_prefill": prefill,
        "embeddings_added_while_measuring": None if before is None else after - before,
        "read_order_mismatches": n_mismatch,
        "read_order_unchecked": n_unchecked,
        "unplaced": n_unplaced,
        "text_unmatched": n_text_unmatched,
        "output_mismatches": n_output_mismatch,
    }


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
