<script lang="ts">
  import { apiGet, type ApiError } from '../../lib/api';
  import { makePollStore } from '../../lib/stores/registry';
  import { usePoll } from '../../lib/poll';
  import { routeHref } from '../../lib/router';
  import { fmtDuration, fmtTime, fmtTokens } from '../../lib/work';
  import type { ModelCallBlock, ModelCallDetail } from '../../lib/types/api';
  import CallBlock from './CallBlock.svelte';

  // F108 Model call detail: one call's numbers, attempts, context and stored text. Fetched once per open; the
  // bodies of the text blocks are fetched by CallBlock when a block is opened. Strings are shown as text only.
  let { callId }: { callId: string } = $props();

  const store = usePoll(
    makePollStore<ModelCallDetail>((signal) => apiGet<ModelCallDetail>(`/dashboard/model-calls/${encodeURIComponent(callId)}`, { signal }), 0),
  );

  let data = $derived($store.data);
  let notFound = $derived(($store.error as ApiError | null)?.status === 404 && !data);
  let call = $derived(data?.call ?? null);

  const PART_NAMES: Record<string, string> = { system: 'System', messages: 'Messages', tools: 'Tools', response: 'Response' };
  const TEXT_SENTENCES: Record<string, string> = {
    off: 'Prompt text is not recorded. Set NOUS_MODEL_CALL_TEXT_ENABLED.',
    expired: 'Prompt text is kept 7 days and this call is older.',
    not_recorded: 'Prompt text was not recorded for this call.',
  };
  function blockTitle(b: ModelCallBlock, all: ModelCallBlock[]): string {
    const name = PART_NAMES[b.part] ?? b.part;
    const same = all.filter((x) => x.part === b.part);
    return same.length > 1 ? `${name} ${same.indexOf(b) + 1}` : name;
  }

  let tiles = $derived(
    call
      ? [
          { label: 'Uncached input', value: fmtTokens(call.input_uncached) },
          { label: 'Cache read', value: fmtTokens(call.cache_read) },
          { label: 'Cache write', value: fmtTokens(call.cache_write) },
          { label: 'Total input', value: fmtTokens(call.input_total) },
          { label: 'Output', value: fmtTokens(call.output_tokens) },
          { label: 'Duration', value: fmtDuration(call.duration_ms) },
          { label: 'Attempts', value: String(call.attempts) },
          { label: 'Stop reason', value: call.stop_reason ?? '—' },
        ]
      : [],
  );
  let sectionTotal = $derived((data?.sections ?? []).reduce((a, s) => a + s.tokens_est, 0));
  const PALETTE = ['#a99df9', '#60a5fa', '#22d3ee', '#34d399', '#fbbf24', '#fb923c', '#f472b6', '#9ca3af', '#c4c4d8'];
  const madeBy = (t: NonNullable<ModelCallDetail['turn']>) => t.session_title || t.session_id || '';
</script>

