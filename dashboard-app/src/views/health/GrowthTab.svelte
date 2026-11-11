<script lang="ts">
  import { apiGet } from '../../lib/api';
  import { makePollStore } from '../../lib/stores/registry';
  import { usePoll } from '../../lib/poll';
  import type { StatusData, StatusTimeseriesPoint, ObservabilityData } from '../../lib/types/api';
  import StaleBadge from '../../lib/ui/StaleBadge.svelte';
  import Chart from '../../lib/viz/Chart.svelte';
  import { Button } from '../../lib/ui/button';
  import { seriesToken, tint } from '../../lib/chartColors';

  // F108 Memory health, Growth tab: Overview's memory growth and fact categories (/status?dashboard=true) and
  // Observability's fact growth rate (/dashboard/observability). Both are fetched once; Refresh reloads both.
  const store = usePoll(
    makePollStore<StatusData>(
      (signal) => apiGet<StatusData>('/status?dashboard=true', { signal }),
      0, // fetch-once (manual refresh only)
    ),
  );

  const obs = usePoll(
    makePollStore<ObservabilityData>(
      (signal) => apiGet<ObservabilityData>('/dashboard/observability', { signal }),
      0, // fetch-once (manual refresh only)
    ),
  );

  function refreshAll() {
    void store.refresh();
    void obs.refresh();
  }

  // ── From Overview ─────────────────────────────────────────────────────

  /** Convert [{date, count},...] arrays into Chart.js-ready {labels, datasets data}. */
  function normalizeTimeseries(raw: { facts: StatusTimeseriesPoint[]; episodes: StatusTimeseriesPoint[]; decisions: StatusTimeseriesPoint[] }) {
    const labels = raw.facts.map((p) => p.date.slice(5)); // MM-DD
    return {
      labels,
      facts: raw.facts.map((p) => p.count),
      episodes: raw.episodes.map((p) => p.count),
      decisions: raw.decisions.map((p) => p.count),
    };
  }

  /** Sort a Record<string, number> by value descending and return as {labels, values}. */
  function sortedDistribution(dist: Record<string, number>): { labels: string[]; values: number[] } {
    const entries = Object.entries(dist).sort((a, b) => b[1] - a[1]);
    return {
      labels: entries.map(([k]) => k.charAt(0).toUpperCase() + k.slice(1)),
      values: entries.map(([, v]) => v),
    };
  }

  /** The doughnut's segment colours: series 1 to 13, so a ninth category never repeats the first beside it. */
  const doughnutColors = (n: number) => Array.from({ length: n }, (_, i) => seriesToken(i));

  // ── From Observability ────────────────────────────────────────────────

  function trendLabels(points: { t: string }[]): string[] {
    return points.map((p) => {
      const d = new Date(p.t);
      return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours()}:00`;
    });
  }

  function factTrendData(points: { t: string; v: number }[]) {
    return {
      labels: trendLabels(points),
      datasets: [{
        label: 'Fact delta',
        data: points.map((p) => p.v),
        borderColor: 'var(--series-1)',
        backgroundColor: tint('--series-1', 0.1),
        fill: true,
        borderWidth: 2,
        pointRadius: 2,
      }],
    };
  }

  const trendLineOpts = {
    scales: {
      x: { display: true, ticks: { maxTicksLimit: 8, font: { size: 10 } } },
      y: { beginAtZero: true },
    },
    plugins: { legend: { display: false } },
  };
</script>

<header class="view-head">
  <p class="subtitle">Facts, episodes and decisions created over 30 days, fact categories, and the fact growth rate</p>
  <div class="head-right">
    <Button variant="outline" size="lg" class="px-4 max-md:min-h-11" onclick={refreshAll} disabled={$store.loading || $obs.loading}>
      {$store.loading || $obs.loading ? 'Loading…' : 'Refresh'}
    </Button>
    <StaleBadge state={$store} />
  </div>
</header>

{#if $store.data}
  {@const db = $store.data.dashboard}
  {@const dist = db.distributions}
  {@const ts = normalizeTimeseries(db.timeseries)}

  <div class="chart-row">
    <!-- Memory growth (line) -->
    <section class="chart-card">
      <h2>Memory growth (30 days)</h2>
      {#if ts.labels.length > 0}
        <Chart
          type="line"
          data={{
            labels: ts.labels,
            datasets: [
              { label: 'Facts',     data: ts.facts,     borderColor: 'var(--series-1)', backgroundColor: tint('--series-1', 0.1), fill: true, borderWidth: 2, pointRadius: 1 },
              { label: 'Episodes',  data: ts.episodes,  borderColor: 'var(--series-2)', backgroundColor: tint('--series-2', 0.1), fill: true, borderWidth: 2, pointRadius: 1 },
              { label: 'Decisions', data: ts.decisions, borderColor: 'var(--series-3)', backgroundColor: tint('--series-3', 0.1), fill: true, borderWidth: 2, pointRadius: 1 },
            ],
          }}
          options={{
            scales: {
              x: { ticks: { maxTicksLimit: 8 }, grid: { display: false } },
              y: { beginAtZero: true, ticks: { precision: 0 } },
            },
            plugins: { legend: { position: 'bottom' } },
          }}
          height="240px"
        />
      {:else}
        <p class="empty">No timeseries data</p>
      {/if}
    </section>

    <!-- Fact categories (doughnut) -->
    <section class="chart-card">
      <h2>Fact categories</h2>
      {#if Object.keys(dist.fact_categories).length > 0}
        {@const fc = sortedDistribution(dist.fact_categories)}
        <Chart
          type="doughnut"
          data={{
            labels: fc.labels,
            datasets: [{ data: fc.values, backgroundColor: doughnutColors(fc.labels.length), borderWidth: 0, hoverOffset: 4 }],
          }}
          options={{ cutout: '60%', plugins: { legend: { position: 'bottom', labels: { font: { size: 11 } } } } }}
          height="240px"
        />
      {:else}
        <p class="empty">No facts categorized yet</p>
      {/if}
    </section>
  </div>

{:else if $store.error}
  <p class="status-msg error">Failed to load growth data — <button class="retry-link" onclick={() => void store.refresh()}>retry</button></p>
{:else}
  <p class="status-msg">Loading…</p>
{/if}

<!-- Fact growth rate (line), from Observability's drift trends -->
<section class="chart-card mt">
  <div class="card-head">
    <h2>Fact Growth Rate (7d)</h2>
    <StaleBadge state={$obs} />
  </div>
  {#if $obs.data}
    {@const factPts = $obs.data.drift_trends?.fact_count_delta ?? []}
    {#if factPts.length > 0}
      <Chart type="line" data={factTrendData(factPts)} options={trendLineOpts} height="180px" />
    {:else}
      <p class="empty">No fact growth data yet</p>
    {/if}
  {:else if $obs.error}
    <p class="empty error">Failed to load the fact growth rate — <button class="retry-link" onclick={() => void obs.refresh()}>retry</button></p>
  {:else}
    <p class="empty">Loading…</p>
  {/if}
</section>

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
    font-size: 0.875rem;
    font-weight: 600;
    color: var(--text);
    margin: 0 0 0.75rem;
  }

  :global(html:root .page-ground) h2 {
    font-size: 15px;
    font-weight: 650;
  }

  .head-right {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-shrink: 0;
  }

  .chart-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1rem;
  }

  @media (max-width: 640px) {
    .chart-row {
      grid-template-columns: 1fr;
    }
  }

  .chart-card {
    background: var(--surface);
    border: var(--card-border);
    border-radius: var(--card-radius);
    box-shadow: var(--card-shadow);
    padding: 1rem;
  }

  :global(html:root .page-ground) .chart-card {
    padding: 1.125rem 1.25rem;
  }

  .mt {
    margin-top: 1rem;
  }

  .card-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 0.75rem;
  }

  .empty {
    color: var(--muted);
    font-size: 0.875rem;
    padding: 1.5rem 0;
    text-align: center;
    margin: 0;
  }

  .status-msg {
    color: var(--muted);
    font-size: 0.9375rem;
    padding: 3rem 2rem;
    text-align: center;
  }

  .status-msg.error,
  .empty.error {
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
</style>
