<script lang="ts">
  import { currentRoute, currentSub, routeHref } from '../lib/router';
  import { parseWorkSub } from '../lib/work';
  import SubNav from '../lib/ui/SubNav.svelte';
  import SessionList from './work/SessionList.svelte';
  import Patterns from './work/Patterns.svelte';
  import Trace from './work/Trace.svelte';
  import Ledger from './Ledger.svelte';

  // F108 Work host: one screen, its views chosen by the sub path of #/work. The list keeps its filters in the
  // hash query; the Changes tab is the execution ledger, under this screen's title.
  const items = [
    { id: 'sessions', label: 'Sessions', href: routeHref('work') },
    { id: 'patterns', label: 'Patterns', href: routeHref('work', 'patterns') },
    { id: 'changes', label: 'Changes', href: routeHref('work', 'changes') },
  ];

  // Route and sub are read together: a route change is announced after its sub and query are already set.
  let view = $derived.by(() => {
    if ($currentRoute !== 'work') return null;
    return parseWorkSub($currentSub);
  });
  let current = $derived(view?.view === 'patterns' ? 'patterns' : view?.view === 'changes' ? 'changes' : 'sessions');
</script>

<div class="work">
  <SubNav {items} {current} label="Work views" />

  {#if view?.view === 'sessions'}
    <SessionList />
  {:else if view?.view === 'patterns'}
    <Patterns />
  {:else if view?.view === 'changes'}
    <div>
      <h1 class="page-h1">Work</h1>
      <Ledger embedded />
    </div>
  {:else if view?.view === 'trace'}
    {#key view.sessionId}
      <Trace sessionId={view.sessionId} turnId={view.turn ?? undefined} />
    {/key}
  {:else if view?.view === 'notfound'}
    <p class="gone" role="status">Nothing here. <a href={routeHref('work')}>Back to sessions</a></p>
  {/if}
</div>

<style>
  .work { display: flex; flex-direction: column; gap: 1rem; }
  .gone { color: var(--muted); font-size: 0.9375rem; }
  .gone a { color: var(--accent-text); }
</style>
