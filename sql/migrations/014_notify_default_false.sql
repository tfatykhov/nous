-- Migration 014: Flip notify default from TRUE to FALSE for subtasks and schedules
-- Users should opt-in to Telegram notifications rather than opt-out.

ALTER TABLE heart.subtasks ALTER COLUMN notify SET DEFAULT FALSE;
ALTER TABLE heart.schedules ALTER COLUMN notify SET DEFAULT FALSE;
