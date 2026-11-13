<script lang="ts">
  import { tick, untrack } from 'svelte';
  import { SvelteMap } from 'svelte/reactivity';
  import { Button } from '../lib/ui/button';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';
  import {
    CHANGED_TEXT, NAME_RULE, URL_RULE, ceilingLines, clampText, createPayload, createServer, isConflict, listGate,
    listServers, nameProblem, prettySchema, probeServer, problemText, refreshLines, refreshServer, removeServer,
    serverStatus, shortName, toolChange, toolStatus, updateServer, urlProblem,
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
  /** A result in words. `expect` is the state it speaks of (a server's `enabled background`, a tool's permission):
   * when the list shows another, the note is dropped, so it never contradicts the status beside it. */
  type Note = { text: string; ok: boolean; expect?: string };

  // Per server, keyed by name in Maps: a server's or a tool's name is never read as a property of a plain object
  // (a server named "constructor", a tool named "__proto__"). A tool's key is `<server> <tool>`: a server name has no
  // space. Element ids of a server are `mcp-<letter>.<name>`: a name has no ".", so no server name can take the id of
  // the form's own elements (`mcp-name`, `mcp-url`, `mcp-add`).
  const busy = new SvelteMap<string, string>(); // the action in flight
  const notes = new SvelteMap<string, Note>(); // the last result, of a server or of one tool's control
  const confirming = new SvelteMap<string, 'background' | 'remove'>();
  const refreshed = new SvelteMap<string, RefreshResult>(); // the last refresh's changes
  const expanded = new SvelteMap<string, boolean>(); // descriptions shown whole
  let pageNote = $state<Note | null>(null);

  const toolKey = (server: string, tool: string) => `${server} ${tool}`;
  const serverState = (s: Pick<McpServer, 'enabled' | 'background'>) => `${s.enabled} ${s.background}`;
  const byId = (id: string) => document.getElementById(id);

  /** Drop a server's notes: its own and every tool's. */
  function clearNotes(server: string) {
    for (const key of [...notes.keys()]) if (key === server || key.startsWith(`${server} `)) notes.delete(key);
  }

  /** When the list reloads: drop the notes of a server or tool that is gone, or whose state is not the one the note
   * spoke of, and the refresh result of a server that is gone. */
  function prune(d: McpServerList | null) {
    if (!d) return;
    const servers = new Map(d.servers.map((s) => [s.name, s]));
    for (const [key, n] of [...notes]) {
      const at = key.indexOf(' ');
      const s = servers.get(at < 0 ? key : key.slice(0, at));
      const t = s && at >= 0 ? s.tools.find((x) => x.tool === key.slice(at + 1)) : undefined;
      const now = !s ? undefined : at < 0 ? serverState(s) : t?.permission;
      if (now === undefined || (n.expect !== undefined && n.expect !== now)) notes.delete(key);
    }
    for (const name of [...refreshed.keys()]) if (!servers.has(name)) refreshed.delete(name);
  }
  $effect(() => {
    const d = $store.data;
    untrack(() => prune(d));
  });

  /** Focus `target` when the focus was lost (its control was removed, or disabled while the change ran). */
  function restoreFocus(target: HTMLElement | null) {
    const active = document.activeElement;
    if (target?.isConnected && (!active || active === document.body || !active.isConnected)) target.focus();
  }

  /** Run one change: its server's controls are disabled until it and the reload that follows have landed; its note is
   * shown after the reload. The focus goes back to `focus()`, else to the control that started it. */
  async function act(
    server: string, action: string, key: string, run: () => Promise<Note | null>, focus?: () => HTMLElement | null,
  ) {
    const opener = document.activeElement as HTMLElement | null;
    busy.set(server, action);
    notes.delete(key);
    pageNote = null;
    let note: Note | null;
    try {
      note = await run();
    } catch (err) {
      note = { text: problemText(err), ok: false };
    }
    await store.refresh();
    busy.delete(server);
    if (note) notes.set(key, note);
    await tick();
    restoreFocus(focus?.() ?? opener);
  }

  const setEnabled = (s: McpServer) =>
    act(s.name, 'enabled', s.name, async () => {
      await updateServer(s.name, { enabled: !s.enabled });
      const text = s.enabled ? 'Switched off: its tools are no longer offered to the agent.' : 'Switched on.';
      return { text, ok: true, expect: serverState({ enabled: !s.enabled, background: s.background }) };
    });

  /** Open a confirmation and put the focus on its first button. */
  async function openConfirm(server: string, kind: 'background' | 'remove') {
    confirming.set(server, kind);
    await tick();
    byId(`mcp-c.${server}`)?.querySelector('button')?.focus();
  }

  /** Close a confirmation and give the focus back to the control that opened it. */
  async function cancelConfirm(server: string) {
    const kind = confirming.get(server);
    confirming.delete(server);
    await tick();
    byId(kind === 'remove' ? `mcp-r.${server}` : `mcp-k.${server}`)?.focus();
  }

  /** Turning background work on asks first; turning it off does not. The box shows what is stored until then. */
  function onBackground(s: McpServer, e: Event) {
    const box = e.currentTarget as HTMLInputElement;
    const want = box.checked;
    box.checked = s.background;
    if (want) void openConfirm(s.name, 'background');
    else void setBackground(s, false);
  }

  function setBackground(s: McpServer, on: boolean) {
    confirming.delete(s.name);
    return act(s.name, 'background', s.name, async () => {
      await updateServer(s.name, { background: on });
      const text = on ? 'Background work may now call its tools.' : 'Background work can no longer call its tools.';
      return { text, ok: true, expect: serverState({ enabled: s.enabled, background: on }) };
    }, () => byId(`mcp-k.${s.name}`));
  }

  /** The word on a tool's control: what pressing it does. */
  const allowWord = (t: McpTool) => (t.permission === 'allow' ? "Don't allow" : 'Allow');

  const setTool = (s: McpServer, t: McpTool, allow: boolean) =>
    act(s.name, `tool ${t.tool}`, toolKey(s.name, t.tool), async () => {
      await updateServer(s.name, toolChange(t.tool, allow));
      return { text: allow ? 'Allowed.' : 'Not allowed now.', ok: true, expect: allow ? 'allow' : 'deny' };
    });

  const refreshTools = (s: McpServer) => {
    refreshed.delete(s.name);
    clearNotes(s.name);
    return act(s.name, 'refresh', s.name, async () => {
      refreshed.set(s.name, await refreshServer(s.name));
      return { text: 'Read the tool list again.', ok: true };
    });
  };

  function remove(s: McpServer) {
    confirming.delete(s.name);
    return act(s.name, 'remove', s.name, async () => {
      await removeServer(s.name);
      clearNotes(s.name);
      refreshed.delete(s.name);
      pageNote = { text: `Removed ${s.name}.`, ok: true };
      return null;
    }, () => byId(`mcp-r.${s.name}`) ?? byId('mcp-top')); // the Remove button while the server is listed, else the title
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
  /** The ticked tools, by the server's own name (a Map: a name such as "constructor" is never ticked by a prototype). */
  const allowed = new SvelteMap<string, boolean>();
  let checking = $state(false);
  let adding = $state(false);
  let formNote = $state<Note | null>(null);
  let replaces = $derived(($store.data?.servers ?? []).some((s) => s.name === newName));

  async function check(e: SubmitEvent) {
    e.preventDefault();
    tried = true;
    if (nameProblem(newName) || urlProblem(newUrl.trim())) return;
    formNote = null;
    await runProbe(newName, newUrl.trim());
  }

  /** Probe a name and address. Every choice starts at not allowed. */
  async function runProbe(name: string, url: string) {
    checking = true;
    try {
      const result = await probeServer(name, url);
      probe = { name, url, result };
      allowed.clear();
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
      clearNotes(name);
      refreshed.delete(name);
      formNote = {
        text: answer.created ? `Added ${name}. It is listed above.` : `Replaced ${name}. It is switched on, and background work is off.`,
        ok: true,
      };
      newName = '';
      newUrl = '';
      tried = false;
      probe = null;
      allowed.clear();
    } catch (err) {
      if (isConflict(err)) {
        // The server answered differently from what was shown: the new list for the same name and address, every
        // choice back at not allowed.
        formNote = { text: CHANGED_TEXT, ok: false };
        adding = false;
        await runProbe(name, url);
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
    {@const open = expanded.get(key) === true}
    <p class="desc">{open || !c.cut ? text : c.text}</p>
    {#if c.cut}
      <button type="button" class="more" aria-expanded={open} onclick={() => expanded.set(key, !open)}>
        {open ? 'Show less' : 'Show all'}
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

{#snippet toolName(name: string)}
  <code class="tool-name" title={name.length > 80 ? name : undefined}>{shortName(name)}</code>
{/snippet}

{#snippet note(n: Note | undefined | null)}
  {#if n}
    <p class="note" class:note-err={!n.ok} role={n.ok ? 'status' : 'alert'}>{n.text}</p>
  {/if}
{/snippet}

<header class="view-head">
  <div>
    <h1 class="page-h1" id="mcp-top" tabindex="-1">Integrations</h1>
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
    {@const inFlight = busy.get(s.name)}
    {@const asking = confirming.get(s.name)}
    {@const changes = refreshed.get(s.name)}
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
          <input id="mcp-k.{s.name}" type="checkbox" checked={s.background} disabled={!!inFlight} aria-describedby="mcp-b.{s.name}"
            onchange={(e) => onBackground(s, e)} />
          Allow in background work
        </label>
        <p id="mcp-b.{s.name}" class="hint">Scheduled jobs, subtasks and other unattended work may call this server's tools.</p>
        {#if s.background_blocked_by_ceiling}
          <p class="limit">Background work is blocked by the server's host settings: it is not listed in NOUS_MCP_CLIENT_BACKGROUND_SERVERS.</p>
        {/if}
        {#if asking === 'background'}
          <div class="confirm" id="mcp-c.{s.name}" role="group" aria-label="Confirm background work for {s.name}">
            <p>Let background work call {s.name}'s tools? They would then run with no one watching.</p>
            <div class="actions">
              <Button size="sm" class="max-md:min-h-11 max-md:px-4" disabled={!!inFlight} onclick={() => void setBackground(s, true)}>Yes, allow in background work</Button>
              <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" onclick={() => void cancelConfirm(s.name)}>Cancel</Button>
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
          id="mcp-r.{s.name}" disabled={!!inFlight} onclick={() => void openConfirm(s.name, 'remove')}>
          {inFlight === 'remove' ? 'Removing…' : 'Remove'}
        </Button>
      </div>
      {#if asking === 'remove'}
        <div class="confirm" id="mcp-c.{s.name}" role="group" aria-label="Confirm removing {s.name}">
          <p>Remove {s.name} and its {s.tools.length} tool{s.tools.length === 1 ? '' : 's'}? The agent loses them at once; adding it again means choosing its tools again.</p>
          <div class="actions">
            <Button variant="destructive" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={!!inFlight} onclick={() => void remove(s)}>Yes, remove</Button>
            <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" onclick={() => void cancelConfirm(s.name)}>Cancel</Button>
          </div>
        </div>
      {/if}
      {@render note(notes.get(s.name))}
      {#if changes}
        <ul class="changes" aria-label="What the refresh changed">
          {#each refreshLines(changes) as line (line.lead)}
            <li>{line.lead}{#each line.tools as name, i (i)}{i ? ', ' : ' '}{@render toolName(name)}{/each}{line.tools.length ? '.' : ''}{line.tail ? ` ${line.tail}` : ''}</li>
          {/each}
        </ul>
      {/if}

      <h3>Tools ({s.tools.length})</h3>
      {#if s.tools.length === 0}
        <p class="hint">The server lists no tools.</p>
      {/if}
      <ul class="tools">
        {#each s.tools as t (t.tool)}
          {@const ts = toolStatus(t, s.enabled)}
          {@const key = toolKey(s.name, t.tool)}
          <li class="tool">
            <div class="tool-head">
              {@render toolName(t.tool)}
              <span class="status-pill {PILL[ts.tone]}">{ts.word}</span>
              {#if ts.detail}<span class="reason">{ts.detail}</span>{/if}
              {#if changes?.changed.includes(t.tool)}<span class="status-pill status-pill--warn">definition changed</span>{/if}
              <span class="tool-act">
                <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={!!inFlight}
                  aria-label={`${allowWord(t)} ${t.tool}`} onclick={() => void setTool(s, t, t.permission !== 'allow')}>
                  {inFlight === `tool ${t.tool}` ? 'Saving…' : allowWord(t)}
                </Button>
              </span>
            </div>
            {#if t.blocked_by_ceiling}<p class="limit">Blocked by the server's host settings.</p>{/if}
            {@render note(notes.get(key))}
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
          aria-invalid={nameErr ? 'true' : undefined} disabled={checking || adding} />
        <p id="mcp-name-hint" class="hint" class:hint-err={nameErr}>{nameErr ?? `${NAME_RULE} It becomes part of each tool's name.`}</p>
      </div>
      <div class="field">
        <label for="mcp-url">Address</label>
        <input id="mcp-url" type="url" inputmode="url" bind:value={newUrl} autocomplete="off" spellcheck="false"
          placeholder="https://" aria-describedby="mcp-url-hint" aria-invalid={urlErr ? 'true' : undefined} disabled={checking || adding} />
        <p id="mcp-url-hint" class="hint" class:hint-err={urlErr}>{urlErr ?? URL_RULE}</p>
      </div>
      {#if replaces}
        <p class="hint">A server named {newName} exists. Adding it again replaces the server: its tools are checked again, it is switched on, and background work is turned off. Its call timeout goes back to 30 s.</p>
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
                <input type="checkbox" checked={allowed.get(t.tool) === true} disabled={adding}
                  onchange={(e) => allowed.set(t.tool, e.currentTarget.checked)} />
                Allow {@render toolName(t.tool)}
              </label>
              {#if t.skip_reason}
                <span class="status-pill status-pill--warn">would be skipped</span><span class="reason">{t.skip_reason}</span>
              {/if}
            </div>
            {#if t.name}<p class="hint">Offered as <code>{t.name}</code></p>{/if}
            {@render description(`probe:${t.tool}`, t.description)}
            {@render schemaBlock(t.schema)}
          </li>
        {/each}
      </ul>
      <div class="actions">
        <Button size="sm" class="max-md:min-h-11 max-md:px-4" disabled={adding || checking} onclick={() => void add()}>
          {adding ? (replaces ? 'Replacing…' : 'Adding…') : replaces ? 'Replace server' : 'Add server'}
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
  .field input:disabled { opacity: 0.6; }
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
