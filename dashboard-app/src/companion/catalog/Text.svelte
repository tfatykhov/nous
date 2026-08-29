<script lang="ts">
  // Basic catalog Text. Markdown-lite (headings/bold/italic/code/links/lists)
  // is wave 2; wave 1 renders headings by prefix and everything else as plain
  // text — never {@html}.
  import { store } from '../store.svelte';
  import { resolveDynamic, toDisplayString } from '../functions';
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

  const ctx = $derived({ dataModel: store.surfaces[surfaceId]?.dataModel ?? {}, scope });
  const raw = $derived(toDisplayString(resolveDynamic(comp.text, ctx)));
  const heading = $derived(/^(#{1,3})\s+(.*)$/.exec(raw));
  const variant = $derived(typeof comp.variant === 'string' ? comp.variant : 'body');
</script>

{#if heading}
  {#if heading[1].length === 1}
    <h1>{heading[2]}</h1>
  {:else if heading[1].length === 2}
    <h2>{heading[2]}</h2>
  {:else}
    <h3>{heading[2]}</h3>
  {/if}
{:else}
  <span class="text" class:caption={variant === 'caption'} style:flex-grow={comp.weight ?? null}
    >{raw}</span
  >
{/if}

<style>
  h1,
  h2,
  h3 {
    margin: 0;
    line-height: 1.3;
  }
  h1 {
    font-size: 1.35rem;
  }
  h2 {
    font-size: 1.15rem;
  }
  h3 {
    font-size: 1rem;
  }
  .text {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .caption {
    color: var(--muted);
    font-size: 0.82rem;
  }
</style>
