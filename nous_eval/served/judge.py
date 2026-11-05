"""LLM relevance judge with one label cache shared by every arm, and a cost ledger.

A label is keyed by what the judge saw: (entry point, the query or prompt as shown, the item's
type, the item text as shown), hashed. Two arms that render the same item for the same query
share its label; an item whose rendered text changed is judged again. Labels are arm-blind: the
judge sees one query and its items in a fixed order, never which arm produced them.

The prompts are the ones the earlier harnesses used, verbatim: the recall_deep prompt from the
fusion/spreading A/Bs, the pre-turn prompt from the query-mode A/B. Calls go through
``nous.api.anthropic_client`` with the Claude Code preamble (``nous_eval._oat_preamble``), as
every eval script does; credentials come from the process environment and are never logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger("nous_eval.served")

PRIMARY_MODEL = "claude-sonnet-5-5"
AGREEMENT_MODEL = "claude-opus-5-5"

# $ per million tokens: input, output, cache write, cache read (Anthropic list prices, 2026-10).
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.20),
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
}

RECALL_SYSTEM = "You are a strict, impartial relevance assessor for search results. Reply with JSON only."
RECALL_PROMPT = """An AI agent (Nous) searched its own long-term memory with the SEARCH QUERY below. It wrote the \
query itself while working on a task for its owner. The memory system returned the MEMORY ITEMS below \
(facts, conversation episodes, transcript chunks, decisions, procedures). Label each item.

RELEVANT (true): the item is a useful result for this search. It is about the specific topic, entity, project, \
system, event, decision, person or procedure the query asks about, or it directly helps answer the query.
NOT RELEVANT (false): it is about something else, it only shares generic words or a broad theme with the query, \
or it would only distract. Judge topical usefulness for this query, not freshness or quality.

SEARCH QUERY:
<<<
{query}
>>>

MEMORY ITEMS:
{items}

Reply as JSON with one entry per item id: {{"labels": {{"i1": true, "i2": false, ...}}}}"""

PRETURN_SYSTEM = "You are a strict, impartial relevance assessor. Reply with JSON only."
PRETURN_PROMPT = """An AI agent (Nous) received the TASK PROMPT below. Before it started, its memory system put the \
MEMORY ITEMS below into its context. Label each item.

RELEVANT (true): knowing this item would plausibly help the agent do THIS task well. It concerns the same project, \
system, component, check, person or procedure the task is about, or it states a rule, preference or lesson that \
applies to how this task should be done.
NOT RELEVANT (false): it is about something else, it only shares generic words with the task, or it would only \
distract. The memory may be older than the task: judge topical usefulness, not freshness.

TASK PROMPT:
<<<
{prompt}
>>>

MEMORY ITEMS:
{items}

Reply as JSON with one entry per item id: {{"labels": {{"i1": true, "i2": false, ...}}}}"""

# entry point -> (system, template, template field, item chars shown, items per call)
JUDGE_SPECS: dict[str, tuple[str, str, str, int, int]] = {
    "recall": (RECALL_SYSTEM, RECALL_PROMPT, "query", 800, 25),
    "preturn": (PRETURN_SYSTEM, PRETURN_PROMPT, "prompt", 700, 30),
}


def shown_prompt(entry: str, text: str) -> str:
    """The query or prompt exactly as the judge sees it (a long pre-turn prompt keeps head and tail)."""
    if entry == "preturn" and len(text) > 6000:
        return text[:4000] + "\n[... middle omitted ...]\n" + text[-2000:]
    return text


def label_key(entry: str, prompt_shown: str, item_type: str, item_shown: str) -> str:
    h = hashlib.sha256("\x00".join((entry, prompt_shown, item_type, item_shown)).encode("utf-8"))
    return h.hexdigest()[:32]


def rendered_keys(row: dict) -> list[str]:
    """A row's rendered items as ``type:id``, in render order (recall rows carry dicts)."""
    return [x["k"] if isinstance(x, dict) else x for x in row.get("rendered") or []]


def units_for_rows(entry: str, rows: list[dict]) -> list[dict]:
    """One judge unit per row: its prompt as shown and its rendered items as shown, in render
    order, each with its label key."""
    chars = JUDGE_SPECS[entry][3]
    units = []
    for r in rows:
        if r.get("error"):
            continue
        p = shown_prompt(entry, r["text"])
        items = []
        for k in rendered_keys(r):
            t = k.split(":", 1)[0]
            shown = (r["contents"].get(k) or "")[:chars].strip()
            items.append({"item": k, "type": t, "shown": shown, "key": label_key(entry, p, t, shown)})
        units.append({"row": r["key"], "prompt": p, "items": items})
    return units


class LabelCache:
    """Append-only JSONL of {k, relevant, model, run}; the first label for a key wins."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.labels: dict[str, bool] = {}
        for d in read_jsonl(path):
            self.labels.setdefault(d["k"], bool(d["relevant"]))

    def get(self, k: str) -> bool | None:
        return self.labels.get(k)

    def put(self, f, k: str, value: bool, model: str, run: str) -> None:
        if k in self.labels:
            return
        self.labels[k] = value
        f.write(json.dumps({"k": k, "relevant": value, "model": model, "run": run}) + "\n")


def cache_path(data_dir: Path, model: str) -> Path:
    return data_dir / "judge" / f"{model}.jsonl"


def read_jsonl(path: Path) -> list[dict]:
    """Rows of a JSONL file; a partial trailing line (a writer mid-append) is skipped."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


