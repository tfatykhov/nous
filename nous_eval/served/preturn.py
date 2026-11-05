"""Pre-turn entry point: each input replays pre_turn's retrieval steps (layer.py: FrameEngine.select
-> IntentClassifier.classify -> plan_retrieval -> deictic/recap adjustments -> ContextEngine.build).
What reached the model is read from the F091 retrieval trace (candidates with disposition
``rendered``, in each section's render order), plus the Session Profile intent-leg facts whose text
reached that section. Ported from the query-mode A/B harness (eval/query-mode-ab, 082135ea).
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


async def build_one(stack: Any, text: str, session_id: str, *, turn_number: int) -> dict[str, Any]:
    """One pre-turn context build; what reached the model, by id, plus its cost."""
    agent = stack.settings.agent_id
    frame = await stack.frames.select(agent, text)
    plan, temporal_boost = plan_like_pre_turn(stack, text, frame, False)
    stack.intent_leg_hits.clear()
    t0 = time.monotonic()
    br = await stack.ctx.build(
        agent,
        session_id,
        text,
        frame,
        conversation_messages=[],
        retrieval_plan=plan,
        temporal_boost=temporal_boost,
        is_first_turn=False,
        turn_number=turn_number,
    )
    ms = (time.monotonic() - t0) * 1000
    trace = br.retrieval_trace.to_dict() if br.retrieval_trace is not None else {}
    cands = trace.get("candidates") or []
    rendered_cands = [c for c in cands if c.get("disposition") == "rendered"]
    rendered_keys = {(c["type"], str(c["id"])) for c in rendered_cands}
    order: list[tuple[str, str]] = []
    for t, ids in br.recalled_ids.items():  # recalled_ids carries each section's render order
        order += [(t, str(i)) for i in ids if (t, str(i)) in rendered_keys]
    for c in sorted(rendered_cands, key=lambda c: (c.get("final_rank") or 10**6, c.get("entry_rank") or 10**6)):
        if (c["type"], str(c["id"])) not in order:
            order.append((c["type"], str(c["id"])))
    snippets = {(c["type"], str(c["id"])): c.get("snippet") or "" for c in rendered_cands}
    profile_leg = "".join(sec.content for sec in br.sections if sec.label == "Session Profile")
    for f in stack.intent_leg_hits:
        key, body = ("fact", str(f.id)), (getattr(f, "content", "") or "")
        if profile_leg and body.strip()[:40] in profile_leg and key not in order:
            order.append(key)
            snippets[key] = body
    contents = {f"{t}:{i}": (br.recalled_content_map.get(i) or snippets.get((t, i)) or "")[:1200] for t, i in order}
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
    }


class DegradedBuild(Exception):
    """ContextEngine.build contains section failures (a dropped connection loses a section, it
    does not raise), so a build is checked: no leg error, and the User Profile still renders."""


async def build_item(stack: Any, it: dict, profile_seen: list[bool], attempts: int = 4) -> dict[str, Any]:
    """One input; a failed or degraded build retries under a fresh session id."""
    last = ""
    for attempt in range(attempts):
        sid = f"{it['session']}-served" + (f"-r{attempt}" if attempt else "")
        try:
            res = await build_one(stack, it["text"], sid, turn_number=it["turn_number"])
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
    """Warm up on the first ``warm`` inputs (discarded, own session ids), then one build per input."""
    for it in items[:warm]:
        try:
            await build_one(stack, it["text"], f"{it['session']}-warm", turn_number=it["turn_number"])
        except Exception:  # noqa: BLE001 - a warm-up failure shows up again in the measured build
            pass
    profile_seen: list[bool] = []
    n_err = 0
    with rows_path.open("w", encoding="utf-8") as f:
        for i, it in enumerate(items):
            r = await build_item(stack, it, profile_seen)
            n_err += 1 if r.get("error") else 0
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
    return {"n": len(items), "errors": n_err}
