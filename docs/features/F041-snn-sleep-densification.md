# F041 — SNN Sleep Densification: tinyHippo-Driven Graph Augmentation

> **Status:** Draft  
> **Priority:** P1  
> **Depends on:** F040 (Graph Densification — shipped), F022 (Graph-Augmented Recall — shipped)  
> **Related:** F031 (Sleep Consolidation), tinyHippo (github.com/max-talanov/tinyHippo)  
> **Author:** Nous + Tim  
> **Created:** 2026-04-12

---

## Thesis

Prove that a spiking neural network (SNN) modeled on hippocampal microcircuitry produces **better memory consolidation decisions** than pure software heuristics. Specifically: tinyHippo's bidirectional SWR replay and STC competitive consolidation, run offline during Nous's sleep cycle, creates graph edges that improve retrieval quality compared to F040's cosine-similarity-based densification alone.

---

## Problem Statement

Nous's current graph densification (F040) uses **embedding cosine similarity** to connect memory nodes. This is effective but limited:

1. **Cosine similarity is symmetric and static** — it cannot model temporal sequence relationships, competitive inhibition, or consolidation dynamics.
2. **Thresholds are hand-tuned** — `graph_threshold_fact_fact: 0.82`, `fact_decision: 0.72`, etc. These have no biological or empirical basis.
3. **No replay dynamics** — in biological memory, sleep replay selectively strengthens some associations and weakens others through competitive inhibition. F040 treats all above-threshold pairs equally.
4. **No consolidation gating** — the brain's STC (Synaptic Tagging and Capture) mechanism ensures only memories that survive protein-synthesis-dependent consolidation become permanent. F040 has no equivalent filter.

### What tinyHippo Provides

tinyHippo (github.com/max-talanov/tinyHippo) is a biologically realistic hippocampal microcircuit simulation built on NEST with Izhikevich neurons. A single run produces an HDF5 file containing:

- **SWR replay dynamics** — bidirectional (forward + reverse) sharp-wave ripple events showing which neuron groups co-activated during replay
- **STC consolidation results** — which synapses survived competitive consolidation to achieve L-LTP (late long-term potentiation)
- **Replay quality metrics** — Spearman ρ correlation measuring replay fidelity

These outputs encode the **results of actual neural computation** — not heuristics. The SNN's inhibitory interneurons, competitive synaptic tagging, and replay dynamics produce association patterns that cosine similarity cannot.

---

## Design: Use tinyHippo Only During Sleep

### Why Sleep-Only Is Correct

This mirrors actual neurobiology:
- The hippocampus performs consolidation replay **during sleep**, not during active recall
- tinyHippo simulates this exact process (SWR events are a sleep phenomenon)
- **Latency**: eliminated — sleep has no time constraint, tinyHippo can run for minutes
- **Encoding bridge at query time**: eliminated — batch-encode during sleep, not per-query
- **Proving SNN works**: clean A/B test — graph before vs after SNN-augmented sleep

### Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    NOUS SLEEP CYCLE                      │
│                                                          │
│  Phase 1-7: existing phases (prune, compress, reflect…) │
│                                                          │
│  Phase 8: F040 cosine-similarity densification          │
│                                                          │
│  Phase 8b: F041 SNN densification (NEW)                 │
│    ┌─────────────────────────────────────────────┐      │
│    │ 1. COLLECT: batch recent memories            │      │
│    │ 2. CLUSTER: KMeans → n_groups buckets        │      │
│    │ 3. READ H5: load pre-computed tinyHippo run  │      │
│    │ 4. EXTRACT: co-activation matrix + STC gates │      │
│    │ 5. MAP: group pairs → memory ID pairs        │      │
│    │ 6. WRITE: create weighted graph edges         │      │
│    └─────────────────────────────────────────────┘      │
│                                                          │
│  Phase 9-10: generalize, evolve rubric                  │
│                                                          │
│  Nous wakes up → existing F022 recall traverses         │
│  the enriched graph → better retrieval                  │
└─────────────────────────────────────────────────────────┘
```

---

## HDF5 Schema (Fact-Checked Against tinyHippo Source)

All field names verified against `replay_scaled.py` lines 1497-1690 (`save_replay_hdf5()`).

### Root Attributes

| Attribute | Type | Source Line | Description |
|-----------|------|-------------|-------------|
| `sim_ms` | float | L1588 (h5.attrs) | Total simulation duration in milliseconds |
| `n_groups` | int | L1588 (h5.attrs) | Number of sequence groups (default 20, scales as `max(10, round(10 * sqrt(pct)))`) |
| `swr_fwd_start` | float | L1588 (h5.attrs) | Forward SWR replay window start (ms) |
| `swr_fwd_stop` | float | L1588 (h5.attrs) | Forward SWR replay window end (ms) |
| `swr_rev_start` | float | L1588 (h5.attrs) | Reverse SWR replay window start (ms) |
| `swr_rev_stop` | float | L1588 (h5.attrs) | Reverse SWR replay window end (ms) |
| `scale` | str | L1588 (h5.attrs) | Scale label (e.g., "100% scale") |
| `dt_ms` | float | L1588 (h5.attrs) | Time bin width used for heatmap (from `bin_ms` param, default 10.0) |

### `/ca3_sup/` — CA3 Superficial Layer (Primary Replay Data)

| Dataset | Shape | Dtype | Description |
|---------|-------|-------|-------------|
| `spk_times` | `[n_spikes]` | float32 | Raw spike timestamps (ms), gzip compressed |
| `spk_senders` | `[n_spikes]` | int32 | Neuron IDs that spiked, gzip compressed |
| `rate` | `[n_bins]` | float64 | Population-mean firing rate per time bin (Hz) |
| `group_ids` | `[n_groups, group_size]` | int32 | Neuron ID membership per sequence group |
| `heatmap` | `[n_groups, n_bins]` | float32 | **KEY DATA** — Per-group firing rate per time bin |

### `/ca3_deep/` — CA3 Deep Layer

Same structure as `/ca3_sup/` but **no heatmap dataset** (verified: only `spk_times`, `spk_senders`, `rate`, `group_ids`).

### `/ca3_int_sup/`, `/ca3_int_deep/` — Inhibitory Interneurons

| Dataset | Shape | Dtype | Description |
|---------|-------|-------|-------------|
| `spk_times` | `[n_spikes]` | float32 | Interneuron spike times |
| `spk_senders` | `[n_spikes]` | int32 | Interneuron IDs |
| `rate` | `[n_bins]` | float64 | Mean firing rate |

### `/ca1_pyr/`, `/ca1_basket/`, `/ca1_olm/` — CA1 Populations

Same structure: `spk_times`, `spk_senders`, `rate`.

### `/ec_lii/` — Entorhinal Cortex Layer II (only with `--ec-lii` flag)

| Dataset/Attr | Type | Description |
|-------------|------|-------------|
| `spk_times` | float32[] | EC neuron spike times |
| `spk_senders` | int32[] | EC neuron IDs |
| `rate` | float64[] | Mean firing rate |
| `.attrs["n_cells"]` | int | Number of EC neurons |
| `.attrs["w_init"]` | float | Initial EC→CA1 weight |
| `.attrs["w_ca1_ec_note"]` | str | Weight description |

### `/stats` — Replay Quality Metrics

| Attribute | Type | Description |
|-----------|------|-------------|
| `rho_fwd` | float | **Spearman ρ for forward replay** — correlation between group index and mean spike time during forward SWR window. Positive = correct temporal order. |
| `pval_fwd` | float | p-value for forward ρ |
| `rho_rev` | float | **Spearman ρ for reverse replay** — negative = correct reverse temporal order. |
| `pval_rev` | float | p-value for reverse ρ |
| `mean_rate_{pop}` | float | Mean firing rate per population (Hz). Keys: `ca3_sup`, `ca3_deep`, `ca3_int_sup`, `ca3_int_deep`, `ca1_pyr`, `ca1_basket`, `ca1_olm`, optionally `ec_lii` |

**Replay quality interpretation** (from `replay_scaled.py` lines 1455-1465):
- `|rho_fwd| > 0.5` → forward replay PASS
- `|rho_rev| > 0.5` → reverse replay PASS
- Both NaN → replay failed entirely (no spikes in SWR window)

### `/stc/` — Synaptic Tagging and Capture (only with `--ec-lii` flag)

| Dataset | Shape | Dtype | Description |
|---------|-------|-------|-------------|
| `event` | `[n_swr_events]` | int32 | SWR event index |
| `t_swr_start` | `[n_swr_events]` | float32 | SWR event start time (ms) |
| `t_swr_end` | `[n_swr_events]` | float32 | SWR event end time (ms) |
| `n_active_syn` | `[n_swr_events]` | int32 | Number of synapses active during this SWR |
| `n_ltp_new` | `[n_swr_events]` | int32 | New L-LTP synapses created this event |
| `n_ltp_total` | `[n_swr_events]` | int32 | Cumulative L-LTP synapses after this event |
| `w_mean` | `[n_swr_events]` | float32 | Mean weight across all synapses after event |
| `w_ltp_mean` | `[n_swr_events]` | float32 | Mean weight of L-LTP synapses only |
| `w_final` | `[n_synapses]` | float32 | **Final weight distribution** — every synapse's weight after all SWR events |
| `ltp_mask` | `[n_synapses]` | uint8 | **Boolean mask** — 1 = synapse achieved L-LTP (permanent), 0 = did not survive |

| Attribute | Type | Description |
|-----------|------|-------------|
| `.attrs["n_swr_events"]` | int | Total number of SWR consolidation events |
| `.attrs["w_init"]` | float | Initial weight (from EC module) |

**STC interpretation:**
- `ltp_mask.sum() / len(ltp_mask)` = survival rate (fraction of synapses that became permanent)
- `w_final[ltp_mask == 1]` = weight distribution of survivors — this is the "consolidation threshold" the SNN learned
- The 25th percentile of surviving weights becomes our biologically-derived threshold

### `/times_ms` — Time Axis

| Dataset | Shape | Dtype | Description |
|---------|-------|-------|-------------|
| `times_ms` | `[n_bins]` | float64 | Bin-centre timestamps in ms, spaced by `dt_ms` |

---

## Detailed Implementation

### New File: `nous/brain/snn_densifier.py`

```python
"""F041 — SNN Sleep Densification using tinyHippo HDF5 replay data.

Reads pre-computed tinyHippo simulation results and uses SWR replay
co-activation patterns + STC consolidation gates to create graph edges
during Nous sleep cycles.

Data flow:
  1. Load .h5 from configured path
  2. Quality-gate on replay Spearman ρ (reject bad replays)
  3. Extract co-activation matrix from ca3_sup heatmap during SWR windows
  4. Extract STC survival threshold from w_final + ltp_mask
  5. Cluster recent Nous memories into n_groups via KMeans on embeddings
  6. Map SNN co-activation strengths → memory pair → graph edges
"""

