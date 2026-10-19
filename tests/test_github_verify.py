"""Scorer v4: a PR check earns credit only from GitHub itself, read by the scorer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from nous.observability import task_scoring as ts
from nous.observability.github_verify import (
    GitHubReader,
    PrVerifier,
    StoredPrEvidence,
    stored_pr_evidence,
)
from nous.observability.task_scoring import PrCheck, TaskSignals

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
DAG_AT = NOW - timedelta(days=2)
AGENT = "cognition-engines-nous-agent"


def leaf(task_id="t1", *, number=5, head=None, dag_at=DAG_AT, node_id="n1", **kw) -> TaskSignals:
    return TaskSignals(
        task_kind="subtask",
        task_id=task_id,
        finished_at=NOW - timedelta(hours=1),
        status="completed",
        detail="completed",
        node_check="passed",
        pr_check=PrCheck(repo="tfatykhov/nous", number=None if head else number, head=head),
        node_id=node_id,
        dag_created_at=dag_at,
        evidence={"node_check_passed": [node_id]},
        **kw,
    )


def pr(number=5, *, author=AGENT, merged_by="tfatykhov", state="closed", opened=DAG_AT + timedelta(hours=1)):
    return {
        "number": number,
        "state": state,
        "user": {"login": author},
        "created_at": opened.isoformat().replace("+00:00", "Z"),
        "merged_at": None if merged_by is None else (NOW - timedelta(hours=2)).isoformat(),
        "merged_by": None if merged_by is None else {"login": merged_by},
    }


class FakeGitHub:
    def __init__(self, pulls=(), *, status=200, branch=None):
        self.pulls = {p["number"]: p for p in pulls}
        self.status = status
        self.branch = branch or []
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={})
        path = request.url.path
        if path.endswith("/pulls"):
            return httpx.Response(200, json=self.branch)
        number = int(path.rsplit("/", 1)[1])
        found = self.pulls.get(number)
        return httpx.Response(200, json=found) if found else httpx.Response(404, json={})


OWNER = "tfatykhov"


async def decide(leaves, github, *, stored=StoredPrEvidence(), logins=(AGENT,), mergers=(OWNER,), cap=60):
    reader = GitHubReader(transport=httpx.MockTransport(github), call_cap=cap)
    try:
        return await PrVerifier(reader, logins, mergers).verify(leaves, stored)
    finally:
        await reader.aclose()


def score(signals):
    return ts.score_leaf(signals, NOW)


async def test_a_pr_the_agent_opened_and_someone_else_merged_verifies():
    [out] = await decide([leaf()], FakeGitHub([pr()]))
    assert out.v2_verified and not out.v2_hold
    assert out.evidence["v2_pr_merged"] == ["tfatykhov/nous#5"] and out.evidence["v2_pr"] == ["tfatykhov/nous#5"]
    assert (score(out).outcome, score(out).score) == ("done_verified", 1.0)


@pytest.mark.parametrize(
    ("pull", "reason"),
    [
        (pr(merged_by=AGENT), "merged by the agent"),  # the bot merging its own PR confirms nothing
        (pr(merged_by="Cognition-Engines-Nous-Agent"), "merged by the agent"),  # logins compare case-insensitively
        (pr(author="tfatykhov"), "not opened by the agent"),  # someone else's PR
        (pr(opened=DAG_AT - timedelta(minutes=1)), "opened before the DAG"),  # an older PR is not this work
        (pr(merged_by=None, state="closed"), "closed without a merge"),
    ],
)
async def test_what_earns_nothing(pull, reason):
    [out] = await decide([leaf()], FakeGitHub([pull]))
    assert not out.v2_verified and not out.v2_hold
    assert out.evidence["v2_rejected"] == [reason]
    assert score(out).outcome == "done_self"


async def test_a_pr_that_does_not_exist_earns_nothing():
    [out] = await decide([leaf(number=6)], FakeGitHub([pr()]))
    assert out.evidence["v2_rejected"] == ["no such PR"] and not out.v2_verified


async def test_an_open_pr_holds_the_row_provisional_for_a_bounded_time():
    [out] = await decide([leaf()], FakeGitHub([pr(merged_by=None, state="open")]))
    assert out.v2_hold and not out.v2_verified and out.evidence["v2_pending"] == ["open"]
    old = ts.SETTLE_DAYS + 1
    assert ts.score_leaf(_aged(out, days=old), NOW).settled is False  # past the settle age, still held
    limit = ts.SETTLE_DAYS + ts.V2_HOLD_EXTRA_DAYS
    assert ts.score_leaf(_aged(out, days=limit), NOW).settled is True  # then it settles with what it has


def _aged(signals: TaskSignals, *, days: int) -> TaskSignals:
    from dataclasses import replace

    return replace(signals, finished_at=NOW - timedelta(days=days))


@pytest.mark.parametrize("status", [500, 403, 429])
async def test_github_failing_holds_the_row_and_credits_nothing(status):
    [out] = await decide([leaf()], FakeGitHub([pr()], status=status))
    assert out.v2_hold and not out.v2_verified
    assert out.evidence["v2_unavailable"] == [f"HTTP {status}"]


async def test_the_call_cap_holds_the_rest():
    leaves = [leaf("t1", number=1, node_id="a"), leaf("t2", number=2, node_id="b")]
    first, second = await decide(leaves, FakeGitHub([pr(1), pr(2)]), cap=1)
    assert first.v2_verified
    assert second.v2_hold and second.evidence["v2_unavailable"] == ["call cap reached"]


async def test_one_pr_credits_the_earliest_leaf_only():
    later = leaf("late", dag_at=DAG_AT + timedelta(hours=1), node_id="b")
    early = leaf("early", node_id="a")
    out = {s.task_id: s for s in await decide([later, early], FakeGitHub([pr(opened=DAG_AT + timedelta(hours=2))]))}
    assert out["early"].v2_verified
    assert not out["late"].v2_verified and out["late"].evidence["v2_pr_duplicate"] == ["early"]


async def test_a_credit_stored_by_an_earlier_pass_stands():
    stored = StoredPrEvidence(credited={"tfatykhov/nous#5": "someone-else"})
    [out] = await decide([leaf()], FakeGitHub([pr()]), stored=stored)
    assert not out.v2_verified and out.evidence["v2_pr_duplicate"] == ["someone-else"]
    [own] = await decide([leaf()], FakeGitHub([pr()]), stored=StoredPrEvidence(credited={"tfatykhov/nous#5": "t1"}))
    assert own.v2_verified  # its own earlier credit is not a duplicate


async def test_a_head_branch_resolves_once_and_is_pinned():
    github = FakeGitHub([pr(7)], branch=[{"number": 7}])
    [out] = await decide([leaf(head="fix/thing")], github)
    assert out.v2_verified and out.evidence["v2_pr"] == ["tfatykhov/nous#7"]
    assert [r.url.path for r in github.requests] == ["/repos/tfatykhov/nous/pulls", "/repos/tfatykhov/nous/pulls/7"]
    # A later pass reads the pin: a newer PR on the same branch cannot take its place.
    github = FakeGitHub([pr(7), pr(8)], branch=[{"number": 8}])
    [again] = await decide([leaf(head="fix/thing")], github, stored=StoredPrEvidence(pins={"t1": "tfatykhov/nous#7"}))
    assert again.evidence["v2_pr"] == ["tfatykhov/nous#7"]
    assert [r.url.path for r in github.requests] == ["/repos/tfatykhov/nous/pulls/7"]


async def test_leaves_without_a_pr_check_are_untouched_and_github_is_not_called():
    plain = TaskSignals(task_kind="subtask", task_id="x", finished_at=NOW, status="completed")
    github = FakeGitHub()
    assert await decide([plain], github) == [plain]
    assert github.requests == []


def test_stored_evidence_keeps_the_first_credit_and_the_pins():
    stored = stored_pr_evidence(
        [
            ("a", {"v2_pr": ["r/x#1"], "v2_pr_merged": ["r/x#1"]}),
            ("b", {"v2_pr": ["r/x#1"], "v2_pr_merged": ["r/x#1"]}),  # a later row cannot take it over
            ("c", {"v2_pr": ["r/x#2"], "v2_pending": ["open"]}),
            ("d", None),
        ]
    )
    assert stored.credited == {"r/x#1": "a"}
    assert stored.pins == {"a": "r/x#1", "b": "r/x#1", "c": "r/x#2"}


@pytest.mark.parametrize(
    ("command", "parsed"),
    [
        ("python -m nous.dag.typed_checks pr tfatykhov/nous --pr 12", PrCheck("tfatykhov/nous", 12, None)),
        (
            "  python -m nous.dag.typed_checks pr tfatykhov/nous --head fix/a.b-c\n",
            PrCheck("tfatykhov/nous", None, "fix/a.b-c"),
        ),
        ("python -m nous.dag.typed_checks pr tfatykhov/nous --pr 12 || true", None),  # not the check any more
        ("python -m nous.dag.typed_checks pr tfatykhov/nous --pr 12; exit 0", None),
        ("python  -m nous.dag.typed_checks pr tfatykhov/nous --pr 12", None),
        ("python -m nous.dag.typed_checks pr tfatykhov/nous --pr 0", None),
        ("test -f /tmp/done", None),
        (None, None),
    ],
)
def test_only_the_exact_pr_check_is_one(command, parsed):
    assert ts.parse_pr_check(command) == parsed


# ---- review round 1: what must not credit, and what must survive an outage ---------------------------------------


@pytest.mark.parametrize(
    ("merger", "reason"),
    [
        ("github-actions[bot]", "not merged by an accepted login"),  # a workflow merge is not the owner
        ("some-maintainer", "not merged by an accepted login"),  # an allowlist, not "anyone but the agent"
    ],
)
async def test_only_an_accepted_merger_confirms(merger, reason):
    [out] = await decide([leaf()], FakeGitHub([pr(merged_by=merger)]))
    assert not out.v2_verified and out.evidence["v2_rejected"] == [reason]


async def test_an_accepted_bot_login_still_confirms_nothing():
    [out] = await decide([leaf()], FakeGitHub([pr(merged_by="owner-tool[bot]")]), mergers=("owner-tool[bot]",))
    assert not out.v2_verified and out.evidence["v2_rejected"] == ["not merged by an accepted login"]


async def test_a_merge_with_no_merger_is_said_so():
    pull = pr()
    pull["merged_by"] = None  # a deleted account
    [out] = await decide([leaf()], FakeGitHub([pull]))
    assert out.evidence["v2_rejected"] == ["no merger"]


async def test_no_mergers_configured_verifies_nothing_and_asks_nothing():
    github = FakeGitHub([pr()])
    [out] = await decide([leaf()], github, mergers=())
    assert not out.v2_verified and github.requests == []


async def test_a_stored_credit_stands_without_asking_github_even_when_it_is_down():
    github = FakeGitHub([pr()], status=503)
    [out] = await decide([leaf()], github, stored=StoredPrEvidence(credited={"tfatykhov/nous#5": "t1"}))
    assert out.v2_verified and not out.v2_hold and out.evidence["v2_pr_merged"] == ["tfatykhov/nous#5"]
    assert github.requests == []


async def test_a_pr_another_leaf_took_for_good_is_a_duplicate_without_a_call():
    github = FakeGitHub([pr()])
    [out] = await decide([leaf()], github, stored=StoredPrEvidence(credited={"tfatykhov/nous#5": "other"}))
    assert out.evidence["v2_pr_duplicate"] == ["other"] and github.requests == []


async def test_a_leaf_with_no_dag_creation_time_is_rejected():
    [out] = await decide([leaf(dag_at=None)], FakeGitHub([pr()]))
    assert not out.v2_verified and out.evidence["v2_rejected"] == ["no DAG creation time"]


async def test_a_resolved_head_is_pinned_even_when_the_pr_read_fails():
    class ListThenFail(FakeGitHub):
        def __call__(self, request):
            self.requests.append(request)
            if request.url.path.endswith("/pulls"):
                return httpx.Response(200, json=[{"number": 7}])
            return httpx.Response(502, json={})

    [out] = await decide([leaf(head="fix/thing")], ListThenFail())
    assert out.v2_hold and out.evidence["v2_pr"] == ["tfatykhov/nous#7"]  # the next pass reads this pin


async def test_a_pr_an_earlier_leaf_could_not_decide_is_held_for_the_later_one():
    class FailOnce(FakeGitHub):
        def __call__(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                return httpx.Response(503, json={})
            return httpx.Response(200, json=self.pulls[5])

    early, late = leaf("early", node_id="a"), leaf("late", dag_at=DAG_AT + timedelta(hours=1), node_id="b")
    out = {s.task_id: s for s in await decide([late, early], FailOnce([pr(opened=DAG_AT + timedelta(hours=2))]))}
    assert out["early"].v2_hold and out["late"].v2_hold  # the later leaf may not take the PR the earlier one named
    assert not out["late"].v2_verified


async def test_a_rate_limit_stops_the_pass_from_asking_again():
    leaves = [leaf("t1", number=1, node_id="a"), leaf("t2", number=2, node_id="b")]
    github = FakeGitHub([pr(1), pr(2)], status=403)
    first, second = await decide(leaves, github)
    assert first.v2_hold and second.v2_hold and second.evidence["v2_unavailable"] == ["HTTP 403"]
    assert len(github.requests) == 1


def test_a_path_segment_is_not_a_repository():
    assert ts.parse_pr_check("python -m nous.dag.typed_checks pr ../.. --pr 5") is None
    assert ts.parse_pr_check("python -m nous.dag.typed_checks pr tfatykhov/.. --pr 5") is None
