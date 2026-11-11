<script lang="ts">
  import { apiGet } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import type { RubricData } from '../lib/types/api';
  import StatGrid from '../lib/ui/StatGrid.svelte';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';
  import Chart from '../lib/viz/Chart.svelte';
  import { Button } from '../lib/ui/button';
  import { lineLegendLabels, seriesToken, tint } from '../lib/chartColors';

  // Load-once (rubric.js has no polling)
  const store = usePoll(
    makePollStore<RubricData>(
      (signal) => apiGet<RubricData>('/dashboard/rubric', { signal }),
      0, // fetch-once (manual refresh only)
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

  /** The cell's tint: the status fill mixed with transparent at the old alpha (5% to 65%); the text stays the ink. */
  function correlationColor(r: number): string {
    const abs = Math.min(Math.abs(r), 1.0);
    const pct = Math.round((abs * 0.6 + 0.05) * 100);
    return `color-mix(in srgb, var(${r >= 0 ? '--green' : '--red'}) ${pct}%, transparent)`;
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

  // Chart colours are tokens that Chart.svelte resolves against the canvas's ground (dark: the old literals). The
  // first dimension and the radar take --series-0, not the accent, whose paper value is the question frame's blue.
  const DIM_TOKENS = ['--series-0', '--series-2', '--series-1', '--series-4', '--series-5', '--series-3', '--series-6', '--series-8', '--series-9'];
  const dimColor = (i: number) => (i < DIM_TOKENS.length ? `var(${DIM_TOKENS[i]})` : seriesToken(i));
  // A dash per line as well as a hue: some dimension hues merge under red-green colour blindness (tokens.test.ts), and
  // each such pair gets dashes of different kinds (solid, long dash, dotted, dash-dot).
  const DIM_DASHES = [[], [10, 4], [2, 3], [8, 3, 2, 3], [2, 2], [14, 5], [6, 2, 2, 2], [2, 4], [8, 3, 1, 3, 1, 3]];
  const dimDash = (i: number) => DIM_DASHES[i] ?? [i + 2, 3];

  // Reworked is --err-text, apart from corrected's orange under deuteranopia; self-corrected a mark of its own
  // (--series-3 was 4.2 from praised's blue under protanopia); an unknown type --series-7.
  const SIGNAL_TOKENS: Record<string, string> = {
    completed: '--oc-success',
    praised: '--series-1',
    corrected: '--series-4',
    reworked: '--err-text',
    self_corrected: '--signal-self-corrected',
  };
  // 12 px swatches: at phone width (300 px for the radar, the trend and the weights, 200 px for the doughnut) the default
  // 40 px ones took the legend to three or four rows and squeezed the plot (about 20 px tall in the old 200 px).
  const LEGEND_LABELS = { boxWidth: 12 };
  // Dashed lines (the weights, the radar's bounds) are told apart by their dash as well: the legend samples the line,
  // 28 px wide, with its dash (lib/chartColors.ts).
  const DASH_LEGEND_LABELS = lineLegendLabels();

  const signalToken = (t: string) => (Object.hasOwn(SIGNAL_TOKENS, t) ? SIGNAL_TOKENS[t] : '--series-7');

  function radarChartData(d: RubricData) {
    const dims = d.active_rubric!.dimensions;
    return {
      labels: dims.map((x) => x.name),
      datasets: [
        {
          label: 'Current Weight',
          data: dims.map((x) => x.weight),
          borderColor: 'var(--series-0)',
          backgroundColor: tint('--series-0', 0.2),
          pointBackgroundColor: 'var(--series-0)',
          borderWidth: 2,
        },
        {
          // Full-strength lines (3:1 or more on paper), dashes of different kinds: Min dotted, Max dashed.
          label: 'Min Bound',
          data: dims.map((x) => x.min_weight ?? 0.10),
          borderColor: 'var(--red)',
          backgroundColor: 'transparent',
          borderDash: [2, 3],
          borderWidth: 1,
          pointRadius: 0,
        },
        {
          label: 'Max Bound',
          data: dims.map((x) => x.max_weight ?? 0.40),
          borderColor: 'var(--green)',
          backgroundColor: 'transparent',
          borderDash: [8, 4],
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
        backgroundColor: types.map((t) => `var(${signalToken(t)})`),
        borderColor: 'var(--surface)',
        borderWidth: 2,
      }],
    };
  }

  function signalTrendData(d: RubricData) {
    const trend = d.outcome_signals.daily_trend;
    const line = (label: string, data: number[], type: string, fill: string) => {
      const token = signalToken(type);
      return { label, data, borderColor: `var(${token})`, backgroundColor: tint(token, 0.15), fill };
    };
    return {
      labels: trend.map((x) => x.date.slice(5)), // month-day: a phone keeps the labels level
      datasets: [
        line('Completed', trend.map((x) => x.completed), 'completed', 'origin'),
        line('Praised', trend.map((x) => x.praised), 'praised', '-1'),
        line('Corrected', trend.map((x) => x.corrected), 'corrected', '-1'),
        line('Reworked', trend.map((x) => x.reworked), 'reworked', '-1'),
        line('Self-corrected', trend.map((x) => x.self_corrected), 'self_corrected', '-1'),
      ],
    };
  }

  function weightHistoryData(d: RubricData) {
    // Oldest first
    const wh = d.weight_history.slice().reverse();
    const seen = new Set<string>();
    for (const v of wh) Object.keys(v.weights).forEach((k) => seen.add(k));
    // A dimension's hue and dash follow its name, not the history's order (the endpoint sends the newest 20 versions, so
    // the oldest one changes): the active rubric's dimensions first, then any name seen only in the history, sorted.
    const active = d.active_rubric?.dimensions.map((x) => x.name) ?? [];
    const order = [...active.filter((n) => seen.has(n)), ...[...seen].filter((n) => !active.includes(n)).sort()];
    return {
      labels: wh.map((v) => `v${v.version}`),
      datasets: order.map((dim, i) => ({
        label: dim,
        data: wh.map((v) => v.weights[dim] ?? null),
        borderColor: dimColor(i),
        borderDash: dimDash(i),
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
    <h1>Scoring</h1>
    <p class="subtitle">F024 Phase 3b — Evaluation dimensions, outcome signals, and evolution tracking</p>
  </div>
  <div class="head-actions">
    <Button variant="outline" size="lg" class="px-4 max-md:min-h-11" onclick={() => void store.refresh()} disabled={$store.loading}>
      {$store.loading ? 'Loading…' : 'Refresh'}
    </Button>
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
            phoneHeight="300px"
            options={{
              scales: {
                r: {
                  min: 0,
                  max: 0.5,
                  ticks: { stepSize: 0.1, showLabelBackdrop: false }, // the labels sit on the weights: no box hides them
                  grid: { color: 'var(--border)' },
                  angleLines: { color: 'var(--border)' },
                  pointLabels: { font: { size: 12 } },
                },
              },
              plugins: { legend: { display: true, position: 'bottom', labels: DASH_LEGEND_LABELS } },
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
            options={{ plugins: { legend: { position: 'bottom', labels: LEGEND_LABELS } } }}
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
          phoneHeight="300px"
          options={{
            plugins: { legend: { display: true, position: 'bottom', labels: LEGEND_LABELS } },
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
        <h2>Dimension and signal correlations</h2>
        <p class="section-note">
          No correlation data yet. Need {cfg.min_episodes_for_correlation}+ episodes with outcome signals before correlations are computed.
        </p>
      </section>
    {:else}
      {@const { dims, sigTypes, matrix } = buildCorrMatrix(d)}
      <section class="chart-card mt">
        <h2>Dimension and signal correlations</h2>
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
                        style:--heat={correlationColor(entry.pearson_r)}
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
          phoneHeight="300px"
          options={{
            plugins: { legend: { display: true, position: 'bottom', labels: DASH_LEGEND_LABELS } },
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
    font-size: 30px;
    font-weight: 650;
    letter-spacing: -0.022em;
    line-height: 1.15;
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

  /* ── States ──────────────────────────────────────────────────── */
  .status-msg {
    text-align: center;
    padding: 3rem 1rem;
    color: var(--muted);
  }

  .status-msg.error {
    color: var(--err-text);
  }

  .retry-link {
    background: none;
    border: none;
    color: var(--accent);
    cursor: pointer;
    text-decoration: underline;
    font-size: inherit;
  }

  .empty-state {
    padding: 3rem 1rem;
    text-align: center;
    color: var(--muted);
    background: var(--surface);
    border: var(--card-border);
    border-radius: var(--card-radius);
    box-shadow: var(--card-shadow);
  }

  .empty-state code {
    font-family: var(--font-mono);
    background: var(--track);
    padding: 0.125rem 0.375rem;
    border-radius: 4px;
  }

  /* ── Config banner ───────────────────────────────────────────── */
  .config-banner {
    display: flex;
    align-items: flex-start;
    gap: 0.75rem;
    padding: 0.875rem 1rem;
    border-radius: var(--card-radius);
    border: 1px solid var(--border);
    margin-bottom: 1.25rem;
    font-size: 0.875rem;
  }

  .config-banner--active {
    background: color-mix(in srgb, var(--green) 6%, transparent);
    border-color: color-mix(in srgb, var(--green) 30%, transparent);
  }

  .config-banner--off {
    background: var(--wash);
  }

  .banner-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    flex-shrink: 0;
    margin-top: 3px;
  }

  .dot-active { background: var(--green); }
  .dot-off    { background: var(--st-neutral); }

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
    border: var(--card-border);
    border-radius: var(--card-radius);
    box-shadow: var(--card-shadow);
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

  /* A grid cell may shrink below the radar canvas's width, so a phone never scrolls sideways. */
  .dim-grid > * {
    min-width: 0;
  }

  @media (max-width: 768px) {
    .dim-grid {
      grid-template-columns: 1fr;
    }
  }

  .radar-wrap {
    min-height: 320px;
  }

  /* After the rule above: the radar asks for 300 px here (its phoneHeight), so the cell does not reserve 320 px around it. */
  @media (max-width: 768px) {
    .radar-wrap {
      min-height: 0;
    }
  }

  .dim-cards {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
    gap: 0.75rem;
  }

  .dim-card {
    background: var(--bg);
    border: var(--card-border);
    border-radius: var(--card-radius-sm);
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
    background: var(--bg);
    color: var(--muted);
    font-weight: 600;
    text-transform: capitalize;
  }

  .corr-table .dim-label {
    text-align: left;
    color: var(--text);
    font-weight: 500;
  }

  /* --heat is the cell's tint, set per cell (correlationColor) */
  .corr-cell {
    font-weight: 600;
    color: var(--text);
    background: var(--heat, transparent);
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
    text-transform: none;
    letter-spacing: 0;
  }

  .data-table tbody tr:hover {
    background: var(--wash);
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

  /* Text tokens on their tints; a pending or unknown status is neutral. The partial tint is PR 3's badge tint (12.5%):
     at 15% the paper text is 4.49:1 on a hovered row. */
  .badge-success { background: var(--badge-good-bg); color: var(--ok-text); }
  .badge-failure { background: var(--badge-bad-bg); color: var(--err-text); }
  .badge-partial { background: color-mix(in srgb, var(--st-partial) 12.5%, transparent); color: var(--st-partial); }
  .badge-pending { background: var(--wash); color: var(--muted); }
</style>
