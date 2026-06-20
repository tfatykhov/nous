<script lang="ts">
  import { apiGet } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import type { HealthData } from '../lib/types/api';
  import StatGrid from '../lib/ui/StatGrid.svelte';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';
  import Chart from '../lib/viz/Chart.svelte';

  // Load-once: interval so large it never auto-fires. Refresh button handles reload.
  const store = usePoll(
    makePollStore<HealthData>(
      (signal) => apiGet<HealthData>('/dashboard/health?days=30', { signal }),
      0, // fetch-once (manual refresh only)
    ),
  );

  // ── Chart data builders ────────────────────────────────────────────────

  function shortDate(iso: string): string {
    // Slice MM-DD from YYYY-MM-DD
    return iso.length >= 10 ? iso.slice(5) : iso;
  }

  function densityChartData(d: HealthData) {
    return {
      labels: d.density_history.map((p) => shortDate(p.date)),
      datasets: [
        {
          label: 'Density',
          data: d.density_history.map((p) => p.density),
          borderColor: '#7c6af7',
          backgroundColor: 'rgba(124, 106, 247, 0.1)',
          fill: true,
          pointBackgroundColor: d.density_history.map((p) =>
            p.density >= 3.0 ? '#34d399' : p.density >= 1.0 ? '#fbbf24' : '#f87171',
          ),
          pointRadius: 4,
        },
      ],
    };
  }

  const densityOptions = {
    scales: {
      x: {
        ticks: { maxTicksLimit: 10 },
        grid: { display: false },
      },
      y: {
        beginAtZero: true,
        title: { display: true, text: 'Avg Edges per Node' },
      },
    },
    plugins: {
      legend: { display: false },
      annotation: {
        annotations: {
          targetLine: {
            type: 'line',
            yMin: 3.0,
            yMax: 3.0,
            borderColor: 'rgba(52, 211, 153, 0.5)',
            borderWidth: 1,
            borderDash: [5, 5],
            label: {
              content: 'Target: 3.0',
              enabled: true,
              position: 'end',
              color: 'rgba(52, 211, 153, 0.7)',
              font: { size: 10 },
            },
          },
        },
      },
    },
  };

  function edgeRateChartData(d: HealthData) {
    return {
      labels: d.daily_edges.map((p) => shortDate(p.date)),
      datasets: [
        {
          label: 'Edges Created',
          data: d.daily_edges.map((p) => p.count),
          backgroundColor: 'rgba(124, 106, 247, 0.5)',
          borderColor: '#7c6af7',
          borderWidth: 1,
        },
      ],
    };
  }

  const edgeRateOptions = {
    scales: {
      x: {
        ticks: { maxTicksLimit: 15 },
        grid: { display: false },
      },
      y: { beginAtZero: true, ticks: { precision: 0 } },
    },
    plugins: { legend: { display: false } },
  };

  function degreeChartData(d: HealthData) {
    return {
      labels: d.degree_distribution.map((p) => p.degree),
      datasets: [
        {
          label: 'Node Count',
          data: d.degree_distribution.map((p) => p.count),
          backgroundColor: 'rgba(96, 165, 250, 0.5)',
          borderColor: '#60a5fa',
          borderWidth: 1,
        },
      ],
    };
  }

  const degreeOptions = {
    scales: {
      x: {
        title: { display: true, text: 'Edge Count (degree)' },
        grid: { display: false },
      },
      y: {
        beginAtZero: true,
        ticks: { precision: 0 },
        title: { display: true, text: 'Number of Nodes' },
      },
    },
    plugins: { legend: { display: false } },
  };

  function orphanChartData(d: HealthData) {
    return {
      labels: d.orphan_trend.map((p) => shortDate(p.date)),
      datasets: [
        {
          label: 'Orphan Nodes',
          data: d.orphan_trend.map((p) => p.count),
          borderColor: '#f87171',
          backgroundColor: 'rgba(248, 113, 113, 0.1)',
          fill: true,
          pointBackgroundColor: '#f87171',
        },
      ],
    };
  }

  const orphanOptions = {
    scales: {
      x: {
        ticks: { maxTicksLimit: 10 },
        grid: { display: false },
      },
      y: {
        beginAtZero: true,
        ticks: { precision: 0 },
        title: { display: true, text: 'Orphan Count' },
      },
    },
    plugins: { legend: { display: false } },
  };

  function autoManualChartData(d: HealthData) {
    return {
      labels: d.daily_edges.map((p) => shortDate(p.date)),
      datasets: [
        {
          label: 'Auto-linked',
          data: d.daily_edges.map((p) => p.auto),
          backgroundColor: 'rgba(52, 211, 153, 0.5)',
          borderColor: '#34d399',
          borderWidth: 1,
        },
        {
          label: 'Manual',
          data: d.daily_edges.map((p) => p.manual),
          backgroundColor: 'rgba(124, 106, 247, 0.5)',
          borderColor: '#7c6af7',
          borderWidth: 1,
        },
      ],
    };
  }

  const autoManualOptions = {
    scales: {
      x: {
        stacked: true,
        ticks: { maxTicksLimit: 15 },
        grid: { display: false },
      },
      y: {
        stacked: true,
        beginAtZero: true,
        ticks: { precision: 0 },
      },
    },
    plugins: { legend: { position: 'bottom' } },
  };
</script>

