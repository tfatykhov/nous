"""The built-in PR completion check: exit 0 once the PR exists, 1 when it is final, 2 while it may still come."""

from __future__ import annotations

import httpx
import pytest

from nous.dag import typed_checks


def transport(handler):
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.MockTransport(handle), seen


def pull(state="open", merged_at=None, number=5):
    return {"number": number, "state": state, "merged_at": merged_at}


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(200, json=pull()), 0),  # open: the PR exists
        (httpx.Response(200, json=pull("closed", "2026-10-19T00:00:00Z")), 0),  # merged
        (httpx.Response(200, json=pull("closed")), 1),  # closed without a merge: final
        (httpx.Response(404, json={}), 2),  # not there yet
        (httpx.Response(500, json={}), 2),  # GitHub failing never fails the node
        (httpx.Response(403, json={"message": "rate limited"}), 2),
    ],
)
def test_pr_by_number(response, code, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    mock, seen = transport(lambda request: response)
    assert typed_checks.main(["pr", "tfatykhov/nous", "--pr", "5"], transport=mock) == code
    assert seen[0].url.path == "/repos/tfatykhov/nous/pulls/5"
    assert "authorization" not in seen[0].headers


def test_a_transport_error_is_pending():
    def boom(request):
        raise httpx.ConnectError("down", request=request)

    mock, _ = transport(boom)
    assert typed_checks.main(["pr", "tfatykhov/nous", "--pr", "5"], transport=mock) == 2


def _repo_then(listing, full_name="tfatykhov/nous"):
    """GitHub for a --head check: the repository lookup, then the branch's PR listing."""

    def handle(request):
        if request.url.path.count("/") == 3:
            return httpx.Response(200, json={"full_name": full_name})
        return listing(request)

    return handle


def test_pr_by_head_branch_takes_the_newest_pr_on_it(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    mock, seen = transport(_repo_then(lambda request: httpx.Response(200, json=[pull(number=9)])))
    assert typed_checks.main(["pr", "tfatykhov/nous", "--head", "fix/thing"], transport=mock) == 0
    request = seen[1]
    assert request.url.path == "/repos/tfatykhov/nous/pulls"
    assert dict(request.url.params) == {
        "state": "all",
        "head": "tfatykhov:fix/thing",
        "sort": "created",
        "direction": "desc",
        "per_page": "1",
    }
    assert request.headers["authorization"] == "Bearer t0ken"


def test_no_pr_on_the_branch_yet_is_pending():
    mock, _ = transport(_repo_then(lambda request: httpx.Response(200, json=[])))
    assert typed_checks.main(["pr", "tfatykhov/nous", "--head", "fix/thing"], transport=mock) == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["pr", "not-a-repo", "--pr", "5"],
        ["pr", "tfatykhov/nous", "--pr", "0"],
        ["pr", "tfatykhov/nous", "--pr", "five"],
        ["pr", "tfatykhov/nous", "--head", "has space"],
        ["pr", "tfatykhov/nous"],  # neither --pr nor --head
        ["pr", "tfatykhov/nous", "--pr", "5", "--head", "b"],  # both
        ["merged", "tfatykhov/nous", "--pr", "5"],  # no such check
        ["--help"],  # would exit 0 and read as a pass
        ["pr", "--help"],
    ],
)
def test_wrong_arguments_fail_the_check_and_never_call_github(argv):
    def unexpected(request):
        raise AssertionError("GitHub must not be called")

    mock, _ = transport(unexpected)
    assert typed_checks.main(argv, transport=mock) == 1


def test_a_path_segment_is_not_a_repository():
    def unexpected(request):
        raise AssertionError("GitHub must not be called")

    mock, _ = transport(unexpected)
    assert typed_checks.main(["pr", "../..", "--pr", "5"], transport=mock) == 1


@pytest.mark.parametrize(
    ("opened", "state", "merged_at", "code"),
    [
        ("2026-10-18T00:00:00Z", "open", None, 2),  # an older PR on a reused branch name: not this DAG's PR yet
        ("2026-10-18T00:00:00Z", "closed", None, 2),  # an older PR closed unmerged must not fail this node either
        ("2026-10-19T13:00:00Z", "open", None, 0),  # opened after the DAG: this DAG's PR
    ],
)
def test_head_ignores_prs_opened_before_the_dag(opened, state, merged_at, code, monkeypatch):
    monkeypatch.setenv("NOUS_DAG_CREATED_AT", "2026-10-19T12:00:00+00:00")
    body = [{"number": 9, "state": state, "merged_at": merged_at, "created_at": opened}]
    mock, _ = transport(_repo_then(lambda request: httpx.Response(200, json=body)))
    assert typed_checks.main(["pr", "tfatykhov/nous", "--head", "fix/thing"], transport=mock) == code


def test_a_renamed_repository_is_followed():
    def handle(request):
        if request.url.path == "/repos/old-owner/old-name/pulls/5":
            return httpx.Response(301, headers={"Location": "https://api.github.com/repositories/42/pulls/5"})
        return httpx.Response(200, json=pull())

    mock, seen = transport(handle)
    assert typed_checks.main(["pr", "old-owner/old-name", "--pr", "5"], transport=mock) == 0
    assert [r.url.path for r in seen] == ["/repos/old-owner/old-name/pulls/5", "/repositories/42/pulls/5"]


def test_head_on_a_transferred_repository_filters_on_its_new_owner():
    mock, seen = transport(_repo_then(lambda request: httpx.Response(200, json=[pull(number=9)]), "new-owner/nous"))
    assert typed_checks.main(["pr", "old-owner/nous", "--head", "fix/thing"], transport=mock) == 0
    assert seen[1].url.path == "/repos/new-owner/nous/pulls"
    assert dict(seen[1].url.params)["head"] == "new-owner:fix/thing"
