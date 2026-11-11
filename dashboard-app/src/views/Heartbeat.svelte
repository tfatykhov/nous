<script lang="ts">
  import { apiGet } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import type { HeartbeatData, HeartbeatTrackedFinding } from '../lib/types/api';
  import StatGrid from '../lib/ui/StatGrid.svelte';
  import DataTable from '../lib/ui/DataTable.svelte';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';
  import Chart from '../lib/viz/Chart.svelte';

  const store = usePoll(
    makePollStore<HeartbeatData>(
      (signal) => apiGet<HeartbeatData>('/dashboard/heartbeat', { signal }),
      30_000,
    ),
  );

  // ── Formatters ────────────────────────────────────────────────────────────

  function fmtTokens(n: number): string {
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
    if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K';
    return String(n);
  }

  /** seconds → human string: 30s / 5m / 2h */
  function fmtInterval(secs: number | null | undefined): string {
    if (secs == null) return '--';
    if (secs < 60) return secs + 's';
    if (secs < 3600) return Math.round(secs / 60) + 'm';
    return Math.round(secs / 3600) + 'h';
  }

  /** ISO string → "3m ago" / "just now" */
  function fmtAgo(iso: string | null | undefined): string {
    if (!iso) return '--';
    const diff = (Date.now() - new Date(iso).getTime()) / 1000;
    if (diff < 30) return 'just now';
    const mins = Math.floor(diff / 60);
    if (mins < 1) return Math.floor(diff) + 's ago';
    if (mins < 60) return mins + 'm ago';
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return hrs + 'h ago';
    return Math.floor(hrs / 24) + 'd ago';
  }

  function fmtTs(iso: string | null | undefined): string {
    if (!iso) return '--';
    return new Date(iso).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' });
  }

  // Colours are tokens: Chart resolves them against its ground, the DOM reads var() directly. Budget and urgency are
  // status. A colour written as text takes a text token (URGENCY_INK): paper's amber and red fills are marks.
  function budgetColor(pct: number): string {
    if (pct >= 100) return 'var(--red)';
    if (pct > 80) return 'var(--yellow)';
    return 'var(--pending)';
  }

  // High is a mark in the error text value: the red fill and the amber merge under deuteranopia (review I3).
  const URGENCY_COLOR: Record<string, string> = {
    high: 'var(--err-text)',
    normal: 'var(--yellow)',
    low: 'var(--st-neutral)',
  };

  const URGENCY_INK: Record<string, string> = {
    high: 'var(--err-text)',
    normal: 'var(--warn-text)',
    low: 'var(--st-neutral)',
  };

  /** A lifecycle state's status pill (app.css .status-pill): new is info, acknowledged a warning, resolved ok. */
  const STATE_PILL: Record<string, string> = {
    new: 'info',
    acknowledged: 'warn',
    resolved: 'ok',
  };

  /** A check's status, in a word and a pill: an open circuit breaker, failures on the way to it, or ok. */
  function checkStatus(c: { circuit_breaker_open: boolean; consecutive_failures: number }): { word: string; pill: string } {
    if (c.circuit_breaker_open) return { word: 'open', pill: 'bad' };
    if (c.consecutive_failures > 0) return { word: 'failing', pill: 'warn' };
    return { word: 'ok', pill: 'ok' };
  }

  // ── Chart data builders ───────────────────────────────────────────────────

  function budgetChartData(used: number, limit: number) {
    const remaining = Math.max(0, limit - used);
    const pct = limit > 0 ? Math.round((used / limit) * 100) : 0;
    return {
      labels: ['Used', 'Remaining'],
      datasets: [{
        data: [used, remaining],
        backgroundColor: [budgetColor(pct), 'var(--track)'],
        borderWidth: 0,
      }],
    };
  }

  function findingsBarData(byDay: HeartbeatData['findings_by_day']) {
    return {
      labels: byDay.map((d) => d.date.slice(5)), // MM-DD
      datasets: [
        {
          label: 'High',
          data: byDay.map((d) => d.by_urgency.high),
          backgroundColor: URGENCY_COLOR.high,
          borderWidth: 0,
        },
        {
          label: 'Normal',
          data: byDay.map((d) => d.by_urgency.normal),
          backgroundColor: URGENCY_COLOR.normal,
          borderWidth: 0,
        },
        {
          label: 'Low',
          data: byDay.map((d) => d.by_urgency.low),
          backgroundColor: URGENCY_COLOR.low,
          borderWidth: 0,
        },
      ],
    };
  }

  // ── Table columns ─────────────────────────────────────────────────────────

  const checkCols = [
    { key: 'name', label: 'Check' },
    { key: 'interval_fmt', label: 'Interval' },
    { key: 'last_run_fmt', label: 'Last run' },
    { key: 'failures_fmt', label: 'Failures' },
    { key: 'status_fmt', label: 'Status' },
  ];

  const findingCols = [
    { key: 'state', label: 'State' },
    { key: 'check_name', label: 'Check' },
    { key: 'summary_short', label: 'Summary' },
    { key: 'seen_count', label: 'Seen' },
    { key: 'age_fmt', label: 'Age' },
  ];

  const sessionCols = [
    { key: 'time_fmt', label: 'Time' },
    { key: 'session_short', label: 'Session' },
    { key: 'findings_count', label: 'Findings' },
    { key: 'tokens_fmt', label: 'Tokens' },
  ];
