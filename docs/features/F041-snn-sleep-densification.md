# F041 — SNN Sleep Densification: tinyHippo-Driven Graph Augmentation

> **Status:** Draft  
> **Priority:** P1  
> **Depends on:** F040 (Graph Densification — shipped), F022 (Graph-Augmented Recall — shipped)  
> **Related:** F031 (Sleep Consolidation), tinyHippo (github.com/max-talanov/tinyHippo)  
> **Author:** Nous + Tim  
> **Created:** 2026-04-12  
> **Updated:** 2026-04-12 (fact-checked against actual replay_12pct_stc.h5)

---

## Thesis

Prove that a spiking neural network (SNN) modeled on hippocampal microcircuitry produces **better memory consolidation decisions** than pure software heuristics. Specifically: tinyHippo's bidirectional SWR replay and STC competitive consolidation, run offline during Nous's sleep cycle, creates graph edges that improve retrieval quality compared to F040's cosine-similarity-based densification alone.

---

## Problem Statement

Nous's current graph densification (F040) uses **embedding cosine similarity** to connect memory nodes. This is effective but limited:

- Cosine similarity only finds **surface-level semantic overlap** — misses cross-domain associations
- Thresholds are **hand-tuned** (`graph_threshold_fact_fact`, etc.) with no principled basis
- No **competitive inhibition** — all above-threshold pairs get edges equally, no winner-take-all dynamics
- No **temporal sequence** awareness — doesn't model "memory A precedes memory B" relationships

tinyHippo provides a biologically-grounded alternative: hippocampal SWR replay with STC (Synaptic Tagging and Capture) consolidation that inherently performs competitive weight selection.

---

## Reference File: replay_12pct_stc.h5

**Actual file analyzed:** `replay_12pct_stc.h5` (59.8 MB) from Google Drive  
**Generated:** 2026-04-11T21:14:46 UTC  
**Source:** tinyHippo `replay_scaled.py` with `--ec-lii --stc` flags  
**Scale:** 12% (~93,000 neurons)  
**Simulation:** 7,000 ms with 14 SWR events  
**NEST version:** 3.9.0

### Complete HDF5 Schema (Fact-Checked)

```
ROOT ATTRIBUTES:
  created_utc:     "2026-04-11T21:14:46.449236"
  dt_ms:           10.0              # Time bin width in milliseconds
  n_groups:        35                # Number of sequence groups
  sim_ms:          7000.0            # Total simulation time
  scale:           "12% scale"       # ~93k neurons
  nest_version:    "3.9.0"
  ec_lii_present:  True              # EC Layer II cortical module active
  ec_lii_N:        12005             # EC LII neuron count
  ec_lii_K_ca1:    50                # CA1→EC fan-in per EC neuron
  ec_lii_w_init:   1.0               # Initial synaptic weight
  swr_fwd_start:   300.0             # First forward SWR window start (ms)
  swr_fwd_stop:    420.0             # First forward SWR window end (ms)
  swr_rev_start:   600.0             # First reverse SWR window start (ms)
  swr_rev_stop:    720.0             # First reverse SWR window end (ms)

POPULATIONS (8 total):
  ca3_sup/          # CA3 superficial pyramidal (31,675 cells)
    spk_times       float32[1,859,951]    # All spike times
    spk_senders     int32[1,859,951]      # Cell IDs per spike
    group_ids       int32[35, 905]        # Cell→group mapping (905 cells/group)
    heatmap         float32[35, 700]      # Firing rate per group per time bin
    rate            float32[700]          # Population-average rate per bin

  ca3_deep/         # CA3 deep pyramidal (7,910 cells)
    spk_times       float32[752,750]
    spk_senders     int32[752,750]
    group_ids       int32[35, 226]        # 226 cells/group
    heatmap         float32[35, 700]
    rate            float32[700]

  ca3_int_sup/      # CA3 inhibitory interneurons superficial (2,870 cells)
    spk_times       float32[424,120]
    spk_senders     int32[424,120]
    rate            float32[700]

  ca3_int_deep/     # CA3 inhibitory interneurons deep (945 cells)
    spk_times       float32[138,815]
    spk_senders     int32[138,815]
    rate            float32[700]

  ca1_pyr/          # CA1 pyramidal readout (55,195 cells)
    spk_times       float32[15,335,605]   # ← 15M spikes, richest signal
    spk_senders     int32[15,335,605]
    rate            float32[700]

  ca1_basket/       # CA1 basket interneurons (1,680 cells)
    spk_times       float32[842,123]
    spk_senders     int32[842,123]
    rate            float32[700]

  ca1_olm/          # CA1 OLM interneurons (1,085 cells)
    spk_times       float32[202,404]
    spk_senders     int32[202,404]
    rate            float32[700]

  ec_lii/           # Entorhinal Cortex Layer II (12,005 cells)
    spk_times       float32[2,546,335]
    spk_senders     int32[2,546,335]
    rate            float32[700]

STC (Synaptic Tagging & Capture):
  stc/
    n_synapses      attr: 612,255         # CA1→EC synapses
    n_swr_events    attr: 14              # Total SWR events in simulation
    w_init          attr: 1.0             # Starting weight
    w_final         float32[612,255]      # Final weight per synapse
    ltp_mask        uint8[612,255]        # 1 = achieved L-LTP
    tag_final       float32[612,255]      # Residual tag strength
    post_idx        int32[612,255]        # Which EC neuron this synapse targets
    event           int32[14]             # SWR event indices [1..14]
    t_swr_start     float32[14]           # SWR window start times
    t_swr_end       float32[14]           # SWR window end times
    n_tagged_syn    int32[14]             # Tagged synapses per event
    n_active_syn    int32[14]             # Active synapses per event
    n_ltp_new       int32[14]             # New L-LTP captures per event
    n_ltp_total     int32[14]             # Cumulative L-LTP count
    n_ec_fired      int32[14]             # EC neurons that fired per event
    w_mean          float32[14]           # Mean weight per event
    w_ltp_mean      float32[14]           # Mean weight of L-LTP synapses per event
    prp_mean        float32[14]           # Mean PRP pool per event
    prp_max         float32[14]           # Max PRP pool per event
    prp_pool_final  float32[12,005]       # Final PRP per EC neuron

STATS:
  stats/
    rho_fwd         attr: 0.3070          # Forward replay Spearman ρ
    pval_fwd        attr: 0.0728          # Forward replay p-value
    rho_rev         attr: 0.4305          # Reverse replay Spearman ρ (SIGNIFICANT)
    pval_rev        attr: 0.0098          # Reverse replay p-value
    mean_rate_*     attrs per population  # Mean firing rates

GLOBAL:
  times_ms          float32[700]          # Time bin centers
```

