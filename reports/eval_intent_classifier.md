# Intent classifier eval — 30 scenarios

- accuracy: **27/30 (90.0%)**
- SUT: `nous.cognitive.intent.IntentClassifier`
- Pattern-matching only; ground truth is hand-labeled.

## Failed scenarios

| name | input | failed checks |
|---|---|---|
| greeting_hello | `Hello, how are you?` | `is_greeting`: got is_greeting=False |
| greeting_morning | `Good morning, Nous` | `is_greeting`: got is_greeting=False |
| greeting_howdy | `Howdy partner` | `is_greeting`: got is_greeting=False |