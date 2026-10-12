"""F099 Phase 3a: the answer validator and the actor cleaner: one definition each, shared by routes and cards."""

from __future__ import annotations

import json

import httpx
import pytest
from starlette.applications import Starlette

from nous import owner_actions
from nous.api import intention_routes
from nous.brain import continuation
from nous.config import Settings

LONE_SURROGATE = "a" + chr(0xD800) + "b"


@pytest.mark.parametrize("text", ["Yes.", "  padded  ", "x" * 8000, "multi\nline"])
def test_valid_answer_text_returns_the_text_unchanged(text):
    assert continuation.valid_answer_text(text) == text


@pytest.mark.parametrize(
    "text", [None, 5, "", "   \n", "x" * 8001, "a\x00b", LONE_SURROGATE, ["x"], {"text": "x"}, b"bytes"]
)
def test_valid_answer_text_refuses(text):
    assert continuation.valid_answer_text(text) is None


def test_the_limit_is_one_constant():
    assert continuation.ANSWER_MAX_CHARS == 8000
    assert intention_routes.ANSWER_MAX_CHARS == continuation.ANSWER_MAX_CHARS  # the route keeps the name


def test_clean_actor():
    assert owner_actions.clean_actor("alice", default="rest") == "alice"
    assert owner_actions.clean_actor("a\u202eb\x00c", default="rest") == "abc"  # bidi and NUL are not printable
    assert owner_actions.clean_actor("", default="rest") == "rest"
    assert owner_actions.clean_actor(None, default="rest") == "rest"
    assert owner_actions.clean_actor("   ", default="rest") == "rest"
    assert len(owner_actions.clean_actor("x" * 500, default="rest")) == owner_actions.ACTOR_MAX_CHARS == 100


@pytest.mark.parametrize("text", ["", "   ", "x" * 8001, "a\x00b", LONE_SURROGATE, 5, None])
async def test_the_route_texts_are_unchanged(text):  # PIN
    """The reply route answers 400 for the same texts before any lookup. Mutation: drop ``_has_surrogate`` from the
    hoisted validator and the surrogate case gets past the 400 into the missing database (an exception here)."""
    app = Starlette(
        routes=intention_routes.build_intention_routes(
            database=None, settings=Settings(_env_file=None), continuation_runner=None
        )
    )
    # ASCII-escaped JSON: httpx's json= would try to UTF-8 encode the lone surrogate on the client side.
    body = json.dumps({"chat_id": 1, "message_id": 2, "text": text})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        response = await client.post(
            "/intentions/questions/answer", content=body, headers={"content-type": "application/json"}
        )
    assert response.status_code == 400
