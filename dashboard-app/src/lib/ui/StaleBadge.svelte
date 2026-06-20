<script lang="ts">
  import type { PollState } from '../stores/registry';

  let { state }: { state: PollState<unknown> } = $props();

  function formatAgo(ts: number | null): string {
    if (ts === null) return '';
    const secs = Math.floor((Date.now() - ts) / 1000);
    if (secs < 60) return `${secs}s ago`;
    const mins = Math.floor(secs / 60);
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    return `${hours}h ago`;
  }

  let label = $derived(
    state.error
      ? 'error — retrying'
      : state.loading && state.data === null
        ? 'loading…'
        : state.lastUpdated !== null
          ? `updated ${formatAgo(state.lastUpdated)}`
          : 'loading…'
  );

  let variant = $derived(
    state.error ? 'error' : state.loading && state.data === null ? 'loading' : 'ok'
  );
</script>

<span class="stale-badge" class:error={variant === 'error'} class:loading={variant === 'loading'}>
  {label}
</span>

<style>
  .stale-badge {
    display: inline-flex;
    align-items: center;
    font-size: 0.7rem;
    font-weight: 500;
    padding: 0.125rem 0.5rem;
    border-radius: 999px;
    background: var(--surface);
    color: var(--muted);
    border: 1px solid var(--border);
    white-space: nowrap;
  }

  .stale-badge.error {
    color: var(--red);
    border-color: var(--red);
    background: rgba(248, 113, 113, 0.08);
  }

  .stale-badge.loading {
    color: var(--muted);
    animation: pulse 1.5s ease-in-out infinite;
  }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.5; }
  }
</style>
