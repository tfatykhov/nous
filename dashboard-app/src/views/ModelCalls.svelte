<script lang="ts">
  import { currentRoute, currentSub, currentQuery, routeHref } from '../lib/router';
  import { parseModelCallsSub, modelCallFilters, modelCallQuery } from '../lib/work';
  import SubNav from '../lib/ui/SubNav.svelte';
  import CallList from './modelcalls/CallList.svelte';
  import CallDetail from './modelcalls/CallDetail.svelte';
  import Cache from './Cache.svelte';

  // F108 Model calls host: the list, and with `c/<id>` the call's detail beside it (below 1000 px wide the detail
  // replaces the list); `cache` is the Cache tab, under this screen's title. Filters live in the hash query. Route,
  // sub and query are read together and never written.
  let state = $derived.by(() => {
    const route = $currentRoute;
    const sub = $currentSub;
    const query = $currentQuery;
    if (route !== 'model-calls') return null;
    return { view: parseModelCallsSub(sub), filters: modelCallFilters(query) };
  });
  let view = $derived(state?.view);
  let callId = $derived(view?.view === 'list' ? view.callId : null);
  let current = $derived(view?.view === 'cache' ? 'cache' : 'calls');

  const items = [
    { id: 'calls', label: 'Calls', href: routeHref('model-calls') },
    { id: 'cache', label: 'Cache', href: routeHref('model-calls', 'cache') },
  ];
</script>

<div class="mc">
  <SubNav {items} {current} label="Model calls views" />

  {#if state && (view?.view === 'list' || view?.view === 'cache')}
    <h1 class="page-h1">Model calls</h1>
  {/if}

  {#if state && view?.view === 'list'}
    <div class="split" class:open={callId !== null}>
      <div class="listcol"><CallList filters={state.filters} selectedId={callId} /></div>
      {#if callId !== null}
        <div class="detailcol">
          <a class="back" href={routeHref('model-calls', '', modelCallQuery(state.filters))}>Back to calls</a>
          {#key callId}<CallDetail {callId} />{/key}
        </div>
      {/if}
    </div>
  {:else if view?.view === 'cache'}
    <Cache embedded />
  {:else if view?.view === 'notfound'}
    <p class="gone" role="status">Nothing here. <a href={routeHref('model-calls')}>Back to calls</a></p>
  {/if}
</div>

<style>
  .mc { display: flex; flex-direction: column; gap: 1rem; }
  .gone { color: var(--muted); font-size: 0.9375rem; }
  .gone a, .back { color: var(--accent-text); }
  .split { display: block; }
  .detailcol { display: flex; flex-direction: column; gap: 1rem; min-width: 0; }
  .back { font-size: 0.875rem; }
  @media (max-width: 999px) {
    .split.open .listcol { display: none; }
  }
  @media (min-width: 1000px) {
    .split.open { display: grid; grid-template-columns: minmax(320px, 1fr) minmax(0, 2fr); gap: 1.25rem; align-items: start; }
    .back { display: none; }
  }
</style>
