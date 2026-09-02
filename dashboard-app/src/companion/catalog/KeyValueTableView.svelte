<script lang="ts">
  // nous-core KeyValueTable — `rows` is a DynamicValue, normally a binding to
  // an array of {key, value} objects. Anything that is not an array resolves
  // to no rows rather than throwing: a surface streamed in pieces legitimately
  // renders before its data model arrives.
  import { store } from '../store.svelte';
  import { flexGrow, omittedNote, resolveDynamic, splitTruncation, toDisplayString } from '../functions';
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
  // F096: a truncated source's trailing marker is not a row (codex P2 on #630).
  const split = $derived.by(() => {
    const resolved = resolveDynamic(comp.rows, ctx);
    return splitTruncation(Array.isArray(resolved) ? resolved : []);
  });
  const rows = $derived.by(() => {
    return split.rows.map((row) => {
      const record = (typeof row === 'object' && row !== null ? row : {}) as Record<string, unknown>;
      return { key: toDisplayString(record.key), value: toDisplayString(record.value) };
    });
  });
</script>

{#if rows.length > 0}
  <table class="kv" style:flex-grow={flexGrow(comp.weight)}>
    <tbody>
      {#each rows as row, i (i)}
        <tr>
          <th scope="row">{row.key}</th>
          <td>{row.value}</td>
        </tr>
      {/each}
    </tbody>
  </table>
{/if}
{#if split.omitted !== null}
  <div class="omitted">{omittedNote(split.omitted)}</div>
{/if}

<style>
  .kv {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.9rem;
  }
  th,
  td {
    text-align: left;
    vertical-align: top;
    padding: 0.35rem 0.5rem;
    border-bottom: 1px solid var(--border);
    overflow-wrap: anywhere;
  }
  tr:last-child th,
  tr:last-child td {
    border-bottom: none;
  }
  th {
    color: var(--muted);
    font-weight: 500;
    white-space: nowrap;
    width: 1%;
  }
  .omitted {
    color: var(--muted);
    font-size: 0.78rem;
    font-style: italic;
    padding: 0.35rem 0.5rem;
  }
</style>
