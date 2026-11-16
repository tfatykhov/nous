"""docs/reference/rest-api.md says, on the row of every route built with ``owner_guard.route``, that it requires the
owner's or the proxy's secret. No database: the app is built with stand-ins and the reference is read."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from nous.api.rest import create_app
from nous.config import Settings

DOC = Path(__file__).resolve().parents[1] / "docs" / "reference" / "rest-api.md"
SENTENCE = "Requires the owner's or the proxy's secret"


def _guarded(tmp_path) -> set[tuple[str, str]]:
    settings = Settings(_env_file=None, workspace_dir=str(tmp_path))
    app = create_app(
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(schedules=None),
        SimpleNamespace(),
        SimpleNamespace(),
        settings,
    )
    return {
        (method, route.path)
        for route in app.routes
        if getattr(route, "methods", None) and getattr(route.endpoint, "owner_guarded", False)
        for method in route.methods - {"HEAD"}
    }


def _rows() -> dict[tuple[str, str], str]:
    rows = {}
    for line in DOC.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\| (GET|POST|PUT|PATCH|DELETE) \| `([^`]+)` \|", line)
        if match:
            rows[(match.group(1), match.group(2))] = line
    return rows


def test_every_owner_route_has_a_row_that_says_it_requires_the_owners_or_the_proxys_secret(tmp_path) -> None:
    guarded = _guarded(tmp_path)
    assert len(guarded) >= 30
    rows = _rows()
    missing = sorted(key for key in guarded if key not in rows)
    unsaid = sorted(key for key in guarded if key in rows and SENTENCE not in rows[key])
    assert (missing, unsaid) == ([], [])
