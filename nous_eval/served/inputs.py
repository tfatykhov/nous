"""The inputs of a run: real prompts or queries (set ``prod``, judged) and gold questions (set
``gold``, each naming the memory that should come back).

Files (all under the untracked data directory):

- recall_deep queries, JSONL: ``{"key", "query", "logged_at"?, "session_id"?}``; ``logged_at``
  fixes the date a relative date phrase is resolved against, ``session_id`` is the conversation
  the logged call came from (``source_session``: its ongoing episode is left out, as prod does).
- pre-turn prompts, the query-mode A/B's ``prod_templated_prompts.json``
  (``{"prompts": [{"kind", "group", "group_rows", "session_id", "logged_query", "full_text",
  "recovered"}]}``), keyed ``B000``.. in file order.
- gold, JSONL as ``gold`` writes it: ``{"id", "question", "gold": [ids], "gold_type",
  "gold_category", "gold_content"}``. Keys are ``g<type initial>-<id>`` so files never collide.
"""

from __future__ import annotations

import hashlib
import json
import random
import uuid
from pathlib import Path
from typing import Any

from nous_eval.served.judge import read_jsonl

GOLD_TYPES = ("fact", "episode", "decision")
# Prompt kinds (session-id prefixes) whose turns run as subtasks (pre_turn is_subtask=True: the
# subtask worker and executor, heartbeat checks), so pre_turn never treats them as a first turn.
SUBTASK_KINDS = frozenset({"subtask", "dynamic", "heartbeat"})


def gold_items(paths: list[Path], as_of: str) -> list[dict[str, Any]]:
    items = []
    for p in paths:
        for r in read_jsonl(p):
            gtype = r.get("gold_type", "fact")
            if gtype not in GOLD_TYPES:
                raise SystemExit(f"{p.name}: unknown gold_type {gtype!r}")
            items.append(
                {
                    "key": f"g{gtype[0]}-{r['id']}",
                    "set": "gold",
                    "text": r["question"],
                    "as_of": as_of,
                    "source_session": None,  # a gold question was never asked in a conversation
                    "kind": "chat",
                    "gold": {
                        "type": gtype,
                        "ids": [str(i) for i in r["gold"]],
                        "category": r.get("gold_category"),
                        "content": r.get("gold_content") or "",
                    },
                }
            )
    return items


def recall_items(queries: Path | None, gold: list[Path], as_of: str) -> list[dict[str, Any]]:
    items = []
    if queries is not None:
        for q in read_jsonl(queries):
            items.append(
                {
                    "key": q["key"],
                    "set": "prod",
                    "text": q["query"],
                    "kind": q.get("kind"),
                    "as_of": (q.get("logged_at") or "")[:10] or as_of,
                    "source_session": q.get("session_id"),
                }
            )
    return _unique(items + gold_items(gold, as_of))


def preturn_items(prompts: Path | None, gold: list[Path], as_of: str) -> list[dict[str, Any]]:
    items = []
    if prompts is not None:
        data = json.loads(prompts.read_text(encoding="utf-8"))
        for i, p in enumerate(data["prompts"]):
            items.append(
                {
                    "key": f"B{i:03d}",
                    "set": "prod",
                    "text": p["full_text"] or p["logged_query"],
                    "session": p["session_id"],
                    "source_session": p["session_id"],
                    "kind": p["kind"],
                    "group": f"{p['kind']}:{p['group']}",
                    "complete": bool(p.get("recovered") or len(p["logged_query"]) < 500),
                    "turn_number": 1,
                    "first_turn": p["kind"] not in SUBTASK_KINDS,  # turn 1 of a fresh session
                }
            )
    for g in gold_items(gold, as_of):  # chat questions: a later turn of a fresh session (query-mode A/B)
        g.update(session=str(uuid.uuid5(uuid.NAMESPACE_URL, f"served-{g['key']}")), turn_number=2, first_turn=False)
        items.append(g)
    return _unique(items)


def _unique(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    for it in items:
        if it["key"] in seen:
            raise SystemExit(f"duplicate input key {it['key']}")
        seen.add(it["key"])
    return items


def sample(items: list[dict], n: int, seed: int) -> list[dict]:
    """A fixed-seed smoke sample of n inputs, in input order (all when n <= 0)."""
    if n <= 0 or n >= len(items):
        return items
    keep = set(random.Random(seed).sample(range(len(items)), n))
    return [it for i, it in enumerate(items) if i in keep]


def inputs_sha256(items: list[dict]) -> str:
    """What compare checks to know two runs answered the same inputs: each input's text and gold,
    and what changes how it is run (its effective as-of date, source conversation, first-turn state
    and turn number)."""
    h = hashlib.sha256()
    for it in sorted(items, key=lambda x: x["key"]):
        fields = [
            it["key"],
            it["set"],
            it["text"],
            it.get("gold"),
            it.get("as_of"),
            it.get("source_session"),
            it.get("first_turn"),
            it.get("turn_number"),
        ]
        h.update(json.dumps(fields, sort_keys=True).encode() + b"\n")
    return h.hexdigest()


async def mark_gold_validity(db: Any, agent: str, items: list[dict]) -> dict[str, int]:
    """Set gold["valid"]: the gold memory is still one the read path can return on this copy
    (fact active and not superseded; episode open or closed and not abandoned; decision not
    superseded)."""
    from sqlalchemy import text

    sql = {
        "fact": "SELECT id::text FROM heart.facts WHERE agent_id = :a AND id::text = ANY(:ids) "
        "AND active AND superseded_by IS NULL",
        "episode": "SELECT id::text FROM heart.episodes WHERE agent_id = :a AND id::text = ANY(:ids) "
        "AND (active OR ended_at IS NOT NULL) AND outcome IS DISTINCT FROM 'abandoned'",
        "decision": "SELECT id::text FROM brain.decisions WHERE agent_id = :a AND id::text = ANY(:ids) "
        "AND superseded_by IS NULL",
    }
    counts = {"valid": 0, "invalid": 0}
    async with db.session() as s:
        for t, q in sql.items():
            its = [it for it in items if it.get("gold") and it["gold"]["type"] == t]
            ids = sorted({i for it in its for i in it["gold"]["ids"]})
            ok = set((await s.execute(text(q), {"a": agent, "ids": ids})).scalars().all()) if ids else set()
            for it in its:
                it["gold"]["valid"] = any(i in ok for i in it["gold"]["ids"])
                counts["valid" if it["gold"]["valid"] else "invalid"] += 1
    return counts
