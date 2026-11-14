<script lang="ts">
  import { untrack } from 'svelte';
  import { combinePolls } from '../lib/pollBadge';
  import { fmtAgo, fmtClockUtc, fmtUtc, fmtWhen } from '../lib/harness';
  import { apiGet } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import { STATE_WORD, byDay, defaultOpen, fmtMinutes, graphSentence, sleepRows, sleepSentence, type SleepState } from '../lib/consolidation';
  import type { ActivityData, ConsolidationData, ConsolidationCycleDetail } from '../lib/types/api';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';
  import { Button } from '../lib/ui/button';
  import SleepChart from './consolidation/SleepChart.svelte';
  import SleepReport from './consolidation/SleepReport.svelte';

  // The sleep journal: a chart of the newest sleeps, then every sleep under its day as one sentence that opens in place
  // into its report. Both stores are fetched once; Refresh reloads them and the open report.

  /** Sleeps fetched per page, and the most the endpoint returns. */
  const PAGE = 30;
  const MOST = 200;
  let limit = $state(PAGE);

  const store = usePoll(
    makePollStore<ConsolidationData>(
      (signal) => apiGet<ConsolidationData>(`/dashboard/consolidation?limit=${limit}`, { signal }),
      0, // fetch-once (manual refresh only)
    ),
  );

  // The sleep counts of /dashboard/activity (every sleep started, audited or not), moved here from Activity (F108).
  const activity = usePoll(
    makePollStore<ActivityData>(
      (signal) => apiGet<ActivityData>('/dashboard/activity?hours=168', { signal }),
      0, // fetch-once
    ),
  );

  const rows = $derived(sleepRows($store.data?.cycles ?? []));
  const days = $derived(byDay(rows));
  const unfinished = $derived(rows.filter((r) => r.state === 'unfinished').length);

  // ── The open sleep and its change rows (fetched when a sleep is first opened) ──
  type Detail = { loading: boolean; error: boolean; data: ConsolidationCycleDetail | null };
  let openId = $state<string | null>(null);
  let details = $state<Record<string, Detail>>({});
  let generation = 0; // a Refresh drops the answers still on their way

  async function loadDetail(id: string) {
    const gen = generation;
    details[id] = { loading: true, error: false, data: null };
    try {
      const data = await apiGet<ConsolidationCycleDetail>(`/dashboard/consolidation/${encodeURIComponent(id)}`, { retries: 1 });
      if (gen === generation) details[id] = { loading: false, error: false, data };
    } catch (err) {
      if (gen !== generation) return;
      // A 404 is a cycle with no rows left to read, not a failure.
      const gone = (err as { status?: number }).status === 404;
      details[id] = { loading: false, error: !gone, data: gone ? { cycle: null, actions: [] } : null };
    }
  }

  function open(id: string) {
    openId = id;
    if (!details[id] || details[id].error) void loadDetail(id);
  }

  function toggle(id: string) {
    if (openId === id) openId = null;
    else open(id);
  }

  /** A bar of the chart opens its sleep and brings its row into view. */
  function pick(id: string) {
    open(id);
    requestAnimationFrame(() => document.getElementById(`sleep-${id}`)?.scrollIntoView?.({ block: 'nearest' }));
  }

  // The page opens on the newest finished sleep, once: a sleep the reader closed stays closed.
  let opened = false;
  $effect(() => {
    if (opened || rows.length === 0) return;
    opened = true;
    const id = defaultOpen(rows);
    if (id) untrack(() => open(id));
  });

  function refreshAll() {
    generation++;
    details = {};
    void store.refresh();
    void activity.refresh();
    if (openId) void loadDetail(openId);
  }

  function showMore() {
    limit = Math.min(MOST, limit + PAGE);
    void store.refresh();
  }

  const STATE_PILL: Record<SleepState, string> = {
    finished: '',
    failed: 'status-pill--bad',
    running: 'status-pill--info',
    unfinished: 'status-pill--warn',
  };

  /** The header badge speaks for every store this page reads (Codex round 1). */
  const pagePoll = $derived(combinePolls({ sleeps: $store, 'sleep counts': $activity }));
</script>

