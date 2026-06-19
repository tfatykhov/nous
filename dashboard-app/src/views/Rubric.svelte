<script lang="ts">
  import { apiGet } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import type { RubricData } from '../lib/types/api';
  import StatGrid from '../lib/ui/StatGrid.svelte';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';
  import Chart from '../lib/viz/Chart.svelte';

  // Load-once (rubric.js has no polling)
  const store = usePoll(
    makePollStore<RubricData>(
      (signal) => apiGet<RubricData>('/dashboard/rubric', { signal }),
      Number.MAX_SAFE_INTEGER,
    ),
  );

  // ── Formatters ────────────────────────────────────────────────────────────

  function fmtDate(iso: string | null | undefined): string {
    if (!iso) return '—';
    return new Date(iso).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' });
  }

  function fmtPct(n: number): string {
    return (n * 100).toFixed(0) + '%';
  }

  // ── Correlation heatmap helpers ───────────────────────────────────────────

  function correlationColor(r: number): string {
    const abs = Math.min(Math.abs(r), 1.0);
    const alpha = (abs * 0.6 + 0.05).toFixed(2);
    if (r >= 0) return `rgba(52, 211, 153, ${alpha})`;
    return `rgba(248, 113, 113, ${alpha})`;
  }

  /** Build {dims, sigTypes, matrix} from flat correlations array. */
  function buildCorrMatrix(data: RubricData) {
    const dims: string[] = [];
    const sigTypes: string[] = [];
    const matrix: Record<string, { pearson_r: number; spearman_rho: number }> = {};
    for (const c of data.correlations.data) {
      if (!dims.includes(c.dimension)) dims.push(c.dimension);
      if (!sigTypes.includes(c.signal_type)) sigTypes.push(c.signal_type);
      matrix[`${c.dimension}|${c.signal_type}`] = { pearson_r: c.pearson_r, spearman_rho: c.spearman_rho };
    }
    return { dims, sigTypes, matrix };
  }

  // ── Chart data builders ───────────────────────────────────────────────────

  const DIM_COLORS = ['#7c6af7', '#34d399', '#60a5fa', '#fb923c', '#f87171', '#a78bfa', '#fbbf24'];

  const SIGNAL_COLORS: Record<string, string> = {
    completed: '#34d399',
    praised: '#60a5fa',
    corrected: '#fb923c',
    reworked: '#f87171',
    self_corrected: '#a78bfa',
  };

  function radarChartData(d: RubricData) {
    const dims = d.active_rubric!.dimensions;
    return {
      labels: dims.map((x) => x.name),
      datasets: [
        {
          label: 'Current Weight',
          data: dims.map((x) => x.weight),
          borderColor: '#7c6af7',
          backgroundColor: 'rgba(124, 106, 247, 0.2)',
          pointBackgroundColor: '#7c6af7',
          borderWidth: 2,
        },
        {
          label: 'Min Bound',
          data: dims.map((x) => x.min_weight ?? 0.10),
          borderColor: 'rgba(248, 113, 113, 0.4)',
          backgroundColor: 'transparent',
          borderDash: [4, 4],
          borderWidth: 1,
          pointRadius: 0,
        },
        {
          label: 'Max Bound',
          data: dims.map((x) => x.max_weight ?? 0.40),
          borderColor: 'rgba(52, 211, 153, 0.4)',
          backgroundColor: 'transparent',
          borderDash: [4, 4],
          borderWidth: 1,
          pointRadius: 0,
        },
      ],
    };
  }

  function signalDoughnutData(byType: Record<string, number>) {
    const types = Object.keys(byType);
    return {
      labels: types.map((t) => t.replace('_', ' ')),
      datasets: [{
        data: types.map((t) => byType[t]),
        backgroundColor: types.map((t) => SIGNAL_COLORS[t] ?? '#6b6b8a'),
        borderColor: '#111118',
        borderWidth: 2,
      }],
    };
  }

  function signalTrendData(d: RubricData) {
    const trend = d.outcome_signals.daily_trend;
    return {
      labels: trend.map((x) => x.date),
      datasets: [
        { label: 'Completed', data: trend.map((x) => x.completed), borderColor: '#34d399', backgroundColor: 'rgba(52,211,153,0.15)', fill: 'origin' },
        { label: 'Praised', data: trend.map((x) => x.praised), borderColor: '#60a5fa', backgroundColor: 'rgba(96,165,250,0.15)', fill: '-1' },
        { label: 'Corrected', data: trend.map((x) => x.corrected), borderColor: '#fb923c', backgroundColor: 'rgba(251,146,60,0.15)', fill: '-1' },
        { label: 'Reworked', data: trend.map((x) => x.reworked), borderColor: '#f87171', backgroundColor: 'rgba(248,113,113,0.15)', fill: '-1' },
        { label: 'Self-corrected', data: trend.map((x) => x.self_corrected), borderColor: '#a78bfa', backgroundColor: 'rgba(167,139,250,0.15)', fill: '-1' },
      ],
    };
  }

  function weightHistoryData(d: RubricData) {
    // Oldest first
    const wh = d.weight_history.slice().reverse();
    const allDims = new Set<string>();
    for (const v of wh) Object.keys(v.weights).forEach((k) => allDims.add(k));
    const dimList = Array.from(allDims);
    return {
      labels: wh.map((v) => `v${v.version}`),
      datasets: dimList.map((dim, i) => ({
        label: dim,
        data: wh.map((v) => v.weights[dim] ?? null),
        borderColor: DIM_COLORS[i % DIM_COLORS.length],
        backgroundColor: 'transparent',
        spanGaps: true,
      })),
    };
  }

  // ── Signal trend guard ─────────────────────────────────────────────────────

  function hasTrendData(d: RubricData): boolean {
    return d.outcome_signals.daily_trend.some(
      (x) => x.completed + x.corrected + x.praised + x.reworked + x.self_corrected > 0,
    );
  }

  // ── Badge helpers ─────────────────────────────────────────────────────────

  const SIGNAL_BADGE: Record<string, string> = {
    completed: 'badge-success',
    praised: 'badge-success',
    corrected: 'badge-partial',
    reworked: 'badge-failure',
    self_corrected: 'badge-pending',
  };

  const STATUS_BADGE: Record<string, string> = {
    active: 'badge-success',
    rollback: 'badge-failure',
  };
