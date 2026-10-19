-- 089: dag_nodes.attempt_history
--
-- A reset of a node (retry_node, a fix stage's retry, the unblock of a cancelled node) overwrites
-- the columns that say how the attempt it discards ended: subtask_id, status, error and the
-- check columns. The node row is the only record of them, so the reset first appends one entry
-- per discarded attempt to this list, in the write that resets the node. The task scorer reads the
-- entry of a superseded attempt to tell a check that failed from one that never confirmed.
-- Nullable: rows from before 089 have no history, and a node that never launched gets none.
ALTER TABLE nous_system.dag_nodes
    ADD COLUMN IF NOT EXISTS attempt_history JSONB;
