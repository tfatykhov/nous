"""Gold questions: an LLM writes a short chat message that asks for one sampled memory back; that
memory is the gold item. The recipe of the query-mode and episode-cap A/Bs' stratum A, with a
decision variant added:

- facts: sampled from active, embedded facts of 40-400 chars, in two strata (tier-1 categories
  rule/person/preference, and the rest); facts already in context for content-free probe
  prompts (always-on profile tiers) are removed, they cannot tell arms apart;
- episodes: open, or closed and not abandoned, summary 120-3000 chars, split by tagged/untagged;
- decisions: not superseded, embedded, description 40-600 chars.

A question is dropped when the LLM returns null (noise), when its length is outside 20-90
characters, or when it shares too many distinctive words with the memory (more than 1 for a fact,
more than 2 for the longer episodes and decisions): the question must paraphrase, or it measures
keyword overlap.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from nous_eval.served.judge import PRIMARY_MODEL, CostLedger, llm_json

GEN_SYSTEM = "You write realistic chat messages for an evaluation. Reply with JSON only."
_RULES = """Rules:
- Casual chat style: lowercase is fine, a small typo is fine, a question mark is optional.
- Paraphrase. Do NOT reuse the {noun}'s distinctive words, especially words of 6+ letters and names, \
unless the question is impossible without one. Prefer short everyday words and synonyms.
{point}
- If a {noun} is noise ({noise}), use null."""
PROMPTS = {
    "fact": """Below are facts from the long-term memory of an AI assistant ("Nous") that works with its owner \
on software projects. For EACH fact, write one short chat message (20-90 characters) that the owner might type to \
Nous when they want exactly this information back. Real chat messages are short: make about half of them under 50 \
characters.

"""
    + _RULES.format(
        noun="fact",
        noise="a log line, a transient status, too vague to ask about",
        point="- The question must be answerable from the fact alone, and should not be answerable "
        "from general knowledge.",
    )
    + """

Facts:
{items}

Reply as JSON: {{"items": [{{"id": "f1", "question": "..." or null}}, ...]}}""",
    "episode": """Below are summaries of past sessions between an AI assistant ("Nous") and its owner, or of \
background jobs Nous ran. For EACH summary, write one short chat message (20-90 characters) that the owner might \
type later to get something specific about that session back: what was decided, found, fixed or left open. Real \
chat messages are short: make about half of them under 50 characters.

"""
    + _RULES.format(
        noun="summary",
        noise="a greeting, an empty or trivial session, too vague to ask about",
        point="- The question must point at THIS session (its topic or outcome) and not be "
        "answerable from general knowledge.",
    )
    + """

Summaries:
{items}

Reply as JSON: {{"items": [{{"id": "f1", "question": "..." or null}}, ...]}}""",
    "decision": """Below are decisions an AI assistant ("Nous") recorded while working for its owner: what it \
decided and why. For EACH decision, write one short chat message (20-90 characters) that the owner might type later \
to get this decision back: what was chosen, or why. Real chat messages are short: make about half of them under 50 \
characters.

"""
    + _RULES.format(
        noun="decision",
        noise="a routine log entry, too vague to ask about",
        point="- The question must point at THIS decision (its subject or its choice) and not be "
        "answerable from general knowledge.",
    )
    + """

Decisions:
{items}

