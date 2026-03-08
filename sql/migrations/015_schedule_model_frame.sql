-- Migration: Add model and frame_type columns to heart.schedules
-- Part of subtask/schedule improvements (2026-03-08)

ALTER TABLE heart.schedules ADD COLUMN IF NOT EXISTS model VARCHAR(100);
ALTER TABLE heart.schedules ADD COLUMN IF NOT EXISTS frame_type VARCHAR(20);
