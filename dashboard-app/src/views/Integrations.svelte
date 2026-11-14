<script lang="ts">
  import { tick, untrack } from 'svelte';
  import { SvelteMap, SvelteSet } from 'svelte/reactivity';
  import { Button } from '../lib/ui/button';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';
  import FilterBar from '../lib/ui/FilterBar.svelte';
  import { currentRoute, currentSub, routeHref } from '../lib/router';
  import OAuthSignIn from './integrations/OAuthSignIn.svelte';
  import {
    CHANGED_TEXT, HEADERS_MAX, HTTP_NOTE, KEY_HINT, NAME_RULE, NO_KEY_FILE_TEXT, OAUTH_UNAVAILABLE_TEXT, SIGN_OUT_NOTE,
    TOOL_FILTER_FROM, TOOL_SHOW, URL_RULE, VALUE_HINT,
    blankHeaderRow, ceilingLines, createPayload, createServer, filterTools, headersBody, headersProblem, isConflict, isHttp,
    isNoKey, keyLine, listGate, listServers, nameProblem, prettySchema, probeServer, problemText, refreshLines, refreshServer,
    removeServer, serverStatus, shortName, signOut, toolChange, toolCountText, toolStatus, updateServer, urlProblem,
    type HeaderRow, type McpServer, type McpServerList, type McpTool, type OAuthFlowState, type ProbeResult, type RefreshResult,
    type Tone,
  } from '../lib/integrations';

  // F112 PR 2b: the owner's MCP servers (GET /integrations/mcp/servers) and every change to them. A request to these
  // routes is the approval: the server stores it and applies it at once. Every string a server wrote (tool names,
  // descriptions, schemas, reasons) is shown as a text node: interpolation only.
  //
  // The screen is a list of the servers beside one pane: a server (`#/integrations/s/<name>`) or the form that adds one
  // (`#/integrations/add`; a name holds no "/", so neither address can be taken for the other). With no address the
  // pane shows the first server, or the form when there is none. A narrow screen has one column: the list until
  // something is picked, then the pane alone with a link back to the list.
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
  const confirming = new SvelteMap<string, 'background' | 'remove' | 'key' | 'signout'>();
  const refreshed = new SvelteMap<string, RefreshResult>(); // the last refresh's changes
  let pageNote = $state<Note | null>(null);

  const toolKey = (server: string, tool: string) => `${server} ${tool}`;

  // ── Which pane is open ─────────────────────────────────────────────────────
  /** The address after the screen's own; '' while another screen's address shows (the screen rendered on its own). */
  let sub = $derived($currentRoute === 'integrations' ? $currentSub : '');
  /** Whether the address names the form or a server. Any other address picks nothing, like the bare one. */
  let picked = $derived(sub === 'add' || (sub.startsWith('s/') && sub.length > 2));
  /** One column (the list, or the pane): the width the stylesheet's own breakpoint uses. */
  let narrow = $state(false);
  $effect(() => {
    if (typeof matchMedia !== 'function') return;
    const media = matchMedia('(max-width: 900px)');
    const read = () => {
      narrow = media.matches;
    };
    read();
    media.addEventListener('change', read);
    return () => media.removeEventListener('change', read);
  });
  const LIST_HREF = routeHref('integrations');
  const ADD_HREF = routeHref('integrations', 'add');
  const serverHref = (name: string) => routeHref('integrations', ['s', name]);
  type Pane = { kind: 'list' } | { kind: 'add' } | { kind: 'server'; server: McpServer } | { kind: 'missing'; name: string };
  /** The server shown last. The bare address shows that one while it exists: a reload that puts another server
   * first (the list is in name order) must not swap the pane under the owner's pointer, and coming back to the
   * screen's own address returns to the server that was open. */
  let stay = $state<string | null>(null);
  let pane = $derived.by((): Pane => {
    const servers = $store.data?.servers ?? [];
    if (picked && sub !== 'add') {
      let name = sub.slice(2);
      try {
        name = decodeURIComponent(name);
      } catch {
        // an address that does not decode names no server: it is shown as it was typed
      }
      const server = servers.find((s) => s.name === name);
      return server ? { kind: 'server', server } : { kind: 'missing', name };
    }
    if (sub === 'add') return { kind: 'add' };
    if (narrow) return { kind: 'list' }; // nothing picked: the list is the screen
    if (!servers.length) return { kind: 'add' };
    return { kind: 'server', server: servers.find((s) => s.name === stay) ?? servers[0] };
  });
  $effect(() => {
    if (pane.kind === 'server') stay = pane.server.name; // the last server shown, whichever address showed it
  });
  /** The server the pane shows, by name. */
  let shown = $derived(pane.kind === 'server' ? pane.server.name : null);

  // ── A server's tools: one line each ────────────────────────────────────────
  /** The rows opened to show a tool's description and schema, by `toolKey` (`probe:<tool>` in the add form). */
  const opened = new SvelteSet<string>();
  function toggleOpen(key: string) {
    if (opened.has(key)) opened.delete(key);
    else opened.add(key);
  }
  /** What is typed in "Find a tool", and which tools are shown: both start over with each server. */
  let query = $state('');
  let show = $state('all');
  /** Tools whose permission was changed while the list was filtered, by `toolKey`: they stay listed until the filter
   * or the query changes, so the row, its note and the focus stay where the owner pressed (allowing a tool under
   * "Not allowed" would otherwise take the row away with its note). */
  const kept = new SvelteSet<string>();
  $effect(() => {
    void show;
    void query;
    untrack(() => kept.clear());
  });

  // 8. One column: the list and the pane replace each other, so a click would leave the focus on nothing. It goes
  // to what came into view: the pane, or the list.
  let lastSub: string | null = null;
  $effect(() => {
    const now = sub;
    const moved = lastSub !== null && lastSub !== now;
    lastSub = now;
    if (!moved || !untrack(() => narrow)) return;
    void tick().then(() => (byId('mcp-pane') ?? byId('mcp-list'))?.focus());
  });
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
    for (const name of [...confirming.keys()]) if (!servers.has(name)) confirming.delete(name);
    if (keyEdit && !servers.has(keyEdit.server)) closeKeyEdit(); // its typed values go with it
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
    // Its own note is on a page that is not open: a change that failed while another server (or the form) is shown
    // is said above the pane as well.
    if (note && !note.ok && shown !== server) pageNote = { text: `${server}: ${note.text}`, ok: false };
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
  async function openConfirm(server: string, kind: 'background' | 'remove' | 'key' | 'signout') {
    confirming.set(server, kind);
    await tick();
    byId(`mcp-c.${server}`)?.querySelector('button')?.focus();
  }

  /** Close a confirmation and give the focus back to the control that opened it. */
  async function cancelConfirm(server: string) {
    const kind = confirming.get(server);
    confirming.delete(server);
    await tick();
    const ids = { remove: 'mcp-r', key: 'mcp-z', signout: 'mcp-o', background: 'mcp-k' } as const;
    byId(`${ids[kind ?? 'background']}.${server}`)?.focus();
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

  const setTool = (s: McpServer, t: McpTool, allow: boolean) => {
    kept.add(toolKey(s.name, t.tool));
    return act(s.name, `tool ${t.tool}`, toolKey(s.name, t.tool), async () => {
      await updateServer(s.name, toolChange(t.tool, allow));
      return { text: allow ? 'Allowed.' : 'Not allowed now.', ok: true, expect: allow ? 'allow' : 'deny' };
    });
  };

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
      // Its own address names no server now. Only while it is the one shown: the owner may have gone elsewhere.
      if ($currentRoute === 'integrations' && shown === s.name) location.replace(LIST_HREF);
      return null;
    }, () => byId(`mcp-r.${s.name}`) ?? byId('mcp-top')); // the Remove button while the server is listed, else the title
  }

  // ── Stored keys (F112 PR 2c) ───────────────────────────────────────────────
  // Typed values live only here, in memory: never in localStorage, cleared on success, on Cancel and on a 409. The
  // server never sends a value back, so Show reveals only what was just typed.
  let secretsAvailable = $derived($store.data?.secrets_available === true);
  /** Values shown as text instead of dots, by input id. */
  const revealed = new SvelteSet<string>();
  /** The key form open on one server's card (one at a time). */
  let keyEdit = $state<{ server: string; rows: HeaderRow[] } | null>(null);
  let keyError = $state<string | null>(null);

  function toggleShown(id: string) {
    if (revealed.has(id)) revealed.delete(id);
    else revealed.add(id);
  }

  /** Back to dots for every value field whose id starts with `prefix` (a form's rows are opened, closed or saved). */
  function hideValues(prefix: string) {
    for (const id of [...revealed]) if (id.startsWith(prefix)) revealed.delete(id);
  }

  /** Drop row `i`. The shown flags are by place, so every row of the form goes back to dots: none moves onto the
   * next row's value. */
  function dropRow(rows: HeaderRow[], i: number, valueId: (i: number) => string) {
    for (let k = 0; k < rows.length; k += 1) revealed.delete(valueId(k));
    rows.splice(i, 1);
  }

  /** Close the card's key form: its typed values and their shown flags go with it. */
  function closeKeyEdit() {
    keyEdit = null;
    keyError = null;
    hideValues('mcp-v.');
  }

  // Another server (or the form) is shown: what was open on the one left is closed. A typed key is never kept out of
  // sight, a confirmation is not left armed behind the pane, and the opened rows and the tool filter start over.
  $effect(() => {
    void shown;
    untrack(() => {
      closeKeyEdit();
      signingIn = null;
      confirming.clear();
      opened.clear();
      kept.clear();
      query = '';
      show = 'all';
    });
  });

  async function openKeyEdit(server: string) {
    closeKeyEdit();
    keyEdit = { server, rows: [blankHeaderRow('Authorization')] };
    await tick();
    byId(`mcp-h.${server}.0`)?.focus();
  }

  async function cancelKeyEdit(server: string) {
    closeKeyEdit();
    await tick();
    byId(`mcp-y.${server}`)?.focus();
  }

  function saveKey(s: McpServer) {
    if (keyEdit?.server !== s.name) return;
    keyError = headersProblem(keyEdit.rows);
    if (keyError) return;
    const headers = headersBody(keyEdit.rows);
    return act(s.name, 'key', s.name, async () => {
      try {
        await updateServer(s.name, { headers });
      } catch (err) {
        if (isNoKey(err)) closeKeyEdit();
        throw err;
      }
      closeKeyEdit();
      return { text: 'Key saved: the next call to this server sends it.', ok: true };
    }, () => byId(`mcp-y.${s.name}`));
  }

  function removeKey(s: McpServer) {
    confirming.delete(s.name);
    return act(s.name, 'key', s.name, async () => {
      await updateServer(s.name, { headers: null });
      return { text: 'Key removed: calls to this server go without it.', ok: true };
    }, () => byId(`mcp-y.${s.name}`));
  }

  // ── OAuth sign-in on a card (F112 PR 2d) ───────────────────────────────────
  let oauthAvailable = $derived($store.data?.oauth_available === true);
  /** The server whose card shows the sign-in panel (one at a time). */
  let signingIn = $state<string | null>(null);

  async function toggleSignIn(server: string) {
    signingIn = signingIn === server ? null : server;
    await tick();
    if (signingIn === server) byId(`mcp-sign.${server}`)?.querySelector<HTMLElement>('summary, button')?.focus();
  }

  /** A card's sign-in finished: its tokens were stored with the server at once (a stored server's sign-in is). */
  function signedInAgain(s: McpServer, state: OAuthFlowState) {
    signingIn = null;
    return act(s.name, 'signin', s.name, async () =>
      state.stored
        ? { text: 'Signed in: the next call to this server uses the new sign-in.', ok: true }
        : { text: 'The sign-in finished, but the server was gone or had moved, so nothing was stored.', ok: false },
    () => byId(`mcp-i.${s.name}`));
  }

  function signOutOf(s: McpServer) {
    confirming.delete(s.name);
    return act(s.name, 'signout', s.name, async () => {
      await signOut(s.name);
      return { text: 'Signed out: calls to this server now go without the sign-in.', ok: true };
    }, () => byId(`mcp-i.${s.name}`));
  }

  // ── Add a server ───────────────────────────────────────────────────────────
  let newName = $state('');
  let newUrl = $state('');
  let tried = $state(false);
  let nameErr = $derived(newName || tried ? nameProblem(newName) : null);
  let urlErr = $derived(newUrl.trim() || tried ? urlProblem(newUrl.trim()) : null);
  /** "API key or header": the rows typed for the new server ("None": no credential; "Sign in (OAuth)": a sign-in). */
  let authKind = $state<'none' | 'headers' | 'oauth'>('none');
  /** The finished sign-in for the new server: bound to the name and address it was made for. */
  let signedIn = $state<{ flowId: string; name: string; url: string } | null>(null);
  /** Bumped to start the form's sign-in panel afresh (its state goes with it). */
  let oauthPanel = $state(0);
  // A sign-in is bound to its name and address: when either changes, it no longer applies.
  $effect(() => {
    const name = newName;
    const url = newUrl.trim();
    untrack(() => {
      if (signedIn && (signedIn.name !== name || signedIn.url !== url)) {
        signedIn = null;
        oauthPanel += 1;
      }
    });
  });
  let newHeaders = $state<HeaderRow[]>([blankHeaderRow('Authorization')]);
  let headersErr = $state<string | null>(null);
  /** How many times the key (the choice or a row) changed. A check records the count, never the values, so a key
   * edited after the check needs a new one. */
  let headersEdits = $state(0);
  const headersEdited = () => {
    headersEdits += 1;
  };
  /** The probe the tools below come from: the name and address it was made for, and the key's edit count then. */
  let probe = $state<{ name: string; url: string; edits: number; result: ProbeResult } | null>(null);
  let probeFresh = $derived(
    probe !== null && probe.name === newName && probe.url === newUrl.trim() && probe.edits === headersEdits,
  );
  /** The ticked tools, by the server's own name (a Map: a name such as "constructor" is never ticked by a prototype). */
  const allowed = new SvelteMap<string, boolean>();
  let checking = $state(false);
  let adding = $state(false);
  let formNote = $state<Note | null>(null);
  let replaces = $derived(($store.data?.servers ?? []).some((s) => s.name === newName));

  /** Drop every typed value and any sign-in: "None" again, one empty row, nothing shown. */
  function clearHeaders() {
    authKind = 'none';
    newHeaders = [blankHeaderRow('Authorization')];
    headersErr = null;
    hideValues('mcp-new-');
    signedIn = null;
    oauthPanel += 1;
    headersEdited();
  }

  // The form is left (a server, or the list, is shown instead): what was typed for a key, a finished sign-in and
  // the check made with them go with it, so no typed value waits out of sight and none comes back shown. The name
  // and the address stay: neither is a secret.
  let onForm = $derived(pane.kind === 'add');
  $effect(() => {
    if (onForm) return;
    untrack(() => {
      clearHeaders();
      probe = null;
      allowed.clear();
      formNote = null;
    });
  });

  /** Choosing "None" is the form's Cancel for a key: what was typed is dropped. Leaving "Sign in" drops the sign-in. */
  function chooseAuth(kind: 'none' | 'headers' | 'oauth') {
    if (kind === 'none') clearHeaders();
    else {
      if (authKind === 'oauth') {
        signedIn = null;
        oauthPanel += 1;
      }
      authKind = kind;
      headersEdited();
    }
  }

  /** The form's sign-in finished. For a server that already exists its tokens are stored with it at once: the card
   * shows it, and the form starts over. */
  function formSignedIn(name: string, url: string, flowId: string, state: OAuthFlowState) {
    if (state.stored) {
      clearHeaders();
      formNote = { text: `Signed in: the sign-in is stored with ${name}, which already exists. Its page shows it.`, ok: true };
      void store.refresh();
      return;
    }
    if (name !== newName || url !== newUrl.trim()) {
      oauthPanel += 1;
      formNote = { text: 'The name or address changed during the sign-in: sign in again for the new ones.', ok: false };
      return;
    }
    signedIn = { flowId, name, url };
    headersEdited(); // a check made before this sign-in is not one made with it
  }

  /** The headers to send, or undefined for "None"; null when they break a rule (said beside the rows). */
  function typedHeaders(): Record<string, string> | undefined | null {
    if (authKind !== 'headers') return undefined;
    headersErr = headersProblem(newHeaders);
    return headersErr ? null : headersBody(newHeaders);
  }

  async function check(e: SubmitEvent) {
    e.preventDefault();
    tried = true;
    const headers = typedHeaders();
    if (nameProblem(newName) || urlProblem(newUrl.trim()) || headers === null) return;
    if (authKind === 'oauth' && !signedIn) {
      formNote = { text: 'Sign in first: the server is checked with the sign-in.', ok: false };
      return;
    }
    formNote = null;
    await runProbe(newName, newUrl.trim(), headers, authKind === 'oauth' ? signedIn?.flowId : undefined);
  }

  /** Probe a name and address, with the typed headers or the finished sign-in. Every choice starts at not allowed. */
  async function runProbe(name: string, url: string, headers: Record<string, string> | undefined, flowId?: string) {
    checking = true;
    const edits = headersEdits; // the key as it is sent (the fields are disabled while the check runs)
    try {
      const result = await probeServer(name, url, headers, flowId);
      probe = { name, url, edits, result };
      allowed.clear();
      opened.clear(); // a row opened on the last check's list is not one of this list's
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
    const headers = authKind === 'headers' ? headersBody(newHeaders) : undefined;
    const flowId = authKind === 'oauth' ? signedIn?.flowId : undefined;
    adding = true;
    formNote = null;
    pageNote = null;
    let made: string | null = null;
    try {
      const answer = await createServer(createPayload(name, url, result, allowed, headers, flowId));
      clearNotes(name);
      refreshed.delete(name);
      // A confirmation opened before the replace asked about the old server: it must not grant the new one.
      confirming.delete(name);
      // The form gives way to the server it made: the note goes with it, above the pane.
      pageNote = {
        text: answer.created ? `Added ${name}.` : `Replaced ${name}. It is switched on, and background work is off.`,
        ok: true,
      };
      made = name;
      newName = '';
      newUrl = '';
      tried = false;
      probe = null;
      allowed.clear();
      clearHeaders();
    } catch (err) {
      if (!onForm) {
        // The form was left while the add was in flight: its key and its check are gone, so there is nothing on it
        // to say this beside. It is said above the pane.
        const why = isConflict(err) ? 'its tools changed since the check' : problemText(err);
        pageNote = { text: `${name} was not added: ${why}`, ok: false };
      } else if (isConflict(err)) {
        // The server answered differently from what was shown: the new list for the same name, address and headers,
        // every choice back at not allowed. The typed headers stay: the check needs them.
        formNote = { text: CHANGED_TEXT, ok: false };
        adding = false;
        await runProbe(name, url, headers, flowId);
      } else {
        if (isNoKey(err)) {
          clearHeaders(); // nothing typed can be stored here; a sign-in that expired or was used is over too
          probe = null;
        }
        formNote = { text: problemText(err), ok: false };
      }
    } finally {
      adding = false;
      await store.refresh();
      // The form gives way to the server it made: once the list holds it (a reload that failed leaves the form, with
      // the note), and only while the form is still what is shown (the owner may have gone elsewhere meanwhile).
      const listed = ($store.data?.servers ?? []).some((s) => s.name === made);
      if (made && listed && pane.kind === 'add') location.hash = serverHref(made);
    }
  }
</script>

{#snippet schemaBlock(schema: string | null)}
  {#if schema}
    <details class="schema">
      <summary>Schema</summary>
      <pre>{prettySchema(schema)}</pre>
    </details>
  {/if}
{/snippet}

<!-- A row's disclosure mark: a drawn chevron that turns when the row is open. -->
{#snippet chevron()}
  <svg class="chev" viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
    <path d="M7 5l6 5-6 5" />
  </svg>
{/snippet}

<!-- What an opened row shows: the name the agent is offered, the description and the schema, as the server gave them. -->
{#snippet toolDetail(name: string | null, text: string | null, schema: string | null)}
  <div class="tool-detail">
    {#if name}<p class="hint">Offered as <code>{name}</code></p>{/if}
    {#if text}<p class="desc">{text}</p>{/if}
    {@render schemaBlock(schema)}
    {#if !name && !text && !schema}<p class="hint">The server gave no description and no schema for it.</p>{/if}
  </div>
{/snippet}

{#snippet toolName(name: string)}
  <code class="tool-name" title={name.length > 80 ? name : undefined}>{shortName(name)}</code>
{/snippet}

{#snippet note(n: Note | undefined | null)}
  {#if n}
    <p class="note" class:note-err={!n.ok} role={n.ok ? 'status' : 'alert'}>{n.text}</p>
  {/if}
{/snippet}

<!-- A key's rows: a header name, its value (dots until Show), and the controls to add or drop a row. `nameId` and
     `valueId` give each input's id: `mcp-new-h-<i>` in the add form, `mcp-h.<server>.<i>` on a card (a server name
     has no "." and no "-new-", so neither can take the other's id). -->
{#snippet headerRows(rows: HeaderRow[], nameId: (i: number) => string, valueId: (i: number) => string, disabled: boolean, err: string | null, edited: () => void)}
  {#each rows as row, i (i)}
    {@const vid = valueId(i)}
    {@const shown = revealed.has(vid)}
    <div class="header-row">
      <div class="field">
        <label for={nameId(i)}>Header {i + 1} name</label>
        <input id={nameId(i)} bind:value={row.name} autocomplete="off" spellcheck="false" {disabled} oninput={edited} />
      </div>
      <div class="field">
        <label for={vid}>Header {i + 1} value</label>
        {#if shown}
          <input id={vid} type="text" bind:value={row.value} autocomplete="off" spellcheck="false" {disabled} oninput={edited} />
        {:else}
          <input id={vid} type="password" bind:value={row.value} autocomplete="off" spellcheck="false" {disabled} oninput={edited} />
        {/if}
      </div>
      <div class="actions row-actions">
        <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" {disabled} aria-pressed={shown}
          aria-label={`${shown ? 'Hide' : 'Show'} the value of header ${i + 1}`} onclick={() => toggleShown(vid)}>
          {shown ? 'Hide' : 'Show'}
        </Button>
        {#if rows.length > 1}
          <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" {disabled}
            aria-label={`Drop header ${i + 1}`} onclick={() => { dropRow(rows, i, valueId); edited(); }}>Drop</Button>
        {/if}
      </div>
    </div>
  {/each}
  {#if err}<p class="hint hint-err" role="alert">{err}</p>{/if}
  <div class="actions">
    {#if rows.length < HEADERS_MAX}
      <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" {disabled}
        onclick={() => { rows.push(blankHeaderRow()); edited(); }}>Add another header</Button>
    {/if}
  </div>
  <p class="hint">{KEY_HINT}</p>
  <p class="hint">{VALUE_HINT}</p>
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

  <div class="split" class:picked>
    <nav class="card list" id="mcp-list" tabindex="-1" aria-label="MCP servers">
      <Button href={ADD_HREF} variant="outline" size="lg"
        class="w-full max-md:min-h-11 aria-[current=page]:border-[var(--muted)] aria-[current=page]:bg-[var(--bg)]"
        aria-current={pane.kind === 'add' ? 'page' : undefined}>Add server</Button>
      {#if d.servers.length === 0}
        <p class="empty">No MCP servers yet.</p>
      {:else}
        <ul>
          {#each d.servers as s (s.name)}
            {@const st = serverStatus(s)}
            <li>
              <a class="item" href={serverHref(s.name)} aria-current={shown === s.name ? 'page' : undefined}>
                <span class="item-name">{s.name}</span>
                <span class="item-count">{toolCountText(s.tools)}</span>
                <span class="item-state">
                  <span class="status-pill {PILL[st.tone]}">{st.word}</span>
                  {#if s.blocked_by_ceiling}<span class="status-pill status-pill--warn">blocked on the host</span>{/if}
                </span>
              </a>
            </li>
          {/each}
        </ul>
      {/if}
    </nav>

    {#if pane.kind !== 'list'}
    <div class="detail" id="mcp-pane" tabindex="-1">
      <a class="back" href={LIST_HREF}>All servers</a>
      {#if pane.kind === 'missing'}
        <section class="card" role="status">
          <p class="empty">There is no server named <code>{shortName(pane.name)}</code> here. Choose one from the list.</p>
        </section>
      {:else if pane.kind === 'server'}
        {@const s = pane.server}
        {@const st = serverStatus(s)}
        {@const inFlight = busy.get(s.name)}
        {@const asking = confirming.get(s.name)}
        {@const changes = refreshed.get(s.name)}
        {@const matching = new Set(filterTools(s.tools, show, query).map((t) => t.tool))}
        {@const listed = s.tools.filter((t) => matching.has(t.tool) || kept.has(toolKey(s.name, t.tool)))}
        <section class="server" aria-labelledby="mcp-s.{s.name}">
          <div class="card">
            <div class="server-head">
              <h2 id="mcp-s.{s.name}">{s.name}</h2>
              <span class="status-pill {PILL[st.tone]}">{st.word}</span>
              {#if st.detail}<span class="reason">{st.detail}</span>{/if}
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

            <div class="key">
              <p class="key-line">{keyLine(s.auth)}</p>
              {#if keyEdit?.server === s.name}
                <div class="confirm" role="group" aria-label="New key for {s.name}">
                  {@render headerRows(keyEdit.rows, (i) => `mcp-h.${s.name}.${i}`, (i) => `mcp-v.${s.name}.${i}`, !!inFlight, keyError, () => {})}
                  <div class="actions">
                    <Button size="sm" class="max-md:min-h-11 max-md:px-4" disabled={!!inFlight} onclick={() => void saveKey(s)}>
                      {inFlight === 'key' ? 'Saving…' : 'Save key'}
                    </Button>
                    <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={!!inFlight} onclick={() => void cancelKeyEdit(s.name)}>Cancel</Button>
                  </div>
                </div>
              {:else}
                <div class="actions">
                  {#if s.auth.kind !== 'oauth'}
                    <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" id="mcp-y.{s.name}"
                      disabled={!!inFlight || !secretsAvailable} onclick={() => void openKeyEdit(s.name)}>
                      {s.auth.kind === 'headers' ? 'Replace key' : 'Add key'}
                    </Button>
                  {/if}
                  {#if s.auth.kind === 'headers'}
                    <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4 hover:border-[var(--err-text)] hover:text-[var(--err-text)]"
                      id="mcp-z.{s.name}" disabled={!!inFlight} onclick={() => void openConfirm(s.name, 'key')}>
                      {inFlight === 'key' ? 'Removing…' : 'Remove key'}
                    </Button>
                  {/if}
                  {#if oauthAvailable && secretsAvailable}
                    <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" id="mcp-i.{s.name}" disabled={!!inFlight}
                      aria-expanded={signingIn === s.name} onclick={() => void toggleSignIn(s.name)}>
                      {s.auth.kind === 'oauth' ? 'Sign in again' : 'Sign in (OAuth)'}
                    </Button>
                  {/if}
                  {#if s.auth.kind === 'oauth'}
                    <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4 hover:border-[var(--err-text)] hover:text-[var(--err-text)]"
                      id="mcp-o.{s.name}" disabled={!!inFlight} onclick={() => void openConfirm(s.name, 'signout')}>
                      {inFlight === 'signout' ? 'Signing out…' : 'Sign out'}
                    </Button>
                  {/if}
                </div>
                {#if !secretsAvailable && s.auth.kind !== 'oauth'}<p class="hint">{NO_KEY_FILE_TEXT}</p>{/if}
                {#if signingIn === s.name}
                  <div class="confirm" id="mcp-sign.{s.name}" role="group" aria-label="Sign in for {s.name}">
                    <p>{s.auth.kind === 'oauth' ? 'Sign in again' : 'Sign in'} at the provider of {s.name}. The new sign-in replaces what is stored for it.</p>
                    <OAuthSignIn name={s.name} url={s.url} idPrefix="mcp-sign.{s.name}" disabled={!!inFlight}
                      onDone={(_flowId, state) => void signedInAgain(s, state)} />
                    <div class="actions">
                      <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" onclick={() => void toggleSignIn(s.name)}>Cancel</Button>
                    </div>
                  </div>
                {/if}
              {/if}
              {#if asking === 'signout'}
                <div class="confirm" id="mcp-c.{s.name}" role="group" aria-label="Confirm signing out of {s.name}">
                  <p>Sign out of {s.name}? {SIGN_OUT_NOTE}</p>
                  <div class="actions">
                    <Button variant="destructive" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={!!inFlight} onclick={() => void signOutOf(s)}>Yes, sign out</Button>
                    <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" onclick={() => void cancelConfirm(s.name)}>Cancel</Button>
                  </div>
                </div>
              {/if}
              {#if asking === 'key'}
                <div class="confirm" id="mcp-c.{s.name}" role="group" aria-label="Confirm removing the key of {s.name}">
                  <p>Remove the key stored for {s.name}? Its calls then go without it.</p>
                  <div class="actions">
                    <Button variant="destructive" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={!!inFlight} onclick={() => void removeKey(s)}>Yes, remove the key</Button>
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
          </div>

          <div class="card">
            <div class="tools-head">
              <h3 class="card-title">Tools</h3>
              <span class="count">{toolCountText(s.tools)}</span>
              {#if s.tools.length >= TOOL_FILTER_FROM || query !== '' || show !== 'all'}
                <div class="find">
                  <input class="search" type="search" placeholder="Find a tool" aria-label="Find a tool of {s.name}" bind:value={query} />
                  <div class="filters"><FilterBar options={TOOL_SHOW} bind:value={show} required label="Which tools of {s.name} to show" /></div>
                </div>
              {/if}
            </div>
            {#if s.tools.length === 0}
              <p class="hint">The server lists no tools.</p>
            {:else if listed.length === 0}
              <p class="hint" role="status">No tool matches.</p>
            {:else if listed.length < s.tools.length}
              <p class="hint" role="status">Showing {listed.length} of {s.tools.length}.</p>
            {/if}
            <ul class="tools">
              {#each listed as t (t.tool)}
                {@const ts = toolStatus(t, s.enabled)}
                {@const key = toolKey(s.name, t.tool)}
                {@const isOpen = opened.has(key)}
                <li class="tool">
                  <div class="tool-head">
                    <button type="button" class="open" aria-expanded={isOpen} aria-label={`Details of ${t.tool}`} onclick={() => toggleOpen(key)}>
                      {@render chevron()}
                      {@render toolName(t.tool)}
                    </button>
                    <span class="status-pill {PILL[ts.tone]}">{ts.word}</span>
                    {#if changes?.changed.includes(t.tool)}<span class="status-pill status-pill--warn">definition changed</span>{/if}
                    <span class="tool-act">
                      <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={!!inFlight}
                        aria-label={`${allowWord(t)} ${t.tool}`} onclick={() => void setTool(s, t, t.permission !== 'allow')}>
                        {inFlight === `tool ${t.tool}` ? 'Saving…' : allowWord(t)}
                      </Button>
                    </span>
                  </div>
                  {#if ts.detail}<p class="reason why">{ts.detail}</p>{/if}
                  {#if t.blocked_by_ceiling}<p class="limit why">Blocked by the server's host settings.</p>{/if}
                  {@render note(notes.get(key))}
                  {#if isOpen}{@render toolDetail(t.name, t.description, t.schema)}{/if}
                </li>
              {/each}
            </ul>
          </div>
        </section>
      {:else}
        <section class="card" aria-labelledby="mcp-add">
          <h2 id="mcp-add">Add a server</h2>
          <p class="lead">Check the server's address, choose which of its tools the agent may use, then add it.</p>
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
              <p id="mcp-url-hint" class="hint" class:hint-err={urlErr}>{urlErr ?? (isHttp(newUrl.trim()) ? HTTP_NOTE : URL_RULE)}</p>
            </div>
            <fieldset class="field auth" aria-describedby="mcp-auth-hint">
              <legend>Authentication</legend>
              <label class="check">
                <input type="radio" name="mcp-auth" value="none" checked={authKind === 'none'} disabled={checking || adding}
                  onchange={() => chooseAuth('none')} />
                None
              </label>
              <label class="check">
                <input type="radio" name="mcp-auth" value="headers" checked={authKind === 'headers'}
                  disabled={checking || adding || !secretsAvailable} onchange={() => chooseAuth('headers')} />
                API key or header
              </label>
              <label class="check">
                <input type="radio" name="mcp-auth" value="oauth" checked={authKind === 'oauth'}
                  disabled={checking || adding || !secretsAvailable || !oauthAvailable} onchange={() => chooseAuth('oauth')} />
                Sign in (OAuth)
              </label>
              <p id="mcp-auth-hint" class="hint">
                {!secretsAvailable ? NO_KEY_FILE_TEXT : !oauthAvailable ? OAUTH_UNAVAILABLE_TEXT : 'Sign in when the server asks you to log in at its provider.'}
              </p>
            </fieldset>
            {#if authKind === 'headers'}
              <div class="key-rows">
                {@render headerRows(newHeaders, (i) => `mcp-new-h-${i}`, (i) => `mcp-new-v-${i}`, checking || adding, headersErr, headersEdited)}
              </div>
            {/if}
            {#if authKind === 'oauth'}
              <div class="key-rows" role="group" aria-label="Sign in for the new server">
                {#if nameProblem(newName) || urlProblem(newUrl.trim())}
                  <p class="hint">Enter the name and the address first: the sign-in is made for them.</p>
                {/if}
                {#key oauthPanel}
                  <OAuthSignIn name={newName} url={newUrl.trim()} idPrefix="mcp-new-oauth"
                    disabled={checking || adding || !!nameProblem(newName) || !!urlProblem(newUrl.trim())}
                    onDone={(flowId, state, name, url) => formSignedIn(name, url, flowId, state)} />
                {/key}
                {#if signedIn}<p class="hint">Signed in for {signedIn.name}. Check the server next: it is checked with this sign-in.</p>{/if}
              </div>
            {/if}
            {#if replaces}
              <p class="hint">A server named {newName} exists. Adding it again replaces the server: its tools are checked again, it is switched on, and background work is turned off. Its call timeout goes back to 30 s.</p>
              <p class="hint">{authKind === 'headers' ? 'The key typed here replaces its stored key.' : authKind === 'oauth' ? 'Signing in for it stores the new sign-in with it at once; use Sign in again on its page for that.' : 'A stored key is kept at the same address, and removed at another.'}</p>
            {/if}
            <div class="actions">
              <Button type="submit" variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={checking || adding}>
                {checking ? 'Checking…' : 'Check server'}
              </Button>
            </div>
          </form>

          {#if probe && !probeFresh}
            <p class="hint">{probe.name !== newName || probe.url !== newUrl.trim() ? 'The name or address changed since the check: check the server again.' : 'The key changed since the check: check the server again.'}</p>
          {:else if probe}
            {@const tools = probe.result.tools}
            <h3>{tools.length} tool{tools.length === 1 ? '' : 's'} listed</h3>
            <p class="hint">Exactly what the agent would be offered. Nothing is allowed until you tick it.</p>
            <ul class="tools">
              {#each tools as t (t.tool)}
                {@const key = `probe:${t.tool}`}
                {@const isOpen = opened.has(key)}
                <li class="tool">
                  <div class="tool-head">
                    <button type="button" class="open" aria-expanded={isOpen} aria-label={`Details of ${t.tool}`} onclick={() => toggleOpen(key)}>
                      {@render chevron()}
                    </button>
                    <label class="check">
                      <input type="checkbox" checked={allowed.get(t.tool) === true} disabled={adding}
                        onchange={(e) => allowed.set(t.tool, e.currentTarget.checked)} />
                      Allow {@render toolName(t.tool)}
                    </label>
                    {#if t.skip_reason}
                      <span class="status-pill status-pill--warn">would be skipped</span>
                    {/if}
                  </div>
                  {#if t.skip_reason}<p class="reason why">{t.skip_reason}</p>{/if}
                  {#if isOpen}{@render toolDetail(t.name, t.description, t.schema)}{/if}
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
      {/if}
    </div>
    {/if}
  </div>
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
  .card > h2 { margin-bottom: 0.375rem; }
  .lead { font-size: 0.8125rem; color: var(--muted); margin: 0 0 0.875rem; max-width: 72ch; }

  /* The servers beside one pane. The list stays in view while a long tool list scrolls. */
  .split { display: grid; grid-template-columns: 17rem minmax(0, 1fr); gap: 1.25rem; align-items: start; }
  .detail { min-width: 0; }
  .list { position: sticky; top: 1.5rem; margin-bottom: 0; padding: 0.75rem; max-height: calc(100vh - 3rem); overflow-y: auto; }
  .list ul { list-style: none; margin: 0.625rem 0 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
  .list .empty { padding: 0.875rem 0 0.375rem; font-size: 0.8125rem; }
  .item { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: baseline; gap: 0.25rem 0.5rem;
    padding: 0.5rem 0.625rem; border-radius: var(--radius-sm); color: var(--text); text-decoration: none;
    transition: background var(--transition); }
  .item:hover { background: var(--wash); }
  .item[aria-current='page'] { background: var(--bg); }
  .item:focus-visible, .back:focus-visible, .open:focus-visible, .search:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
  .item-name { font-size: 0.875rem; font-weight: 500; overflow-wrap: anywhere; }
  .item[aria-current='page'] .item-name { font-weight: 650; }
  .item-count { font-size: 0.75rem; color: var(--muted); white-space: nowrap; }
  .item-state { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 0.25rem; }
  .back { display: none; align-items: center; min-height: 2.75rem; margin-bottom: 0.25rem; font-size: 0.875rem;
    color: var(--accent-text); text-decoration: none; border-radius: var(--radius-sm); }
  .back:hover { text-decoration: underline; }
  .server { min-width: 0; }
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
  /* A server's tools: the heading with its count, then the search and the filter on one line while they fit. */
  .tools-head { display: flex; align-items: center; gap: 0.5rem 0.75rem; flex-wrap: wrap; margin-bottom: 0.5rem; }
  .card-title { font-size: 0.9375rem; font-weight: 650; color: var(--text); margin: 0; }
  .count { font-size: 0.8125rem; color: var(--muted); }
  .find { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; margin-left: auto; }
  .search { height: 2.25rem; width: 14rem; max-width: 100%; padding: 0 0.875rem; background: var(--surface);
    border: 1px solid var(--field-border); border-radius: var(--ctl-radius); color: var(--text); font: inherit; font-size: 0.875rem; }
  .search::placeholder { color: var(--muted); }
  .filters { display: inline-flex; background: var(--wash); border-radius: var(--ctl-radius); }
  .tools { list-style: none; margin: 0; padding: 0; }
  /* One line a tool; what the server wrote about it opens under the line. */
  .tool { padding: 0.25rem 0; border-top: 1px solid var(--border); min-width: 0; }
  .tool:first-child { border-top: 0; }
  .tool-head { display: flex; align-items: center; gap: 0.25rem 0.625rem; min-height: 2rem; min-width: 0; }
  /* The name is the part that gives way: it wraps inside its own space, so the state and the control beside it stay
     in the pane whatever the server called its tool. */
  .open { display: inline-flex; align-items: center; gap: 0.375rem; flex: 0 1 auto; min-width: 0; min-height: 1.75rem; margin-left: -0.25rem;
    padding: 0.125rem 0.375rem 0.125rem 0.25rem; background: none; border: 0; border-radius: var(--radius-sm); color: var(--text); font: inherit;
    text-align: left; cursor: pointer; transition: background var(--transition); }
  .open:hover { background: var(--wash); }
  .chev { flex: none; color: var(--muted); transition: transform var(--transition); }
  .open[aria-expanded='true'] .chev { transform: rotate(90deg); }
  .tool-name { font-weight: 500; min-width: 0; }
  .reason { font-size: 0.75rem; color: var(--muted); overflow-wrap: anywhere; min-width: 0; }
  /* Why a tool is skipped or blocked: whole, on its own line under the tool's, in line with its name. */
  .why { margin: 0 0 0.25rem 1.375rem; }
  .list:focus, .detail:focus { outline: none; }
  .tool-head .status-pill { flex: none; }
  .tool-act { margin-left: auto; flex: none; }
  .tool-detail { padding: 0.125rem 0 0.5rem 1.375rem; min-width: 0; }
  .tool-detail .hint:first-child { margin-top: 0; }
  .desc { font-size: 0.8125rem; margin: 0.375rem 0 0; max-width: 72ch; overflow-wrap: anywhere; white-space: pre-line; }
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
  .auth { border: 0; padding: 0; margin: 0; min-width: 0; }
  .auth legend { font-size: 0.75rem; font-weight: 600; color: var(--muted); padding: 0; margin-bottom: 0.25rem; }
  .key { margin-top: 0.875rem; }
  .key-line { font-size: 0.8125rem; margin: 0; overflow-wrap: anywhere; }
  .key-rows { display: flex; flex-direction: column; gap: 0.5rem; }
  .header-row { display: flex; align-items: flex-end; gap: 0.5rem 0.75rem; flex-wrap: wrap; margin-top: 0.5rem; }
  .header-row .field { flex: 1 1 14rem; }
  .header-row .row-actions { margin-top: 0; }
  @media (max-width: 900px) {
    /* One column: the list first, then (once something is picked) the pane alone, with a way back to the list. */
    .split { grid-template-columns: minmax(0, 1fr); }
    .list { position: static; max-height: none; }
    .split.picked .list { display: none; }
    .back { display: inline-flex; }
  }
  @media (max-width: 768px) {
    /* 44px targets on a phone: the list's rows, the checkbox rows, a tool's line and the schema's disclosure (a
       summary stays a list item, so the browser keeps drawing its marker). */
    .item, .check, .open { min-height: 2.75rem; }
    .schema summary { line-height: 2.75rem; padding: 0; }
    .tool-head { flex-wrap: wrap; }
    .why { margin-left: 0; }
    .find { margin-left: 0; width: 100%; }
    .search { height: 2.75rem; width: 100%; }
    .tool-detail { padding-left: 0; }
  }
</style>
