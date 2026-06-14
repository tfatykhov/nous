"""Aggregate BEAM per-conversation evaluation-result.json into per-category +
overall scores. Used to compare a run vs the prior baseline snapshot.

    PYTHONPATH=. uv run python scripts/diag/beam_aggregate.py <dir-with-conv-subdirs>
"""

from __future__ import annotations

import glob
import json
import os
import sys
from statistics import mean


def aggregate(root: str) -> tuple[dict[str, float], float, float, dict[str, int]]:
    per_cat: dict[str, list[float]] = {}
    convs = sorted(d for d in glob.glob(os.path.join(root, "*")) if os.path.isdir(d))
    for conv in convs:
        f = os.path.join(conv, "evaluation-result.json")
        if not os.path.exists(f):
            continue
        data = json.load(open(f, encoding="utf-8"))
        for cat, items in data.items():
            scores = [it["llm_judge_score"] for it in items
                      if isinstance(it, dict) and "llm_judge_score" in it]
            per_cat.setdefault(cat, []).extend(scores)
    cat_means = {c: (mean(v) if v else 0.0) for c, v in per_cat.items()}
    macro = mean(cat_means.values()) if cat_means else 0.0           # mean of category means
    all_q = [s for v in per_cat.values() for s in v]
    micro = mean(all_q) if all_q else 0.0                            # mean over all questions
    counts = {c: len(v) for c, v in per_cat.items()}
    return cat_means, macro, micro, counts


def main() -> None:
    root = sys.argv[1] if len(sys.argv) > 1 else "reports/beam/answers/100K"
    cat_means, macro, micro, counts = aggregate(root)
    print(f"=== {root} ===")
    for c in sorted(cat_means):
        print(f"  {c:<26} {cat_means[c]:.3f}  (n={counts[c]})")
    print(f"  {'MACRO (mean of cats)':<26} {macro:.3f}")
    print(f"  {'MICRO (mean of all Q)':<26} {micro:.3f}")


if __name__ == "__main__":
    main()
