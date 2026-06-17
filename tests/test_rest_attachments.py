"""REST endpoint tests for F024 inbound attachments.

Reuses the test_rest.py harness pattern: httpx AsyncClient + ASGITransport,
real Postgres brain/heart/cognitive/db fixtures (from conftest), and a custom
Settings object so create_app() closes over the attachment flags we want.

The shared MockAgentRunner in test_rest.py swallows **kwargs without recording
attachments, so we use a small capturing runner here.
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from nous.config import Settings

# Reuse the DB-backed fixtures + canned TurnContext from the existing harness.
from tests.test_rest import (  # noqa: F401  (brain/cognitive are fixtures)
    MockAgentRunner,
    brain,
    cognitive,
)


class CapturingRunner(MockAgentRunner):
    """Records the attachments kwarg passed to run_turn / stream_chat."""

    def __init__(self) -> None:
        super().__init__()
        self.last_attachments = "UNSET"

    async def run_turn(self, session_id, user_message, agent_id=None, **kwargs):
        self.last_attachments = kwargs.get("attachments")
        return self.preset_response, self.preset_context, {
            "input_tokens": 100, "output_tokens": 50}


def _settings(**overrides) -> Settings:
    base = dict(
        attachments_enabled=True,
        attachments_max_per_message=5,
        attachments_default_prompt="What can you tell me about this?",
    )
    base.update(overrides)
    return Settings(**base)


def _make_app(runner, brain, heart, cognitive, db, settings):
    from nous.api.rest import create_app

    return create_app(runner, brain, heart, cognitive, db, settings)


# A tiny valid base64 PNG-ish payload (content is irrelevant; we only assert
# that size_bytes is derived from the base64, not any client-declared value).
_DATA_B64 = "aGVsbG8gd29ybGQ="  # "hello world" -> 11 decoded bytes


@pytest.mark.asyncio
async def test_chat_with_attachment_no_message(brain, heart, cognitive, db):
    """attachments_enabled=True, empty message but one attachment -> 200,
    runner receives exactly one Attachment with computed size + content_type."""
    runner = CapturingRunner()
    settings = _settings()
    app = _make_app(runner, brain, heart, cognitive, db, settings)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post("/chat", json={
            "message": "",
            "attachments": [{
                "filename": "hello.png",
                "media_type": "image/png",
                "data_base64": _DATA_B64,
                "size_bytes": 999999,  # client-declared lie; must be ignored
            }],
        })

    assert resp.status_code == 200, resp.text
    atts = runner.last_attachments
    assert atts is not None and len(atts) == 1
    att = atts[0]
    assert att.filename == "hello.png"
    assert att.content_type == "image"
    assert att.source == "rest"
    # size derived from base64 ("hello world" = 11 bytes), NOT the declared 999999
    assert att.size_bytes == 11


@pytest.mark.asyncio
async def test_chat_neither_message_nor_attachments(brain, heart, cognitive, db):
    """No message and no attachments -> 400."""
    runner = CapturingRunner()
    settings = _settings()
    app = _make_app(runner, brain, heart, cognitive, db, settings)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post("/chat", json={"message": ""})

    assert resp.status_code == 400
    assert "message" in resp.json()["error"]


@pytest.mark.asyncio
async def test_chat_body_too_large(brain, heart, cognitive, db):
    """A body exceeding the size cap -> 413.

    Approach: set attachments_max_per_message=0 so the cap collapses to the
    1_000_000-byte floor, then send a genuinely >1MB body. ASGITransport sets
    its own Content-Length, so a real large body is the portable way to hit the
    header check (rather than spoofing Content-Length, which TestClient/httpx
    overrides).
    """
    runner = CapturingRunner()
    settings = _settings(attachments_max_per_message=0)  # cap == 1_000_000
    app = _make_app(runner, brain, heart, cognitive, db, settings)
    big = "x" * (1_100_000)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post("/chat", json={"message": big})

    assert resp.status_code == 413
    assert resp.json()["error"] == "Request too large"


@pytest.mark.asyncio
async def test_chat_attachments_disabled_ignored(brain, heart, cognitive, db):
    """attachments_enabled=False -> attachments in body ignored, runner gets None."""
    runner = CapturingRunner()
    settings = _settings(attachments_enabled=False)
    app = _make_app(runner, brain, heart, cognitive, db, settings)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post("/chat", json={
            "message": "hi",
            "attachments": [{
                "filename": "hello.png",
                "media_type": "image/png",
                "data_base64": _DATA_B64,
            }],
        })

    assert resp.status_code == 200, resp.text
    assert runner.last_attachments is None
