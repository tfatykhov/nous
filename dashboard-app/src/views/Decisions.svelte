<script lang="ts">
  import { apiGet } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import type { CalibrationData } from '../lib/types/api';
  import StatGrid from '../lib/ui/StatGrid.svelte';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';
  import Chart from '../lib/viz/Chart.svelte';
  import { Button } from '../lib/ui/button';
  import { tint } from '../lib/chartColors';

  // Load-once: interval so large it never auto-fires. Refresh button handles manual reload.
  const store = usePoll(
    makePollStore<CalibrationData>(
      (signal) => apiGet<CalibrationData>('/dashboard/calibration', { signal }),
      0, // fetch-once (manual refresh only)
    ),
  );

  // ── Helpers ──────────────────────────────────────────────────────────────

  const OUTCOME_ORDER = ['success', 'partial', 'failure', 'pending'] as const;
  // Chart colours are tokens that Chart.svelte resolves against the canvas's ground (dark: the old literals). The
  // outcomes take the Overview's decision-outcome palette (success and failure apart from partial and from each other
  // under red-green colour blindness); the violet marks --series-0, not the accent, whose paper value is the question
  // frame's blue.
  const OUTCOME_COLORS: Record<string, string> = {
    success: 'var(--oc-success)',
    partial: 'var(--yellow)',
    failure: 'var(--err-text)',
    pending: 'var(--series-7)',
  };
  const MARK = 'var(--series-0)';
  const BRIER_WORD: Record<string, string> = { good: 'good', warn: 'fair', bad: 'poor' };

  function capitalize(s: string): string {
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  function fmtDate(iso: string): string {
    // Show MM-DD from YYYY-MM-DD
    return iso.length >= 10 ? iso.slice(5, 10) : iso;
  }

  function brierClass(b: number | null): 'good' | 'warn' | 'bad' | 'neutral' {
    if (b == null) return 'neutral';
    if (b <= 0.15) return 'good';
    if (b <= 0.25) return 'warn';
    return 'bad';
  }

  /**
   * Build stacked bar datasets for outcome_by_category / outcome_by_stakes.
   * Returns { labels, datasets } ready for Chart.svelte.
   */
  function buildStackedOutcomeChart(
    outcomeMap: Record<string, Record<string, number>>,
  ): { labels: string[]; datasets: { label: string; data: number[]; backgroundColor: string }[] } {
    const keys = Object.keys(outcomeMap);
    const labels = keys.map(capitalize);
    const datasets = OUTCOME_ORDER.map((outcome) => ({
      label: capitalize(outcome),
      data: keys.map((k) => (outcomeMap[k]?.[outcome] ?? 0)),
      backgroundColor: OUTCOME_COLORS[outcome],
    }));
    return { labels, datasets };
  }

  /**
   * Build sorted horizontal-bar data for reason_type_stats.
   */
  function buildReasonChart(stats: Record<string, { count: number; success_rate: number }>) {
    const pairs = Object.entries(stats)
      .map(([type, s]) => ({ type, count: s.count, rate: s.success_rate }))
      .sort((a, b) => b.count - a.count);
    return {
      labels: pairs.map((p) => p.type.replace(/_/g, ' ')),
      data: pairs.map((p) => p.count),
      rates: pairs.map((p) => p.rate),
    };
  }

  /** Latest Brier score from history (last entry with a value). */
  function latestBrier(history: { brier_score: number }[]): number | null {
    for (let i = history.length - 1; i >= 0; i--) {
      if (history[i].brier_score != null) return history[i].brier_score;
    }
    return null;
  }

  /** Total and reviewed decisions from daily_decisions. */
  function decisionTotals(daily: { count: number; reviewed: number; successes: number }[]) {
    return daily.reduce(
      (acc, d) => ({
        total: acc.total + d.count,
        reviewed: acc.reviewed + d.reviewed,
        successes: acc.successes + d.successes,
      }),
      { total: 0, reviewed: 0, successes: 0 },
    );
  }
</script>

<header class="view-head">
  <div>
    <h1>Decision Intelligence</h1>
    <p class="subtitle">Calibration, confidence, and reasoning analytics</p>
  </div>
  <div class="head-right">
    <Button variant="outline" size="lg" class="px-4 max-md:min-h-11" onclick={() => void store.refresh()} disabled={$store.loading}>
      {$store.loading ? 'Loading…' : 'Refresh'}
    </Button>
    <StaleBadge state={$store} />
  </div>
</header>

{#if $store.data}
  {@const d = $store.data}
  {@const brier = latestBrier(d.brier_history)}
  {@const totals = decisionTotals(d.daily_decisions)}
  {@const accuracy = totals.reviewed > 0 ? totals.successes / totals.reviewed : null}

  <!-- ── Stat cards ──────────────────────────────────────────────────────── -->
  <StatGrid stats={[
    { label: 'Decisions (30d)',  value: totals.total.toLocaleString() },
    { label: 'Reviewed (30d)',   value: totals.reviewed.toLocaleString() },
    { label: 'Accuracy (30d)',   value: accuracy != null ? (accuracy * 100).toFixed(1) + '%' : '—' },
    { label: 'Brier score',      value: brier != null ? brier.toFixed(3) : '—' },
  ]} />

  <!-- Brier colour-coded indicator card -->
  <div class="indicator-row">
    <div class="stat-card">
      <div class="stat-value">
        <span class="indicator {brierClass(brier)}"></span>
        {brier != null ? brier.toFixed(4) : '—'}
        {#if BRIER_WORD[brierClass(brier)]}<span class="indicator-word">{BRIER_WORD[brierClass(brier)]}</span>{/if}
      </div>
      <div class="stat-label">Latest Brier score (lower is better)</div>
    </div>
  </div>

  <!-- ── Row 1: Calibration curve + Confidence histogram ───────────────── -->
  <div class="chart-row">
    <section class="chart-card">
      <h2>Calibration Curve</h2>
      {#if d.calibration_curve.length > 0}
        <Chart
          type="line"
          data={{
            labels: d.calibration_curve.map((p) => p.bucket),
            datasets: [
              {
                label: 'Actual Success Rate',
                data: d.calibration_curve.map((p) => p.actual_success_rate),
                borderColor: MARK,
                backgroundColor: tint('--series-0', 0.1),
                fill: true,
                pointBackgroundColor: MARK,
                pointRadius: 5,
                borderWidth: 2,
              },
              {
                // Perfect-calibration reference line (y = x)
                label: 'Perfect calibration',
                data: d.calibration_curve.map((p) => parseFloat(p.bucket)),
                borderColor: tint('--muted', 0.4),
                borderDash: [5, 5],
                borderWidth: 1,
                pointRadius: 0,
                fill: false,
              },
            ],
          }}
          options={{
            scales: {
              x: { title: { display: true, text: 'Predicted Confidence' } },
              y: { title: { display: true, text: 'Actual Success Rate' }, min: 0, max: 1 },
            },
            plugins: { legend: { display: false } },
          }}
          height="260px"
        />
      {:else}
        <p class="empty">Not enough data for calibration curve</p>
      {/if}
    </section>

    <section class="chart-card">
      <h2>Confidence Distribution</h2>
      {#if d.confidence_histogram.length > 0}
        <Chart
          type="bar"
          data={{
            labels: d.confidence_histogram.map((b) => b.range),
            datasets: [{
              label: 'Count',
              data: d.confidence_histogram.map((b) => b.count),
              backgroundColor: tint('--series-0', 0.5),
              borderColor: MARK,
              borderWidth: 1,
            }],
          }}
          options={{
            scales: {
              x: { title: { display: true, text: 'Confidence Range' }, grid: { display: false } },
              y: { beginAtZero: true, ticks: { precision: 0 }, title: { display: true, text: 'Count' } },
            },
            plugins: { legend: { display: false } },
          }}
          height="260px"
        />
      {:else}
        <p class="empty">No confidence data</p>
      {/if}
    </section>
  </div>

  <!-- ── Row 2: Outcome by category + Outcome by stakes ────────────────── -->
  <div class="chart-row">
    <section class="chart-card">
      <h2>Outcome by Category</h2>
      {#if Object.keys(d.outcome_by_category).length > 0}
        {@const chart = buildStackedOutcomeChart(d.outcome_by_category)}
        <Chart
          type="bar"
          data={{ labels: chart.labels, datasets: chart.datasets }}
          options={{
            scales: {
              x: { stacked: true, grid: { display: false } },
              y: { stacked: true, beginAtZero: true, ticks: { precision: 0 } },
            },
            plugins: { legend: { position: 'bottom' } },
          }}
          height="260px"
        />
      {:else}
        <p class="empty">No category data</p>
      {/if}
    </section>

    <section class="chart-card">
      <h2>Outcome by Stakes</h2>
      {#if Object.keys(d.outcome_by_stakes).length > 0}
        {@const chart = buildStackedOutcomeChart(d.outcome_by_stakes)}
        <Chart
          type="bar"
          data={{ labels: chart.labels, datasets: chart.datasets }}
          options={{
            scales: {
              x: { stacked: true, grid: { display: false } },
              y: { stacked: true, beginAtZero: true, ticks: { precision: 0 } },
            },
            plugins: { legend: { position: 'bottom' } },
          }}
          height="260px"
        />
      {:else}
        <p class="empty">No stakes data</p>
      {/if}
    </section>
  </div>

  <!-- ── Row 3: Reason type usage (full width) ─────────────────────────── -->
  <section class="chart-card">
    <h2>Reason Type Usage</h2>
    {#if Object.keys(d.reason_type_stats).length > 0}
      {@const r = buildReasonChart(d.reason_type_stats)}
      <Chart
        type="bar"
        data={{
          labels: r.labels,
          datasets: [{
            label: 'Usage Count',
            data: r.data,
            backgroundColor: tint('--series-0', 0.5),
            borderColor: MARK,
            borderWidth: 1,
          }],
        }}
        options={{
          indexAxis: 'y',
          scales: {
            x: { beginAtZero: true, ticks: { precision: 0 }, grid: { display: false } },
            y: { grid: { display: false } },
          },
          plugins: { legend: { display: false } },
        }}
        height="220px"
      />
    {:else}
      <p class="empty">No reason type data</p>
    {/if}
  </section>

  <!-- ── Row 4: Brier score over time + Decisions per day ──────────────── -->
  <div class="chart-row">
    <section class="chart-card">
      <h2>Brier Score Over Time</h2>
      {#if d.brier_history.length > 0}
        <Chart
          type="line"
          data={{
            labels: d.brier_history.map((p) => fmtDate(p.date)),
            datasets: [{
              label: 'Brier Score',
              data: d.brier_history.map((p) => p.brier_score),
              borderColor: MARK,
              backgroundColor: tint('--series-0', 0.1),
              fill: true,
              pointBackgroundColor: MARK,
              pointRadius: 3,
              borderWidth: 2,
            }],
          }}
          options={{
            scales: {
              x: { ticks: { maxTicksLimit: 10 }, grid: { display: false } },
              y: { title: { display: true, text: 'Brier Score (lower is better)' }, min: 0, max: 0.5 },
            },
            plugins: { legend: { display: false } },
          }}
          height="240px"
        />
      {:else}
        <p class="empty">No Brier score history</p>
      {/if}
    </section>

    <section class="chart-card">
      <h2>Decisions Per Day</h2>
      {#if d.daily_decisions.length > 0}
        <Chart
          type="bar"
          data={{
            labels: d.daily_decisions.map((p) => fmtDate(p.date)),
            datasets: [{
              label: 'Decisions',
              data: d.daily_decisions.map((p) => p.count),
              backgroundColor: tint('--series-3', 0.5),
              borderColor: 'var(--series-3)',
              borderWidth: 1,
            }],
          }}
          options={{
            scales: {
              x: { ticks: { maxTicksLimit: 15 }, grid: { display: false } },
              y: { beginAtZero: true, ticks: { precision: 0 } },
            },
            plugins: { legend: { display: false } },
          }}
          height="240px"
        />
      {:else}
        <p class="empty">No daily decision data</p>
      {/if}
    </section>
  </div>

{:else if $store.error}
  <p class="status-msg error">
    Failed to load decision intelligence data —
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
    flex-wrap: wrap;
    margin-bottom: 1.25rem;
  }

  h1 {
    font-size: 1.4rem;
    font-weight: 700;
    color: var(--text);
    margin: 0 0 0.125rem;
  }

  /* The paper look's title size; the dark title keeps the rule above. */
  :global(html:root .page-ground) h1 {
    font-size: 30px;
    font-weight: 650;
    letter-spacing: -0.022em;
    line-height: 1.15;
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

  /* Brier indicator card */
  .indicator-row {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 1rem;
    margin-top: 1rem;
  }

  /* A copy of StatGrid's card, by the same tokens. */
  .stat-card {
    background: var(--surface);
    border: var(--card-border);
    border-radius: var(--card-radius-sm);
    box-shadow: var(--card-shadow);
    padding: 1rem;
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  .stat-value {
    font-size: 1.5rem;
    font-weight: 700;
    color: var(--text);
    line-height: 1.2;
    display: flex;
    align-items: center;
    gap: 0.375rem;
  }

  .stat-label {
    font-size: 0.75rem;
    font-weight: 500;
    color: var(--muted);
    text-transform: none;
    letter-spacing: 0;
  }

  .indicator {
    display: inline-block;
    width: 0.625rem;
    height: 0.625rem;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .indicator.good    { background: var(--green); }
  .indicator.warn    { background: var(--yellow); }
  .indicator.bad     { background: var(--red); }
  .indicator.neutral { background: var(--muted); }

  /* The dot's meaning in a word: status is never colour alone. */
  .indicator-word {
    font-size: 0.75rem;
    font-weight: 500;
    color: var(--muted);
  }

  .chart-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1rem;
    margin-top: 1rem;
  }

  /* A chart cell may shrink below its canvas's width (a grid item's min-width is auto), so a phone never scrolls sideways. */
  .chart-row > * {
    min-width: 0;
  }

  @media (max-width: 640px) {
    .chart-row {
      grid-template-columns: 1fr;
    }
    .indicator-row {
      grid-template-columns: 1fr;
    }
  }

  .chart-card {
    background: var(--surface);
    border: var(--card-border);
    border-radius: var(--card-radius);
    box-shadow: var(--card-shadow);
    padding: 1rem;
    margin-top: 1rem;
  }

  /* chart-row children already get margin-top from the row */
  .chart-row > .chart-card {
    margin-top: 0;
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
