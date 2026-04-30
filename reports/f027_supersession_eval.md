# F027 Supersession Classifier Eval

- judge_model: `claude-haiku-4-5-20251001`
- facts sampled: 30
- pairs scored: 120
- overall accuracy: **77.50%**

## Per-category accuracy

| truth | n | correct | accuracy | avg_confidence |
|---|---:|---:|---:|---:|
| CONTRADICTION | 30 | 29 | 96.67% | 0.96 |
| UPDATE | 30 | 16 | 53.33% | 0.90 |
| REFINEMENT | 30 | 30 | 100.00% | 0.92 |
| UNRELATED | 30 | 18 | 60.00% | 0.90 |

## Confusion matrix

rows=truth, cols=predicted

| truth \ pred | CONTRADICTION | REFINEMENT | UNRELATED | UPDATE |
|---|---|---|---|---|
| CONTRADICTION | 29 | 1 | 0 | 0 |
| UPDATE | 7 | 7 | 0 | 16 |
| REFINEMENT | 0 | 30 | 0 | 0 |
| UNRELATED | 8 | 4 | 18 | 0 |

## Caveat

Generator and classifier are both `claude-haiku-4-5`. Agreement here is a self-consistency signal, not a strict precision test. Re-run with `--judge-model claude-sonnet-4-6` to use a stronger judge.
