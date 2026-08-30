<script lang="ts">
  // nous-core StatTile — one labeled statistic. `value` and `delta` arrive
  // PREFORMATTED (the catalog says so): the agent has already decided how
  // many decimals and what unit, so the renderer must not reformat them.
  import { store } from '../store.svelte';
  import { flexGrow, resolveDynamic, toDisplayString } from '../functions';
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

  const INTENTS = ['neutral', 'good', 'bad', 'warn'];

  const ctx = $derived({ dataModel: store.surfaces[surfaceId]?.dataModel ?? {}, scope });
  const label = $derived(toDisplayString(resolveDynamic(comp.label, ctx)));
  const value = $derived(toDisplayString(resolveDynamic(comp.value, ctx)));
  const delta = $derived(toDisplayString(resolveDynamic(comp.delta, ctx)));
  const intent = $derived(
    typeof comp.intent === 'string' && INTENTS.includes(comp.intent) ? comp.intent : 'neutral',
  );
</script>

<div class="tile {intent}" style:flex-grow={flexGrow(comp.weight)}>
  <span class="label">{label}</span>
  <span class="value">{value}</span>
  {#if delta}
    <span class="delta">{delta}</span>
  {/if}
</div>

<style>
  .tile {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    padding: 0.7rem 0.85rem;
    min-width: 0;
    background: var(--surface);
    border: 1px solid var(--border);
    border-left: 3px solid var(--border);
    border-radius: var(--radius-sm);
  }
  .label {
    color: var(--muted);
    font-size: 0.78rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .value {
    font-size: 1.4rem;
    font-weight: 600;
    line-height: 1.15;
    overflow-wrap: anywhere;
  }
  .delta {
    color: var(--muted);
    font-size: 0.82rem;
  }
  .tile.good {
    border-left-color: var(--ok);
  }
  .tile.good .value {
    color: var(--ok);
  }
  .tile.bad {
    border-left-color: var(--crit);
  }
  .tile.bad .value {
    color: var(--crit);
  }
  .tile.warn {
    border-left-color: var(--warn);
  }
  .tile.warn .value {
    color: var(--warn);
  }
</style>
