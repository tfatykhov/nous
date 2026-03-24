# Memristor Actor-Critic Simulation — Software Spec

> **Status:** Draft v1
> **Type:** Research / Experiment
> **Source paper:** Lammie et al., "Actor–critic networks with analogue memristors mimicking reward-based learning", *Nature Machine Intelligence* (2025). DOI: 10.1038/s42256-025-01149-w
> **Language:** Python (NumPy only, no ML frameworks)
> **Goal:** Reproduce the paper's algorithmic results entirely in software, without memristor hardware, and explore the self-correction mechanism parametrically.

---

## 1. Motivation

The paper demonstrates actor-critic TD learning on analogue memristors. The hardware enables in-memory weight updates, but the **algorithm itself** — including the self-correction property — is mathematical, not physical. A pure software simulation lets us:

1. **Verify** the paper's claims about error-correcting weight updates
2. **Explore** the noise/nonlinearity tolerance envelope beyond what the physical devices allow
3. **Understand** whether self-correction is a general property of in-memory update loops or specific to their device characteristics
4. **Connect** to Nous architecture — the self-correction loop has structural parallels to admission control feedback

---

## 2. What We're Simulating

### 2.1 The Algorithm (fully reproducible)

- **Actor-critic TD learning** with three-factor update rule
- **Critic update:** `Δw_j = α × δ_t × H_cri(j)` where `δ_t = r(s_t) + γ × V(s_{t+1}) - V(s_t)`
- **Actor update:** `Δθ_ij = α × δ_t × H_act(i,j)`
- **Hebbian terms:**
  - `H_cri(j) = x_j(t)` (place cell activity at state j)
  - `H_act(i,j) = x_j(t) × (1_{a_t=i} - π_i(s_t))` (eligibility-like: active place cell × (chosen action indicator minus action probability))
- **Action selection:** softmax over actor outputs `π_i(s) = exp(Σ_j θ_ij × x_j) / Σ_k exp(Σ_j θ_kj × x_j)`

### 2.2 The Memristor Device Model (simulated)

The paper's devices have specific nonlinear characteristics we model mathematically:

- **Nonlinear potentiation/depression curves** — conductance does not change linearly with pulse count
  - Potentiation: approximately `G(p) = G_min + (G_max - G_min) × (1 - exp(-p/τ_p))` (saturating exponential)
  - Depression: approximately `G(p) = G_max - (G_max - G_min) × (1 - exp(-p/τ_d))`
  - Parameters `τ_p`, `τ_d` control nonlinearity severity
- **Update noise (ε₂)** — stochastic cycle-to-cycle variability
  - Modeled as additive Gaussian: `ΔG_actual = ΔG_intended + N(0, σ_noise)`
  - σ_noise calibrated from paper's Fig. 2f histograms
- **Conductance bounds** — `[G_min, G_max]` with saturation at extremes
- **Pulse-count conversion** — `Δp = Δw_des × N` where N = 200 (total pulse levels)

### 2.3 The Self-Correction Mechanism

This is the paper's key insight: because the same memristors that store weights also compute the *next* weight update, errors (ε₁ from nonlinearity, ε₂ from noise) are automatically factored into the next update calculation.

**How it works:**
1. Memristor stores weight `w_t` (with accumulated errors from prior updates)
2. In-memory computation reads the *actual* conductance (including errors) to compute `Δw_des`
3. The TD error `δ_t` is computed using the *actual* (erroneous) weight values
4. So the next update naturally compensates: if `w_t` was set too high, `V(s_t)` reads too high → `δ_t` is more negative → next update pushes weight down

**In software simulation:** we track both `w_ideal` (what the weight should be) and `w_actual` (what the memristor actually stores after noise/nonlinearity). The update calculation uses `w_actual`, reproducing the self-correction loop.

### 2.4 What We Cannot Simulate

- **Energy consumption** — inherently a hardware property (computing where data lives vs. data movement)
- **Speed advantage** — latency of in-memory vs. von Neumann is physical
- **Device-to-device variability** — we can model it statistically, but we don't have their exact device distribution
- **True analog computation** — we compute in digital floating point, which is more precise than real memristors. We compensate by injecting noise.