class CostLedger:
    """One JSONL row per LLM call: purpose, run, model and the API's usage counts."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def add(self, purpose: str, run: str, model: str, usage: dict | None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "purpose": purpose,
                        "run": run,
                        "model": model,
                        "usage": usage or {},
                    }
                )
                + "\n"
            )

    def summary(self, run: str | None = None) -> dict[str, Any]:
        """Calls, tokens and list-price dollars per model, for one run or all."""
        out: dict[str, Any] = {}
        for r in read_jsonl(self.path):
            if run is not None and r.get("run") != run:
                continue
            m = out.setdefault(r["model"], {"calls": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0})
            u = r.get("usage") or {}
            m["calls"] += 1
            m["input"] += int(u.get("input_tokens") or 0)
            m["output"] += int(u.get("output_tokens") or 0)
            m["cache_write"] += int(u.get("cache_creation_input_tokens") or 0)
            m["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
        total = 0.0
        for model, m in out.items():
            p = PRICES.get(model)
            m["usd"] = (
                None
                if p is None
                else round(
                    (m["input"] * p[0] + m["output"] * p[1] + m["cache_write"] * p[2] + m["cache_read"] * p[3]) / 1e6, 4
                )
            )
            total += m["usd"] or 0.0
        return {"by_model": out, "usd_total": round(total, 4)}


async def make_llm_client() -> Any:
    """The Anthropic client every eval script uses; credentials from the process environment."""
    from nous.api.anthropic_client import create_client
    from nous.config import Settings
    from nous_eval.env_pin import hidden_env

    with hidden_env():
        client = create_client(Settings(_env_file=None))
    await client.start()
    return client


async def llm_json(
    client: Any,
    limiter: Any,
    system: str,
    user: str,
    *,
    model: str,
    max_tokens: int,
    ledger: CostLedger | None,
    purpose: str,
    run: str,
) -> Any:
    """One call; returns the first parseable JSON object in the reply. temperature 0 and a
    4000-token cap (Sonnet 5.5 thinks by default), as the earlier harnesses sent."""
    from nous_eval._oat_preamble import call_with_retries, with_oat_preamble

    res = await call_with_retries(
        client,
        {
            "model": model,
            "system": with_oat_preamble(system),
            "messages": [{"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": 0.0,
        },
        rate_limiter=limiter,
    )
    if ledger is not None:
        ledger.add(purpose, run, model, getattr(res, "usage", None))
    text = "".join(b.get("text", "") for b in res.content if b.get("type") == "text")
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            return dec.raw_decode(text[m.start() :])[0]
        except json.JSONDecodeError:
            continue
    raise ValueError("no JSON object in the reply")


def sample_units(units: list[dict], n: int, seed: int) -> list[dict]:
    """A fixed-seed sample of n units (all when n <= 0 or n >= len)."""
    if n <= 0 or n >= len(units):
        return units
    keep = set(random.Random(seed).sample(sorted(u["row"] for u in units), n))
    return [u for u in units if u["row"] in keep]


async def judge_units(
    entry: str,
    units: list[dict],
    *,
    model: str,
    cache: LabelCache,
    client: Any,
    ledger: CostLedger,
    run: str,
    concurrency: int = 4,
    min_interval_s: float = 0.5,
) -> dict:
    """Label every (unit, item) the cache lacks. A call that fails three times leaves its items
    unlabelled; a rerun retries them."""
    from nous_eval._oat_preamble import RateLimiter

    system, template, field_name, _, chunk = JUDGE_SPECS[entry]
    jobs = []
    seen: set[str] = set()  # a label pending in several units (same query, same item) is asked once
    for u in units:
        todo = []
        for it in u["items"]:
            if cache.get(it["key"]) is None and it["key"] not in seen:
                seen.add(it["key"])
                todo.append(it)
        jobs += [(u, todo[b : b + chunk]) for b in range(0, len(todo), chunk)]
    counts = defaultdict(int)
    counts["jobs"] = len(jobs)
    limiter = RateLimiter(min_interval_s=min_interval_s)
    sem = asyncio.Semaphore(concurrency)
    cache.path.parent.mkdir(parents=True, exist_ok=True)

    async def run_one(u: dict, items: list[dict], f) -> None:
        listing = "\n".join(f"i{j + 1} [{it['type']}]: {it['shown']}" for j, it in enumerate(items))
        user = template.format(**{field_name: u["prompt"], "items": listing})
        data = None
        async with sem:
            for attempt in range(3):
                try:
                    data = await llm_json(
                        client,
                        limiter,
                        system,
                        user,
                        model=model,
                        max_tokens=4000,
                        ledger=ledger,
                        purpose=f"judge:{entry}",
                        run=run,
                    )
                    break
                except Exception as exc:  # noqa: BLE001 - recorded; a rerun retries
                    logger.warning("judge %s try %d: %s", u["row"], attempt + 1, type(exc).__name__)
        if data is None:
            counts["failed_calls"] += 1
            return
        counts["calls"] += 1
        labels = data.get("labels", {}) if isinstance(data, dict) else {}
        for j, it in enumerate(items):
            v = labels.get(f"i{j + 1}")
            if isinstance(v, bool):
                cache.put(f, it["key"], v, model, run)
                counts["new_labels"] += 1
            else:
                counts["missing_labels"] += 1
        f.flush()
        if counts["calls"] % 50 == 0:
            logger.info("judge %s: %d/%d calls", model, counts["calls"], len(jobs))

    with cache.path.open("a", encoding="utf-8") as f:
        await asyncio.gather(*(run_one(u, items, f) for u, items in jobs))
    return dict(counts)