from __future__ import annotations

import logging
from pathlib import Path
from dataclasses import dataclass
from uuid import UUID

import numpy as np
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from nous.brain.graph_linker import GraphLinker
from nous.config import Settings
from nous.storage.database import Database

logger = logging.getLogger(__name__)

# Minimum replay quality to use results (Spearman |ρ| threshold)
MIN_REPLAY_RHO = 0.5

# Maximum edges to create per sleep cycle
MAX_EDGES_PER_CYCLE = 500

# Relation type for SNN-derived edges
SNN_RELATION = "snn_coactivation"


@dataclass
class TinyHippoResults:
    """Parsed results from a tinyHippo HDF5 file."""
    n_groups: int
    sim_ms: float
    rho_fwd: float
    rho_rev: float
    coactivation_matrix: np.ndarray  # [n_groups, n_groups] correlation matrix
    stc_survival_rate: float | None  # fraction of synapses reaching L-LTP
    stc_weight_threshold: float | None  # 25th percentile of survivor weights
    has_stc: bool


def load_tinyhippo_h5(h5_path: str | Path) -> TinyHippoResults:
    """Load and parse tinyHippo HDF5 file.
    
    Extracts:
    - Co-activation matrix from ca3_sup heatmap during SWR windows
    - STC consolidation thresholds (if --ec-lii data present)
    - Replay quality metrics
    
    Raises:
        FileNotFoundError: if h5_path does not exist
        ValueError: if required datasets are missing
        ImportError: if h5py is not installed
    """
    try:
        import h5py
    except ImportError:
        raise ImportError(
            "h5py is required for F041 SNN densification. "
            "Install with: pip install h5py"
        )
    
    h5_path = Path(h5_path)
    if not h5_path.exists():
        raise FileNotFoundError(f"tinyHippo HDF5 not found: {h5_path}")
    
    with h5py.File(h5_path, "r") as h5:
        # --- Root attributes ---
        n_groups = int(h5.attrs["n_groups"])
        sim_ms = float(h5.attrs["sim_ms"])
        dt_ms = float(h5.attrs.get("dt_ms", 10.0))
        
        # SWR windows
        swr_fwd_start = float(h5.attrs["swr_fwd_start"])
        swr_fwd_stop = float(h5.attrs["swr_fwd_stop"])
        swr_rev_start = float(h5.attrs["swr_rev_start"])
        swr_rev_stop = float(h5.attrs["swr_rev_stop"])
        
        # --- Replay quality ---
        stats = h5["stats"]
        rho_fwd = float(stats.attrs.get("rho_fwd", float("nan")))
        rho_rev = float(stats.attrs.get("rho_rev", float("nan")))
        
        # --- CA3 SUP heatmap: [n_groups, n_bins] ---
        if "ca3_sup" not in h5 or "heatmap" not in h5["ca3_sup"]:
            raise ValueError("Missing ca3_sup/heatmap in HDF5")
        
        heatmap = np.array(h5["ca3_sup"]["heatmap"], dtype=np.float32)
        # heatmap shape: [n_groups, n_bins]
        
        # --- Extract SWR-window activity ---
        # Convert ms timestamps to bin indices
        fwd_start_bin = int(swr_fwd_start / dt_ms)
        fwd_stop_bin = int(swr_fwd_stop / dt_ms)
        rev_start_bin = int(swr_rev_start / dt_ms)
        rev_stop_bin = int(swr_rev_stop / dt_ms)
        
        # Clamp to heatmap bounds
        n_bins = heatmap.shape[1]
        fwd_start_bin = max(0, min(fwd_start_bin, n_bins - 1))
        fwd_stop_bin = max(0, min(fwd_stop_bin, n_bins))
        rev_start_bin = max(0, min(rev_start_bin, n_bins - 1))
        rev_stop_bin = max(0, min(rev_stop_bin, n_bins))
        
        # Concatenate forward + reverse SWR windows
        swr_activity = np.hstack([
            heatmap[:, fwd_start_bin:fwd_stop_bin],
            heatmap[:, rev_start_bin:rev_stop_bin],
        ])
        
        # --- Co-activation matrix ---
        # Pearson correlation between group activity during SWR replay
        # Groups that co-fire during SWR replay are associated
        if swr_activity.shape[1] < 2:
            logger.warning("SWR window too narrow for correlation, using full heatmap")
            swr_activity = heatmap
        
        # Handle constant rows (groups that never fired)
        row_std = swr_activity.std(axis=1)
        active_mask = row_std > 1e-10
        
        coactivation = np.zeros((n_groups, n_groups), dtype=np.float32)
        if active_mask.sum() >= 2:
            active_indices = np.where(active_mask)[0]
            active_corr = np.corrcoef(swr_activity[active_mask])
            # Map back to full matrix
            for i_idx, i_full in enumerate(active_indices):
                for j_idx, j_full in enumerate(active_indices):
                    coactivation[i_full, j_full] = active_corr[i_idx, j_idx]
        
        # Zero out diagonal (no self-edges)
        np.fill_diagonal(coactivation, 0.0)
        
        # Clamp negative correlations to 0 (anti-correlation = inhibition = no edge)
        coactivation = np.clip(coactivation, 0.0, 1.0)
        
        # --- STC consolidation data (optional) ---
        has_stc = "stc" in h5
        stc_survival_rate = None
        stc_weight_threshold = None
        
        if has_stc:
            stc = h5["stc"]
            w_final = np.array(stc["w_final"], dtype=np.float32)
            ltp_mask = np.array(stc["ltp_mask"], dtype=np.uint8)
            
            total_synapses = len(ltp_mask)
            survivors = ltp_mask.sum()
            stc_survival_rate = float(survivors / total_synapses) if total_synapses > 0 else 0.0
            
            # Threshold = 25th percentile of surviving weights
            surviving_weights = w_final[ltp_mask.astype(bool)]
            if len(surviving_weights) > 0:
                stc_weight_threshold = float(np.percentile(surviving_weights, 25))
            
            logger.info(
                "STC data: %d/%d synapses survived (%.1f%%), weight threshold: %.4f",
                survivors, total_synapses, stc_survival_rate * 100,
                stc_weight_threshold or 0.0,
            )
    
    return TinyHippoResults(
        n_groups=n_groups,
        sim_ms=sim_ms,
        rho_fwd=rho_fwd,
        rho_rev=rho_rev,
        coactivation_matrix=coactivation,
        stc_survival_rate=stc_survival_rate,
        stc_weight_threshold=stc_weight_threshold,
        has_stc=has_stc,
    )


