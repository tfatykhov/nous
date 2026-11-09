<script lang="ts">
  import { onDestroy } from 'svelte';
  import { apiGet } from '../../lib/api';
  import { makePollStore } from '../../lib/stores/registry';
  import { usePoll } from '../../lib/poll';
  import { currentRoute, currentSub, currentQuery, routeHref } from '../../lib/router';
  import { fmtTime, laneColor, toolChips } from '../../lib/work';
  import type { WorkSession, WorkSessionsData } from '../../lib/types/api';
  import StatGrid from '../../lib/ui/StatGrid.svelte';
  import StaleBadge from '../../lib/ui/StaleBadge.svelte';

  // F108 Work sessions list. Fetched once per open and on every filter change, plus a Refresh button: the keyset
  // is on an aggregate, so every page aggregates the whole window. Filters live in the hash query. Every string
  // here (title, message, tool name) came from a model or a user and is shown as a text node only.
  const KINDS = [
    { id: 'all', label: 'All but background' },
    { id: 'conversation', label: 'Conversations' },
    { id: 'task', label: 'Tasks' },
    { id: 'background', label: 'Background' },
  ];
  const LENSES = [
    { id: 'error', label: 'A tool failed' },
    { id: 'repeat', label: 'Same call repeated' },
    { id: 'many', label: '10 or more calls in a turn' },
    { id: 'asked', label: 'Nous asked you a question' },
    { id: 'changed', label: 'Changed something' },
  ];
  const KIND_LABEL: Record<string, string> = { conversation: 'Conversation', task: 'Task', background: 'Background' };

  type Filters = { kind: string; lens: string; tool: string; q: string };

  // Route, sub and query are read together, and never written. A filter change goes through the hash.
  let filters = $derived.by((): Filters | null => {
    const route = $currentRoute;
    const sub = $currentSub;
    const query = $currentQuery;
    if (route !== 'work' || sub.replace(/\/+$/, '') !== '') return null;
    const kind = query.get('kind') ?? 'all';
    const lens = query.get('lens') ?? '';
    return {
      kind: KINDS.some((k) => k.id === kind) ? kind : 'all',
      lens: LENSES.some((l) => l.id === lens) ? lens : '',
      tool: query.get('tool') ?? '',
      q: query.get('q') ?? '',
    };
  });

  function apiQuery(f: Filters): string {
    const p = new URLSearchParams({ kind: f.kind });
    if (f.lens) p.set('lens', f.lens);
    if (f.tool) p.set('tool', f.tool);
    if (f.q) p.set('q', f.q);
    return p.toString();
  }

  // The query string of page one. While the hash is off this screen (a route change is mid-flight) it holds
  // the last one, so a stray refresh never fetches another screen's query.
  let key = $derived(filters ? apiQuery(filters) : null);
  let lastKey = '';
  const currentKey = () => {
    if (key !== null) lastKey = key;
    return lastKey;
  };

  const store = usePoll(
    makePollStore<WorkSessionsData>(
      (signal) => apiGet<WorkSessionsData>(`/dashboard/work/sessions?${currentKey()}`, { signal }),
      0, // fetch-once (a filter change or Refresh reloads)
    ),
  );

  // Pages after the first, and the cursor to the next. Reset by anything that refetches page one.
  let extra = $state<WorkSession[]>([]);
  let moreCursor = $state<string | null | undefined>(undefined);
  let moreError = $state(false);
  let moreBusy = $state(false);
  let generation = 0;

  function resetPages() {
    generation += 1;
    extra = [];
    moreCursor = undefined;
    moreError = false;
    moreBusy = false;
  }

  let first = true;
  $effect(() => {
    if (key === null) return;
    if (first) { first = false; return; }
    resetPages();
    void store.refresh();
  });

  function refresh() {
    resetPages();
    void store.refresh();
  }

  let data = $derived($store.data);
  let sessions = $derived(data ? [...data.sessions, ...extra] : []);
  let cursor = $derived(moreCursor === undefined ? (data?.next_before ?? null) : moreCursor);

  async function loadMore() {
    if (cursor === null || moreBusy) return;
    const mine = generation;
    const params = new URLSearchParams(currentKey());
    params.set('before', cursor);
    moreBusy = true;
    moreError = false;
    try {
      const page = await apiGet<WorkSessionsData>(`/dashboard/work/sessions?${params}`);
      if (mine !== generation) return; // a filter change or Refresh came first
      const seen = new Set(sessions.map((s) => s.session_id));
      extra = [...extra, ...page.sessions.filter((s) => !seen.has(s.session_id))];
      moreCursor = page.next_before;
    } catch {
      if (mine === generation) moreError = true;
    } finally {
      if (mine === generation) moreBusy = false;
    }
  }

  // Each control writes the hash; the router turns that into a new query and the effect above refetches.
  function go(patch: Partial<Filters>) {
    const f = { ...(filters ?? { kind: 'all', lens: '', tool: '', q: '' }), ...patch };
    const query: Record<string, string> = {};
    if (f.kind !== 'all') query.kind = f.kind;
    if (f.lens) query.lens = f.lens;
    if (f.tool) query.tool = f.tool;
    if (f.q) query.q = f.q;
    location.replace(routeHref('work', '', query)); // a filter is not a page: Back leaves the screen
  }

  let qText = $state('');
  // Re-sync the box only when the URL's q itself changed (Back, a shared link), never on another filter's change,
  // and without reading qText here: that would wipe what is being typed.
  let syncedQ = '';
  $effect(() => {
    const q = filters?.q ?? '';
    if (q !== syncedQ) { syncedQ = q; qText = q; }
  });
  let timer: ReturnType<typeof setTimeout> | null = null;
  function onSearch() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      syncedQ = qText.trim(); // the URL will echo this back: not a change to re-sync from
      go({ q: syncedQ });
    }, 300);
  }
  onDestroy(() => { if (timer) clearTimeout(timer); });

  let toolNames = $derived.by(() => {
    const names = data?.tool_names ?? [];
    const cur = filters?.tool ?? '';
    return cur && !names.includes(cur) ? [cur, ...names] : names;
  });

  const fmt = (n: number) => n.toLocaleString('en-US');
  let stats = $derived(
    data
      ? [
          { label: 'Conversations', value: fmt(data.counts.conversation.sessions), note: `${fmt(data.counts.conversation.turns)} turns, with you and with Claude Code` },
          { label: 'Tasks', value: fmt(data.counts.task.sessions), note: `${fmt(data.counts.task.turns)} turns: subtasks, workflow steps and intentions` },
          { label: 'Background', value: fmt(data.counts.background.sessions), note: `${fmt(data.counts.background.turns)} turns: heartbeats and checks, hidden by default` },
        ]
      : [],
  );
