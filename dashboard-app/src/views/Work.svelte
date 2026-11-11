<script lang="ts">
  import { currentRoute, currentSub, routeHref } from '../lib/router';
  import { parseWorkSub } from '../lib/work';
  import SubNav from '../lib/ui/SubNav.svelte';
  import Ground from '../lib/ui/Ground.svelte';
  import SessionList from './work/SessionList.svelte';
  import Patterns from './work/Patterns.svelte';
  import Trace from './work/Trace.svelte';
  import Ledger from './Ledger.svelte';

  // F108 Work host: one screen, its views chosen by the sub path of #/work. The list keeps its filters in the
  // hash query; the Changes tab is the execution ledger, under this screen's title. The ledger is not redesigned
  // yet: under the paper look it sits in a dark island (its own tokens) inside a titled card, so it reads as a
  // panel, not a bug. With the look off the frame adds nothing.
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
      <section class="island" aria-labelledby="changes-h">
        <h2 id="changes-h" class="island-title">Changes</h2>
        <Ground ground="dark" class="island-ground"><Ledger embedded /></Ground>
      </section>
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
  /* The Changes frame: nothing under the dark look (the ledger renders as before); a white card holding the dark
     panel under the paper look. */
  .island-title { display: none; }
  :global(html[data-look="paper"]) .island { margin-top: 0.75rem; padding: 1rem; background: var(--surface); border-radius: var(--card-radius);
    box-shadow: var(--card-shadow); }
  :global(html[data-look="paper"]) .island-title { display: block; margin: 0 0.25rem 0.75rem; font-size: 0.9375rem; font-weight: 650; }
  :global(html[data-look="paper"]) .island :global(.island-ground) { border-radius: var(--radius-sm); padding: 1rem 1.25rem; }
</style>
