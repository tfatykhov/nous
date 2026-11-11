<script lang="ts">
  import { apiGet } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import type { ActivityData, HarnessData } from '../lib/types/api';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';
  import FilterBar from '../lib/ui/FilterBar.svelte';
  import DataTable from '../lib/ui/DataTable.svelte';
  import StatGrid from '../lib/ui/StatGrid.svelte';
  import Chart from '../lib/viz/Chart.svelte';
  import { Button } from '../lib/ui/button';
  import { lineLegendLabels } from '../lib/chartColors';
  import { ruleVerdict, claimVerdict, fmtUtc, FLAG_GLOSS, type Verdict } from '../lib/harness';

  // Harness dashboard §3.3 / §4.3: what each rule flagged — the evidence for
  // moving it from warn to enforce. Never summed across rules: one call in
  // warn mode can be flagged by the offered-tool rule AND the context policy.
  let windowSel = $state('7d');
  const store = usePoll(
    makePollStore<HarnessData>(
      (signal) => apiGet<HarnessData>(`/dashboard/harness?window=${windowSel}`, { signal }),
      60_000,
    ),
  );
  let first = true;
  $effect(() => {
    void windowSel;
    if (first) { first = false; return; }
    void store.refresh();
  });

  // Most Active Censors and the censor numbers, moved here from the Activity screen (F108). Fetched once per open,
  // as Activity did; its retry reloads it. Its 7-day numbers do not follow the window above.
  const activity = usePoll(
    makePollStore<ActivityData>(
      (signal) => apiGet<ActivityData>('/dashboard/activity?hours=168', { signal }),
      0, // fetch-once
    ),
  );

  const WIN_LABEL: Record<string, string> = { '24h': '24 h', '7d': '7 d', '30d': '30 d' };
  // Series colours outside the status set (spec §4.1), as tokens: Chart.svelte resolves them against the canvas's
  // ground, and the DOM paints them through --fill (dark: the old literals; claims is the ramp's cyan, not a teal).
  const SERIES_TOKEN = { offered_set: '--series-1', context_policy: '--series-4', claims: '--series-9' };
  const seriesVar = (rule: string) => `var(${SERIES_TOKEN[rule as keyof typeof SERIES_TOKEN] ?? '--muted'})`;
  const RULE_LABEL: Record<string, string> = {
    offered_set: 'Offered-tool rule', context_policy: 'Context policy', claims: 'Claim check',
  };

  // The top pattern for the mode a verdict describes: an enforce-mode
  // pattern is not what warn "would refuse" (patterns carry their own mode).
  function top(d: HarnessData, rule: string, mode: string | null) {
    return d.patterns.find((p) => p.rule === rule && p.mode === (mode ?? 'warn')) ?? null;
  }

  function cards(d: HarnessData) {
    const w = WIN_LABEL[d.window] ?? d.window;
    const off = d.rules.offered_set;
    const pol = d.rules.context_policy;
    const cl = d.rules.claims;
    const sum = (m: Record<string, number>) => Object.values(m).reduce((a, b) => a + b, 0);
    const bars = (rows: { key: string; count: number }[]) => {
      const max = Math.max(1, ...rows.map((r) => r.count));
      return rows.slice(0, 5).map((r) => ({ ...r, pct: Math.round((r.count / max) * 100) }));
    };
    return [
      {
        id: 'offered_set', title: 'Offered-tool rule', env: 'NOUS_TOOL_OFFERED_SET_ENFORCEMENT_MODE',
        mode: off.mode ?? 'off', total: sum(off.by_mode), totalLabel: 'calls to a tool the turn was not offered',
        since: d.events_persisted && off.first_event_at ? `First flag ${fmtUtc(off.first_event_at)}` : '',
        breakdownLabel: 'By context', breakdown: bars(off.by_context),
        verdict: ruleVerdict(off, w, d.events_persisted, top(d, 'offered_set', off.mode)),
      },
      {
        id: 'context_policy', title: 'Context policy', env: 'NOUS_TOOL_CONTEXT_POLICY_MODE',
        mode: pol.mode ?? 'off', total: sum(pol.by_mode), totalLabel: 'calls outside what their context may do',
        since: d.events_persisted && pol.first_event_at ? `First flag ${fmtUtc(pol.first_event_at)}` : '',
        breakdownLabel: 'By flag', breakdown: bars(pol.by_violation),
        verdict: ruleVerdict(pol, w, d.events_persisted, top(d, 'context_policy', pol.mode)),
      },
      {
        id: 'claims', title: 'Claim checks', env: 'NOUS_CLAIM_VERIFICATION_MODE',
        mode: cl.mode ?? 'off',
        total: cl.by_evidence.exact + cl.by_evidence.plausible + cl.by_evidence.none,
        totalLabel: 'completion claims checked',
        since: !d.events_persisted ? ''
          : cl.legacy.events && cl.evidence_since
            ? `Evidence levels recorded since ${fmtUtc(cl.evidence_since)} · ${cl.legacy.events} older checks without them`
            : cl.legacy.events ? `${cl.legacy.events} older checks without evidence levels`
              // No pre-2c check in view: evidence levels began before the
              // window, so there is no start date to give — only coverage.
              : sum(cl.by_mode) ? 'Every check in this window recorded evidence levels' : '',
        breakdownLabel: 'By evidence',
        breakdown: bars(['exact', 'plausible', 'none'].map((k) => ({
          key: k, count: cl.by_evidence[k as 'exact' | 'plausible' | 'none'],
        }))),
        verdict: claimVerdict(cl, d.events_persisted),
      },
    ] as {
      id: keyof typeof SERIES_TOKEN; title: string; env: string; mode: string; total: number; totalLabel: string;
      since: string; breakdownLabel: string; breakdown: { key: string; count: number; pct: number }[]; verdict: Verdict;
    }[];
  }

  function chartData(d: HarnessData) {
    // null = not measured: Chart.js breaks the line there (spanGaps off).
    // A distinct dash per rule — the three colours are close in brightness.
    const line = (label: string, key: 'offered_set' | 'context_policy' | 'claims_none', color: string, dash: number[]) => ({
      label, data: d.daily.map((x) => x[key]), borderColor: color, backgroundColor: color,
      borderWidth: 2, borderDash: dash, pointRadius: 2, tension: 0.2, fill: false, spanGaps: false,
    });
    return {
      labels: d.daily.map((x) => x.date.slice(5)),
      datasets: [
        line('Offered-tool rule', 'offered_set', seriesVar('offered_set'), []),
        line('Context policy', 'context_policy', seriesVar('context_policy'), [6, 4]),
        line('Claims without evidence', 'claims_none', seriesVar('claims'), [2, 3]),
      ],
    };
  }

  // Explicit axis colours (today's under dark) beat Chart.svelte's paper defaults, so they are the paper tokens' too.
  const chartOptions = {
    // The legend samples each line with its dash (a box outline turns four corners); see Rubric.svelte.
    plugins: { legend: { labels: lineLegendLabels({ color: 'var(--muted)' }) } },
    scales: {
      x: { ticks: { color: 'var(--muted)' }, grid: { color: 'var(--border)' } },
      y: { beginAtZero: true, ticks: { color: 'var(--muted)', precision: 0 }, grid: { color: 'var(--border)' } },
    },
  };

  const patternCols = [
    { key: 'rule', label: 'Rule' },
    { key: 'context', label: 'Context' },
    { key: 'tool', label: 'Tool' },
    { key: 'violation', label: 'Flag' },
    { key: 'count', label: 'Count' },
    { key: 'last_seen', label: 'Last seen' },
  ];