</script>

<!-- ── View header ───────────────────────────────────────────────────── -->
<header class="view-head">
  <div>
    <h1>Self-Modifying Rubric</h1>
    <p class="subtitle">F024 Phase 3b — Evaluation dimensions, outcome signals, and evolution tracking</p>
  </div>
  <div class="head-actions">
    <button class="refresh-btn" onclick={() => void store.refresh()} disabled={$store.loading}>
      {$store.loading ? 'Loading…' : 'Refresh'}
    </button>
    <StaleBadge state={$store} />
  </div>
</header>

<!-- ── Loading / error states ───────────────────────────────────────── -->
{#if $store.loading && !$store.data}
  <p class="status-msg">Loading rubric data…</p>
{:else if $store.error}
  <p class="status-msg error">Failed to load rubric data. <button class="retry-link" onclick={() => void store.refresh()}>Retry</button></p>
{:else if $store.data}
  {@const d = $store.data}
  {@const cfg = d.config}

  {#if !d.active_rubric}
    <!-- ── No rubric configured ── -->
    <div class="empty-state">
      <p>No active rubric — ensure <code>NOUS_RUBRIC_ENABLED=true</code> and the rubric has been seeded.</p>
    </div>
  {:else}
    {@const rubric = d.active_rubric}

    <!-- ── Config banner ──────────────────────────────────────────── -->
    <div class="config-banner" class:config-banner--active={cfg.evolution_enabled} class:config-banner--off={!cfg.evolution_enabled}>
      <span class="banner-dot" class:dot-active={cfg.evolution_enabled} class:dot-off={!cfg.evolution_enabled}></span>
      <div class="banner-text">
        <strong>Rubric v{rubric.version}</strong> — {rubric.dimension_count} dimensions
        {cfg.evolution_enabled ? ' | Evolution ACTIVE' : ' | Evolution OFF (observation mode)'}
        <div class="banner-stats">
          Outcome detection: {cfg.outcome_detection_enabled ? 'ON' : 'OFF'} |
          Signals collected: {d.outcome_signals.total.toLocaleString()} |
          Min episodes for correlation: {cfg.min_episodes_for_correlation} |
          Weight cap: ±{(cfg.weight_change_cap * 100).toFixed(0)}%
        </div>
      </div>
    </div>

    <!-- ── Stat grid ──────────────────────────────────────────────── -->
    <StatGrid stats={[
      { label: 'Rubric Version', value: rubric.version },
      { label: 'Dimensions', value: rubric.dimension_count },
      { label: 'Total Signals', value: d.outcome_signals.total.toLocaleString() },
      { label: 'Versions', value: d.version_history.length },
    ]} />

    <!-- ── Current Dimensions & Weights ──────────────────────────── -->
    <section class="chart-card mt">
      <h2>Current Dimensions &amp; Weights</h2>
      <div class="dim-grid">
        <div class="radar-wrap">
          <Chart
            type="radar"
            data={radarChartData(d)}
            height="320px"
            options={{
              scales: {
                r: {
                  min: 0,
                  max: 0.5,
                  ticks: { stepSize: 0.1 },
                  grid: { color: '#1e1e2e' },
                  angleLines: { color: '#1e1e2e' },
                  pointLabels: { font: { size: 12 } },
                },
              },
              plugins: { legend: { display: true, position: 'bottom' } },
            }}
          />
        </div>
        <div class="dim-cards">
          {#each rubric.dimensions as dim}
            <div class="dim-card">
              <div class="dim-weight">{fmtPct(dim.weight)}</div>
              <div class="dim-name">{dim.name}</div>
              <div class="dim-desc">{dim.description}</div>
            </div>
          {/each}
        </div>
      </div>
    </section>

    <!-- ── Outcome Signal Distribution ───────────────────────────── -->
    {#if Object.keys(d.outcome_signals.by_type).length > 0}
      <section class="chart-card mt">
        <h2>Outcome Signal Distribution</h2>
        <p class="section-note">Total: {d.outcome_signals.total.toLocaleString()} signals detected from episodes</p>
        <div class="doughnut-wrap">
          <Chart
            type="doughnut"
            data={signalDoughnutData(d.outcome_signals.by_type)}
            height="280px"
            options={{ plugins: { legend: { position: 'bottom' } } }}
          />
        </div>
      </section>
    {/if}

    <!-- ── Signal Trend (30 days) ─────────────────────────────────── -->
    {#if hasTrendData(d)}
      <section class="chart-card mt">
        <h2>Signal Trend (30 Days)</h2>
        <Chart
          type="line"
          data={signalTrendData(d)}
          height="280px"
          options={{
            plugins: { legend: { display: true, position: 'bottom' } },
            scales: {
              x: { title: { display: true, text: 'Date' } },
              y: { title: { display: true, text: 'Signals' }, beginAtZero: true, stacked: true },
            },
          }}
        />
      </section>
    {/if}

    <!-- ── Correlation Heatmap ────────────────────────────────────── -->
    {#if d.correlations.data.length === 0}
      <section class="chart-card mt">
        <h2>Dimension ↔ Signal Correlations</h2>
        <p class="section-note">
          No correlation data yet. Need {cfg.min_episodes_for_correlation}+ episodes with outcome signals before correlations are computed.
        </p>
      </section>
    {:else}
      {@const { dims, sigTypes, matrix } = buildCorrMatrix(d)}
      <section class="chart-card mt">
        <h2>Dimension ↔ Signal Correlations</h2>
        <p class="section-note">Pearson r (Spearman ρ in tooltip). Sample size: {d.correlations.sample_size} episodes.</p>
        <div class="table-scroll">
          <table class="corr-table">
            <thead>
              <tr>
                <th>Dimension</th>
                {#each sigTypes as sig}
                  <th>{sig.replace('_', ' ')}</th>
                {/each}
              </tr>
            </thead>
            <tbody>
              {#each dims as dim}
                <tr>
                  <td class="dim-label">{dim}</td>
                  {#each sigTypes as sig}
                    {@const entry = matrix[`${dim}|${sig}`]}
                    {#if entry}
                      <td
                        class="corr-cell"
                        style="background:{correlationColor(entry.pearson_r)}"
                        title="Pearson r={entry.pearson_r.toFixed(3)}, Spearman ρ={entry.spearman_rho.toFixed(3)}"
                      >{entry.pearson_r.toFixed(2)}</td>
                    {:else}
                      <td class="corr-cell corr-empty">—</td>
                    {/if}
                  {/each}
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </section>
    {/if}

    <!-- ── Weight Evolution ───────────────────────────────────────── -->
    {#if d.weight_history.length >= 2}
      <section class="chart-card mt">
        <h2>Weight Evolution</h2>
        <p class="section-note">How dimension weights have changed across rubric versions</p>
        <Chart
          type="line"
          data={weightHistoryData(d)}
          height="280px"
          options={{
            plugins: { legend: { display: true, position: 'bottom' } },
            scales: {
              x: { title: { display: true, text: 'Version' } },
              y: { title: { display: true, text: 'Weight' }, min: 0, max: 0.5 },
            },
          }}
        />
      </section>
    {/if}

    <!-- ── Version History ───────────────────────────────────────── -->
    {#if d.version_history.length > 0}
      <section class="chart-card mt">
        <h2>Version History</h2>
        <div class="table-scroll">
          <table class="data-table">
            <thead>
              <tr>
                <th>Version</th>
                <th>Status</th>
                <th>Dimensions</th>
                <th>Change Reason</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {#each d.version_history as v}
                <tr>
                  <td><strong>{v.version}</strong></td>
                  <td><span class="badge {STATUS_BADGE[v.status] ?? 'badge-pending'}">{v.status}</span></td>
                  <td>{v.dimension_count}</td>
                  <td class="content-cell">{v.change_reason ?? '—'}</td>
                  <td>{fmtDate(v.created_at)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </section>
    {/if}

    <!-- ── Recent Outcome Signals ─────────────────────────────────── -->
    {#if d.outcome_signals.recent.length > 0}
      <section class="chart-card mt">
        <h2>Recent Outcome Signals</h2>
        <div class="table-scroll">
          <table class="data-table">
            <thead>
              <tr>
                <th>Type</th>
                <th>Confidence</th>
                <th>Evidence</th>
                <th>Detected</th>
              </tr>
            </thead>
            <tbody>
              {#each d.outcome_signals.recent as s}
                <tr>
                  <td><span class="badge {SIGNAL_BADGE[s.signal_type] ?? 'badge-pending'}">{s.signal_type.replace('_', ' ')}</span></td>
                  <td>{(s.confidence * 100).toFixed(0)}%</td>
                  <td class="content-cell">{s.evidence ? s.evidence.slice(0, 120) : '—'}</td>
                  <td>{fmtDate(s.created_at)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </section>
    {/if}
  {/if}
{/if}

<style>
  /* ── Header ──────────────────────────────────────────────────── */
  .view-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 1.5rem;
    flex-wrap: wrap;
  }

  .view-head h1 {
    margin: 0 0 0.25rem;
    font-size: 1.5rem;
    font-weight: 700;
    color: var(--text);
  }

  .subtitle {
    margin: 0;
    color: var(--muted);
    font-size: 0.875rem;
  }

  .head-actions {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    flex-shrink: 0;
  }

  .refresh-btn {
    padding: 0.375rem 0.875rem;
    border-radius: var(--radius-sm, 6px);
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text);
    font-size: 0.8125rem;
    cursor: pointer;
  }

  .refresh-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  /* ── States ──────────────────────────────────────────────────── */
  .status-msg {
    text-align: center;
    padding: 3rem 1rem;
    color: var(--muted);
  }

  .status-msg.error {
    color: #f87171;
  }

  .retry-link {
    background: none;
    border: none;
    color: var(--accent, #7c6af7);
    cursor: pointer;
    text-decoration: underline;
    font-size: inherit;
  }

  .empty-state {
    padding: 3rem 1rem;
    text-align: center;
    color: var(--muted);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm, 8px);
  }

  .empty-state code {
    font-family: monospace;
    background: rgba(255,255,255,0.08);
    padding: 0.125rem 0.375rem;
    border-radius: 4px;
  }

  /* ── Config banner ───────────────────────────────────────────── */
  .config-banner {
    display: flex;
    align-items: flex-start;
    gap: 0.75rem;
    padding: 0.875rem 1rem;
    border-radius: var(--radius-sm, 8px);
    border: 1px solid var(--border);
    margin-bottom: 1.25rem;
    font-size: 0.875rem;
  }

  .config-banner--active {
    background: rgba(52, 211, 153, 0.06);
    border-color: rgba(52, 211, 153, 0.3);
  }

  .config-banner--off {
    background: rgba(255, 255, 255, 0.03);
  }

  .banner-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    flex-shrink: 0;
    margin-top: 3px;
  }

  .dot-active { background: #34d399; }
  .dot-off    { background: #6b7280; }

  .banner-text {
    color: var(--text);
    line-height: 1.5;
  }

  .banner-stats {
    color: var(--muted);
    font-size: 0.8125rem;
    margin-top: 0.25rem;
  }

  /* ── Sections ────────────────────────────────────────────────── */
  .mt { margin-top: 1.25rem; }

  .chart-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm, 8px);
    padding: 1.25rem;
  }

  .chart-card h2 {
    margin: 0 0 0.5rem;
    font-size: 1rem;
    font-weight: 600;
    color: var(--text);
  }

  .section-note {
    margin: 0 0 1rem;
    color: var(--muted);
    font-size: 0.8125rem;
  }

  /* ── Radar + dimension cards ─────────────────────────────────── */
  .dim-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1.5rem;
    align-items: start;
  }

  @media (max-width: 768px) {
    .dim-grid {
      grid-template-columns: 1fr;
    }
  }

  .radar-wrap {
    min-height: 320px;
  }

  .dim-cards {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
    gap: 0.75rem;
  }

  .dim-card {
    background: var(--bg, #0a0a12);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm, 6px);
    padding: 0.75rem;
  }

  .dim-weight {
    font-size: 1.25rem;
    font-weight: 700;
    color: var(--text);
  }

  .dim-name {
    font-size: 0.8125rem;
    font-weight: 600;
    color: var(--text);
    margin-top: 0.125rem;
  }

  .dim-desc {
    font-size: 0.75rem;
    color: var(--muted);
    margin-top: 0.25rem;
    line-height: 1.4;
  }

  /* ── Doughnut ────────────────────────────────────────────────── */
  .doughnut-wrap {
    max-width: 500px;
  }

  /* ── Correlation heatmap table ───────────────────────────────── */
  .table-scroll {
    overflow-x: auto;
  }

  .corr-table {
    border-collapse: collapse;
    width: 100%;
    font-size: 0.8125rem;
  }

  .corr-table th,
  .corr-table td {
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--border);
    text-align: center;
    white-space: nowrap;
  }

  .corr-table th {
    background: var(--bg, #0a0a12);
    color: var(--muted);
    font-weight: 600;
    text-transform: capitalize;
  }

  .corr-table .dim-label {
    text-align: left;
    color: var(--text);
    font-weight: 500;
  }

  .corr-cell {
    font-weight: 600;
    color: var(--text);
    cursor: default;
  }

  .corr-empty {
    color: var(--muted);
    background: transparent !important;
  }

  /* ── Version history / recent signals tables ─────────────────── */
  .data-table {
    border-collapse: collapse;
    width: 100%;
    font-size: 0.8125rem;
  }

  .data-table th,
  .data-table td {
    padding: 0.5rem 0.75rem;
    border-bottom: 1px solid var(--border);
    text-align: left;
    vertical-align: middle;
  }

  .data-table th {
    color: var(--muted);
    font-weight: 600;
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .data-table tbody tr:hover {
    background: rgba(255, 255, 255, 0.02);
  }

  .content-cell {
    max-width: 320px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* ── Badges ──────────────────────────────────────────────────── */
  .badge {
    display: inline-block;
    padding: 0.15rem 0.5rem;
    border-radius: 9999px;
    font-size: 0.7rem;
    font-weight: 600;
    text-transform: capitalize;
    white-space: nowrap;
  }

  .badge-success { background: rgba(52, 211, 153, 0.15); color: #34d399; }
  .badge-failure { background: rgba(248, 113, 113, 0.15); color: #f87171; }
  .badge-partial { background: rgba(251, 146, 60, 0.15); color: #fb923c; }
  .badge-pending { background: rgba(167, 139, 250, 0.15); color: #a78bfa; }
</style>
