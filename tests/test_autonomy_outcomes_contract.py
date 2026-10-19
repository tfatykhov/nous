"""Cross-language contract: the dashboard lists exactly the outcomes the scorer writes.

``task_score_gatherer``/``task_scoring`` write ``scoring.task_scores.outcome``; ``dashboard-app/src/lib/autonomy.ts``
decides which outcome keys the Autonomy view shows. A new outcome the TS list lacks would be counted but never shown.
"""

from __future__ import annotations

import re
from pathlib import Path

from nous.observability import task_scoring

AUTONOMY_TS = Path(__file__).resolve().parent.parent / "dashboard-app" / "src" / "lib" / "autonomy.ts"


def _ts_array(name: str) -> list[str]:
    source = AUTONOMY_TS.read_text(encoding="utf-8")
    match = re.search(rf"export const {name}\s*=\s*\[(.*?)\]\s*as const", source, re.S)
    assert match, f"{name} not found in autonomy.ts"
    return re.findall(r"'([a-z_]+)'", match.group(1))


def test_the_dashboards_outcomes_are_the_scorers_outcomes():
    listed = _ts_array("OUTCOMES")
    assert len(listed) == len(set(listed))
    assert set(listed) == set(task_scoring.OUTCOMES)


def test_every_outcome_has_a_label_and_checked_follows_verified():
    source = AUTONOMY_TS.read_text(encoding="utf-8")
    labels = re.search(r"export const OUTCOME_LABEL[^{]*\{(.*?)\};", source, re.S)
    assert labels
    assert set(re.findall(r"^\s*([a-z_]+):", labels.group(1), re.M)) == set(task_scoring.OUTCOMES)
    assert "done_checked: 'Done, checked'" in labels.group(1)
    listed = _ts_array("OUTCOMES")
    assert listed.index("done_checked") == listed.index("done_verified") + 1
