<script lang="ts">
  import { onDestroy } from 'svelte';
  import { Button } from '../../lib/ui/button';
  import {
    completeSignIn, problemText, signInHosts, signInHref, signInState, signInText, startSignIn,
    type OAuthAdvanced, type OAuthFlowState, type OAuthStart,
  } from '../../lib/integrations';

  // F112 PR 2d: one OAuth sign-in for a server name and address, in the add form or on a server's card. The owner
  // presses Sign in (with their own OAuth app under "Advanced" if the provider needs one), reads where they will sign
  // in and where the tokens come from, opens the provider's page in a new tab with the link, and this panel asks how
  // the sign-in stands every 2 s until it is no longer pending. The paste field takes the address the provider sent
  // the browser back to, for when that page could not reach Nous. A client secret typed here lives only in this
  // panel's state and is cleared once the sign-in starts. Every string from the server is shown as text.
  let {
    name, url, idPrefix, disabled = false, label = 'Sign in', onDone,
  }: {
    name: string;
    url: string;
    /** Prefix of this panel's element ids (the add form's and each card's differ). */
    idPrefix: string;
    disabled?: boolean;
    label?: string;
    /** The sign-in finished: its id, whether its tokens were already stored with an existing server, and the name and
     * address it was made for (bound to them: the props may have changed since). */
    onDone: (flowId: string, state: OAuthFlowState, name: string, url: string) => void;
  } = $props();

  const POLL_MS = 2000;
  let advanced = $state<OAuthAdvanced>({ client_id: '', client_secret: '', scope: '' });
  let started = $state<OAuthStart | null>(null);
  /** The name and address the current sign-in was started for. */
  let startedFor = { name: '', url: '' };
  let flowState = $state<OAuthFlowState | null>(null);
  let starting = $state(false);
  let error = $state<string | null>(null);
  let pasted = $state('');
  let pasting = $state(false);
  let timer: ReturnType<typeof setTimeout> | null = null;
  let asking: AbortController | null = null;

  function stopPolling() {
    if (timer) clearTimeout(timer);
    timer = null;
    asking?.abort();
    asking = null;
  }
  onDestroy(stopPolling);

  const pending = $derived(started !== null && flowState?.status === 'pending');

  /** A new state of the sign-in `flowId` (ignored once another sign-in started). */
  function settle(flowId: string, state: OAuthFlowState) {
    if (started?.flow_id !== flowId) return;
    flowState = state;
    if (state.status === 'pending') return;
    stopPolling();
    if (state.status === 'complete') onDone(flowId, state, startedFor.name, startedFor.url);
  }

  function poll(flowId: string) {
    timer = setTimeout(async () => {
      asking = new AbortController();
      try {
        settle(flowId, await signInState(flowId, asking.signal));
      } catch (err) {
        // Unknown to Nous (a restart, or long expired): over. Any other miss: ask again at the next tick.
        if ((err as { status?: number } | null)?.status === 404) {
          settle(flowId, { status: 'expired', reason: null, stored: false });
        }
      }
      if (started?.flow_id === flowId && flowState?.status === 'pending') poll(flowId);
    }, POLL_MS);
  }

  async function start() {
    stopPolling();
    starting = true;
    error = null;
    started = null;
    flowState = null;
    pasted = '';
    try {
      const target = { name, url };
      const answer = await startSignIn(target.name, target.url, advanced);
      startedFor = target;
      started = answer;
      flowState = { status: 'pending', reason: null, stored: false };
      advanced.client_secret = '';
      poll(answer.flow_id);
    } catch (err) {
      error = problemText(err);
    } finally {
      starting = false;
    }
  }

  async function usePasted() {
    if (!started || !pasted.trim()) return;
    const flowId = started.flow_id;
    pasting = true;
    error = null;
    try {
      settle(flowId, await completeSignIn(flowId, pasted.trim()));
      pasted = '';
    } catch (err) {
      error = problemText(err);
    } finally {
      pasting = false;
    }
  }
</script>

