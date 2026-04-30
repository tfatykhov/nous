# Compaction fidelity eval — 3 scenarios

- judge: `claude-sonnet-4-6`
- overall fact preservation: **7/9 (77.8%)**
- SUT: `nous.api.compaction.ConversationCompactor.compact`

## Per-scenario

| name | facts | preserved | rate |
|---|---:|---:|---:|
| config_value | 3 | 2 | 67% |
| decision_with_rationale | 2 | 2 | 100% |
| person_attributes | 4 | 3 | 75% |

## Dropped facts (samples)

- **config_value**: Orders service binds to 0.0.0.0:8080 — _This binding address and port are never mentioned anywhere in the original conversation or the summary; the fact cannot be derived from the summary._
- **person_attributes**: Marcus Webb is the primary contact at marcus.webb@acme.com — _Marcus Webb and his email address never appear anywhere in the summary or original conversation; this information was never provided_