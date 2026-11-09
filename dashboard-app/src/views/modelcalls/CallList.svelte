<script lang="ts">
  import { apiGet } from '../../lib/api';
  import { makePollStore } from '../../lib/stores/registry';
  import { usePoll } from '../../lib/poll';
  import { routeHref } from '../../lib/router';
  import { fmtDays, fmtDuration, fmtTime, fmtTokens, modelCallFilters, modelCallQuery, type ModelCallFilters } from '../../lib/work';
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

  // The page one the last fetch returned, with the filters it was fetched for. When a poll replaces page one, the
  // rows that fell off it (newer calls pushed them past the seam) are folded into the front of the loaded pages, so
  // the list has no gap. Only while a later page is loaded, and only for the same filters.
  let lastPage: { key: string; calls: ModelCallRow[] } | null = null;
  // The filters of the page one now on screen. After a filter change whose page one failed it is still the old one,
  // and so is the cursor: Load more waits for a page one of the current filters.
  let dataKey = $state<string | null>(null);

  const store = usePoll(
    makePollStore<ModelCallsData>((signal) => {
      const k = key;
      return apiGet<ModelCallsData>(`/dashboard/model-calls?${k}`, { signal }).then((d) => {
        if (lastPage?.key === k && (moreCursor !== undefined || moreBusy)) {
          const seen = new Set(d.calls.map((c) => c.id));
          if (lastPage.calls.length > 0 && d.calls.length > 0 && !lastPage.calls.some((c) => seen.has(c.id))) {
            // So many new calls that the new page one no longer meets the old one: the rows between them cannot be
            // folded in, so start paging again from the new page one.
            resetPages();
          } else {
            const have = new Set(extra.map((c) => c.id));
            const fell = lastPage.calls.filter((c) => !seen.has(c.id) && !have.has(c.id));
            if (fell.length > 0) extra = [...fell, ...extra];
          }
        }
        lastPage = { key: k, calls: d.calls };
        dataKey = k;
        return d;
      });
    }, 60000),
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
  // True only while a refetch the user asked for (a filter change) is in flight: a background poll never dims.
  let refetching = $state(false);
  let seenKey: string | null = null;
  $effect(() => {
    const k = key;
    if (seenKey === null) { seenKey = k; return; }
    if (k === seenKey) return;
    seenKey = k;
    resetPages();
    refetching = true;
    const mine = generation; // a later change aborts this refresh: only the latest one re-enables Load more
    void store.refresh().finally(() => { if (mine === generation) refetching = false; });
  });

  let data = $derived($store.data);
  let calls = $derived.by(() => {
    const first = data?.calls ?? [];
    const seen = new Set(first.map((c) => c.id));
    return [...first, ...extra.filter((c) => !seen.has(c.id))];
  });
  let cursor = $derived(moreCursor === undefined ? (data?.next_before ?? null) : moreCursor);

  async function loadMore() {
    // While page one of the new filters loads, the cursor on screen belongs to the old filters.
    if (cursor === null || moreBusy || refetching || dataKey !== key) return;
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
    // The base is the hash as the last write left it, not the props (they lag by one hashchange task): two writes in
    // one turn keep both.
    const base = modelCallFilters(new URLSearchParams(location.hash.split('?')[1] ?? ''));
    const query = modelCallQuery({ ...base, ...patch });
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
    <p class="subtitle">Every call Nous makes to a model, background ones included. Full prompt and response for 7 days, numbers for 30.</p>
    <div class="head-right">
      {#if $store.loading && (!data || refetching)}<span class="loading" role="status">Loading…</span>{/if}
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
          {label(p.purpose)} <span class="n">{fmt(p.calls)}</span>{#if p.bucketed} <span class="note">minute totals</span>{/if}
        </button>
      {/each}
    </section>
    <p class="muted">Counts are calls in the last {fmtDays(windowDays)}. Embedding and rerank calls are counted per minute.</p>

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
      <p class="empty">No model calls in the last {fmtDays(windowDays)} match.</p>
    {:else}
      <ul class="calls" class:dim={refetching}>
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
          <button type="button" class="btn" disabled={moreBusy || refetching || dataKey !== key} onclick={loadMore}>Load more</button>
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
  @media (max-width: 768px) {
    .chip, .btn, select { min-height: 44px; }
  }
</style>