---

## 3. Environments

### 3.1 T-Maze (Discrete)

- **States:** 9 positions in a T-shaped grid (labeled 0–8)
- **Layout:**
  ```
  6 — 5 — 4 — 7 — 8
              |
              3
              |
              2
              |
              1
              |
              0 (start)
  ```
- **Actions:** 2 per state — forward/backward (states 0–3, 5–8), left/right (state 4)
- **Reward:** +1 at state 6, 0 elsewhere
- **Encoding:** one-hot (9-dim vector, one active place cell per state)
- **Network size:** 9 critic weights, 18 actor weights (9 states × 2 actions)
- **Episode termination:** agent reaches state 6 or 8, or step limit (e.g., 100 steps)

### 3.2 Morris Water Maze (Continuous)

- **Environment:** circular pool, radius R = 1.0, centered at origin
- **Platform:** hidden at fixed location (e.g., (0.3, 0.3)), radius r_platform = 0.1
- **Place cells:** 11 × 11 = 121 cells on a uniform grid covering the pool
  - Activity: radial basis function `x_j = exp(-||s - c_j||² / (2σ²))` where `c_j` is cell center, `σ` = grid spacing
- **Actions:** 8 directions (N, NE, E, SE, S, SW, W, NW), fixed step size
- **Reward:** +1 when agent is within `r_platform` of platform, 0 elsewhere
- **Network size:** 121 critic weights, 121 × 8 = 968 actor weights
- **Episode termination:** agent reaches platform or step limit (e.g., 500 steps)
- **Boundary:** if agent moves outside pool, position is clipped to pool edge

---

## 4. Architecture

### 4.1 Module Structure

```
memristor_sim/
├── __init__.py
├── memristor.py          # Device model (nonlinearity, noise, bounds)
├── actor_critic.py       # AC network with pluggable weight backend
├── environments/
│   ├── __init__.py
│   ├── tmaze.py          # T-maze environment
│   └── water_maze.py     # Morris water maze
├── experiments/
│   ├── __init__.py
│   ├── run_tmaze.py      # T-maze training + plotting
│   ├── run_water_maze.py # Water maze training + plotting
│   └── sweep_noise.py    # Parametric noise/nonlinearity sweep
└── utils/
    ├── __init__.py
    └── plotting.py       # Visualization helpers
```

### 4.2 Key Classes

#### `MemristorArray`
```python
class MemristorArray:
    """Simulates a crossbar array of memristor devices."""
    
    def __init__(self, shape, g_min, g_max, tau_p, tau_d, 
                 noise_std, n_pulses=200):
        self.weights      # actual conductance values (with errors)
        self.g_min        # minimum conductance
        self.g_max        # maximum conductance
        self.tau_p        # potentiation nonlinearity
        self.tau_d        # depression nonlinearity
        self.noise_std    # update noise σ
        self.n_pulses     # total pulse levels (200)
    
    def read(self) -> np.ndarray:
        """Read current weights (actual, with accumulated errors)."""
    
    def update(self, delta_w_des: np.ndarray):
        """Apply desired weight update through pulse conversion.
        
        1. Convert Δw_des → Δp (pulse count), assuming linear
        2. Apply nonlinear potentiation/depression curve
        3. Add Gaussian noise ε₂
        4. Clip to [g_min, g_max]
        """
    
    def compute_update_in_memory(self, indices_t, indices_t1, 
                                  alpha, gamma, reward):
        """In-memory weight update calculation.
        
        Uses actual stored conductances (not ideal values) to compute
        Δw_des = α×r + α×γ×w[t+1] - α×w[t]
        
        This is where self-correction happens — errors in stored weights
        feed into the next update calculation.
        """
```

#### `IdealArray`
```python
class IdealArray:
    """Perfect weights — no noise, no nonlinearity. Baseline comparison."""
    
    def read(self) -> np.ndarray:
    def update(self, delta_w: np.ndarray):
```

