<script lang="ts">
  // A ground: the box whose tokens are the paper (or dark) set under the paper look. A dark ground inside a paper
  // page is an island for a screen the redesign has not migrated yet (it renders as today). With the look off it
  // changes nothing.
  import type { Snippet } from 'svelte';

  let {
    ground,
    class: className = '',
    children,
  }: { ground: 'paper' | 'dark'; class?: string; children?: Snippet } = $props();
</script>

<div data-ground={ground} class={['ground', className]}>{@render children?.()}</div>
