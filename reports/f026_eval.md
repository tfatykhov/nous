# F026 synthetic eval

## ClaimVerifier
- scenarios: 20
- accuracy: **18/20 (90.0%)**

| name | expected | actual | passed |
|---|---:|---:|---|
| file_save_grounded | 0 | 0 | OK |
| email_grounded | 0 | 0 | OK |
| push_grounded | 0 | 0 | OK |
| file_save_grounded_in_ledger | 0 | 0 | OK |
| file_save_ungrounded | 1 | 1 | OK |
| email_ungrounded | 1 | 1 | OK |
| push_ungrounded | 1 | 1 | OK |
| multiple_claims_one_grounded | 1 | 0 | FAIL |
| multiple_claims_none_grounded | 2 | 1 | FAIL |
| saved_to_path_pattern | 1 | 1 | OK |
| no_claims_plain_response | 0 | 0 | OK |
| no_claims_planning | 0 | 0 | OK |
| no_claims_question | 0 | 0 | OK |
| description_not_claim | 0 | 0 | OK |
| user_message_quoted | 1 | 1 | OK |
| ive_pushed | 0 | 0 | OK |
| just_committed | 1 | 1 | OK |
| email_sent_to_grounded | 0 | 0 | OK |
| email_sent_to_ungrounded | 1 | 1 | OK |
| forwarded_message | 1 | 1 | OK |

## ActionGate Tier 1+2 (deterministic)
- scenarios: 12
- accuracy: **9/12 (75.0%)**

| name | expected | actual | reason | passed |
|---|---|---|---|---|
| read_file_always_allowed | True | True | read-only | OK |
| recall_deep_always_allowed | True | True | read-only | OK |
| bash_status_command_allowed | True | True | read-only | OK |
| write_first_time_allowed | True | True | no-duplicates | OK |
| write_exact_duplicate_blocked | False | True | under-threshold(1/6) | FAIL |
| write_same_path_different_content_allowed | True | True | under-threshold(1/6) | OK |
| write_different_path_allowed | True | True | no-duplicates | OK |
| learn_fact_distinct_allowed | True | True | no-duplicates | OK |
| learn_fact_duplicate_blocked | False | True | under-threshold(1/3) | FAIL |
| record_decision_distinct_allowed | True | True | no-duplicates | OK |
| schedule_first_time_allowed | True | True | no-duplicates | OK |
| schedule_exact_duplicate_blocked | False | True | no-duplicates | FAIL |

## Caveat

F026 components (ActionGate, ClaimVerifier, ExecutionLedger) do not persist their decisions. This eval uses synthetic fixtures with known ground truth; it cannot measure F026's actual production behavior. Add persistence (separate PR) for retrospective accuracy on real data.