#### `ActorCritic`
```python
class ActorCritic:
    """Actor-critic network with pluggable weight backend."""
    
    def __init__(self, n_states, n_actions, weight_backend, 
                 alpha, gamma, use_self_correction=True):
        self.critic_weights  # weight_backend instance for critic
        self.actor_weights   # weight_backend instance for actor
    
    def get_value(self, state_encoding) -> float:
        """V(s) = Σ w_j × x_j using actual memristor reads."""
    
    def get_action_probs(self, state_encoding) -> np.ndarray:
        """π(a|s) via softmax over actor outputs."""
    
    def select_action(self, state_encoding) -> int:
        """Sample action from π(a|s)."""
    
    def update(self, s_t, a_t, r_t, s_t1):
        """One-step TD update for both actor and critic.
        
        If use_self_correction=True:
            δ = r + γ×V_actual(s') - V_actual(s)  # uses memristor reads
            Δw computed in-memory (self-correcting)
        If use_self_correction=False:
            δ = r + γ×V_actual(s') - V_actual(s)
            Δw computed externally (no error compensation)
        """
```

---

## 5. Experiments

### 5.1 Experiment 1: T-Maze Reproduction

**Goal:** Reproduce Fig. 4 from the paper — learning curves and value maps.

- Run 1000 independent training runs, 200 episodes each
- Compare three weight backends:
  - `IdealArray` — perfect weights (upper bound)
  - `MemristorArray` with self-correction ON
  - `MemristorArray` with self-correction OFF
- **Metrics:**
  - Steps to reach reward per episode (learning curve)
  - Converged value map V(s) for all 9 states
  - Converged policy (action probabilities per state)
- **Hyperparameters (from paper):**
  - α = 0.2, γ = 0.9
  - N = 200 pulses
  - Noise and nonlinearity calibrated from paper's Fig. 2e,f

### 5.2 Experiment 2: Morris Water Maze

**Goal:** Reproduce Fig. 5 — learning in continuous space with RBF encoding.

- Run 100 independent training runs, 100 episodes each
- Same three-backend comparison
- **Metrics:**
  - Path length / steps to platform per episode
  - Learned value landscape V(x,y) as 2D heatmap
  - Trajectory visualization (episode 1 vs. episode N)
- **Hyperparameters (from paper):**
  - α = 0.005, γ = 0.99
  - Place cell grid: 11×11, σ = grid spacing
  - Step size: 0.05 × pool radius

### 5.3 Experiment 3: Self-Correction Parametric Sweep

**Goal:** Map the envelope where self-correction works. This goes beyond the paper.

- **Sweep parameters:**
  - Noise level σ_noise: [0, 0.001, 0.005, 0.01, 0.02, 0.05, 0.1]
  - Nonlinearity strength τ_p/τ_d: [10, 20, 50, 100, 200, ∞(linear)]
  - Learning rate α: [0.05, 0.1, 0.2, 0.3, 0.5]
- **For each combination:** run 100 T-maze trials, record convergence rate and final value error
- **Output:** heatmaps showing convergence success rate as f(noise, nonlinearity) with and without self-correction
- **Key question:** At what noise/nonlinearity levels does self-correction break down?

### 5.4 Experiment 4: Comparison with Standard RL

**Goal:** Compare memristor-modeled actor-critic against standard implementations.

- Same environments, same hyperparameters
- Compare with clean TD actor-critic (no memristor model)
- Quantify: does the memristor model (with self-correction) converge to the same solution? How many more episodes does it need?

---

## 6. Outputs / Deliverables

1. **Runnable Python package** — `python -m memristor_sim.experiments.run_tmaze`
2. **Plots reproducing paper figures:**
   - Learning curves (steps vs. episode) with error bands
   - Value maps (T-maze) and value landscapes (water maze)
   - Trajectory plots (random → learned)
3. **Novel analysis plots:**
   - Self-correction tolerance envelope (noise × nonlinearity heatmap)
   - Weight trajectory plots showing error accumulation with/without self-correction
   - Convergence time as f(noise level)
4. **Summary document** with findings

---

## 7. Parameters Reference

