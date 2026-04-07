-- Issue #273: Add on_complete callback fields to dynamic_checks
-- When a dynamic check self-disables, the on_complete_prompt executes as a callback mini-task.
ALTER TABLE nous_system.dynamic_checks
  ADD COLUMN on_complete_prompt TEXT,
  ADD COLUMN on_complete_tools TEXT[] NOT NULL DEFAULT '{}';
