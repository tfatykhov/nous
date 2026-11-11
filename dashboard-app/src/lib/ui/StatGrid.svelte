<script lang="ts" module>
  /** The column count for n tiles: all in one row up to five, then as few rows of at most five as hold them, as even
   *  as they go (6: 3 + 3, 7: 4 + 3, 9: 5 + 4). It does not depend on the width, so no desktop width leaves one tile
   *  alone on its row (auto-fill at 200px fitted five at 1440 and four at 1280: five became four and one). */
  export function balancedCols(n: number): number {
    if (n <= 5) return Math.max(1, n);
    return Math.ceil(n / Math.ceil(n / 5));
  }
</script>

<script lang="ts">
  import type { Snippet } from 'svelte';

  /** tone colours the value (never alone: the label says what it is). */
  type Stat = {
    label: string;
    value: string | number;
    note?: string;
    tone?: 'waiting' | 'unknown' | 'error' | 'warn' | 'ok';
  };

  /** flat: the tiles sit inside a card, so they are a flat tint, not a shadow on a shadow (finish review fix 8).
   *  cols: the column count above phone width; by default it comes from the number of stats (balancedCols). A grid
   *  with extra tiles names it, since the grid cannot count a view's own tiles.
   *  extra: a view's own tiles (a coloured dot, a word), in the same grid so a row never ends in one lone tile. */
  let { stats, flat = false, cols, extra }: { stats: Stat[]; flat?: boolean; cols?: number; extra?: Snippet } = $props();
</script>

<div class="stat-grid" class:stat-grid--flat={flat} style:--cols={cols ?? balancedCols(stats.length)}>
  {#each stats as stat}
    <div class="stat-card">
      <div class="stat-value" class:tone-waiting={stat.tone === 'waiting'} class:tone-unknown={stat.tone === 'unknown'}
        class:tone-error={stat.tone === 'error'} class:tone-warn={stat.tone === 'warn'} class:tone-ok={stat.tone === 'ok'}
      >{stat.value}</div>
      <div class="stat-label">{stat.label}</div>
      {#if stat.note}<div class="stat-note">{stat.note}</div>{/if}
    </div>
  {/each}
  {@render extra?.()}
</div>

<style>
  .stat-grid {
    display: grid;
    grid-template-columns: repeat(var(--cols), minmax(0, 1fr));
    gap: 1rem;
  }

  /* Structure and tones read tokens (shared.style.test.ts pins each one). */
  .stat-card {
    background: var(--surface);
    border: var(--card-border);
    border-radius: var(--card-radius-sm);
    box-shadow: var(--card-shadow);
    padding: 1rem;
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  .stat-value {
    font-size: 1.5rem;
    font-weight: 700;
    color: var(--text);
    line-height: 1.2;
  }

  .stat-label {
    font-size: 0.75rem;
    font-weight: 500;
    color: var(--muted);
    text-transform: none;
    letter-spacing: 0;
  }

  .stat-note {
    font-size: 0.75rem;
    color: var(--muted);
  }

  .stat-grid--flat .stat-card { background: var(--wash); box-shadow: none; }


  .tone-waiting { color: var(--waiting); }
  .tone-unknown { color: var(--unknown); }
  .tone-error { color: var(--err-text); }
  .tone-warn { color: var(--warn-text); }
  .tone-ok { color: var(--ok-text); }

  @media (max-width: 640px) {
    /* Two up, and an odd last tile takes the whole row: never one lone tile (finish review fix 5). */
    .stat-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 0.75rem;
    }

    /* Any last tile, a view's extra tiles included. */
    .stat-grid > :global(:last-child:nth-child(odd)) {
      grid-column: 1 / -1;
    }

    .stat-card {
      padding: 1rem;
    }
  }
</style>