Reply as JSON: {{"items": [{{"id": "f1", "question": "..." or null}}, ...]}}""",
}
# gold type -> (items per call, shown chars, max shared distinctive words)
SPECS = {"fact": (10, 400, 1), "episode": (8, 700, 2), "decision": (8, 700, 2)}
BAG_WORD_RE = re.compile(r"\b[A-Z][a-z]+\b|\b\w{6,}\b|\b[A-Z]{2,}\b")  # IntentClassifier's topic_keywords
PROBES = ("ok what's next on the list", "can you look at the thing from before", "what time is it in tokyo")


def bag_words(text: str) -> set[str]:
    return {w.lower() for w in BAG_WORD_RE.findall(text)}


async def sample_pool(db: Any, agent: str, gold_type: str, *, n: int, n_second: int, seed: int) -> list[Any]:
    """The candidate memories, two strata where the type has them (fact: other / tier-1;
    episode: untagged / tagged), in a fixed md5(id || seed) order."""
    from sqlalchemy import text

    sql = {
        "fact": (
            "SELECT id::text AS id, content, category FROM heart.facts WHERE agent_id = :a AND active "
            "AND superseded_by IS NULL AND embedding IS NOT NULL AND length(content) BETWEEN 40 AND 400 "
            "AND (coalesce(category, '') IN ('rule', 'person', 'preference')) = :second "
            "ORDER BY md5(id::text || :seed) LIMIT :n"
        ),
        "episode": (
            "SELECT id::text AS id, summary AS content, CASE WHEN coalesce(cardinality(tags), 0) > 0 "
            "THEN 'tagged' ELSE 'untagged' END AS category FROM heart.episodes WHERE agent_id = :a "
            "AND (active OR ended_at IS NOT NULL) AND outcome IS DISTINCT FROM 'abandoned' "
            "AND embedding IS NOT NULL AND length(summary) BETWEEN 120 AND 3000 "
            "AND (coalesce(cardinality(tags), 0) > 0) = :second ORDER BY md5(id::text || :seed) LIMIT :n"
        ),
        "decision": (
            "SELECT id::text AS id, description AS content, coalesce(context, '') AS context, "
            "coalesce(category, '') AS category FROM brain.decisions WHERE agent_id = :a "
            "AND superseded_by IS NULL AND embedding IS NOT NULL AND length(description) BETWEEN 40 AND 600 "
            "ORDER BY md5(id::text || :seed) LIMIT :n"
        ),
    }[gold_type]
    rows = []
    async with db.session() as s:
        for second, k in ((False, n), (True, n_second)):
            if k > 0 and not (gold_type == "decision" and second):
                rows += (await s.execute(text(sql), {"a": agent, "seed": str(seed), "n": k, "second": second})).all()
    return rows


async def generate(
    stack: Any,
    client: Any,
    *,
    gold_type: str,
    n: int,
    n_second: int,
    seed: int,
    out: Path,
    ledger: CostLedger,
    run: str,
) -> dict[str, Any]:
    from nous_eval._oat_preamble import RateLimiter
    from nous_eval.served.preturn import build_one

    rows = await sample_pool(stack.db, stack.settings.agent_id, gold_type, n=n, n_second=n_second, seed=seed)
    stats: dict[str, int] = defaultdict(int)
    stats["sampled"] = len(rows)
    if gold_type == "fact":  # facts in context whatever the query cannot tell arms apart
        prompts = [(await build_one(stack, p, str(uuid.uuid4()), turn_number=2))["system_prompt"] for p in PROBES]
        kept = [r for r in rows if not all(r.content.strip()[:60] in p for p in prompts)]
        stats["always_in_context"] = len(rows) - len(kept)
        rows = kept
    per_call, chars, max_shared = SPECS[gold_type]
    limiter = RateLimiter(min_interval_s=1.0)
    out_rows: list[dict[str, Any]] = []
    prefix = gold_type[0].upper()
    for b in range(0, len(rows), per_call):
        batch = rows[b : b + per_call]
        listing = "\n".join(f"f{j + 1}: {' '.join(_shown(r).split())[:chars]}" for j, r in enumerate(batch))
        data = await llm_json(
            client,
            limiter,
            GEN_SYSTEM,
            PROMPTS[gold_type].format(items=listing),
            model=PRIMARY_MODEL,
            max_tokens=4000,
            ledger=ledger,
            purpose=f"gold:{gold_type}",
            run=run,
        )
        qs = {it.get("id"): it.get("question") for it in (data.get("items") or [])} if isinstance(data, dict) else {}
        for j, r in enumerate(batch):
            q = (qs.get(f"f{j + 1}") or "").strip()
            if not q:
                stats["llm_null"] += 1
                continue
            if not 20 <= len(q) <= 90:
                stats["bad_length"] += 1
                continue
            shared = bag_words(q) & bag_words(_shown(r))
            if len(shared) > max_shared:
                stats["too_much_overlap"] += 1
                continue
            out_rows.append(
                {
                    "id": f"{prefix}{len(out_rows):03d}",
                    "question": q,
                    "gold": [r.id],
                    "gold_type": gold_type,
                    "gold_category": r.category,
                    "gold_content": r.content,
                    "shared_bag_words": len(shared),
                }
            )
    out.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in out_rows), encoding="utf-8")
    stats["kept"] = len(out_rows)
    return dict(stats)


def _shown(r: Any) -> str:
    ctx = getattr(r, "context", "") or ""
    return f"{r.content} Why: {ctx}" if ctx else r.content
