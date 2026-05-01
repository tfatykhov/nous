# Compaction fidelity eval — 15 scenarios

- judge: `claude-sonnet-4-6`
- overall fact preservation: **33/45 (73.3%)**
- SUT: `nous.api.compaction.ConversationCompactor.compact`

## Per-scenario

| name | facts | preserved | rate |
|---|---:|---:|---:|
| config_value | 3 | 2 | 67% |
| decision_with_rationale | 2 | 2 | 100% |
| person_attributes | 4 | 3 | 75% |
| schedule_change | 3 | 3 | 100% |
| credential_redacted | 4 | 3 | 75% |
| long_chat_one_fact | 1 | 1 | 100% |
| version_pin | 1 | 1 | 100% |
| negative_decision | 2 | 1 | 50% |
| fact_in_assistant_turn | 3 | 2 | 67% |
| user_preference | 4 | 3 | 75% |
| deadline_change | 3 | 2 | 67% |
| multiple_subjects | 4 | 2 | 50% |
| nested_priorities | 3 | 2 | 67% |
| model_change | 4 | 3 | 75% |
| incident_postmortem | 4 | 3 | 75% |

## Dropped facts (samples)

- **config_value**: Orders service binds to 0.0.0.0:8080 — _This binding address and port are never mentioned anywhere in the original conversation or the summary; the fact cannot be derived from the summary._
- **person_attributes**: Marcus Webb is the primary contact at marcus.webb@acme.com — _The summary names 'David Park' (VP of IT, david.park@acmecorp.com) as the primary contact — Marcus Webb and marcus.webb@acme.com appear nowhere in the summary, directly contradicting this fact_
- **credential_redacted**: Default deploy region is us-east-2 — _The summary lists the AWS region as 'us-east-1', which contradicts the claimed fact of 'us-east-2'. Additionally, the original conversation never mentioned a region at all, so the summary's value is fabricated but still contradicts the fact being checked._
- **negative_decision**: Will use Postgres LISTEN/NOTIFY for event streaming instead — _The summary specifies Redis Streams as the event bus (not Postgres LISTEN/NOTIFY). Postgres is used only as an analytics sink, not for event streaming._
- **fact_in_assistant_turn**: graph_edges has 2,589 total rows; 1,604 are 'related_to' — _graph_edges is never mentioned anywhere in the summary; this fact does not appear in the original conversation either._
- **user_preference**: User dislikes filler phrases like 'great question' and 'absolutely' — _The summary notes 'no filler' and 'concise with no filler' but never specifies filler phrases or gives examples like 'great question' or 'absolutely'. The original conversation also did not mention these specific phrases — only 'no filler' generally — so the specific claim cannot be derived from either source._
- **deadline_change**: Priya Patel from infosec leads the security review — _No mention of Priya Patel or any named individual anywhere in the summary. This fact does not appear in the original conversation either, so it cannot be derived from the summary._
- **multiple_subjects**: Fix was to chunk to 5000 rows per batch — _No specific fix or batch size is mentioned anywhere in the summary. The summary notes no resolution was discussed yet._
- **multiple_subjects**: Took 3 retries to land the fix — _No mention of retries or a fix being landed in the summary. The summary explicitly states no resolution or fix was discussed yet._
- **nested_priorities**: The P0 is a SQL injection in the search endpoint — _The summary identifies the P0 as CVE-2024-23917 but contains no mention of SQL injection or the search endpoint. This specific detail is absent and cannot be derived from the summary._
- **model_change**: Thinking budget set to 18000 tokens — _No mention of a thinking budget or token limit anywhere in the summary; this fact is entirely absent._
- **incident_postmortem**: Sarah Chen was on-call, Emerson on backup — _No mention of Sarah Chen, Emerson, or any on-call personnel anywhere in the summary. This information does not appear in the original conversation either, so it cannot be derived from the summary._