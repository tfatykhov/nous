<script lang="ts">
  type Stat = { label: string; value: string | number };

  let { stats }: { stats: Stat[] } = $props();
</script>

<div class="stat-grid">
  {#each stats as stat}
    <div class="stat-card">
      <div class="stat-value">{stat.value}</div>
      <div class="stat-label">{stat.label}</div>
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
