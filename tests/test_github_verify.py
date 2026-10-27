"""Scorer v4: a PR check earns credit only from GitHub itself, read by the scorer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from nous.observability import task_scoring as ts
from nous.observability.github_verify import (
    GitHubReader,
    GitHubUnavailable,
    PrVerifier,
    StoredPrEvidence,
    stored_pr_evidence,
)
from nous.observability.task_scoring import PrCheck, TaskSignals

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
DAG_AT = NOW - timedelta(days=2)
AGENT = "cognition-engines-nous-agent"
SINCE_DAG = (DAG_AT + timedelta(minutes=30)).isoformat()  # a branch PR opened after the DAG began


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
    def __init__(self, pulls=(), *, status=200, branch=None, renamed_to=None):
        self.pulls = {p["number"]: p for p in pulls}
        self.status = status
        self.branch = branch or []
        self.renamed_to = renamed_to  # the repository's current owner/name, when it was transferred
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={})
        path = request.url.path
        if path.count("/") == 3:  # /repos/<owner>/<name>: the repository itself
            return httpx.Response(200, json={"full_name": self.renamed_to or path.split("/", 2)[2]})
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
    github = FakeGitHub([pr(7)], branch=[{"number": 7, "created_at": SINCE_DAG}])
    [out] = await decide([leaf(head="fix/thing")], github)
    assert out.v2_verified and out.evidence["v2_pr"] == ["tfatykhov/nous#7"]
    assert [r.url.path for r in github.requests] == [
        "/repos/tfatykhov/nous",  # the repository as it is now
        "/repos/tfatykhov/nous/pulls",
        "/repos/tfatykhov/nous/pulls/7",
    ]
    # A later pass reads the pin: a newer PR on the same branch cannot take its place.
    github = FakeGitHub([pr(7), pr(8)], branch=[{"number": 8, "created_at": SINCE_DAG}])
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
        (
            "python -m nous.dag.typed_checks pr tfatykhov/nous --head fix/x --job-file /tmp/nous-workspace/f/job.id",
            PrCheck("tfatykhov/nous", None, "fix/x"),  # the job file changes nothing the scorer reads
        ),
        ("python -m nous.dag.typed_checks pr tfatykhov/nous --pr 12 --job-file /tmp/j.id; exit 0", None),
        ("python -m nous.dag.typed_checks pr tfatykhov/nous --pr 12 --job-file /tmp/$J", None),
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
            if request.url.path.count("/") == 3:
                return httpx.Response(200, json={"full_name": "tfatykhov/nous"})
            if request.url.path.endswith("/pulls"):
                return httpx.Response(200, json=[{"number": 7, "created_at": SINCE_DAG}])
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
    # The reader remembers the failure for the pass: the later leaf may not take the PR the earlier one named.
    assert out["early"].v2_hold and out["late"].v2_hold
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


async def test_two_spellings_of_one_repository_are_one_pr():
    # GitHub matches owner and repository names case-insensitively: both checks name the same PR, which credits once.
    def pull_with_base():
        p = pr(opened=DAG_AT + timedelta(hours=2))
        p["base"] = {"repo": {"full_name": "tfatykhov/nous"}}
        return p

    first = leaf("first", node_id="a")
    other = leaf("other", node_id="b", dag_at=DAG_AT + timedelta(hours=1))
    other = TaskSignals(**{**other.__dict__, "pr_check": PrCheck(repo="TfAtykhov/NOUS", number=5)})
    out = {s.task_id: s for s in await decide([first, other], FakeGitHub([pull_with_base()]))}
    assert out["first"].v2_verified and out["first"].evidence["v2_pr_merged"] == ["tfatykhov/nous#5"]
    assert not out["other"].v2_verified and out["other"].evidence["v2_pr_duplicate"] == ["first"]


async def test_a_stored_credit_matches_whatever_the_spelling():
    other = leaf()
    other = TaskSignals(**{**other.__dict__, "pr_check": PrCheck(repo="TFATYKHOV/nous", number=5)})
    github = FakeGitHub([pr()])
    [out] = await decide([other], github, stored=stored_pr_evidence([("x", {"v2_pr_merged": ["tfatykhov/nous#5"]})]))
    assert out.evidence["v2_pr_duplicate"] == ["x"] and github.requests == []


async def test_the_scorer_follows_a_renamed_repository():
    def handle(request):
        if request.url.path == "/repos/tfatykhov/nous/pulls/5":
            return httpx.Response(301, headers={"Location": "https://api.github.com/repositories/42/pulls/5"})
        return httpx.Response(200, json={**pr(), "base": {"repo": {"full_name": "tfatykhov/nous-renamed"}}})

    reader = GitHubReader(transport=httpx.MockTransport(handle))
    try:
        [out] = await PrVerifier(reader, [AGENT], [OWNER]).verify([leaf()], StoredPrEvidence())
    finally:
        await reader.aclose()
    assert out.v2_verified and out.evidence["v2_pr_merged"] == ["tfatykhov/nous-renamed#5"]  # the canonical name


async def test_a_pin_survives_a_renamed_repository():
    # The first pass followed the old name to the renamed repository and pinned the PR under its canonical name. A
    # later pass uses that pin although the command still spells the old name: no re-resolution of the branch.
    github = FakeGitHub()
    github.pulls = {7: pr(7)}
    old = leaf(head="fix/thing")
    old = TaskSignals(**{**old.__dict__, "pr_check": PrCheck(repo="tfatykhov/old-name", head="fix/thing")})
    [out] = await decide([old], github, stored=StoredPrEvidence(pins={"t1": "tfatykhov/nous#7"}))
    assert out.v2_verified
    assert [r.url.path for r in github.requests] == ["/repos/tfatykhov/nous/pulls/7"]


async def test_a_head_check_resolves_to_the_pr_its_node_passed_on():
    # The branch is reused: a PR from before this DAG (#3), a first PR of this DAG closed unmerged (#6), the
    # replacement the node passed on (#7), then a later DAG's PR (#8), opened after this node completed.
    done = DAG_AT + timedelta(hours=5)
    branch = [  # GitHub's order: newest first
        {"number": 8, "created_at": (DAG_AT + timedelta(days=2)).isoformat()},
        {"number": 7, "created_at": (DAG_AT + timedelta(hours=4)).isoformat()},
        {"number": 6, "created_at": SINCE_DAG},
        {"number": 3, "created_at": (DAG_AT - timedelta(days=3)).isoformat()},
    ]
    github = FakeGitHub([pr(6, merged_by=None), pr(7), pr(8)], branch=branch)
    [out] = await decide([leaf(head="fix/thing", node_completed_at=done)], github)
    assert out.v2_verified and out.evidence["v2_pr_merged"] == ["tfatykhov/nous#7"]
    listing = next(r for r in github.requests if r.url.path.endswith("/pulls"))
    assert dict(listing.url.params)["direction"] == "desc"


async def test_a_head_check_on_a_transferred_repository_filters_on_its_new_owner():
    branch = [{"number": 7, "created_at": SINCE_DAG}]
    github = FakeGitHub([pr(7)], branch=branch, renamed_to="new-owner/nous")
    [out] = await decide([leaf(head="fix/thing")], github)
    listing = next(r for r in github.requests if r.url.path.endswith("/pulls"))
    assert listing.url.path == "/repos/new-owner/nous/pulls"
    assert dict(listing.url.params)["head"] == "new-owner:fix/thing"
    assert out.evidence["v2_pr"][0].endswith("#7")


async def test_a_numbered_check_uses_the_tasks_canonical_pin():
    # An earlier pass followed the renamed repository and pinned the PR under its canonical name: a later pass reads
    # that pin (and its stored credit) even though the command spells the old name.
    github = FakeGitHub([pr()], status=503)
    old = leaf()
    old = TaskSignals(**{**old.__dict__, "pr_check": PrCheck(repo="tfatykhov/old-name", number=5)})
    stored = StoredPrEvidence(pins={"t1": "tfatykhov/nous#5"}, credited={"tfatykhov/nous#5": "t1"})
    [out] = await decide([old], github, stored=stored)
    assert out.v2_verified and github.requests == []  # the stored credit stood: GitHub being down changed nothing


async def test_a_malformed_answer_holds_the_pr_instead_of_failing_the_pass():
    def handle(request):
        return httpx.Response(200, content=b"<html>proxy error</html>")

    reader = GitHubReader(transport=httpx.MockTransport(handle))
    try:
        [out] = await PrVerifier(reader, [AGENT], [OWNER]).verify([leaf()], StoredPrEvidence())
    finally:
        await reader.aclose()
    assert out.v2_hold and out.evidence["v2_unavailable"] == ["malformed response"]


@pytest.mark.parametrize(
    ("head", "body"),
    [
        (None, [1, 2, 3]),  # a list where the PR object belongs
        (None, "a string"),
        ("fix/thing", ["not a repository object"]),  # the repository lookup of a --head check
    ],
)
async def test_an_answer_of_the_wrong_shape_holds_the_pr_never_rejects_it(head, body):
    reader = GitHubReader(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
    try:
        [out] = await PrVerifier(reader, [AGENT], [OWNER]).verify([leaf(head=head)], StoredPrEvidence())
    finally:
        await reader.aclose()
    assert out.v2_hold and out.evidence["v2_unavailable"] == ["unexpected response"]
    assert "v2_rejected" not in out.evidence


# ---------------------------------------------------------------------------
# Typed waits for lineage DAGs: the reader's check-run and review reads
# ---------------------------------------------------------------------------


def _recording(handler):
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.MockTransport(handle), seen


async def test_check_runs_and_reviews_read_one_page_of_100():
    runs = {"total_count": 1, "check_runs": [{"name": "ci", "status": "completed"}]}
    reviews = [{"id": 1, "state": "APPROVED"}]

    def handle(request):
        return httpx.Response(200, json=runs if request.url.path.endswith("/check-runs") else reviews)

    mock, seen = _recording(handle)
    reader = GitHubReader(transport=mock)
    try:
        assert await reader.check_runs("o/r", "abc1234") == runs
        assert await reader.reviews("o/r", 7) == reviews
        assert await reader.reviews("o/r", 7) == reviews  # remembered: no second request
    finally:
        await reader.aclose()
    assert [r.url.path for r in seen] == ["/repos/o/r/commits/abc1234/check-runs", "/repos/o/r/pulls/7/reviews"]
    assert all(dict(r.url.params) == {"per_page": "100"} for r in seen)


async def test_check_runs_and_reviews_are_none_on_a_404():
    reader = GitHubReader(transport=httpx.MockTransport(lambda request: httpx.Response(404, json={})))
    try:
        assert await reader.check_runs("o/r", "abc1234") is None
        assert await reader.reviews("o/r", 7) is None
    finally:
        await reader.aclose()


async def test_a_500_is_unavailable_and_a_rate_limit_stops_the_reader():
    mock, seen = _recording(lambda request: httpx.Response(500, json={}))
    reader = GitHubReader(transport=mock)
    try:
        with pytest.raises(GitHubUnavailable, match="HTTP 500"):
            await reader.reviews("o/r", 7)
        assert reader.rate_limited is False
    finally:
        await reader.aclose()
    mock, seen = _recording(lambda request: httpx.Response(403, json={}))
    reader = GitHubReader(transport=mock)
    try:
        with pytest.raises(GitHubUnavailable, match="HTTP 403"):
            await reader.check_runs("o/r", "abc1234")
        assert reader.rate_limited is True
        with pytest.raises(GitHubUnavailable):
            await reader.reviews("o/r", 7)
    finally:
        await reader.aclose()
    assert len(seen) == 1  # the 403 stopped the reader: the second read asked nothing


@pytest.mark.parametrize(
    ("method", "args", "body"), [("reviews", ("o/r", 7), {}), ("check_runs", ("o/r", "a1b2c3d"), [])]
)
async def test_a_wrong_shape_is_unavailable(method, args, body):
    reader = GitHubReader(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
    try:
        with pytest.raises(GitHubUnavailable, match="unexpected response"):
            await getattr(reader, method)(*args)
    finally:
        await reader.aclose()


async def test_the_user_agent_and_timeout_are_the_callers():
    mock, seen = _recording(lambda request: httpx.Response(404, json={}))
    default = GitHubReader(transport=mock)
    own = GitHubReader(transport=mock, user_agent="nous-dag-wait", timeout=5.0)
    try:
        await default.pull("o/r", 1)
        await own.pull("o/r", 1)
        assert default._client.timeout.read == 10.0
        assert own._client.timeout.read == 5.0
    finally:
        await default.aclose()
        await own.aclose()
    assert [r.headers["user-agent"] for r in seen] == ["nous-task-scorer", "nous-dag-wait"]
