# F027 Supersession Classifier Eval

- judge_model: `claude-haiku-4-5-20251001`
- facts sampled: 30
- pairs scored: 120
- overall accuracy: **83.33%**

## Per-category accuracy

| truth | n | correct | accuracy | avg_confidence |
|---|---:|---:|---:|---:|
| CONTRADICTION | 30 | 24 | 80.00% | 0.94 |
| UPDATE | 30 | 26 | 86.67% | 0.92 |
| REFINEMENT | 30 | 30 | 100.00% | 0.94 |
| UNRELATED | 30 | 20 | 66.67% | 0.91 |

## Confusion matrix

rows=truth, cols=predicted

| truth \ pred | CONTRADICTION | REFINEMENT | UNRELATED | UPDATE |
|---|---|---|---|---|
| CONTRADICTION | 24 | 0 | 0 | 6 |
| UPDATE | 2 | 1 | 1 | 26 |
| REFINEMENT | 0 | 30 | 0 | 0 |
| UNRELATED | 8 | 2 | 20 | 0 |

## Caveat

Generator and classifier are both `claude-haiku-4-5`. Agreement here is a self-consistency signal, not a strict precision test. Re-run with `--judge-model claude-sonnet-4-6` to use a stronger judge.
