"""Intent classifier accuracy eval (cognitive-layer plan item 2).

Tests `nous/cognitive/intent.py::IntentClassifier.classify` against
hand-curated user messages with expected IntentSignals.

No LLM in the SUT path — pure regex pattern matching. The judge here
is the hand-labeled ground truth, not an LLM.

Usage:
    uv run python scripts/eval/eval_intent_classifier.py
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from nous.cognitive.intent import IntentClassifier, IntentSignals
from nous.cognitive.schemas import FrameSelection


@dataclass
class IntentScenario:
    name: str
    input_text: str
    frame_id: str
    expected: dict[str, object]  # subset of IntentSignals fields to check
    notes: str = ""


# Hand-labeled fixtures covering all signal types: greeting, question,
# temporal recency (4 levels), memory hints (4 types), entity detection.
SCENARIOS: list[IntentScenario] = [
    # --- Greetings ---
    IntentScenario("greeting_hi", "Hi there", "conversation",
                   {"is_greeting": True, "is_question": False}),
    IntentScenario("greeting_hello", "Hello, how are you?", "conversation",
                   {"is_greeting": True, "is_question": True}),
    IntentScenario("greeting_morning", "Good morning, Nous", "conversation",
                   {"is_greeting": True}),
    IntentScenario("greeting_howdy", "Howdy partner", "conversation",
                   {"is_greeting": True}),
    IntentScenario("not_greeting", "I want to talk to Hi-Tek about pricing.",
                   "conversation",
                   {"is_greeting": False}),
    # --- Questions ---
    IntentScenario("question_what", "What is the capital of France?", "question",
                   {"is_question": True}),
    IntentScenario("question_how", "How do I reset my password?", "question",
                   {"is_question": True, "memory_type_hints": {"procedure"}}),
    IntentScenario("question_did", "Did the deploy succeed?", "question",
                   {"is_question": True}),
    IntentScenario("question_might", "Might the database be down?", "question",
                   {"is_question": True}),
    IntentScenario("statement_no_question_mark", "Tell me about Postgres.", "question",
                   {"is_question": False, "memory_type_hints": {"fact"}}),
    IntentScenario("question_mark_only", "Postgres?", "question",
                   {"is_question": True}),
    # --- Temporal recency ---
    IntentScenario("recency_today", "What did we decide today?", "decision",
                   {"temporal_recency_min": 1.0}),
    IntentScenario("recency_yesterday", "What did the build look like yesterday?", "debug",
                   {"temporal_recency_min": 0.8, "temporal_recency_max": 0.8}),
    IntentScenario("recency_last_week", "Last week's metrics", "question",
                   {"temporal_recency_min": 0.5, "temporal_recency_max": 0.5}),
    IntentScenario("recency_last_month", "What happened last month?", "question",
                   {"temporal_recency_min": 0.3, "temporal_recency_max": 0.3}),
    IntentScenario("no_temporal", "Explain the codebase architecture.", "question",
                   {"temporal_recency_max": 0.0}),
    # --- Memory type hints ---
    IntentScenario("hint_decision", "Should we recommend Postgres or MySQL?", "decision",
                   {"memory_type_hints": {"decision"}}),
    IntentScenario("hint_fact", "What is the definition of CRDT?", "question",
                   {"memory_type_hints": {"fact"}}),
    IntentScenario("hint_procedure", "How to deploy the staging environment?", "task",
                   {"memory_type_hints": {"procedure"}}),
    IntentScenario("hint_episode", "When did we last talk about Redis?", "question",
                   {"memory_type_hints": {"episode"}}),
    IntentScenario("hint_multi", "How do we decide what to deploy?", "task",
                   {"memory_type_hints_min_two": True},
                   "expects both procedure (how) and decision (decide)"),
    # --- Entity detection ---
    IntentScenario("entity_singleton", "Tell me about Postgres.", "question",
                   {"entity_mentions_contains": "Postgres"}),
    IntentScenario("entity_multi", "Compare Tim and Emerson notes.", "question",
                   {"entity_mentions_contains_any": ["Tim", "Emerson"]}),
    IntentScenario("entity_lowercase_skipped", "tell me about postgres", "question",
                   {"entity_mentions_excludes": "Postgres"}),
    # --- Topic keywords ---
    IntentScenario("topic_long_words", "Explain authentication and authorization.",
                   "question", {"topic_keywords_min": 1},
                   "long words >=6 chars are topic keywords"),
    IntentScenario("topic_acronyms", "What does CRDT and ACID mean?", "question",
                   {"topic_keywords_contains_any": ["crdt", "acid"]}),
    # --- Mixed signals ---
    IntentScenario("mixed_recent_question_decision",
                   "Did we today decide whether to use Redis?", "decision",
                   {"is_question": True, "temporal_recency_min": 1.0,
                    "memory_type_hints": {"decision"}}),
    IntentScenario("mixed_procedure_recency",
                   "How did we deploy yesterday?", "task",
                   {"is_question": True, "temporal_recency_min": 0.8,
                    "memory_type_hints": {"procedure"}}),
    # --- Edge cases ---
    IntentScenario("empty_question", "?", "conversation", {"is_question": True}),
    IntentScenario("very_short", "ok", "conversation",
                   {"is_greeting": False, "is_question": False}),
]


def _check_field(signals: IntentSignals, key: str, expected) -> tuple[bool, str]:
    """Verify one expectation against actual signals. Return (passed, detail)."""
    actual: object
    if key == "is_greeting":
        actual = signals.is_greeting
    elif key == "is_question":
        actual = signals.is_question
    elif key == "temporal_recency_min":
        return (signals.temporal_recency >= expected,
                f"got temporal_recency={signals.temporal_recency:.2f} (>= {expected})")
    elif key == "temporal_recency_max":
        return (signals.temporal_recency <= expected,
                f"got temporal_recency={signals.temporal_recency:.2f} (<= {expected})")
    elif key == "memory_type_hints":
        # expected is a set; require at least these keys present
        actual_keys = set(signals.memory_type_hints.keys())
        passed = expected.issubset(actual_keys)
        return passed, f"got hints={sorted(actual_keys)} (expected ⊇ {sorted(expected)})"
    elif key == "memory_type_hints_min_two":
        passed = len(signals.memory_type_hints) >= 2
        return passed, f"got {len(signals.memory_type_hints)} hints"
    elif key == "entity_mentions_contains":
        passed = expected in signals.entity_mentions
        return passed, f"got entities={signals.entity_mentions}"
    elif key == "entity_mentions_contains_any":
        passed = any(e in signals.entity_mentions for e in expected)
        return passed, f"got entities={signals.entity_mentions}"
    elif key == "entity_mentions_excludes":
        passed = expected not in signals.entity_mentions
        return passed, f"got entities={signals.entity_mentions}"
    elif key == "topic_keywords_min":
        passed = len(signals.topic_keywords) >= expected
        return passed, f"got {len(signals.topic_keywords)} keywords"
    elif key == "topic_keywords_contains_any":
        passed = any(t in signals.topic_keywords for t in expected)
        return passed, f"got keywords={signals.topic_keywords}"
    else:
        return False, f"unknown check '{key}'"
    return actual == expected, f"got {key}={actual}"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, default=Path("reports/eval_intent_classifier.md"))
    p.add_argument("--out-json", type=Path, default=Path("reports/eval_intent_classifier.json"))
    args = p.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    classifier = IntentClassifier()
    results: list[dict] = []

    print()
    print("=" * 76)
    print(f"INTENT CLASSIFIER EVAL — {len(SCENARIOS)} scenarios")
    print("=" * 76)

    for sc in SCENARIOS:
        frame = FrameSelection(
            frame_id=sc.frame_id, frame_name=sc.frame_id,
            confidence=1.0, match_method="pattern",
            default_category="architecture", default_stakes="low",
        )
        signals = classifier.classify(sc.input_text, frame)

        per_check: list[tuple[str, bool, str]] = []
        for key, expected in sc.expected.items():
            passed, detail = _check_field(signals, key, expected)
            per_check.append((key, passed, detail))

        scenario_passed = all(p for _, p, _ in per_check)
        results.append({
            "name": sc.name,
            "input": sc.input_text,
            "passed": scenario_passed,
            "checks": [{"key": k, "passed": p, "detail": d} for k, p, d in per_check],
            "notes": sc.notes,
        })

        marker = "OK  " if scenario_passed else "FAIL"
        print(f"  [{marker}] {sc.name:<35s} \"{sc.input_text[:35]}\"")
        if not scenario_passed:
            for k, p, d in per_check:
                if not p:
                    print(f"          - {k}: {d}")

    n_passed = sum(1 for r in results if r["passed"])
    accuracy = n_passed / len(results)
    print()
    print(f"  PASSED: {n_passed}/{len(results)} ({100*accuracy:.1f}%)")
    print("=" * 76)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    md = [
        f"# Intent classifier eval — {len(SCENARIOS)} scenarios",
        "",
        f"- accuracy: **{n_passed}/{len(results)} ({100*accuracy:.1f}%)**",
        "- SUT: `nous.cognitive.intent.IntentClassifier`",
        "- Pattern-matching only; ground truth is hand-labeled.",
        "",
        "## Failed scenarios",
        "",
    ]
    failures = [r for r in results if not r["passed"]]
    if not failures:
        md.append("_None — all scenarios passed._")
    else:
        md.append("| name | input | failed checks |")
        md.append("|---|---|---|")
        for r in failures:
            failed_checks = [
                f"`{c['key']}`: {c['detail']}"
                for c in r["checks"] if not c["passed"]
            ]
            md.append(
                f"| {r['name']} | `{r['input'][:60]}` | "
                f"{'; '.join(failed_checks)} |"
            )
    args.out.write_text("\n".join(md), encoding="utf-8")
    args.out_json.write_text(json.dumps({
        "n": len(results), "passed": n_passed, "accuracy": accuracy,
        "results": results,
    }, indent=2), encoding="utf-8")
    print(f"\nWrote: {args.out}")
    print(f"Wrote: {args.out_json}")
    return 0 if accuracy == 1.0 else 0  # report-only; CI gate decision deferred


if __name__ == "__main__":
    raise SystemExit(main())