<div class="detail">
  {#if notFound}
    <p class="gone" role="status">Call not found (numbers are kept 30 days).</p>
  {:else if data && !data.enabled}
    <div class="notice" role="status">Model-call recording is off. Set NOUS_MODEL_CALL_LOG_ENABLED.</div>
  {:else if data && call}
    <section class="card">
      <div class="title-row">
        <h2>{call.purpose.replaceAll('_', ' ')}</h2>
        <span class="mono">{call.model ?? '—'}</span>
        <span class="muted">{fmtTime(call.started_at)}</span>
      </div>
      {#if data.turn?.session_id}
        <div class="made">Made by <a href={routeHref('work', ['s', data.turn.session_id, data.turn.turn_id])}>{madeBy(data.turn)}</a></div>
      {/if}
      {#if call.error_text}<pre class="wrap err">{call.error_text}</pre>{/if}
      {#if data.attempts.length > 0}
        <ol class="attempts">
          {#each data.attempts as a (a.attempt_no)}
            {@const failed = a.error_type != null || (a.http_status != null && a.http_status >= 400)}
            <li class:failed>
              {#if failed}<strong>Failed:</strong>{/if} Attempt {a.attempt_no}{#if a.http_status != null}, HTTP {a.http_status}{/if}{#if a.error_type}, {a.error_type}{/if}, {fmtDuration(a.duration_ms)}
              {#if a.error_text}<span class="wrap"> {a.error_text}</span>{/if}
            </li>
          {/each}
        </ol>
      {/if}
    </section>

    <section class="tiles" aria-label="Tokens and timing">
      {#each tiles as t (t.label)}
        <div class="tile"><span class="muted">{t.label}</span><span class="val">{t.value}</span></div>
      {/each}
    </section>

    <section class="card sections">
      <h3>What filled the context</h3>
      {#if data.sections.length === 0}
        <p class="muted">No section breakdown for this call (only the main loop records one).</p>
      {:else}
        <div class="bars">
          {#each data.sections as s, i (s.name)}
            <span class="bar" title={`${s.name}: ${s.tokens_est.toLocaleString('en-US')} tokens`}
              style:width={`${sectionTotal ? (s.tokens_est / sectionTotal) * 100 : 0}%`} style:background={PALETTE[i % PALETTE.length]}></span>
          {/each}
        </div>
        <ul class="legend">
          {#each data.sections as s, i (s.name)}
            <li><i class="dot" aria-hidden="true" style:background={PALETTE[i % PALETTE.length]}></i><span class="grow">{s.name}</span><span class="muted">{s.tokens_est.toLocaleString('en-US')}</span></li>
          {/each}
        </ul>
      {/if}
    </section>

    <section class="card">
      <h3>Full prompt and response</h3>
      {#if data.text_status === 'ok' && data.blocks.length > 0}
        <p class="muted">Blocks shared with other calls are stored once; the note on each says how many calls reused it this week.</p>
        {#each data.blocks as b (`${b.part}/${b.ord}`)}
          <CallBlock {callId} block={b} title={blockTitle(b, data.blocks)} open={b.part === 'response'} />
        {/each}
      {:else if data.text_status === 'ok'}
        <p class="muted">{TEXT_SENTENCES.not_recorded}</p>
      {:else}
        <p class="muted">{TEXT_SENTENCES[data.text_status] ?? TEXT_SENTENCES.not_recorded}</p>
      {/if}
    </section>
  {:else if $store.error}
    <p class="err" role="alert">Could not load this call.</p>
  {:else}
    <p class="muted" role="status">Loading…</p>
  {/if}
</div>

<style>
  .detail { display: flex; flex-direction: column; gap: 1rem; min-width: 0; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 1rem 1.25rem; display: flex; flex-direction: column; gap: 0.625rem; min-width: 0; }
  h2 { margin: 0; font-size: 1.25rem; font-weight: 700; }
  h3 { margin: 0; font-size: 1rem; font-weight: 600; }
  .title-row { display: flex; flex-wrap: wrap; gap: 6px 16px; align-items: baseline; }
  .mono { font-family: var(--font-mono); font-size: 0.8125rem; overflow-wrap: anywhere; }
  .muted { color: var(--muted); font-size: 0.8125rem; margin: 0; }
  .made { font-size: 0.875rem; }
  .made a { color: var(--accent-text); }
  .gone { color: var(--muted); }
  .err { color: var(--red); font-size: 0.875rem; margin: 0; }
  .notice { padding: 0.75rem 1rem; border-radius: 8px; background: rgba(245, 158, 11, 0.08); border: 1px solid rgba(245, 158, 11, 0.4); font-size: 0.875rem; }
  .wrap { white-space: pre-wrap; overflow-wrap: anywhere; margin: 0; }
  pre.err { color: var(--red); font-family: var(--font-mono); font-size: 0.75rem; max-height: 320px; overflow: auto; }
  .attempts { margin: 0; padding-left: 1.25rem; display: flex; flex-direction: column; gap: 4px; font-size: 0.8125rem; }
  .attempts li.failed { color: var(--red); }
  .tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 10px; }
  .tile { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 0.75rem 0.875rem; display: flex; flex-direction: column; gap: 4px; }
  .val { font-size: 1.125rem; font-weight: 600; overflow-wrap: anywhere; }
  .bars { display: flex; height: 14px; border-radius: 4px; overflow: hidden; gap: 2px; }
  .bar { display: block; min-width: 2px; }
  .legend { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 6px 24px; font-size: 0.8125rem; }
  .legend li { display: flex; align-items: center; gap: 8px; }
  .grow { flex: 1 1 auto; overflow-wrap: anywhere; }
  .dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
</style>
