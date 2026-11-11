<script lang="ts">
  // The look switch: the paper redesign on or off, remembered per browser (src/lib/look.ts).
  import { look, setLook } from '$lib/look';

  let on = $derived($look === 'paper');
</script>

<button type="button" role="switch" aria-checked={on} class="look-toggle" onclick={() => setLook(on ? 'dark' : 'paper')}>
  <span class="look-label">New look</span>
  <span class="look-state" class:on aria-hidden="true">{on ? 'On' : 'Off'}</span>
</button>

<style>
  .look-toggle {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    min-height: 32px;
    padding: 0.25rem 0.375rem 0.25rem 0.75rem;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: transparent;
    color: var(--text);
    font: inherit;
    font-size: 0.8125rem;
    font-weight: 500;
    cursor: pointer;
    transition: background var(--transition);
  }

  .look-toggle:hover {
    background: var(--surface-hover);
  }

  .look-state {
    min-width: 2.25rem;
    padding: 0.0625rem 0.5rem;
    border-radius: 999px;
    color: var(--muted);
    font-size: 0.75rem;
    font-weight: 600;
    text-align: center;
  }

  .look-state.on {
    background: var(--accent-glow);
    color: var(--accent-text);
  }

  @media (max-width: 768px) {
    .look-toggle { min-height: 44px; }
  }
</style>
