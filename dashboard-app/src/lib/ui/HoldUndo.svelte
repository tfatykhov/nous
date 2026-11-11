<script lang="ts">
  // The two operator statements that settle a send whose outcome is unknown, for one execution-ledger row. Shared
  // by the Ledger's attention list and the Work trace. Nothing here runs SQL: the operator copies a statement.
  let { id }: { id: string } = $props();

  let okEl = $state<HTMLElement | null>(null);
  let relEl = $state<HTMLElement | null>(null);
  let copied = $state('');

  // Clipboard API needs a secure context; the LAN host serves plain http, so fall back to selecting the text.
  async function copySql(el: HTMLElement | null) {
    const text = el?.textContent ?? '';
    try {
      await navigator.clipboard.writeText(text);
      copied = 'Copied to the clipboard.';
    } catch {
      if (el) {
        const range = document.createRange();
        range.selectNodeContents(el);
        const sel = window.getSelection();
        sel?.removeAllRanges();
        sel?.addRange(range);
      }
      copied = 'Selected — press Ctrl+C (or ⌘C) to copy.';
    }
  }
</script>

<div class="hold-undo">
  <div>
    <div class="sql-title">They got it — retries are answered “already sent”</div>
    <code class="sql" bind:this={okEl}>UPDATE nous_system.execution_ledger SET status = 'success',
  result_summary = concat_ws(' · ', result_summary, 'confirmed delivered by operator')
WHERE id = '{id}' AND status = 'unknown';</code>
    <button type="button" class="btn" onclick={() => copySql(okEl)}>Copy SQL: they got it</button>
  </div>
  <div>
    <div class="sql-title">Nobody got it — the next retry sends</div>
    <code class="sql" bind:this={relEl}>UPDATE nous_system.execution_ledger SET status = 'error',
  result_summary = concat_ws(' · ', result_summary, 'released by operator')
WHERE id = '{id}' AND status = 'unknown';</code>
    <button type="button" class="btn" onclick={() => copySql(relEl)}>Copy SQL: nobody got it</button>
  </div>
  <p class="note">Releasing re-sends nothing by itself — retry the step to send again. If only some recipients got it, keep the hold.</p>
  <p class="sr-only" aria-live="polite">{copied}</p>
</div>

<style>
  .hold-undo { display: flex; flex-direction: column; gap: 0.625rem; }
  .sql-title { font-size: 0.8125rem; font-weight: 600; }
  .hold-undo .btn { margin-top: 0.375rem; }
  .sql { display: block; margin-top: 0.25rem; padding: 0.5rem 0.625rem; border-radius: var(--card-radius-sm); background: var(--bg);
    border: 1px solid var(--border); font-family: var(--font-mono); font-size: 0.6875rem; line-height: 1.6;
    white-space: pre-wrap; overflow-wrap: anywhere; }
  .note { font-size: 0.75rem; color: var(--muted); margin: 0; }
  .btn { min-height: 32px; padding: 0 0.75rem; border-radius: var(--ctl-radius); border: 1px solid var(--border); background: var(--surface);
    color: var(--text); font-family: inherit; font-size: 0.75rem; font-weight: 500; cursor: pointer; }
  .btn:hover { background: var(--surface-hover); }
  .btn:focus-visible { outline: 2px solid var(--accent-text); outline-offset: 2px; }
  .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  @media (max-width: 768px) {
    .btn { min-height: 44px; }
  }
</style>
