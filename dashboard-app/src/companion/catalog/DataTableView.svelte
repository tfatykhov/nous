<script lang="ts">
  // F096 DataTable — a real table over record rows. `columns` is a LITERAL
  // array (schema-closed: key, label, align, secondary); `rows` resolves to
  // an array of objects. align:end right-aligns in tabular figures,
  // secondary de-emphasises a supporting column. Cells wrap — the table
  // never widens the page. No sorting/filtering/formatting: cells are shown
  // as the agent formatted them.
  import { store } from '../store.svelte';
  import { flexGrow, resolveDynamic, toDisplayString } from '../functions';
  import type { Scope } from '../pointer';
  import type { A2uiComponent } from '../store.svelte';

  interface Column {
    key: string;
    label: string;
    end: boolean;
    secondary: boolean;
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
  const columns = $derived.by((): Column[] => {
    if (!Array.isArray(comp.columns)) return [];
    return comp.columns
      .filter((c) => typeof c === 'object' && c !== null && typeof c.key === 'string' && c.key)
      .slice(0, 6)
      .map((c) => ({
        key: c.key as string,
        label: toDisplayString(c.label ?? c.key),
        end: c.align === 'end',
        secondary: c.secondary === true,
      }));
  });
  const rows = $derived.by((): Record<string, unknown>[] => {
    const resolved = resolveDynamic(comp.rows, ctx);
    if (!Array.isArray(resolved)) return [];
    return resolved.filter((r) => typeof r === 'object' && r !== null) as Record<
      string,
      unknown
    >[];
  });
  const emptyText = $derived(
    typeof comp.emptyText === 'string' && comp.emptyText.trim() ? comp.emptyText : 'no rows',
  );
</script>

{#if columns.length > 0 && rows.length > 0}
  <table class="dtable" style:flex-grow={flexGrow(comp.weight)}>
    <thead>
      <tr>
        <!-- Keyed by INDEX, not col.key: the grammar rejects duplicate keys at
             compose time, but the renderer never trusts its input, and Svelte's
             keyed each throws on a duplicate key in prod too — one bad table
             would take down the whole surface (the LineChart series precedent). -->
        {#each columns as col, i (i)}
          <th class:end={col.end} class:secondary={col.secondary}>{col.label}</th>
        {/each}
      </tr>
    </thead>
    <tbody>
      {#each rows as row, i (i)}
        <tr>
          {#each columns as col, j (j)}
            <td class:end={col.end} class:secondary={col.secondary}>{toDisplayString(row[col.key])}</td>
          {/each}
        </tr>
      {/each}
    </tbody>
  </table>
{:else if columns.length > 0}
  <div class="empty">{emptyText}</div>
{/if}

<style>
  .dtable {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.85rem;
    table-layout: auto;
  }
  th {
    text-align: left;
    color: var(--muted);
    font-weight: 500;
    font-size: 0.74rem;
    padding: 0.35rem 0.5rem;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }
  td {
    padding: 0.45rem 0.5rem;
    border-bottom: 1px solid var(--border);
    vertical-align: top;
    overflow-wrap: anywhere;
  }
  tr:last-child td {
    border-bottom: 0;
  }
  th.end,
  td.end {
    text-align: right;
    font-family: var(--font-numeric);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  td.secondary {
    color: var(--muted);
  }
  .empty {
    color: var(--muted);
    font-size: 0.85rem;
    font-style: italic;
    padding: 0.4rem 0;
  }
</style>
