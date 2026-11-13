// F112 PR 2b: the Integrations screen's client for the owner's MCP server routes (nous/api/mcp_client_routes.py) and
// the pure rules it shows: the name and address checks the server makes, the status words, the create payload and
// the words for a refused request. Every string a server wrote is shown as text by the view, never as markup.
// PR 2c: a server's stored key (header names only ever come back) and the header rules checked before sending.
import { apiRequest, type ApiAnswerError } from './api';

/** One stored tool as the listing shows it. `tool` is the server's own name (permissions are keyed by it); `name` is
 * `mcp__<server>__<tool>`; `description` and `schema` are what the model is offered, null when it cannot be. */
export interface McpTool {
  tool: string;
  name: string | null;
  permission: string;
  registered: boolean;
  skip_reason: string | null;
  blocked_by_ceiling: boolean;
  description: string | null;
  schema: string | null;
}

/** F112 PR 2c: a server's stored credential, never its values. `status`: `unavailable` when it cannot be opened on
 * this host (`reason` says why), `credentials_refused` when the server's last answer was a 401. */
export interface McpAuth {
  kind: 'none' | 'headers' | 'oauth';
  header_names: string[];
  updated_at: string | null;
  status: 'ok' | 'credentials_refused' | 'unavailable';
  reason: string | null;
}

export interface McpServer {
  name: string;
  url: string;
  enabled: boolean;
  background: boolean;
  background_in_effect: boolean;
  background_blocked_by_ceiling: boolean;
  call_timeout_seconds: number;
  connected: boolean;
  blocked_by_ceiling: boolean;
  skip_reason: string | null;
  tools: McpTool[];
  auth: McpAuth;
}

export interface McpServerList {
  ceilings: { pinned_servers: boolean; background_servers: boolean };
  /** Whether the host has a key, so a credential can be stored. */
  secrets_available: boolean;
  servers: McpServer[];
}

/** One tool of a probe: what would be registered, or why it would be skipped. */
export interface ProbedTool {
  tool: string;
  name: string | null;
  description: string | null;
  schema: string | null;
  skip_reason: string | null;
}

export interface ProbeResult {
  tools: ProbedTool[];
  definitions_sha256: string;
}

export interface CreatePayload {
  name: string;
  url: string;
  tools: Record<string, 'allow' | 'deny'>;
  expected_definitions_sha256: string;
  headers?: Record<string, string>;
}

export interface RefreshResult {
  added: string[];
  removed: string[];
  changed: string[];
  definitions_sha256: string;
  server: McpServer | null;
}

export interface ServerChange {
  enabled?: boolean;
  background?: boolean;
  tools?: Record<string, 'allow' | 'deny'>;
  /** Replace the stored credential, or remove it with null. */
  headers?: Record<string, string> | null;
}

const BASE = '/integrations/mcp';
/** A server's route. A name is encoded as a path segment whatever it holds (the name rule leaves nothing to encode). */
export const serverPath = (name: string) => `${BASE}/servers/${encodeURIComponent(name)}`;

export const listServers = (signal?: AbortSignal) => apiRequest<McpServerList>(`${BASE}/servers`, 'GET', undefined, signal);
/** A probe; `headers` are sent on its requests and never stored. The body names them only when there are some. */
export const probeServer = (name: string, url: string, headers?: Record<string, string>) =>
  apiRequest<ProbeResult>(`${BASE}/probe`, 'POST', headers ? { name, url, headers } : { name, url });
export const createServer = (payload: CreatePayload) =>
  apiRequest<{ created: boolean; definitions_sha256: string; server: McpServer | null }>(`${BASE}/servers`, 'POST', payload);
export const updateServer = (name: string, change: ServerChange) =>
  apiRequest<{ server: McpServer | null }>(serverPath(name), 'PATCH', change);
export const refreshServer = (name: string) => apiRequest<RefreshResult>(`${serverPath(name)}/refresh`, 'POST');
export const removeServer = (name: string) => apiRequest<{ deleted: string }>(serverPath(name), 'DELETE');

/** The server's rule for a name (config.SERVER_NAME, and no "__"), in words. */
export const NAME_RULE = '1 to 24 lower-case letters, digits, - and _, starting with a letter or digit, with no __.';
/** The server's rule for an address (config.url_problem), in words. */
export const URL_RULE = 'An https address, with no user name, password, query string or fragment.';

/** Why `name` cannot name a server, or null when it can. */
export function nameProblem(name: string): string | null {
  if (name === '') return 'Enter a name.';
  if (!/^[a-z0-9][a-z0-9_-]{0,23}$/.test(name) || name.includes('__')) return `A name is ${NAME_RULE}`;
  return null;
}

