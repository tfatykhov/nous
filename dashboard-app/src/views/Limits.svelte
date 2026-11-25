<script lang="ts">
  import { currentRoute, currentSub, routeHref } from '../lib/router';
  import SubNav from '../lib/ui/SubNav.svelte';
  import LimitsTab from './limits/LimitsTab.svelte';
  import Tools from './limits/Tools.svelte';
  import ToolLoading from './limits/ToolLoading.svelte';
  import { PREFERENCES_TEXT } from '../lib/autonomy_settings';

  // F115 autonomy settings: the limits of background work (`#/limits`) and the tools background work may use
  // (`#/limits/tools`), and F120 tool loading, watch only (`#/limits/loading`), chosen by the sub path. Route and sub
  // are read together and never written.
  let tab = $derived.by(() => {
    if ($currentRoute !== 'limits') return null;
    const sub = $currentSub.replace(/\/+$/, '');
    if (sub === '') return 'limits';
    if (sub === 'tools') return 'tools';
    return sub === 'loading' ? 'loading' : 'notfound';
  });

  const items = [
    { id: 'limits', label: 'Limits', href: routeHref('limits') },
    { id: 'tools', label: 'Tools', href: routeHref('limits', 'tools') },
    { id: 'loading', label: 'Tool loading', href: routeHref('limits', 'loading') },
  ];
</script>

<div class="limits">
  <SubNav {items} current={tab ?? ''} label="Limits and tools views" />
  <h1 class="page-h1">Limits and tools</h1>
  <p class="preferences">{PREFERENCES_TEXT}</p>

  {#if tab === 'limits'}
    <LimitsTab />
  {:else if tab === 'tools'}
    <Tools />
  {:else if tab === 'loading'}
    <ToolLoading />
  {:else if tab === 'notfound'}
    <p class="gone" role="status">Nothing here. <a href={routeHref('limits')}>Back to the limits</a></p>
  {/if}
</div>

<style>
  .limits { display: flex; flex-direction: column; gap: 1rem; }
  .preferences { margin: 0; padding: 0.75rem 1rem; border-radius: var(--card-radius-sm); background: var(--wash);
    font-size: 0.8125rem; max-width: 72ch; }
  .gone { color: var(--muted); font-size: 0.9375rem; }
  .gone a { color: var(--accent-text); }
</style>
