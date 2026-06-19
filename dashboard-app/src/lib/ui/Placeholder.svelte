<script lang="ts">
  let { route }: { route: string } = $props();
</script>

<div class="placeholder">
  <div class="placeholder-icon" aria-hidden="true">🚧</div>
  <p class="placeholder-text">
    <strong>{route}</strong> — this view is being migrated. Use the classic dashboard at
    <a href="/dashboard/" class="placeholder-link">/dashboard/</a> for now.
  </p>
</div>

<style>
  .placeholder {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 1rem;
    padding: 3rem 2rem;
    text-align: center;
    color: var(--muted);
    min-height: 200px;
  }

  .placeholder-icon {
    font-size: 2.5rem;
    line-height: 1;
  }

  .placeholder-text {
    font-size: 0.9375rem;
    line-height: 1.6;
    max-width: 480px;
  }

  .placeholder-text strong {
    color: var(--text);
    font-weight: 600;
  }

  .placeholder-link {
    color: var(--accent);
    text-decoration: underline;
    text-underline-offset: 2px;
  }

  .placeholder-link:hover {
    color: var(--text);
  }
</style>