/** Why `url` cannot be a server's address, or null when it can. The address guard (private, loopback and metadata
 * addresses) is the server's check only. */
export function urlProblem(url: string): string | null {
  if (url === '') return 'Enter the server\'s address.';
  let parts: URL;
  try {
    parts = new URL(url);
  } catch {
    return 'This is not a valid address.';
  }
  if (parts.protocol !== 'https:') return 'Only https addresses are accepted.';
  if (!parts.hostname) return 'The address has no host.';
  if (parts.username || parts.password) return 'The address may not carry a user name or password.';
  if (parts.search || parts.hash) return 'The address may not carry a query string or fragment.';
  return null;
}

export type ListGate = 'unavailable' | 'forbidden' | null;

/** The list's refusals that are a state of the screen, not an error: 403 (the request did not come through the
 * proxy); 503 (the secret is not configured, or no client runs) and 404 (the routes are not mounted: the client is
 * switched off). */
export function listGate(err: unknown): ListGate {
  const status = (err as ApiAnswerError | null)?.status;
  if (status === 403) return 'forbidden';
  if (status === 404 || status === 503) return 'unavailable';
  return null;
}

export const FORBIDDEN_TEXT = 'Open this screen through the protected address: this request did not come through the proxy.';
export const CHANGED_TEXT = "The server's tools changed since you checked it. The new list is below: choose again.";

/** A refused or failed request in words: the route's own message when it sent one, else a fixed sentence. */
export function problemText(err: unknown): string {
  const e = err as ApiAnswerError | null;
  const status = e?.status;
  if (status === undefined) return `No answer from Nous (${e?.message || 'no message'}).`;
  if (status === 403) return FORBIDDEN_TEXT;
  if (status === 415) return 'The request was not sent as JSON, so it was refused.';
  if (status >= 200 && status < 300) return `The answer could not be read: it is not JSON (HTTP ${status}).`;
  const api = (e?.body as { error?: unknown } | undefined)?.error;
  if (typeof api === 'string' && api.trim()) {
    const text = api.trim();
    return `${text[0].toUpperCase()}${text.slice(1)}${/[.!?]$/.test(text) ? '' : '.'}`;
  }
  if (status === 503) return 'MCP servers are not available here.';
  if (status === 404) return 'Not found: the MCP client may be switched off.';
  return `The request failed (HTTP ${status}).`;
}

/** 409 with the new hash: the server's listing differs from what the owner was shown. Another 409 (no key file on
 * the host) is not this one. */
export function isConflict(err: unknown): boolean {
  const e = err as ApiAnswerError | null;
  const body = e?.body as { definitions_sha256?: unknown } | undefined;
  return e?.status === 409 && typeof body?.definitions_sha256 === 'string';
}

/** A 409 that is not a changed listing: the host has no key file, so nothing typed can be stored. */
export const isNoKey = (err: unknown) => (err as ApiAnswerError | null)?.status === 409 && !isConflict(err);

export type Tone = 'ok' | 'warn' | 'neutral';
export interface Status { word: string; detail: string | null; tone: Tone }

/** The reason a switched-off server's tools are skipped, in the screen's words (the server says "disabled"). */
export const SWITCHED_OFF_REASON = 'the server is switched off';

/** A stored tool's status in words, from its fields (never from the reason's text): in use, not allowed (the owner's
 * permission; a stored "ask" is not allowed either), or skipped with the server's reason. A tool not allowed that could
 * not be offered anyway (no schema) carries the reason too, so allowing it is not a surprise. Every tool of a server
 * that is switched off is skipped for that first, so its reason is said in the screen's words. */
export function toolStatus(t: McpTool, serverEnabled = true): Status {
  const reason = serverEnabled ? t.skip_reason : SWITCHED_OFF_REASON;
  if (t.registered) return { word: 'in use', detail: null, tone: 'ok' };
  if (t.permission !== 'allow') return { word: 'not allowed', detail: t.schema === null ? reason : null, tone: 'neutral' };
  return { word: 'skipped', detail: reason ?? 'it is not registered', tone: 'warn' };
}

/** `toolStatus` as one line of text: "in use", "not allowed" or "skipped: <reason>". */
export function toolStatusText(t: McpTool, serverEnabled = true): string {
  const s = toolStatus(t, serverEnabled);
  return s.detail ? `${s.word}: ${s.detail}` : s.word;
}

/** A server's status in words. A server connects at its first call, so "connects on first use" is the usual state
 * (the owner read "not connected yet" as a fault). A stored key that cannot be read here, or that the server refused,
 * comes before the connection. */
