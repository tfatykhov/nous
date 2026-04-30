# Calibration eval — agent_id=nous-prod-snapshot
- decisions analyzed: 401
- bins: 10

## Aggregate
- mean confidence: **0.834**
- mean outcome (strict): 0.636 (gap +0.198)
- mean outcome (lenient, partial=0.5): 0.658 (gap +0.175)

- Brier (strict): **0.2520**, ECE 0.1986, MCE 0.6500
- Brier (lenient): 0.2279, ECE 0.1861, MCE 0.3000

**Verdict:** OVERCONFIDENT by +19.8% on average. Brier score is POOR — confidence is barely informative. ECE is high — substantial miscalibration in specific bins.

## Reliability curve (strict scoring)

| bin | n | % | mean_conf | mean_acc | gap |
|---|---:|---:|---:|---:|---:|
| [0.0,0.1) | 0 | 0% | – | – | – |
| [0.1,0.2) | 0 | 0% | – | – | – |
| [0.2,0.3) | 0 | 0% | – | – | – |
| [0.3,0.4) | 6 | 1.5% | 0.300 | 0.000 | +0.300 |
| [0.4,0.5) | 0 | 0% | – | – | – |
| [0.5,0.6) | 18 | 4.5% | 0.500 | 0.278 | +0.222 |
| [0.6,0.7) | 1 | 0.2% | 0.650 | 0.000 | +0.650 |
| [0.7,0.8) | 28 | 7.0% | 0.745 | 0.750 | -0.005 |
| [0.8,0.9) | 215 | 53.6% | 0.825 | 0.609 | +0.216 |
| [0.9,1.0) | 133 | 33.2% | 0.937 | 0.737 | +0.200 |

## Per-category

| category | n | mean_conf | mean_acc | gap | brier |
|---|---:|---:|---:|---:|---:|
| architecture | 205 | 0.831 | 0.698 | +0.133 | 0.2173 |
| process | 105 | 0.890 | 0.676 | +0.213 | 0.2720 |
| tooling | 75 | 0.745 | 0.360 | +0.385 | 0.3507 |
| security | 11 | 0.919 | 0.909 | +0.010 | 0.0721 |
| integration | 5 | 0.924 | 0.800 | +0.124 | 0.1683 |

## Per-stakes

| stakes | n | mean_conf | mean_acc | gap | brier |
|---|---:|---:|---:|---:|---:|
| low | 34 | 0.875 | 0.706 | +0.169 | 0.2317 |
| medium | 218 | 0.835 | 0.601 | +0.234 | 0.2711 |
| high | 148 | 0.822 | 0.669 | +0.153 | 0.2302 |
| critical | 1 | 0.950 | 1.000 | -0.050 | 0.0025 |

## Method

- **Brier score** = mean((confidence − outcome)²) — 0 perfect, 0.25 random
- **ECE** = Σ (n_bin / N) · |mean_conf_bin − mean_acc_bin|
- **MCE** = max over bins of |mean_conf − mean_acc|
- **Strict**: success=1, partial=failure=0
- **Lenient**: success=1, partial=0.5, failure=0
- Pending decisions excluded.