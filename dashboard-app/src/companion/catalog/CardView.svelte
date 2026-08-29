<script lang="ts">
  import Renderer from '../Renderer.svelte';
  import { flexGrow } from '../functions';
  import type { Scope } from '../pointer';
  import type { A2uiComponent } from '../store.svelte';

  let {
    surfaceId,
    comp,
    scope = null,
    depth = 0,
    ancestors = [],
  }: {
    surfaceId: string;
    comp: A2uiComponent;
    scope?: Scope | null;
    depth?: number;
    ancestors?: readonly string[];
  } = $props();
</script>

<div class="card" style:flex-grow={flexGrow(comp.weight)}>
  {#if typeof comp.child === 'string'}
    <Renderer {surfaceId} componentId={comp.child} {scope} {depth} {ancestors} />
  {/if}
</div>

<style>
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 0.9rem;
  }
</style>
