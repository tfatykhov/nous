<script lang="ts">
  import type { Snippet } from 'svelte';

  type Col = { key: string; label: string };

  let {
    columns,
    rows,
    mode = 'scroll',
    rowKey = (r: any, i: number) => String(i),
    detail,
  }: {
    columns: Col[];
    rows: any[];
    mode?: 'scroll' | 'cards';
    rowKey?: (r: any, i: number) => string;
    detail?: Snippet<[any]>;
  } = $props();

  let expanded = $state<Record<string, boolean>>({});

  const toggle = (k: string) => {
    expanded[k] = !expanded[k];
  };
</script>

<div class="dt" class:dt--cards={mode === 'cards'}>
  <table>
    <thead>
      <tr>
        {#each columns as c}
          <th>{c.label}</th>
        {/each}
      </tr>
    </thead>
    <tbody>
      {#each rows as row, i (rowKey(row, i))}
        <tr
          onclick={() => toggle(rowKey(row, i))}
          class:expanded={expanded[rowKey(row, i)]}
        >
          {#each columns as c}
            <td data-label={c.label}>{row[c.key]}</td>
          {/each}
        </tr>
        {#if detail && expanded[rowKey(row, i)]}
          <tr class="detail">
            <td colspan={columns.length}>
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
