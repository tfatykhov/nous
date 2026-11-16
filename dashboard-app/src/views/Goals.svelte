<script lang="ts">
  import { tick, untrack } from 'svelte';
  import { Button } from '../lib/ui/button';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';
  import FilterBar from '../lib/ui/FilterBar.svelte';
  import { currentRoute, currentSub, routeHref } from '../lib/router';
  import GoalPane from './goals/GoalPane.svelte';
  import NewGoal from './goals/NewGoal.svelte';
  import {
    DRIVER_OFF_TEXT, GOALS_OFF_TEXT, HALT_TEXT, HELD_TEXT, PILL, READ_FORBIDDEN_TEXT,
    deadlineText, formatNumber, getGoal, goalOptions, haltState, listGoals, nextSessionText, refusalText, spentPercent, stateWord,
    type GoalDetail, type GoalFilter, type GoalList, type GoalOptions, type GoalRow, type HaltState,
  } from '../lib/goals';
  import type { ApiAnswerError } from '../lib/api';

  // F114 goals PR 2: the owner's goals (GET /goals) beside one goal's pane (`#/goals/g/<ref>`) or the form that makes
  // a draft (`#/goals/new`; a ref holds no "/", so neither address can be taken for the other). With no address the
  // pane shows the first open goal. A narrow screen has one column: the list until something is picked, then the pane
  // alone with a link back. Every change is the store's: the buttons are the goal's `actions` from the server, and a
  // refusal is the server's sentence. Text a goal's sessions wrote is shown as text nodes only.

  type Shown = { ref: string; detail: GoalDetail | null; error: unknown };
  type Page = { filter: GoalFilter; list: GoalList; options: GoalOptions; halt: HaltState | null; shown: Shown | null };

  /** The goal the pane shows, how much of its journal, and which goals the list asks for: read by each fetch, so one
   * poll serves the whole screen. */
  let want: string | null = null;
  let journalAll = $state(false);
  let filter = $state<GoalFilter>('open');
  const JOURNAL_ALL = 200;
  /** A fetch that has no answer after this long is given up: the screen says so, and the next tick tries again. */
  const REQUEST_TIMEOUT_MS = 15_000;
  /** The options change only with the server's settings: read once, and again on the page Refresh. */
  let knownOptions: GoalOptions | null = null;

  async function fetchPage(signal?: AbortSignal): Promise<Page> {
    const ref = want;
    const all = journalAll;
    const asked = filter;
    const timeout = new AbortController();
    const timer = setTimeout(() => timeout.abort(), REQUEST_TIMEOUT_MS);
    const stop = () => timeout.abort();
    signal?.addEventListener('abort', stop);
    try {
      const detail = ref
        ? getGoal(ref, all ? JOURNAL_ALL : undefined, timeout.signal).then(
            (d): Shown => ({ ref, detail: d.enabled ? (d as GoalDetail) : null, error: null }),
            (error: unknown): Shown => ({ ref, detail: null, error }),
          )
        : Promise.resolve(null);
      const [list, options, halt, shown] = await Promise.all([
        listGoals(asked, timeout.signal),
        knownOptions ?? goalOptions(timeout.signal),
        haltState(timeout.signal),
        detail,
      ]);
      if (options.enabled) knownOptions = options;
      return { filter: asked, list, options, halt, shown };
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort', stop);
    }
  }

  const store = usePoll(makePollStore<Page>(fetchPage, 15_000));
  /** The page Refresh: everything this screen shows, the options included. */
  const refreshPage = () => {
    knownOptions = null;
    return store.refresh();
  };

  // No poll while the tab is hidden: it starts again, with a fetch at once, when the tab is shown.
  $effect(() => {
    const seen = () => (document.hidden ? store.stop() : store.start());
    document.addEventListener('visibilitychange', seen);
    return () => document.removeEventListener('visibilitychange', seen);
  });

  let forbidden = $derived(($store.error as ApiAnswerError | null)?.status === 403);
  let enabled = $derived($store.data?.list.enabled === true);
  let goals = $derived($store.data?.list.goals ?? []);
  let options = $derived($store.data?.options ?? { enabled: false });
  let halted = $derived($store.data?.halt?.halted === true);
  let driverRunning = $derived(options.driver_running === true);
  let now = $derived($store.lastUpdated ?? Date.now());

  const FILTERS = [
    { value: 'open', label: 'Open' },
    { value: 'all', label: 'All' },
    { value: 'finished', label: 'Finished' },
  ];
  const isOpen = (g: GoalRow) => g.state === 'draft' || g.state === 'active' || g.state === 'paused';
  /** The list as the server sent it for the filter shown (an answer for another filter is not shown). */
  let listed = $derived($store.data?.filter === filter ? goals : []);
  let listTotal = $derived($store.data?.list.total ?? listed.length);
  // A new filter asks the server at once.
  let lastFilter: GoalFilter = 'open';
  $effect(() => {
    const now = filter;
    if (now === lastFilter) return;
    lastFilter = now;
    untrack(() => void store.refresh());
  });

  // ── Which pane is open ─────────────────────────────────────────────────────
  let sub = $derived($currentRoute === 'goals' ? $currentSub : '');
  let picked = $derived(sub === 'new' || (sub.startsWith('g/') && sub.length > 2));
  let narrow = $state(false);
  $effect(() => {
    if (typeof matchMedia !== 'function') return;
    const media = matchMedia('(max-width: 1040px)');
    const read = () => {
      narrow = media.matches;
    };
    read();
    media.addEventListener('change', read);
    return () => media.removeEventListener('change', read);
  });
  const LIST_HREF = routeHref('goals');
  const NEW_HREF = routeHref('goals', 'new');
  const goalHref = (ref: string) => routeHref('goals', ['g', ref]);

  type Pane = { kind: 'list' } | { kind: 'new' } | { kind: 'goal'; ref: string };
  /** The goal shown last: the bare address keeps showing it while it is listed. */
  let stay = $state<string | null>(null);
  let pane = $derived.by((): Pane => {
    if (sub === 'new') return { kind: 'new' };
    if (picked) {
      let ref = sub.slice(2);
      try {
        ref = decodeURIComponent(ref);
      } catch {
        // an address that does not decode is shown as it was typed, and names no goal
      }
      return { kind: 'goal', ref };
    }
    if (narrow) return { kind: 'list' };
    const first = goals.find((g) => g.ref === stay) ?? goals.find(isOpen) ?? goals[goals.length - 1];
    return first ? { kind: 'goal', ref: first.ref } : { kind: 'new' };
  });
  let paneRef = $derived(pane.kind === 'goal' ? pane.ref : null);

  // One column: the list and the pane replace each other, so a click would leave the focus on nothing. It goes to
  // what came into view: the pane, or the list.
  let lastSub: string | null = null;
  $effect(() => {
    const now = sub;
    const moved = lastSub !== null && lastSub !== now;
    lastSub = now;
    if (!moved || !untrack(() => narrow)) return;
    void tick().then(() => (document.getElementById('goal-pane') ?? document.getElementById('goal-list'))?.focus());
  });

  // The pane's goal is read with the list: a new one is fetched at once, and its journal starts at the newest rows.
  $effect(() => {
    const ref = paneRef;
    untrack(() => {
      if (ref === want) return;
      want = ref;
      journalAll = false;
      if (ref) void store.refresh();
    });
  });

  // Only an answer for the goal the pane shows is used: one fetched for another goal is never shown under this one.
  let shown = $derived($store.data?.shown && $store.data.shown.ref === paneRef ? $store.data.shown : null);
  /** The last good detail of the goal the pane shows: when a later read of it fails, the pane keeps it (and what is
   * typed in it) and says the read failed above it. */
  let lastGood = $state<{ ref: string; detail: GoalDetail } | null>(null);
  $effect(() => {
    if (shown?.detail) {
      stay = shown.detail.ref;
      lastGood = { ref: shown.ref, detail: shown.detail };
    }
  });
  let paneDetail = $derived(shown?.detail ?? (lastGood && lastGood.ref === paneRef ? lastGood.detail : null));
  let readError = $derived(shown?.error && paneDetail ? refusalText(shown.error, 'see') : null);
  let missing = $derived((shown?.error as ApiAnswerError | null)?.status === 404);

  function showMore() {
    journalAll = true;
    void store.refresh();
  }

  async function created(ref: string) {
    await store.refresh();
    location.hash = goalHref(ref);
  }
