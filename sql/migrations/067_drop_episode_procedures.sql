-- 067: Drop heart.episode_procedures (dead table, never wired)
--
-- Defined in init.sql:376 as episode -> procedure links carrying a categorical
-- effectiveness verdict (helped/neutral/hindered). The write API was fully built
-- (EpisodeProcedure model, EpisodeManager.link_procedure, Heart.link_procedure_to_episode)
-- but no runtime path ever called it: the only callers were tests. Zero rows in
-- prod, and zero readers anywhere in nous/ -- no code selects from this table.
--
-- The concept it was designed for did ship, on a different substrate:
-- procedure effectiveness is a Laplace-smoothed float computed from
-- heart.procedures.success_count / failure_count (heart/procedures.py:958),
-- live in prod with 13402 success / 1344 failure across 62 of 70 active
-- procedures, feeding the heartbeat HealthCheck, procedure_learner triage,
-- critic.py, and context/tool rendering. Populating this table would have
-- created a second, contradictory representation of a working metric.
--
-- Note: heart.episode_decisions is deliberately NOT dropped here. It still has
-- live readers pending a separate reader migration.

DROP TABLE IF EXISTS heart.episode_procedures;