<header class="view-head">
  <div>
    <h1>Graph Health</h1>
    <p class="subtitle">Density trends, edge creation, and node connectivity</p>
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

  {#if (d.total_edges ?? 0) === 0}
    <p class="empty-state">
      No graph data yet. Graph edges are created as Nous links facts, episodes, and decisions.
    </p>
  {:else}
    <!-- ── Stat cards ────────────────────────────────────────────────────── -->
    {@const latestDensity = d.density_history.length > 0
      ? d.density_history[d.density_history.length - 1].density
      : 0}
    {@const totalEdges30d = d.daily_edges.reduce((s, e) => s + e.count, 0)}
    {@const autoEdges = d.daily_edges.reduce((s, e) => s + e.auto, 0)}
    {@const autoPercent = totalEdges30d > 0
      ? Math.round((autoEdges / totalEdges30d) * 100)
      : 0}
    {@const latestOrphans = d.orphan_trend.length > 0
      ? d.orphan_trend[d.orphan_trend.length - 1].count
      : 0}

    <StatGrid stats={[
      { label: 'Current Density', value: latestDensity.toFixed(1) },
      { label: 'Edges (30d)',     value: totalEdges30d },
      { label: 'Auto-linked',     value: `${autoPercent}%` },
      { label: 'Orphan Nodes',   value: latestOrphans },
    ]} />

    <!-- ── Chart grid ────────────────────────────────────────────────────── -->
    <div class="chart-grid">

      <!-- Density over time -->
      {#if d.density_history.length > 0}
        <section class="chart-card">
          <h2>Graph Density Over Time</h2>
          <Chart type="line" data={densityChartData(d)} options={densityOptions} height="220px" />
        </section>
      {/if}

      <!-- Edge creation rate -->
      {#if d.daily_edges.length > 0}
        <section class="chart-card">
          <h2>Edge Creation Rate</h2>
          <Chart type="bar" data={edgeRateChartData(d)} options={edgeRateOptions} height="220px" />
        </section>
      {/if}

      <!-- Degree distribution -->
      {#if d.degree_distribution.length > 0}
        <section class="chart-card">
          <h2>Node Degree Distribution</h2>
          <Chart type="bar" data={degreeChartData(d)} options={degreeOptions} height="220px" />
        </section>
      {/if}

      <!-- Orphan nodes over time -->
      {#if d.orphan_trend.length > 0}
        <section class="chart-card">
          <h2>Orphan Nodes Over Time</h2>
          <Chart type="line" data={orphanChartData(d)} options={orphanOptions} height="220px" />
        </section>
      {/if}

      <!-- Auto vs Manual edges — full width -->
      {#if d.daily_edges.length > 0}
        <section class="chart-card full-width">
          <h2>Auto vs Manual Edges</h2>
          <Chart type="bar" data={autoManualChartData(d)} options={autoManualOptions} height="220px" />
        </section>
      {/if}

    </div>

    <!-- ── Orphan breakdown ──────────────────────────────────────────────── -->
    {#if d.total_orphans > 0}
      <section class="chart-card orphan-section">
        <h2>Orphan Breakdown by Type</h2>
        <ul class="orphan-list">
          {#each Object.entries(d.orphan_counts) as [type, count]}
            <li class="orphan-row">
              <span class="orphan-type">{type}</span>
              <span class="orphan-count">{count}</span>
            </li>
          {/each}
          <li class="orphan-row total-row">
            <span class="orphan-type">Total</span>
            <span class="orphan-count">{d.total_orphans}</span>
          </li>
        </ul>
      </section>
    {/if}
  {/if}

{:else if $store.error}
  <p class="status-msg error">
    Failed to load graph health data —
    <button class="retry-link" onclick={() => void store.refresh()}>retry</button>
  </p>
{:else}
  <p class="status-msg">Loading…</p>
{/if}

<style>
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
  .refresh-btn:hover:not(:disabled) {
    background: var(--surface-hover);
  }
  .refresh-btn:disabled {
    opacity: 0.5;
    cursor: default;
  }

  /* ── Chart grid ──────────────────────────────────────────────── */
  .chart-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 1rem;
    margin-top: 1rem;
  }

  .chart-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1rem;
  }

  .full-width {
    grid-column: 1 / -1;
  }

  @media (max-width: 900px) {
    .chart-grid {
      grid-template-columns: 1fr;
    }
    .full-width {
      grid-column: 1;
    }
  }

  /* ── Orphan breakdown ────────────────────────────────────────── */
  .orphan-section {
    margin-top: 1rem;
  }

  .orphan-list {
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .orphan-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.4rem 0;
    border-bottom: 1px solid var(--border);
    font-size: 0.8125rem;
  }
  .orphan-row:last-child {
    border-bottom: none;
  }

  .orphan-type {
    color: var(--text);
    text-transform: capitalize;
  }

  .orphan-count {
    font-weight: 600;
    color: var(--text);
  }

  .total-row .orphan-type,
  .total-row .orphan-count {
    font-weight: 700;
    color: var(--accent);
  }

  /* ── State messages ──────────────────────────────────────────── */
  .empty-state {
    color: var(--muted);
    font-size: 0.9375rem;
    padding: 3rem 2rem;
    text-align: center;
  }

  .status-msg {
    color: var(--muted);
    font-size: 0.9375rem;
    padding: 3rem 2rem;
    text-align: center;
  }

  .status-msg.error {
    color: var(--red, #ef4444);
  }

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
