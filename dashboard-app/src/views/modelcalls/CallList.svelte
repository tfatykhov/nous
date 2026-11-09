<script lang="ts">
  import { apiGet } from '../../lib/api';
  import { makePollStore } from '../../lib/stores/registry';
  import { usePoll } from '../../lib/poll';
  import { routeHref } from '../../lib/router';
  import { fmtDuration, fmtTime, fmtTokens, modelCallQuery, type ModelCallFilters } from '../../lib/work';
  import type { ModelCallRow, ModelCallsData } from '../../lib/types/api';
  import StaleBadge from '../../lib/ui/StaleBadge.svelte';

  // F108 Model calls list. Polled every 60 s (page one only); pages after the first are fetched on demand and survive
  // a poll. Filters and the days window come from the hash query and are never lifted by a filter click. Every string
  // (purpose, model, error) is shown as a text node only.
  let { filters, selectedId }: { filters: ModelCallFilters; selectedId: string | null } = $props();

  const STATUSES = [
    { id: '', label: 'Any status' },
    { id: 'problem', label: 'Has a problem' },
    { id: 'error', label: 'Failed' },
    { id: 'cancelled', label: 'Cancelled' },
    { id: 'ok', label: 'Succeeded' },
  ];
  const BUCKET_KINDS = ['embedding', 'rerank'];

  let key = $derived(new URLSearchParams(modelCallQuery(filters)).toString());

  const store = usePoll(
    makePollStore<ModelCallsData>(
      (signal) => apiGet<ModelCallsData>(`/dashboard/model-calls?${key}`, { signal }),
      60000,
    ),
  );

  let extra = $state<ModelCallRow[]>([]);
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

  // A filter change refetches page one and drops the appended pages. A poll does neither: it replaces page one only.
  let seenKey: string | null = null;
  $effect(() => {
    const k = key;
    if (seenKey === null) { seenKey = k; return; }
    if (k === seenKey) return;
    seenKey = k;
    resetPages();
    void store.refresh();
  });

  let data = $derived($store.data);
  let calls = $derived.by(() => {
    const first = data?.calls ?? [];
    const seen = new Set(first.map((c) => c.id));
    return [...first, ...extra.filter((c) => !seen.has(c.id))];
  });
  let cursor = $derived(moreCursor === undefined ? (data?.next_before ?? null) : moreCursor);

  async function loadMore() {
    if (cursor === null || moreBusy) return;
    const mine = generation;
    const params = new URLSearchParams(key);
    params.set('before', cursor);
    moreBusy = true;
    moreError = false;
    try {
      const page = await apiGet<ModelCallsData>(`/dashboard/model-calls?${params}`);
      if (mine !== generation) return;
      const seen = new Set(calls.map((c) => c.id));
      extra = [...extra, ...page.calls.filter((c) => !seen.has(c.id))];
      moreCursor = page.next_before;
    } catch {
      if (mine === generation) moreError = true;
    } finally {
      if (mine === generation) moreBusy = false;
    }
  }

  // Each control writes the hash with replace (a filter is not a page); the host turns it into new props.
  function go(patch: Partial<ModelCallFilters>) {
    const query = modelCallQuery({ ...filters, ...patch });
    location.replace(routeHref('model-calls', selectedId ? ['c', selectedId] : '', query));
  }

  const label = (purpose: string) => purpose.replaceAll('_', ' ');
  const fmt = (n: number) => n.toLocaleString('en-US');
  const cacheShare = (c: ModelCallRow) =>
    c.cache_read != null && c.input_total ? `${Math.round((c.cache_read / c.input_total) * 100)}%` : '—';
  const isBucket = (c: ModelCallRow) => BUCKET_KINDS.includes(c.call_kind) || c.call_count > 1;
  const href = (id: string) => routeHref('model-calls', ['c', id], modelCallQuery(filters));
  let windowDays = $derived(data?.window_days ?? 7);
  const ACTIVE: { key: 'model' | 'session_id' | 'turn_id'; name: string }[] = [
    { key: 'model', name: 'Model' },
    { key: 'session_id', name: 'Session' },
    { key: 'turn_id', name: 'Turn' },
  ];
</script>

