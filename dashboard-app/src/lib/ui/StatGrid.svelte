<script lang="ts" module>
  /** The narrowest a tile may be: the widest real content ("0.028 good" with its dot, or a six-digit total with its
   *  "+312 in 7 days" note) at the 24px figure size, plus the tile's 16px padding each side (Codex round 3). */
  export const STAT_MIN_TILE = 160;
  /** The grid's gap above phone width (1rem). */
  export const STAT_GAP = 16;

  /**
   * The columns for n tiles in a grid `width` px wide (Codex round 3): as many as fit at STAT_MIN_TILE, at most `max`
   * (five), then as few rows as hold the tiles, as even as they go (5 in three columns: 3 + 2; 9: 5 + 4, or
   * 3 + 3 + 3). Where a last row would hold one tile it spans the row. With no width measured (0, as in jsdom) the
   * tile count alone decides.
   */
  export function statLayout(n: number, width: number, max = 5): { cols: number; spanLast: boolean } {
    const fit = width > 0 ? Math.max(1, Math.floor((width + STAT_GAP) / (STAT_MIN_TILE + STAT_GAP))) : max;
    const cap = Math.max(1, Math.min(max, fit, n));
    const cols = Math.ceil(n / Math.ceil(n / cap));
    return { cols, spanLast: n > 1 && cols > 1 && n % cols === 1 };
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
   *  cols: the most columns this grid wants (Execution integrity: three); the width may allow fewer (statLayout).
   *  extra: a view's own tiles (a coloured dot, a word), in the same grid so a row never ends in one lone tile. */
  let { stats, flat = false, cols, extra }: { stats: Stat[]; flat?: boolean; cols?: number; extra?: Snippet } = $props();

  // The grid's own width (the rail takes 220px of the window) and its tiles, a view's extra tiles counted.
  let width = $state(0);
  let grid = $state<HTMLElement>();
  let count = $state(0);
  $effect(() => {
    void stats.length;
    count = grid?.children.length ?? stats.length;
  });
  // Measured with a ResizeObserver where there is one (not in jsdom: there the width stays 0 and the count decides).
  $effect(() => {
    if (!grid || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(([entry]) => { width = entry.contentRect.width; });
    ro.observe(grid);
    return () => ro.disconnect();
  });
  const layout = $derived(statLayout(Math.max(count, stats.length), width, cols ?? 5));
</script>

<div class="stat-grid" class:stat-grid--flat={flat} class:stat-grid--span-last={layout.spanLast} style:--cols={layout.cols}
  bind:this={grid}>
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

  /* A last row of one tile: it spans the row (statLayout), so no tile stands alone. */
  .stat-grid--span-last > :global(:last-child) {
    grid-column: 1 / -1;
  }


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
