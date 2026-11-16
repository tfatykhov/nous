"""python -m nous.ping: post one message to chat with the owner's secret, from where the secret is. No database and no
network: the HTTP post is a stand-in."""

from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest

from nous import ping

SECRET = "p" * 64
ENV = {"NOUS_API_URL": "http://nous:8000", "NOUS_OWNER_ROUTE_SECRET": SECRET}


class _Post:
    """httpx.post as the ping calls it: each call is recorded as (url, json, headers, timeout)."""

    def __init__(self, status: int = 200, body: object = None, raises: Exception | None = None) -> None:
        self.status, self.body, self.raises = status, body, raises
        self.calls: list[tuple[str, object, object, object]] = []

    def __call__(self, url: str, *, json: object, headers: object, timeout: object) -> SimpleNamespace:  # noqa: A002
        self.calls.append((url, json, headers, timeout))
        if self.raises is not None:
            raise self.raises

        def body() -> object:
            if isinstance(self.body, Exception):
                raise self.body
            return self.body

        return SimpleNamespace(status_code=self.status, json=body)


def _output(capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    captured = capsys.readouterr()
    return captured.out, captured.err


def test_the_post_carries_the_secret_and_the_body(capsys: pytest.CaptureFixture[str]) -> None:
    post = _Post(200, {"response": "reviewed", "session_id": "pr-review-897"})
    code = ping.main(["pr-review-897", "Please review PR 897"], ENV, post=post)
    assert code == 0
    assert post.calls == [
        (
            "http://nous:8000/chat",
            {"message": "Please review PR 897", "session_id": "pr-review-897"},
            {"X-Nous-Owner-Secret": SECRET},
            600,
        )
    ]
    out, err = _output(capsys)
    assert out.splitlines() == ["HTTP 200", "reviewed"]
    assert SECRET not in out + err


def test_the_api_url_defaults_to_localhost_and_a_trailing_slash_is_dropped() -> None:
    post = _Post(200, {"response": "ok"})
    assert ping.main(["s-1", "hi"], {"NOUS_OWNER_ROUTE_SECRET": SECRET}, post=post) == 0
    assert ping.main(["s-1", "hi"], {**ENV, "NOUS_API_URL": "http://nous:8000/"}, post=post) == 0
    assert [call[0] for call in post.calls] == ["http://localhost:8000/chat", "http://nous:8000/chat"]


@pytest.mark.parametrize("env", [{"NOUS_API_URL": "http://nous:8000"}, {**ENV, "NOUS_OWNER_ROUTE_SECRET": ""}])
def test_without_the_secret_here_it_exits_2_and_posts_nothing(
    capsys: pytest.CaptureFixture[str], env: dict[str, str]
) -> None:
    post = _Post()
    assert ping.main(["pr-review-1", "hi"], env, post=post) == 2
    assert post.calls == []
    out, err = _output(capsys)
    assert out == ""
    assert "NOUS_OWNER_ROUTE_SECRET is not set" in err


@pytest.mark.parametrize(
    "session_id", ["", "a b", "../x", "x" * 101, "id\n", "caf" + chr(0xE9), "a;b", "$(id)"], ids=lambda s: repr(s)[:12]
)
def test_a_session_id_outside_the_pattern_exits_2(capsys: pytest.CaptureFixture[str], session_id: str) -> None:
    post = _Post()
    assert ping.main([session_id, "hi"], ENV, post=post) == 2
    assert post.calls == []
    assert "session id" in _output(capsys)[1]


@pytest.mark.parametrize("session_id", ["pr-review-897", "a", "A.b_c-9", "x" * 100])
def test_a_session_id_inside_the_pattern_is_posted(session_id: str) -> None:
    post = _Post(200, {"response": "ok"})
    assert ping.main([session_id, "hi"], ENV, post=post) == 0
    assert post.calls[0][1] == {"message": "hi", "session_id": session_id}


@pytest.mark.parametrize("argv", [[], ["only-one"], ["a", "b", "c"]])
def test_the_wrong_number_of_arguments_exits_2(capsys: pytest.CaptureFixture[str], argv: list[str]) -> None:
    post = _Post()
    assert ping.main(argv, ENV, post=post) == 2
    assert post.calls == []
    assert "usage" in _output(capsys)[1]


def test_an_empty_message_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    post = _Post()
    assert ping.main(["s-1", ""], ENV, post=post) == 2
    assert post.calls == []


@pytest.mark.parametrize(
    ("status", "body"),
    [(403, {"error": "forbidden"}), (500, {"error": "Chat error"}), (502, ValueError("not json"))],
)
def test_another_answer_exits_1_and_prints_neither_the_secret_nor_the_headers(
    capsys: pytest.CaptureFixture[str], status: int, body: object
) -> None:
    assert ping.main(["pr-review-1", "hi"], ENV, post=_Post(status, body)) == 1
    out, err = _output(capsys)
    assert out.splitlines() == [f"HTTP {status}"]
    for text in (SECRET, "X-Nous-Owner-Secret", "x-nous-owner-secret"):
        assert text not in out + err


def test_a_long_response_is_cut(capsys: pytest.CaptureFixture[str]) -> None:
    assert ping.main(["s-1", "hi"], ENV, post=_Post(200, {"response": "y" * 10_000})) == 0
    out, _err = _output(capsys)
    assert out.splitlines() == ["HTTP 200", "y" * 4000]


def test_an_exception_prints_its_class_only(capsys: pytest.CaptureFixture[str]) -> None:
    post = _Post(raises=httpx.ConnectError(f"cannot reach http://nous:8000 with {SECRET}"))
    assert ping.main(["s-1", "hi"], ENV, post=post) == 1
    out, err = _output(capsys)
    assert "ConnectError" in err
    assert SECRET not in out + err
    assert "nous:8000" not in out + err


def test_it_runs_as_a_module_and_refuses_without_the_secret() -> None:
    env = {key: value for key, value in os.environ.items() if key != "NOUS_OWNER_ROUTE_SECRET"}
    env["NOUS_API_URL"] = "http://127.0.0.1:9"  # never contacted: the missing secret stops it first
    done = subprocess.run(
        [sys.executable, "-m", "nous.ping", "pr-review-1", "hi"], capture_output=True, text=True, env=env, timeout=60
    )
    assert done.returncode == 2
    assert done.stdout == ""
    assert "NOUS_OWNER_ROUTE_SECRET is not set" in done.stderr