### SWR Event Windows (All 14)

| Event | Type    | Start (ms) | End (ms) |
|-------|---------|-----------|----------|
| 1     | Forward | 300       | 420      |
| 2     | Reverse | 600       | 720      |
| 3     | Forward | 1300      | 1420     |
| 4     | Reverse | 1600      | 1720     |
| 5     | Forward | 2300      | 2420     |
| 6     | Reverse | 2600      | 2720     |
| 7     | Forward | 3300      | 3420     |
| 8     | Reverse | 3600      | 3720     |
| 9     | Forward | 4300      | 4420     |
| 10    | Reverse | 4600      | 4720     |
| 11    | Forward | 5300      | 5420     |
| 12    | Reverse | 5600      | 5720     |
| 13    | Forward | 6300      | 6420     |
| 14    | Reverse | 6600      | 6720     |

Alternating forward/reverse every 300ms gap, repeating every 1000ms cycle.

---

## Honest Assessment: What Works vs What Doesn't In This File

### ❌ Co-Activation Matrix: USELESS for Discrimination

**What the naive approach assumed:** Compute Pearson correlation between group heatmap rows during SWR windows → use as edge weights. Groups that co-fire get connected.

**What the data actually shows:**
- **All 595 group pairs** have correlation > 0.6
- Mean correlation: **0.963** (nearly 1.0)
- Median: **0.995**
- 94.6% of pairs have correlation > 0.8

**Why:** At 12% scale, the network is "hot" — all groups fire during every SWR event. The inhibitory interneurons don't create enough selectivity to silence specific groups. The co-activation matrix is effectively uniform.

**Conclusion:** Cannot use heatmap co-activation as edge weight discriminator with this .h5 file.

### ❌ STC Survival Rate: TOO PERMISSIVE

