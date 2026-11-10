-- 106: why the frame was chosen, what the critic said, whether the turn failed (F110 v2)
-- agent_turn_log.frame_reason: pattern, default, carried, referent or critic (FrameSelection.match_method).
-- agent_turn_log.critic_frame: the frame the critic's model recommended (a frame id, or unknown for anything else),
-- NULL when the critic did not run or its model gave no frame.
-- agent_turn_log.had_error: true when the turn ended with an error.
-- Writer: the app (the recorder). Reader: dashboards, and the db_query reader once scripts/ops/db_roles.sql is run again
-- (the grants name columns, so these stay unreadable until then). Nullable, no default: adding them rewrites nothing.
ALTER TABLE nous_system.agent_turn_log ADD COLUMN IF NOT EXISTS frame_reason TEXT;
ALTER TABLE nous_system.agent_turn_log ADD COLUMN IF NOT EXISTS critic_frame TEXT;
ALTER TABLE nous_system.agent_turn_log ADD COLUMN IF NOT EXISTS had_error BOOLEAN;
