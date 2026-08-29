<script lang="ts">
  // F092.1 StatRow — horizontal row of StatTiles. Pure delegate: children
  // render through the shared walker, threading depth/ancestors so the
  // cycle guard and depth cap keep working (rev-ui #8).
  import Children from './Children.svelte';
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

<div class="stat-row">
  <Children {surfaceId} children={comp.children} {scope} {depth} {ancestors} />
</div>

<style>
  .stat-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
    gap: 0.5rem;
  }
</style>
