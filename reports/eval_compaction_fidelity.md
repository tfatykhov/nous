# Compaction fidelity eval — 15 scenarios

- judge: `claude-sonnet-4-6`
- overall fact preservation: **31/45 (68.9%)**
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
| negative_decision | 2 | 0 | 0% |
| fact_in_assistant_turn | 3 | 2 | 67% |
| user_preference | 4 | 3 | 75% |
| deadline_change | 3 | 2 | 67% |
| multiple_subjects | 4 | 2 | 50% |
| nested_priorities | 3 | 1 | 33% |
| model_change | 4 | 3 | 75% |
| incident_postmortem | 4 | 3 | 75% |

## Dropped facts (samples)

- **config_value**: Orders service binds to 0.0.0.0:8080 — _This fact never appeared in the original conversation and is entirely absent from the summary; it cannot be derived from any part of it._
- **person_attributes**: Marcus Webb is the primary contact at marcus.webb@acme.com — _Marcus Webb and his email address are never mentioned anywhere in the original conversation or the summary._
- **credential_redacted**: Default deploy region is us-east-2 — _No deploy region is mentioned anywhere in the summary or original conversation._
- **negative_decision**: Decided NOT to add Kafka — _The summary explicitly states no decision has been made yet; Kafka is still under consideration._
- **negative_decision**: Will use Postgres LISTEN/NOTIFY for event streaming instead — _No decision to use Postgres LISTEN/NOTIFY appears anywhere in the summary; it is only mentioned as a possible alternative to evaluate._
- **fact_in_assistant_turn**: graph_edges has 2,589 total rows; 1,604 are 'related_to' — _graph_edges is never mentioned anywhere in the summary; this fact is entirely absent_
- **user_preference**: User dislikes filler phrases like 'great question' and 'absolutely' — _The summary records 'concise, no filler' but never specifies filler phrases or gives examples like 'great question' or 'absolutely'; the specific nature of the dislike is too vague to reconstruct_
- **deadline_change**: Priya Patel from infosec leads the security review — _No mention of Priya Patel or her role anywhere in the summary; this person and their involvement are entirely absent._
- **multiple_subjects**: Fix was to chunk to 5000 rows per batch — _No mention of any specific batching strategy or row count in the summary; the fix details were never discussed in the original conversation._
- **multiple_subjects**: Took 3 retries to land the fix — _The summary explicitly notes it is unknown whether the deploy was retried at all; no retry count appears anywhere in the summary or original conversation._
- **nested_priorities**: The P0 is a SQL injection in the search endpoint — _The summary contains no information about the nature or location of the P0 finding. It only references 'P0' as a severity level without any detail about SQL injection or the search endpoint._
- **nested_priorities**: P0 is blocking; P1s are after — _While the Next Steps section implies sequencing (triage P0 first, then P1s), the summary never explicitly states that the P0 is 'blocking' in a technical or workflow sense, nor does the original conversation contain this detail — it was not present in the source conversation to be preserved._
- **model_change**: Thinking budget set to 18000 tokens — _This fact does not appear anywhere in the summary and was never mentioned in the original conversation._
- **incident_postmortem**: Sarah Chen was on-call, Emerson on backup — _No mention of Sarah Chen, Emerson, or any on-call personnel anywhere in the summary. This information does not appear in the original conversation either, so it could not have been captured._