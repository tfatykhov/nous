# Intent classifier eval — 30 scenarios

- accuracy: **27/30 (90.0%)**
- SUT: `nous.cognitive.intent.IntentClassifier`
- Pattern-matching only; ground truth is hand-labeled.

## Failed scenarios

| name | input | failed checks |
|---|---|---|
| hint_multi | `How do we decide what to deploy?` | `memory_type_hints_min_two`: got 1 hints |
| mixed_recent_question_decision | `Did we today decide whether to use Redis?` | `memory_type_hints`: got hints=[] (expected ⊇ ['decision']) |
| mixed_procedure_recency | `How did we deploy yesterday?` | `memory_type_hints`: got hints=[] (expected ⊇ ['procedure']) |