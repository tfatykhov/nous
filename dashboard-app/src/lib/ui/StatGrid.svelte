<script lang="ts">
  /** tone colours the value (never alone: the label says what it is). */
  type Stat = {
    label: string;
    value: string | number;
    note?: string;
    tone?: 'waiting' | 'unknown' | 'error' | 'warn' | 'ok';
  };

  let { stats }: { stats: Stat[] } = $props();
</script>

<div class="stat-grid">
  {#each stats as stat}
    <div class="stat-card">
      <div class="stat-value" class:tone-waiting={stat.tone === 'waiting'} class:tone-unknown={stat.tone === 'unknown'}
        class:tone-error={stat.tone === 'error'} class:tone-warn={stat.tone === 'warn'} class:tone-ok={stat.tone === 'ok'}
      >{stat.value}</div>
      <div class="stat-label">{stat.label}</div>
      {#if stat.note}<div class="stat-note">{stat.note}</div>{/if}
    </div>
  {/each}
</div>

<style>
  .stat-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 1rem;
  }

  .stat-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm, 8px);
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
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .stat-note {
    font-size: 0.75rem;
    color: var(--muted);
  }

  .tone-waiting { color: var(--waiting); }
  .tone-unknown { color: var(--unknown); }
  .tone-error { color: var(--red); }
  .tone-warn { color: #f59e0b; }
  .tone-ok { color: #10b981; }

  @media (max-width: 640px) {
    .stat-grid {
      grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
      gap: 0.75rem;
    }

    .stat-card {
      padding: 1rem;
    }
  }
</style>