</script>

<header class="view-head">
  <div>
    <h1>Heartbeat</h1>
    <p class="subtitle">Autonomous monitoring, checks, and cognitive triage</p>
  </div>
  <StaleBadge state={$store} />
</header>

{#if $store.data}
  {@const d = $store.data}
  {@const st = d.status}
  {@const bgt = d.budget}
  {@const qh = d.quiet_hours}
  {@const enabled = st.enabled !== false}

  <!-- ── Status Banner ── -->
  {@const activeChecks = d.checks.filter((c) => c.active).length}
  {@const tripped = d.checks.filter((c) => c.circuit_breaker_open).length}
  {@const budgetPct = bgt.limit > 0 ? Math.round((bgt.used / bgt.limit) * 100) : 0}

  <div class="hb-banner" class:hb-banner--disabled={!enabled} class:hb-banner--warn={enabled && tripped > 0}>
    <span class="hb-dot" class:hb-dot--ok={enabled && tripped === 0} class:hb-dot--warn={enabled && tripped > 0} class:hb-dot--off={!enabled}></span>
    <span class="hb-banner-label">{enabled ? 'Heartbeat Active' : 'Heartbeat Disabled'}</span>
    <div class="hb-pills">
      <span class="status-pill" class:status-pill--warn={qh.active}>
        Quiet hours: {qh.active ? `active (${qh.start}:00–${qh.end}:00)` : 'inactive'}
      </span>
      <span
        class="status-pill"
        class:status-pill--ok={budgetPct < 80}
        class:status-pill--warn={budgetPct >= 80 && budgetPct < 100}
        class:status-pill--bad={budgetPct >= 100}
      >
        Budget: {budgetPct >= 100 ? 'exhausted' : budgetPct + '% used'}
      </span>
      {#if st.last_tick}
        <span class="status-pill">Last tick: {fmtAgo(st.last_tick)}</span>
      {/if}
    </div>
  </div>

  <!-- ── Stat cards ── -->
  <StatGrid stats={[
    { label: 'Total ticks (24h)',   value: d.recent_ticks.length.toLocaleString() },
    { label: 'Findings (24h)',      value: d.totals.total.toLocaleString() },
    { label: 'Cognitive sessions',  value: d.cognitive_sessions.length.toLocaleString() },
    { label: 'Checks active',       value: activeChecks + ' / ' + d.checks.length },
    { label: 'Circuit breakers',    value: tripped === 0 ? 'none' : tripped + ' tripped' },
  ]} />

  {#if !enabled}
    <p class="empty" style="padding:3rem 0">Heartbeat is not running — enable it to see monitoring data.</p>
  {:else}

    <!-- ── Charts row ── -->
    <div class="chart-row">
      <section class="chart-card">
        <h2>Token Budget</h2>
        <div class="budget-wrap">
          <Chart
            type="doughnut"
            data={budgetChartData(bgt.used, bgt.limit)}
            options={{ cutout: '75%', plugins: { legend: { display: false } } }}
            height="200px"
          />
          <div class="budget-center">
            <div class="budget-value">{fmtTokens(bgt.used)}</div>
            <div class="budget-sub">of {fmtTokens(bgt.limit)}</div>
          </div>
        </div>
      </section>

      <section class="chart-card">
        <h2>Findings by Urgency (7d)</h2>
        {#if d.findings_by_day.length > 0}
          <Chart
            type="bar"
            data={findingsBarData(d.findings_by_day)}
            options={{
              scales: {
                x: { stacked: true, grid: { display: false } },
                y: { stacked: true, beginAtZero: true, ticks: { precision: 0 } },
              },
              plugins: { legend: { position: 'bottom' } },
            }}
            height="200px"
          />
        {:else}
          <p class="empty">No findings data</p>
        {/if}
      </section>
    </div>

    <!-- ── Check status table ── -->
    <section class="table-card">
      <h2>Check Status</h2>
      {#if d.checks.length > 0}
        <DataTable
          columns={checkCols}
          rows={d.checks.map((c) => ({
            ...c,
            interval_fmt: fmtInterval(c.interval),
            last_run_fmt: fmtAgo(c.last_run),
            failures_fmt: c.consecutive_failures + ' / ' + c.max_failures,
            status_fmt: checkStatus(c).word,
          }))}
          mode="cards"
          rowKey={(r) => r.name}
        >
          {#snippet cell(r, c)}
            {#if c.key === 'status_fmt'}<span class="status-pill status-pill--{checkStatus(r).pill}">{r.status_fmt}</span>{:else}{r[c.key]}{/if}
          {/snippet}
        </DataTable>
      {:else}
        <p class="empty">No checks configured</p>
      {/if}
    </section>

    <!-- ── Finding lifecycle ── -->
    {#if d.finding_lifecycle}
      {@const lc = d.finding_lifecycle}
      {@const byState = lc.stats.by_state}
      {@const activeFindings = lc.findings.filter((f) => f.state !== 'resolved')}
      <section class="table-card">
        <h2>Finding Lifecycle</h2>

        <!-- State pills -->
        <div class="state-pills">
          {#each ['new', 'acknowledged', 'resolved', 'suppressed', 'auto_closed_known_fp'] as st}
            <span class="status-pill status-pill--{STATE_PILL[st] ?? 'neutral'}">
              {st}: {byState[st] ?? 0}
            </span>
          {/each}
          <span class="status-pill">total: {lc.stats.total}</span>
        </div>

        {#if activeFindings.length > 0}
          <DataTable
            columns={findingCols}
            rows={activeFindings.slice(0, 15).map((f) => ({
              ...f,
              summary_short: (f.summary ?? '').slice(0, 80),
              age_fmt: fmtAgo(f.first_seen),
            }))}
            mode="cards"
            rowKey={(r: HeartbeatTrackedFinding) => r.fingerprint}
          >
            {#snippet detail(row: HeartbeatTrackedFinding)}
              <div class="finding-detail">
                <div><span class="dl">Summary</span><span>{row.summary}</span></div>
                <div><span class="dl">First seen</span><span>{fmtTs(row.first_seen)}</span></div>
                <div><span class="dl">Last seen</span><span>{fmtTs(row.last_seen)}</span></div>
                <div><span class="dl">Urgency</span><span class="urgency" style:color={URGENCY_INK[row.urgency] ?? 'var(--st-neutral)'}>{row.urgency}</span></div>
                <div><span class="dl">Escalated</span><span>{row.escalated ? 'yes' : 'no'}</span></div>
                {#if row.outcome}<div><span class="dl">Outcome</span><span>{row.outcome}</span></div>{/if}
                {#if row.reopen_count > 0}<div><span class="dl">Reopens</span><span>{row.reopen_count}</span></div>{/if}
                {#if row.auto_closed_rule}<div><span class="dl">Known-FP rule</span><span>{row.auto_closed_rule}</span></div>{/if}
              </div>
            {/snippet}
          </DataTable>
        {:else}
          <p class="empty">No active findings</p>
        {/if}

        <p class="escalation-note">
          Escalation: low to normal {lc.escalation_policy.low_to_normal_hours}h,
          normal to high {lc.escalation_policy.normal_to_high_hours}h,
          high re-alert {lc.escalation_policy.high_realert_hours}h,
          accumulation threshold {lc.escalation_policy.accumulation_threshold}
        </p>
      </section>
    {/if}

    <!-- ── Tuning status ── -->
    {#if d.tuning}
      <section class="chart-card">
        <h2>Self-Tuning</h2>
        {#if !d.tuning.enabled}
          <p class="empty">Tuning disabled (set NOUS_HEARTBEAT_TUNING_ENABLED=true)</p>
        {:else if d.tuning.last_report}
          {@const tr = d.tuning.last_report}
          <div class="tuning-meta">
            <span>Last run: <strong>{fmtAgo(tr.timestamp)}</strong></span>
            <span>Adjustments: <strong>{tr.adjustments}</strong></span>
            {#if tr.skipped_checks.length > 0}
              <span class="muted">Skipped: {tr.skipped_checks.join(', ')}</span>
            {/if}
          </div>
          <pre class="tuning-pre">{tr.summary || 'No changes'}</pre>
        {:else}
          <p class="empty">No tuning runs yet</p>
        {/if}
      </section>
    {/if}

    <!-- ── Findings timeline + cognitive sessions ── -->
    <div class="chart-row">
      <section class="chart-card">
        <h2>Findings (Last 24h)</h2>
        {#if d.findings_timeline.length > 0}
          <div class="timeline">
            {#each d.findings_timeline as f}
              <div class="tl-item">
                <div class="tl-row">
                  <span
                    class="tl-dot"
                    style:background={URGENCY_COLOR[f.urgency ?? ''] ?? 'var(--st-neutral)'}
                  ></span>
                  <span class="tl-source">{f.source ?? 'unknown'}</span>
                  <span class="tl-time">{fmtAgo(f.timestamp)}</span>
                </div>
                <p class="tl-summary">{f.summary ?? ''}</p>
              </div>
            {/each}
          </div>
        {:else}
          <p class="empty">All clear — no findings in the last 24 hours</p>
        {/if}
      </section>

      <section class="chart-card">
        <h2>Cognitive Sessions</h2>
        {#if d.cognitive_sessions.length > 0}
          <DataTable
            columns={sessionCols}
            rows={d.cognitive_sessions.map((s) => ({
              ...s,
              time_fmt: fmtAgo(s.timestamp),
              session_short: (s.session_id ?? '').slice(0, 12),
              tokens_fmt: fmtTokens(s.tokens_used),
            }))}
            mode="cards"
            rowKey={(_, i) => String(i)}
          />
        {:else}
          <p class="empty">No cognitive sessions today</p>
        {/if}
      </section>
    </div>

  {/if}

{:else if $store.error}
  <p class="status-msg error">Failed to load heartbeat data — retrying…</p>
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
    font-size: 30px;
    font-weight: 650;
    color: var(--text);
    margin: 0 0 0.125rem;
    letter-spacing: -0.022em;
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

  /* ── Banner ── */
  .hb-banner {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    flex-wrap: wrap;
    padding: 0.625rem 0.875rem;
    background: var(--surface);
    border: var(--card-border);
    border-radius: var(--card-radius);
    box-shadow: var(--card-shadow);
    margin-bottom: 1rem;
  }

  /* A tripped breaker tints the banner (no coloured outline); the pills and the dot say what is wrong. */
  .hb-banner--warn { background: var(--warn-bg); }
  /* A disabled banner is not dimmed (its muted pills at 65% were 2.8:1 on the white card): the grey dot and the words
     "Heartbeat Disabled" say it. */

  .hb-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .hb-dot--ok { background: var(--st-completed); }
  .hb-dot--warn { background: var(--yellow); }
  .hb-dot--off { background: var(--st-neutral); }

  .hb-banner-label {
    font-size: 0.875rem;
    font-weight: 600;
    color: var(--text);
    white-space: nowrap;
  }

  .hb-pills {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
    margin-left: auto;
  }

  /* The pills are app.css's .status-pill (one tinted pill on every screen). The total is not dimmed. */

  /* ── Layout ── */
  .chart-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1rem;
    margin-top: 1rem;
  }

  /* A grid item's min-width is auto: one unbroken finding would widen the row (and the page) to fit it. */
  .chart-row > * { min-width: 0; }

  @media (max-width: 640px) {
    .chart-row { grid-template-columns: 1fr; }
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

  /* ── Budget doughnut ── */
  .budget-wrap {
    position: relative;
    max-width: 200px;
    margin: 0 auto;
  }

  .budget-center {
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    pointer-events: none;
  }

  .budget-value {
    font-size: 1.125rem;
    font-weight: 700;
    color: var(--text);
  }

  .budget-sub {
    font-size: 0.6875rem;
    color: var(--muted);
  }

  /* ── Findings timeline ── */
  .timeline {
    padding-left: 0.75rem;
    border-left: 2px solid var(--border);
  }

  .tl-item {
    margin-bottom: 0.875rem;
  }

  .tl-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.1875rem;
  }

  .tl-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
    margin-left: -1.125rem;
  }

  .tl-source {
    font-size: 0.8125rem;
    font-weight: 600;
    color: var(--text);
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .tl-time {
    font-size: 0.75rem;
    color: var(--muted);
    margin-left: auto;
  }

  .tl-summary {
    font-size: 0.8125rem;
    color: var(--muted);
    margin: 0;
    padding-left: 0.25rem;
    overflow-wrap: anywhere;
  }

  /* ── Finding lifecycle ── */
  .state-pills {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
    margin-bottom: 0.75rem;
  }

  .finding-detail {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 0.25rem 0.75rem;
    font-size: 0.8125rem;
    padding: 0.5rem 0;
  }

  .finding-detail > div {
    display: contents;
  }

  .dl {
    color: var(--muted);
    font-weight: 600;
    white-space: nowrap;
  }

  .escalation-note {
    margin-top: 0.75rem;
    font-size: 0.75rem;
    color: var(--muted);
  }

  /* ── Tuning ── */
  .tuning-meta {
    display: flex;
    gap: 1rem;
    flex-wrap: wrap;
    font-size: 0.8125rem;
    margin-bottom: 0.625rem;
  }

  .tuning-pre {
    font-size: 0.6875rem;
    background: var(--wash);
    border: 1px solid var(--border);
    border-radius: var(--card-radius-sm);
    padding: 0.75rem;
    overflow-x: auto;
    white-space: pre-wrap;
    margin: 0;
  }

  /* ── Shared ── */
  .muted { color: var(--muted); }

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

  .status-msg.error { color: var(--err-text); }
</style>
