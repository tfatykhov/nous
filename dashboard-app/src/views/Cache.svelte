<script lang="ts">
  import { fmtUtc } from '../lib/harness';
  import { apiGet } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import { routeHref } from '../lib/router';
  import type { CacheData } from '../lib/types/api';
  import StatGrid from '../lib/ui/StatGrid.svelte';
  import DataTable from '../lib/ui/DataTable.svelte';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';
  import Chart from '../lib/viz/Chart.svelte';
  import { tint } from '../lib/chartColors';

  // Embedded as the Model calls screen's Cache tab: that screen's header is the title, so this view drops its h1
  // (its subtitle and staleness badge stay).
  let { embedded = false }: { embedded?: boolean } = $props();

  const store = usePoll(
    makePollStore<CacheData>(
      (signal) => apiGet<CacheData>('/dashboard/cache', { signal }),
      30_000,
    ),
  );

  // ── Helpers ──────────────────────────────────────────────────────────
  function formatTokens(n: number): string {
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
    if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K';
    return String(n);
  }

  function formatTime(ts: string): string {
    return fmtUtc(ts);
  }

  // ── Session table columns ─────────────────────────────────────────────
  const sessionCols = [
    { key: 'session_id_short', label: 'Session' },
    { key: 'calls', label: 'Calls' },
    { key: 'input_tokens_fmt', label: 'Input' },
    { key: 'cache_read_fmt', label: 'Cache read' },
    { key: 'hit_rate_fmt', label: 'Hit rate' },
    { key: 'breaks', label: 'Breaks' },
  ];

  // ── Recent calls table columns ────────────────────────────────────────
  const callCols = [
    { key: 'time_fmt', label: 'Time' },
    { key: 'session_short', label: 'Session' },
    { key: 'turn', label: 'Turn' },
    { key: 'model', label: 'Model' },
    { key: 'input_fmt', label: 'Input' },
    { key: 'cache_read_fmt', label: 'Cache read' },
    { key: 'hit_rate_fmt', label: 'Hit rate' },
    { key: 'break_fmt', label: 'Break' },
  ];
</script>

