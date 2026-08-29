<script lang="ts">
  // F092: adjacency-list walker. Looks up the component by id, dispatches to
  // the catalog adapter, and guards the recursion — placeholders (never
  // exceptions) for dangling refs, unknown components, cycles, and depth
  // overflow, because progressive rendering depends on partial trees
  // rendering quietly.
  import { store } from './store.svelte';
  import { registry } from './catalog/index';
  import type { Scope } from './pointer';

  const MAX_DEPTH = 64;

  let {
    surfaceId,
    componentId,
    scope = null,
    depth = 0,
    ancestors = [],
  }: {
    surfaceId: string;
    componentId: string;
    scope?: Scope | null;
    depth?: number;
    ancestors?: readonly string[];
  } = $props();

  const surface = $derived(store.surfaces[surfaceId]);
  const comp = $derived(surface?.components[componentId]);
  const Adapter = $derived(comp ? registry[comp.component] : undefined);
  const cycle = $derived(ancestors.includes(componentId));
</script>

{#if depth > MAX_DEPTH || cycle}
  <span class="ph" title="cycle or depth limit at {componentId}">⟳</span>
{:else if !surface || !comp}
  <span class="ph" title="waiting for {componentId}">…</span>
{:else if !Adapter}
  <span class="ph" title="unknown component {comp.component}">[{comp.component}]</span>
{:else}
  <Adapter
    {surfaceId}
    {comp}
    {scope}
    depth={depth + 1}
    ancestors={[...ancestors, componentId]}
  />
{/if}

<style>
  .ph {
    color: var(--muted);
    font-size: 0.85em;
  }
</style>
