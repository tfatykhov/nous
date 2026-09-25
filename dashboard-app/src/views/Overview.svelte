<script lang="ts">
  import { apiGet } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import type { StatusData, StatusTimeseriesPoint, ExecutionData } from '../lib/types/api';
  import { attentionPoll } from '../lib/stores/attention';
  import { fmtUtc, relUntil } from '../lib/harness';
  import StatGrid from '../lib/ui/StatGrid.svelte';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';
  import Chart from '../lib/viz/Chart.svelte';

  // Load once: interval so large it never auto-fires (~285k years).
  // StaleBadge still gets lastUpdated from the initial tick.
  const store = usePoll(
    makePollStore<StatusData>(
      (signal) => apiGet<StatusData>('/status?dashboard=true', { signal }),
      0, // fetch-once (manual refresh only)
    ),
  );

  // Harness dashboard §4: the durable ledger's 24 h numbers (fetch-once, like
  // the rest of the Overview). The attention strip reads the app-wide poll.
  const exec = usePoll(
    makePollStore<ExecutionData>(
      (signal) => apiGet<ExecutionData>('/dashboard/execution?window=24h&limit=1', { signal }),
      0,
    ),
  );

  function refreshAll() {
    void store.refresh();
    void exec.refresh();
    void attentionPoll.refresh();
  }

  // ── Helpers ───────────────────────────────────────────────────────────

  function fmt(n: number): string {
    return n.toLocaleString();
  }

  function fmtWithDelta(value: number, delta: number | null | undefined): string {
    if (delta == null) return fmt(value);
    if (delta === 0) return fmt(value);
    const sign = delta > 0 ? '+' : '';
    return `${fmt(value)} (${sign}${delta} 7d)`;
  }

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

  function densityClass(d: number | null): 'good' | 'warn' | 'bad' {
    if (d == null) return 'warn';
    if (d >= 3.0) return 'good';
    if (d < 1.0) return 'bad';
    return 'warn';
  }

  function brierClass(b: number | null): 'good' | 'warn' | 'bad' | 'neutral' {
    if (b == null) return 'neutral';
    if (b <= 0.15) return 'good';
    if (b <= 0.25) return 'warn';
    return 'bad';
  }

  const DOUGHNUT_COLORS = ['#60a5fa', '#34d399', '#a78bfa', '#fb923c', '#f87171', '#fbbf24', '#6b6b8a', '#e2e2f0'];
  const OUTCOME_COLORS: Record<string, string> = {
    success: '#34d399', partial: '#fbbf24', failure: '#f87171', pending: '#6b6b8a',
  };
</script>

<header class="view-head">
  <div>
    <h1>Overview</h1>
    <p class="subtitle">At-a-glance memory health and trends</p>
  </div>
  <div class="head-right">
    <button class="refresh-btn" onclick={refreshAll} disabled={$store.loading}>
      {$store.loading ? 'Loading…' : 'Refresh'}
    </button>
    <StaleBadge state={$store} />
  </div>
</header>