**What the naive approach assumed:** `ltp_mask` separates "strong" (survived L-LTP) from "weak" (didn't survive) synapses, providing a consolidation threshold.

**What the data actually shows:**
- **98.0%** of synapses (600,250/612,255) achieved L-LTP
- Every EC neuron has exactly **50/51** LTP synapses
- The 12,005 non-LTP synapses = exactly 1 per EC neuron
- Non-LTP synapses have **HIGHER** weight (1.5000) than LTP synapses (mean 1.2941)

**Why:** The PRP pool is uniform across all neurons (all = 14.0) and all synapses get tagged (100% tagged at every event). The competitive inhibition isn't competitive enough at this scale/configuration.

**Conclusion:** Cannot use `ltp_mask` as a binary consolidation gate — it's nearly all-pass.

### ❌ Replay Quality: Below Naive Threshold

**What the spec assumed:** Quality gate at |ρ| > 0.5.

**What the data shows:**
- Forward: ρ = **0.307** (p = 0.073, NOT significant)
- Reverse: ρ = **0.431** (p = 0.010, SIGNIFICANT at α=0.01)

**Implication:** The 0.5 threshold would reject this entire file. But reverse replay IS statistically significant. The quality gate needs to use **p-value** (< 0.05) rather than absolute ρ magnitude.

### ✅ w_final Distribution: REAL Per-Synapse Variance

**This is the primary discriminative signal.**

612,255 synapses with weights forming a bell curve:
- Range: 1.192 → 1.500
- Mean: 1.298, StdDev: ~0.04
- Clear structure: peak around 1.29, tail to 1.40

Per-EC-neuron mean weight has genuine variance:
- Range: 1.245 → 1.355 (std = 0.013)
- This means some EC neurons consolidated stronger than others

**How to use:** The w_final distribution encodes how strongly each synapse was consolidated by competitive SNN dynamics (STC with tag decay, PRP competition). Neurons with higher mean incoming weight were "preferred" by the consolidation process.

### ✅ Bidirectional Replay Sequence: REAL

Odd SWR events → reverse replay (negative ρ):
- SWR 1: ρ = -0.402 (p = 0.017)*
- SWR 3: ρ = -0.402 (p = 0.017)*
- SWR 5: ρ = -0.402 (p = 0.017)*

Even SWR events → forward replay (positive ρ):
- SWR 2: ρ = +0.485 (p = 0.003)**
- SWR 4: ρ = +0.289 (p = 0.093)

The alternating direction is consistent with hippocampal biology. Forward replay strengthens sequential associations (A→B→C). Reverse replay strengthens backward associations (C→B→A).

### ✅ Weight Evolution Trajectory: REAL

```
Events 1-2: w_mean ≈ 1.00 (no LTP yet)
Event 3:    w_mean → 1.31  (massive L-LTP capture, 600,250 new LTP synapses)
Events 4-14: gradual decline 1.31 → 1.30 (STC tag decay + competition)
```

This trajectory encodes **temporal consolidation dynamics** — how quickly the SNN settles and which synapses maintain their strength.

### ✅ Per-SWR Spike Timing: RICHEST SIGNAL

- **15.3M CA1 pyramidal spikes** with millisecond-precision timing
- **1.86M CA3 spikes** organized into 35 groups with known cell→group mapping
- CA3 groups fire at different times during each SWR (e.g., SWR 1: Group 4 leads at 337ms, Group 34 fires last)
- The temporal order of group activation encodes **sequence associations**

---

## Revised Architecture

Given the actual .h5 data, the integration uses **three discriminative signals** (not the co-activation matrix):

### Signal 1: w_final Distribution → Edge Weights

Each of the 612,255 CA1→EC synapses has a unique weight that emerged from SNN competition. Map Nous memories to EC neuron indices, then use the per-neuron mean incoming weight as a **consolidation strength score**.

```python
# Per-EC-neuron consolidation strength
neuron_strength = {}
for n in range(n_ec_neurons):
    mask = post_idx == n
    neuron_strength[n] = w_final[mask].mean()
    
# Range: 1.245 → 1.355
# Normalize to [0, 1] for edge weights
min_w, max_w = min(neuron_strength.values()), max(neuron_strength.values())
for n in neuron_strength:
    neuron_strength[n] = (neuron_strength[n] - min_w) / (max_w - min_w)
```

**Mapping:** Assign Nous memories to EC neurons via embedding-based clustering (KMeans, n_clusters = 12,005 is too many → use 35 groups, then sub-cluster within). Each memory gets a consolidation strength from the mean w_final of its mapped neurons.

### Signal 2: CA3 Spike Timing → Temporal Proximity Edges

During each SWR event, CA3 groups fire in a specific temporal order. Groups that fire close together in time have stronger temporal association than groups that fire far apart.

```python
# For each SWR event, compute CA3 group peak times
for evt_i in range(14):
    s, e = t_swr_start[evt_i], t_swr_end[evt_i]
    for g in range(35):
        cells = ca3_group_ids[g]
        spike_mask = (spk_senders in cells) & (s <= spk_times <= e)
        group_peak_time[evt_i][g] = mean(spk_times[spike_mask])
    
    # Temporal proximity between groups
    for gi in range(35):
        for gj in range(gi+1, 35):
            dt = abs(group_peak_time[evt_i][gi] - group_peak_time[evt_i][gj])
            # Closer in time → stronger association
            temporal_weight[gi][gj] += exp(-dt / tau)
```

**Mapping:** Cluster Nous memories into 35 groups (matching n_groups). Groups whose CA3 representations fire close together get edges. This captures **temporal co-occurrence** — "these memories were replayed together."

### Signal 3: Replay Direction → Edge Directionality

Forward replay (odd events) encodes A→B sequence direction.
Reverse replay (even events) encodes B→A backward association.

```python
# Forward events: group that fires first → second is a "precedes" edge
# Reverse events: group that fires last → first is a "follows" edge
for evt_i in fwd_events:
    order = argsort(group_peak_time[evt_i])
    for rank in range(len(order) - 1):
        add_directed_edge(order[rank], order[rank+1], 
                         relation="snn_temporal_sequence",
                         weight=1.0 / (rank + 1))  # earlier pairs weighted more
```

---

## Integration Pipeline

### Where It Plugs In

`handlers/sleep_handler.py`, Phase 8b — after F040 heuristic densification (line ~886):

```python
# Existing sleep phases:
#   Phase 1-7: standard sleep consolidation
#   Phase 8a: F040 graph densification (cosine similarity)
#   Phase 8b: F041 SNN densification (tinyHippo) ← NEW
#   Phase 9-10: cleanup and stats
```

### Pipeline Steps

```
┌────────────────────────────────────┐
│  STEP 1: LOAD .h5                  │
│  Read replay_12pct_stc.h5          │
│  Parse: w_final, post_idx,         │
│  group_ids, spk_times, spk_senders │
│  t_swr_start, t_swr_end            │
│  Quality gate: p_rev < 0.05        │
└──────────────┬─────────────────────┘
               │
┌──────────────▼─────────────────────┐
│  STEP 2: MAP MEMORIES → GROUPS     │
│  Fetch recent memories (48h)       │
│  KMeans(n_clusters=35) on          │
│  embedding vectors                 │
│  Each memory → one of 35 groups    │
└──────────────┬─────────────────────┘
               │
┌──────────────▼─────────────────────┐
│  STEP 3: EXTRACT SNN SIGNALS       │
│  A. Per-group consolidation:       │
│     Map group→EC neurons→mean_w    │
│  B. Temporal proximity matrix:     │
│     CA3 spike timing per SWR       │
│  C. Replay direction edges:        │
│     Forward vs reverse ordering    │
└──────────────┬─────────────────────┘
               │
┌──────────────▼─────────────────────┐
│  STEP 4: CREATE GRAPH EDGES        │
│  For each memory pair (mi, mj):    │
│    temporal_weight = proximity[g_i, │
│    g_j] from spike timing          │
│    consolidation = mean(strength[  │
│    g_i], strength[g_j])            │
│    edge_weight = temporal_weight * │
│    consolidation                   │
│    If edge_weight > threshold:     │
│      create_edge(mi, mj,          │
│        relation="snn_coactivation",│
│        weight=edge_weight)         │
│  For directed temporal sequences:  │
│    create_edge(mi, mj,            │
│      relation="snn_temporal_seq",  │
│      weight=sequence_weight)       │
└──────────────┬─────────────────────┘
               │
┌──────────────▼─────────────────────┐
│  STEP 5: LOG & STATS               │
│  edges_created, avg_weight,        │
│  coverage (% memories connected),  │
│  comparison with F040 edges        │
└────────────────────────────────────┘
```

### Code: brain/snn_densifier.py

```python
"""
F041 — SNN Sleep Densification.

Reads pre-computed tinyHippo .h5 replay file and creates
graph edges based on SNN consolidation dynamics.

Discriminative signals used:
  1. w_final per-synapse distribution → consolidation strength
  2. CA3 spike timing during SWR → temporal proximity
  3. Replay direction → directed sequence edges
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import h5py
import numpy as np

logger = logging.getLogger(__name__)


class SNNDensifier:
    """Extracts graph edges from tinyHippo .h5 replay data."""

    def __init__(self, h5_path: str | Path):
        self.h5_path = Path(h5_path)
        self._data: dict[str, Any] = {}

    def load(self) -> bool:
        """Load and validate .h5 file. Returns False if quality gate fails."""
        if not self.h5_path.exists():
            logger.warning("H5 file not found: %s", self.h5_path)
            return False

        with h5py.File(self.h5_path, "r") as h5:
            # Quality gate: p-value based, not ρ magnitude
            p_fwd = h5["stats"].attrs["pval_fwd"]
            p_rev = h5["stats"].attrs["pval_rev"]
            rho_fwd = h5["stats"].attrs["rho_fwd"]
            rho_rev = h5["stats"].attrs["rho_rev"]

            if p_fwd > 0.05 and p_rev > 0.05:
                logger.warning(
                    "Both replay directions non-significant "
                    "(p_fwd=%.4f, p_rev=%.4f). Rejecting file.",
                    p_fwd, p_rev,
                )
                return False

            logger.info(
                "Replay quality: fwd ρ=%.3f (p=%.4f), rev ρ=%.3f (p=%.4f)",
                rho_fwd, p_fwd, rho_rev, p_rev,
            )

            # Load STC data
            self._data["w_final"] = h5["stc/w_final"][:]
            self._data["post_idx"] = h5["stc/post_idx"][:]
            self._data["n_ec"] = h5["stc"].attrs["n_ec_neurons"]

            # Load spike data for temporal analysis
            self._data["n_groups"] = h5.attrs["n_groups"]
            self._data["dt_ms"] = h5.attrs["dt_ms"]
            self._data["ca3_group_ids"] = h5["ca3_sup/group_ids"][:]
            self._data["ca3_spk_times"] = h5["ca3_sup/spk_times"][:]
            self._data["ca3_spk_senders"] = h5["ca3_sup/spk_senders"][:]
            self._data["t_swr_start"] = h5["stc/t_swr_start"][:]
            self._data["t_swr_end"] = h5["stc/t_swr_end"][:]
            self._data["rho_fwd"] = rho_fwd
            self._data["rho_rev"] = rho_rev

        return True

    def compute_consolidation_strength(self) -> np.ndarray:
        """Per-group consolidation strength from w_final.
        
        Maps EC neurons to groups based on which CA1 cells
        project to them, then aggregates w_final per group.
        
        Returns: float array [n_groups] normalized to [0, 1].
        """
        w_final = self._data["w_final"]
        post_idx = self._data["post_idx"]
        n_ec = self._data["n_ec"]
        n_groups = self._data["n_groups"]

        # Per-EC-neuron mean weight
        neuron_w = np.zeros(n_ec)
        for n in range(n_ec):
            mask = post_idx == n
            neuron_w[n] = w_final[mask].mean()

        # Partition EC neurons into n_groups buckets
        # (simple equal partition — EC neurons are ordered)
        group_size = n_ec // n_groups
        group_strength = np.zeros(n_groups)
        for g in range(n_groups):
            start = g * group_size
            end = start + group_size if g < n_groups - 1 else n_ec
            group_strength[g] = neuron_w[start:end].mean()

        # Normalize to [0, 1]
        mn, mx = group_strength.min(), group_strength.max()
        if mx > mn:
            group_strength = (group_strength - mn) / (mx - mn)
        else:
            group_strength[:] = 0.5

        return group_strength

    def compute_temporal_proximity(self, tau_ms: float = 20.0) -> np.ndarray:
        """Temporal proximity between groups from CA3 spike timing.
        
        For each SWR event, compute mean spike time per CA3 group.
        Groups that fire closer together get higher proximity score.
        Aggregated across all 14 SWR events.
        
        Args:
            tau_ms: Time constant for exponential decay (ms).
                    Smaller = more selective (only very close groups link).
        
        Returns: float array [n_groups, n_groups] normalized to [0, 1].
        """
        n_groups = self._data["n_groups"]
        group_ids = self._data["ca3_group_ids"]
        spk_times = self._data["ca3_spk_times"]
        spk_senders = self._data["ca3_spk_senders"]
        t_starts = self._data["t_swr_start"]
        t_ends = self._data["t_swr_end"]

        proximity = np.zeros((n_groups, n_groups))

        # Pre-build sender→group lookup
        sender_to_group = {}
        for g in range(n_groups):
            for cell_id in group_ids[g]:
                sender_to_group[int(cell_id)] = g

        for evt_i in range(len(t_starts)):
            s, e = t_starts[evt_i], t_ends[evt_i]
            mask = (spk_times >= s) & (spk_times <= e)
            evt_times = spk_times[mask]
            evt_senders = spk_senders[mask]

            # Mean spike time per group
            group_mean_t = np.full(n_groups, np.nan)
            for g in range(n_groups):
                cells = set(int(c) for c in group_ids[g])
                cell_mask = np.isin(evt_senders, list(cells))
                if cell_mask.sum() > 0:
                    group_mean_t[g] = evt_times[cell_mask].mean()

            # Temporal proximity: exp(-|dt|/tau)
            for gi in range(n_groups):
                if np.isnan(group_mean_t[gi]):
                    continue
                for gj in range(gi + 1, n_groups):
                    if np.isnan(group_mean_t[gj]):
                        continue
                    dt = abs(group_mean_t[gi] - group_mean_t[gj])
                    proximity[gi, gj] += np.exp(-dt / tau_ms)
                    proximity[gj, gi] = proximity[gi, gj]

        # Normalize to [0, 1]
        mx = proximity.max()
        if mx > 0:
            proximity /= mx

        return proximity

    def compute_sequence_edges(self) -> list[tuple[int, int, float]]:
        """Directed edges from replay temporal ordering.
        
        Forward replay (odd events): group firing order → sequence edges.
        Reverse replay (even events): reversed order → backward edges.
        
        Returns: list of (source_group, target_group, weight) tuples.
        """
        n_groups = self._data["n_groups"]
        group_ids = self._data["ca3_group_ids"]
        spk_times = self._data["ca3_spk_times"]
        spk_senders = self._data["ca3_spk_senders"]
        t_starts = self._data["t_swr_start"]
        t_ends = self._data["t_swr_end"]

        edges: dict[tuple[int, int], float] = {}

        for evt_i in range(len(t_starts)):
            s, e = t_starts[evt_i], t_ends[evt_i]
            mask = (spk_times >= s) & (spk_times <= e)
            evt_times = spk_times[mask]
            evt_senders = spk_senders[mask]

            # Mean spike time per group
            group_mean_t = {}
            for g in range(n_groups):
                cells = set(int(c) for c in group_ids[g])
                cell_mask = np.isin(evt_senders, list(cells))
                if cell_mask.sum() > 0:
                    group_mean_t[g] = evt_times[cell_mask].mean()

            # Sort by firing time
            ordered = sorted(group_mean_t.items(), key=lambda x: x[1])

            # Create sequence edges (adjacent in temporal order)
            for rank in range(len(ordered) - 1):
                src, _ = ordered[rank]
                tgt, _ = ordered[rank + 1]
                weight = 1.0 / (rank + 1)  # Earlier pairs weighted more
                key = (src, tgt)
                edges[key] = edges.get(key, 0.0) + weight

        # Normalize
        mx = max(edges.values()) if edges else 1.0
        return [(s, t, w / mx) for (s, t), w in edges.items()]

    def compute_all(self) -> dict[str, Any]:
        """Compute all signals. Returns dict with results + metadata."""
        strength = self.compute_consolidation_strength()
        proximity = self.compute_temporal_proximity()
        sequences = self.compute_sequence_edges()

        return {
            "n_groups": self._data["n_groups"],
            "consolidation_strength": strength,
            "temporal_proximity": proximity,
            "sequence_edges": sequences,
            "rho_fwd": self._data["rho_fwd"],
            "rho_rev": self._data["rho_rev"],
            "n_swr_events": len(self._data["t_swr_start"]),
        }
```

### Code: Sleep Handler Integration

```python
# In handlers/sleep_handler.py, add Phase 8b:

async def _phase_snn_densification(self, session, sleep_stats):
    """Phase 8b: SNN-driven graph densification using tinyHippo replay."""
    from brain.snn_densifier import SNNDensifier
    from sklearn.cluster import KMeans

    h5_path = self._settings.get("tinyhippo_h5_path")
    if not h5_path:
        logger.info("No tinyHippo .h5 configured, skipping SNN densification")
        return

    densifier = SNNDensifier(h5_path)
    if not densifier.load():
        sleep_stats["snn_status"] = "rejected_quality"
        return

    signals = densifier.compute_all()
    n_groups = signals["n_groups"]
    proximity = signals["temporal_proximity"]
    strength = signals["consolidation_strength"]
    sequences = signals["sequence_edges"]

    # Fetch recent memories
    recent = await self._get_recent_memories(session, hours=48)
    if len(recent) < n_groups:
        logger.warning("Only %d memories, need at least %d", len(recent), n_groups)
        sleep_stats["snn_status"] = "insufficient_memories"
        return

    # Cluster memories into n_groups
    embeddings = np.array([m.embedding for m in recent])
    labels = KMeans(n_clusters=n_groups, n_init=10, random_state=42).fit_predict(embeddings)

    edges_created = 0
    edges_skipped = 0

    # A. Temporal proximity edges (undirected)
    for gi in range(n_groups):
        for gj in range(gi + 1, n_groups):
            prox = proximity[gi, gj]
            cons = (strength[gi] + strength[gj]) / 2.0
            edge_weight = prox * cons

            if edge_weight < 0.1:  # Threshold
                edges_skipped += 1
                continue

            mems_i = [m for m, l in zip(recent, labels) if l == gi]
            mems_j = [m for m, l in zip(recent, labels) if l == gj]

            # Connect representative pairs (centroid-closest from each group)
            # to avoid O(n²) edge explosion
            for mi in mems_i[:3]:  # Top 3 per group
                for mj in mems_j[:3]:
                    await self._linker.create_edge(
                        source_id=mi.id,
                        target_id=mj.id,
                        source_type=mi.type,
                        target_type=mj.type,
                        relation="snn_coactivation",
                        weight=float(edge_weight),
                        metadata={
                            "source": "tinyhippo",
                            "signal": "temporal_proximity",
                            "groups": [gi, gj],
                        },
                        session=session,
                    )
                    edges_created += 1

    # B. Sequence edges (directed)
    for src_g, tgt_g, seq_weight in sequences:
        if seq_weight < 0.1:
            continue

        src_mems = [m for m, l in zip(recent, labels) if l == src_g]
        tgt_mems = [m for m, l in zip(recent, labels) if l == tgt_g]

        for mi in src_mems[:2]:
            for mj in tgt_mems[:2]:
                await self._linker.create_edge(
                    source_id=mi.id,
                    target_id=mj.id,
                    source_type=mi.type,
                    target_type=mj.type,
                    relation="snn_temporal_sequence",
                    weight=float(seq_weight),
                    metadata={
                        "source": "tinyhippo",
                        "signal": "replay_sequence",
                        "direction": "forward" if src_g < tgt_g else "reverse",
                    },
                    session=session,
                )
                edges_created += 1

    sleep_stats["snn_edges_created"] = edges_created
    sleep_stats["snn_edges_skipped"] = edges_skipped
    sleep_stats["snn_groups_used"] = n_groups
    sleep_stats["snn_rho_fwd"] = signals["rho_fwd"]
    sleep_stats["snn_rho_rev"] = signals["rho_rev"]
    sleep_stats["snn_status"] = "completed"

    logger.info(
        "SNN densification complete: %d edges created, %d skipped",
        edges_created, edges_skipped,
    )
```

---

## Graph Schema

### New Relation Types

| Relation | Directed? | Source |
|----------|-----------|--------|
| `snn_coactivation` | No | Temporal proximity during SWR replay |
| `snn_temporal_sequence` | Yes | Firing order during directed replay |

### Edge Metadata

```json
{
  "source": "tinyhippo",
  "signal": "temporal_proximity|replay_sequence",
  "groups": [3, 17],
  "direction": "forward|reverse",
  "h5_file": "replay_12pct_stc.h5",
  "h5_created": "2026-04-11T21:14:46"
}
```

### Database Migration

```sql
-- No schema change needed: graph_edges already supports arbitrary relations
-- and JSON metadata. Just ensure the relation values are indexed.
-- Verify with:
SELECT DISTINCT relation FROM brain.graph_edges;
-- Add to documentation only.
```

---

## Configuration

```yaml
# nous.yaml or environment variables
tinyhippo:
  h5_path: "/data/tinyhippo/replay_12pct_stc.h5"  # Path to .h5 file
  enabled: true
  quality_gate_p: 0.05                # p-value threshold for replay quality
  temporal_tau_ms: 20.0               # Exponential decay for temporal proximity
  edge_threshold: 0.1                 # Minimum edge weight to create
  max_edges_per_group_pair: 3         # Limit to prevent edge explosion
  max_sequence_edges_per_pair: 2      # Directed edge limit
```

---

## A/B Test Design

### Setup

```
Week 1 (Control):   Sleep with F040 only (cosine similarity densification)
Week 2 (Treatment): Sleep with F040 + F041 (cosine + SNN densification)
```

### Metrics

| Metric | How to Measure | Expected |
|--------|---------------|----------|
| Recall precision@5 | Query 20 test prompts, count relevant results in top 5 | Treatment higher |
| Graph connectivity | `SELECT AVG(degree) FROM graph_node_stats` | Treatment 15-30% higher |
| Cross-domain edges | Edges connecting different memory types (episode↔fact) | Treatment has more |
| Orphan reduction | Memories with 0 edges | Treatment has fewer |
| Edge overlap | % of SNN edges that duplicate F040 edges | < 80% (if > 80%, SNN adds no value) |
| Tim's subjective rating | Blind test: "which recall set is more useful?" | Treatment preferred |

### Falsification Criteria

The SNN integration provides **no value** if any of these are true:
1. **> 80% edge overlap** with F040 — SNN just rediscovered cosine similarity
2. **No precision improvement** on test queries — edges exist but don't help retrieval
3. **All group pairs get edges** — temporal proximity is as uniform as co-activation was (would need to tune tau_ms down)

---

## Known Limitations

### This .h5 File Specifically

1. **Fixed topology** — CA3 groups are wired as sequential chain (0→1→2→...→34). The SNN replays this fixed sequence. It did NOT discover novel connections from Nous's memories.

2. **98% LTP survival** — Nearly all synapses consolidated. The competitive inhibition at 12% scale isn't selective enough. Future runs could increase inhibitory strength or use 100% scale.

3. **Moderate replay quality** — Best ρ is 0.431 (reverse). Good enough to use, but not strong. Higher-scale runs (100% on HPC) should produce cleaner replay.

4. **No pre_idx** — The .h5 doesn't record WHICH CA1 cell feeds which EC synapse. We can't do direct CA1→EC→memory mapping, only statistical grouping.

5. **Static file** — Same .h5 reused every sleep cycle. The SNN doesn't learn from Nous's actual memories. New edges will follow the same template applied to different memory clusterings.

### Architectural

6. **KMeans clustering is arbitrary** — Mapping memories to 35 groups via KMeans may not reflect meaningful categories. The SNN's temporal patterns are overlaid on this arbitrary grouping.

7. **Transfer learning assumption** — We're assuming hippocampal replay dynamics generalize from a fixed-topology simulation to arbitrary memory associations. This is the core hypothesis being tested.

---

## Phase 2: Custom tinyHippo Runs (Future)

To address limitations 1 and 5, Phase 2 would:

1. **Generate connectivity from Nous memories** — Build CA3 group wiring based on actual embedding similarity between memory clusters (not sequential chain)
2. **Run tinyHippo with custom topology** — Requires Max Talanov's help to parameterize `replay_scaled.py` with arbitrary connectivity matrices
3. **Produce memory-specific .h5** — Each sleep cycle generates a new .h5 from that cycle's memories
4. **Feedback loop** — Consolidation results feed back into next run's connectivity

This requires:
- Modifying tinyHippo's `sequence_connect_ca3_layered()` to accept arbitrary adjacency matrices
- A Nous→tinyHippo parameter export pipeline
- Access to NEST (local install or HPC job submission)

---

## Implementation Plan

### Phase 1: Static .h5 Integration (~555 LOC, 1-2 weeks)

| Component | LOC | Description |
|-----------|-----|-------------|
| `brain/snn_densifier.py` | ~250 | HDF5 reader + signal extraction |
| Sleep handler Phase 8b | ~120 | Integration with `_phase_snn_densification` |
| Config schema | ~30 | YAML config for h5_path, thresholds |
| Graph relation types | ~20 | Add `snn_coactivation`, `snn_temporal_sequence` |
| Tests | ~100 | Unit tests with synthetic .h5 data |
| Stats/logging | ~35 | Sleep stats extension |

### Dependencies

- `h5py` — HDF5 file reading (pip install)
- `scikit-learn` — KMeans clustering (already in requirements)
- `numpy` — array operations (already available)
- tinyHippo .h5 file at configured path

### Risks

| Risk | Probability | Mitigation |
|------|-------------|------------|
| Temporal proximity also uniform | Medium | Tune tau_ms; try CA3 deep instead of sup |
| KMeans produces bad clusters | Low | Try HDBSCAN, or use existing memory categories |
| h5py not available in prod | Low | Add to requirements.txt |
| Edge explosion with 35×35 groups | Medium | Cap at max_edges_per_group_pair |

---

## References

- tinyHippo: https://github.com/max-talanov/tinyHippo
- `replay_scaled.py` — main simulation script (SWR generation, STC hooks)
- `replay_plot_from_hdf5.py` — offline .h5 visualization
- F040: `docs/features/F040-graph-densification.md`
- F022: `docs/features/F022-graph-augmented-recall.md`
- Nous sleep handler: `handlers/sleep_handler.py` (lines 886+)
- Graph linker: `brain/graph_linker.py`