export function serverStatus(s: McpServer): Status {
  if (!s.enabled) return { word: 'switched off', detail: null, tone: 'neutral' };
  if (s.auth.status === 'unavailable') return { word: 'key cannot be read on this host', detail: s.auth.reason, tone: 'warn' };
  if (s.auth.status === 'credentials_refused') {
    // The reason says a 403 ("forbidden"): the key may be wrong or lack access.
    return { word: s.auth.kind === 'none' ? 'the server asks for a key' : 'the server refused the key', detail: s.auth.reason, tone: 'warn' };
  }
  if (s.connected) return { word: 'connected', detail: null, tone: 'ok' };
  return { word: 'connects on first use', detail: null, tone: 'neutral' };
}

// ── Stored keys (F112 PR 2c) ────────────────────────────────────────────────

/** One row of the key form: a header's name and the value typed for it. Values live only in the form's state. */
export interface HeaderRow { name: string; value: string }

export const HEADERS_MAX = 8;
export const KEY_HINT = 'Most servers want Authorization with the value "Bearer <your key>".';
export const NO_KEY_FILE_TEXT = 'A key cannot be stored here: no key file is configured on the host (NOUS_MCP_CLIENT_KEY_FILE).';
export const blankHeaderRow = (name = ''): HeaderRow => ({ name, value: '' });

// The server's rules (config.validate_headers), checked here too so a typo is said before anything is sent.
const HEADER_NAME = /^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$/;
const HEADER_VALUE = /^[\x20\x21\x23-\x5b\x5d-\x7e]{1,4096}$/;
/** A shorter value could never be withheld from a result (bridge.WITHHELD_MIN_CHARS), so the server refuses it. */
export const HEADER_VALUE_MIN = 8;
/** Letters and digits a value needs in all (config.HEADER_VALUE_MIN_ALNUM): the scrub matches them literally. */
export const HEADER_VALUE_MIN_ALNUM = 8;
/** Characters that are not letters or digits allowed before the first one, in the value and in the key after its
 * scheme (config.HEADER_VALUE_MAX_LEADING_PUNCTUATION). */
export const HEADER_VALUE_MAX_LEADING_PUNCTUATION = 8;
const leadingPunctuation = (text: string) => {
  const at = text.search(/[A-Za-z0-9]/);
  return at < 0 ? text.length : at;
};
const RESERVED_HEADERS = new Set([
  'host', 'content-length', 'content-type', 'content-encoding', 'transfer-encoding', 'trailer', 'connection',
  'keep-alive', 'proxy-connection', 'expect', 'accept', 'accept-encoding', 'cookie', 'origin', 'user-agent',
  'mcp-session-id', 'mcp-protocol-version', 'last-event-id', 'te', 'upgrade', 'proxy-authorization',
]);

/** Why `rows` cannot be stored, or null when they can. A refusal names the header by its place, never its value. */
export function headersProblem(rows: readonly HeaderRow[]): string | null {
  if (rows.length < 1 || rows.length > HEADERS_MAX) return `Give 1 to ${HEADERS_MAX} headers.`;
  const seen = new Set<string>();
  for (const [i, row] of rows.entries()) {
    const at = `Header ${i + 1}`;
    if (!HEADER_NAME.test(row.name)) return `${at}: a name is 1 to 64 letters, digits or any of !#$%&'*+.^_\`|~- (no space).`;
    const lower = row.name.toLowerCase();
    if (RESERVED_HEADERS.has(lower) || lower.startsWith('x-nous-')) return `${at}: Nous sets this header itself, so it cannot be stored.`;
    if (seen.has(lower)) return `${at}: this name is used twice (names are compared without case).`;
    seen.add(lower);
    if (row.value === '') return `${at}: enter its value.`;
    if (row.value.length < HEADER_VALUE_MIN) {
      return `${at}: a value is ${HEADER_VALUE_MIN} to 4096 characters (a shorter one could not be kept out of the tools' results).`;
    }
    if (!HEADER_VALUE.test(row.value)) return `${at}: a value is at most 4096 printable ASCII characters, with no " or \\.`;
    if (row.value !== row.value.trim()) return `${at}: a value may not start or end with a space.`;
    if ((row.value.match(/[A-Za-z0-9]/g) ?? []).length < HEADER_VALUE_MIN_ALNUM) {
      return `${at}: a value needs at least ${HEADER_VALUE_MIN_ALNUM} letters or digits.`;
    }
    const parts = row.value.split(/\s+/);
    const starts = parts.length === 2 ? [row.value, parts[1]] : [row.value];
    if (starts.some((s) => leadingPunctuation(s) > HEADER_VALUE_MAX_LEADING_PUNCTUATION)) {
      return `${at}: a value, and the key after its scheme, may start with at most ${HEADER_VALUE_MAX_LEADING_PUNCTUATION} characters that are not letters or digits.`;
    }
  }
  return null;
}

