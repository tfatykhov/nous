<script lang="ts">
  // F096 DeltaList — ranked movers: label · tone-coloured delta · from → to.
  // An EMPTY list is a state, never a blank block: "no significant adverse
  // moves" is the good news the page exists to show, so it renders emptyText.
  // Strings are preformatted; row tones are closed by normalizeTone.
  import { store } from '../store.svelte';
  import { flexGrow, omittedNote, resolveDynamic, splitTruncation, toDisplayString } from '../functions';
  import { normalizeTone, toneInkVar } from '../chart';
  import type { Scope } from '../pointer';
  import type { A2uiComponent } from '../store.svelte';

  interface Row {
    label: string;
    delta: string;
    range: string;
    ink: string;
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
  const emptyText = $derived(
    typeof comp.emptyText === 'string' && comp.emptyText.trim()
      ? comp.emptyText
      : 'nothing to report',
  );
  const split = $derived.by(() => {
    const resolved = resolveDynamic(comp.rows, ctx);
    return splitTruncation(Array.isArray(resolved) ? resolved : []);
  });
  const rows = $derived.by((): Row[] => {
    return split.rows.map((row) => {
      const r = (typeof row === 'object' && row !== null ? row : {}) as Record<string, unknown>;
      const from = toDisplayString(r.from);
      const to = toDisplayString(r.to);
      return {
        label: toDisplayString(r.label),
        delta: toDisplayString(r.delta),
        range: from && to ? `${from} → ${to}` : from || to,
        ink: toneInkVar(normalizeTone(r.tone)),
      };
    });
  });
</script>

<ul class="deltas" style:flex-grow={flexGrow(comp.weight)}>
  {#if rows.length === 0}
    <li class="none"><span class="empty">{emptyText}</span></li>
  {:else}
    {#each rows as row, i (i)}
      <li style:--row-ink={row.ink}>
        <span class="n">{row.label}</span>
        <span class="d">{row.delta}</span>
        <span class="r">{row.range}</span>
      </li>
    {/each}
  {/if}
  {#if split.omitted !== null}
    <li class="none"><span class="omitted">{omittedNote(split.omitted)}</span></li>
  {/if}
</ul>

<style>
  .deltas {
    list-style: none;
    margin: 0;
    padding: 0;
    min-width: 0;
  }
  li {
    display: grid;
    grid-template-columns: 1fr auto auto;
    gap: 0.75rem;
    align-items: center;
    padding: 0.45rem 0;
    border-bottom: 1px solid var(--border);
  }
  li:last-child {
    border-bottom: 0;
  }
  li.none {
    display: block;
  }
  .n {
    font-size: 0.9rem;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .d {
    color: var(--row-ink);
    font-family: var(--font-numeric);
    font-variant-numeric: tabular-nums;
    font-size: 0.85rem;
    font-weight: 700;
    white-space: nowrap;
  }
  .r {
    color: var(--muted);
    font-family: var(--font-numeric);
    font-variant-numeric: tabular-nums;
    font-size: 0.72rem;
    min-width: 6.5rem;
    text-align: right;
    white-space: nowrap;
  }
  .empty,
  .omitted {
    color: var(--muted);
    font-size: 0.85rem;
    font-style: italic;
  }
  .omitted {
    font-size: 0.78rem;
  }
</style>
