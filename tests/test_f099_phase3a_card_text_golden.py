"""F099 Phase 3a: the server's escaping rule and the web renderer's test share ONE golden vector."""

from __future__ import annotations

import json
import re
from pathlib import Path

from test_f099_phase3a_card_text import GOLDEN_IN, GOLDEN_OUT

from nous.brain import continuation

WEB_TEST = Path(__file__).resolve().parents[1] / "dashboard-app" / "src" / "companion" / "intentionCard.test.ts"


def _constant(name: str) -> str:
    match = re.search(rf'^const {name} = ("(?:[^"\\]|\\.)*");$', WEB_TEST.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"{name} is no longer a one-line JSON string in {WEB_TEST.name}"
    return json.loads(match.group(1))


def test_the_web_test_and_the_server_share_the_golden_vector():
    web_in, web_out = _constant("GOLDEN_IN"), _constant("GOLDEN_OUT")
    assert continuation.card_text(web_in, 4000) == web_out
    assert (web_in, web_out) == (GOLDEN_IN, GOLDEN_OUT)  # and it is the vector of the Python unit test too