/** The rows as the request's `headers`, every name an own key whatever it is. */
export const headersBody = (rows: readonly HeaderRow[]): Record<string, string> =>
  Object.fromEntries(rows.map((r) => [r.name, r.value]));

/** The card's line about the stored key: the header names only, never a value. */
export function keyLine(auth: McpAuth): string {
  if (auth.kind === 'headers') return `Key set: ${auth.header_names.join(', ')}`;
  if (auth.kind === 'oauth') return 'Signed in with OAuth.';
  return 'No key stored.';
}

/** The host's limits in effect, in words; empty when neither is set. */
export function ceilingLines(c: McpServerList['ceilings']): string[] {
  const lines: string[] = [];
  if (c.pinned_servers) {
    lines.push('Only the servers and tools pinned on the host (NOUS_MCP_CLIENT_PINNED_SERVERS) can be used, whatever is allowed here.');
  }
  if (c.background_servers) {
    lines.push('Background work can use only the servers listed on the host (NOUS_MCP_CLIENT_BACKGROUND_SERVERS).');
  }
  return lines;
}

/** The add request: every probed tool named, allowed only when the owner ticked it (`true`, nothing else), and the
 * probe's hash, so a server that answers differently at the add is refused (409) with nothing stored. A tool's name is
 * the server's text: `allowed` is a Map and `tools` is built by Object.fromEntries, so a name such as `constructor` is
 * never read from a prototype and `__proto__` is an own key of the body, not the object's prototype. */
export function createPayload(
  name: string, url: string, probe: ProbeResult, allowed: ReadonlyMap<string, boolean>, headers?: Record<string, string>,
): CreatePayload {
  const payload: CreatePayload = {
    name,
    url,
    tools: Object.fromEntries(probe.tools.map((t) => [t.tool, allowed.get(t.tool) === true ? 'allow' : 'deny'])),
    expected_definitions_sha256: probe.definitions_sha256,
  };
  if (headers) payload.headers = headers; // named only when the owner typed some
  return payload;
}

/** The change that allows or stops allowing one stored tool, its name an own key of the body whatever it is. */
export function toolChange(tool: string, allow: boolean): ServerChange {
  return { tools: Object.fromEntries([[tool, allow ? 'allow' : 'deny']]) };
}

/** One line of a refresh's result: the screen's words, then the server's tool names (shown as data), then more words. */
export interface RefreshLine { lead: string; tools: string[]; tail: string }

/** What a refresh changed. A changed definition keeps its permission, so it is named for review. */
export function refreshLines(r: Pick<RefreshResult, 'added' | 'removed' | 'changed'>): RefreshLine[] {
  const lines: RefreshLine[] = [];
  if (r.added.length) lines.push({ lead: 'New, not allowed until you allow them:', tools: r.added, tail: '' });
  if (r.removed.length) lines.push({ lead: 'No longer listed by the server, now not allowed:', tools: r.removed, tail: '' });
  if (r.changed.length) lines.push({ lead: 'Definition changed, permission kept:', tools: r.changed, tail: 'Read them again below.' });
  if (!lines.length) lines.push({ lead: 'No change: the server lists the same tools.', tools: [], tail: '' });
  return lines;
}

/** A refresh line as one sentence (what the screen shows, the names in their own elements). */
export const refreshLineText = (l: RefreshLine) =>
  `${l.lead}${l.tools.length ? ` ${l.tools.join(', ')}.` : ''}${l.tail ? ` ${l.tail}` : ''}`;

/** A raw name cut to `max` characters with an ellipsis (the full name goes in a title). */
export const shortName = (name: string, max = 80) => (name.length > max ? `${name.slice(0, max - 1)}…` : name);

/** A long text cut at a word near `max` characters, with an ellipsis; `cut` says whether anything was left out. */
export function clampText(text: string, max = 180): { text: string; cut: boolean } {
  if (text.length <= max) return { text, cut: false };
  const head = text.slice(0, max);
  const space = head.lastIndexOf(' ');
  return { text: `${(space > max * 0.6 ? head.slice(0, space) : head).trimEnd()}…`, cut: true };
}

/** A serialized schema indented for reading; the text as given when it is not JSON. */
export function prettySchema(schema: string): string {
  try {
    return JSON.stringify(JSON.parse(schema), null, 2);
  } catch {
    return schema;
  }
}
