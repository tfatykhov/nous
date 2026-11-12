-- 108: the overdue watcher's index on brain.commitments (F111 PR B1)
--
-- 107 indexed (status, due_at) over open, backed and proposed rows. The watcher reads one agent's open and proposed
-- rows past their deadline (CommitmentStore.list_watchable: agent_id, status and due_at < now), so the index leads
-- with agent_id and leaves backed rows out. Idempotent: the old index is dropped if present and the new one created,
-- so a second run ends with the same index. No grant.

DROP INDEX IF EXISTS brain.commitments_watch_idx;

CREATE INDEX IF NOT EXISTS commitments_watch_idx
    ON brain.commitments (agent_id, status, due_at)
    WHERE status IN ('open', 'proposed');
