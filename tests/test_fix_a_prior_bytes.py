"""PR A: a reverted snapshot does not go on holding the file's prior bytes.

A write_file snapshot stores the previous file content so a revert can put
it back. Once the revert is recorded nothing reads those bytes again, but on
236c110 they stayed in the row until the ledger retention pruned it.
"""

from __future__ import annotations

import base64
import hashlib
import uuid

import pytest

from nous.api.compensation import SnapshotStore


@pytest.mark.asyncio
async def test_reverted_snapshot_drops_the_prior_bytes(db):
    agent = f"fix-a-{uuid.uuid4().hex[:8]}"
    store = SnapshotStore(db, agent)
    entry = uuid.uuid4()
    kept = {
        "path": "secrets.env",
        "full_path": "/ws/secrets.env",
        "workspace_root": "/ws",
        "existed": True,
        "prior_sha256": hashlib.sha256(b"TOKEN=old").hexdigest(),
        "written_content_hash": hashlib.sha256(b"TOKEN=new").hexdigest(),
        "written_size": 9,
    }
    prior_b64 = base64.b64encode(b"TOKEN=old").decode("ascii")
    snapshot_id = await store.capture(
        ledger_entry_id=entry, tool_name="write_file", snapshot_data={**kept, "prior_b64": prior_b64}
    )

    # another agent can neither revert the snapshot nor strip it
    assert await SnapshotStore(db, agent + "-other").mark_reverted(snapshot_id, result_message="x") is False
    assert (await store.get_by_ledger_entry(entry)).snapshot_data["prior_b64"] == prior_b64

    assert await store.mark_reverted(snapshot_id, result_message="restored prior content") is True

    row = await store.get_by_ledger_entry(entry)
    assert row.reverted_at is not None and row.revert_result == "restored prior content"
    assert row.snapshot_data == kept  # the prior bytes are gone, everything else stays

    # still idempotent: a second revert records nothing
    assert await store.mark_reverted(snapshot_id, result_message="again") is False
    assert (await store.get_by_ledger_entry(entry)).revert_result == "restored prior content"
