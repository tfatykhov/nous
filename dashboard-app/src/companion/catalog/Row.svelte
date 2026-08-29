<script lang="ts">
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

  const JUSTIFY: Record<string, string> = {
    start: 'flex-start',
    center: 'center',
    end: 'flex-end',
    spaceBetween: 'space-between',
    spaceAround: 'space-around',
    spaceEvenly: 'space-evenly',
    stretch: 'stretch',
  };
</script>

<div
  class="row"
  style:justify-content={JUSTIFY[comp.justify as string] ?? null}
  style:align-items={JUSTIFY[comp.align as string] ?? null}
  style:flex-grow={comp.weight ?? null}
>
  <Children {surfaceId} children={comp.children} {scope} {depth} {ancestors} />
</div>

<style>
  .row {
    display: flex;
    flex-direction: row;
    flex-wrap: wrap;
    gap: 0.6rem;
    align-items: center;
  }
</style>
