<script lang="ts">
  import { onMount } from 'svelte';
  import { apiGet, type ApiError } from '../../lib/api';
  import type { ModelCallBlock, ModelCallBlockBody } from '../../lib/types/api';

  // One stored text block of a call, as a disclosure. The body is fetched when the block is first opened (a block
  // that loads open fetches on mount), once; a failed fetch can be retried by closing and opening again. The text
  // came from a model or a user and is shown as a text node only.
  let { callId, block, title, open = false }: { callId: string; block: ModelCallBlock; title: string; open?: boolean } = $props();

  let started = false;
  let body = $state<ModelCallBlockBody | null>(null);
  let failure = $state<'' | 'gone' | 'failed'>('');

  async function load() {
    if (started) return;
    started = true;
    try {
      body = await apiGet<ModelCallBlockBody>(
        `/dashboard/model-calls/${encodeURIComponent(callId)}/blocks/${block.part}/${block.ord}`,
      );
    } catch (e) {
      started = false;
      failure = (e as ApiError).status === 404 ? 'gone' : 'failed';
    }
  }

  onMount(() => { if (open) void load(); });

  const meta = $derived(
    block.reused_by > 1
      ? `about ${block.tokens_est.toLocaleString('en-US')} tokens, sent in ${block.reused_by.toLocaleString('en-US')} calls in the last 7 days`
      : `about ${block.tokens_est.toLocaleString('en-US')} tokens, this call only`,
  );
</script>

<details class="block" {open} ontoggle={(e) => { if (e.currentTarget.open) { failure = ''; void load(); } }}>
  <summary><span class="title">{title}</span><span class="meta">{meta}</span></summary>
  <div class="inner">
    {#if body}
      <p class="size">{body.size_chars != null ? body.size_chars.toLocaleString('en-US') : '—'} characters</p>
      <div class="scroll"><pre class="wrap">{body.body}</pre></div>
    {:else if failure === 'gone'}
      <p class="size">This text is no longer stored.</p>
    {:else if failure === 'failed'}
      <p class="size">Could not load this text. Close and open the block to try again.</p>
    {:else}
      <p class="size">Loading…</p>
    {/if}
  </div>
</details>

<style>
  .block { border: 1px solid var(--border); border-radius: 8px; background: var(--bg); }
  summary { padding: 0.625rem 0.75rem; cursor: pointer; display: flex; flex-wrap: wrap; gap: 6px 14px; align-items: baseline; font-size: 0.875rem; }
  summary:focus-visible { outline: 2px solid var(--accent-text); outline-offset: 2px; }
  .title { font-weight: 600; }
  .meta { font-size: 0.75rem; color: var(--muted); }
  .inner { padding: 0 0.75rem 0.75rem; }
  .size { margin: 0 0 6px; font-size: 0.75rem; color: var(--muted); }
  .scroll { max-height: 480px; overflow: auto; border-top: 1px solid var(--border); padding-top: 8px; }
  pre.wrap { margin: 0; font-family: var(--font-mono); font-size: 0.75rem; line-height: 1.55; color: var(--text); white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