</script>

<header class="view-head">
  <div>
    <h1 class="page-h1" id="goal-top" tabindex="-1">Goals</h1>
    <p class="subtitle">What the agent works on in the background, within the limits you set. Every change here applies at once.</p>
  </div>
  <div class="head-right">
    <StaleBadge state={$store} />
    <Button variant="outline" size="lg" class="px-4 max-md:min-h-11" onclick={() => void refreshPage()} disabled={$store.loading}>{$store.loading ? 'Loading…' : 'Refresh'}</Button>
  </div>
</header>

{#if forbidden}
  <section class="notice" role="status">
    <h2>Sign in to see goals</h2>
    <p>{READ_FORBIDDEN_TEXT}</p>
  </section>
{:else if $store.data && !enabled}
  <section class="notice" role="status">
    <h2>{GOALS_OFF_TEXT}</h2>
    <p>Nothing about goals is shown or changed here while they are off.</p>
  </section>
{:else if $store.data}
  {#if halted}
    <section class="halt" role="status">{HALT_TEXT}</section>
  {/if}
  {#if !driverRunning}
    <p class="quiet">{DRIVER_OFF_TEXT}</p>
  {/if}

  <div class="split" class:picked>
    <nav class="card list" id="goal-list" tabindex="-1" aria-label="Goals">
      <Button href={NEW_HREF} variant="outline" size="lg"
        class="w-full max-md:min-h-11 aria-[current=page]:border-[var(--muted)] aria-[current=page]:bg-[var(--bg)]"
        aria-current={pane.kind === 'new' ? 'page' : undefined}>New goal</Button>
      <div class="filters"><FilterBar options={FILTERS} bind:value={filter} required label="Which goals to show" /></div>
      {#if listed.length === 0}
        <p class="empty">{filter === 'open' ? 'No open goals.' : filter === 'finished' ? 'No finished goals.' : 'No goals yet.'}</p>
      {:else}
        <ul>
          {#each listed as g (g.id)}
            {@const word = stateWord(g, driverRunning)}
            {@const next = nextSessionText(g, now, driverRunning)}
            {@const due = deadlineText(g, now)}
            <li>
              <a class="item" href={goalHref(g.ref)} aria-current={shown?.detail?.id === g.id ? 'page' : undefined}>
                <span class="item-name">{g.title}</span>
                <span class="item-ref">{g.ref}</span>
                <span class="item-state">
                  <span class="status-pill {PILL[word.tone]}">{word.text}</span>
                  {#if halted && g.state === 'active'}<span class="status-pill status-pill--warn">{HELD_TEXT}</span>{/if}
                </span>
                {#if next || due}<span class="item-line">{[next, due].filter(Boolean).join('. ')}</span>{/if}
                <span class="item-spend">
                  <span class="meter" aria-hidden="true"><span class="fill" class:full={g.spent_today >= g.day_budget} style:width="{spentPercent(g.spent_today, g.day_budget)}%"></span></span>
                  <span>Today {formatNumber(g.spent_today)} of {formatNumber(g.day_budget)}</span>
                  <span class="meter" aria-hidden="true"><span class="fill" class:full={g.spent_total >= g.total_budget} style:width="{spentPercent(g.spent_total, g.total_budget)}%"></span></span>
                  <span>In all {formatNumber(g.spent_total)} of {formatNumber(g.total_budget)} tokens</span>
                </span>
              </a>
            </li>
          {/each}
        </ul>
        {#if listTotal > listed.length}<p class="list-note">Showing the newest {listed.length} of {listTotal}.</p>{/if}
      {/if}
    </nav>

    {#if pane.kind !== 'list'}
      <div class="detail" id="goal-pane" tabindex="-1" role="group" aria-label={pane.kind === 'new' ? 'New goal' : (paneDetail?.title ?? pane.ref)}>
        <a class="back" href={LIST_HREF}>All goals</a>
        {#if pane.kind === 'new'}
          <NewGoal {options} onCreated={(ref) => void created(ref)} />
        {:else if paneDetail}
          {#if readError}<p class="read-error" role="alert">Could not read {pane.ref} again: {readError} What is shown is from the last read.</p>{/if}
          {#key paneDetail.id}
            <GoalPane goal={paneDetail} {options} {halted} {now} {journalAll} onChanged={() => store.refresh()} onMore={showMore} />
          {/key}
        {:else if missing}
          <section class="card" role="status">
            <p class="empty">There is no goal {pane.ref} here. Choose one from the list.</p>
          </section>
        {:else if shown?.error}
          <section class="card" role="alert">
            <p class="empty">Could not load {pane.ref}: {refusalText(shown.error, 'see')}</p>
          </section>
        {:else}
          <p class="empty">Loading…</p>
        {/if}
      </div>
    {/if}
  </div>
{:else if $store.error}
  <div class="notice" role="alert">Could not load the goals: {refusalText($store.error, 'see')}</div>
{:else}
  <p class="empty">Loading…</p>
{/if}

<style>
  .view-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; flex-wrap: wrap; margin-bottom: 1.25rem; }
  .head-right { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
  .subtitle { font-size: 0.8125rem; color: var(--muted); margin: 0; }
  h2 { font-size: 0.9375rem; font-weight: 650; margin: 0 0 0.375rem; }
  .notice { padding: 0.875rem 1rem; border-radius: var(--card-radius-sm); background: var(--warn-bg);
    border: 1px solid var(--warn-border); font-size: 0.8125rem; margin-bottom: 1rem; }
  .notice p { margin: 0; }
  .halt { padding: 0.75rem 1rem; border-radius: var(--card-radius-sm); background: var(--warn-bg); color: var(--warn-text);
    font-size: 0.875rem; font-weight: 500; margin-bottom: 1rem; }
  .quiet { font-size: 0.8125rem; color: var(--muted); margin: 0 0 1rem; }
  .card { background: var(--surface); border: var(--card-border); border-radius: var(--card-radius); box-shadow: var(--card-shadow);
    padding: 1.125rem 1.25rem; margin-bottom: 1.25rem; min-width: 0; }

  /* The goals beside one pane. The list stays in view while a long journal scrolls. */
  .split { display: grid; grid-template-columns: 19rem minmax(0, 1fr); gap: 1.25rem; align-items: start; }
  .detail { min-width: 0; }
  .list { position: sticky; top: 1.5rem; margin-bottom: 0; padding: 0.75rem; max-height: calc(100vh - 3rem); overflow-y: auto; }
  .filters { display: flex; margin-top: 0.625rem; background: var(--wash); border-radius: var(--ctl-radius); width: fit-content; }
  .list ul { list-style: none; margin: 0.625rem 0 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
  .item { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: baseline; gap: 0.25rem 0.5rem;
    padding: 0.5rem 0.625rem; border-radius: var(--radius-sm); color: var(--text); text-decoration: none;
    transition: background var(--transition); }
  .item:hover { background: var(--wash); }
  .item[aria-current='page'] { background: var(--bg); }
  .item:focus-visible, .back:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
  .item-name { font-size: 0.875rem; font-weight: 500; overflow-wrap: anywhere; }
  .item[aria-current='page'] .item-name { font-weight: 650; }
  .item-ref { font-family: var(--font-mono); font-size: 0.75rem; color: var(--muted); white-space: nowrap; }
  .item-state { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 0.25rem; }
  .item-line { grid-column: 1 / -1; font-size: 0.75rem; color: var(--muted); }
  .item-spend { grid-column: 1 / -1; display: grid; grid-template-columns: 3rem minmax(0, 1fr); align-items: center;
    gap: 0.125rem 0.5rem; font-size: 0.75rem; color: var(--muted); }
  .meter { display: block; height: 0.375rem; border-radius: 999px; background: var(--track); overflow: hidden; }
  .fill { display: block; height: 100%; border-radius: 999px; background: var(--action); }
  .fill.full { background: var(--warn-text); }
  .back { display: none; align-items: center; min-height: 2.75rem; margin-bottom: 0.25rem; font-size: 0.875rem;
    color: var(--accent-text); text-decoration: none; border-radius: var(--radius-sm); }
  .back:hover { text-decoration: underline; }
  .empty { color: var(--muted); font-size: 0.875rem; text-align: center; padding: 1rem 0; margin: 0; }
  .list .empty { padding: 0.875rem 0 0.375rem; font-size: 0.8125rem; }
  .list-note { font-size: 0.75rem; color: var(--muted); margin: 0.5rem 0.625rem 0; }
  .read-error { font-size: 0.8125rem; color: var(--warn-text); margin: 0 0 0.75rem; overflow-wrap: anywhere; }
  .list:focus, .detail:focus { outline: none; }
  @media (max-width: 1040px) {
    /* One column: the list first, then (once something is picked) the pane alone, with a way back to the list. */
    .split { grid-template-columns: minmax(0, 1fr); }
    .list { position: static; max-height: none; }
    .split.picked .list { display: none; }
    .back { display: inline-flex; }
  }
  @media (max-width: 768px) {
    .item { min-height: 2.75rem; }
  }
</style>
