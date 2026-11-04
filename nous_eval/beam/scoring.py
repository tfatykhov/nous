# ruff: noqa: E501 -- the prompts below are upstream text, kept verbatim.
"""BEAM's evaluator, ported from the upstream repository.

Source: https://github.com/mohammadtavakoli78/BEAM (MIT License, Copyright (c)
2025 Mohammad Tavakoli), ``src/evaluation/compute_metrics.py``,
``src/evaluation/run_evaluation.py``, ``src/evaluation/report_results.py`` and the
prompts ``unified_llm_judge_base_prompt`` and ``answer_generation_for_rag`` in
``src/prompts.py``. The judge model upstream is ``gpt-4.1-mini`` at temperature 0.

What upstream does, kept here on purpose so scores stay comparable:
  * Every ability: one judge call per rubric item, score in {0, 0.5, 1}; the
    question's ``llm_judge_score`` is the mean over its rubric.
  * Every ability except event ordering adds ``int(score)``, so a 0.5 counts as 0.
    Event ordering adds ``float(score)``. The raw per-item scores are kept in
    ``rubric_scores`` so another aggregation can be computed without new calls.
  * The judge prompt's ``<question>`` placeholder is never filled upstream; it
    is left unfilled here too.
  * Event ordering also aligns the answer's lines (``answer.split("\\n")``,
    blank lines included) with the rubric items by pairwise LLM equivalence,
    then reports precision/recall/F1, Kendall tau-b and ``final_score = tau_norm * f1``.
    ``aligned_events`` is the number of rubric events the answer matched (tp).
  * The report's per-ability score is ``llm_judge_score``, except event ordering,
    which reports ``tau_norm``; abilities are averaged per conversation first,
    then across conversations.

Departures: upstream also calls ``extract_facts`` for event ordering and discards
the result (no effect on any score), which is skipped. A tau-b that is undefined
(scipy returns NaN, e.g. one item in all) gives ``tau_norm`` 0 instead of NaN.
An unparseable judge reply raises upstream; here the question is recorded with
an ``error`` and left out of the means.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable
from statistics import mean
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# A judge takes chat messages and returns the reply text.
Judge = Callable[[list[dict[str, str]]], Awaitable[str]]

JUDGE_PROMPT = """
You are an expert evaluator tasked with judging whether the LLM's response demonstrates compliance with the specified RUBRIC CRITERION.

## EVALUATION INPUTS
- QUESTION (what the user asked): <question>
- RUBRIC CRITERION (what to check): <rubric_item>
- RESPONSE TO EVALUATE: <llm_response>

## EVALUATION RUBRIC:
The rubric defines a specific requirement, constraint, or expected behavior that the LLM response should demonstrate.\x20

**IMPORTANT**: Pay careful attention to whether the rubric specifies:
- **Positive requirements** (things the response SHOULD include/do)
- **Negative constraints** (things the response SHOULD NOT include/do, often indicated by "no", "not", "avoid", "absent")

## RESPONSIVENESS REQUIREMENT (anchored to the QUESTION)
A compliant response must be **on-topic with respect to the QUESTION** and attempt to answer it.
- If the response does not address the QUESTION, score **0.0** and stop.
- For negative constraints, both must hold: (a) the response is responsive to the QUESTION, and (b) the prohibited element is absent.

## SEMANTIC TOLERANCE RULES:
Judge by meaning, not exact wording.
- Accept **paraphrases** and **synonyms** that preserve intent.
- **Case/punctuation/whitespace** differences must be ignored.
- **Numbers/currencies/dates** may appear in equivalent forms (e.g., “$68,000”, “68k”, “68,000 USD”, or “sixty-eight thousand dollars”). Treat them as equal when numerically equivalent.
- If the rubric expects a number or duration, prefer **normalized comparison** (extract and compare values) over string matching.

## STYLE NEUTRALITY (prevents style contamination):
Ignore tone, politeness, length, and flourish unless the rubric explicitly requires a format/structure (e.g., “itemized list”, “no citations”, “one sentence”).
- Do **not** penalize hedging, voice, or verbosity if content satisfies the rubric.
- Only evaluate format when the rubric **explicitly** mandates it.

## SCORING SCALE:
- **1.0 (Complete Compliance)**: Fully complies with the rubric criterion.
  - Positive: required element present, accurate, properly executed (allowing semantic equivalents).
  - Negative: prohibited element **absent** AND response is **responsive**.
