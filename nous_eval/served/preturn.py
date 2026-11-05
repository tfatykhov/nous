"""Pre-turn entry point: each input replays pre_turn's retrieval steps (layer.py: FrameEngine.select
-> IntentClassifier.classify -> plan_retrieval -> deictic/recap adjustments -> ContextEngine.build).
What reached the model is read from the F091 retrieval trace (candidates with disposition
``rendered``, in each section's render order), plus the Session Profile intent-leg facts whose text
reached that section. Items are ordered as the model reads them: section by section in the order
the build emitted its sections, each section in its own order (``section_order``). An item's text
is what it was rendered as: a procedure's body block or catalog line, not its name; an episode
shown only in Recent Conversations, its block there. Ported from the query-mode A/B harness
(eval/query-mode-ab, 082135ea).

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
    stack: Any, text: str, session_id: str, *, turn_number: int, first_turn: bool = False
) -> dict[str, Any]:
    """One pre-turn context build; what reached the model, by id, plus its cost."""
    agent = stack.settings.agent_id
    frame = await stack.frames.select(agent, text)
    plan, temporal_boost = plan_like_pre_turn(stack, text, frame, first_turn)
    stack.intent_leg_hits.clear()
    stack.procedure_text.clear()
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
    rendered_cands = [c for c in cands if c.get("disposition") == "rendered"]
    snippets = {(c["type"], str(c["id"])): c.get("snippet") or "" for c in rendered_cands}
    rendered = [(c["type"], str(c["id"]), c.get("final_rank")) for c in rendered_cands]
    profile_ids = _profile_fact_ids(stack.intent_leg_hits, br, snippets, rendered)
    recent_text = "\n".join(sec.content for sec in br.sections if sec.label == "Recent Conversations")
    temporal_ids, temporal_blocks, temporal_ok = recent_episodes(stack.listed_episodes, recent_text)
    order, unplaced = section_order(
        [sec.label for sec in br.sections], br.recalled_ids, temporal_ids, profile_ids, rendered
    )
    contents = item_texts(
        order,
        stack.procedure_text,
        temporal_blocks,
        br.recalled_ids.get("episode", []),
        br.recalled_content_map,
        snippets,
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
        "rendered": [f"{t}:{i}" for t, i in order],
        "dropped": dict(dropped),
        "contents": contents,
        "tokens_total": sum(by_label.values()),
        "tokens_memory": sum(v for k, v in by_label.items() if k in MEMORY_LABELS),
        "tokens_by_label": dict(by_label),
        "system_prompt": br.system_prompt,
        "build_ms": round(ms, 1),
        "first_turn": first_turn,
        "unplaced": unplaced,
        "recent_parsed": temporal_ok,
    }


def item_texts(
    order: list[tuple[str, str]],
    procedure_text: dict[str, str],
    recent_blocks: dict[str, str],
    past_episode_ids: list,
    recalled_content_map: dict[str, str],
    snippets: dict[tuple[str, str], str],
) -> dict[str, str]:
    """What each item was rendered as (to 1200 chars), the text the judge sees: a procedure's body
    block or catalog line, not its name; an episode shown only in Recent Conversations, its block
    there (title, plus a summary only on a first turn or under temporal boost), not its full
    summary; otherwise the section's content map, then the trace snippet."""
    past = {str(i) for i in past_episode_ids}
    shown = {("procedure", k): v for k, v in procedure_text.items()}
    shown.update({("episode", i): v for i, v in recent_blocks.items() if i not in past})
    return {
        f"{t}:{i}": (shown.get((t, i)) or recalled_content_map.get(i) or snippets.get((t, i)) or "")[:1200]
        for t, i in order
    }


# Section label -> what renders in it, in the order the build appended the sections.
_SECTION_ITEMS = {
    "Related Decisions": "decision",
    "Relevant Facts": "fact",
    "Session Profile": "profile",
    "Recommended Procedures": "procedure",
    "Known Procedures": "procedure",
    "Recent Conversations": "recent",
    "Past Episodes": "episode",
}


def section_order(
    labels: list[str],
    recalled_ids: dict[str, list],
    recent_ids: list[str],
    profile_ids: list[str],
    rendered: list[tuple[str, str, int | None]],
) -> tuple[list[tuple[str, str]], int]:
    """Rendered items in the order the model reads them: section by section as the build emitted
    its sections (Recent Conversations before Past Episodes), each section in its own order. A
    rendered item no section claims (none expected) goes last by final rank, and is counted."""
    keyed = {(t, i) for t, i, _ in rendered}
    lists = {
        "recent": [("episode", i) for i in recent_ids],
        "profile": [("fact", i) for i in profile_ids],
        **{t: [(t, str(i)) for i in recalled_ids.get(t, [])] for t in ("decision", "fact", "procedure", "episode")},
    }
    order: list[tuple[str, str]] = []
    for label in labels:
        for k in lists.get(_SECTION_ITEMS.get(label, ""), []):
            if k in keyed and k not in order:
                order.append(k)
    placed = set(order)
    rest = sorted(((r, t, i) for t, i, r in rendered if (t, i) not in placed), key=lambda x: (x[0] is None, x[0] or 0))
    return order + [(t, i) for _, t, i in rest], len(rest)


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


def _profile_fact_ids(hits: list, br: Any, snippets: dict, rendered: list) -> list[str]:
    """Session Profile facts in the order their text appears in that section. The intent leg is
    not in recalled_ids; a hit counts when its text is in the section (query-mode A/B)."""
    text = "\n".join(sec.content for sec in br.sections if sec.label == "Session Profile")
    facts = {str(i) for i in br.recalled_ids.get("fact", [])}
    found: dict[str, int] = {}
    for f in hits:
        fid, body = str(f.id), (getattr(f, "content", "") or "")
        pos = text.find(body.strip()[:40]) if text and body.strip() else -1
        if pos >= 0 and fid not in facts and fid not in found:
            found[fid] = pos
            snippets.setdefault(("fact", fid), body)
            if ("fact", fid) not in {(t, i) for t, i, _ in rendered}:
                rendered.append(("fact", fid, None))
    return sorted(found, key=lambda fid: found[fid])


class DegradedBuild(Exception):
    """ContextEngine.build contains section failures (a dropped connection loses a section, it
    does not raise), so a build is checked: no leg error, and the User Profile still renders."""


async def build_item(stack: Any, it: dict, profile_seen: list[bool], attempts: int = 4) -> dict[str, Any]:
    """One input; a failed or degraded build retries under a fresh session id."""
    last = ""
    for attempt in range(attempts):
        sid = f"{it['session']}-served" + (f"-r{attempt}" if attempt else "")
        try:
            res = await build_one(stack, it["text"], sid, turn_number=it["turn_number"], first_turn=it["first_turn"])
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
                stack, it["text"], f"{it['session']}-warm", turn_number=it["turn_number"], first_turn=it["first_turn"]
            )
        except Exception:  # noqa: BLE001 - a warm-up failure shows up again in the measured build
            pass
    profile_seen: list[bool] = []
    n_err = n_unplaced = n_recent_unparsed = 0
    before = embedding_cache_size(stack.embeddings)
    with rows_path.open("w", encoding="utf-8") as f:
        for i, it in enumerate(items):
            r = await build_item(stack, it, profile_seen)
            n_err += 1 if r.get("error") else 0
            n_unplaced += r.get("unplaced") or 0
            n_recent_unparsed += r.get("recent_parsed") is False
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
        "unplaced": n_unplaced,
        "recent_unparsed": n_recent_unparsed,
    }
