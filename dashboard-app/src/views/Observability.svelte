<script lang="ts">
  import { fmtUtc } from '../lib/harness';
  import { SvelteSet } from 'svelte/reactivity';
  import { apiGet } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import { routeHref } from '../lib/router';
  import type { ActivityData, ActivityEvent, ObservabilityData } from '../lib/types/api';
  import StatGrid from '../lib/ui/StatGrid.svelte';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';
  import Chart from '../lib/viz/Chart.svelte';
  import { Button } from '../lib/ui/button';
  import { tint } from '../lib/chartColors';

  const store = usePoll(
    makePollStore<ObservabilityData>(
      (signal) => apiGet<ObservabilityData>('/dashboard/observability', { signal }),
      30_000,
    ),
  );

  // The Event Timeline, moved here from the Activity screen (F108). Fetched once per open, as Activity did; its
  // retry reloads it.
  const activity = usePoll(
    makePollStore<ActivityData>(
      (signal) => apiGet<ActivityData>('/dashboard/activity?hours=168', { signal }),
      0, // fetch-once
    ),
  );
  /** The page Refresh: everything this screen shows (finish review fix 8: one header order). */
  const refreshPage = () => Promise.all([store.refresh(), activity.refresh()]);

  function formatDateTime(dateStr: string | null): string {
    return fmtUtc(dateStr);
  }

  function eventSummary(event: ActivityEvent): string {
    const d = (event.data ?? {}) as Record<string, unknown>;
    const str = (k: string): string => String(d[k] ?? '');
    const num = (k: string): number => Number(d[k] ?? 0);
    switch (event.type) {
      case 'censor_triggered':
        return `Censor ${str('censor_id')} fired — matched: ${str('matched_text')}`;
      case 'sleep_completed':
        return `Sleep cycle completed. ${num('facts_created')} facts, ${num('procedures_created')} procedures.`;
      case 'schedule_fired':
        return `Schedule ${str('task') || str('schedule_id')} fired.`;
      case 'subtask_completed':
        return `Subtask completed: ${str('title') || str('subtask_id')}`;
      case 'subtask_failed':
        return `Subtask failed: ${str('title') || str('subtask_id')}${d['error'] ? ' — ' + str('error') : ''}`;
      case 'censor_created':
        return `New censor created: ${str('trigger_pattern')} (${d['auto'] ? 'auto' : 'manual'})`;
      case 'censor_escalated':
        return `Censor escalated: ${str('trigger_pattern')} — ${str('reason')}`;
      default:
        return JSON.stringify(event.data).slice(0, 120);
    }
  }

  function typeLabel(type: string): string {
    return type.replace(/_/g, ' ');
  }

  // ── Trace expand/collapse ────────────────────────────────────────────────
  // SvelteSet keyed by trace_id. Adding/removing an id toggles expansion.
  // Survives the 30s data refresh — no {#key} resets, no save/restore needed.
  const expandedTraces = new SvelteSet<string>();

  function toggleTrace(traceId: string) {
    if (expandedTraces.has(traceId)) {
      expandedTraces.delete(traceId);
    } else {
      expandedTraces.add(traceId);
    }
  }

  // ── Helpers ───────────────────────────────────────────────────────────────

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

  function fmtNum(n: number | null | undefined): string {
    if (n == null) return '--';
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
    if (n >= 1_000) return (n / 1_000).toFixed(1) + 'k';
    return String(n);
  }

  function fmtUptime(secs: number): string {
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    return h > 0 ? `${h}h ${m}m` : `${m}m`;
  }

  /** Strip module prefix: "nous.handlers.fact_extractor" → "fact_extractor" */
  function shortHandler(name: string): string {
    const parts = name.split('.');
    return parts.length >= 2 ? parts[parts.length - 2] : name;
  }

  /** The dot's token by error rate: the success mark, the amber, and --err-text (not --red: under deuteranopia it stays
   *  apart from the amber, 12.5 where red is 5.1; both are #f87171 under dark). Every pair is 9.8 or more apart under
   *  all three visions, and the rate itself is printed beside the dot. */
  function handlerDotToken(errorRate: number): string {
    if (errorRate > 0.1) return '--err-text';
    if (errorRate > 0) return '--yellow';
    return '--oc-success';
  }

  // ── Drift trend chart builders ────────────────────────────────────────────

  function trendLabels(points: { t: string }[]): string[] {
    return points.map((p) => {
      const d = new Date(p.t);
      return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours()}:00`;
    });
  }

  function errorTrendData(points: { t: string; v: number }[]) {
    return {
      labels: trendLabels(points),
      datasets: [{
        label: 'Error rate',
        data: points.map((p) => p.v),
        borderColor: 'var(--red)',
        backgroundColor: tint('--red', 0.1),
        fill: true,
        borderWidth: 2,
        pointRadius: 2,
      }],
    };
  }

  const trendLineOpts = {
    scales: {
      // Level labels: at phone width Chart.js skips some instead of tilting them over the plot, and keeps a gap between
      // the ones it draws (with its default 3 px they ran together).
      x: { display: true, ticks: { maxTicksLimit: 8, maxRotation: 0, autoSkipPadding: 16, font: { size: 10 } } },
      y: { beginAtZero: true },
    },
    plugins: { legend: { display: false } },
  };
</script>

<header class="view-head">
  <div>
    <h1>Observability</h1>
    <p class="subtitle">How the event bus is doing, the causal traces, and drift in behaviour.</p>
  </div>
  <div class="head-right">
    <StaleBadge state={$store} />
    <Button variant="outline" size="lg" class="px-4 max-md:min-h-11" onclick={() => void refreshPage()} disabled={$store.loading || $activity.loading}>{$store.loading || $activity.loading ? 'Loading…' : 'Refresh'}</Button>
  </div>
</header>

{#if $store.data}
  {@const d = $store.data}
  {@const eb = d.event_bus}
  {@const handlers = eb.handlers ?? {}}
  {@const handlerNames = Object.keys(handlers)}

  <!-- ── Section 1: Event Bus Health ── -->
  <StatGrid stats={[
    { label: 'Events processed',  value: fmtNum(eb.total_processed) },
    { label: 'Queue depth',       value: String(eb.queue_depth ?? 0) },
    { label: 'Handlers',          value: handlerNames.length + ' active' },
    { label: 'Events dropped',    value: String(eb.total_dropped ?? 0) },
    { label: 'Uptime',            value: fmtUptime(eb.uptime_seconds ?? 0) },
  ]} />

  <!-- Handler health list -->
  <section class="chart-card">
    <h2>Handler health</h2>
    {#if handlerNames.length > 0}
      <div class="handler-list">
        {#each handlerNames as name}
          {@const h = handlers[name]}
          <div class="handler-row">
            <span class="handler-dot" style:--dot={`var(${handlerDotToken(h.error_rate ?? 0)})`}></span>
            <span class="handler-name">{shortHandler(name)}</span>
            <!-- The figures as one group: at phone width it wraps under the name as a whole. -->
            <span class="handler-stats">
              <span class="handler-stat">{h.successes ?? 0}/{h.invocations ?? 0}</span>
              <span class="handler-stat">{(h.avg_duration_ms ?? 0).toFixed(1)}ms</span>
              <!-- Always the slot, empty when the handler never ran, so the rate stays in its column. -->
              <span class="handler-stat">{h.last_invoked_ago_s != null ? `${Math.round(h.last_invoked_ago_s)}s ago` : ''}</span>
              <span class="handler-rate" class:handler-rate--warn={(h.error_rate ?? 0) > 0.1}>
                {(h.error_rate * 100).toFixed(1)}% err
              </span>
            </span>
          </div>
        {/each}
      </div>
    {:else}
      <p class="empty">No handlers registered</p>
    {/if}
  </section>

  <!-- ── Section 2: Causal Traces + Modifications ── -->
  <div class="chart-row">
    <!-- Recent traces with expand/collapse tree -->
    <section class="chart-card">
      <h2>Recent causal traces</h2>
      {#if d.recent_traces.length > 0}
        <div class="trace-list">
          {#each d.recent_traces as t (t.trace_id)}
            <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
            <div
              class="trace-row"
              class:trace-row--expanded={expandedTraces.has(t.trace_id)}
              onclick={() => toggleTrace(t.trace_id)}
              role="button"
              tabindex="0"
              onkeydown={(e) => {
                if (e.key !== 'Enter' && e.key !== ' ') return;
                e.preventDefault(); // Space would scroll the page
                toggleTrace(t.trace_id);
              }}
              aria-expanded={expandedTraces.has(t.trace_id)}
            >
              <span class="trace-toggle">{expandedTraces.has(t.trace_id) ? 'Hide' : 'Show'}</span>
              <span class="trace-type">{t.root_type ?? 'unknown'}</span>
              <span class="trace-badge">{t.event_count} {t.event_count === 1 ? 'event' : 'events'}</span>
              {#if t.has_modifications}
                <span class="trace-badge trace-badge--mod">modified</span>
              {/if}
              <span class="trace-age">{fmtAgo(t.timestamp)}</span>
            </div>
            {#if expandedTraces.has(t.trace_id)}
              <div class="trace-detail">
                <div class="trace-detail-row">
                  <span class="dl">Trace ID</span>
                  <span class="mono">{t.trace_id}</span>
                </div>
                <div class="trace-detail-row">
                  <span class="dl">Root type</span>
                  <span>{t.root_type}</span>
                </div>
                <div class="trace-detail-row">
                  <span class="dl">Events</span>
                  <span>{t.event_count}</span>
                </div>
                <div class="trace-detail-row">
                  <span class="dl">Has modifications</span>
                  <span>{t.has_modifications ? 'yes' : 'no'}</span>
                </div>
                <div class="trace-detail-row">
                  <span class="dl">Timestamp</span>
                  <span>{t.timestamp ? fmtUtc(t.timestamp) : '--'}</span>
                </div>
              </div>
            {/if}
          {/each}
        </div>
      {:else}
        <p class="empty">No traces recorded yet</p>
      {/if}
    </section>

    <!-- Autonomous modifications -->
    <section class="chart-card">
      <h2>Autonomous modifications (24h)</h2>
      {#if d.recent_modifications.length > 0}
        <div class="mod-list">
          {#each d.recent_modifications as m}
            <div class="mod-row">
              <span class="mod-type">{m.modifies ?? 'unknown'}</span>
              <span class="mod-event">{m.type}</span>
              <span class="mod-age">{fmtAgo(m.timestamp)}</span>
            </div>
          {/each}
        </div>
      {:else}
        <p class="empty">No autonomous modifications in the last 24h</p>
      {/if}
    </section>
  </div>

  <!-- ── Section 3: Behavioral Drift ── -->

  <!-- Anomalies banner -->
  {#if d.drift?.anomalies && d.drift.anomalies.length > 0}
    <section class="chart-card">
      <h2>Active drift anomalies</h2>
      {#each d.drift.anomalies as a}
        <div class="anomaly" class:anomaly--alert={a.severity === 'alert'}>
          <!-- The severity in a word as well as the tint (status is never colour alone). -->
          <span class="anomaly-level status-pill" class:status-pill--bad={a.severity === 'alert'} class:status-pill--warn={a.severity !== 'alert'}>{a.severity === 'alert' ? 'Alert' : 'Warning'}</span>
          <strong>{a.metric}</strong>:
          {#if a.residualized_by}
            {a.raw_current} raw, {a.current} unexplained after {a.residualized_by}
          {:else}
            {a.current}
          {/if}
          ({a.direction} from {a.mean}{a.z_score != null ? ' ± ' + a.stddev : ', previously constant'})
        </div>
      {/each}
    </section>
  {/if}

  <!-- Drift trends (the fact growth rate is on Memory health's Growth tab, F108) -->
  {@const errPts = d.drift_trends?.handler_error_rate ?? []}
  {#if errPts.length > 0}
    <div class="chart-row">
      <section class="chart-card">
        <h2>Handler error rate (7d)</h2>
        <Chart type="line" data={errorTrendData(errPts)} options={trendLineOpts} height="180px" />
      </section>
    </div>
  {:else}
    <section class="chart-card">
      <p class="empty">No drift trend data available yet</p>
    </section>
  {/if}

  <!-- Latest snapshot -->
  {#if d.drift}
    {@const m = d.drift.metrics}
    <div class="snapshot-grid">
      <StatGrid stats={[
        { label: 'Facts',       value: String(m.fact_count ?? 0),
          note: m.fact_count_delta != null ? 'change ' + (m.fact_count_delta > 0 ? '+' : '') + m.fact_count_delta : undefined },
        { label: 'Episodes',   value: String(m.episode_count ?? 0) },
        { label: 'Censors',    value: String(m.active_censor_count ?? 0) },
        { label: 'Procedures', value: String(m.procedure_count ?? 0) },
        { label: 'Error rate', value: ((m.handler_error_rate ?? 0) * 100).toFixed(1) + '%' },
      ]} />
    </div>
    <p class="snapshot-ts">Snapshot: {fmtAgo(d.drift.timestamp)}</p>
  {/if}

{:else if $store.error}
  <p class="status-msg error">Failed to load observability data — retrying…</p>
{:else}
  <p class="status-msg">Loading…</p>
{/if}

<!-- ── Event Timeline (moved from the Activity screen, F108) ── -->
<section class="chart-card">
  <div class="section-head">
    <h2>Event timeline</h2>
  </div>
  {#if $activity.data}
    {@const d = $activity.data}
    {#if d.events.length > 0}
      <div class="timeline">
        {#each d.events as event}
          <div class="timeline-item">
            <div class="event-header">
              <span class="event-badge">{typeLabel(event.type)}</span>
              <span class="event-time">{formatDateTime(event.created_at)}</span>
            </div>
            <div class="event-summary">{eventSummary(event)}</div>
          </div>
        {/each}
      </div>
    {:else}
      <p class="empty">No events in the last 7 days.</p>
    {/if}
  {:else if $activity.error}
    <p class="empty error">
      Failed to load activity data —
      <button class="retry-link" onclick={() => void activity.refresh()}>retry</button>
    </p>
  {:else}
    <p class="empty">Loading…</p>
  {/if}
</section>

<!-- The last call's token breakdown and the recent API calls are on the Model calls screen (F108). -->
<p class="moved">Model calls moved to <a href={routeHref('model-calls')}>Model calls</a></p>

<style>
  .head-right { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
  .view-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    flex-wrap: wrap;
    margin-bottom: 1.25rem;
  }

  h1 {
    font-size: 30px;
    font-weight: 650;
    color: var(--text);
    margin: 0 0 0.125rem;
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

  .chart-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1rem;
    margin-top: 1rem;
  }

  @media (max-width: 640px) {
    .chart-row { grid-template-columns: 1fr; }
  }

  .chart-row > * { min-width: 0; }

  .chart-card {
    background: var(--surface);
    border: var(--card-border);
    border-radius: var(--card-radius);
    box-shadow: var(--card-shadow);
    padding: 1rem;
    margin-top: 1rem;
  }

  /* ── Handler health ── */
  .handler-list {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  /* At phone width the figures wrap under the name as one group, instead of widening the page. */
  .handler-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.625rem;
    font-size: 0.8125rem;
  }

  .handler-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
    background: var(--dot);
  }

  /* A basis, not auto: a long name wraps beside its dot rather than leaving the dot alone on a line. */
  .handler-name {
    flex: 1 1 8rem;
    min-width: 0;
    overflow-wrap: anywhere;
    color: var(--text);
    font-weight: 500;
  }

  .handler-stats {
    display: flex;
    gap: 0.625rem;
    margin-left: auto;
  }

  .handler-stat {
    color: var(--muted);
    min-width: 60px;
    text-align: right;
    white-space: nowrap;
  }

  .handler-rate {
    min-width: 60px;
    text-align: right;
    color: var(--muted);
    white-space: nowrap;
  }

  .handler-rate--warn {
    color: var(--err-text);
  }

  /* ── Causal traces ── */
  .trace-list {
    display: flex;
    flex-direction: column;
  }

  .trace-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem 0.375rem;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.8125rem;
    transition: background 0.1s;
  }

  .trace-row:hover,
  .trace-row--expanded {
    background: var(--surface-hover);
  }

  .trace-row:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: 2px;
  }

  /* The open or closed state in a word (it was a triangle glyph); aria-expanded carries it for assistive tech. */
  .trace-toggle {
    font-size: 0.75rem;
    color: var(--muted);
    min-width: 2.25rem;
    flex-shrink: 0;
  }

  .trace-type {
    font-weight: 600;
    color: var(--accent-text);
    flex: 1 1 8rem; /* a basis: a long type wraps in its place instead of taking a line of its own */
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .trace-badge {
    font-size: 0.6875rem;
    padding: 0.125rem 0.375rem;
    border-radius: 999px;
    /* A tint, not the wash: on paper the wash is the hovered row's own fill, so the badge lost its shape there. */
    background: color-mix(in srgb, var(--muted) 10%, transparent);
    color: var(--muted);
    white-space: nowrap;
  }

  /* PR 3's badge tint: at 15% the paper text is 4.49:1 on a hovered row. */
  .trace-badge--mod {
    background: color-mix(in srgb, var(--st-partial) 12.5%, transparent);
    color: var(--st-partial);
  }

  .trace-age {
    color: var(--muted);
    font-size: 0.75rem;
    white-space: nowrap;
    margin-left: auto;
  }

  /* A tinted block at the world's radius, indented under its row (no side stripe: the craft floor). */
  .trace-detail {
    padding: 0.5rem 0.75rem;
    background: var(--wash);
    margin: 0 0 0.25rem 1rem;
    border-radius: var(--card-radius-sm);
  }

  .trace-detail-row {
    display: flex;
    gap: 0.5rem;
    font-size: 0.8125rem;
    margin-bottom: 0.25rem;
  }

  .dl {
    color: var(--muted);
    font-weight: 600;
    min-width: 130px;
    flex-shrink: 0;
  }

  .mono {
    font-family: var(--font-mono);
    font-size: 0.75rem;
    word-break: break-all;
    color: var(--muted);
  }

  /* ── Modifications ── */
  .mod-list {
    display: flex;
    flex-direction: column;
    gap: 0.375rem;
  }

  .mod-row {
    display: flex;
    align-items: center;
    gap: 0.625rem;
    font-size: 0.8125rem;
    padding: 0.375rem 0;
    border-bottom: 1px solid var(--border);
  }

  .mod-row:last-child {
    border-bottom: none;
  }

  .mod-type {
    font-size: 0.6875rem;
    padding: 0.125rem 0.375rem;
    border-radius: 999px;
    background: var(--badge-good-bg);
    color: var(--ok-text);
    white-space: nowrap;
    font-weight: 600;
  }

  .mod-event {
    flex: 1;
    color: var(--text);
  }

  .mod-age {
    color: var(--muted);
    font-size: 0.75rem;
    white-space: nowrap;
  }

  /* ── Drift anomalies ── */
  /* A tinted block at the world's radius; the severity is the tinted pill in front (Alert red, Warning amber). No side
     stripe (the craft floor). */
  .anomaly {
    padding: 0.5rem 0.75rem;
    border-radius: var(--card-radius-sm);
    background: var(--wash);
    font-size: 0.8125rem;
    margin-bottom: 0.5rem;
    color: var(--text);
    overflow-wrap: anywhere;
  }

  .anomaly-level {
    margin-right: 0.375rem;
  }

  .snapshot-grid {
    margin-top: 1rem;
  }

  .snapshot-ts {
    font-size: 0.75rem;
    color: var(--muted);
    margin: 0.375rem 0 0;
  }

  /* ── Event Timeline (from Activity) ── */
  .section-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    margin-bottom: 0.75rem;
  }

  .section-head h2 {
    margin: 0;
  }

  .timeline {
    display: flex;
    flex-direction: column;
    gap: 0;
  }

  .timeline-item {
    padding: 0.75rem 0;
    border-bottom: 1px solid var(--border);
  }
  .timeline-item:last-child {
    border-bottom: none;
  }

  .event-header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    margin-bottom: 0.25rem;
  }

  .event-badge {
    font-size: 0.6875rem;
    font-weight: 600;
    text-transform: none;
    letter-spacing: 0;
    color: var(--accent);
    background: color-mix(in srgb, var(--accent) 12%, transparent);
    border-radius: 999px;
    padding: 0.125rem 0.375rem;
    max-width: 100%;
    overflow-wrap: anywhere;
  }

  .event-time {
    font-size: 0.6875rem;
    color: var(--muted);
    white-space: nowrap;
  }

  .event-summary {
    font-size: 0.8125rem;
    color: var(--text);
    line-height: 1.45;
    word-break: break-word;
  }

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

  .moved {
    font-size: 0.8125rem;
    color: var(--muted);
    margin: 1rem 0 0;
  }

  .moved a {
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