\x20\x20
- **0.5 (Partial Compliance)**: Partially complies.
  - Positive: element present but minor inaccuracies/incomplete execution.
  - Negative: generally responsive and mostly avoids the prohibited element but with minor/edge violations.
\x20\x20
- **0.0 (No Compliance)**: Fails to comply.
  - Positive: required element missing or incorrect.
  - Negative: prohibited element present **or** response is non-responsive/evasive even if the element is absent.

## EVALUATION INSTRUCTIONS:
1. **Understand the Requirement**: Determine if the rubric is asking for something to be present (positive) or absent (negative/constraint).

2. **Parse Compound Statements**: If the rubric contains multiple elements connected by "and" or commas, evaluate whether:
   - **All elements** must be present for full compliance (1.0)
   - **Some elements** present indicates partial compliance (0.5)
   - **No elements** present indicates no compliance (0.0)
\x20\x20\x20
3. **Check Compliance**:\x20
   - For positive requirements: Look for the presence and quality of the required element
   - For negative constraints: Look for the absence of the prohibited element

4. **Assign Score**: Based on compliance with the specific rubric criterion according to the scoring scale above.

5. **Provide Reasoning**: Explain whether the rubric criterion was satisfied and justify the score.

## OUTPUT FORMAT:
Return your evaluation in JSON format with two fields:

{
   "score": [your score: 1.0, 0.5, or 0.0],
   "reason": "[detailed explanation of whether the rubric criterion was satisfied and why this justified the assigned score]"
}

NOTE: ONLY output the json object, without any explanation before or after that
"""

# Upstream's answer prompt for retrieval methods (answer_generation_for_rag).
ANSWER_PROMPT = """
You are an assistant that MUST answer questions using ONLY the information provided in the context below.\x20

STRICT INSTRUCTIONS:
1. Answer ONLY based on the provided context
2. Do NOT use your internal knowledge

CONTEXT:
<context>

QUESTION:
<question>

ANSWER REQUIREMENTS:
- Be direct and concise
- Only output the answer to the question without any explanation\x20

RESPONSE:
"""

_EQUIVALENCE_SYSTEM = """
            You are a binary classifier.
            If the TWO snippets describe the SAME event/fact, reply **YES**
            Otherwise reply **NO**. No extra words.
            DO NOT provide any exaplanation.
        """


def fill(template: str, values: dict[str, str]) -> str:
    """Replace ``<name>`` placeholders in one pass, so a value is never re-substituted."""
    pattern = re.compile("|".join(re.escape(f"<{k}>") for k in values))
    return pattern.sub(lambda m: values[m.group(0)[1:-1]], template)


def parse_json_response(response: str) -> Any:
    """Upstream's parser (fenced, bare, then the first ``{...}``/``[...]``), with
    ``parse_llm_json`` standing in for upstream's ``json_repair`` fallback."""
    from nous.handlers import parse_llm_json

    text = response.strip()
    if text.startswith("```"):
        m = re.search(r"```(?:json)?\s*(\[.*\]|\{.*\})\s*```", text, re.DOTALL)
        if m:
            text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"(\{.*?\}|\[.*?\])", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return parse_llm_json(response)


async def judge_rubric(judge: Judge, rubric: list[str], answer: str, *, float_scores: bool) -> dict[str, Any]:
    """One judge call per rubric item; mean of ``int(score)`` (``float`` for event ordering)."""
    responses: list[Any] = []
    raw: list[float] = []
    total = 0.0
    for item in rubric:
        prompt = fill(JUDGE_PROMPT, {"rubric_item": item, "llm_response": answer})
        reply = parse_json_response(await judge([{"role": "user", "content": prompt}]))
        score = float(reply["score"])
        raw.append(score)
        total += score if float_scores else int(score)
        responses.append(reply)
    return {"llm_judge_score": total / len(rubric), "rubric_scores": raw, "llm_judge_responses": responses}


async def llm_equivalence(judge: Judge, first: str, second: str) -> bool:
    user = f"""First snippet: {first} \n
                       Second snippet: {second}
                    """
    reply = await judge([{"role": "system", "content": _EQUIVALENCE_SYSTEM}, {"role": "user", "content": user}])
    return "yes" in reply.lower()


async def align_with_llm(reference: list[str], system: list[str], judge: Judge) -> list[str]:
    """Replace each system item by the first unused reference item the judge calls equivalent."""
    used: set[int] = set()
    out: list[str] = []
    for s in system:
        match = None
        for i, r in enumerate(reference):
            if i not in used and await llm_equivalence(judge, r, s):
                match = i
                break
        if match is None:
            out.append(s)
        else:
            out.append(reference[match])
            used.add(match)
    return out


