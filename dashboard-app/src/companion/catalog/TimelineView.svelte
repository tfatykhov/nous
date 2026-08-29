<script lang="ts">
  // F092.1 Timeline — time-ordered event list (Briefing archetype). Own
  // DOM over a resolved DynamicValue array; index-keyed deliberately — two
  // entries at the same time are plausible from model-supplied data and an
  // `at`-keyed each would be a duplicate-key crash. Non-array resolves to
  // no rows, never throws: a surface can render before its data arrives.
  import { store } from '../store.svelte';
  import { resolveDynamic, toDisplayString } from '../functions';
  import type { Scope } from '../pointer';
  import type { A2uiComponent } from '../store.svelte';

  interface TimelineItem {
    at: string;
    label: string;
    detail: string;
    flag: boolean;
  }

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
  const items = $derived.by(() => {
    const resolved = resolveDynamic(comp.items, ctx);
    if (!Array.isArray(resolved)) return [];
    return resolved.map((row): TimelineItem => {
      const record = (typeof row === 'object' && row !== null ? row : {}) as Record<
        string,
        unknown
      >;
      return {
        at: toDisplayString(record.at),
        label: toDisplayString(record.label),
        detail: toDisplayString(record.detail),
        flag: record.flag === true,
      };
    });
  });
</script>

{#if items.length > 0}
  <ol class="timeline">
    {#each items as item, i (i)}
      <li class:flag={item.flag}>
        <span class="at">{item.at}</span>
        <span class="body">
          <span class="label">{item.label}</span>
          {#if item.detail}
            <span class="detail">{item.detail}</span>
          {/if}
        </span>
      </li>
    {/each}
  </ol>
{/if}

<style>
  .timeline {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  li {
    display: flex;
    gap: 0.7rem;
    padding: 0.35rem 0;
    border-left: 2px solid var(--border);
    padding-left: 0.7rem;
  }
  li.flag {
    border-left-color: var(--yellow);
  }
  .at {
    color: var(--muted);
    font-size: 0.8rem;
    white-space: nowrap;
    min-width: 5.5rem;
    font-variant-numeric: tabular-nums;
  }
  .body {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }
  .label {
    overflow-wrap: anywhere;
  }
  li.flag .label {
    color: var(--yellow);
  }
  .detail {
    color: var(--muted);
    font-size: 0.82rem;
    overflow-wrap: anywhere;
  }
</style>