@dataclass
class MemoryNode:
    """A Nous memory node for clustering."""
    id: UUID
    node_type: str  # "fact", "decision", "episode", "procedure"
    embedding: np.ndarray
    content: str


class SNNDensifier:
    """F041: SNN-based graph densification using tinyHippo replay data.
    
    During Nous sleep, clusters recent memories into groups matching
    tinyHippo's n_groups, then uses the SNN's co-activation matrix
    to determine which memory groups should be connected.
    
    The co-activation matrix encodes which neuron groups fired together
    during SWR replay — this is the SNN's "opinion" on which memories
    are associated, derived from actual neural computation including
    competitive inhibition and bidirectional replay dynamics.
    """
    
    def __init__(
        self,
        db: Database,
        graph_linker: GraphLinker,
        settings: Settings,
        agent_id: str,
    ) -> None:
        self.db = db
        self._linker = graph_linker
        self._settings = settings
        self._agent_id = agent_id
        self._interrupted = False
    
    def interrupt(self) -> None:
        self._interrupted = True
    
    async def run_snn_densification(
        self,
        h5_path: str | Path,
        lookback_hours: int = 48,
        max_memories: int = 500,
    ) -> dict:
        """Main entry point: run SNN-based densification.
        
        Steps:
        1. Load tinyHippo .h5 results
        2. Quality-gate on replay fidelity
        3. Fetch recent memories with embeddings
        4. Cluster memories into n_groups via KMeans
        5. Apply co-activation matrix to create edges
        6. Apply STC threshold as consolidation gate
        
        Returns:
            dict with keys: edges_created, groups_used, replay_quality,
            stc_survival_rate, memories_processed
        """
        stats = {
            "edges_created": 0,
            "groups_used": 0,
            "replay_quality": {"rho_fwd": None, "rho_rev": None},
            "stc_survival_rate": None,
            "memories_processed": 0,
            "skipped_reason": None,
        }
        
        # Step 1: Load .h5
        try:
            results = load_tinyhippo_h5(h5_path)
        except (FileNotFoundError, ValueError, ImportError) as e:
            logger.warning("F041: Cannot load tinyHippo H5: %s", e)
            stats["skipped_reason"] = str(e)
            return stats
        
        stats["replay_quality"] = {
            "rho_fwd": results.rho_fwd,
            "rho_rev": results.rho_rev,
        }
        stats["stc_survival_rate"] = results.stc_survival_rate
        
        # Step 2: Quality gate
        fwd_ok = not np.isnan(results.rho_fwd) and abs(results.rho_fwd) >= MIN_REPLAY_RHO
        rev_ok = not np.isnan(results.rho_rev) and abs(results.rho_rev) >= MIN_REPLAY_RHO
        
        if not (fwd_ok or rev_ok):
            logger.warning(
                "F041: Replay quality too low (rho_fwd=%.3f, rho_rev=%.3f), skipping",
                results.rho_fwd, results.rho_rev,
            )
            stats["skipped_reason"] = "replay_quality_below_threshold"
            return stats
        
        logger.info(
            "F041: Replay quality OK (rho_fwd=%.3f, rho_rev=%.3f)",
            results.rho_fwd, results.rho_rev,
        )
        
        # Step 3: Fetch recent memories
        async with self.db.session() as session:
            memories = await self._fetch_recent_memories(
                session, lookback_hours, max_memories
            )
        
        if len(memories) < results.n_groups:
            logger.warning(
                "F041: Only %d memories (need >= %d for %d groups), skipping",
                len(memories), results.n_groups, results.n_groups,
            )
            stats["skipped_reason"] = "insufficient_memories"
            stats["memories_processed"] = len(memories)
            return stats
        
        stats["memories_processed"] = len(memories)
        
        # Step 4: Cluster memories into n_groups
        embeddings = np.array([m.embedding for m in memories])
        labels = self._cluster_memories(embeddings, results.n_groups)
        stats["groups_used"] = results.n_groups
        
        # Step 5: Build group→memory mapping
        groups: dict[int, list[MemoryNode]] = {}
        for mem, label in zip(memories, labels):
            groups.setdefault(label, []).append(mem)
        
        # Step 6: Apply co-activation matrix to create edges
        coact = results.coactivation_matrix
        
        # Determine edge weight threshold
        # If STC data available: use biologically-derived threshold
        # Otherwise: use percentile of co-activation values
        if results.has_stc and results.stc_survival_rate is not None:
            # Map STC survival rate to co-activation threshold
            # If only 30% of synapses survived, we want to be selective
            # Use survival rate as a percentile selector on co-activation values
            nonzero_coact = coact[coact > 0]
            if len(nonzero_coact) > 0:
                # Higher survival = lower threshold (more edges)
                # Lower survival = higher threshold (more selective, like the SNN)
                percentile = (1.0 - results.stc_survival_rate) * 100
                percentile = max(50, min(95, percentile))  # clamp to [50, 95]
                edge_threshold = float(np.percentile(nonzero_coact, percentile))
            else:
                edge_threshold = 0.3
            logger.info(
                "F041: STC-derived edge threshold: %.3f (survival=%.1f%%, percentile=%.0f)",
                edge_threshold, results.stc_survival_rate * 100, percentile,
            )
        else:
            # No STC: use top 25% of co-activation values
            nonzero_coact = coact[coact > 0]
            if len(nonzero_coact) > 0:
                edge_threshold = float(np.percentile(nonzero_coact, 75))
            else:
                edge_threshold = 0.3
            logger.info("F041: Heuristic edge threshold: %.3f (no STC data)", edge_threshold)
        
        # Step 7: Create edges
        edges_created = 0
        async with self.db.session() as session:
            for i in range(results.n_groups):
                if self._interrupted:
                    break
                for j in range(i + 1, results.n_groups):
                    if self._interrupted:
                        break
                    
                    strength = float(coact[i, j])
                    if strength < edge_threshold:
                        continue
                    
                    if i not in groups or j not in groups:
                        continue
                    
                    # Create edges between representative pairs
                    # Don't create N*M edges — pick top pairs by embedding similarity
                    new_edges = await self._create_group_edges(
                        session,
                        groups[i],
                        groups[j],
                        strength,
                        max_pairs=3,
                    )
                    edges_created += new_edges
                    
                    if edges_created >= MAX_EDGES_PER_CYCLE:
                        logger.info("F041: Hit edge cap (%d), stopping", MAX_EDGES_PER_CYCLE)
                        break
                
                if edges_created >= MAX_EDGES_PER_CYCLE:
                    break
            
            await session.commit()
        
        stats["edges_created"] = edges_created
        logger.info(
            "F041: SNN densification complete — %d edges from %d memories in %d groups",
            edges_created, len(memories), results.n_groups,
        )
        return stats
    
    async def _fetch_recent_memories(
        self,
        session: AsyncSession,
        lookback_hours: int,
        max_memories: int,
    ) -> list[MemoryNode]:
        """Fetch recent facts, decisions, episodes with embeddings."""
        memories: list[MemoryNode] = []
        
        # Fetch facts
        sql = text("""
            SELECT id, content, embedding::text
            FROM heart.facts
            WHERE agent_id = :agent_id
              AND active = true
              AND embedding IS NOT NULL
              AND created_at >= NOW() - INTERVAL ':hours hours'
            ORDER BY created_at DESC
            LIMIT :limit
        """)
        result = await session.execute(sql, {
            "agent_id": self._agent_id,
            "hours": lookback_hours,
            "limit": max_memories // 3,
        })
        for row in result.fetchall():
            emb = np.fromstring(row[2].strip("[]"), sep=",", dtype=np.float32)
            if len(emb) > 0:
                memories.append(MemoryNode(
                    id=row[0], node_type="fact",
                    embedding=emb, content=row[1][:200],
                ))
        
        # Fetch decisions
        sql = text("""
            SELECT id, description, embedding::text
            FROM brain.decisions
            WHERE agent_id = :agent_id
              AND embedding IS NOT NULL
              AND created_at >= NOW() - INTERVAL ':hours hours'
            ORDER BY created_at DESC
            LIMIT :limit
        """)
        result = await session.execute(sql, {
            "agent_id": self._agent_id,
            "hours": lookback_hours,
            "limit": max_memories // 3,
        })
        for row in result.fetchall():
            emb = np.fromstring(row[2].strip("[]"), sep=",", dtype=np.float32)
            if len(emb) > 0:
                memories.append(MemoryNode(
                    id=row[0], node_type="decision",
                    embedding=emb, content=row[1][:200],
                ))
        
        # Fetch episodes
        sql = text("""
            SELECT id,
                   COALESCE(structured_summary->>'summary', ''),
                   embedding::text
            FROM heart.episodes
            WHERE agent_id = :agent_id
              AND active = true
              AND embedding IS NOT NULL
              AND structured_summary IS NOT NULL
              AND created_at >= NOW() - INTERVAL ':hours hours'
            ORDER BY created_at DESC
            LIMIT :limit
        """)
        result = await session.execute(sql, {
            "agent_id": self._agent_id,
            "hours": lookback_hours,
            "limit": max_memories // 3,
        })
        for row in result.fetchall():
            emb = np.fromstring(row[2].strip("[]"), sep=",", dtype=np.float32)
            if len(emb) > 0:
                memories.append(MemoryNode(
                    id=row[0], node_type="episode",
                    embedding=emb, content=row[1][:200],
                ))
        
        logger.info("F041: Fetched %d memories (%d-hour lookback)", len(memories), lookback_hours)
        return memories
    
    @staticmethod
    def _cluster_memories(embeddings: np.ndarray, n_clusters: int) -> np.ndarray:
        """Cluster memory embeddings into n_clusters groups via KMeans.
        
        Falls back to simple modular assignment if sklearn unavailable.
        """
        try:
            from sklearn.cluster import KMeans
            km = KMeans(n_clusters=n_clusters, n_init=10, random_state=42)
            return km.fit_predict(embeddings)
        except ImportError:
            logger.warning("F041: sklearn not available, using modular assignment")
            return np.arange(len(embeddings)) % n_clusters
    
    async def _create_group_edges(
        self,
        session: AsyncSession,
        group_a: list[MemoryNode],
        group_b: list[MemoryNode],
        snn_strength: float,
        max_pairs: int = 3,
    ) -> int:
        """Create edges between memories in two co-activated groups.
        
        Selects the top-N most similar pairs across groups to avoid
        creating O(N*M) edges. The edge weight is the SNN co-activation
        strength (not cosine similarity — that's what F040 does).
        
        Args:
            group_a: memories in cluster i
            group_b: memories in cluster j  
            snn_strength: co-activation correlation from tinyHippo heatmap
            max_pairs: maximum edges to create between these two groups
            
        Returns:
            Number of edges created
        """
        # Compute pairwise cosine similarity to rank pairs
        # But the WEIGHT is the SNN strength (this is the key difference from F040)
        pairs: list[tuple[float, MemoryNode, MemoryNode]] = []
        
        for ma in group_a:
            for mb in group_b:
                # Cosine similarity for ranking (which pairs within groups are best)
                sim = float(np.dot(ma.embedding, mb.embedding) / (
                    np.linalg.norm(ma.embedding) * np.linalg.norm(mb.embedding) + 1e-10
                ))
                pairs.append((sim, ma, mb))
        
        # Sort by embedding similarity, take top pairs
        pairs.sort(key=lambda x: x[0], reverse=True)
        
        edges_created = 0
        for sim, ma, mb in pairs[:max_pairs]:
            if self._interrupted:
                break
            
            # Determine relation type based on memory types
            relation = _get_snn_relation(ma.node_type, mb.node_type)
            
            edge = await self._linker.create_edge(
                source_id=ma.id,
                target_id=mb.id,
                source_type=ma.node_type,
                target_type=mb.node_type,
                relation=relation,
                weight=snn_strength,
                session=session,
            )
            if edge is not None:
                edges_created += 1
        
        return edges_created