<header class="view-head">
  <div>
    {#if !embedded}<h1>Cache</h1>{/if}
    <p class="subtitle">API cache performance, token savings, and break analysis</p>
  </div>
  <StaleBadge state={$store} />
</header>

{#if $store.data}
  {@const d = $store.data}
  {@const s = d.summary}
  {@const uncached = Math.max(0, s.total_input_tokens - s.total_cache_read - s.total_cache_created)}

  <StatGrid stats={[
    { label: 'Total API calls',         value: s.total_calls.toLocaleString() },
    { label: 'Cache hit rate',           value: s.overall_hit_rate + '%' },
    { label: 'Tokens saved (cache read)', value: formatTokens(s.total_cache_read) },
    { label: 'Cache breaks',             value: s.total_breaks.toLocaleString() + ' (' + s.break_rate + '%)' },
  ]} />

  <!-- Row 2: two charts side by side -->
  <div class="chart-row">
    <section class="chart-card">
      <h2>Token Breakdown</h2>
      <Chart
        type="doughnut"
        data={{
          labels: ['Cache Read', 'Cache Created', 'Uncached'],
          datasets: [{
            data: [s.total_cache_read, s.total_cache_created, uncached],
            backgroundColor: ['var(--hit-good)', 'var(--cache-blue)', 'var(--cache-grey)'],
            borderWidth: 0,
          }],
        }}
        options={{ cutout: '60%', plugins: { legend: { position: 'bottom' } } }}
        height="240px"
      />
    </section>

    <section class="chart-card">
      <h2>Break Components</h2>
      {#if Object.keys(d.break_components).length > 0}
        <Chart
          type="bar"
          data={{
            labels: Object.keys(d.break_components),
            datasets: [{
              label: 'Breaks',
              data: Object.values(d.break_components),
              backgroundColor: [5, 6, 1, 2, 3, 4, 9, 10].map((n) => `var(--series-${n})`),
              borderWidth: 0,
            }],
          }}
          options={{
            indexAxis: 'y',
            scales: { x: { beginAtZero: true, ticks: { precision: 0 }, grid: { display: false } }, y: { grid: { display: false } } },
            plugins: { legend: { display: false } },
          }}
          height="240px"
        />
      {:else}
        <p class="empty">No break component data</p>
      {/if}
    </section>
  </div>

  <!-- Row 3: efficiency timeline -->
  <section class="chart-card">
    <h2>Efficiency Timeline</h2>
    {#if d.timeline.length > 0}
      {@const sorted = [...d.timeline].reverse()}
      <Chart
        type="line"
        data={{
          labels: sorted.map((t) => formatTime(t.timestamp)),
          datasets: [{
            label: 'Hit Rate %',
            data: sorted.map((t) => t.hit_rate),
            borderColor: 'var(--hit-good)',
            backgroundColor: tint('--hit-good', 0.1),
            fill: true,
            borderWidth: 2,
            pointRadius: 2,
            pointHoverRadius: 5,
          }],
        }}
        options={{
          scales: {
            x: { grid: { display: false } },
            y: { beginAtZero: true, max: 100, ticks: { callback: (v: number) => v + '%' } },
          },
          plugins: { legend: { display: false } },
        }}
        height="200px"
      />
    {:else}
      <p class="empty">No timeline data</p>
    {/if}
  </section>

  <!-- Row 4: sessions table -->
  <section class="table-card">
    <h2>Sessions</h2>
    {#if d.sessions.length > 0}
      <DataTable
        columns={sessionCols}
        rows={d.sessions.map((r) => ({
          ...r,
          session_id_short: (r.session_id ?? '').slice(0, 8) || '—',
          input_tokens_fmt: formatTokens(r.input_tokens),
          cache_read_fmt: formatTokens(r.cache_read),
          hit_rate_fmt: r.hit_rate + '%',
        }))}
        mode="cards"
        rowKey={(r) => r.session_id ?? '—'}
      />
    {:else}
      <p class="empty">No session data available</p>
    {/if}
  </section>

  <!-- Row 5: recent calls table -->
  <section class="table-card">
    <h2>Recent Calls</h2>
    {#if d.timeline.length > 0}
      <DataTable
        columns={callCols}
        rows={d.timeline.slice(0, 20).map((c) => ({
          ...c,
          time_fmt: formatTime(c.timestamp),
          session_short: (c.session_id ?? '').slice(0, 8) || '—',
          input_fmt: formatTokens(c.input_tokens),
          cache_read_fmt: formatTokens(c.cache_read),
          hit_rate_fmt: c.hit_rate + '%',
          break_fmt: c.cache_break ? 'yes' : '',
        }))}
        mode="cards"
        rowKey={(_, i) => String(i)}
      >
        {#snippet cell(r, c)}
          {#if c.key === 'time_fmt' && r.id}
            <a class="call-link" href={routeHref('model-calls', ['c', r.id])} aria-label={`Open the call at ${r.time_fmt}`}>{r.time_fmt}</a>
          {:else}
            {r[c.key]}
          {/if}
        {/snippet}
      </DataTable>
    {:else}
      <p class="empty">No recent API calls</p>
    {/if}
  </section>

{:else if $store.error}
  <p class="status-msg error">Failed to load cache data — retrying…</p>
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

  .chart-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1rem;
    margin-top: 1rem;
  }

  @media (max-width: 640px) {
    .chart-row {
      grid-template-columns: 1fr;
    }
  }

  .chart-card,
  .table-card {
    background: var(--surface);
    border: var(--card-border);
    border-radius: var(--card-radius);
    box-shadow: var(--card-shadow);
    padding: 1rem;
    margin-top: 1rem;
  }

  .call-link {
    color: var(--accent-text);
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

  .status-msg.error {
    color: var(--err-text);
  }
</style>
