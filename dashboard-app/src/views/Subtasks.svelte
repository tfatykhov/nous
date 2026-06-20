<script lang="ts">
  import { apiGet } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import type { SubtasksData } from '../lib/types/api';
  import StatGrid from '../lib/ui/StatGrid.svelte';
  import DataTable from '../lib/ui/DataTable.svelte';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';
  import Chart from '../lib/viz/Chart.svelte';

  // ── Hours selector ────────────────────────────────────────────────────────
  // The fetcher closes over `hours` by reading it each time it is called.
  // When the user changes `hours`, the $effect below calls store.refresh()
  // so the new value is used immediately; subsequent 30-s ticks also read
  // the updated value because `hours` is a module-scope $state variable.
  const HOUR_OPTIONS = [24, 72, 168] as const;
  let hours = $state<24 | 72 | 168>(24);

  const store = usePoll(
    makePollStore<SubtasksData>(
      (signal) => apiGet<SubtasksData>(`/dashboard/subtasks?hours=${hours}`, { signal }),
      30_000,
    ),
  );

  // Re-fetch immediately when hours changes (store was already started by usePoll)
  $effect(() => {
    // reading `hours` establishes the reactive dependency
    void hours;
    void store.refresh();
  });

  // ── Outcome colours (mirrors subtasks.js OUTCOME_COLORS) ─────────────────
  const OUTCOME_COLORS: Record<string, string> = {
    completed: '#10b981',
    incomplete_blocked: '#f59e0b',
    incomplete_no_terminal: '#ef4444',
    validation_failed: '#ef4444',
    timed_out: '#dc2626',
    errored: '#7c2d12',
    cancelled: '#6b7280',
    unknown: '#9ca3af',
  };

  function outcomeColor(name: string): string {
    return OUTCOME_COLORS[name] ?? '#6b7280';
  }

  // ── Formatters ────────────────────────────────────────────────────────────
  function pct(v: number): string {
    return (v * 100).toFixed(1) + '%';
  }

  function formatTs(ts: string | null): string {
    if (!ts) return '—';
    return new Date(ts).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' });
  }

  // ── Chart data builders ───────────────────────────────────────────────────
  // Outcome doughnut — from totals.by_outcome
  function outcomeChartData(byOutcome: Record<string, number>) {
    const labels = Object.keys(byOutcome);
    return {
      labels,
      datasets: [{
        data: labels.map((k) => byOutcome[k]),
        backgroundColor: labels.map(outcomeColor),
        borderWidth: 0,
      }],
    };
  }

  // Tokens-by-outcome bar — mean_total_tokens per outcome
  function tokensChartData(tokensByOutcome: Record<string, { mean_total_tokens: number; mean_tool_calls: number; n: number }>) {
    const labels = Object.keys(tokensByOutcome);
    return {
      labels,
      datasets: [{
        label: 'Mean total tokens',
        data: labels.map((k) => tokensByOutcome[k].mean_total_tokens),
        backgroundColor: labels.map(outcomeColor),
        borderWidth: 0,
      }],
    };
  }

  // Daily trend line — one dataset per outcome, labels = dates
  function trendChartData(daily: { date: string; by_outcome: Record<string, number> }[]) {
    if (daily.length === 0) return { labels: [], datasets: [] };
    const allOutcomes = new Set<string>();
    daily.forEach((d) => Object.keys(d.by_outcome).forEach((k) => allOutcomes.add(k)));
    const outcomes = Array.from(allOutcomes).sort();
    return {
      labels: daily.map((d) => d.date),
      datasets: outcomes.map((outcome) => ({
        label: outcome,
        data: daily.map((d) => d.by_outcome[outcome] ?? 0),
        borderColor: outcomeColor(outcome),
        backgroundColor: outcomeColor(outcome) + '33',
        fill: false,
        borderWidth: 2,
        pointRadius: 2,
        pointHoverRadius: 5,
      })),
    };
  }

  // ── Table column definitions ──────────────────────────────────────────────
  const failingCols = [
    { key: 'task_prefix', label: 'Task (first 80 chars)' },
    { key: 'failures', label: 'Failures' },
    { key: 'total', label: 'Total' },
    { key: 'rate_fmt', label: 'Rate' },
  ];

  const recentCols = [
    { key: 'completed_fmt', label: 'Completed' },
    { key: 'final_outcome', label: 'Outcome' },
    { key: 'attempts', label: 'Attempts' },
    { key: 'tokens_total', label: 'Tokens' },
    { key: 'tool_calls_made', label: 'Tool calls' },
    { key: 'task', label: 'Task' },
  ];
</script>