def kendall_tau_b(x: list[float], y: list[float]) -> float | None:
    """Kendall's tau-b with tie correction; None where it is undefined."""
    n = len(x)
    concordant = discordant = ties_x = ties_y = 0
    for i in range(n):
        for j in range(i + 1, n):
            dx, dy = x[i] - x[j], y[i] - y[j]
            if dx == 0 and dy == 0:
                ties_x += 1
                ties_y += 1
            elif dx == 0:
                ties_x += 1
            elif dy == 0:
                ties_y += 1
            elif (dx > 0) == (dy > 0):
                concordant += 1
            else:
                discordant += 1
    pairs = n * (n - 1) / 2
    denom = math.sqrt((pairs - ties_x) * (pairs - ties_y))
    if denom == 0:
        return None
    return (concordant - discordant) / denom


async def event_ordering_score(reference: list[str], system: list[str], judge: Judge) -> dict[str, Any]:
    system_canon = await align_with_llm(reference, system, judge)
    tp = len(set(reference) & set(system_canon))
    fp = len([x for x in system_canon if x not in reference])
    fn = len([x for x in reference if x not in system_canon])
    precision = tp / (tp + fp) if tp + fp else 0
    recall = tp / (tp + fn) if tp + fn else 0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0

    union = list(dict.fromkeys(reference + system_canon))
    tie_rank = len(union) + 1

    def to_rank(seq: list[str]) -> list[float]:
        r = {item: i + 1 for i, item in enumerate(seq)}
        return [r.get(u, tie_rank) for u in union]

    tau = kendall_tau_b(to_rank(reference), to_rank(system_canon))
    tau_norm = (tau + 1) / 2 if tau is not None else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "tau_norm": tau_norm,
        "final_score": tau_norm * f1,
        "aligned_events": tp,
        "n_reference": len(reference),
        "n_system": len(system),
    }


async def score_answer(judge: Judge, ability: str, rubric: list[str], answer: str) -> dict[str, Any]:
    """Upstream ``evaluate_<ability>``; adds ``beam_score``, the number the upstream report uses."""
    if ability == "event_ordering":
        result = await event_ordering_score(rubric, answer.split("\n"), judge)
        result.update(await judge_rubric(judge, rubric, answer, float_scores=True))
        result["beam_score"] = result["tau_norm"]
    else:
        result = await judge_rubric(judge, rubric, answer, float_scores=False)
        result["beam_score"] = result["llm_judge_score"]
    return result


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-ability mean of per-conversation means (upstream report order), then their mean."""
    per_conv: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    eo_final: list[float] = []
    eo_aligned: list[int] = []
    for r in rows:
        if r.get("error") or r.get("score") is None:
            continue
        per_conv[r["ability"]][r["conversation"]].append(r["score"]["beam_score"])
        if r["ability"] == "event_ordering":
            eo_final.append(r["score"]["final_score"])
            eo_aligned.append(r["score"]["aligned_events"])
    abilities = {
        a: {"score": mean(mean(v) for v in convs.values()), "n_questions": sum(len(v) for v in convs.values())}
        for a, convs in per_conv.items()
    }
    return {
        "abilities": abilities,
        "overall": mean(a["score"] for a in abilities.values()) if abilities else None,
        "event_ordering": {
            "final_score_mean": mean(eo_final) if eo_final else None,
            "aligned_events_total": sum(eo_aligned),
            "aligned_events_per_question": eo_aligned,
        },
    }


class OpenAIJudge:
    """Chat-completions judge over httpx (upstream: ChatOpenAI, gpt-4.1-mini, temperature 0)."""

    URL = "https://api.openai.com/v1/chat/completions"

    def __init__(self, api_key: str, model: str = "gpt-4.1-mini", max_retries: int = 6) -> None:
        if not api_key:
            raise SystemExit("OPENAI_API_KEY is required for the BEAM judge")
        self.model = model
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(timeout=120, headers={"Authorization": f"Bearer {api_key}"})

    async def __call__(self, messages: list[dict[str, str]]) -> str:
        body = {"model": self.model, "temperature": 0, "messages": messages}
        for attempt in range(self._max_retries):
            resp = await self._client.post(self.URL, json=body)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self._max_retries - 1:
                await asyncio.sleep(2 ** (attempt + 2))
                continue
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"] or ""
        raise RuntimeError("unreachable")

    async def close(self) -> None:
        await self._client.aclose()
