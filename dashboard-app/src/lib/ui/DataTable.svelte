<script lang="ts">
  import type { Snippet } from 'svelte';

  type Col = { key: string; label: string };

  let {
    columns,
    rows,
    mode = 'scroll',
    rowKey = (r: any, i: number) => String(i),
    detail,
    cell,
    rowLabel,
    onrowclick,
  }: {
    columns: Col[];
    rows: any[];
    mode?: 'scroll' | 'cards';
    rowKey?: (r: any, i: number) => string;
    detail?: Snippet<[any]>;
    /** Custom cell content (status pills, links); falls back to row[key]. */
    cell?: Snippet<[any, Col]>;
    /** Names a row for its disclosure button ("Show details for <label>"). */
    rowLabel?: (r: any) => string;
    /** Called when a row is clicked (toggled), with the row value. */
    onrowclick?: (row: any) => void;
  } = $props();

  let expanded = $state<Record<string, boolean>>({});
  // Harness dashboard §4.6: a real <button> per expandable row, so the
  // detail is reachable by keyboard and announced (aria-expanded) — a click
  // handler on <tr> alone was mouse-only.
  const uid = `dt-${Math.random().toString(36).slice(2, 8)}`;

  const toggle = (k: string, row: any) => {
    expanded[k] = !expanded[k];
    onrowclick?.(row);
  };
</script>

<div class="dt" class:dt--cards={mode === 'cards'}>
  <table>
    <thead>
      <tr>
        {#if detail}<th class="dt-disclose-col"><span class="sr-only">Details</span></th>{/if}
        {#each columns as c}
          <th>{c.label}</th>
        {/each}
      </tr>
    </thead>
    <tbody>
      {#each rows as row, i (rowKey(row, i))}
        {@const k = rowKey(row, i)}
        {@const panelId = `${uid}-${i}`}
        <tr
          onclick={() => toggle(k, row)}
          class:expanded={expanded[k]}
        >
          {#if detail}
            <td class="dt-disclose-col">
              <button
                type="button"
                class="dt-disclose"
                aria-expanded={expanded[k] ? 'true' : 'false'}
                aria-controls={panelId}
                aria-label={`Show details for ${rowLabel ? rowLabel(row) : k}`}
                onclick={(e) => { e.stopPropagation(); toggle(k, row); }}
              >
                <svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                  <path d="M7 5l6 5-6 5" />
                </svg>
              </button>
            </td>
          {/if}
          {#each columns as c}
            <td data-label={c.label}>
              {#if cell}{@render cell(row, c)}{:else}{row[c.key]}{/if}
            </td>
          {/each}
        </tr>
        {#if detail && expanded[k]}
          <tr class="detail" id={panelId}>
            <td colspan={columns.length + 1}>
              {@render detail(row)}
            </td>
          </tr>
        {/if}
      {/each}
    </tbody>
  </table>
</div>

<style>
  .dt {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }

  table {
    width: 100%;
    border-collapse: collapse;
  }

  th,
  td {
    text-align: left;
    padding: 0.75rem;
    border-bottom: 1px solid var(--border);
  }

  th {
    color: var(--muted);
    font-size: 0.75rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  tr:hover td {
    background: var(--surface-hover);
  }

  tr.detail td {
    background: var(--surface);
    padding: 0.5rem 0.75rem;
  }

  .dt-disclose-col {
    width: 2.25rem;
    padding-right: 0;
  }

  .dt-disclose {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.75rem;
    height: 1.75rem;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    cursor: pointer;
  }

  .dt-disclose svg {
    transition: transform var(--transition);
  }

  .dt-disclose[aria-expanded='true'] svg {
    transform: rotate(90deg);
  }

  .dt-disclose:focus-visible {
    outline: 2px solid var(--accent-text);
    outline-offset: 2px;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
  }

  @media (prefers-reduced-motion: reduce) {
    .dt-disclose svg {
      transition: none;
    }
  }

  @media (max-width: 640px) {
    .dt--cards table,
    .dt--cards thead,
    .dt--cards tbody,
    .dt--cards tr,
    .dt--cards td {
      display: block;
    }

    .dt--cards thead {
      display: none;
    }

    .dt--cards tr {
      margin-bottom: 0.75rem;
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
    }

    .dt--cards td {
      display: flex;
      justify-content: space-between;
      border: none;
      padding: 0.75rem;
      /* 44px touch target via padding, not min-height (no-op in table layout) */
      padding-top: 0.6875rem;
      padding-bottom: 0.6875rem;
      align-items: center;
      border-bottom: 1px solid var(--border);
    }

    .dt--cards td:last-child {
      border-bottom: none;
    }

    /* The disclosure button leads the card: a 44px target. */
    .dt--cards td.dt-disclose-col {
      width: auto;
      justify-content: flex-end;
    }

    .dt--cards td.dt-disclose-col::before {
      content: none;
    }

    .dt--cards .dt-disclose {
      width: 2.75rem;
      height: 2.75rem;
    }

    .dt--cards td::before {
      content: attr(data-label);
      font-weight: 600;
      color: var(--muted);
      margin-right: 0.5rem;
      flex-shrink: 0;
    }

    /* Suppress data-label ::before on detail row */
    .dt--cards tr.detail td {
      display: block;
    }

    .dt--cards tr.detail td::before {
      content: none;
    }
  }
</style>
