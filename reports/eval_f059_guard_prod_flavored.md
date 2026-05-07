# F059 hallucination guard — `prod-flavored` corpus

- scenarios: **15**, errored: **0**
- guard fired (>=1 suspect): **3/15** (20.0%)
- total suspect entities: **5**

| name | turns | summary chars | suspects |
|---|---:|---:|---|
| config_value | 8 | 1392 | 0: — |
| decision_with_rationale | 7 | 1076 | 0: — |
| person_attributes | 6 | 1315 | 2: tom rivers, trivers@acmecorp.com |
| schedule_change | 7 | 1345 | 0: — |
| credential_redacted | 7 | 1450 | 0: — |
| long_chat_one_fact | 7 | 532 | 0: — |
| version_pin | 5 | 1225 | 0: — |
| negative_decision | 5 | 1306 | 1: redis streams |
| fact_in_assistant_turn | 6 | 1280 | 0: — |
| user_preference | 6 | 1179 | 0: — |
| deadline_change | 5 | 824 | 0: — |
| multiple_subjects | 6 | 1212 | 0: — |
| nested_priorities | 6 | 1999 | 2: 2.1, 2.3.7 |
| model_change | 5 | 1008 | 0: — |
| incident_postmortem | 8 | 1208 | 0: — |

## Suspect samples (full lists)

### person_attributes

```
  tom rivers
  trivers@acmecorp.com
```

### negative_decision

```
  redis streams
```

### nested_priorities

```
  2.1
  2.3.7
```