</script>

<header class="head">
  <div>
    <h1>Work</h1>
    <p class="subtitle">
      Every conversation and task{data ? ` in the last ${data.window_days} days` : ''}, with the tools Nous used. Open one to see each step and why it took it.
    </p>
  </div>
  <div class="head-right">
    <button type="button" class="refresh" onclick={refresh}>Refresh</button>
    {#if $store.loading}<span class="loading" role="status">Loading…</span>{/if}
    <StaleBadge state={$store} />
  </div>
</header>

{#if data && !data.enabled}
  <div class="notice" role="status">Tool-call recording is off. Set NOUS_TOOL_CALL_LOG_ENABLED to record what Nous does.</div>
{:else if data}
  <section aria-label="Totals"><StatGrid {stats} /></section>

  <section class="filters" aria-label="Filters">
    <div class="seg" role="group" aria-label="Kind of work">
      {#each KINDS as k (k.id)}
        <button type="button" aria-pressed={filters?.kind === k.id} class:on={filters?.kind === k.id}
          onclick={() => go({ kind: k.id })}>{k.label}</button>
      {/each}
    </div>
    <label class="field">
      <span>Tool</span>
      <select value={filters?.tool ?? ''} onchange={(e) => go({ tool: e.currentTarget.value })}>
        <option value="">Any tool</option>
        {#each toolNames as t (t)}<option value={t}>{t}</option>{/each}
      </select>
    </label>
    <label class="field grow">
      <span>Search</span>
      <input type="search" placeholder="Title, your message or a reason" bind:value={qText} oninput={onSearch} />
    </label>
  </section>

  <section class="lenses" aria-label="Narrow the list">
    <span class="muted">Only show</span>
    {#each LENSES as l (l.id)}
      <button type="button" class="chip" class:on={filters?.lens === l.id} aria-pressed={filters?.lens === l.id}
        onclick={() => go({ lens: filters?.lens === l.id ? '' : l.id })}>{l.label}</button>
    {/each}
  </section>

  {#if sessions.length === 0}
    <p class="empty">No sessions match.</p>
  {:else}
    <ul class="sessions" class:dim={$store.loading}>
      {#each sessions as s (s.session_id)}
        <li>
          <a class="row" href={routeHref('work', ['s', s.session_id])}>
            <span class="main">
              <span class="title">{s.title}</span>
              {#if s.first_message}<span class="first">{s.first_message}</span>{/if}
            </span>
            <span class="meta">
              <span class="kind kind-{s.kind}">{KIND_LABEL[s.kind] ?? s.kind}</span>
              <span class="when"><span class="vh">Started </span>{fmtTime(s.started_at)}</span>
              <span class="turns">{s.turns} {s.turns === 1 ? 'turn' : 'turns'}</span>
            </span>
            <span class="tools">
              {#each toolChips(s.tools) as c}
                <span class="chip-tool"><span class="dot" aria-hidden="true" style:background={laneColor(c.lane)}></span>{c.label}</span>
              {/each}
            </span>
          </a>
        </li>
      {/each}
    </ul>
    <div class="legend">
      <span>Dot colour is the kind of tool:</span>
      <span><i class="dot" aria-hidden="true" style:background={laneColor('memory')}></i>memory</span>
      <span><i class="dot" aria-hidden="true" style:background={laneColor('live')}></i>live state</span>
      <span><i class="dot" aria-hidden="true" style:background={laneColor('workspace')}></i>workspace</span>
      <span><i class="dot" aria-hidden="true" style:background={laneColor('acts')}></i>acts on the world</span>
      <span class="count">Showing {sessions.length} {sessions.length === 1 ? 'session' : 'sessions'}</span>
    </div>
    {#if cursor !== null}
      <div class="more">
        <button type="button" class="refresh" disabled={moreBusy} onclick={loadMore}>Load more</button>
        {#if moreError}<span class="err" role="alert">Could not load more.</span>{/if}
      </div>
    {/if}
  {/if}
{/if}

<style>
  .head { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
  .head-right { display: flex; align-items: center; gap: 0.75rem; }
  h1 { font-size: 1.75rem; font-weight: 700; letter-spacing: -0.02em; margin: 0.25rem 0; }
  .subtitle { color: var(--muted); font-size: 0.9375rem; margin: 0; max-width: 70ch; }
  .muted { color: var(--muted); font-size: 0.875rem; }
  .notice { padding: 0.75rem 1rem; border-radius: 8px; background: rgba(245, 158, 11, 0.08);
    border: 1px solid rgba(245, 158, 11, 0.4); font-size: 0.875rem; }
  .empty { color: var(--muted); text-align: center; padding: 2rem 0; margin: 0; }
  button { font: inherit; cursor: pointer; }
  button:focus-visible, select:focus-visible, input:focus-visible, a.row:focus-visible { outline: 2px solid var(--accent-text); outline-offset: 2px; }
  .refresh { background: var(--surface); color: var(--text); border: 1px solid var(--border); border-radius: 8px; padding: 0.5rem 0.875rem; min-height: 36px; }
  .refresh:disabled { opacity: 0.6; cursor: default; }
  .filters, .lenses { display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: center; margin-top: 0.25rem; }
  .lenses { gap: 0.5rem; }
  .seg { display: flex; gap: 4px; background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 4px; }
  .seg button { background: transparent; color: var(--muted); border: 0; border-radius: 7px; padding: 0.5rem 0.875rem; min-height: 36px; font-size: 0.875rem; }
  .seg button.on { background: var(--surface-hover); color: var(--text); font-weight: 600; box-shadow: inset 0 0 0 1px var(--accent); }
  .field { display: flex; align-items: center; gap: 0.5rem; color: var(--muted); font-size: 0.875rem; }
  .field.grow { flex: 1 1 260px; }
  select, input[type='search'] { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; color: var(--text);
    padding: 0.5rem 0.75rem; font: inherit; min-height: 40px; min-width: 0; }
  input[type='search'] { flex: 1 1 auto; }
  .chip { background: var(--surface); color: var(--text); border: 1px solid var(--border); border-radius: 999px; padding: 0.5rem 0.75rem; font-size: 0.8125rem; font-weight: 500; min-height: 36px; }
  .chip.on { border-color: var(--accent); background: var(--accent-glow); }
  .sessions { list-style: none; margin: 0; padding: 0; border: 1px solid var(--border); border-radius: 12px; background: var(--surface); overflow: hidden; }
  .sessions li + li { border-top: 1px solid var(--border); }
  a.row { display: grid; grid-template-columns: minmax(0, 2fr) auto minmax(0, 2fr); gap: 1rem; padding: 0.875rem 1rem; color: inherit; text-decoration: none; align-items: start; }
  a.row:hover { background: var(--surface-hover); }
  .main { display: flex; flex-direction: column; gap: 0.25rem; min-width: 0; }
  .title { font-weight: 600; overflow-wrap: anywhere; }
  .first { color: var(--muted); font-size: 0.8125rem; overflow-wrap: anywhere; }
  .meta { display: flex; flex-direction: column; gap: 0.25rem; align-items: flex-start; font-size: 0.8125rem; color: var(--muted); white-space: nowrap; }
  .kind { display: inline-block; padding: 0.125rem 0.5rem; border-radius: 6px; font-size: 0.75rem; font-weight: 500; border: 1px solid var(--border); background: var(--bg); color: var(--muted); }
  .kind-conversation { color: #a99df9; }
  .kind-task { color: #22d3ee; }
  .tools { display: flex; flex-wrap: wrap; gap: 6px; }
  .chip-tool { display: inline-flex; align-items: center; gap: 6px; padding: 3px 8px; border-radius: 6px; border: 1px solid var(--border); background: var(--bg);
    font-family: var(--font-mono); font-size: 0.75rem; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; flex: none; }
  .legend { display: flex; flex-wrap: wrap; gap: 1rem; font-size: 0.8125rem; color: var(--muted); align-items: center; }
  .legend span { display: inline-flex; align-items: center; gap: 6px; }
  .legend .count { margin-left: auto; }
  .more { display: flex; align-items: center; gap: 0.75rem; }
  .loading { color: var(--muted); font-size: 0.8125rem; }
  .sessions.dim { opacity: 0.6; }
  .vh { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
  .err { color: var(--red); font-size: 0.875rem; }
  @media (max-width: 768px) {
    a.row { grid-template-columns: minmax(0, 1fr); }
    .meta { flex-direction: row; flex-wrap: wrap; gap: 0.75rem; align-items: center; }
  }
</style>
