<script lang="ts">
  // F092.1 AppHeader — title, subtitle, and the mandatory freshness stamp.
  // composedAt is a BINDING (app.refresh patches /meta/composedAt in the
  // data model; a literal would keep saying "2h ago" over fresh data).
  // The stamp ticks: nowMs updates every 30s via $effect with teardown —
  // a stamp that said "just now" forever would be worse than none.
  import { store } from '../store.svelte';
  import { resolveDynamic, toDisplayString } from '../functions';
  import { formatFreshness } from '../freshness';
  import type { Scope } from '../pointer';
  import type { A2uiComponent } from '../store.svelte';

  let {
    surfaceId,
    comp,
    scope = null,
  }: {
    surfaceId: string;
    comp: A2uiComponent;
    scope?: Scope | null;
    depth?: number;
    ancestors?: readonly string[];
  } = $props();

  let nowMs = $state(Date.now());
  $effect(() => {
    const id = setInterval(() => {
      nowMs = Date.now();
    }, 30_000);
    return () => clearInterval(id);
  });

  const ctx = $derived({ dataModel: store.surfaces[surfaceId]?.dataModel ?? {}, scope });
  const title = $derived(toDisplayString(resolveDynamic(comp.title, ctx)));
  const subtitle = $derived(toDisplayString(resolveDynamic(comp.subtitle, ctx)));
  const composedAt = $derived(toDisplayString(resolveDynamic(comp.composedAt, ctx)));
  const staleAfterS = $derived(typeof comp.staleAfterS === 'number' ? comp.staleAfterS : 3600);
  const freshness = $derived(formatFreshness(composedAt, nowMs, staleAfterS));
</script>

<header class="app-header">
  <div class="titles">
    <h2>{title}</h2>
    {#if subtitle}
      <p class="subtitle">{subtitle}</p>
    {/if}
  </div>
  <span class="stamp" class:stale={freshness.stale}>{freshness.label}</span>
</header>

<style>
  .app-header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 0.8rem;
    flex-wrap: wrap;
  }
  h2 {
    margin: 0;
    font-size: 1.15rem;
    line-height: 1.3;
  }
  .subtitle {
    margin: 0.15rem 0 0;
    color: var(--muted);
    font-size: 0.88rem;
  }
  .stamp {
    color: var(--muted);
    font-size: 0.75rem;
    white-space: nowrap;
  }
  .stamp.stale {
    color: var(--yellow);
  }
</style>
