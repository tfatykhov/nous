<script lang="ts">
  import { onMount } from 'svelte';
  import { apiGet } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import type { StatusData, StatusTimeseriesPoint, ExecutionData, WorkToday, HeartbeatTrackedFinding } from '../lib/types/api';
  import { attentionPoll } from '../lib/stores/attention';
  import { fmtUtc, relUntil } from '../lib/harness';
  import { routeHref } from '../lib/router';
  import { fmtTokens } from '../lib/work';
  import { fmtSeconds } from '../lib/frames';
  import { hhmm, utcMinute } from '../lib/dayTimeline';
  import { look } from '../lib/look';
  import { chartPalette, readToken } from '../lib/overviewCharts';
  import StatGrid from '../lib/ui/StatGrid.svelte';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';
  import { Button } from '../lib/ui/button';
  import { Badge } from '../lib/ui/badge';
  import Chart from '../lib/viz/Chart.svelte';
  import DayTimeline from '../lib/viz/DayTimeline.svelte';

  // The Overview: what needs the owner, today's numbers and health, a compact timeline of today's turns, then the
  // memory numbers. Every card reads data the dashboard already serves (nothing authored, nothing invented): the
  // attention counts (the app-wide poll), the open heartbeat findings, GET /dashboard/work/today, /status and the
  // ledger's 24 h numbers. Each loads once (Refresh reloads); a part that fails says so and the rest still renders.
  // Not shown, for want of cheap data: live companion surfaces (the index has no "awaiting the owner" flag, and the
  // DAG questions it would duplicate are already counted), retrieval latency, write-health findings.

  // Load once: interval 0 never auto-fires. StaleBadge still gets lastUpdated from the initial tick.
  const store = usePoll(
    makePollStore<StatusData>(
      (signal) => apiGet<StatusData>('/status?dashboard=true', { signal }),
      0, // fetch-once (manual refresh only)
    ),
  );

  // Harness dashboard §4: the durable ledger's 24 h numbers (fetch-once, like the rest of the Overview). The
  // attention cards read the app-wide poll.
  const exec = usePoll(
    makePollStore<ExecutionData>(
      (signal) => apiGet<ExecutionData>('/dashboard/execution?window=24h&limit=1', { signal }),
      0,
    ),
  );

  const today = usePoll(makePollStore<WorkToday>((signal) => apiGet<WorkToday>('/dashboard/work/today', { signal }), 0));

  // The heartbeat's in-memory finding store; 503 when the heartbeat is not running (one retry, then it says so).
  type FindingsData = { findings: HeartbeatTrackedFinding[] };
  const findings = usePoll(
    makePollStore<FindingsData>((signal) => apiGet<FindingsData>('/heartbeat/findings', { signal, retries: 1 }), 0),
  );

  function refreshAll() {
    void store.refresh();
    void exec.refresh();
    void attentionPoll.refresh();
    void today.refresh();
    void findings.refresh();
  }

  // The day rolls over at 00:00 UTC: refetch then, and when the tab comes back after it, so a page left open never
  // calls yesterday "Today".
  const utcDate = () => new Date().toISOString().slice(0, 10);
  function checkDay() {
    const day = $today.data?.day;
    if (day && day !== utcDate()) refreshAll();
  }
  onMount(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const arm = () => {
      const now = new Date();
      const next = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1);
      timer = setTimeout(() => {
        checkDay();
        arm();
      }, next - now.getTime() + 1000);
    };
    arm();
    const onVisible = () => {
      if (document.visibilityState === 'visible') checkDay();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  });

  // ── Helpers ───────────────────────────────────────────────────────────

  function fmt(n: number): string {
    return n.toLocaleString('en-US');
  }

  const plural = (n: number, one: string, many: string) => `${fmt(n)} ${n === 1 ? one : many}`;

  function fmtWithDelta(value: number, delta: number | null | undefined): string {
    if (delta == null) return fmt(value);
    if (delta === 0) return fmt(value);
    const sign = delta > 0 ? '+' : '';
    return `${fmt(value)} (${sign}${delta} 7d)`;
  }

  /**
   * Rows the daily timeseries counts in all, for the growth summary line. The series has one point per calendar day
   * from date(now − 30 days) through today (31 days), and counts rows by creation day, later deactivated ones included.
   */
  function seriesTotal(points: StatusTimeseriesPoint[]): number {
    return points.reduce((s, p) => s + p.count, 0);
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

  // The charts' colours come from the ground's tokens, read off the chart row itself (a canvas cannot read var()),
  // and again whenever the look changes: today's colours exactly under dark, the paper palette under paper.
  let chartRow = $state<HTMLElement>();
  let palette = $derived.by(() => {
    void $look;
    const el = chartRow;
    return el ? chartPalette((name) => readToken(el, name)) : null;
  });

  // ── Today ─────────────────────────────────────────────────────────────

  let td = $derived($today.data);
  let tt = $derived(td && td.enabled ? td.totals : null);
  /** `Today, 10 November` from the endpoint's UTC day, else the browser's UTC date. */
  let dayTitle = $derived.by(() => {
    const day = td?.day ?? new Date().toISOString().slice(0, 10);
    const d = new Date(`${day}T00:00:00Z`);
    return Number.isNaN(d.getTime()) ? 'Today' : `Today, ${d.toLocaleDateString('en-GB', { day: 'numeric', month: 'long', timeZone: 'UTC' })}`;
  });
  let nowText = $derived.by(() => {
    const m = utcMinute(td?.now);
    return m === null ? null : `Now ${hhmm(m)} UTC`;
  });
  let soFar = $derived.by(() => {
    if (!tt) return [];
    const rows: [string, string | null][] = [
      ['Owner turns', fmt(tt.owner_turns)],
      ['Background turns', fmt(tt.background_turns)],
      ['Tool calls', fmt(tt.tool_calls)],
      ['Errors', fmt(tt.errors)],
      ['Median turn', tt.median_seconds == null ? null : fmtSeconds(tt.median_seconds)],
      ['Model calls', tt.model_calls == null ? null : fmt(tt.model_calls)],
      ['Input tokens', tt.input_tokens == null ? null : fmtTokens(tt.input_tokens)],
      ['Cache reads', tt.cache_read_tokens == null ? null : fmtTokens(tt.cache_read_tokens)],
    ];
    return rows.filter((r): r is [string, string] => r[1] !== null);
  });

  // ── Needs you ─────────────────────────────────────────────────────────

  // Open = new or acknowledged, not resolved: both are "in front of the owner" in the finding store's own words.
  // The heartbeat acknowledges a finding itself when it routes it to triage, so the two states are not the owner's
  // and both get the one badge, Open. Suppressed, auto-closed and resolved ones are left out. High urgency first,
  // then the most recently seen.
  const URGENCY: Record<string, number> = { high: 0, normal: 1, low: 2 };
  const FINDINGS_SHOWN = 3;
  let openFindings = $derived(
    ($findings.data?.findings ?? [])
      .filter((f) => f.state === 'new' || f.state === 'acknowledged')
      .sort((a, b) => (URGENCY[a.urgency] ?? 3) - (URGENCY[b.urgency] ?? 3) || (Date.parse(b.last_seen ?? '') || 0) - (Date.parse(a.last_seen ?? '') || 0)),
  );

  let a = $derived($attentionPoll.data);

  // ── Health ────────────────────────────────────────────────────────────

  type Rule = { mode: string | null; warn_7d: number; refused_7d: number };
  const ruleCount = (r: Rule) => (r.mode === 'enforce' ? r.refused_7d : r.mode === 'off' ? 0 : r.warn_7d);
</script>

<header class="view-head">
  <h1 class="title">{dayTitle}</h1>
  <div class="head-right">
    <StaleBadge state={$store} />
    <Button variant="outline" size="lg" class="px-4 max-md:min-h-11" onclick={refreshAll} disabled={$store.loading}>
      {$store.loading ? 'Loading…' : 'Refresh'}
    </Button>
  </div>
</header>
{#if tt}
  <div class="meta">
    <span>{plural(tt.owner_turns + tt.background_turns, 'turn', 'turns')}</span>
    <span>{plural(tt.tool_calls, 'tool call', 'tool calls')}</span>
    {#if tt.model_calls != null}<span>{plural(tt.model_calls, 'model call', 'model calls')}</span>{/if}
    <span class:bad={tt.errors > 0}>{plural(tt.errors, 'error', 'errors')}</span>
    {#if nowText}<span>{nowText}</span>{/if}
  </div>
{/if}

<div class="cards">
  <!-- ── Needs you (harness dashboard §4.6, plus the open heartbeat findings) ── -->
  <section class="card sec" aria-labelledby="needs-h">
    <h2 id="needs-h">Needs you</h2>
    {#if !a}
      <p class="muted">{$attentionPoll.error ? "Questions and sends in doubt could not be loaded." : "Loading…"}</p>
    {:else}
      {#if a.questions_waiting > 0 || a.sends_in_doubt > 0 || openFindings.length > 0}
        <ul class="items">
          {#if a.questions_waiting > 0}
            <li class="item">
              <Badge variant="warn">Waiting</Badge>
              <a href={routeHref('dag')}>{a.questions_waiting} question{a.questions_waiting === 1 ? '' : 's'} waiting on you</a>
              {#if a.next}
                <span class="k">Next deadline {fmtUtc(a.next.deadline)} ({relUntil(a.next.deadline)}), {a.next.dag_name}, {a.next.node_name}. No answer by then applies ‘{a.next.default_label}’.</span>
              {/if}
            </li>
          {/if}
          {#if a.sends_in_doubt > 0}
            <li class="item">
              <Badge variant="warn">Check</Badge>
              <a href={routeHref('work', 'changes')}>{a.sends_in_doubt} send{a.sends_in_doubt === 1 ? '' : 's'} ended without confirming delivery</a>
              {#if a.latest_in_doubt}
                <span class="k">
                  {a.latest_in_doubt.tool_name}{a.latest_in_doubt.tombstone ? '' : a.latest_in_doubt.recipients.length ? ` to ${a.latest_in_doubt.recipients.join(', ')}` : ''}{a.latest_in_doubt.dag_name ? `, ${a.latest_in_doubt.dag_name}` : ''}, {fmtUtc(a.latest_in_doubt.created_at)}. Retries of it are held until you check.
                </span>
              {/if}
            </li>
          {/if}
          {#each openFindings.slice(0, FINDINGS_SHOWN) as f (f.fingerprint)}
            <li class="item">
              <Badge variant={f.urgency === 'high' ? 'error' : 'warn'}>Open</Badge>
              <a class="wrap" href={routeHref('heartbeat')}>{f.summary}</a>
              <span class="k">Heartbeat finding from {f.check_name}, {f.urgency} urgency. Last seen {fmtUtc(f.last_seen)}.</span>
            </li>
          {/each}
        </ul>
        {#if openFindings.length > FINDINGS_SHOWN}
          {@const rest = openFindings.length - FINDINGS_SHOWN}
          <a class="more-link" href={routeHref('heartbeat')}>{rest} more open finding{rest === 1 ? '' : 's'} on Heartbeat</a>
        {/if}
      {:else if !a.ledger_persisted}
        <p class="all-clear">
          No questions waiting. Ledger persistence is off (NOUS_EXECUTION_LEDGER_PERSIST_ENABLED), so a send whose delivery went unconfirmed is not recorded — none can be shown here.
        </p>
      {:else if $findings.data}
        <p class="all-clear">Nothing needs you — no questions waiting, no sends in doubt and no open heartbeat findings.</p>
      {:else}
        <p class="all-clear">No questions waiting and no sends in doubt.</p>
      {/if}
      {#if $findings.error}<p class="muted small">Heartbeat findings could not be loaded.</p>{/if}
    {/if}
  </section>

  <!-- ── Today so far ── -->
  <section class="card sec" aria-labelledby="sofar-h">
    <h2 id="sofar-h">Today so far</h2>
    {#if tt}
      <table class="nums">
        <tbody>
          {#each soFar as [label, value] (label)}<tr><th scope="row">{label}</th><td>{value}</td></tr>{/each}
        </tbody>
      </table>
    {:else if td && !td.enabled}
      <p class="muted">Tool-call recording is off, so there are no turn numbers.</p>
    {:else if $today.error}
      <p class="muted">Today's turns are unavailable.</p>
    {:else}
      <p class="muted">Loading…</p>
    {/if}
  </section>

  <!-- ── Health ── -->
  <section class="card sec" aria-labelledby="health-h">
    <h2 id="health-h">Health</h2>
    {#if !a && $attentionPoll.error}
      <p class="muted">Guards and the ledger could not be loaded.</p>
    {/if}
    {#if a}
      {@const h = a.harness}
      {@const guardsWatch = !h.events_persisted || ruleCount(h.offered_set) + ruleCount(h.context_policy) > 0}
      <div class="item">
        <Badge variant={guardsWatch ? 'warn' : 'ok'}>{guardsWatch ? 'Watch' : 'Clear'}</Badge>
        <span class="what">Guards</span>
        <span class="k">
          {#if !h.events_persisted}
            Not measured: event persistence is off.
          {:else}
            Last 7 days:
            {#each [['Offered-tool rule', h.offered_set], ['Context policy', h.context_policy]] as [name, r], i (name)}
              {@const rule = r as Rule}
              {i ? '; ' : ''}{name}
              {#if rule.mode === 'enforce'} refused <strong>{rule.refused_7d}</strong>
              {:else if rule.mode === 'off'} is off
              {:else} flagged <strong>{rule.warn_7d}</strong> (warn, the calls still ran){/if}
            {/each}.
          {/if}
          <a href={routeHref('harness')}>Open Guards</a>
        </span>
      </div>
      <div class="item">
        <Badge variant={a.ledger_persisted ? 'ok' : 'warn'}>{a.ledger_persisted ? 'Clear' : 'Watch'}</Badge>
        <span class="what">Ledger</span>
        <span class="k">
          {a.ledger_persisted
            ? 'Every side-effecting call is recorded, and repeat sends are refused.'
            : 'Ledger persistence is off (NOUS_EXECUTION_LEDGER_PERSIST_ENABLED): calls are not recorded and sends are not de-duplicated.'}
        </span>
      </div>
    {/if}
    {#if tt}
      <div class="item">
        <Badge variant={tt.errors > 0 ? 'warn' : 'ok'}>{tt.errors > 0 ? 'Watch' : 'Clear'}</Badge>
        <span class="what">Errors today</span>
        <span class="k">
          {#if tt.errors > 0}
            {plural(tt.errors, 'turn', 'turns')} had an error{tt.gated > 0 ? `, ${plural(tt.gated, 'turn', 'turns')} a gated call` : ''}.
            <!-- The Work list's lens finds failed tool calls over the last 24 h: linked only when one failed today. -->
            {#if (tt.tool_error_turns ?? 0) > 0}
              <a href={routeHref('work', '', { lens: 'error', kind: 'any', days: '1' })}>Sessions where a tool failed, last 24 h</a>
            {/if}
          {:else}
            No turn has had an error today{tt.gated > 0 ? `; ${plural(tt.gated, 'turn', 'turns')} had a gated call` : ''}.
          {/if}
        </span>
      </div>
    {/if}
  </section>
</div>

<!-- ── Today's turns: compact, below the cards ── -->
<section class="card field" aria-labelledby="turns-h">
  <h2 id="turns-h">Today's turns</h2>
  {#if td && td.enabled}
    <DayTimeline turns={td.turns} now={td.now} truncated={td.truncated} />
  {:else if td && !td.enabled}
    <p class="muted">Tool-call recording is off. Set NOUS_TOOL_CALL_LOG_ENABLED to record what Nous does.</p>
  {:else if $today.error}
    <p class="muted">Today's turns are unavailable.</p>
  {:else}
    <p class="muted">Loading…</p>
  {/if}
</section>

<h2 class="group-h">Memory and decisions</h2>
{#if $store.data}
  {@const d = $store.data}
  {@const mem = d.memory}
  {@const db = d.dashboard}
  {@const deltas = db.deltas}
  {@const dist = db.distributions}

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
      <a class="detail-link" href={routeHref('work', 'changes')}>Open ledger</a>
    </div>
    {#if x && !x.modes.persist}
      <p class="muted">Ledger persistence is off (NOUS_EXECUTION_LEDGER_PERSIST_ENABLED) — no calls are recorded and sends are not de-duplicated, so there are no numbers to show.</p>
      <StatGrid stats={[
        { label: 'Claim checks',          value: x.modes.claim_verification },
        { label: 'Context policy',        value: x.modes.context_policy },
        { label: 'Offered-tool rule',     value: x.modes.offered_set },
      ]} />
    {:else if x}
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

  <!-- ── Growth: one line; the charts are on Memory health's Growth tab ── -->
  <p class="growth-line">
    Created in the last {db.timeseries.facts.length} days: <strong>{fmt(seriesTotal(db.timeseries.facts))}</strong> facts,
    <strong>{fmt(seriesTotal(db.timeseries.episodes))}</strong> episodes,
    <strong>{fmt(seriesTotal(db.timeseries.decisions))}</strong> decisions.
    <a href={routeHref('health', 'growth')}>Memory growth</a>
  </p>

  <!-- ── Charts ───────────────────────────────────────────────────────── -->
  <div class="chart-row" bind:this={chartRow}>
    <!-- Decision outcomes (doughnut) -->
    <section class="chart-card">
      <h2>Decision outcomes</h2>
      {#if Object.keys(dist.decision_outcomes).length > 0}
        {@const do_ = sortedDistribution(dist.decision_outcomes)}
        {#if palette}
        {@const pal = palette}
        <Chart
          type="doughnut"
          data={{
            labels: do_.labels,
            datasets: [{
              data: do_.values,
              backgroundColor: do_.labels.map((l) => pal.outcome(l)),
              borderWidth: 0,
              hoverOffset: 4,
            }],
          }}
          options={{ cutout: '60%', plugins: { legend: { position: 'bottom', labels: { font: { size: 11 } } } } }}
          height="240px"
        />
        {/if}
      {:else}
        <p class="empty">No decisions recorded yet</p>
      {/if}
    </section>

    <!-- Edge types (horizontal bar) -->
    <section class="chart-card">
      <h2>Edge types</h2>
      {#if Object.keys(dist.edge_relations).length > 0}
        {@const er = sortedDistribution(dist.edge_relations)}
        {#if palette}
        {@const pal = palette}
        <Chart
          type="bar"
          data={{
            labels: er.labels.map((l) => l.replace(/_/g, ' ')),
            datasets: [{ data: er.values, backgroundColor: pal.bar, borderColor: pal.barBorder, borderWidth: 1 }],
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
        {/if}
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
  /* Colours and structure are tokens only (hexGuard): today's values under the dark look, the paper ground's
     rounded white cards under the paper look. */
  .view-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
  .title { font-size: 1.4rem; font-weight: 700; color: var(--text); margin: 0; line-height: 1.2; }
  :global(html[data-look="paper"]) .title { font-size: 30px; font-weight: 650; letter-spacing: -0.022em; line-height: 1.15; }
  .head-right { display: flex; align-items: center; gap: 0.75rem; flex-shrink: 0; margin-left: auto; }
  .meta { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.75rem; font-size: 0.8125rem; }
  .meta > * { padding: 3px 11px; border-radius: 999px; background: var(--surface); border: var(--card-border); box-shadow: var(--card-shadow); color: var(--text); }
  .meta .bad { color: var(--err-text); background: var(--err-bg); border-color: transparent; box-shadow: none; font-weight: 600; }

  h2 { font-size: 0.9375rem; font-weight: 650; color: var(--text); margin: 0 0 0.5rem; }
  .muted { color: var(--muted); font-size: 0.875rem; margin: 0; }
  .small { font-size: 0.8125rem; margin-top: 0.5rem; }
  .wrap { overflow-wrap: anywhere; }
  a { color: var(--accent-text); }
  a:focus-visible, button:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
  .card { background: var(--surface); border: var(--card-border); border-radius: var(--card-radius); box-shadow: var(--card-shadow); min-width: 0; }

  /* Needs you, Today so far, Health */
  .cards { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr) minmax(0, 1fr); gap: 1.25rem; margin-top: 1.25rem; align-items: start; }
  .sec { padding: 1.125rem 1.25rem 0.75rem; }
  .items { list-style: none; margin: 0; padding: 0; }
  .item { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 4px 12px; padding: 0.625rem 0; border-top: 1px solid var(--border); align-items: start; font-size: 0.875rem; }
  .item > :global([data-slot="badge"]) { margin-top: 1px; }
  .item a { font-weight: 550; text-decoration: none; }
  .item a:hover { text-decoration: underline; }
  .item .k { grid-column: 2; font-size: 0.78125rem; color: var(--muted); overflow-wrap: anywhere; }
  .item .k a { font-weight: 600; margin-left: 0.25rem; white-space: nowrap; }
  .item .k strong { color: var(--text); }
  .what { font-weight: 550; }
  .more-link { display: inline-block; margin: 0.25rem 0 0.25rem; font-size: 0.8125rem; font-weight: 600; text-decoration: none; }
  .all-clear { margin: 0; padding: 0.625rem 0; border-top: 1px solid var(--border); font-size: 0.875rem; }
  table.nums { width: 100%; border-collapse: collapse; font-size: 0.875rem; }
  table.nums th, table.nums td { padding: 0.5rem 0; border-top: 1px solid var(--border); }
  table.nums th { text-align: left; font-weight: 400; color: var(--text); }
  table.nums td { text-align: right; font-weight: 650; font-stretch: 82%; font-size: 0.9375rem; }

  /* Today's turns: compact */
  .field { margin-top: 1.25rem; padding: 1rem 1.25rem 0.75rem; }

  /* Memory and decisions: today's content, in cards */
  .group-h { margin: 1.75rem 0 0.75rem; font-size: 1.0625rem; }

  .special-cards {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 1rem;
    margin-top: 1rem;
  }

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
    text-transform: var(--label-transform);
    letter-spacing: var(--label-tracking);
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

  .integrity-section { margin-top: 1rem; }
  .section-title-row { display: flex; align-items: baseline; gap: 0.75rem; margin-bottom: 0.75rem; }
  .section-title-row h2 { margin: 0; }
  .detail-link { font-size: 0.8125rem; color: var(--accent-text); text-decoration: none; margin-left: auto; font-weight: 600; }
  .detail-link:hover { text-decoration: underline; }

  .growth-line { margin: 1rem 0 0; font-size: 0.875rem; color: var(--muted); }
  .growth-line strong { color: var(--text); }
  .growth-line a { font-weight: 600; text-decoration: none; margin-left: 0.25rem; }

  .chart-row { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 1rem; margin-top: 1rem; }
  .chart-card { background: var(--surface); border: var(--card-border); border-radius: var(--card-radius); box-shadow: var(--card-shadow); padding: 1rem 1.25rem; min-width: 0; }

  .empty { color: var(--muted); font-size: 0.875rem; padding: 1.5rem 0; text-align: center; margin: 0; }
  .status-msg { color: var(--muted); font-size: 0.9375rem; padding: 3rem 2rem; text-align: center; }
  .status-msg.error { color: var(--err-text); }
  .retry-link { background: none; border: none; color: var(--accent-text); cursor: pointer; font-size: inherit; padding: 0; text-decoration: underline; }

  @media (max-width: 900px) {
    .cards { grid-template-columns: minmax(0, 1fr); }
  }
  @media (max-width: 640px) {
    .chart-row { grid-template-columns: minmax(0, 1fr); }
    .special-cards { grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 0.75rem; }
    .sec, .field { padding-left: 1rem; padding-right: 1rem; }
  }
</style>
