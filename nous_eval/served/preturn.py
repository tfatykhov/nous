"""Pre-turn entry point: each input replays pre_turn's retrieval steps (layer.py: FrameEngine.select
-> IntentClassifier.classify -> plan_retrieval -> deictic/recap adjustments -> ContextEngine.build).
Ported from the query-mode A/B harness (eval/query-mode-ab, 082135ea).

What a row holds is read from the final text, not from the engine's id lists: every section
formatter's calls are recorded (``stack.formatter_calls``), each item's own line or block is
re-rendered from them, and each judged section's final content (after date sorting and budget
cuts) is split into those items, sections in the order the final prompt holds them
(``read_items``). That split is the only source of a
row's items, their positions and the text the judge sees, and it is checked per build against
the retrieval trace and the final system prompt (``READ_CHECKS``).

First turn: pre_turn computes ``is_first_turn`` as no earlier turn in the session, no active
episode, and not a subtask turn. Each replay is a fresh session, so an input is a first turn when
its ``first_turn`` says so (inputs.py: a turn-1 prompt of a kind that does not run as a subtask).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger("nous_eval.served")
SNIPPET_CHARS = 400
MEMORY_LABELS = {
    "Related Decisions",
    "Relevant Facts",
    "Session Profile",
    "Recommended Procedures",
    "Known Procedures",
    "Past Episodes",
    "Recent Conversations",
    "Memory Retrieval Notice",
}


def plan_like_pre_turn(stack: Any, text: str, frame: Any, is_first_turn: bool) -> tuple[Any, bool]:
    """layer.py pre_turn step 2b, in effect: classify, plan, deictic and recap rescues."""
    from nous.cognitive.intent import IntentSignals
    from nous.cognitive.layer import _DEICTIC_RECENCY_FLOOR, _is_recap_query, _should_boost_deictic

    clf, s = stack.classifier, stack.settings
    signals = clf.classify(text, frame)
    plan = clf.plan_retrieval(signals, input_text=text)
    if _should_boost_deictic(is_first_turn, s.followup_deictic_detection_enabled, text):
        signals.temporal_recency = max(signals.temporal_recency, _DEICTIC_RECENCY_FLOOR)
        signals.is_greeting = False
        plan = clf.plan_retrieval(signals, input_text=text)
    is_recap = _is_recap_query(text)
    temporal_boost = is_recap or signals.temporal_recency > 0.5
    if is_recap and signals.temporal_recency <= 0.5:
        signals = IntentSignals(
            frame_type=signals.frame_type,
            entity_mentions=signals.entity_mentions,
            temporal_recency=0.8,
            memory_type_hints=signals.memory_type_hints,
            is_question=signals.is_question,
            is_greeting=signals.is_greeting and not s.followup_greeting_request_detection_enabled,
            topic_keywords=signals.topic_keywords,
            text=signals.text,
        )
        plan = clf.plan_retrieval(signals, input_text=text)
    return plan, temporal_boost


async def build_one(
    stack: Any,
    text: str,
    session_id: str,
    *,
    turn_number: int,
    first_turn: bool = False,
    source_session: str | None = None,
) -> dict[str, Any]:
    """One pre-turn context build; what reached the model, by id, plus its cost. ``session_id``
    is the build's own (fresh) id; ``source_session`` the conversation the prompt came from,
    which only the own-session boundary sees (stack._route_source_session)."""
    from nous_eval.served.recall import own_session_dropped

    stack.source_session = source_session
    agent = stack.settings.agent_id
    frame = await stack.frames.select(agent, text)
    plan, temporal_boost = plan_like_pre_turn(stack, text, frame, first_turn)
    stack.formatter_calls.clear()
    stack.listed_episodes = []
    t0 = time.monotonic()
    br = await stack.ctx.build(
        agent,
        session_id,
        text,
        frame,
        conversation_messages=[],
        retrieval_plan=plan,
        temporal_boost=temporal_boost,
        is_first_turn=first_turn,
        turn_number=turn_number,
    )
    ms = (time.monotonic() - t0) * 1000
    trace = br.retrieval_trace.to_dict() if br.retrieval_trace is not None else {}
    cands = trace.get("candidates") or []
    traced = {(c["type"], str(c["id"])) for c in cands if c.get("disposition") == "rendered"}
    recent_text = "\n".join(sec.content for sec in br.sections if sec.label == "Recent Conversations")
    recent_ids, recent_blocks, recent_ok = recent_episodes(stack.listed_episodes, recent_text)
    read = read_items(
        [(sec.label, sec.content) for sec in br.sections],
        formatter_lines(stack.formatter_calls),
        recent_ids,
        recent_blocks,
        recent_ok,
        traced,
        br.system_prompt,
    )
    by_label: dict[str, int] = defaultdict(int)
    for sec in br.sections:
        by_label[sec.label] += sec.token_estimate
    legs = [
        {k: lg.get(k) for k in ("name", "attempted", "n_returned", "skip_reason", "error", "query")}
        for lg in trace.get("legs", [])
    ]
    dropped: dict[str, int] = defaultdict(int)  # "<type>:<disposition>:<stage>" of each candidate not rendered
    for c in cands:
        if c.get("disposition") != "rendered":
            dropped[f"{c.get('type')}:{c.get('disposition')}:{c.get('disposition_stage')}"] += 1
    return {
        "frame_id": frame.frame_id,
        "plan_queries": {q.memory_type: q.query_text for q in plan.queries},
        "plan_skip_types": sorted(plan.skip_types),
        "legs": legs,
        "rendered": [f"{t}:{i}" for t, i in read["order"]],
        "dropped": dict(dropped),
        "own_session_excluded": own_session_dropped(cands),
        "contents": {f"{t}:{i}": read["texts"][(t, i)][:1200] for t, i in read["order"]},
        "tokens_total": sum(by_label.values()),
        "tokens_memory": sum(v for k, v in by_label.items() if k in MEMORY_LABELS),
        "tokens_by_label": dict(by_label),
        "system_prompt": br.system_prompt,
        "build_ms": round(ms, 1),
        "first_turn": first_turn,
        "recent_parsed": recent_ok,
        **{k: read[k] for k in (*READ_CHECKS, *READ_NOTES)},
        **({"read_problems": read["problems"]} if read["problems"] else {}),
    }


# Section label -> the formatter pool its items come from. Recent Conversations is built inline
# (not by a formatter) and is read by ``recent_episodes``.
_SECTION_POOLS = {
    "Related Decisions": "decision",
    "Relevant Facts": "fact",
    "Session Profile": "fact",
    "Recommended Procedures": "procedure",
    "Known Procedures": "procedure",
    "Past Episodes": "episode",
    "Recent Conversations": "recent",
}
# What each build checks; a run sums them and ``judge`` refuses a run where any is non-zero.
READ_CHECKS = ("unplaced", "untraced", "unmatched_segments", "sections_not_in_prompt")
# Recorded per build and summed per run, not a refusal: see read_items.
READ_NOTES = ("budget_cut_but_traced", "fragment_untraced")


def formatter_lines(calls: list) -> dict[str, list[tuple[tuple[str, str], str]]]:
    """Each item's text as its section formatter renders it, per pool, from the calls the build
    made (``stack.formatter_calls``). A fact's line is re-rendered alone with its own exemption
    (inside ``full_top_n`` or not), lineage and date flag, which is how the formatter renders
    it inside the list; the date sort and the budget only reorder and cut whole lines."""
    pools: dict[str, list[tuple[tuple[str, str], str]]] = defaultdict(list)
    for call in calls:
        kind = call[0]
        if kind == "procedure_blocks":
            for p, block in call[1]:
                pools["procedure"].append((("procedure", str(getattr(p, "id", ""))), block))
            continue
        _, orig, items, kwargs = call
        for idx, it in enumerate(items):
            if kind == "fact":
                full = 1 if idx < int(kwargs.get("full_top_n") or 0) else 0
                line = orig([it], full_top_n=full, lineage=kwargs.get("lineage"), dated=kwargs.get("dated", False))
            else:
                line = orig([it])
            if line:
                pools[kind].append(((kind, str(getattr(it, "id", ""))), line))
    return pools


def parse_section(text: str, candidates: list[tuple[tuple[str, str], str]]) -> tuple[list, list[str]]:
    """Split a section's final text into the items it rendered, in order. At each position the
    longest unused candidate line that matches and ends at a line break (or the end) is taken;
    the separating newlines are skipped. A tail that only begins a candidate's line (the section
    budget cut it, ``...`` appended by a character cut) is that item, as cut. Returns
    [(key, text as rendered)] and the text segments (lines) no candidate matched."""
    cands = sorted((c for c in candidates if c[1]), key=lambda c: -len(c[1]))
    used: set = set()
    out: list = []
    unmatched: list[str] = []
    pos, n = 0, len(text)
    while pos < n:
        if text[pos] == "\n":
            pos += 1
            continue
        hit = None
        for key, line in cands:
            end = pos + len(line)
            if key not in used and text.startswith(line, pos) and (end == n or text[end] == "\n"):
                hit = (key, line)
                break
        if hit is not None:
            out.append(hit)
            used.add(hit[0])
            pos += len(hit[1])
            continue
        tail = text[pos:]
        stem = tail[:-3] if tail.endswith("...") else tail
        part = next((k for k, line in cands if stem and k not in used and line.startswith(stem)), None)
        if part is not None:
            out.append((part, tail))
            break
        nl = text.find("\n", pos)
        if stem:
            unmatched.append(text[pos : n if nl < 0 else nl])
        pos = n if nl < 0 else nl + 1
    return out, unmatched


def read_items(
    sections: list[tuple[str, str]],
    pools: dict[str, list],
    recent_ids: list[str],
    recent_blocks: dict[str, str],
    recent_ok: bool,
    traced: set[tuple[str, str]],
    prompt: str,
) -> dict[str, Any]:
    """The items the model read, in the order it read them, each with the text it read: judged
    sections in the order they appear in the final system prompt, each split into the items its
    formatter rendered. This is the only source of a row's items, their text and their positions.

    Checks, each expected 0: ``unplaced`` (the trace says rendered, no section text holds it),
    ``untraced`` (in the text, not rendered per the trace), ``unmatched_segments`` (text no
    candidate matched), ``sections_not_in_prompt`` (a judged section missing from the prompt)."""
    placed = []
    missing = 0
    for label, content in sections:
        if label not in _SECTION_POOLS or not content:
            continue
        at = prompt.find(f"## {label}\n\n{content}")
        if at < 0:
            missing += 1
            continue
        placed.append((at, label, content))
    order: list[tuple[str, str]] = []
    texts: dict[tuple[str, str], str] = {}
    problems: list[dict[str, str]] = []  # what a non-zero check is about, for the row
    for _, label, content in sorted(placed):
        if _SECTION_POOLS[label] == "recent":
            items = [(("episode", i), recent_blocks[i]) for i in recent_ids if i in recent_blocks] if recent_ok else []
        else:
            items, bad = parse_section(strip_tools_note(content), pools.get(_SECTION_POOLS[label], []))
            problems += [{"check": "unmatched_segments", "section": label, "text": t[:300]} for t in bad]
        for key, txt in items:
            if key not in texts:
                order.append(key)
                texts[key] = txt
    # A traced item that no section text holds although its formatter rendered a line for it: the
    # section budget cut that line, and the trace's own budget check (a 40-char content probe that
    # only ever downgrades on a clear absence) found its content elsewhere in the prompt. The text
    # is the authority; these are recorded, not counted as unplaced.
    formatted = {k for pool in pools.values() for k, _ in pool}
    missing_items = [(t, i) for t, i in traced if (t, i) not in texts]
    unplaced = sorted(f"{t}:{i}" for t, i in missing_items if (t, i) not in formatted)
    cut = sorted(f"{t}:{i}" for t, i in missing_items if (t, i) in formatted)
    # The mirror case: the budget left only the start of an item's line (a fragment such as
    # "- [succ..."), so the trace's content probe dropped it. It is in the text and is judged as
    # shown; it is recorded, not counted as untraced.
    whole = defaultdict(set)
    for pool in pools.values():
        for k, line in pool:
            whole[k].add(line)
    fragments = {k for k in texts if k in whole and texts[k] not in whole[k]}
    untraced = sorted(f"{t}:{i}" for t, i in texts if (t, i) not in traced and (t, i) not in fragments)
    fragment_untraced = sorted(f"{t}:{i}" for t, i in texts if (t, i) not in traced and (t, i) in fragments)
    problems += [{"check": "unplaced", "item": k} for k in unplaced]
    problems += [{"check": "untraced", "item": k} for k in untraced]
    return {
        "order": order,
        "texts": texts,
        "unplaced": len(unplaced),
        "untraced": len(untraced),
        "unmatched_segments": sum(1 for x in problems if x["check"] == "unmatched_segments"),
        "sections_not_in_prompt": missing,
        "budget_cut_but_traced": len(cut),
        "fragment_untraced": len(fragment_untraced),
        "problems": problems
        + [{"check": "budget_cut_but_traced", "item": k} for k in cut]
        + [{"check": "fragment_untraced", "item": k} for k in fragment_untraced],
    }


def strip_tools_note(content: str) -> str:
    """A recalled-memory section that names a changed tool ends with one note line about it
    (ContextEngine._note_changed_tools): section furniture, not an item. Removed before splitting."""
    try:
        from nous.tool_names import changed_tools_note
    except ImportError:  # code that predates the note
        return content
    head, sep, last = content.rpartition("\n")
    return head if sep and last and changed_tools_note(head) == last else content


def recent_episodes(listed: list, section_text: str) -> tuple[list[str], dict[str, str], bool]:
    """The episodes of Recent Conversations, in render order, and each one's block as rendered.
    The section shows ``heart.list_episodes``' result minus the build's own system and abandoned
    filters (the same predicates), one block per episode starting ``- [``; ``ok`` is False when
    the blocks and the episodes do not pair up (then no block is used)."""
    from nous.cognitive.context import _is_abandoned_episode, _is_system_episode

    ids = [str(e.id) for e in listed if not _is_system_episode(e) and not _is_abandoned_episode(e)]
    if not section_text:
        return [], {}, not ids
    blocks: list[str] = []
    for line in section_text.split("\n"):
        if line.startswith("- [") or not blocks:
            blocks.append(line)
        else:
            blocks[-1] += "\n" + line
    ok = len(blocks) == len(ids)
    return ids, (dict(zip(ids, blocks, strict=True)) if ok else {}), ok


class DegradedBuild(Exception):
    """ContextEngine.build contains section failures (a dropped connection loses a section, it
    does not raise), so a build is checked: no leg error, and the User Profile still renders."""


async def build_item(stack: Any, it: dict, profile_seen: list[bool], attempts: int = 4) -> dict[str, Any]:
    """One input; a failed or degraded build retries under a fresh session id."""
    last = ""
    for attempt in range(attempts):
        sid = f"{it['session']}-served" + (f"-r{attempt}" if attempt else "")
        try:
            res = await build_one(
                stack,
                it["text"],
                sid,
                turn_number=it["turn_number"],
                first_turn=it["first_turn"],
                source_session=it.get("source_session"),
            )
            errs = [lg["name"] for lg in res["legs"] if lg.get("error")]
            has_profile = "User Profile" in res["tokens_by_label"]
            if errs or (profile_seen and profile_seen[0] and not has_profile):
                raise DegradedBuild(f"leg errors {errs}, profile={has_profile}")
            if not profile_seen:
                profile_seen.append(has_profile)
            prompt = res.pop("system_prompt")
            row = {"attempt": attempt, "system_prompt_chars": len(prompt), **res}
            if it.get("gold"):
                row["gold_text_in_prompt"] = (
                    bool(it["gold"]["content"]) and it["gold"]["content"].strip()[:60] in prompt
                )
            return row
        except Exception as exc:  # noqa: BLE001 - retried, then recorded
            last = f"{type(exc).__name__}: {exc}"[:300]
            logger.warning("preturn %s attempt %d failed: %s", it["key"], attempt + 1, type(exc).__name__)
            await asyncio.sleep(2 * (attempt + 1))
    return {"error": last}


async def run(stack: Any, items: list[dict], rows_path: Path, *, warm: int) -> dict[str, Any]:
    """A warm pass (discarded, own session ids) over the first ``warm`` inputs, every input when
    ``warm`` < 0, so every query a build embeds is cached before anything is measured; then one
    build per input."""
    from nous_eval.served.stack import embedding_cache_size

    for it in items if warm < 0 else items[:warm]:
        try:
            await build_one(
                stack,
                it["text"],
                f"{it['session']}-warm",
                turn_number=it["turn_number"],
                first_turn=it["first_turn"],
                source_session=it.get("source_session"),
            )
        except Exception:  # noqa: BLE001 - a warm-up failure shows up again in the measured build
            pass
    profile_seen: list[bool] = []
    n_err = n_recent_unparsed = n_own_queries = n_own_rows = 0
    checks = dict.fromkeys((*READ_CHECKS, *READ_NOTES), 0)
    before = embedding_cache_size(stack.embeddings)
    with rows_path.open("w", encoding="utf-8") as f:
        for i, it in enumerate(items):
            r = await build_item(stack, it, profile_seen)
            n_err += 1 if r.get("error") else 0
            for k in checks:
                checks[k] += r.get(k) or 0
            n_recent_unparsed += r.get("recent_parsed") is False
            n_own_queries += bool(r.get("own_session_excluded"))
            n_own_rows += r.get("own_session_excluded") or 0
            row = {
                "key": it["key"],
                "set": it["set"],
                "kind": it.get("kind"),
                "text": it["text"],
                "input_chars": len(it["text"]),
                **r,
            }
            if it.get("gold"):
                row["gold"] = it["gold"]
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            if (i + 1) % 25 == 0:
                f.flush()
                logger.info("preturn %d/%d", i + 1, len(items))
    after = embedding_cache_size(stack.embeddings)
    return {
        "n": len(items),
        "errors": n_err,
        "embeddings_added_while_measuring": None if before is None else after - before,
        **checks,
        "recent_unparsed": n_recent_unparsed,
        "own_session_queries": n_own_queries,
        "own_session_rows": n_own_rows,
    }