<div class="list">
  <header class="head">
    <div>
      <h1>Model calls</h1>
      <p class="subtitle">Every call Nous makes to a model, background ones included. Full prompt and response for 7 days, numbers for 30.</p>
    </div>
    <div class="head-right">
      {#if $store.loading}<span class="loading" role="status">Loading…</span>{/if}
      <StaleBadge state={$store} />
    </div>
  </header>

  {#if data && !data.enabled}
    <div class="notice" role="status">Model-call recording is off. Set NOUS_MODEL_CALL_LOG_ENABLED.</div>
  {:else if data}
    <section class="chips" aria-label="Filter by purpose">
      {#each data.purposes as p (p.purpose)}
        <button type="button" class="chip" class:on={filters.purpose === p.purpose} aria-pressed={filters.purpose === p.purpose}
          onclick={() => go({ purpose: filters.purpose === p.purpose ? '' : p.purpose })}>
          {label(p.purpose)} <span class="n">{fmt(p.calls)}</span>{#if p.bucketed} <span class="note">per minute</span>{/if}
        </button>
      {/each}
    </section>
    <p class="muted">Counts are calls in the last {windowDays} days. Embedding and rerank calls are counted per minute.</p>

    <section class="filters" aria-label="Filters">
      <label class="field">
        <span>Status</span>
        <select value={filters.status} onchange={(e) => go({ status: e.currentTarget.value })}>
          {#each STATUSES as s (s.id)}<option value={s.id}>{s.label}</option>{/each}
        </select>
      </label>
      {#each ACTIVE as a (a.key)}
        {#if filters[a.key]}
          <button type="button" class="chip on" aria-label={`Clear ${a.name.toLowerCase()} filter`} onclick={() => go({ [a.key]: '' })}>
            {a.name}: <span class="mono">{filters[a.key]}</span> ✕
          </button>
        {/if}
      {/each}
    </section>

    {#if calls.length === 0}
      <p class="empty">No model calls in the last {windowDays} days match.</p>
    {:else}
      <ul class="calls" class:dim={$store.loading}>
        {#each calls as c (c.id)}
          <li>
            <a class="row" href={href(c.id)} aria-current={c.id === selectedId ? 'true' : undefined}>
              <span class="top">
                <span class="purpose">{label(c.purpose)}</span>
                <span class="when">{fmtTime(c.started_at)}</span>
              </span>
              <span class="model mono">{c.model ?? '—'}</span>
              <span class="nums">
                <span>{fmtTokens(c.input_total)} in</span>
                <span>{cacheShare(c)} from cache</span>
                <span>{fmtTokens(c.output_tokens)} out</span>
                <span>{fmtDuration(c.duration_ms)}</span>
                {#if isBucket(c)}<span>×{fmt(c.call_count)} {c.call_count === 1 ? 'call' : 'calls'}</span>{/if}
              </span>
              {#if c.problem}<span class="problem">Problem: {c.problem}</span>{/if}
            </a>
            {#if c.error}
              <p class="error">{c.error} <a class="full" href={href(c.id)}>full error</a></p>
            {:else if c.status === 'error'}
              <p class="error"><a class="full" href={href(c.id)}>full error</a></p>
            {/if}
          </li>
        {/each}
      </ul>
      {#if cursor !== null}
        <div class="more">
          <button type="button" class="btn" disabled={moreBusy} onclick={loadMore}>Load more</button>
          {#if moreError}<span class="err" role="alert">Could not load more.</span>{/if}
        </div>
      {/if}
    {/if}
  {/if}
</div>

<style>
  .list { display: flex; flex-direction: column; gap: 0.875rem; min-width: 0; }
  .head { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
  .head-right { display: flex; align-items: center; gap: 0.75rem; }
  h1 { font-size: 1.75rem; font-weight: 700; letter-spacing: -0.02em; margin: 0.25rem 0; }
  .subtitle { color: var(--muted); font-size: 0.9375rem; margin: 0; max-width: 75ch; }
  .muted { color: var(--muted); font-size: 0.8125rem; margin: 0; }
  .loading { color: var(--muted); font-size: 0.8125rem; }
  .mono { font-family: var(--font-mono); }
  .notice { padding: 0.75rem 1rem; border-radius: 8px; background: rgba(245, 158, 11, 0.08); border: 1px solid rgba(245, 158, 11, 0.4); font-size: 0.875rem; }
  .empty { color: var(--muted); text-align: center; padding: 2rem 0; margin: 0; }
  button { font: inherit; cursor: pointer; }
  button:focus-visible, select:focus-visible, a:focus-visible { outline: 2px solid var(--accent-text); outline-offset: 2px; }
  .chips, .filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .chip { background: var(--surface); color: var(--text); border: 1px solid var(--border); border-radius: 999px; padding: 0.4rem 0.75rem; font-size: 0.8125rem; font-weight: 500; min-height: 36px; }
  .chip.on { border-color: var(--accent); background: var(--accent-glow); }
  .chip .n { color: var(--muted); }
  .chip .note { color: var(--muted); font-size: 0.75rem; border: 1px dashed var(--border); border-radius: 6px; padding: 0 5px; margin-left: 2px; }
  .field { display: flex; align-items: center; gap: 0.5rem; color: var(--muted); font-size: 0.875rem; }
  select { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; color: var(--text); padding: 0.5rem 0.75rem; font: inherit; min-height: 40px; }
  .calls { list-style: none; margin: 0; padding: 0; border: 1px solid var(--border); border-radius: 12px; background: var(--surface); overflow: hidden; }
  .calls li + li { border-top: 1px solid var(--border); }
  .calls.dim { opacity: 0.6; }
  a.row { display: flex; flex-direction: column; gap: 4px; padding: 0.75rem 1rem; color: inherit; text-decoration: none; }
  a.row:hover { background: var(--surface-hover); }
  a.row[aria-current='true'] { background: var(--surface-hover); box-shadow: inset 3px 0 0 var(--accent); }
  .top { display: flex; justify-content: space-between; gap: 8px; align-items: baseline; }
  .purpose { font-weight: 600; font-size: 0.875rem; overflow-wrap: anywhere; }
  .when { font-size: 0.75rem; color: var(--muted); white-space: nowrap; }
  .model { font-size: 0.75rem; color: var(--text); overflow-wrap: anywhere; }
  .nums { display: flex; flex-wrap: wrap; gap: 2px 14px; font-size: 0.75rem; color: var(--muted); }
  .problem { font-size: 0.75rem; color: var(--red); }
  .error { margin: 0; padding: 0 1rem 0.75rem; font-size: 0.75rem; color: var(--red); overflow-wrap: anywhere; }
  .full { color: var(--accent-text); margin-left: 6px; }
  .more { display: flex; align-items: center; gap: 0.75rem; }
  .btn { background: var(--surface); color: var(--text); border: 1px solid var(--border); border-radius: 8px; padding: 0.5rem 0.875rem; min-height: 36px; }
  .btn:disabled { opacity: 0.6; cursor: default; }
  .err { color: var(--red); font-size: 0.875rem; }
</style>
