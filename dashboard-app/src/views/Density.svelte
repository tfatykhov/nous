<script lang="ts">
  import { apiGet } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import type { DensityData } from '../lib/types/api';
  import StatGrid from '../lib/ui/StatGrid.svelte';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';

  const store = usePoll(
    makePollStore<DensityData>(
      (signal) => apiGet<DensityData>('/dashboard/density', { signal }),
      Number.MAX_SAFE_INTEGER,
    ),
  );

  function orphanRateClass(rate: number): string {
    if (rate > 0.40) return 'badge-bad';
    if (rate > 0.15) return 'badge-warn';
    return 'badge-good';
  }

  function orphanRateLabel(rate: number): string {
    const pct = (rate * 100).toFixed(1) + '%';
    if (rate > 0.40) return pct + ' (high)';
    if (rate > 0.15) return pct + ' (moderate)';
    return pct + ' (healthy)';
  }
</script>

<!-- ── Header ──────────────────────────────────────────────────────────── -->
<header class="view-head">
  <div>
    <h1>Graph Density</h1>
    <p class="subtitle">Knowledge graph connectivity health and backfill progress</p>
  </div>
  <div class="head-right">
    <button class="refresh-btn" onclick={() => void store.refresh()} disabled={$store.loading}>
      {$store.loading ? 'Loading…' : 'Refresh'}
    </button>
    <StaleBadge state={$store} />
  </div>
</header>

