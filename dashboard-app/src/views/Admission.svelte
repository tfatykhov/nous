<script lang="ts">
  import { apiGet } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import type { AdmissionData, AdmissionRejectedPage, AdmissionDimStats } from '../lib/types/api';
  import StatGrid from '../lib/ui/StatGrid.svelte';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';
  import DataTable from '../lib/ui/DataTable.svelte';
  import Chart from '../lib/viz/Chart.svelte';

  // ── Main payload — loaded once (effectively: interval = MAX so it never auto-fires) ──
  const store = usePoll(
    makePollStore<AdmissionData>(
      (signal) => apiGet<AdmissionData>('/dashboard/admission', { signal }),
      Number.MAX_SAFE_INTEGER,
    ),
  );

  // ── Threshold simulator — pure client-side state ───────────────────────
  let simThreshold = $state(0.55);

  $effect(() => {
    // Sync slider default to loaded config threshold
    if ($store.data) {
      simThreshold = $store.data.config.threshold;
    }
  });

  function simulatorResult(d: AdmissionData, threshold: number) {
    let admitted = 0;
    let rejected = 0;
    for (const bucket of d.score_distribution) {
      const start = parseFloat(bucket.bucket.split('-')[0]);
      if (start >= threshold) admitted += bucket.count;
      else rejected += bucket.count;
    }
    const total = admitted + rejected;
    const pct = total > 0 ? ((rejected / total) * 100).toFixed(1) : '0.0';
    return { admitted, rejected, pct };
  }

  // ── Rejected facts table — on-demand paginated fetch ──────────────────
  const PAGE_SIZE = 25;

  let rejectedOffset = $state(0);
  let rejectedData = $state<AdmissionRejectedPage | null>(null);
  let rejectedLoading = $state(false);
  let rejectedError = $state<string | null>(null);

  async function loadRejected(offset: number) {
    rejectedLoading = true;
    rejectedError = null;
    try {
      const data = await apiGet<AdmissionRejectedPage>(
        `/dashboard/admission/rejected?limit=${PAGE_SIZE}&offset=${offset}`,
      );
      rejectedData = data;
      rejectedOffset = offset;
    } catch (e) {
      rejectedError = e instanceof Error ? e.message : 'Failed to load rejected facts.';
    } finally {
      rejectedLoading = false;
    }
  }

  // Load the first page of rejected facts once the main payload is available
  $effect(() => {
    if ($store.data && rejectedData === null && !rejectedLoading) {
      void loadRejected(0);
    }
  });

  // ── Chart data builders ────────────────────────────────────────────────

  function scoreDistChartData(d: AdmissionData, threshold: number) {
    const labels = d.score_distribution.map((b) => b.bucket);
    const counts = d.score_distribution.map((b) => b.count);
    const colors = d.score_distribution.map((b) => {
      const start = parseFloat(b.bucket.split('-')[0]);
      return start >= threshold ? '#34d399' : '#f87171';
    });
    return {
      labels,
      datasets: [{ label: 'Facts', data: counts, backgroundColor: colors, borderRadius: 4 }],
    };
  }

  const scoreDistOptions = {
    plugins: { legend: { display: false } },
    scales: {
      x: { title: { display: true, text: 'Composite Score' } },
      y: { beginAtZero: true, title: { display: true, text: 'Fact Count' }, ticks: { precision: 0 } },
    },
  };

  function bySourceChartData(d: AdmissionData) {
    const sources = Object.keys(d.by_source);
    return {
      labels: sources,
      datasets: [
        { label: 'Admitted', data: sources.map((s) => d.by_source[s].admitted), backgroundColor: '#34d399' },
        { label: 'Rejected', data: sources.map((s) => d.by_source[s].rejected), backgroundColor: '#f87171' },
        { label: 'Bypassed', data: sources.map((s) => d.by_source[s].bypassed ?? 0), backgroundColor: '#6b6b8a' },
      ],
    };
  }

  const bySourceOptions = {
    plugins: { legend: { display: true, position: 'bottom' } },
    scales: {
      x: { stacked: true },
      y: { stacked: true, beginAtZero: true, title: { display: true, text: 'Count' }, ticks: { precision: 0 } },
    },
  };

  function byCategoryChartData(d: AdmissionData) {
    const cats = Object.keys(d.by_category);
    return {
      labels: cats,
      datasets: [
        { label: 'Admitted', data: cats.map((c) => d.by_category[c].admitted), backgroundColor: '#34d399' },
        { label: 'Rejected', data: cats.map((c) => d.by_category[c].rejected), backgroundColor: '#f87171' },
      ],
    };
  }

  const byCategoryOptions = {
    plugins: { legend: { display: true, position: 'bottom' } },
    scales: {
      x: { stacked: true },
      y: { stacked: true, beginAtZero: true, title: { display: true, text: 'Count' }, ticks: { precision: 0 } },
    },
  };

  function trendRateChartData(d: AdmissionData) {
    return {
      labels: d.daily_trend.map((t) => t.date.slice(5)),
      datasets: [{
        label: 'Admission Rate (%)',
        data: d.daily_trend.map((t) => t.scored > 0 ? +((t.admitted / t.scored) * 100).toFixed(1) : null),
        borderColor: '#34d399',
        backgroundColor: 'rgba(52,211,153,0.1)',
        fill: true,
        spanGaps: true,
      }],
    };
  }

  const trendRateOptions = {
    plugins: { legend: { display: false } },
    scales: {
      x: { ticks: { maxTicksLimit: 10 }, grid: { display: false } },
      y: { min: 0, max: 100, title: { display: true, text: 'Admission Rate (%)' } },
    },
  };

  function trendScoreChartData(d: AdmissionData) {
    return {
      labels: d.daily_trend.map((t) => t.date.slice(5)),
      datasets: [{
        label: 'Avg Score',
        data: d.daily_trend.map((t) => t.avg_score),
        borderColor: '#7c6af7',
        backgroundColor: 'rgba(124,106,247,0.1)',
        fill: true,
        spanGaps: true,
      }],
    };
  }

  const trendScoreOptions = {
    plugins: { legend: { display: false } },
    scales: {
      x: { ticks: { maxTicksLimit: 10 }, grid: { display: false } },
      y: { min: 0, max: 1, title: { display: true, text: 'Avg Composite Score' } },
    },
  };

  function bypassChartData(d: AdmissionData) {
    const reasons = Object.keys(d.bypass_breakdown);
    const palette = ['#7c6af7', '#60a5fa', '#34d399', '#fb923c', '#f87171', '#6b6b8a'];
    return {
      labels: reasons,
      datasets: [{
        data: reasons.map((r) => d.bypass_breakdown[r]),
        backgroundColor: palette.slice(0, reasons.length),
        borderColor: 'var(--surface)',
        borderWidth: 2,
      }],
    };
  }

  const bypassOptions = {
    plugins: { legend: { position: 'bottom' } },
  };

  // ── Dimension box-plot helpers ─────────────────────────────────────────

  const DIMS = ['utility', 'confidence', 'novelty', 'recency', 'type_prior'] as const;

  function isBoxStats(v: unknown): v is AdmissionDimStats {
    return typeof v === 'object' && v !== null && 'min' in v && 'median' in v;
  }

  function pct(val: number, maxVal: number): string {
    return (Math.min(val / Math.max(maxVal, 1), 1) * 100).toFixed(1) + '%';
  }

  // ── Rejected table columns ─────────────────────────────────────────────

  const rejectedCols = [
    { key: 'content_preview', label: 'Content' },
    { key: 'source',          label: 'Source' },
    { key: 'category',        label: 'Category' },
    { key: 'composite_score', label: 'Score' },
    { key: 'utility',         label: 'Utility' },
    { key: 'confidence',      label: 'Conf' },
    { key: 'novelty',         label: 'Novelty' },
    { key: 'recency',         label: 'Recency' },
    { key: 'type_prior',      label: 'Type' },
    { key: 'created_at_fmt',  label: 'Date' },
  ];

  function flattenFacts(page: AdmissionRejectedPage) {
    return page.facts.map((f) => ({
      ...f,
      utility:      f.scores.utility     != null ? f.scores.utility.toFixed(2)     : '-',
      confidence:   f.scores.confidence  != null ? f.scores.confidence.toFixed(2)  : '-',
      novelty:      f.scores.novelty     != null ? f.scores.novelty.toFixed(2)     : '-',
      recency:      f.scores.recency     != null ? f.scores.recency.toFixed(2)     : '-',
      type_prior:   f.scores.type_prior  != null ? f.scores.type_prior.toFixed(2)  : '-',
      created_at_fmt: f.created_at ? f.created_at.slice(0, 10) : '-',
    }));
  }