def _get_snn_relation(type_a: str, type_b: str) -> str:
    """Map memory type pair to relation type for SNN-derived edges."""
    pair = tuple(sorted([type_a, type_b]))
    return {
        ("fact", "fact"): "snn_coactivation",
        ("decision", "fact"): "snn_coactivation",
        ("episode", "fact"): "snn_coactivation",
        ("decision", "decision"): "snn_coactivation",
        ("decision", "episode"): "snn_coactivation",
        ("episode", "episode"): "snn_coactivation",
        ("fact", "procedure"): "snn_coactivation",
        ("decision", "procedure"): "snn_coactivation",
        ("episode", "procedure"): "snn_coactivation",
        ("procedure", "procedure"): "snn_coactivation",
    }.get(pair, "snn_coactivation")
```

### Sleep Handler Integration

Add to `handlers/sleep_handler.py`:

```python
# In _run_sleep(), after Phase 8 (F040 graph densification):

    success = await self._phase_snn_densification(sleep_stats)
    if success:
        phases_completed.append("snn_densification")

# New method:
async def _phase_snn_densification(self, sleep_stats: dict) -> bool:
    """F041 Phase: SNN-based graph densification using tinyHippo replay."""
    h5_path = self._settings.tinyhippo_h5_path
    if not h5_path or not self._snn_densifier:
        return True
    try:
        self._snn_densifier._interrupted = self._interrupted
        result = await self._snn_densifier.run_snn_densification(
            h5_path=h5_path,
            lookback_hours=self._settings.snn_lookback_hours,
            max_memories=self._settings.snn_max_memories,
        )
        sleep_stats["snn_edges_created"] = result["edges_created"]
        sleep_stats["snn_replay_quality"] = result["replay_quality"]
        sleep_stats["snn_stc_survival"] = result.get("stc_survival_rate")
        sleep_stats["snn_skipped"] = result.get("skipped_reason")
        
        logger.info(
            "F041 SNN densification: %d edges (rho_fwd=%.3f, rho_rev=%.3f)",
            result["edges_created"],
            result["replay_quality"].get("rho_fwd", 0),
            result["replay_quality"].get("rho_rev", 0),
        )
        return True
    except Exception:
        logger.warning("F041 SNN densification failed", exc_info=True)
        return False
