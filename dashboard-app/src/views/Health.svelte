<script lang="ts">
  import { currentRoute, currentSub, routeHref } from '../lib/router';
  import SubNav from '../lib/ui/SubNav.svelte';
  import GraphTab from './health/GraphTab.svelte';
  import WritesTab from './health/WritesTab.svelte';
  import GrowthTab from './health/GrowthTab.svelte';

  // F108 Memory health host: one screen with three tabs, chosen by the sub path of #/health (Graph by default).
  // Route and sub are read together and never written.
  const TABS = ['graph', 'writes', 'growth'];
  let tab = $derived.by(() => {
    const route = $currentRoute;
    const sub = $currentSub.replace(/\/+$/, '');
    if (route !== 'health') return null;
    if (sub === '') return 'graph';
    return TABS.includes(sub) ? sub : 'notfound';
  });

  const items = [
    { id: 'graph', label: 'Graph', href: routeHref('health', 'graph') },
    { id: 'writes', label: 'Writes', href: routeHref('health', 'writes') },
    { id: 'growth', label: 'Growth', href: routeHref('health', 'growth') },
  ];
</script>

<div class="health">
  <h1 class="page-h1">Memory health</h1>
  <SubNav {items} current={tab ?? ''} label="Memory health views" />

  {#if tab === 'graph'}
    <div><GraphTab /></div>
  {:else if tab === 'writes'}
    <WritesTab />
  {:else if tab === 'growth'}
    <div><GrowthTab /></div>
  {:else if tab === 'notfound'}
    <p class="gone" role="status">Nothing here. <a href={routeHref('health', 'graph')}>Back to the Graph tab</a></p>
  {/if}
</div>

<style>
  .health { display: flex; flex-direction: column; gap: 1rem; }
  .gone { color: var(--muted); font-size: 0.9375rem; }
  .gone a { color: var(--accent-text); }
</style>
