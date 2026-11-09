<script lang="ts">
  import { apiGet, type ApiError } from '../../lib/api';
  import type { WorkCallResult } from '../../lib/types/api';

  // The full, redacted result of one tool call (kept 7 days), fetched once on first open. It owns its state so the
  // trace poll, which swaps call objects by id, never collapses it. The text is shown as a text node only.
  let { callId }: { callId: string } = $props();

  const CUT_MARKER = '…[middle cut]…';
  let open = $state(false);
  let status = $state<'idle' | 'loading' | 'ready' | 'gone' | 'error'>('idle');
  let text = $state('');
  let panel = $state<HTMLDivElement>();
  let panelId = $derived(`full-result-${callId.replace(/[^A-Za-z0-9_-]/g, "_")}`);

  async function load() {
    status = 'loading';
    try {
      const r = await apiGet<WorkCallResult>(`/dashboard/work/calls/${encodeURIComponent(callId)}/result`, { retries: 1 });
      if (r.result === null) status = 'gone';
      else { text = r.result; status = 'ready'; }
    } catch (e) {
      status = (e as ApiError).status === 404 ? 'gone' : 'error';
    }
  }

  /** Retry removes the focused button, so focus moves to the panel instead of falling to the body. */
  function retry() {
    panel?.focus();
    void load();
  }

  function toggle() {
    open = !open;
    if (open && status === 'idle') void load();
  }
</script>

<div class="full">
  <button type="button" class="toggle" aria-expanded={open} aria-controls={panelId} onclick={toggle}>{open ? 'Hide full result' : 'Show full result'}</button>
  {#if open}
    <div id={panelId} class="panel" tabindex="-1" bind:this={panel}>
    {#if status === 'loading'}
      <p class="muted" role="status">Loading…</p>
    {:else if status === 'ready'}
      <p class="muted small">{text.length.toLocaleString('en-US')} characters{text.includes(CUT_MARKER) ? ' (cut at 20,000 characters)' : ''}</p>
      <div class="scroll"><pre class="wrap">{text}</pre></div>
    {:else if status === 'gone'}
      <p class="muted">Full result not available (kept 7 days).</p>
    {:else if status === 'error'}
      <p class="err" role="alert">Could not load the full result.</p>
      <button type="button" class="toggle" onclick={retry}>Retry</button>
    {/if}
    </div>
  {/if}
</div>

<style>
  .full { display: flex; flex-direction: column; gap: 6px; align-items: flex-start; min-width: 0; }
  .full > * { max-width: 100%; }
  .toggle { font: inherit; font-size: 0.8125rem; cursor: pointer; background: var(--surface); color: var(--text); border: 1px solid var(--border); border-radius: 8px; padding: 0.25rem 0.625rem; min-height: 32px; }
  .toggle:focus-visible { outline: 2px solid var(--accent-text); outline-offset: 2px; }
  .panel { display: flex; flex-direction: column; gap: 6px; width: 100%; outline: none; }
  .muted { color: var(--muted); margin: 0; font-size: 0.875rem; }
  .small { font-size: 0.75rem; }
  .err { color: var(--red); font-size: 0.875rem; margin: 0; }
  .scroll { max-height: 24rem; overflow: auto; width: 100%; background: var(--bg); border: 1px solid var(--border); border-radius: 8px; }
  .wrap { overflow-wrap: anywhere; white-space: pre-wrap; margin: 0; font-family: var(--font-mono); font-size: 0.8125rem; line-height: 1.5; padding: 0.5rem 0.625rem; }
  @media (max-width: 768px) { .toggle { min-height: 44px; } }
</style>
