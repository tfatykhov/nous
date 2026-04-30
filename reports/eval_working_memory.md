# Working memory selection eval — 3 scenarios

- judge: `claude-sonnet-4-6`
- threshold: 0.7
- avg precision: **58%**, avg recall: **83%**

## Per-scenario

| name | seeded | loaded | relevant (judge) | TP | FP | FN | P | R |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| decision_continuity | 4 | 4 | 1 | 1 | 3 | 0 | 25% | 100% |
| fresh_question_no_relevant | 2 | 2 | 1 | 1 | 1 | 0 | 50% | 100% |
| low_score_filtered | 4 | 2 | 4 | 2 | 0 | 2 | 100% | 50% |