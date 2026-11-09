<script lang="ts">
  import { currentRoute, currentSub, routeHref } from '../lib/router';
  import { parseWorkSub } from '../lib/work';
  import SubNav from '../lib/ui/SubNav.svelte';
  import Placeholder from '../lib/ui/Placeholder.svelte';
  import SessionList from './work/SessionList.svelte';
  import Trace from './work/Trace.svelte';

  // F108 Work host: one screen, its views chosen by the sub path of #/work. The list keeps its filters in the
  // hash query; Patterns and the change ledger are stubs until their tasks land.
  const items = [
    { id: 'sessions', label: 'Sessions', href: routeHref('work') },
    { id: 'patterns', label: 'Patterns', href: routeHref('work', 'patterns') },
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
    <Placeholder route="patterns" />
  {:else if view?.view === 'changes'}
    <Placeholder route="changes" />
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
