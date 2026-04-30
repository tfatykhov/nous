# F031 contradiction-resolution synthetic eval

- resolver model: `claude-sonnet-4-6`
- pairs: 30
- overall accuracy (after 0.7 floor): **37%**
- total downgrades to KEEP_BOTH: 0/30

## Per-category

| truth | n | correct | acc | avg_conf | downgrades |
|---|---:|---:|---:|---:|---:|
| SUPERSEDE_A | 5 | 4 | 80% | 0.92 | 0 |
| SUPERSEDE_B | 5 | 1 | 20% | 0.85 | 0 |
| MERGE | 5 | 2 | 40% | 0.93 | 0 |
| KEEP_BOTH | 5 | 4 | 80% | 0.96 | 0 |
| REMOVE_A | 5 | 0 | 0% | 0.88 | 0 |
| REMOVE_B | 5 | 0 | 0% | 0.89 | 0 |

## Confusion matrix (post-downgrade)

rows=ground truth, cols=action after 0.7-floor downgrade

| truth \ pred | KEEP_BOTH | MERGE | SUPERSEDE_A | SUPERSEDE_B |
|---|---|---|---|---|
| SUPERSEDE_A | 0 | 1 | 4 | 0 |
| SUPERSEDE_B | 0 | 0 | 4 | 1 |
| MERGE | 3 | 2 | 0 | 0 |
| KEEP_BOTH | 4 | 1 | 0 | 0 |
| REMOVE_A | 0 | 1 | 3 | 1 |
| REMOVE_B | 0 | 0 | 4 | 1 |

## Raw action distribution (before 0.7 floor)

| action | n |
|---|---:|
| SUPERSEDE_A | 15 |
| KEEP_BOTH | 7 |
| MERGE | 5 |
| SUPERSEDE_B | 3 |

## Confidence histogram

| bin | n |
|---|---:|
| [0.0, 0.50) | 0 |
| [0.5, 0.60) | 0 |
| [0.6, 0.70) | 0 |
| [0.7, 0.80) | 1 |
| [0.8, 0.90) | 10 |
| [0.9, 1.01) | 19 |

**Floor effect**: 0/30 non-KEEP_BOTH actions silently downgraded by the 0.7 floor at `sleep_handler.py:668`.

## Caveat

Generator (Haiku) and resolver may share systematic biases. The categories REMOVE_A and REMOVE_B are conceptually adjacent to SUPERSEDE_A/B; intra-pair confusion is expected. Use the raw-action distribution and downgrade rate as the load-bearing signals.