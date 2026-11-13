<script lang="ts">
  import { Button } from '../lib/ui/button';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';
  import {
    CHANGED_TEXT, NAME_RULE, URL_RULE, ceilingLines, clampText, createPayload, createServer, isConflict, listGate,
    listServers, nameProblem, prettySchema, probeServer, problemText, refreshLines, refreshServer, removeServer,
    serverStatus, toolStatus, updateServer, urlProblem,
    type McpServer, type McpServerList, type McpTool, type ProbeResult, type RefreshResult, type Tone,
  } from '../lib/integrations';

  // F112 PR 2b: the owner's MCP servers (GET /integrations/mcp/servers) and every change to them. A request to these
  // routes is the approval: the server stores it and applies it at once. Every string a server wrote (tool names,
  // descriptions, schemas, reasons) is shown as a text node: interpolation only.
  const store = usePoll(makePollStore<McpServerList>((signal) => listServers(signal), 30_000));
  /** The page Refresh: everything this screen shows (finish review fix 8: one header order). */
  const refreshPage = () => store.refresh();
  // 403, 404 and 503 are a state of the screen; checked before the data, so an older list never hides them.
  let gate = $derived(listGate($store.error));

  const PILL: Record<Tone, string> = { ok: 'status-pill--ok', warn: 'status-pill--warn', neutral: '' };
  type Note = { text: string; ok: boolean };

  // Per server, by name: the action in flight, the last result, an open confirmation, the last refresh's changes.
  // Element ids of a server are `mcp-s.<name>` and `mcp-b.<name>`: a name has no ".", so no server name can take the
  // id of the form's own elements (`mcp-name`, `mcp-url`, `mcp-add`).
  let busy = $state<Record<string, string>>({});
  let notes = $state<Record<string, Note>>({}); // a server's name, or `${server} ${tool}` for one tool's control
  let confirming = $state<Record<string, 'background' | 'remove'>>({});
  let refreshed = $state<Record<string, RefreshResult>>({});
  let expanded = $state<Record<string, boolean>>({});
  let pageNote = $state<Note | null>(null);

  /** Run one change: its server's controls are disabled until it and the reload that follows have landed. */
  async function act(server: string, action: string, key: string, run: () => Promise<string | null>) {
    busy[server] = action;
    delete notes[key];
    pageNote = null;
    try {
      const text = await run();
      if (text !== null) notes[key] = { text, ok: true };
    } catch (err) {
      notes[key] = { text: problemText(err), ok: false };
    } finally {
      await store.refresh();
      delete busy[server];
    }
  }

  const setEnabled = (s: McpServer) =>
    act(s.name, 'enabled', s.name, async () => {
      await updateServer(s.name, { enabled: !s.enabled });
      return s.enabled ? 'Switched off: its tools are no longer offered to the agent.' : 'Switched on.';
    });

  const cancelConfirm = (server: string) => {
    delete confirming[server];
  };

  /** Turning background work on asks first; turning it off does not. The box shows what is stored until then. */
  function onBackground(s: McpServer, e: Event) {
    const box = e.currentTarget as HTMLInputElement;
    const want = box.checked;
    box.checked = s.background;
    if (want) confirming[s.name] = 'background';
    else void setBackground(s, false);
  }

  function setBackground(s: McpServer, on: boolean) {
    delete confirming[s.name];
    return act(s.name, 'background', s.name, async () => {
      await updateServer(s.name, { background: on });
      return on ? 'Background work may now call its tools.' : 'Background work can no longer call its tools.';
    });
  }

  /** The word on a tool's control: what pressing it does. */
  const allowWord = (t: McpTool) => (t.permission === 'allow' ? "Don't allow" : 'Allow');

  const setTool = (s: McpServer, t: McpTool, allow: boolean) =>
    act(s.name, `tool ${t.tool}`, `${s.name} ${t.tool}`, async () => {
      await updateServer(s.name, { tools: { [t.tool]: allow ? 'allow' : 'deny' } });
      return allow ? 'Allowed.' : 'Not allowed now.';
    });

  const refreshTools = (s: McpServer) => {
    delete refreshed[s.name];
    return act(s.name, 'refresh', s.name, async () => {
      refreshed[s.name] = await refreshServer(s.name);
      return 'Read the tool list again.';
    });
  };

  function remove(s: McpServer) {
    delete confirming[s.name];
    return act(s.name, 'remove', s.name, async () => {
      await removeServer(s.name);
      delete refreshed[s.name];
      pageNote = { text: `Removed ${s.name}.`, ok: true };
      return null;
    });
  }

  // ── Add a server ───────────────────────────────────────────────────────────
  let newName = $state('');
  let newUrl = $state('');
  let tried = $state(false);
  let nameErr = $derived(newName || tried ? nameProblem(newName) : null);
  let urlErr = $derived(newUrl.trim() || tried ? urlProblem(newUrl.trim()) : null);
  /** The probe the tools below come from, and the name and address it was made for. */
  let probe = $state<{ name: string; url: string; result: ProbeResult } | null>(null);
  let probeFresh = $derived(probe !== null && probe.name === newName && probe.url === newUrl.trim());
  let allowed = $state<Record<string, boolean>>({});
  let checking = $state(false);
  let adding = $state(false);
  let formNote = $state<Note | null>(null);
  let replaces = $derived(($store.data?.servers ?? []).some((s) => s.name === newName));

  async function check(e: SubmitEvent) {
    e.preventDefault();
    tried = true;
    if (nameProblem(newName) || urlProblem(newUrl.trim())) return;
    formNote = null;
    await runProbe();
  }

  /** Probe the name and address in the form. Every choice starts at not allowed. */
  async function runProbe() {
    checking = true;
    const name = newName;
    const url = newUrl.trim();
    try {
      const result = await probeServer(name, url);
      probe = { name, url, result };
      allowed = {};
    } catch (err) {
      probe = null;
      formNote = { text: problemText(err), ok: false };
    } finally {
      checking = false;
    }
  }

  async function add() {
    if (!probe || !probeFresh) return;
    const { name, url, result } = probe;
    adding = true;
    formNote = null;
    pageNote = null;
    try {
      const answer = await createServer(createPayload(name, url, result, allowed));
      formNote = { text: answer.created ? `Added ${name}. It is listed above.` : `Replaced ${name}.`, ok: true };
      newName = '';
      newUrl = '';
      tried = false;
      probe = null;
      allowed = {};
    } catch (err) {
      if (isConflict(err)) {
        // The server answered differently from what was shown: show the new list, every choice back at not allowed.
        formNote = { text: CHANGED_TEXT, ok: false };
        adding = false;
        await runProbe();
      } else {
        formNote = { text: problemText(err), ok: false };
      }
    } finally {
      adding = false;
      await store.refresh();
    }
  }
