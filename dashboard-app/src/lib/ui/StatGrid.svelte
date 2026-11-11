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
   *  cols: a fixed column count above phone width (six tiles as two rows of three, never five and one).
   *  extra: a view's own tiles (a coloured dot, a word), in the same grid so a row never ends in one lone tile. */
  let { stats, flat = false, cols, extra }: { stats: Stat[]; flat?: boolean; cols?: number; extra?: Snippet } = $props();
</script>

<div class="stat-grid" class:stat-grid--flat={flat} class:stat-grid--cols={cols != null} style:--cols={cols}>
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
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
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

  .stat-grid--cols {
    grid-template-columns: repeat(var(--cols), minmax(0, 1fr));
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