{#if $store.data}
  {@const d = $store.data}

  <!-- ── Stat cards ──────────────────────────────────────────────────────── -->
  <StatGrid stats={[
    { label: 'Total Nodes',    value: d.total_nodes.toLocaleString() },
    { label: 'Total Edges',    value: d.total_edges.toLocaleString() },
    { label: 'Avg Degree',     value: d.avg_degree.toFixed(2) },
    { label: 'Connected Nodes', value: d.connected_nodes.toLocaleString() },
  ]} />

  <!-- Orphan rate — single-card banner with colour badge -->
  <div class="orphan-banner">
    <span class="orphan-label">Overall Orphan Rate</span>
    <span class="badge {orphanRateClass(d.orphan_rate)}">{orphanRateLabel(d.orphan_rate)}</span>
    <span class="orphan-meta">({d.total_orphans.toLocaleString()} of {d.total_nodes.toLocaleString()} nodes unconnected)</span>
  </div>

  <!-- ── Two-column tables ───────────────────────────────────────────────── -->
  <div class="chart-grid mt">

    <!-- Orphan rate by type -->
    <section class="chart-card">
      <h2>Orphan Rate by Type</h2>
      <table class="data-table">
        <thead>
          <tr><th>Type</th><th>Total</th><th>Orphans</th><th>Rate</th></tr>
        </thead>
        <tbody>
          {#each Object.entries(d.density_by_type) as [type, stats]}
            <tr>
              <td class="type-cell">{type}</td>
              <td>{stats.total.toLocaleString()}</td>
              <td>{stats.orphan.toLocaleString()}</td>
              <td><span class="badge {orphanRateClass(stats.orphan_rate)}">{(stats.orphan_rate * 100).toFixed(1)}%</span></td>
            </tr>
          {:else}
            <tr><td colspan="4" class="empty-cell">No data</td></tr>
          {/each}
        </tbody>
      </table>
    </section>

    <!-- Edge distribution -->
    <section class="chart-card">
      <h2>Edge Distribution</h2>
      <table class="data-table">
        <thead>
          <tr><th>Relation</th><th>Count</th></tr>
        </thead>
        <tbody>
          {#each Object.entries(d.edge_distribution) as [relation, count]}
            <tr>
              <td>{relation}</td>
              <td>{count.toLocaleString()}</td>
            </tr>
          {:else}
            <tr><td colspan="2" class="empty-cell">No edges</td></tr>
          {/each}
        </tbody>
      </table>
    </section>

  </div>

  <!-- ── Backfill progress ───────────────────────────────────────────────── -->
  <section class="chart-card mt">
    <h2>Backfill Progress (Last 7 Days)</h2>
    <table class="data-table">
      <thead>
        <tr><th>Date</th><th>Auto-linked Edges</th></tr>
      </thead>
      <tbody>
        {#each d.backfill_progress as row}
          <tr>
            <td>{row.date}</td>
            <td>{row.edges.toLocaleString()}</td>
          </tr>
        {:else}
          <tr><td colspan="2" class="empty-cell">No backfill activity</td></tr>
        {/each}
      </tbody>
    </table>
  </section>

  <!-- ── Summary footer ─────────────────────────────────────────────────── -->
  <section class="chart-card mt">
    <div class="summary-row">
      <div><span class="summary-label">Connected nodes:</span> {d.connected_nodes.toLocaleString()}</div>
      <div><span class="summary-label">Total orphans:</span> {d.total_orphans.toLocaleString()}</div>
    </div>
  </section>

{:else if $store.error}
  <p class="status-msg error">
    Failed to load density data —
    <button class="retry-link" onclick={() => void store.refresh()}>retry</button>
  </p>
{:else}
  <p class="status-msg">Loading…</p>
{/if}

<style>
  /* ── Header ──────────────────────────────────────────────── */
  .view-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 1.25rem;
  }

  h1 {
    font-size: 1.4rem;
    font-weight: 700;
    color: var(--text);
    margin: 0 0 0.125rem;
  }

  .subtitle {
    font-size: 0.8125rem;
    color: var(--muted);
    margin: 0;
  }

  h2 {
    font-size: 0.875rem;
    font-weight: 600;
    color: var(--text);
    margin: 0 0 0.75rem;
  }

  .head-right {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-shrink: 0;
  }

  .refresh-btn {
    font-size: 0.8125rem;
    padding: 0.25rem 0.75rem;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm, 6px);
    background: var(--surface);
    color: var(--text);
    cursor: pointer;
  }
  .refresh-btn:hover:not(:disabled) { background: var(--surface-hover); }
  .refresh-btn:disabled { opacity: 0.5; cursor: default; }

  /* ── Orphan banner ───────────────────────────────────────── */
  .orphan-banner {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.625rem 1rem;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    margin-bottom: 1rem;
    font-size: 0.8125rem;
    flex-wrap: wrap;
  }

  .orphan-label {
    font-weight: 600;
    color: var(--text);
  }

  .orphan-meta {
    color: var(--muted);
  }

  /* ── Badge ───────────────────────────────────────────────── */
  .badge {
    display: inline-block;
    padding: 0.15em 0.55em;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 600;
    white-space: nowrap;
  }

  .badge-good { background: rgba(52, 211, 153, 0.15); color: #34d399; }
  .badge-warn { background: rgba(251, 191, 36,  0.15); color: #fbbf24; }
  .badge-bad  { background: rgba(248, 113, 113, 0.15); color: #f87171; }

  /* ── Chart grid ──────────────────────────────────────────── */
  .chart-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 1rem;
  }

  @media (max-width: 900px) {
    .chart-grid { grid-template-columns: 1fr; }
  }

  /* ── Chart card ──────────────────────────────────────────── */
  .chart-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1rem;
  }

  .mt { margin-top: 1rem; }

  /* ── Data table ──────────────────────────────────────────── */
  .data-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.8125rem;
  }

  .data-table th {
    text-align: left;
    padding: 0.375rem 0.5rem;
    font-size: 0.75rem;
    font-weight: 600;
    color: var(--muted);
    border-bottom: 1px solid var(--border);
  }

  .data-table td {
    padding: 0.375rem 0.5rem;
    color: var(--text);
    border-bottom: 1px solid var(--border);
  }

  .data-table tbody tr:last-child td { border-bottom: none; }

  .data-table tbody tr:hover td { background: var(--surface-hover); }

  .type-cell {
    font-weight: 500;
    text-transform: capitalize;
  }

  .empty-cell {
    text-align: center;
    color: var(--muted);
    font-style: italic;
    padding: 1rem 0.5rem;
  }

  /* ── Summary footer ──────────────────────────────────────── */
  .summary-row {
    display: flex;
    gap: 2rem;
    flex-wrap: wrap;
    font-size: 0.8125rem;
    color: var(--text);
  }

  .summary-label {
    color: var(--muted);
  }

  /* ── Status messages ─────────────────────────────────────── */
  .status-msg {
    color: var(--muted);
    font-size: 0.9375rem;
    padding: 2rem 1rem;
    text-align: center;
    margin: 0;
  }

  .status-msg.error { color: var(--red, #ef4444); }

  .retry-link {
    background: none;
    border: none;
    color: var(--accent);
    cursor: pointer;
    font-size: inherit;
    padding: 0;
    text-decoration: underline;
  }
</style>
