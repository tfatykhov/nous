-- F038-1.1: Raise decision quality gate threshold from 0.5 to 0.55
UPDATE brain.guardrails
SET condition = '{"cel": "decision.quality_score < 0.55"}'
WHERE name = 'low-quality-recording';