</script>

{#snippet description(key: string, text: string | null)}
  {#if text}
    {@const c = clampText(text)}
    <p class="desc">{expanded[key] || !c.cut ? text : c.text}</p>
    {#if c.cut}
      <button type="button" class="more" aria-expanded={!!expanded[key]} onclick={() => (expanded[key] = !expanded[key])}>
        {expanded[key] ? 'Show less' : 'Show all'}
      </button>
    {/if}
  {/if}
{/snippet}

{#snippet schemaBlock(schema: string | null)}
  {#if schema}
    <details class="schema">
      <summary>Schema</summary>
      <pre>{prettySchema(schema)}</pre>
    </details>
  {/if}
{/snippet}

{#snippet note(n: Note | undefined | null)}
  {#if n}
    <p class="note" class:note-err={!n.ok} role={n.ok ? 'status' : 'alert'}>{n.text}</p>
  {/if}
{/snippet}

<header class="view-head">
  <div>
    <h1 class="page-h1">Integrations</h1>
    <p class="subtitle">The external MCP servers the agent may use, and which of their tools it may call. Every change here applies at once.</p>
  </div>
  <div class="head-right">
    <StaleBadge state={$store} />
    <Button variant="outline" size="lg" class="px-4 max-md:min-h-11" onclick={() => void refreshPage()} disabled={$store.loading}>{$store.loading ? 'Loading…' : 'Refresh'}</Button>
  </div>
</header>

{#if gate === 'unavailable'}
  <section class="notice" role="status">
    <h2>MCP servers are not available here</h2>
    <p>One of two things keeps this screen empty:</p>
    <ul>
      <li>the MCP client is switched off (<code>NOUS_MCP_CLIENT_ENABLED</code>) or not running;</li>
      <li>the screen's secret is not configured on the server (<code>NOUS_MCP_CLIENT_UI_SECRET_SHA256</code>).</li>
    </ul>
  </section>
{:else if gate === 'forbidden'}
  <section class="notice" role="status">
    <h2>Open this screen through the protected address</h2>
    <p>This request did not come through the login proxy, which adds the secret these routes need.</p>
  </section>
{:else if $store.data}
  {@const d = $store.data}
  {@const limits = ceilingLines(d.ceilings)}
  {#if limits.length}
    <section class="limits" aria-labelledby="mcp-limits">
      <h2 id="mcp-limits">Limits set on the host are in effect</h2>
      <ul>{#each limits as line (line)}<li>{line}</li>{/each}</ul>
    </section>
  {:else}
    <p class="quiet">No limit is set on the host: what you allow here is what the agent may use.</p>
  {/if}

  {@render note(pageNote)}

  {#if d.servers.length === 0}
    <section class="card">
      <p class="empty">No MCP servers yet. Add the first one below: check its address, choose which of its tools the agent may use, then add it.</p>
    </section>
  {/if}

  {#each d.servers as s (s.name)}
    {@const st = serverStatus(s)}
    {@const inFlight = busy[s.name]}
    <section class="card server" aria-labelledby="mcp-s.{s.name}">
      <div class="server-head">
        <h2 id="mcp-s.{s.name}">{s.name}</h2>
        <span class="status-pill {PILL[st.tone]}">{st.word}</span>
      </div>
      <p class="url">{s.url}</p>
      {#if s.blocked_by_ceiling}
        <p class="limit">Blocked by the server's host settings: {s.skip_reason ?? 'it is outside the pin'}.</p>
      {/if}

      <div class="background">
        <label class="check">
          <input type="checkbox" checked={s.background} disabled={!!inFlight} aria-describedby="mcp-b.{s.name}"
            onchange={(e) => onBackground(s, e)} />
          Allow in background work
        </label>
        <p id="mcp-b.{s.name}" class="hint">Scheduled jobs, subtasks and other unattended work may call this server's tools.</p>
        {#if s.background_blocked_by_ceiling}
          <p class="limit">Background work is blocked by the server's host settings: it is not listed in NOUS_MCP_CLIENT_BACKGROUND_SERVERS.</p>
        {/if}
        {#if confirming[s.name] === 'background'}
          <div class="confirm" role="group" aria-label="Confirm background work for {s.name}">
            <p>Let background work call {s.name}'s tools? They would then run with no one watching.</p>
            <div class="actions">
              <Button size="sm" class="max-md:min-h-11 max-md:px-4" disabled={!!inFlight} onclick={() => void setBackground(s, true)}>Yes, allow in background work</Button>
              <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" onclick={() => cancelConfirm(s.name)}>Cancel</Button>
            </div>
          </div>
        {/if}
      </div>

      <div class="actions">
        <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={!!inFlight} onclick={() => void setEnabled(s)}>
          {inFlight === 'enabled' ? (s.enabled ? 'Switching off…' : 'Switching on…') : s.enabled ? 'Switch off' : 'Switch on'}
        </Button>
        <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={!!inFlight} onclick={() => void refreshTools(s)}>
          {inFlight === 'refresh' ? 'Refreshing…' : 'Refresh tools'}
        </Button>
        <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4 hover:border-[var(--err-text)] hover:text-[var(--err-text)]"
          disabled={!!inFlight} onclick={() => (confirming[s.name] = 'remove')}>
          {inFlight === 'remove' ? 'Removing…' : 'Remove'}
        </Button>
      </div>
      {#if confirming[s.name] === 'remove'}
        <div class="confirm" role="group" aria-label="Confirm removing {s.name}">
          <p>Remove {s.name} and its {s.tools.length} tool{s.tools.length === 1 ? '' : 's'}? The agent loses them at once; adding it again means choosing its tools again.</p>
          <div class="actions">
            <Button variant="destructive" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={!!inFlight} onclick={() => void remove(s)}>Yes, remove</Button>
            <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" onclick={() => cancelConfirm(s.name)}>Cancel</Button>
          </div>
        </div>
      {/if}
      {@render note(notes[s.name])}
      {#if refreshed[s.name]}
        <ul class="changes" aria-label="What the refresh changed">
          {#each refreshLines(refreshed[s.name]) as line (line)}<li>{line}</li>{/each}
        </ul>
      {/if}

      <h3>Tools ({s.tools.length})</h3>
      {#if s.tools.length === 0}
        <p class="hint">The server lists no tools.</p>
      {/if}
      <ul class="tools">
        {#each s.tools as t (t.tool)}
          {@const ts = toolStatus(t)}
          {@const key = `${s.name} ${t.tool}`}
          <li class="tool">
            <div class="tool-head">
              <code class="tool-name">{t.tool}</code>
              <span class="status-pill {PILL[ts.tone]}">{ts.word}</span>
              {#if ts.detail}<span class="reason">{ts.detail}</span>{/if}
              {#if refreshed[s.name]?.changed.includes(t.tool)}<span class="status-pill status-pill--warn">definition changed</span>{/if}
              <span class="tool-act">
                <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={!!inFlight}
                  aria-label={`${allowWord(t)} ${t.tool}`} onclick={() => void setTool(s, t, t.permission !== 'allow')}>
                  {inFlight === `tool ${t.tool}` ? 'Saving…' : allowWord(t)}
                </Button>
              </span>
            </div>
            {#if t.blocked_by_ceiling}<p class="limit">Blocked by the server's host settings.</p>{/if}
            {@render note(notes[key])}
            {#if t.name}<p class="hint">Offered as <code>{t.name}</code></p>{/if}
            {@render description(key, t.description)}
            {@render schemaBlock(t.schema)}
          </li>
        {/each}
      </ul>
    </section>
  {/each}

  <section class="card" aria-labelledby="mcp-add">
    <h2 id="mcp-add">Add a server</h2>
    <form onsubmit={check} novalidate>
      <div class="field">
        <label for="mcp-name">Name</label>
        <input id="mcp-name" bind:value={newName} autocomplete="off" spellcheck="false" aria-describedby="mcp-name-hint"
          aria-invalid={nameErr ? 'true' : undefined} />
        <p id="mcp-name-hint" class="hint" class:hint-err={nameErr}>{nameErr ?? `${NAME_RULE} It becomes part of each tool's name.`}</p>
      </div>
      <div class="field">
        <label for="mcp-url">Address</label>
        <input id="mcp-url" type="url" inputmode="url" bind:value={newUrl} autocomplete="off" spellcheck="false"
          placeholder="https://" aria-describedby="mcp-url-hint" aria-invalid={urlErr ? 'true' : undefined} />
        <p id="mcp-url-hint" class="hint" class:hint-err={urlErr}>{urlErr ?? URL_RULE}</p>
      </div>
      {#if replaces}
        <p class="hint">A server named {newName} exists: adding replaces its address and tools, switches it on and sets its call timeout to 30 s.</p>
      {/if}
      <div class="actions">
        <Button type="submit" variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={checking || adding}>
          {checking ? 'Checking…' : 'Check server'}
        </Button>
      </div>
    </form>

    {#if probe && !probeFresh}
      <p class="hint">The name or address changed since the check: check the server again.</p>
    {:else if probe}
      {@const tools = probe.result.tools}
      <h3>{tools.length} tool{tools.length === 1 ? '' : 's'} listed</h3>
      <p class="hint">Exactly what the agent would be offered. Nothing is allowed until you tick it.</p>
      <ul class="tools">
        {#each tools as t (t.tool)}
          <li class="tool">
            <div class="tool-head">
              <label class="check">
                <input type="checkbox" bind:checked={allowed[t.tool]} disabled={adding} />
                Allow <code class="tool-name">{t.tool}</code>
              </label>
              {#if t.skip_reason}
                <span class="status-pill status-pill--warn">would be skipped</span><span class="reason">{t.skip_reason}</span>
              {/if}
            </div>
            {#if t.name}<p class="hint">Offered as <code>{t.name}</code></p>{/if}
            {@render description(`probe ${t.tool}`, t.description)}
            {@render schemaBlock(t.schema)}
          </li>
        {/each}
      </ul>
      <div class="actions">
        <Button size="sm" class="max-md:min-h-11 max-md:px-4" disabled={adding || checking} onclick={() => void add()}>
          {adding ? 'Adding…' : 'Add server'}
        </Button>
      </div>
    {/if}
    {@render note(formNote)}
  </section>
{:else if $store.error}
  <div class="notice" role="alert">Could not load the MCP servers: {problemText($store.error)}</div>
{:else}
  <p class="empty">Loading…</p>
{/if}

<style>
  .view-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; flex-wrap: wrap; margin-bottom: 1.25rem; }
  .head-right { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
  .subtitle { font-size: 0.8125rem; color: var(--muted); margin: 0; }
  h2 { font-size: 0.9375rem; font-weight: 650; margin: 0; overflow-wrap: anywhere; }
  h3 { font-size: 0.8125rem; font-weight: 600; color: var(--muted); margin: 1rem 0 0.25rem; }
  code { font-family: var(--font-mono); font-size: 0.8125rem; overflow-wrap: anywhere; }
  .notice { padding: 0.875rem 1rem; border-radius: var(--card-radius-sm); background: var(--warn-bg);
    border: 1px solid var(--warn-border); font-size: 0.8125rem; margin-bottom: 1rem; }
  .notice h2 { margin-bottom: 0.375rem; }
  .notice ul, .limits ul { margin: 0.375rem 0 0; padding-left: 1.25rem; list-style: disc; }
  .notice code { font-size: 0.75rem; }
  .limits { padding: 0.75rem 1rem; border-radius: var(--card-radius-sm); background: var(--info-bg); font-size: 0.8125rem; margin-bottom: 1rem; }
  .quiet { font-size: 0.8125rem; color: var(--muted); margin: 0 0 1rem; }
  .card { background: var(--surface); border: var(--card-border); border-radius: var(--card-radius); box-shadow: var(--card-shadow);
    padding: 1.125rem 1.25rem; margin-bottom: 1.25rem; min-width: 0; }
  .card > h2 { margin-bottom: 0.75rem; }
  .empty { color: var(--muted); font-size: 0.875rem; text-align: center; padding: 1rem 0; margin: 0; }
  .server-head { display: flex; align-items: center; gap: 0.5rem 0.75rem; flex-wrap: wrap; }
  .url { font-family: var(--font-mono); font-size: 0.8125rem; color: var(--muted); margin: 0.25rem 0 0; overflow-wrap: anywhere; }
  .limit { font-size: 0.8125rem; color: var(--warn-text); margin: 0.375rem 0 0; }
  .hint { font-size: 0.75rem; color: var(--muted); margin: 0.25rem 0 0; }
  .hint-err { color: var(--err-text); }
  .background { margin-top: 0.875rem; }
  .check { display: inline-flex; align-items: center; gap: 0.5rem; font-size: 0.875rem; cursor: pointer; }
  .check input { width: 1rem; height: 1rem; accent-color: var(--action); cursor: pointer; }
  .check input:disabled { cursor: default; }
  .actions { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; margin-top: 0.75rem; }
  .confirm { margin-top: 0.75rem; padding: 0.75rem 0.875rem; border-radius: var(--card-radius-sm); background: var(--wash); font-size: 0.875rem; }
  .confirm p { margin: 0; }
  .confirm .actions { margin-top: 0.5rem; }
  .note { font-size: 0.8125rem; color: var(--save-ok); margin: 0.5rem 0 0; overflow-wrap: anywhere; }
  .note-err { color: var(--save-err); }
  .changes { margin: 0.5rem 0 0; padding-left: 1.25rem; font-size: 0.8125rem; list-style: disc; }
  .tools { list-style: none; margin: 0; padding: 0; }
  .tool { padding: 0.625rem 0; border-top: 1px solid var(--border); min-width: 0; }
  .tool:first-child { border-top: 0; }
  .tool-head { display: flex; align-items: center; gap: 0.375rem 0.625rem; flex-wrap: wrap; }
  .tool-name { font-weight: 500; }
  .reason { font-size: 0.75rem; color: var(--muted); overflow-wrap: anywhere; min-width: 0; }
  .tool-act { margin-left: auto; }
  .desc { font-size: 0.8125rem; margin: 0.375rem 0 0; max-width: 72ch; overflow-wrap: anywhere; white-space: pre-line; }
  .more { background: none; border: 0; padding: 0.125rem 0; color: var(--accent-text); font: inherit; font-size: 0.75rem;
    cursor: pointer; border-radius: var(--radius-sm); }
  .more:hover { text-decoration: underline; }
  .schema { margin-top: 0.375rem; font-size: 0.75rem; }
  .schema summary { cursor: pointer; color: var(--muted); padding: 0.125rem 0; border-radius: var(--radius-sm); }
  .schema pre { margin: 0.375rem 0 0; padding: 0.375rem 0.625rem; background: var(--wash); border-radius: var(--radius-sm);
    font-family: var(--font-mono); font-size: 0.78125rem; line-height: 1.5; overflow-x: auto; max-height: 20rem; }
  form { display: flex; flex-direction: column; gap: 0.75rem; }
  .field { display: flex; flex-direction: column; gap: 0.25rem; max-width: 36rem; }
  .field label { font-size: 0.75rem; font-weight: 600; color: var(--muted); }
  .field input { height: 2.75rem; padding: 0 0.625rem; background: var(--surface); border: 1px solid var(--field-border);
    border-radius: var(--radius-sm); color: var(--text); font: inherit; font-size: 0.875rem; min-width: 0; }
  .field input:focus-visible, .check input:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
  .field .hint { margin: 0; }
  form .actions { margin-top: 0; }
  @media (max-width: 768px) {
    /* 44px targets on a phone: the checkbox rows, the show-all control and the schema's disclosure (a summary stays a
       list item, so the browser keeps drawing its marker). */
    .check, .more { min-height: 2.75rem; }
    .more { display: inline-flex; align-items: center; }
    .schema summary { line-height: 2.75rem; padding: 0; }
    .tool-act { margin-left: 0; }
  }
</style>
