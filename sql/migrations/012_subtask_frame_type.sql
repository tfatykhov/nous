-- 012.2: Add frame_type and model columns for frame-aware subtasks and per-subtask model selection
ALTER TABLE heart.subtasks ADD COLUMN IF NOT EXISTS frame_type VARCHAR(30);
ALTER TABLE heart.subtasks ADD COLUMN IF NOT EXISTS model VARCHAR(100);
