-- 105: task frame keywords (F110 v2)
-- Adds write, draft, spec, send, deliver, add, edit to the task frame's activation patterns.
-- Writer: this migration. Idempotent. Rollback (inverse):
--   UPDATE nous_system.frames SET activation_patterns = ARRAY(SELECT p FROM unnest(activation_patterns) p WHERE p NOT IN ('write','draft','spec','send','deliver','add','edit')) WHERE id = 'task'
UPDATE nous_system.frames
SET activation_patterns = activation_patterns || ARRAY['write','draft','spec','send','deliver','add','edit']
WHERE id = 'task'
  AND NOT activation_patterns @> ARRAY['write','draft','spec','send','deliver','add','edit'];
