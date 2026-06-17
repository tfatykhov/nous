import base64
import os
import pytest

from nous.api.attachments import classify_attachment
from nous.api.attachment_store import persist_attachment, ATTACHMENT_PATH_PREFIX
from nous.api.models import Attachment


# NOTE: nous.config exposes the Settings CLASS, not a module-level `settings`
# instance, so the spec's `from nous.config import settings as gs` does not
# import. We construct a fresh Settings() per test instead (Task D report).
def _settings():
    from nous.config import Settings
    return Settings()


def _img(raw=b"\x89PNG\r\n\x1a\n"):
    a = Attachment(filename="shot.png", media_type="image/png",
                   data_base64=base64.b64encode(raw).decode(),
                   size_bytes=len(raw), source="telegram")
    a.content_type = classify_attachment(a.filename, a.media_type)
    return a


@pytest.mark.asyncio
async def test_persist_writes_file_under_attachments_root(tmp_path, monkeypatch):
    gs = _settings()
    monkeypatch.setattr(gs, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(gs, "attachments_dir", "")
    monkeypatch.setattr(gs, "attachments_persist", True)
    att = _img()
    path = await persist_attachment(att, session_id="sess-1", settings=gs)
    assert os.path.isfile(path)
    assert ATTACHMENT_PATH_PREFIX in path
    assert att.workspace_path == path
    with open(path, "rb") as f:
        assert f.read() == base64.b64decode(att.data_base64)


@pytest.mark.asyncio
async def test_persist_disabled_returns_empty(tmp_path, monkeypatch):
    gs = _settings()
    monkeypatch.setattr(gs, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(gs, "attachments_persist", False)
    att = _img()
    path = await persist_attachment(att, session_id="s", settings=gs)
    assert path == ""
    assert att.workspace_path == ""


@pytest.mark.asyncio
async def test_persist_path_traversal_session_id_stays_under_root(tmp_path, monkeypatch):
    gs = _settings()
    monkeypatch.setattr(gs, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(gs, "attachments_dir", "")
    monkeypatch.setattr(gs, "attachments_persist", True)
    att = _img()
    path = await persist_attachment(att, session_id="..", settings=gs)
    from pathlib import Path
    root = Path(gs.attachments_root).resolve()
    assert Path(path).resolve().is_relative_to(root)
