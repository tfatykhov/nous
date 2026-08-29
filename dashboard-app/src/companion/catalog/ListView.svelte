<script lang="ts">
  // Basic catalog List — scrollable container; template expansion is the
  // common case (Children handles both forms).
  import Children from './Children.svelte';
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

<div
  class="list"
  class:horizontal={comp.direction === 'horizontal'}
  style:flex-grow={flexGrow(comp.weight)}
>
  <Children {surfaceId} children={comp.children} {scope} {depth} {ancestors} />
</div>

<style>
  .list {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    max-height: 60vh;
    overflow-y: auto;
  }
  .list.horizontal {
    flex-direction: row;
    overflow-x: auto;
    overflow-y: hidden;
    max-height: none;
  }
</style>
