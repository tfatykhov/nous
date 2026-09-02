<script lang="ts">
  // F096 DataTable — a real table over record rows. `columns` is a LITERAL
  // array (schema-closed: key, label, align, secondary); `rows` resolves to
  // an array of objects. align:end right-aligns in tabular figures,
  // secondary de-emphasises a supporting column. Cells wrap — the table
  // never widens the page. No sorting/filtering/formatting: cells are shown
  // as the agent formatted them.
  import { store } from '../store.svelte';
  import { flexGrow, omittedNote, resolveDynamic, splitTruncation, toDisplayString } from '../functions';
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
  const split = $derived.by(() => {
    const resolved = resolveDynamic(comp.rows, ctx);
    return splitTruncation(Array.isArray(resolved) ? resolved : []);
  });
  const rows = $derived.by((): Record<string, unknown>[] => {
    return split.rows.filter((r) => typeof r === 'object' && r !== null) as Record<
      string,
      unknown
    >[];
  });
  const emptyText = $derived(
    typeof comp.emptyText === 'string' && comp.emptyText.trim() ? comp.emptyText : 'no rows',
  );
</script>

{#if columns.length > 0 && rows.length > 0}
  <!-- The scroll box is what keeps a wide table from widening the PAGE: cells
       wrap at word boundaries, numbers and dates never break mid-token, and
       a six-column table on a 390px phone scrolls inside its own box
       (F096 browser check: `overflow-wrap: anywhere` split "212" into
       "21/2" and dates across lines). -->
  <div class="scroll" style:flex-grow={flexGrow(comp.weight)}>
  <table class="dtable">
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
            {@const text = toDisplayString(row[col.key])}
            <!-- A single-token cell (a date, a count, an id) never breaks:
                 browsers treat "-" as a break opportunity, so "2026-08-31"
                 wrapped as "2026-/08-31" on a phone. Multi-word cells still
                 wrap at spaces; the scroll box takes any overflow. -->
            <td class:end={col.end} class:secondary={col.secondary} class:token={!/\s/.test(text)}>{text}</td>
          {/each}
        </tr>
      {/each}
    </tbody>
  </table>
  </div>
{:else if columns.length > 0 && split.omitted === null}
  <div class="empty">{emptyText}</div>
{/if}
{#if split.omitted !== null}
  <div class="omitted">{omittedNote(split.omitted)}</div>
{/if}

<style>
  .scroll {
    min-width: 0;
    max-width: 100%;
    overflow-x: auto;
  }
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
    /* Wrap at spaces only — never inside a token (a date split as "2026-/
       08-31" or a count as "21/2" misreads). The scroll box above absorbs a
       table whose unbreakable tokens do not fit the surface. */
    overflow-wrap: normal;
  }
  tr:last-child td {
    border-bottom: 0;
  }
  /* Figures and dates are single tokens: keep them whole (a broken "21/2"
     misreads as two numbers); the scroll box, not the page, takes the width. */
  th.end,
  td.end {
    text-align: right;
    font-family: var(--font-numeric);
    font-variant-numeric: tabular-nums;
  }
  td.end,
  td.token {
    white-space: nowrap;
  }
  td.secondary {
    color: var(--muted);
  }
  .empty,
  .omitted {
    color: var(--muted);
    font-size: 0.85rem;
    font-style: italic;
    padding: 0.4rem 0;
  }
  .omitted {
    font-size: 0.78rem;
  }
</style>