</script>

<header class="view-head">
  <div>
    <h1>Guards</h1>
    <p class="subtitle">What each safety rule flagged — the evidence for moving it from warn to enforce</p>
  </div>
  <div class="head-right">
    <FilterBar label="Window" required bind:value={windowSel}
      options={[{ value: '24h', label: '24 h' }, { value: '7d', label: '7 d' }, { value: '30d', label: '30 d' }]} />
    <StaleBadge state={$store} />
  </div>
</header>

{#if $store.data}
  {@const d = $store.data}

  {#if !d.events_persisted}
    <div class="notice" role="status">
      Event persistence is off (<code>NOUS_F026_PERSISTENCE_ENABLED=false</code>), so nothing below was measured.
      Zero here means “not recorded”, not “nothing happened”.
    </div>
  {/if}

  <div class="rules">
    {#each cards(d) as c (c.id)}
      <section class="card" aria-labelledby="rule-{c.id}">
        <div class="card-head">
          <div class="min0">
            <h2 id="rule-{c.id}">{c.title}</h2>
            <div class="env">{c.env}</div>
          </div>
          <span class="mode-pill mode-{c.mode}">{c.mode}</span>
        </div>
        <div>
          <div class="big">{d.events_persisted ? c.total : '—'}</div>
          <div class="small muted">{c.totalLabel}</div>
          {#if c.since}<div class="small muted">{c.since}</div>{/if}
        </div>
        {#if d.events_persisted && c.breakdown.some((b) => b.count > 0)}
          <div class="breakdown">
            <div class="label">{c.breakdownLabel}</div>
            {#each c.breakdown as b (b.key)}
              <div class="bar-row">
                <span class="bar-key" title={FLAG_GLOSS[b.key] ?? ''}>{b.key}</span>
                <div class="bar"><div class="bar-fill" style:width="{b.pct}%" style:--fill={seriesVar(c.id)}></div></div>
                <span class="bar-count">{b.count}</span>
              </div>
            {/each}
          </div>
        {/if}
        <div class="verdict verdict-{c.verdict.tone}">
          <div class="verdict-title">{c.verdict.title}</div>
          <div class="verdict-detail">{c.verdict.detail}</div>
        </div>
      </section>
    {/each}
  </div>

  <section class="panel">
    <h2>Flags per day</h2>
    <p class="small muted">One line per rule — a call can be flagged by more than one rule, so the lines are never added up.</p>
    {#if !d.events_persisted}
      <p class="empty">Not measured — event persistence is off.</p>
    {:else}
      <div aria-hidden="true">
        <Chart type="line" data={chartData(d)} options={chartOptions} height="220px" />
      </div>
      <p class="small muted">A gap is a day the record cannot vouch for. The offered-tool rule and the context policy write only when they flag a call, so before a rule's first flag a quiet day may be clean or may predate the rule; a rule that is off records nothing. Claim checks write every turn.</p>
      <!-- A table cannot be 1 px wide: the box hides it, the table stays readable to a screen reader. -->
      <div class="sr-only"><table>
        <caption>Flags per day, per rule</caption>
        <thead><tr><th>Day</th><th>Offered-tool rule</th><th>Context policy</th><th>Claims without evidence</th></tr></thead>
        <tbody>
          {#each d.daily as day (day.date)}
            <tr>
              <td>{day.date}</td>
              <td>{day.offered_set ?? 'no record'}</td>
              <td>{day.context_policy ?? 'no record'}</td>
              <td>{day.claims_none ?? 'no record'}</td>
            </tr>
          {/each}
        </tbody>
      </table></div>
    {/if}
  </section>

  <section class="panel">
    <div class="panel-head">
      <h2>Top patterns</h2>
      <span class="small muted">Fix the source, or accept the refusal, before enforcing.</span>
    </div>
    <!-- The server reads no events when persistence is off; gate here too, so
         no payload can put evidence under "nothing below was measured". -->
    {#if !d.events_persisted || d.patterns.length === 0}
      <p class="empty">{d.events_persisted ? 'Nothing flagged in this window.' : 'Not measured — event persistence is off.'}</p>
    {:else}
      <DataTable columns={patternCols} rows={d.patterns} mode="cards"
        rowKey={(p: HarnessData['patterns'][number]) => JSON.stringify([p.rule, p.mode, p.context, p.tool, p.violation])}
        rowLabel={(p: HarnessData['patterns'][number]) => `${RULE_LABEL[p.rule]} ${p.violation}`}>
        {#snippet cell(p: HarnessData['patterns'][number], c: { key: string })}
          {#if c.key === 'rule'}
            <span class="rule-pill" style:--fill={seriesVar(p.rule)}>{RULE_LABEL[p.rule]}</span>
          {:else if c.key === 'context'}
            <span class="small muted">{p.context ?? '—'}</span>
          {:else if c.key === 'tool'}
            <span class="mono">{p.tool ?? '—'}</span>
          {:else if c.key === 'violation'}
            <span class="mono" title={FLAG_GLOSS[p.violation] ?? ''}>{p.violation}</span>
          {:else if c.key === 'count'}
            <strong>{p.count}</strong>
          {:else if c.key === 'last_seen'}
            <span class="small muted">{fmtUtc(p.last_seen)}</span>
          {/if}
        {/snippet}
        {#snippet detail(p: HarnessData['patterns'][number])}
          <dl class="detail">
            <div><dt>Mode when flagged</dt><dd>{p.mode}</dd></div>
            <div><dt>Latest session</dt><dd class="mono">{p.latest_session ?? '—'}</dd></div>
            {#if p.snippet}<div><dt>Claim</dt><dd>“{p.snippet}”</dd></div>{/if}
            <div><dt>What it means</dt><dd>{FLAG_GLOSS[p.violation] ?? '—'}</dd></div>
          </dl>
        {/snippet}
      </DataTable>
    {/if}
    <dl class="glossary">
      {#each Object.entries(FLAG_GLOSS) as [code, text] (code)}
        <div><dt class="mono">{code}</dt><dd>{text}</dd></div>
      {/each}
    </dl>
  </section>

  <section class="panel order">
    <div>
      <h2>Switch in this order</h2>
      <p class="small muted">A call the offered-tool rule refuses under enforce never reaches the context policy, so switching that rule first hides calls from the policy's evidence.</p>
    </div>
    <ol>
      <li><strong>Move context policy to enforce</strong><code>NOUS_TOOL_CONTEXT_POLICY_MODE=enforce</code></li>
      <li><strong>Move the offered-tool rule to enforce</strong><code>NOUS_TOOL_OFFERED_SET_ENFORCEMENT_MODE=enforce</code></li>
    </ol>
  </section>
{:else if $store.error}
  <p class="state-msg error">Failed to load harness data — retrying…</p>
{:else}
  <p class="state-msg">Loading…</p>
{/if}

<!-- Most Active Censors, with the censor numbers (moved from the Activity screen, F108) -->
<section class="panel censors">
  <div class="section-head">
    <h2>Most Active Censors</h2>
    <Button variant="outline" size="lg" class="px-4 max-md:min-h-11" onclick={() => void activity.refresh()} disabled={$activity.loading}>
      {$activity.loading ? 'Loading…' : 'Refresh'}
    </Button>
  </div>
  {#if $activity.data}
    {@const cs = $activity.data.censor_stats}
    <p class="small muted">Numbers marked (7d) are the last 7 days, whatever the window above. The count beside each censor is its all-time activations.</p>
    <StatGrid stats={[
      { label: 'Censor Activations (7d)',  value: cs.total_activations_7d },
      { label: 'Auto-created Censors',     value: cs.auto_created },
      { label: 'Manual Censors',           value: cs.manual_created },
      { label: 'False Positives (7d)',     value: cs.false_positives_7d },
    ]} />
    {#if cs.top_censors.length > 0}
      <ul class="item-list">
        {#each cs.top_censors as c}
          <li class="item-row">
            <span class="item-label" title={c.trigger_pattern ?? ''}>
              {c.trigger_pattern ?? c.id}
            </span>
            <span class="badge">{c.activations}</span>
          </li>
        {/each}
      </ul>
    {:else}
      <p class="empty">No censor activations.</p>
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

<style>
  .view-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; flex-wrap: wrap; margin-bottom: 1.25rem; }
  .head-right { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
  h1 { font-size: 1.375rem; font-weight: 700; margin: 0 0 0.25rem; }
  :global(html:root .page-ground) h1 { font-size: 30px; font-weight: 650; letter-spacing: -0.022em; line-height: 1.15; }
  h2 { font-size: 0.9375rem; font-weight: 600; margin: 0; }
  .subtitle { font-size: 0.8125rem; color: var(--muted); margin: 0; }
  .notice { padding: 0.75rem 1rem; border-radius: var(--card-radius-sm); background: var(--notice-bg);
    border: 1px solid var(--notice-border); font-size: 0.8125rem; margin-bottom: 1rem; }
  .notice code, .order code, .env, .mono { font-family: var(--font-mono); font-size: 0.75rem; }
  .rules { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1rem; }
  .card, .panel { background: var(--surface); border: var(--card-border); border-radius: var(--card-radius); box-shadow: var(--card-shadow);
    padding: 1.25rem; }
  .card { display: flex; flex-direction: column; gap: 0.875rem; }
  .panel { margin-top: 1.25rem; }
  .panel-head { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; flex-wrap: wrap; margin-bottom: 0.75rem; }
  .card-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 0.75rem; }
  .min0 { min-width: 0; }
  .env { color: var(--muted); margin-top: 0.125rem; overflow-wrap: anywhere; }
  .mode-pill { flex-shrink: 0; font-size: 0.6875rem; font-weight: 600; padding: 0.125rem 0.5rem; border-radius: 999px; border: 1px solid currentColor; }
  .mode-enforce { color: var(--stat-ok); }
  .mode-warn, .mode-shadow { color: var(--stat-warn); }
  .mode-off { color: var(--muted); border-color: var(--border); }
  .big { font-size: 2rem; font-weight: 700; line-height: 1.1; }
  .breakdown { display: flex; flex-direction: column; gap: 0.5rem; }
  .label { font-size: 0.6875rem; font-weight: 600; color: var(--muted); text-transform: var(--label-transform);
    letter-spacing: var(--label-tracking); }
  .bar-row { display: grid; grid-template-columns: minmax(0, 9.5rem) minmax(0, 1fr) 2.25rem; gap: 0.625rem; align-items: center; }
  .bar-key { font-family: var(--font-mono); font-size: 0.75rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .bar { height: 8px; border-radius: 4px; background: var(--border); overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 4px; background: var(--fill); }
  .bar-count { font-size: 0.8125rem; font-weight: 600; text-align: right; }
  .verdict { padding: 0.75rem 0.875rem; border-radius: var(--card-radius-sm); border: 1px solid; }
  /* Status by token: the alphas of the old literals are the mix percentages (dark identical). */
  .verdict-warn { color: var(--stat-warn); border-color: color-mix(in srgb, var(--stat-warn) 35%, transparent);
    background: color-mix(in srgb, var(--stat-warn) 8%, transparent); }
  .verdict-ok { color: var(--stat-ok); border-color: color-mix(in srgb, var(--stat-ok) 35%, transparent);
    background: color-mix(in srgb, var(--stat-ok) 8%, transparent); }
  .verdict-muted { color: var(--muted); border-color: var(--border); background: transparent; }
  .verdict-title { font-size: 0.8125rem; font-weight: 700; }
  .verdict-detail { font-size: 0.8125rem; color: var(--text); margin-top: 0.125rem; }
  /* The rule's colour is its dot and border (a series colour is a mark); its name is the ink. */
  .rule-pill { display: inline-flex; align-items: center; gap: 0.375rem; padding: 2px 8px; border-radius: 999px; font-size: 0.6875rem;
    font-weight: 600; color: var(--text); border: 1px solid color-mix(in srgb, var(--fill) 40%, transparent); white-space: nowrap; }
  .rule-pill::before { content: ''; width: 8px; height: 8px; border-radius: 50%; background: var(--fill); flex-shrink: 0; }
  .detail { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 0.5rem 1.5rem; margin: 0; padding: 0.5rem 0; }
  .detail dt { font-size: 0.6875rem; font-weight: 600; color: var(--muted); text-transform: var(--label-transform);
    letter-spacing: var(--label-tracking); }
  .detail dd { margin: 0.125rem 0 0; font-size: 0.8125rem; }
  .glossary { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 0.375rem 1.5rem; margin: 1rem 0 0; font-size: 0.75rem; }
  .glossary div { display: flex; gap: 0.5rem; }
  .glossary dt { white-space: nowrap; }
  .glossary dd { margin: 0; color: var(--muted); }
  .order { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1.5rem; align-items: start; }
  .order ol { margin: 0; padding-left: 1.25rem; display: flex; flex-direction: column; gap: 0.75rem; font-size: 0.875rem; }
  .order code { display: block; color: var(--muted); margin-top: 0.25rem; overflow-wrap: anywhere; }
  .empty { color: var(--muted); font-size: 0.875rem; text-align: center; padding: 1.5rem 0; margin: 0; }
  .empty.error { color: var(--err-text); }
  /* Most Active Censors (from Activity) */
  .censors { display: flex; flex-direction: column; gap: 0.75rem; }
  .section-head { display: flex; align-items: center; justify-content: space-between; gap: 0.75rem; }
  ul.item-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0; }
  .item-row { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; padding: 0.5rem 0;
    border-bottom: 1px solid var(--border); font-size: 0.8125rem; }
  .item-row:last-child { border-bottom: none; }
  .item-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; color: var(--text); }
  .badge { font-size: 0.75rem; font-weight: 600; color: var(--accent); background: color-mix(in srgb, var(--accent) 12%, transparent);
    border-radius: 999px; padding: 0.125rem 0.5rem; white-space: nowrap; flex-shrink: 0; }
  .retry-link { background: none; border: none; color: var(--accent); cursor: pointer; font-size: inherit; padding: 0;
    text-decoration: underline; }
  .small { font-size: 0.75rem; }
  .muted { color: var(--muted); }
  .state-msg { margin-top: 2rem; text-align: center; color: var(--muted); font-size: 0.875rem; }
  .state-msg.error { color: var(--err-text); }
  .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
</style>