<header class="view-head">
  <div>
    <h1>Subtasks</h1>
    <p class="subtitle">F061 outcome metrics — last {$store.data?.window_hours ?? hours}h</p>
  </div>
  <div class="head-right">
    <label class="hours-label" for="hours-select">Window</label>
    <select
      id="hours-select"
      class="hours-select"
      bind:value={hours}
    >
      {#each HOUR_OPTIONS as h}
        <option value={h}>{h}h</option>
      {/each}
    </select>
    <StaleBadge state={$store} />
  </div>
</header>

{#if $store.data}
  {@const d = $store.data}
  {@const t = d.totals}
  {@const hasOutcomes = Object.keys(t.by_outcome).length > 0}
  {@const hasTokens = Object.keys(d.tokens_by_outcome).length > 0}
  {@const hasDag = Object.keys(d.dag_correlation).length > 0}

  <!-- Stat cards: 5 key numbers -->
  <StatGrid stats={[
    { label: 'Total terminal',  value: t.total_terminal.toLocaleString() },
    { label: 'Empty rate',      value: pct(t.empty_rate) },
    { label: 'Retry rate',      value: pct(t.retry_rate) },
    { label: 'Failing tasks',   value: d.top_failing_tasks.length.toLocaleString() },
    { label: 'DAG-attached',    value: Object.values(d.dag_correlation).reduce((a, b) => a + b, 0).toLocaleString() },
  ]} />

  <!-- Row 2: outcome doughnut + tokens bar -->
  <div class="chart-row">
    <section class="chart-card">
      <h2>Outcome distribution</h2>
      {#if hasOutcomes}
        <Chart
          type="doughnut"
          data={outcomeChartData(t.by_outcome)}
          options={{ cutout: '55%', plugins: { legend: { position: 'bottom' } } }}
          height="240px"
        />
      {:else}
        <p class="empty">No terminal subtasks in window.</p>
      {/if}
    </section>

    <section class="chart-card">
      <h2>Mean tokens by outcome</h2>
      {#if hasTokens}
        <Chart
          type="bar"
          data={tokensChartData(d.tokens_by_outcome)}
          options={{
            indexAxis: 'y',
            scales: {
              x: { beginAtZero: true, ticks: { precision: 0 }, grid: { display: false } },
              y: { grid: { display: false } },
            },
            plugins: { legend: { display: false } },
          }}
          height="240px"
        />
      {:else}
        <p class="empty">No data.</p>
      {/if}
    </section>
  </div>

  <!-- Daily trend line chart -->
  <section class="chart-card">
    <h2>Daily trend</h2>
    {#if d.daily_trend.length > 0}
      <Chart
        type="line"
        data={trendChartData(d.daily_trend)}
        options={{
          scales: {
            x: { grid: { display: false } },
            y: { beginAtZero: true, ticks: { precision: 0 } },
          },
          plugins: { legend: { position: 'bottom' } },
        }}
        height="220px"
      />
    {:else}
      <p class="empty">No data.</p>
    {/if}
  </section>

  <!-- Top failing tasks -->
  <section class="table-card">
    <h2>Top failing tasks</h2>
    {#if d.top_failing_tasks.length > 0}
      <DataTable
        columns={failingCols}
        rows={d.top_failing_tasks.map((r) => ({
          ...r,
          rate_fmt: pct(r.failure_rate),
        }))}
        mode="cards"
        rowKey={(r) => r.task_prefix}
      />
    {:else}
      <p class="empty">No failing tasks in window.</p>
    {/if}
  </section>

  <!-- DAG correlation -->
  {#if hasDag}
    <section class="table-card">
      <h2>DAG correlation</h2>
      <DataTable
        columns={[{ key: 'outcome', label: 'Outcome' }, { key: 'count', label: 'Count (DAG-attached)' }]}
        rows={Object.entries(d.dag_correlation).sort().map(([outcome, count]) => ({ outcome, count }))}
        mode="cards"
        rowKey={(r) => r.outcome}
      />
    </section>
  {/if}

  <!-- Recent terminal subtasks -->
  <section class="table-card">
    <h2>Recent terminal subtasks</h2>
    {#if d.recent_outcomes.length > 0}
      <DataTable
        columns={recentCols}
        rows={d.recent_outcomes.map((r) => ({
          ...r,
          completed_fmt: formatTs(r.completed_at),
          tokens_total: (r.tokens_in ?? 0) + (r.tokens_out ?? 0),
        }))}
        mode="cards"
        rowKey={(r) => r.id}
      />
    {:else}
      <p class="empty">No recent terminal subtasks.</p>
    {/if}
  </section>

{:else if $store.error}
  <p class="status-msg error">Failed to load subtask data — retrying…</p>
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

  .hours-label {
    font-size: 0.75rem;
    color: var(--muted);
    white-space: nowrap;
  }

  .hours-select {
    font-size: 0.8125rem;
    padding: 0.25rem 0.5rem;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm, 6px);
    background: var(--surface);
    color: var(--text);
    cursor: pointer;
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
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1rem;
    margin-top: 1rem;
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
    color: var(--red, #ef4444);
  }
</style>
