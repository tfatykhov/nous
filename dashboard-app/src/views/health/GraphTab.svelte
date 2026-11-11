<script lang="ts">
  import { fmtDay } from '../../lib/harness';
  // F108 Memory health, Graph tab: Density's totals and overall orphan rate first, then Graph Health's charts
  // (/dashboard/health), then Density's tables (/dashboard/density). "Orphan Rate by Type" stays beside "Orphan
  // Breakdown by Type": the orphan counts agree, but only the former has each type's total and rate.
  import { apiGet } from '../../lib/api';
  import { makePollStore } from '../../lib/stores/registry';
  import { usePoll } from '../../lib/poll';
  import type { HealthData, DensityData } from '../../lib/types/api';
  import StatGrid from '../../lib/ui/StatGrid.svelte';
  import StaleBadge from '../../lib/ui/StaleBadge.svelte';
  import Chart from '../../lib/viz/Chart.svelte';
  import { Button } from '../../lib/ui/button';
  import { tint } from '../../lib/chartColors';

  // Load-once: interval so large it never auto-fires. Refresh button handles reload.
  const store = usePoll(
    makePollStore<HealthData>(
      (signal) => apiGet<HealthData>('/dashboard/health?days=30', { signal }),
      0, // fetch-once (manual refresh only)
    ),
  );

  const density = usePoll(
    makePollStore<DensityData>(
      (signal) => apiGet<DensityData>('/dashboard/density', { signal }),
      0, // fetch-once (manual refresh only)
    ),
  );

  function refreshAll() {
    void store.refresh();
    void density.refresh();
  }

  // ── From Density ───────────────────────────────────────────────────────

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

  /** Density keys node types in the singular; the orphan breakdown above it uses the plural. Show one name. */
  const TYPE_LABEL: Record<string, string> = {
    fact: 'facts', decision: 'decisions', episode: 'episodes', procedure: 'procedures', chunk: 'chunks',
  };

  // ── Chart data builders ────────────────────────────────────────────────

  function shortDate(iso: string): string {
    // The one day format, "Oct 20" (finish review fix 4)
    return fmtDay(iso);
  }

  function densityChartData(d: HealthData) {
    return {
      labels: d.density_history.map((p) => shortDate(p.date)),
      datasets: [
        {
          label: 'Density',
          data: d.density_history.map((p) => p.density),
          borderColor: 'var(--series-0)',
          backgroundColor: tint('--series-0', 0.1),
          fill: true,
          pointBackgroundColor: d.density_history.map((p) =>
            // Status: good, watch, bad.
            p.density >= 3.0 ? 'var(--green)' : p.density >= 1.0 ? 'var(--yellow)' : 'var(--red)',
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
            borderColor: tint('--green', 0.5),
            borderWidth: 1,
            borderDash: [5, 5],
            label: {
              content: 'Target: 3.0',
              enabled: true,
              position: 'end',
              color: tint('--green', 0.7),
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
          label: 'Edges created',
          data: d.daily_edges.map((p) => p.count),
          backgroundColor: tint('--series-0', 0.5),
          borderColor: 'var(--series-0)',
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
          label: 'Node count',
          data: d.degree_distribution.map((p) => p.count),
          backgroundColor: tint('--series-0', 0.5),
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
          label: 'Orphan nodes',
          data: d.orphan_trend.map((p) => p.count),
          // Orphans are a defect: the status red.
          borderColor: 'var(--red)',
          backgroundColor: tint('--red', 0.1),
          fill: true,
          pointBackgroundColor: 'var(--red)',
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
          backgroundColor: tint('--series-2', 0.5),
          borderColor: 'var(--series-2)',
          borderWidth: 1,
        },
        {
          label: 'Manual',
          data: d.daily_edges.map((p) => p.manual),
          backgroundColor: tint('--series-0', 0.5),
          borderColor: 'var(--series-0)',
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
  <p class="subtitle">How dense the graph is getting, how many edges are made, and how connected the nodes are.</p>
  <div class="head-right">
    <StaleBadge state={$store} />
    <Button variant="outline" size="lg" class="px-4 max-md:min-h-11" onclick={refreshAll} disabled={$store.loading || $density.loading}>
      {$store.loading || $density.loading ? 'Loading…' : 'Refresh'}
    </Button>
  </div>
</header>

<!-- ── From Density: totals and the overall orphan rate ────────────────── -->
{#if $density.data}
  {@const d = $density.data}

  <StatGrid stats={[
    { label: 'Total nodes',    value: d.total_nodes.toLocaleString() },
    { label: 'Total edges',    value: d.total_edges.toLocaleString() },
    { label: 'Avg degree',     value: d.avg_degree.toFixed(2) },
    { label: 'Connected nodes', value: d.connected_nodes.toLocaleString() },
  ]} />

  <!-- Orphan rate — single-card banner with colour badge; Density's staleness badge at its end -->
  <div class="orphan-banner">
    <span class="orphan-label">Overall orphan rate</span>
    <span class="badge {orphanRateClass(d.orphan_rate)}">{orphanRateLabel(d.orphan_rate)}</span>
    <span class="orphan-meta">({d.total_orphans.toLocaleString()} of {d.total_nodes.toLocaleString()} nodes unconnected)</span>
  </div>
{:else if $density.error}
  <p class="status-msg error">
    Failed to load density data —
    <button class="retry-link" onclick={() => void density.refresh()}>retry</button>
  </p>
{:else}
  <p class="status-msg">Loading…</p>
{/if}

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

    <!-- No "Orphan Nodes" card: the banner above is the direct orphan count. The card was the last point of the
         orphan trend, a running sum whose backend query counts its boundary day twice (pre-existing, parked as a
         follow-up), so it can differ from the banner. -->
    <StatGrid stats={[
      { label: 'Current density', value: latestDensity.toFixed(1) },
      { label: 'Edges (30d)',     value: totalEdges30d },
      { label: 'Auto-linked',     value: `${autoPercent}%` },
    ]} />

    <!-- ── Chart grid ────────────────────────────────────────────────────── -->
    <div class="chart-grid">

      <!-- Density over time -->
      {#if d.density_history.length > 0}
        <section class="chart-card">
          <h2>Graph density over time</h2>
          <Chart type="line" data={densityChartData(d)} options={densityOptions} height="220px" />
        </section>
      {/if}

      <!-- Edge creation rate -->
      {#if d.daily_edges.length > 0}
        <section class="chart-card">
          <h2>Edge creation rate</h2>
          <Chart type="bar" data={edgeRateChartData(d)} options={edgeRateOptions} height="220px" />
        </section>
      {/if}

      <!-- Degree distribution -->
      {#if d.degree_distribution.length > 0}
        <section class="chart-card">
          <h2>Node degree distribution</h2>
          <Chart type="bar" data={degreeChartData(d)} options={degreeOptions} height="220px" />
        </section>
      {/if}

      <!-- Orphan nodes over time -->
      {#if d.orphan_trend.length > 0}
        <section class="chart-card">
          <h2>Orphan nodes over time</h2>
          <p class="chart-note">Running total; may differ from the count above</p>
          <Chart type="line" data={orphanChartData(d)} options={orphanOptions} height="220px" />
        </section>
      {/if}

      <!-- Auto vs Manual edges — full width -->
      {#if d.daily_edges.length > 0}
        <section class="chart-card full-width">
          <h2>Auto vs manual edges</h2>
          <Chart type="bar" data={autoManualChartData(d)} options={autoManualOptions} height="220px" />
        </section>
      {/if}

    </div>

    <!-- ── Orphan breakdown ──────────────────────────────────────────────── -->
    {#if d.total_orphans > 0}
      <section class="chart-card orphan-section">
        <h2>Orphan breakdown by type</h2>
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

<!-- ── From Density: orphan rate by type, edge distribution, backfill ──── -->
{#if $density.data}
  {@const d = $density.data}

  <!-- ── Two-column tables ───────────────────────────────────────────────── -->
  <div class="chart-grid mt">

    <!-- Orphan rate by type -->
    <section class="chart-card">
      <h2>Orphan rate by type</h2>
      <table class="data-table">
        <thead>
          <tr><th>Type</th><th>Total</th><th>Orphans</th><th>Rate</th></tr>
        </thead>
        <tbody>
          {#each Object.entries(d.density_by_type) as [type, stats]}
            <tr>
              <td class="type-cell">{TYPE_LABEL[type] ?? type}</td>
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
      <h2>Edge distribution</h2>
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
    <h2>Backfill progress (last 7 days)</h2>
    <table class="data-table">
      <thead>
        <tr><th>Date</th><th>Auto-linked Edges</th></tr>
      </thead>
      <tbody>
        {#each d.backfill_progress as row}
          <tr>
            <td>{fmtDay(row.date)}</td>
            <td>{row.edges.toLocaleString()}</td>
          </tr>
        {:else}
          <tr><td colspan="2" class="empty-cell">No backfill activity</td></tr>
        {/each}
      </tbody>
    </table>
  </section>
{/if}

<style>
  .view-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 1.25rem;
  }

  .subtitle {
    font-size: 0.8125rem;
    color: var(--muted);
    margin: 0;
  }

  h2 {
    font-size: 15px;
    font-weight: 650;
    color: var(--text);
    margin: 0 0 0.75rem;
  }

  .head-right {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-shrink: 0;
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
    border: var(--card-border);
    border-radius: var(--card-radius);
    box-shadow: var(--card-shadow);
    padding: 1.125rem 1.25rem;
  }

  .full-width {
    grid-column: 1 / -1;
  }

  .chart-note {
    font-size: 0.75rem;
    color: var(--muted);
    margin: -0.5rem 0 0.75rem;
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
    color: var(--err-text);
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

  .mt { margin-top: 1rem; }

  /* ── Orphan banner (from Density) ────────────────────────── */
  .orphan-banner {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.625rem 1rem;
    background: var(--surface);
    border: var(--card-border);
    border-radius: var(--card-radius);
    box-shadow: var(--card-shadow);
    margin: 1rem 0;
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


  /* ── Badge (from Density) ────────────────────────────────── */
  .badge {
    display: inline-block;
    padding: 0.15em 0.55em;
    border-radius: 999px;
    font-size: 0.75rem;
    font-weight: 600;
    white-space: nowrap;
  }

  .badge-good { background: var(--badge-good-bg); color: var(--ok-text); }
  .badge-warn { background: var(--badge-warn-bg); color: var(--warn-text); }
  .badge-bad  { background: var(--badge-bad-bg); color: var(--err-text); }

  /* ── Data table (from Density) ───────────────────────────── */
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
</style>
