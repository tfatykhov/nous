# F027 Supersession Classifier Eval

- judge_model: `claude-haiku-4-5-20251001`
- facts sampled: 30
- pairs scored: 120
- overall accuracy: **86.67%**

## Per-category accuracy

| truth | n | correct | accuracy | avg_confidence |
|---|---:|---:|---:|---:|
| CONTRADICTION | 30 | 28 | 93.33% | 0.92 |
| UPDATE | 30 | 27 | 90.00% | 0.93 |
| REFINEMENT | 30 | 29 | 96.67% | 0.91 |
| UNRELATED | 30 | 20 | 66.67% | 0.91 |

## Confusion matrix

rows=truth, cols=predicted

| truth \ pred | CONTRADICTION | REFINEMENT | UNRELATED | UPDATE |
|---|---|---|---|---|
| CONTRADICTION | 28 | 0 | 0 | 2 |
| UPDATE | 2 | 1 | 0 | 27 |
| REFINEMENT | 1 | 29 | 0 | 0 |
| UNRELATED | 7 | 3 | 20 | 0 |

## Caveat

Generator and classifier are both `claude-haiku-4-5`. Agreement here is a self-consistency signal, not a strict precision test. Re-run with `--judge-model claude-sonnet-4-6` to use a stronger judge.