<header class="view-head">
  <div>
    <h1>Consolidation</h1>
    <p class="subtitle">What each sleep changed in memory.</p>
    {#if $activity.data || $activity.error || unfinished > 0}
      <p class="meta">
        {#if $activity.data}
          {@const sl = $activity.data.sleep_stats}
          <span>Last sleep started {sl.last_sleep ? fmtWhen(sl.last_sleep) : 'never'}</span>
          <span>{sl.total_sleeps.toLocaleString('en-US')} {sl.total_sleeps === 1 ? 'sleep' : 'sleeps'} in all</span>
        {:else if $activity.error}
          <span class="bad">
            The sleep counts did not load.
            <button type="button" class="retry-link" onclick={() => void activity.refresh()}>Try again</button>
          </span>
        {/if}
        {#if unfinished > 0}<span class="warn">{unfinished} with no finish recorded</span>{/if}
      </p>
    {/if}
  </div>
  <div class="head-right">
    <StaleBadge state={pagePoll} />
    <Button variant="outline" size="lg" class="px-4 max-md:min-h-11" onclick={refreshAll} disabled={pagePoll.loading}>
      {pagePoll.loading ? 'Loading…' : 'Refresh'}
    </Button>
  </div>
</header>

{#if $store.data}
  {#if rows.length === 0}
    <section class="card empty-card">
      <h2>No sleep is in the audit yet</h2>
      <p>
        A sleep is written here when <code>NOUS_CONSOLIDATION_AUDIT_ENABLED</code> is on. The next sleep after that
        appears as one line saying what it changed.
      </p>
      {#if $activity.data?.sleep_stats.last_sleep}
        {@const sl = $activity.data.sleep_stats}
        <!-- With no audit, the last finished sleep's own event still carries these three counts. -->
        <p class="last-finished">
          The last sleep that finished: {sl.facts_created.toLocaleString('en-US')} facts created ·
          {sl.procedures_created.toLocaleString('en-US')} procedures created ·
          {sl.censors_retired.toLocaleString('en-US')} censors retired.
        </p>
      {/if}
    </section>
  {:else}
    <SleepChart rows={rows.slice(0, PAGE)} {openId} onpick={pick} />

    {#each days as group (group.key)}
      <h2 class="day">{group.day}</h2>
      <ul class="card sleeps">
        {#each group.rows as row (row.cycle.cycle_id)}
          {@const id = row.cycle.cycle_id}
          {@const isOpen = openId === id}
          {@const detail = details[id]}
          {@const graph = graphSentence(row.numbers)}
          <li id="sleep-{id}" class:open={isOpen}>
            <button type="button" class="sleep" aria-expanded={isOpen} aria-controls="report-{id}" onclick={() => toggle(id)}>
              <svg class="chev" viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                <path d="M7 5l6 5-6 5" />
              </svg>
              <span class="when">
                <span class="clock">{fmtClockUtc(row.cycle.started_at)}</span>
                {#if row.state === 'finished'}
                  <span class="took">{fmtMinutes(row.minutes)}</span>
                {:else}
                  <span class="status-pill {STATE_PILL[row.state]}">{STATE_WORD[row.state]}</span>
                  {#if row.state === 'running'}<span class="took">started {fmtAgo(row.cycle.started_at)}</span>{/if}
                {/if}
              </span>
              <span class="what">
                <span class="facts">{sleepSentence(row)}</span>
                {#if graph}<span class="graph">Graph: {graph}</span>{/if}
              </span>
            </button>
            {#if isOpen}
              <div class="report" id="report-{id}">
                {#if !detail || detail.loading}
                  <p class="report-msg loading">Loading what this sleep changed…</p>
                {:else if detail.error}
                  <p class="report-msg error">
                    The changes of the sleep of {fmtUtc(row.cycle.started_at)} did not load.
                    <button type="button" class="retry-link" onclick={() => void loadDetail(id)}>Try again</button>
                  </p>
                {:else}
                  <SleepReport {row} actions={detail.data?.actions ?? []} />
                {/if}
              </div>
            {/if}
          </li>
        {/each}
      </ul>
    {/each}

    {#if rows.length >= limit && limit < MOST}
      <div class="more-row">
        <Button variant="outline" size="lg" class="px-4 max-md:min-h-11" onclick={showMore} disabled={$store.loading}>
          Show {PAGE} older sleeps
        </Button>
      </div>
    {/if}
  {/if}
{:else if $store.error}
  <p class="status-msg error">
    The sleeps did not load.
    <button type="button" class="retry-link" onclick={() => void store.refresh()}>Try again</button>
  </p>
{:else}
  <p class="status-msg loading">Loading…</p>
{/if}

<style>
  /* ── Header ──────────────────────────────────────────────── */
  .view-head {
    display: flex;
    flex-wrap: wrap; /* at phone width the actions drop below the title rather than widen the page */
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
    line-height: 1.15;
  }

  .subtitle {
    font-size: 0.8125rem;
    color: var(--muted);
    margin: 0;
  }

  /* The facts under the title, as pills. */
  .meta { display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 0.75rem 0 0; font-size: 0.8125rem; }
  .meta > * { padding: 3px 11px; border-radius: 999px; background: var(--surface); border: var(--card-border); box-shadow: var(--card-shadow); color: var(--text); }
  .meta .warn { color: var(--warn-text); background: var(--warn-bg); border-color: transparent; box-shadow: none; font-weight: 600; }
  .meta .bad { color: var(--err-text); background: var(--err-bg); border-color: transparent; box-shadow: none; }

  .head-right {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-shrink: 0;
  }

  /* ── Cards ───────────────────────────────────────────────── */
  .card {
    background: var(--surface);
    border: var(--card-border);
    border-radius: var(--card-radius);
    box-shadow: var(--card-shadow);
  }

  .empty-card { padding: 1.125rem 1.25rem; }
  .empty-card h2 { font-size: 15px; font-weight: 650; color: var(--text); margin: 0 0 0.375rem; }
  .empty-card p { margin: 0; font-size: 0.875rem; color: var(--muted); max-width: 72ch; }
  .empty-card code { font-family: var(--font-mono, monospace); font-size: 0.8125rem; color: var(--text); }
  .empty-card .last-finished { margin-top: 0.625rem; color: var(--text); }

  /* ── A day and its sleeps ────────────────────────────────── */
  .day {
    font-size: 1.0625rem;
    font-weight: 650;
    line-height: 1.2;
    color: var(--text);
    margin: 1.5rem 0 0.625rem;
  }

  .sleeps { list-style: none; margin: 0; padding: 0; overflow: hidden; }
  .sleeps li + li { border-top: 1px solid var(--border); }
  .sleeps li { scroll-margin-top: 1rem; }

  .sleep {
    display: grid;
    grid-template-columns: auto 9.5rem minmax(0, 1fr);
    gap: 0 0.75rem;
    align-items: baseline;
    width: 100%;
    padding: 0.75rem 1.25rem;
    border: 0;
    background: transparent;
    color: inherit;
    font: inherit;
    text-align: left;
    cursor: pointer;
    transition: background var(--transition);
  }
  .sleep:hover { background: var(--surface-hover); }
  .sleep:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: -2px; border-radius: var(--card-radius-sm); }

  /* The chevron sits on the row's first line, whatever the sentence wraps to. */
  .chev { align-self: start; margin-top: 0.25rem; color: var(--muted); transition: transform var(--transition); }
  .sleep[aria-expanded='true'] .chev { transform: rotate(90deg); }

  .when { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.125rem 0.5rem; }
  .clock { font-size: 0.9375rem; font-weight: 650; font-stretch: 82%; color: var(--text); white-space: nowrap; }
  .took { font-size: 0.8125rem; color: var(--muted); white-space: nowrap; }

  .what { display: flex; flex-direction: column; gap: 0.125rem; min-width: 0; }
  .facts { font-size: 0.875rem; color: var(--text); }
  .open .facts { font-weight: 600; }
  .graph { font-size: 0.8125rem; color: var(--muted); }

  /* The report lines up under the sentence it expands. */
  .report { padding: 0 1.25rem 1rem calc(1.25rem + 14px + 0.75rem); }

  .report-msg { margin: 0; padding: 0.75rem 0; font-size: 0.8125rem; color: var(--muted); border-top: 1px solid var(--border); }
  .report-msg.error, .status-msg.error { color: var(--err-text); }

  .more-row { display: flex; justify-content: center; margin-top: 1.25rem; }

  /* ── Status messages ─────────────────────────────────────── */
  .status-msg {
    color: var(--muted);
    font-size: 0.9375rem;
    padding: 2rem 1rem;
    text-align: center;
    margin: 0;
  }

  /* The one loop: the waiting word breathes (off under reduced motion, app.css). */
  .loading { animation: waiting 1.5s ease-in-out infinite; }
  @keyframes waiting {
    50% { opacity: 0.5; }
  }

  .retry-link {
    background: none;
    border: none;
    color: var(--accent-text);
    cursor: pointer;
    font: inherit;
    padding: 0;
    text-decoration: underline;
  }

  @media (max-width: 640px) {
    /* The time and the sentence stack; the report takes the card's width. */
    .sleep { grid-template-columns: auto minmax(0, 1fr); padding: 0.75rem 1rem; min-height: 44px; }
    .what { grid-column: 2; margin-top: 0.125rem; }
    .report { padding: 0 1rem 1rem; }
  }
</style>
