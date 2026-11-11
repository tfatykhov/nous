<script lang="ts">
  import { apiGet } from '../../lib/api';
  import { makePollStore } from '../../lib/stores/registry';
  import { usePoll } from '../../lib/poll';
  import { currentRoute, currentSub, currentQuery, routeHref } from '../../lib/router';
  import { fmtDays, laneVar } from '../../lib/work';
  import type { WorkKind, WorkPatternsData } from '../../lib/types/api';
  import StaleBadge from '../../lib/ui/StaleBadge.svelte';
  import { Button } from '../../lib/ui/button';

  // F108 Work patterns: which tools are used for which kind of work and why, which fail most, and the wasted steps.
  // Fetched once per open and per window change, plus a Refresh button, no polling. Every string here (tool names,
  // reasons) came from a model and is shown as a text node only.
  const WINDOWS = [7, 14, 30];
  const KINDS: { id: WorkKind; label: string; color: string }[] = [
    { id: 'conversation', label: 'Conversations', color: 'var(--accent-text)' },
    { id: 'task', label: 'Tasks', color: 'var(--kind-task)' },
    { id: 'background', label: 'Background', color: 'var(--muted)' },
  ];

  // Route, sub and query are read together, and never written.
  let days = $derived.by((): number | null => {
    const route = $currentRoute;
    const sub = $currentSub;
    const query = $currentQuery;
    if (route !== 'work' || sub.replace(/\/+$/, '') !== 'patterns') return null;
    const n = Number(query.get('days'));
    return WINDOWS.includes(n) ? n : 7;
  });

  // While the hash is off this screen (a route change is mid-flight) the last window stays.
  let lastDays = 7;
  const currentDays = () => {
    if (days !== null) lastDays = days;
    return lastDays;
  };

  const store = usePoll(
    makePollStore<WorkPatternsData>(
      (signal) => apiGet<WorkPatternsData>(`/dashboard/work/patterns?days=${currentDays()}`, { signal }),
      0, // fetch-once (a window change or Refresh reloads)
    ),
  );

  let first = true;
  $effect(() => {
    if (days === null) return;
    if (first) { first = false; return; }
    void store.refresh();
  });

  function setDays(n: string) {
    location.replace(routeHref('work', 'patterns', { days: n })); // a window is not a page: Back leaves the screen
  }

  let data = $derived($store.data);
  let tools = $derived(data ? [...data.tools].sort((a, b) => b.calls - a.calls) : []);
  let maxima = $derived(
    Object.fromEntries(KINDS.map((k) => [k.id, Math.max(0, ...tools.map((t) => t.by_kind[k.id].calls))])) as Record<WorkKind, number>,
  );

  const fmt = (n: number) => n.toLocaleString('en-US');
  const pct = (n: number) => `${Math.round(n * 100)}%`;
  const width = (n: number, max: number) => `${Math.max(2, Math.round((100 * n) / max))}%`;
  let coverage = $derived(
    data
      ? `Reasons recorded on ${fmt(data.reason_coverage.with_reason)} of ${fmt(data.reason_coverage.calls)} calls` +
        (data.reason_coverage.calls > 0 ? ` (${pct(data.reason_coverage.with_reason / data.reason_coverage.calls)})` : '')
      : '',
  );
</script>

