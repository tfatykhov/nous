<script lang="ts">
  import { SvelteSet } from 'svelte/reactivity';
  import { apiGet, type ApiError } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import type { ObservabilityData, ObsContextLogEntry } from '../lib/types/api';
  import StatGrid from '../lib/ui/StatGrid.svelte';
  import DataTable from '../lib/ui/DataTable.svelte';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';
  import Chart from '../lib/viz/Chart.svelte';

  const store = usePoll(
    makePollStore<ObservabilityData>(
      (signal) => apiGet<ObservabilityData>('/dashboard/observability', { signal }),
      30_000,
    ),
  );

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

  // ── Context log expand/collapse (keyed by entry id) ──────────────────────
  const expandedCtx = new SvelteSet<string>();

  function toggleCtx(id: string) {
    if (expandedCtx.has(id)) {
      expandedCtx.delete(id);
    } else {
      expandedCtx.add(id);
    }
  }

  // ── Recent-call drill-down: structured section text + raw payload ──────────
  // Lazy-loaded from /context/log/{id}/sections and /context/log/{id}/payload
  // ONLY on button click (never during render). State keyed by entry id:
  //   ctx*[id] === undefined → not fetched, === null → loading, else → loaded.
  type CtxView = 'sections' | 'payload';
  interface CtxSections {
    sections: Record<string, number>;
    sections_text: Record<string, string>;
  }
  let ctxView = $state<Record<string, CtxView>>({});
  let ctxSections = $state<Record<string, CtxSections | null>>({});
  let ctxPayload = $state<Record<string, unknown>>({});
  let ctxError = $state<Record<string, string>>({});

  async function viewSections(id: string) {
    ctxError[id] = '';
    ctxView[id] = 'sections';
    if (ctxSections[id] === undefined) {
      ctxSections[id] = null; // loading
      try {
        ctxSections[id] = await apiGet<CtxSections>(
          '/context/log/' + encodeURIComponent(id) + '/sections',
        );
      } catch (e) {
        delete ctxSections[id];
        ctxError[id] =
          (e as ApiError).status === 404
            ? 'Context text not available (entry expired or capture disabled).'
            : 'Failed to load context text';
      }
    }
  }

  async function viewPayload(id: string) {
    ctxError[id] = '';
    ctxView[id] = 'payload';
    if (ctxPayload[id] === undefined) {
      ctxPayload[id] = null; // loading
      try {
        ctxPayload[id] = await apiGet<unknown>(
          '/context/log/' + encodeURIComponent(id) + '/payload',
        );
      } catch (e) {
        delete ctxPayload[id];
        ctxError[id] =
          (e as ApiError).status === 404
            ? 'Raw payload was not captured (full-payload capture disabled or evicted).'
            : 'Failed to load raw payload';
      }
    }
  }

  function hideCtx(id: string) {
    delete ctxView[id];
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

  function handlerDotColor(errorRate: number): string {
    if (errorRate > 0.1) return '#f87171';
    if (errorRate > 0) return '#fbbf24';
    return '#4ade80';
  }

  const SECTION_COLORS: Record<string, string> = {
    messages: '#60a5fa',
    tools_definition: '#a78bfa',
    identity: '#34d399',
    working_memory: '#fb923c',
    execution_ledger: '#f87171',
    relevant_facts: '#fbbf24',
    related_decisions: '#22d3ee',
    frame_instructions: '#c084fc',
    user_profile: '#38bdf8',
    censors: '#f472b6',
  };

  function sectionColor(name: string): string {
    return SECTION_COLORS[name] ?? '#6b6b8a';
  }

  // ── Context token doughnut ────────────────────────────────────────────────

  function tokenDoughnutData(entry: ObsContextLogEntry) {
    const breakdown = entry.token_breakdown ?? {};
    const sorted = Object.entries(breakdown).sort(([, a], [, b]) => b - a);
    const top = sorted.slice(0, 6);
    const other = sorted.slice(6).reduce((s, [, v]) => s + v, 0);
    const labels = top.map(([k]) => k);
    const values = top.map(([, v]) => v);
    if (other > 0) {
      labels.push('other');
      values.push(other);
    }
    const colors = [
      '#60a5fa', '#a78bfa', '#34d399', '#fb923c', '#f87171', '#fbbf24', '#6b6b8a',
    ];
    return {
      labels,
      datasets: [{
        data: values,
        backgroundColor: colors.slice(0, labels.length),
        borderWidth: 0,
      }],
    };
  }

  // ── Drift trend chart builders ────────────────────────────────────────────

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
        borderColor: '#60a5fa',
        backgroundColor: 'rgba(96,165,250,0.1)',
        fill: true,
        borderWidth: 2,
        pointRadius: 2,
      }],
    };
  }

  function errorTrendData(points: { t: string; v: number }[]) {
    return {
      labels: trendLabels(points),
      datasets: [{
        label: 'Error rate',
        data: points.map((p) => p.v),
        borderColor: '#f87171',
        backgroundColor: 'rgba(248,113,113,0.1)',
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

  // ── Recent-calls table columns ────────────────────────────────────────────

  const ctxCols = [
    { key: 'turn_fmt', label: 'Turn' },
    { key: 'frame_id', label: 'Frame' },
    { key: 'tokens_fmt', label: 'Tokens' },
    { key: 'util_fmt', label: 'Util' },
    { key: 'tools_count', label: 'Tools' },
    { key: 'age_fmt', label: 'Age' },
  ];
</script>

<header class="view-head">
  <div>
    <h1>Observability</h1>
    <p class="subtitle">Event bus health, causal traces, behavioral drift, and context visibility</p>
  </div>
  <StaleBadge state={$store} />
</header>

{#if $store.data}
  {@const d = $store.data}
  {@const eb = d.event_bus}
  {@const handlers = eb.handlers ?? {}}
  {@const handlerNames = Object.keys(handlers)}
  {@const totalInvoc = handlerNames.reduce((s, n) => s + (handlers[n].invocations ?? 0), 0)}
  {@const totalErrors = handlerNames.reduce((s, n) => s + (handlers[n].errors ?? 0), 0)}
  {@const overallErrRate = totalInvoc > 0 ? totalErrors / totalInvoc : 0}

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
    <h2>Handler Health</h2>
    {#if handlerNames.length > 0}
      <div class="handler-list">
        {#each handlerNames as name}
          {@const h = handlers[name]}
          <div class="handler-row">
            <span class="handler-dot" style="background:{handlerDotColor(h.error_rate ?? 0)}"></span>
            <span class="handler-name">{shortHandler(name)}</span>
            <span class="handler-stat">{h.successes ?? 0}/{h.invocations ?? 0}</span>
            <span class="handler-stat">{(h.avg_duration_ms ?? 0).toFixed(1)}ms</span>
            {#if h.last_invoked_ago_s != null}
              <span class="handler-stat">{Math.round(h.last_invoked_ago_s)}s ago</span>
            {/if}
            <span class="handler-rate" class:handler-rate--warn={overallErrRate > 0.1}>
              {(h.error_rate * 100).toFixed(1)}% err
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
      <h2>Recent Causal Traces</h2>
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
              onkeydown={(e) => e.key === 'Enter' && toggleTrace(t.trace_id)}
              aria-expanded={expandedTraces.has(t.trace_id)}
            >
              <span class="trace-arrow">{expandedTraces.has(t.trace_id) ? '▼' : '▶'}</span>
              <span class="trace-type">{t.root_type ?? '?'}</span>
              <span class="trace-badge">{t.event_count} events</span>
              {#if t.has_modifications}
                <span class="trace-badge trace-badge--mod">MOD</span>
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
                  <span>{t.timestamp ? new Date(t.timestamp).toLocaleString() : '--'}</span>
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
      <h2>Autonomous Modifications (24h)</h2>
      {#if d.recent_modifications.length > 0}
        <div class="mod-list">
          {#each d.recent_modifications as m}
            <div class="mod-row">
              <span class="mod-type">{m.modifies ?? '?'}</span>
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
      <h2>Active Drift Anomalies</h2>
      {#each d.drift.anomalies as a}
        <div class="anomaly" class:anomaly--alert={a.severity === 'alert'}>
          <strong>{a.metric}</strong>: {a.current}
          ({a.direction} from {a.mean} ± {a.stddev})
        </div>
      {/each}
    </section>
  {/if}

  <!-- Drift trends -->
  {@const factPts = d.drift_trends?.fact_count_delta ?? []}
  {@const errPts = d.drift_trends?.handler_error_rate ?? []}
  {#if factPts.length > 0 || errPts.length > 0}
    <div class="chart-row">
      {#if factPts.length > 0}
        <section class="chart-card">
          <h2>Fact Growth Rate (7d)</h2>
          <Chart type="line" data={factTrendData(factPts)} options={trendLineOpts} height="180px" />
        </section>
      {/if}
      {#if errPts.length > 0}
        <section class="chart-card">
          <h2>Handler Error Rate (7d)</h2>
          <Chart type="line" data={errorTrendData(errPts)} options={trendLineOpts} height="180px" />
        </section>
      {/if}
    </div>
  {:else}
    <section class="chart-card">
      <p class="empty">No drift trend data available yet</p>
    </section>
  {/if}

  <!-- Latest snapshot -->
  {#if d.drift}
    {@const m = d.drift.metrics}
    <StatGrid stats={[
      { label: 'Facts',       value: (m.fact_count ?? 0) + (m.fact_count_delta != null ? ' (Δ' + (m.fact_count_delta > 0 ? '+' : '') + m.fact_count_delta + ')' : '') },
      { label: 'Episodes',   value: String(m.episode_count ?? 0) },
      { label: 'Censors',    value: String(m.active_censor_count ?? 0) },
      { label: 'Procedures', value: String(m.procedure_count ?? 0) },
      { label: 'Error rate', value: ((m.handler_error_rate ?? 0) * 100).toFixed(1) + '%' },
    ]} />
    <p class="snapshot-ts">Snapshot: {fmtAgo(d.drift.timestamp)}</p>
  {/if}

  <!-- ── Section 4: Context Visibility ── -->
  <div class="chart-row">
    <!-- Token doughnut for last call -->
    <section class="chart-card">
      <h2>Context Token Breakdown (Last Call)</h2>
      {#if d.context_log.length > 0}
        <Chart
          type="doughnut"
          data={tokenDoughnutData(d.context_log[0])}
          options={{ cutout: '60%', plugins: { legend: { position: 'bottom', labels: { font: { size: 11 }, padding: 8 } } } }}
          height="240px"
        />
      {:else}
        <p class="empty">No API calls logged yet</p>
      {/if}
    </section>

    <!-- Recent API calls table with expandable detail -->
    <section class="chart-card">
      <h2>Recent API Calls</h2>
      {#if d.context_log.length > 0}
        <DataTable
          columns={ctxCols}
          rows={d.context_log.map((e) => ({
            ...e,
            turn_fmt: 'T' + (e.turn_number ?? '?'),
            tokens_fmt: '~' + fmtNum(e.total_tokens_est) + (e.input_tokens_actual != null ? ' / ' + fmtNum(e.input_tokens_actual) : ''),
            util_fmt: (e.utilization_pct ?? 0).toFixed(1) + '%',
            age_fmt: fmtAgo(e.timestamp),
          }))}
          mode="cards"
          rowKey={(r: ObsContextLogEntry) => r.id}
        >
          {#snippet detail(row: ObsContextLogEntry)}
            <div class="ctx-detail">
              <!-- Token breakdown mini bars -->
              {#if Object.keys(row.token_breakdown ?? {}).length > 0}
                <div class="ctx-breakdown">
                  <div class="ctx-breakdown-title">Token Breakdown</div>
                  {#each Object.entries(row.token_breakdown).sort(([,a],[,b]) => b - a) as [section, tokens]}
                    {@const pct = row.total_tokens_est > 0 ? (tokens / row.total_tokens_est * 100) : 0}
                    <div class="ctx-bar-row">
                      <span class="ctx-bar-label">{section}</span>
                      <div class="ctx-bar-track">
                        <div class="ctx-bar-fill" style="width:{pct.toFixed(1)}%;background:{sectionColor(section)}"></div>
                      </div>
                      <span class="ctx-bar-value">{fmtNum(tokens)} <span class="muted">({pct.toFixed(0)}%)</span></span>
                    </div>
                  {/each}
                </div>
              {/if}
              <!-- Metadata grid -->
              <div class="ctx-meta">
                <div><span class="dl">Model</span> {row.model}</div>
                <div><span class="dl">Call type</span> {row.call_type}</div>
                <div><span class="dl">Window</span> {fmtNum(row.context_window_size)}</div>
                <div><span class="dl">Messages</span> {row.messages_count}</div>
                <div><span class="dl">Facts</span> {row.loaded_facts}</div>
                <div><span class="dl">Decisions</span> {row.loaded_decisions}</div>
                {#if row.output_tokens != null}
                  <div><span class="dl">Output</span> {fmtNum(row.output_tokens)} tok</div>
                {/if}
                {#if row.duration_ms != null}
                  <div><span class="dl">Duration</span> {(row.duration_ms / 1000).toFixed(1)}s</div>
                {/if}
                {#if row.cache_read != null}
                  <div><span class="dl">Cache read</span> {fmtNum(row.cache_read)}</div>
                {/if}
                {#if row.stop_reason}
                  <div><span class="dl">Stop</span> {row.stop_reason}</div>
                {/if}
              </div>
              <!-- Sections + tools -->
              {#if row.sections_present.length > 0}
                <p class="ctx-tags">Sections: {row.sections_present.join(', ')}</p>
              {/if}
              {#if row.tool_names.length > 0}
                <p class="ctx-tags">Tools: {row.tool_names.join(', ')}</p>
              {/if}

              <!-- Structured section text + raw payload (lazy-loaded on click) -->
              <div class="ctx-actions">
                <button
                  type="button"
                  class="ctx-btn"
                  class:active={ctxView[row.id] === 'sections'}
                  onclick={() => viewSections(row.id)}
                >View Context Text</button>
                <button
                  type="button"
                  class="ctx-btn"
                  class:active={ctxView[row.id] === 'payload'}
                  onclick={() => viewPayload(row.id)}
                >View Raw Payload</button>
                {#if ctxView[row.id]}
                  <button type="button" class="ctx-btn ghost" onclick={() => hideCtx(row.id)}>Hide</button>
                {/if}
              </div>
              {#if ctxError[row.id]}
                <p class="ctx-err">{ctxError[row.id]}</p>
              {/if}

              {#if ctxView[row.id] === 'sections'}
                {#if ctxSections[row.id] === null}
                  <p class="ctx-loading muted">Loading…</p>
                {:else if ctxSections[row.id]}
                  {@const sec = ctxSections[row.id]!}
                  {#if Object.keys(sec.sections_text ?? {}).length > 0}
                    {#each Object.keys(sec.sections_text) as name}
                      <details class="ctx-section">
                        <summary>
                          <span class="ctx-section-name">{name}</span>
                          <span class="muted">{fmtNum((sec.sections ?? {})[name] ?? 0)} tokens</span>
                        </summary>
                        <pre class="ctx-pre">{sec.sections_text[name]}</pre>
                      </details>
                    {/each}
                  {:else}
                    <p class="ctx-loading muted">No section text available</p>
                  {/if}
                {/if}
              {:else if ctxView[row.id] === 'payload'}
                {#if ctxPayload[row.id] === null}
                  <p class="ctx-loading muted">Loading…</p>
                {:else if ctxPayload[row.id] !== undefined}
                  <pre class="ctx-pre ctx-payload">{JSON.stringify(ctxPayload[row.id], null, 2)}</pre>
                {/if}
              {/if}
            </div>
          {/snippet}
        </DataTable>
      {:else}
        <p class="empty">No API calls logged yet</p>
      {/if}
    </section>
  </div>

{:else if $store.error}
  <p class="status-msg error">Failed to load observability data — retrying…</p>
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
    .chart-row { grid-template-columns: 1fr; }
  }

  .chart-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1rem;
    margin-top: 1rem;
  }

  /* ── Handler health ── */
  .handler-list {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .handler-row {
    display: flex;
    align-items: center;
    gap: 0.625rem;
    font-size: 0.8125rem;
  }

  .handler-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .handler-name {
    flex: 1;
    color: var(--text);
    font-weight: 500;
  }

  .handler-stat {
    color: var(--muted);
    min-width: 60px;
    text-align: right;
  }

  .handler-rate {
    min-width: 60px;
    text-align: right;
    color: var(--muted);
  }

  .handler-rate--warn {
    color: #f87171;
  }

  /* ── Causal traces ── */
  .trace-list {
    display: flex;
    flex-direction: column;
  }

  .trace-row {
    display: flex;
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
    background: var(--surface-hover, rgba(255,255,255,0.04));
  }

  .trace-arrow {
    font-size: 0.625rem;
    color: var(--muted);
    width: 10px;
    flex-shrink: 0;
  }

  .trace-type {
    font-weight: 600;
    color: #38bdf8;
    flex: 1;
  }

  .trace-badge {
    font-size: 0.6875rem;
    padding: 0.125rem 0.375rem;
    border-radius: 4px;
    background: rgba(96,165,250,0.15);
    color: #60a5fa;
    white-space: nowrap;
  }

  .trace-badge--mod {
    background: rgba(251,146,60,0.15);
    color: #fb923c;
  }

  .trace-age {
    color: var(--muted);
    font-size: 0.75rem;
    white-space: nowrap;
    margin-left: auto;
  }

  .trace-detail {
    padding: 0.5rem 0.75rem 0.5rem 1.5rem;
    background: rgba(255,255,255,0.025);
    border-left: 2px solid var(--border);
    margin: 0 0 0.25rem 1rem;
    border-radius: 0 6px 6px 0;
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
    font-family: monospace;
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
    border-radius: 4px;
    background: rgba(52,211,153,0.15);
    color: #34d399;
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
  .anomaly {
    padding: 0.5rem 0.75rem;
    border-radius: 6px;
    background: rgba(251,191,36,0.1);
    border-left: 3px solid #fbbf24;
    font-size: 0.8125rem;
    margin-bottom: 0.5rem;
    color: var(--text);
  }

  .anomaly--alert {
    background: rgba(248,113,113,0.1);
    border-left-color: #f87171;
  }

  .snapshot-ts {
    font-size: 0.75rem;
    color: var(--muted);
    margin: 0.375rem 0 0;
  }

  /* ── Context detail ── */
  .ctx-detail {
    padding: 0.5rem 0;
    font-size: 0.8125rem;
  }

  .ctx-breakdown {
    margin-bottom: 0.75rem;
  }

  .ctx-breakdown-title {
    font-size: 0.6875rem;
    font-weight: 600;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 0.375rem;
  }

  .ctx-bar-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.1875rem;
    font-size: 0.75rem;
  }

  .ctx-bar-label {
    min-width: 130px;
    color: var(--muted);
  }

  .ctx-bar-track {
    flex: 1;
    height: 5px;
    background: var(--border);
    border-radius: 3px;
    overflow: hidden;
  }

  .ctx-bar-fill {
    height: 100%;
    border-radius: 3px;
  }

  .ctx-bar-value {
    min-width: 90px;
    text-align: right;
    color: var(--text);
  }

  .ctx-meta {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
    gap: 0.375rem 0.75rem;
    font-size: 0.75rem;
    margin-bottom: 0.5rem;
  }

  .ctx-tags {
    font-size: 0.6875rem;
    color: var(--muted);
    margin: 0.25rem 0 0;
    line-height: 1.5;
  }

  /* ── Recent-call drill-down (context text + raw payload) ── */
  .ctx-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-top: 0.75rem;
  }
  .ctx-btn {
    font-size: 0.6875rem;
    padding: 0.25rem 0.75rem;
    background: var(--surface-2, var(--border));
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 4px;
    cursor: pointer;
    min-height: 28px;
  }
  .ctx-btn:hover { border-color: var(--accent); }
  .ctx-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  .ctx-btn.ghost { background: transparent; color: var(--muted); }
  .ctx-err { color: var(--red); font-size: 0.6875rem; margin: 0.5rem 0 0; }
  .ctx-loading { font-size: 0.75rem; margin: 0.5rem 0 0; }
  .ctx-section { margin-top: 0.5rem; }
  .ctx-section > summary {
    display: flex;
    justify-content: space-between;
    align-items: center;
    cursor: pointer;
    font-size: 0.75rem;
    padding: 0.25rem 0;
  }
  .ctx-section-name { font-weight: 600; color: #38bdf8; }
  .ctx-pre {
    max-height: 400px;
    overflow: auto;
    font-size: 0.6875rem;
    line-height: 1.5;
    padding: 0.5rem 0.75rem;
    margin: 0.375rem 0 0;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    white-space: pre-wrap;
    word-break: break-word;
    color: var(--text);
    font-family: var(--font-mono, monospace);
  }
  .ctx-payload { max-height: 500px; }

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

  .status-msg.error {
    color: var(--red, #ef4444);
  }
</style>
