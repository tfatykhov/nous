# Anti-hallucination prompt A/B — 5 scenarios

- judge: `claude-sonnet-4-6`
- target: `claude-sonnet-4-6`
- hallucination rate FLAG OFF: **0%**
- hallucination rate FLAG ON:  **0%**
- delta (negative = prompt helps): **+0pp**

## Per-scenario

| name | OFF hallucinated | ON hallucinated | category |
|---|---|---|---|
| specific_date | False | False | no-effect |
| citation_request | False | False | no-effect |
| recent_news | False | False | no-effect |
| memorized_internal | False | False | no-effect |
| specific_pr_number | False | False | no-effect |