```

### Configuration

```python
# Add to nous/config.py Settings class

# F041: SNN Sleep Densification
tinyhippo_h5_path: str | None = None  # Path to tinyHippo .h5 file (None = disabled)
snn_densification_enabled: bool = True
snn_lookback_hours: int = 48  # How far back to look for memories to cluster
snn_max_memories: int = 500  # Max memories per cycle
snn_max_edges_per_cycle: int = 500  # Edge creation cap
snn_min_replay_rho: float = 0.5  # Minimum |Spearman ρ| to accept replay results
```

### Relation Type Registration

Add `snn_coactivation` to the graph linker's weight multiplier map:

```python
# In brain/graph_linker.py RELATION_WEIGHT_MULTIPLIERS:
RELATION_WEIGHT_MULTIPLIERS["snn_coactivation"] = 0.85
# Slightly below evidence_for (1.0) but above discussed_in (0.7)
# because SNN co-activation is a genuine association signal
# but not as semantically specific as evidence_for
```

### Dependencies

```
pip install h5py          # HDF5 file reading
pip install scikit-learn  # KMeans clustering (optional — fallback exists)
numpy                     # Already a dependency
```

---

## What The Existing .h5 Actually Gives Us (Honest Assessment)

### What works directly

1. **Co-activation matrix from heatmap** — this is topology-independent. It measures what the SNN *actually did* during SWR replay, not just what was wired. The inhibitory interneurons (`ca3_int_sup`, `ca3_int_deep`) create competitive dynamics that suppress some group pairs and strengthen others. Adjacent groups in the chain will correlate strongly, but the correlation *magnitudes* are shaped by neural dynamics, not just wiring.

2. **STC survival threshold** — the `w_final`/`ltp_mask` data encodes which synapses won the competitive consolidation. The 25th percentile of surviving weights is a biologically-derived "good enough to keep" threshold that replaces F040's hand-tuned cosine thresholds.

3. **Replay quality gate** — `rho_fwd`/`rho_rev` Spearman ρ gives a principled accept/reject criterion. If the SNN's own replay was disordered, we shouldn't trust its co-activation patterns.

### What's limited

1. **The topology is fixed** — groups 0→1→2→...→N are wired in a sequential chain by `sequence_connect_ca3_layered()`. The SNN didn't process Nous's actual memories. Adjacent groups will always have higher co-activation than distant ones. The co-activation matrix has an inherent spatial gradient.

2. **Group assignment is external** — KMeans clustering on Nous embeddings maps memories to groups, but the mapping is arbitrary relative to the SNN's topology. Group 0's memories have no intrinsic relationship to group 0's neurons.

3. **No feedback loop** — the SNN doesn't learn from Nous's data. It ran once on a generic configuration. The same .h5 file produces the same co-activation matrix every sleep cycle.

### Why it's still scientifically valid

The experiment proves: **Does an SNN-derived association function produce better graph edges than cosine similarity alone?**

The co-activation matrix is a learned transfer function: "given N groups of items processed through hippocampal replay dynamics with competitive inhibition and bidirectional replay, what association strengths emerge?"

This function is NOT the same as cosine similarity because:
- Bidirectional replay creates asymmetric strengthening patterns
- Inhibitory interneurons suppress some associations that would pass a cosine threshold
- STC competitive consolidation eliminates weak associations

The experiment controls for the topology limitation by varying the KMeans→group mapping across cycles and measuring whether SNN edges consistently outperform cosine edges.

---

## A/B Test Design

### Experimental Setup

Run alternating sleep cycles:

- **Control (A):** F040-only densification — cosine similarity thresholds
- **Treatment (B):** F040 + F041 — cosine similarity PLUS SNN co-activation edges

### Metrics

| Metric | How to Measure | Expected Outcome |
|--------|---------------|-----------------|
| Retrieval precision@5 | For 20 test queries, compare top-5 recall results quality | B ≥ A (SNN edges surface non-obvious associations) |
| Graph connectivity | Average degree, orphan rate, component count | B > A (more edges, better connected) |
| Multi-hop success | Queries requiring 2+ hop traversal to find answer | B >> A (SNN bridges cosine-invisible gaps) |
| Edge novelty | % of SNN edges that overlap with F040 cosine edges | Low overlap = SNN finds genuinely different associations |
| Subjective quality | Tim rates recalled memories as relevant/irrelevant | B ≥ A |

### Falsification Criterion

If SNN edges are >80% redundant with cosine edges (same pairs, similar weights), the SNN adds no value over F040. This would falsify the thesis.

### Positive Signal

If SNN edges connect memory pairs with cosine similarity < 0.70 (below F040's threshold) that Tim judges as genuinely relevant — the SNN is discovering associations that pure embedding similarity cannot.

---

## Future Work (Phase 2+)

### Phase 2: Memory-Specific tinyHippo Runs

Instead of using a pre-computed .h5, modify tinyHippo's `sequence_connect_ca3_layered()` to wire groups based on Nous memory embedding similarities instead of a fixed sequential chain.

**Requires:** Max Talanov collaboration to modify tinyHippo's initialization to accept a custom connectivity matrix.

### Phase 3: Bidirectional Feedback

Nous sleep results (which memories were useful next day) feed back to tinyHippo's STC parameters, tuning the consolidation aggressiveness. The SNN learns which consolidation patterns produce useful memories.

### Phase 4: Live Lightweight SNN

Replace tinyHippo (full NEST simulation) with a lightweight SNN (e.g., Nengo or snnTorch) that runs in-process during sleep. Same architecture (CA3 recurrent + CA1 readout + STC) but fast enough for per-cycle execution.

---

## Implementation Plan

| Phase | Work | LOC | Dependencies |
|-------|------|-----|-------------|
| 1a | `snn_densifier.py` — H5 parser + co-activation extraction | ~150 | h5py |
| 1b | `snn_densifier.py` — SNNDensifier class + KMeans clustering | ~200 | scikit-learn (optional) |
| 1c | Sleep handler integration + config | ~50 | None |
| 1d | `snn_coactivation` relation type + weight multiplier | ~5 | None |
| 1e | Tests | ~150 | pytest |
| **Total Phase 1** | | **~555** | |

### Estimated Timeline

- Phase 1 implementation: 1-2 days
- A/B testing: 1 week of sleep cycles
- Phase 2 scoping (with Max): separate spec

---

## Risks & Mitigations

### Risk 1: KMeans Cluster Quality

**Problem:** KMeans on high-dimensional embeddings (1536-d) may produce poor clusters.

**Mitigation:** Add cluster quality metric (silhouette score). If score < 0.1, skip SNN densification for this cycle. Consider PCA dimensionality reduction before clustering.

### Risk 2: Same .h5 Every Cycle

**Problem:** Reusing the same .h5 means the same co-activation matrix every sleep cycle. After the first cycle creates all viable edges, subsequent cycles waste computation.

**Mitigation:** Track which .h5 was last used. Skip if same file + same memories. Different KMeans runs on different memory subsets do produce different group assignments, so there's some variability. True fix is Phase 2 (custom tinyHippo runs).

### Risk 3: h5py Dependency in Production

**Problem:** h5py requires HDF5 C library, adding build complexity.

**Mitigation:** h5py is pip-installable with binary wheels on Linux. No compilation needed. If truly problematic, pre-convert .h5 to JSON/numpy offline.

### Risk 4: SNN Edges Conflict with F040 Edges

**Problem:** Same memory pair might get both a cosine edge (F040) and an SNN edge (F041) with different weights.

**Mitigation:** `create_edge()` uses `ON CONFLICT DO NOTHING` on `(source_id, target_id, relation)`. Since SNN edges use `snn_coactivation` relation (distinct from `related_to`), both can coexist. This is actually desirable — it lets us compare which edges are traversed more in practice.

---

## Non-Goals

- **Running tinyHippo in Nous's process** — tinyHippo requires NEST simulator (HPC). We read its output, not run it.
- **Real-time SNN inference** — all SNN computation is offline (pre-computed .h5)
- **Replacing F040** — SNN densification augments F040, doesn't replace it
- **Custom tinyHippo runs per sleep cycle** — that's Phase 2
- **Membrain integration** — Membrain's Nengo-based SNN is a separate future path; F041 uses tinyHippo directly