</script>

<!-- ── Header ──────────────────────────────────────────────────────────── -->
<header class="view-head">
  <div>
    <h1>Admission Control</h1>
    <p class="subtitle">F023 Memory Admission — score analysis and threshold tuning</p>
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

  {#if d.summary.total_scored === 0 && d.summary.bypassed === 0}
    <p class="empty-state">
      No admission data yet — facts will appear here as they are scored by F023.
      Ensure <code>NOUS_ADMISSION_ENABLED=true</code>.
    </p>
  {:else}

    <!-- ── Mode banner ────────────────────────────────────────────────────── -->
    <div class="banner" class:banner-shadow={d.config.shadow_mode} class:banner-enforced={!d.config.shadow_mode}>
      <div class="banner-dot"></div>
      <div class="banner-body">
        {#if d.config.shadow_mode}
          <strong>SHADOW MODE ACTIVE</strong> — All facts are being admitted. Scores are logged but not enforced.
          <span class="banner-meta">
            Threshold: {d.config.threshold} &nbsp;|&nbsp;
            Facts scored: {d.summary.total_scored.toLocaleString()} &nbsp;|&nbsp;
            Would reject: {d.summary.would_reject.toLocaleString()} ({(d.summary.rejection_rate * 100).toFixed(1)}%)
          </span>
        {:else}
          <strong>ENFORCEMENT ACTIVE</strong> — Facts below {d.config.threshold} are rejected.
          <span class="banner-meta">
            Admitted: {d.summary.admitted.toLocaleString()} ({((1 - d.summary.rejection_rate) * 100).toFixed(0)}%) &nbsp;|&nbsp;
            Rejected: {d.summary.would_reject.toLocaleString()} ({(d.summary.rejection_rate * 100).toFixed(1)}%) &nbsp;|&nbsp;
            Bypassed: {d.summary.bypassed.toLocaleString()}
          </span>
        {/if}
      </div>
    </div>

    <!-- ── Stat cards ─────────────────────────────────────────────────────── -->
    <StatGrid stats={[
      { label: 'Total Scored',    value: d.summary.total_scored.toLocaleString() },
      { label: 'Admitted',        value: d.summary.admitted.toLocaleString() },
      { label: 'Rejected',        value: d.summary.would_reject.toLocaleString() },
      { label: 'Bypassed',        value: d.summary.bypassed.toLocaleString() },
      { label: 'Rejection Rate',  value: `${(d.summary.rejection_rate * 100).toFixed(1)}%` },
      { label: 'Avg Score',       value: d.summary.avg_composite_score.toFixed(3) },
    ]} />

    <!-- ── Score distribution ─────────────────────────────────────────────── -->
    {#if d.score_distribution.length > 0}
      <section class="chart-card mt">
        <h2>Score Distribution</h2>
        <p class="section-note">Green bars ≥ threshold ({d.config.threshold}); red bars below. Colors update with simulator.</p>
        <Chart type="bar" data={scoreDistChartData(d, simThreshold)} options={scoreDistOptions} height="260px" />
      </section>
    {/if}

    <!-- ── Threshold simulator ────────────────────────────────────────────── -->
    {#if d.score_distribution.length > 0}
      {@const sim = simulatorResult(d, simThreshold)}
      <section class="chart-card mt">
        <h2>Threshold Simulator</h2>
        <p class="section-note">Slide to explore different thresholds. Read-only — does not change actual config.</p>
        <div class="sim-controls">
          <input
            type="range"
            min="0" max="1" step="0.05"
            bind:value={simThreshold}
            aria-label="Threshold simulator"
            class="sim-slider"
          />
          <span class="sim-value">{simThreshold.toFixed(2)}</span>
        </div>
        <p class="sim-result">
          At threshold <strong>{simThreshold.toFixed(2)}</strong>:
          <span class="admitted-text">{sim.admitted.toLocaleString()} admitted</span>,
          <span class="rejected-text">{sim.rejected.toLocaleString()} rejected</span>
          ({sim.pct}% rejection rate)
        </p>
      </section>
    {/if}

    <!-- ── Rejected facts table ───────────────────────────────────────────── -->
    <section class="chart-card mt">
      <h2>Rejected Facts</h2>
      <p class="section-note">Review this list to decide if the threshold is safe to enforce.</p>

      {#if rejectedLoading}
        <p class="status-msg">Loading…</p>
      {:else if rejectedError}
        <p class="status-msg error">
          {rejectedError} —
          <button class="retry-link" onclick={() => void loadRejected(rejectedOffset)}>retry</button>
        </p>
      {:else if rejectedData}
        {#if rejectedData.facts.length === 0 && rejectedOffset === 0}
          <p class="status-msg">No facts below threshold in this time window.</p>
        {:else}
          {@const rows = flattenFacts(rejectedData)}
          <DataTable
            columns={rejectedCols}
            {rows}
            rowKey={(r) => r.id}
          >
            {#snippet detail(row)}
              <div class="detail-content">{row.content_full}</div>
            {/snippet}
          </DataTable>

          <!-- Pagination -->
          <div class="pagination">
            <button
              class="page-btn"
              disabled={rejectedOffset === 0 || rejectedLoading}
              onclick={() => void loadRejected(rejectedOffset - PAGE_SIZE)}
            >Previous</button>
            <span class="page-info">
              {rejectedOffset + 1}–{Math.min(rejectedOffset + rejectedData.facts.length, rejectedData.total)}
              of {rejectedData.total}
            </span>
            <button
              class="page-btn"
              disabled={rejectedOffset + rejectedData.facts.length >= rejectedData.total || rejectedLoading}
              onclick={() => void loadRejected(rejectedOffset + PAGE_SIZE)}
            >Next</button>
          </div>
        {/if}
      {/if}
    </section>

    <!-- ── Dimension box plots ────────────────────────────────────────────── -->
    {#if Object.keys(d.dimension_stats).length > 1}
      <section class="chart-card mt">
        <h2>Per-Dimension Breakdown</h2>
        <p class="section-note">Score spread for admitted vs rejected facts. Excludes bypassed.</p>
        <div class="dim-grid">
          {#each DIMS as dim}
            {@const entry = d.dimension_stats[dim]}
            {#if entry && typeof entry !== 'string'}
              <div class="dim-card">
                <h3>{dim.replace('_', ' ')}</h3>
                {#each [{ label: 'Admitted', stats: entry.admitted, color: '#34d399' }, { label: 'Rejected', stats: entry.rejected, color: '#f87171' }] as { label, stats, color }}
                  <div class="bp-row">
                    <span class="bp-label">{label}</span>
                    {#if isBoxStats(stats)}
                      {@const maxVal = Math.max(1.0, stats.max)}
                      <div class="bp-track">
                        <div class="bp-whisker" style:left={pct(stats.min, maxVal)} style:width={pct(stats.max - stats.min, maxVal)} style:background="{color}30"></div>
                        <div class="bp-box"    style:left={pct(stats.q1, maxVal)}  style:width={pct(stats.q3 - stats.q1, maxVal)}   style:background="{color}80"></div>
                        <div class="bp-median" style:left={pct(stats.median, maxVal)} style:background={color}></div>
                      </div>
                      <span class="bp-vals">{stats.min.toFixed(2)} / {stats.median.toFixed(2)} / {stats.max.toFixed(2)}</span>
                    {:else}
                      <span class="bp-empty">No data</span>
                    {/if}
                  </div>
                {/each}
              </div>
            {/if}
          {/each}
        </div>
      </section>
    {/if}

    <!-- ── Charts grid ────────────────────────────────────────────────────── -->
    <div class="chart-grid mt">

      <!-- By source -->
      {#if Object.keys(d.by_source).length > 0}
        <section class="chart-card">
          <h2>By Source</h2>
          <Chart type="bar" data={bySourceChartData(d)} options={bySourceOptions} height="220px" />
        </section>
      {/if}

      <!-- By category -->
      {#if Object.keys(d.by_category).length > 0}
        <section class="chart-card">
          <h2>By Category</h2>
          <Chart type="bar" data={byCategoryChartData(d)} options={byCategoryOptions} height="220px" />
        </section>
      {/if}

      <!-- Trend: admission rate -->
      {#if d.daily_trend.length > 0}
        <section class="chart-card">
          <h2>Admission Rate Over Time</h2>
          <Chart type="line" data={trendRateChartData(d)} options={trendRateOptions} height="220px" />
        </section>
      {/if}

      <!-- Trend: avg score -->
      {#if d.daily_trend.length > 0}
        <section class="chart-card">
          <h2>Avg Score Over Time</h2>
          <Chart type="line" data={trendScoreChartData(d)} options={trendScoreOptions} height="220px" />
        </section>
      {/if}

      <!-- Bypass breakdown -->
      {#if Object.keys(d.bypass_breakdown).length > 0}
        <section class="chart-card">
          <h2>Bypass Breakdown</h2>
          <Chart type="doughnut" data={bypassChartData(d)} options={bypassOptions} height="220px" />
        </section>
      {/if}

    </div>

  {/if}

{:else if $store.error}
  <p class="status-msg error">
    Failed to load admission data —
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
    margin: 0 0 0.5rem;
  }

  h3 {
    font-size: 0.8125rem;
    font-weight: 600;
    color: var(--text);
    margin: 0 0 0.4rem;
    text-transform: capitalize;
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

  /* ── Banner ──────────────────────────────────────────────── */
  .banner {
    display: flex;
    align-items: flex-start;
    gap: 0.75rem;
    padding: 0.75rem 1rem;
    border-radius: 8px;
    border: 1px solid var(--border);
    margin-bottom: 1.25rem;
    font-size: 0.8125rem;
  }

  .banner-shadow  { background: rgba(251, 191, 36, 0.08); border-color: rgba(251, 191, 36, 0.3); }
  .banner-enforced { background: rgba(52, 211, 153, 0.06); border-color: rgba(52, 211, 153, 0.25); }

  .banner-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    margin-top: 0.2rem;
    flex-shrink: 0;
  }
  .banner-shadow  .banner-dot { background: #fbbf24; }
  .banner-enforced .banner-dot { background: #34d399; }

  .banner-body { color: var(--text); }

  .banner-meta {
    display: block;
    margin-top: 0.25rem;
    color: var(--muted);
  }

  /* ── Section note ────────────────────────────────────────── */
  .section-note {
    font-size: 0.75rem;
    color: var(--muted);
    margin: 0 0 0.75rem;
  }

  /* ── Chart card ──────────────────────────────────────────── */
  .chart-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1rem;
  }

  .mt { margin-top: 1rem; }

  /* ── Chart grid ──────────────────────────────────────────── */
  .chart-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 1rem;
  }

  @media (max-width: 900px) {
    .chart-grid { grid-template-columns: 1fr; }
  }

  /* ── Threshold simulator ─────────────────────────────────── */
  .sim-controls {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin-bottom: 0.5rem;
  }

  .sim-slider {
    flex: 1;
    accent-color: var(--accent, #7c6af7);
    cursor: pointer;
  }

  .sim-value {
    font-size: 1rem;
    font-weight: 700;
    color: var(--text);
    min-width: 2.5rem;
    text-align: right;
  }

  .sim-result {
    font-size: 0.875rem;
    color: var(--text);
    margin: 0;
  }

  .admitted-text { color: #34d399; font-weight: 600; }
  .rejected-text { color: #f87171; font-weight: 600; }

  /* ── Dimension box plots ─────────────────────────────────── */
  .dim-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 0.75rem;
  }

  .dim-card {
    background: var(--surface-hover, rgba(255,255,255,0.03));
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.75rem;
  }

  .bp-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.375rem;
    font-size: 0.75rem;
  }

  .bp-label {
    width: 4rem;
    flex-shrink: 0;
    color: var(--muted);
    font-size: 0.6875rem;
  }

  .bp-track {
    position: relative;
    flex: 1;
    height: 12px;
    background: var(--border);
    border-radius: 3px;
  }

  .bp-whisker,
  .bp-box,
  .bp-median {
    position: absolute;
    top: 0;
    height: 100%;
    border-radius: 2px;
  }

  .bp-median {
    width: 2px;
  }

  .bp-vals {
    font-size: 0.6875rem;
    color: var(--muted);
    white-space: nowrap;
  }

  .bp-empty {
    font-size: 0.6875rem;
    color: var(--muted);
    font-style: italic;
  }

  /* ── Rejected table pagination ───────────────────────────── */
  .pagination {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin-top: 0.75rem;
    font-size: 0.8125rem;
  }

  .page-btn {
    padding: 0.25rem 0.75rem;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm, 6px);
    background: var(--surface);
    color: var(--text);
    cursor: pointer;
    font-size: 0.8125rem;
  }
  .page-btn:hover:not(:disabled) { background: var(--surface-hover); }
  .page-btn:disabled { opacity: 0.4; cursor: default; }

  .page-info { color: var(--muted); }

  /* ── Detail row content ──────────────────────────────────── */
  .detail-content {
    font-size: 0.8125rem;
    color: var(--text);
    white-space: pre-wrap;
    word-break: break-word;
    padding: 0.25rem 0;
  }

  /* ── Status / empty messages ─────────────────────────────── */
  .empty-state {
    color: var(--muted);
    font-size: 0.9375rem;
    padding: 3rem 2rem;
    text-align: center;
  }

  code {
    background: var(--surface-hover);
    padding: 0.1em 0.35em;
    border-radius: 3px;
    font-size: 0.875em;
  }

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