| Parameter | T-Maze | Water Maze | Source |
|-----------|--------|------------|--------|
| Learning rate α | 0.2 | 0.005 | Paper |
| Discount factor γ | 0.9 | 0.99 | Paper |
| N (pulse levels) | 200 | 200 | Paper |
| States/place cells | 9 (one-hot) | 121 (11×11 RBF) | Paper |
| Actions | 2 per state | 8 | Paper |
| σ_noise (device) | ~0.01 | ~0.01 | Paper Fig. 2f |
| τ_p (potentiation) | ~50 | ~50 | Estimated from Fig. 2e |
| τ_d (depression) | ~50 | ~50 | Estimated from Fig. 2e |
| G_min | 0.0 (normalized) | 0.0 | Convention |
| G_max | 1.0 (normalized) | 1.0 | Convention |
| RBF σ | N/A | grid spacing | Paper |
| Step size | N/A | 0.05 | Paper |
| Max steps/episode | 100 | 500 | Reasonable |
| Training episodes | 200 | 100 | Paper |
| Independent runs | 1000 | 100 | Paper |

---

## 8. Implementation Notes

### 8.1 Nonlinear Potentiation Model

The paper's devices show saturating potentiation (Fig. 2e). We model this as:

```
For potentiation (Δp > 0 pulses):
  G_new = G_min + (G_max - G_min) × (1 - exp(-(p_current + Δp) / τ_p))
  
For depression (Δp < 0 pulses):  
  G_new = G_max - (G_max - G_min) × (1 - exp(-(N - p_current + |Δp|) / τ_d))
```

Where `p_current` is the equivalent pulse position of the current conductance. This captures the key behavior: updates near saturation boundaries are smaller than expected (the source of error ε₁).

### 8.2 Self-Correction: With vs. Without

- **With (in-memory):** TD error computed using `memristor.read()` which returns actual noisy conductance. The error in the weight IS the error in V(s), which feeds into δ, which feeds into the next Δw. Self-correcting loop.
- **Without:** TD error computed using a separate "ideal shadow" of what the weight should be. Updates are still applied through the noisy memristor, but the update *calculation* doesn't see the errors. No compensation.

### 8.3 Dependencies

- Python 3.10+
- NumPy
- Matplotlib (plotting only)
- No ML frameworks — this is intentionally simple

### 8.4 Performance Estimate

- T-maze: 9 weights, 1000 runs × 200 episodes × ~50 steps = ~10M updates. Seconds on any machine.
- Water maze: 1089 weights, 100 runs × 100 episodes × ~200 steps = ~2M updates × 1089 weights. Maybe a minute.
- Parametric sweep: ~200 combinations × 100 runs. Few minutes total.

---

## 9. Connection to Nous

The self-correction mechanism has a direct parallel in Nous:

- **Memristor loop:** imperfect weight update → error baked into stored weight → next update computation reads actual (erroneous) weight → TD error naturally compensates → convergence despite noise
- **Admission control loop:** imperfect scoring of memory items → some wrong items stored → retrieval quality affected → next scoring iteration sees actual memory state → system self-adjusts

Both are examples of **closed-loop error compensation** — systems that tolerate component-level imperfections because the feedback loop operates on actual state, not assumed state.

If the simulation confirms the paper's claims, it strengthens the theoretical basis for Nous's memory-RL-policy design: you don't need perfect memory scoring, you need a tight feedback loop.

---

## 10. Open Questions for Review

1. **Scope:** Should we implement the general case (RBF encoding for T-maze too) or keep T-maze one-hot as the paper does?
2. **Device model fidelity:** The paper's exact potentiation curves aren't published as data. Our exponential model is an approximation. Good enough, or should we try to digitize their Fig. 2e?
3. **Additional environments?** The paper only tests T-maze and water maze. Should we add a third environment (e.g., cliff walking, frozen lake) to test generalization?
4. **Presentation:** Standalone report, or fold findings into the ongoing article series?
5. **Where to put the code?** Options: (a) inside `nous/` repo under `research/`, (b) separate repo, (c) in cognition-engines.ai as an interactive demo
