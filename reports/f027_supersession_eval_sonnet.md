# F027 Supersession Classifier Eval

- judge_model: `claude-sonnet-4-6`
- facts sampled: 30
- pairs scored: 120
- overall accuracy: **74.17%**

## Per-category accuracy

| truth | n | correct | accuracy | avg_confidence |
|---|---:|---:|---:|---:|
| CONTRADICTION | 30 | 30 | 100.00% | 0.97 |
| UPDATE | 30 | 10 | 33.33% | 0.88 |
| REFINEMENT | 30 | 29 | 96.67% | 0.90 |
| UNRELATED | 30 | 20 | 66.67% | 0.88 |

## Confusion matrix

rows=truth, cols=predicted

| truth \ pred | CONTRADICTION | REFINEMENT | UNRELATED | UPDATE |
|---|---|---|---|---|
| CONTRADICTION | 30 | 0 | 0 | 0 |
| UPDATE | 12 | 8 | 0 | 10 |
| REFINEMENT | 1 | 29 | 0 | 0 |
| UNRELATED | 7 | 3 | 20 | 0 |

## Caveat

Generator and classifier are both `claude-haiku-4-5`. Agreement here is a self-consistency signal, not a strict precision test. Re-run with `--judge-model claude-sonnet-4-6` to use a stronger judge.