{#if $store.data}
  {@const d = $store.data}
  {@const mem = d.memory}
  {@const db = d.dashboard}
  {@const deltas = db.deltas}
  {@const dist = db.distributions}
  {@const ts = normalizeTimeseries(db.timeseries)}

  <!-- ── Needs your attention (harness dashboard §4.6) ─────────────────── -->
  {@const a = $attentionPoll.data}
  {#if a}
    <section class="attention" aria-labelledby="attn-title">
      <h2 id="attn-title">Needs your attention</h2>
      {#if a.questions_waiting === 0 && a.sends_in_doubt === 0}
        <p class="all-clear">
          <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="#10b981" stroke-width="2" aria-hidden="true"><path d="M4 10.5l4 4 8-9"/></svg>
          Nothing needs you — no questions waiting and no sends in doubt.
        </p>
      {:else}
        <div class="attn-cards">
          {#if a.questions_waiting > 0}
            <a class="attn-card attn-waiting" href="#/dag" aria-labelledby="attn-q">
              <span class="attn-kicker">Questions</span>
              <span id="attn-q" class="attn-title">{a.questions_waiting} question{a.questions_waiting === 1 ? '' : 's'} waiting on you</span>
              {#if a.next}
                <span class="attn-detail">Next deadline {fmtUtc(a.next.deadline)} ({relUntil(a.next.deadline)}) — {a.next.dag_name} · {a.next.node_name}. No answer by then applies ‘{a.next.default_label}’.</span>
              {/if}
              <span class="attn-cta">See questions ›</span>
            </a>
          {/if}
          {#if a.sends_in_doubt > 0}
            <a class="attn-card attn-unknown" href="#/execution" aria-labelledby="attn-s">
              <span class="attn-kicker">Sends in doubt</span>
              <span id="attn-s" class="attn-title">{a.sends_in_doubt} send{a.sends_in_doubt === 1 ? '' : 's'} ended without confirming delivery</span>
              {#if a.latest_in_doubt}
                <span class="attn-detail">
                  {a.latest_in_doubt.tool_name}{a.latest_in_doubt.tombstone ? '' : a.latest_in_doubt.recipients.length ? ` to ${a.latest_in_doubt.recipients.join(', ')}` : ''}{a.latest_in_doubt.dag_name ? ` · ${a.latest_in_doubt.dag_name}` : ''} · {fmtUtc(a.latest_in_doubt.created_at)}. Retries of it are held until you check.
                </span>
              {/if}
              <span class="attn-cta">Review in ledger ›</span>
            </a>
          {/if}
        </div>
      {/if}
      <p class="harness-line">
        {#if !a.harness.events_persisted}
          Harness: not measured — event persistence is off.
        {:else}
          Harness, last 7 days:
          {#each [['Offered-tool rule', a.harness.offered_set], ['Context policy', a.harness.context_policy]] as [name, r], i (name)}
            {@const rule = r as { mode: string | null; warn_7d: number; refused_7d: number }}
            {i ? ' · ' : ''}{name}
            {#if rule.mode === 'enforce'} refused <strong>{rule.refused_7d}</strong>
            {:else if rule.mode === 'off'} is off
            {:else} flagged <strong>{rule.warn_7d}</strong> (warn — the calls still ran){/if}
          {/each}.
        {/if}
        <a href="#/harness">Open Harness</a>
      </p>
    </section>
  {/if}

  <!-- ── Stat cards ───────────────────────────────────────────────────── -->
  <StatGrid stats={[
    { label: 'Facts',            value: fmtWithDelta(mem.total_facts,      deltas.facts.last_7_days) },
    { label: 'Episodes',         value: fmtWithDelta(mem.total_episodes,   deltas.episodes.last_7_days) },
    { label: 'Chunks',           value: fmt(mem.total_chunks) },
    { label: 'Decisions',        value: fmtWithDelta(mem.total_decisions,  deltas.decisions.last_7_days) },
    { label: 'Procedures',       value: fmtWithDelta(mem.total_procedures, deltas.procedures.last_7_days) },
    { label: 'Active censors',   value: fmt(mem.active_censors) },
    { label: 'Active sessions',  value: fmt(mem.active_conversations) },
  ]} />

  <!-- Special cards: graph density + Brier score (color-coded) -->
  <div class="special-cards">
    <div class="stat-card">
      <div class="stat-value">
        <span class="indicator {densityClass(db.graph_density)}"></span>
        {db.graph_density != null ? db.graph_density.toFixed(1) : '—'}
      </div>
      <div class="stat-label">Graph density</div>
    </div>
    <div class="stat-card">
      <div class="stat-value">
        <span class="indicator {brierClass(d.calibration.brier_score)}"></span>
        {d.calibration.brier_score != null ? d.calibration.brier_score.toFixed(3) : '—'}
      </div>
      <div class="stat-label">Brier score</div>
    </div>
  </div>

  <!-- ── Execution integrity: the durable ledger (harness dashboard §2) ── -->
  {@const x = $exec.data}
  <section class="chart-card integrity-section">
    <div class="section-title-row">
      <h2>Execution integrity</h2>
      <a class="detail-link" href="#/execution">Open ledger &rsaquo;</a>
    </div>
    {#if x}
      <StatGrid stats={[
        { label: 'Calls (24 h)',          value: x.stats.calls },
        { label: 'Sends',                 value: x.stats.sends },
        { label: 'Repeat sends refused',  value: x.stats.repeat_sends_refused },
        { label: 'Claim checks',          value: x.modes.claim_verification },
        { label: 'Context policy',        value: x.modes.context_policy },
        { label: 'Offered-tool rule',     value: x.modes.offered_set },
      ]} />
    {:else if $exec.error}
      <p class="muted">Ledger unavailable.</p>
    {/if}
  </section>

  <!-- ── Charts ───────────────────────────────────────────────────────── -->
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
              { label: 'Facts',     data: ts.facts,     borderColor: '#60a5fa', backgroundColor: 'rgba(96,165,250,0.10)', fill: true, borderWidth: 2, pointRadius: 1 },
              { label: 'Episodes',  data: ts.episodes,  borderColor: '#34d399', backgroundColor: 'rgba(52,211,153,0.10)', fill: true, borderWidth: 2, pointRadius: 1 },
              { label: 'Decisions', data: ts.decisions, borderColor: '#a78bfa', backgroundColor: 'rgba(167,139,250,0.10)', fill: true, borderWidth: 2, pointRadius: 1 },
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
            datasets: [{ data: fc.values, backgroundColor: DOUGHNUT_COLORS.slice(0, fc.labels.length), borderWidth: 0, hoverOffset: 4 }],
          }}
          options={{ cutout: '60%', plugins: { legend: { position: 'bottom', labels: { font: { size: 11 } } } } }}
          height="240px"
        />
      {:else}
        <p class="empty">No facts categorized yet</p>
      {/if}
    </section>
  </div>

  <div class="chart-row">
    <!-- Decision outcomes (doughnut) -->
    <section class="chart-card">
      <h2>Decision outcomes</h2>
      {#if Object.keys(dist.decision_outcomes).length > 0}
        {@const do_ = sortedDistribution(dist.decision_outcomes)}
        <Chart
          type="doughnut"
          data={{
            labels: do_.labels,
            datasets: [{
              data: do_.values,
              backgroundColor: do_.labels.map((l) => OUTCOME_COLORS[l.toLowerCase()] ?? '#6b6b8a'),
              borderWidth: 0,
              hoverOffset: 4,
            }],
          }}
          options={{ cutout: '60%', plugins: { legend: { position: 'bottom', labels: { font: { size: 11 } } } } }}
          height="240px"
        />
      {:else}
        <p class="empty">No decisions recorded yet</p>
      {/if}
    </section>

    <!-- Edge types (horizontal bar) -->
    <section class="chart-card">
      <h2>Edge types</h2>
      {#if Object.keys(dist.edge_relations).length > 0}
        {@const er = sortedDistribution(dist.edge_relations)}
        <Chart
          type="bar"
          data={{
            labels: er.labels.map((l) => l.replace(/_/g, ' ')),
            datasets: [{ data: er.values, backgroundColor: 'rgba(124,106,247,0.5)', borderColor: '#7c6af7', borderWidth: 1 }],
          }}
          options={{
            indexAxis: 'y',
            scales: {
              x: { beginAtZero: true, ticks: { precision: 0 } },
              y: { grid: { display: false } },
            },
            plugins: { legend: { display: false } },
          }}
          height="240px"
        />
      {:else}
        <p class="empty">No graph edges yet</p>
      {/if}
    </section>
  </div>

{:else if $store.error}
  <p class="status-msg error">Failed to load overview data — <button class="retry-link" onclick={() => void store.refresh()}>retry</button></p>
{:else}
  <p class="status-msg">Loading…</p>
{/if}

<style>
  /* ── Needs your attention (harness dashboard §4.6) ── */
  .attention { display: flex; flex-direction: column; gap: 0.75rem; margin-bottom: 1.25rem; }
  .attention h2 { margin: 0; font-size: 0.9375rem; font-weight: 600; }
  .all-clear { display: flex; align-items: center; gap: 0.625rem; margin: 0; padding: 1rem 1.125rem; border-radius: 8px;
    background: var(--surface); border: 1px solid var(--border); font-size: 0.875rem; }
  .attn-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 1rem; }
  .attn-card { display: flex; flex-direction: column; gap: 0.5rem; padding: 1.125rem; border-radius: 8px; background: var(--surface);
    border: 1px solid; text-decoration: none; color: var(--text); }
  .attn-card:focus-visible { outline: 2px solid var(--accent-text); outline-offset: 2px; }
  .attn-waiting { border-color: rgba(167, 139, 250, 0.35); }
  .attn-unknown { border-color: rgba(244, 114, 182, 0.35); }
  .attn-kicker { font-size: 0.75rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; }
  .attn-waiting .attn-kicker, .attn-waiting .attn-cta { color: var(--waiting); }
  .attn-unknown .attn-kicker, .attn-unknown .attn-cta { color: var(--unknown); }
  .attn-title { font-size: 1.125rem; font-weight: 700; line-height: 1.3; }
  .attn-detail { font-size: 0.8125rem; color: var(--muted); }
  .attn-cta { font-size: 0.8125rem; font-weight: 600; margin-top: auto; }
  .harness-line { margin: 0; font-size: 0.8125rem; color: var(--muted); }
  .harness-line strong { color: var(--text); }
  .harness-line a { color: var(--accent-text); font-weight: 600; text-decoration: none; margin-left: 0.25rem; }

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

  /* Special indicator cards (density + Brier) */
  .special-cards {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 1rem;
    margin-top: 1rem;
  }

  .stat-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm, 8px);
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
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .indicator {
    display: inline-block;
    width: 0.625rem;
    height: 0.625rem;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .indicator.good    { background: var(--green, #22c55e); }
  .indicator.warn    { background: var(--yellow, #eab308); }
  .indicator.bad     { background: var(--red, #ef4444); }
  .indicator.neutral { background: var(--muted); }

  /* Integrity section */
  .integrity-section {
    margin-top: 1rem;
  }

  .section-title-row {
    display: flex;
    align-items: baseline;
    gap: 0.75rem;
    margin-bottom: 0.75rem;
  }

  .section-title-row h2 {
    margin: 0;
  }

  .detail-link {
    font-size: 0.8125rem;
    color: var(--accent);
    text-decoration: none;
    margin-left: auto;
  }
  .detail-link:hover { text-decoration: underline; }

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
    .special-cards {
      grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
      gap: 0.75rem;
    }
  }

  .chart-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1rem;
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