<header class="head">
  <div>
    <h1 class="page-h1">What Nous uses, and what for</h1>
    <p class="subtitle">
      Every tool across all sessions{data ? ` in the last ${fmtDays(data.window_days)}` : ''}. The sessions behind a failing tool are one click away.
    </p>
  </div>
  <div class="head-right">
    <label class="field">
      <span>Window</span>
      <select value={String(days ?? 7)} onchange={(e) => setDays(e.currentTarget.value)}>
        {#each WINDOWS as w (w)}<option value={String(w)}>Last {w} days</option>{/each}
      </select>
    </label>
    <Button variant="outline" size="lg" class="px-4 max-md:min-h-11" onclick={() => void store.refresh()}>Refresh</Button>
    {#if $store.loading}<span class="loading" role="status">Loading…</span>{/if}
    <StaleBadge state={$store} />
  </div>
</header>

{#if data && !data.enabled}
  <div class="notice" role="status">Tool-call recording is off. Set NOUS_TOOL_CALL_LOG_ENABLED to record what Nous does.</div>
{:else if data}
  <section class="card" aria-labelledby="tools-h">
    <div class="card-head">
      <h2 id="tools-h">Tools, by kind of work</h2>
      <span class="muted">Calls per kind of work</span>
    </div>
    {#if tools.length === 0}
      <p class="empty">No tool calls in this window.</p>
    {:else}
      <div class="scroll">
        <table>
          <thead>
            <tr>
              <th scope="col">Tool</th>
              {#each KINDS as k (k.id)}<th scope="col" class="kind">{k.label}</th>{/each}
              <th scope="col" class="why">Common reasons</th>
            </tr>
          </thead>
          <tbody>
            {#each tools as t (t.tool)}
              <tr>
                <th scope="row">
                  <span class="tool"><span class="dot" aria-hidden="true" style:--dot={laneVar(t.lane)}></span><span class="tool-name">{t.tool}</span></span>
                </th>
                {#each KINDS as k (k.id)}
                  {@const n = t.by_kind[k.id].calls}
                  <td>
                    {#if n > 0}
                      <span class="cell"><span class="track"><span class="fill" style:width={width(n, maxima[k.id])} style:--fill={k.color}></span></span><span class="num">{fmt(n)}</span></span>
                    {:else}
                      <span class="cell"><span class="num none">—</span></span>
                    {/if}
                  </td>
                {/each}
                <td class="why">
                  {#if t.reasons.length === 0}
                    <span class="muted">No reason recorded</span>
                  {:else}
                    <ul class="reasons">
                      {#each t.reasons as r (r.text)}<li>{r.text} <span class="muted">×{fmt(r.count)}</span></li>{/each}
                    </ul>
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
    <p class="muted" role="status">{coverage}</p>
  </section>

  <div class="two">
    <section class="card" aria-labelledby="fail-h">
      <h2 id="fail-h">Tools that fail most</h2>
      <span class="muted">Error rate per call, last {fmtDays(data.window_days)}</span>
      {#if data.failing.length === 0}
        <p class="empty">No tool has 20 or more calls with errors in this window.</p>
      {:else}
        <ul class="fails">
          {#each data.failing as f (f.tool)}
            <li>
              <a class="fail" href={routeHref('work', '', { tool: f.tool, lens: 'error', kind: 'any', days: String(data.window_days) })}>
                <span class="fname">{f.tool}</span>
                <span class="track"><span class="fill" style:width={pct(f.error_rate)} style:--fill="var(--red)"></span></span>
                <span class="rate">{pct(f.error_rate)}</span>
                <span class="of muted">{fmt(f.errors)} of {fmt(f.calls)} calls</span>
              </a>
            </li>
          {/each}
        </ul>
        <p class="muted">Each bar opens the sessions where that tool failed.</p>
      {/if}
    </section>

    <section class="card" aria-labelledby="waste-h">
      <h2 id="waste-h">Wasted steps</h2>
      <div class="waste">
        <p class="big">{fmt(data.wasted.repeated_calls)}</p>
        <p>{fmt(data.wasted.repeated_calls)} extra calls repeated an identical call in the same turn.</p>
      </div>
      <div class="waste">
        <p class="big">{fmt(data.wasted.retried_and_failed_again)}</p>
        <p>{fmt(data.wasted.retried_and_failed_again)} calls failed again after the previous identical call in the same turn had failed.</p>
      </div>
    </section>
  </div>
{/if}

<style>
  .head { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
  .head-right { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
  h2 { font-size: 1.0625rem; font-weight: 600; margin: 0; }
  .subtitle { color: var(--muted); font-size: 0.9375rem; margin: 0; max-width: 75ch; }
  .muted { color: var(--muted); font-size: 0.8125rem; margin: 0; }
  .loading { color: var(--muted); font-size: 0.8125rem; }
  .notice { padding: 0.75rem 1rem; border-radius: var(--radius-sm); background: var(--warn-bg);
    border: 1px solid var(--warn-border); font-size: 0.875rem; }
  .empty { color: var(--muted); margin: 0.5rem 0; font-size: 0.9375rem; }
  select:focus-visible, a:focus-visible { outline: 2px solid var(--accent-text); outline-offset: 2px; }
  .field { display: flex; align-items: center; gap: 0.5rem; color: var(--muted); font-size: 0.875rem; }
  select { background: var(--surface); border: 1px solid var(--border); border-radius: var(--ctl-radius); color: var(--text); padding: 0.5rem 0.875rem; font: inherit; min-height: 36px; }
  .card { background: var(--surface); border: var(--card-border); border-radius: var(--card-radius); box-shadow: var(--card-shadow); padding: 1.25rem; display: flex; flex-direction: column; gap: 0.75rem; min-width: 0; }
  .card-head { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 0.5rem; align-items: baseline; }
  .two { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1.25rem; }
  .scroll { overflow-x: auto; }
  table { width: 100%; min-width: 760px; border-collapse: collapse; font-size: 0.875rem; }
  thead th { text-align: left; color: var(--muted); font-size: 0.8125rem; font-weight: 500; padding: 0.625rem 0.5rem; border-bottom: 1px solid var(--border); }
  thead th.kind { width: 170px; }
  tbody tr { border-bottom: 1px solid var(--border); }
  tbody th, tbody td { padding: 0.625rem 0.5rem; text-align: left; font-weight: 400; vertical-align: top; }
  .tool { display: inline-flex; align-items: center; gap: 8px; font-family: var(--font-mono); font-size: 0.8125rem; overflow-wrap: anywhere; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; flex: none; background: var(--dot); }
  .cell { display: flex; align-items: center; gap: 8px; }
  .track { flex: 1 1 auto; height: 8px; background: var(--border); border-radius: 3px; display: block; }
  .fill { display: block; height: 100%; border-radius: 3px; background: var(--fill); }
  .num { min-width: 3rem; text-align: right; font-variant-numeric: tabular-nums; }
  .num.none { color: var(--muted); }
  td.why { padding-left: 1.25rem; font-size: 0.8125rem; line-height: 1.45; }
  .reasons { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.25rem; overflow-wrap: anywhere; }
  .fails { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.75rem; }
  a.fail { display: grid; grid-template-columns: minmax(0, 140px) minmax(0, 1fr) 3rem; gap: 0.25rem 0.75rem; align-items: center; color: inherit; text-decoration: none; border-radius: 6px; }
  a.fail:hover { background: var(--surface-hover); }
  .fname { font-family: var(--font-mono); font-size: 0.8125rem; overflow-wrap: anywhere; }
  .rate { text-align: right; font-size: 0.8125rem; }
  .of { grid-column: 2 / 4; }
  .waste { display: flex; align-items: baseline; gap: 1rem; }
  .waste p { margin: 0; font-size: 0.9375rem; line-height: 1.5; color: var(--muted); }
  .waste .big { font-size: 1.75rem; font-weight: 700; color: var(--text); min-width: 4rem; }
  @media (max-width: 768px) {
    a.fail { min-height: 44px; }
    .two { grid-template-columns: minmax(0, 1fr); }
    select { min-height: 44px; }
  }
</style>
