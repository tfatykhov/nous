# Calibration scaling simulation — agent_id=nous-prod-snapshot
- decisions: 401
- global factor: 0.7627
- best strategy by ECE: **global**

## Strategy comparison

| strategy | mean_conf | gap | Brier | ECE |
|---|---:|---:|---:|---:|
| none | 0.834 | +0.198 | 0.2520 | 0.1986 |
| global | 0.636 | +0.000 | 0.2147 | 0.0333 |
| per_category_floor | 0.629 | -0.007 | 0.2053 | 0.0506 |
| clipped_min | 0.631 | -0.005 | 0.2053 | 0.0503 |

## Per-category factors (clipped_min, floor=0.50)

| category | factor |
|---|---:|
| architecture | 0.8396 |
| process | 0.7602 |
| tooling | 0.5000 |

Categories with n < 20 fall back to global factor (0.7627).

## Recommendation

Ship **global** scaling: ΔBrier +0.0373, ΔECE +0.1653 versus no scaling.