<div class="oauth">
  {#if !pending && flowState?.status !== 'complete'}
    <details class="advanced">
      <summary>Advanced: your own OAuth app</summary>
      <p class="hint">Leave these empty and Nous registers itself with the provider. Fill them in when the provider asks you to register an app.</p>
      <div class="field">
        <label for="{idPrefix}-client-id">Client id</label>
        <input id="{idPrefix}-client-id" bind:value={advanced.client_id} autocomplete="off" spellcheck="false" disabled={disabled || starting} />
      </div>
      <div class="field">
        <label for="{idPrefix}-client-secret">Client secret (if the app has one)</label>
        <input id="{idPrefix}-client-secret" type="password" bind:value={advanced.client_secret} autocomplete="off"
          spellcheck="false" disabled={disabled || starting} />
      </div>
      <div class="field">
        <label for="{idPrefix}-scope">Scope (words separated by spaces)</label>
        <input id="{idPrefix}-scope" bind:value={advanced.scope} autocomplete="off" spellcheck="false" disabled={disabled || starting} />
      </div>
    </details>
    <div class="actions">
      <Button size="sm" class="max-md:min-h-11 max-md:px-4" disabled={disabled || starting} onclick={() => void start()}>
        {starting ? 'Starting…' : label}
      </Button>
    </div>
  {/if}

  {#if started && pending}
    {@const hosts = signInHosts(started)}
    {@const href = signInHref(started.authorization_url)}
    <p class="line">{hosts.line}</p>
    {#if hosts.warning}<p class="warn" role="alert">{hosts.warning}</p>{/if}
    <div class="actions">
      {#if href}
        <Button {href} target="_blank" rel="noopener noreferrer" size="sm" class="max-md:min-h-11 max-md:px-4">Continue to sign in</Button>
      {:else}
        <p class="warn">The provider's sign-in address is not an https address, so Nous does not open it.</p>
      {/if}
      <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" onclick={() => void start()}>Start again</Button>
    </div>
    <p class="hint" role="status">Waiting for you to finish in the other tab. This page notices on its own.</p>
    <div class="field paste">
      <label for="{idPrefix}-paste">Paste the address instead</label>
      <input id="{idPrefix}-paste" bind:value={pasted} autocomplete="off" spellcheck="false" disabled={pasting}
        aria-describedby="{idPrefix}-paste-hint" />
      <p id="{idPrefix}-paste-hint" class="hint">If the provider's last page could not reach Nous, copy the whole address from that tab's address bar and paste it here.</p>
    </div>
    <div class="actions">
      <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={pasting || !pasted.trim()} onclick={() => void usePasted()}>
        {pasting ? 'Checking…' : 'Use this address'}
      </Button>
    </div>
  {/if}

  {#if flowState && flowState.status !== 'pending'}
    <p class="note" class:note-err={flowState.status !== 'complete'} role={flowState.status === 'complete' ? 'status' : 'alert'}>
      {signInText(flowState)}
    </p>
  {/if}
  {#if error}<p class="note note-err" role="alert">{error}</p>{/if}
</div>

<style>
  .oauth { display: flex; flex-direction: column; gap: 0.5rem; margin-top: 0.5rem; }
  .advanced { font-size: 0.8125rem; }
  .advanced summary { cursor: pointer; color: var(--muted); padding: 0.125rem 0; border-radius: var(--radius-sm); }
  .advanced .field { margin-top: 0.5rem; }
  .field { display: flex; flex-direction: column; gap: 0.25rem; max-width: 36rem; }
  .field label { font-size: 0.75rem; font-weight: 600; color: var(--muted); }
  .field input { height: 2.75rem; padding: 0 0.625rem; background: var(--surface); border: 1px solid var(--field-border);
    border-radius: var(--radius-sm); color: var(--text); font: inherit; font-size: 0.875rem; min-width: 0; }
  .field input:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
  .field input:disabled { opacity: 0.6; }
  .line { font-size: 0.875rem; margin: 0; overflow-wrap: anywhere; }
  .warn { font-size: 0.8125rem; color: var(--warn-text); margin: 0; overflow-wrap: anywhere; }
  .hint { font-size: 0.75rem; color: var(--muted); margin: 0; }
  .actions { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
  .note { font-size: 0.8125rem; color: var(--save-ok); margin: 0; overflow-wrap: anywhere; }
  .note-err { color: var(--save-err); }
  @media (max-width: 768px) {
    .advanced summary { line-height: 2.75rem; padding: 0; }
  }
</style>
