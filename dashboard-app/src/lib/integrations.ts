// F112 PR 2b: the Integrations screen's client for the owner's MCP server routes (nous/api/mcp_client_routes.py) and
// the pure rules it shows: the name and address checks the server makes, the status words, the create payload and
// the words for a refused request. Every string a server wrote is shown as text by the view, never as markup.
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
}

export interface McpServerList {
  ceilings: { pinned_servers: boolean; background_servers: boolean };
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
}

const BASE = '/integrations/mcp';
const serverPath = (name: string) => `${BASE}/servers/${encodeURIComponent(name)}`;

export const listServers = (signal?: AbortSignal) => apiRequest<McpServerList>(`${BASE}/servers`, 'GET', undefined, signal);
export const probeServer = (name: string, url: string) => apiRequest<ProbeResult>(`${BASE}/probe`, 'POST', { name, url });
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
  const api = (e?.body as { error?: unknown } | undefined)?.error;
  if (typeof api === 'string' && api.trim()) {
    const text = api.trim();
    return `${text[0].toUpperCase()}${text.slice(1)}${/[.!?]$/.test(text) ? '' : '.'}`;
  }
  if (status === 503) return 'MCP servers are not available here.';
  if (status === 404) return 'Not found: the MCP client may be switched off.';
  return `The request failed (HTTP ${status}).`;
}

/** 409: the server's listing differs from what the owner was shown. */
export const isConflict = (err: unknown) => (err as ApiAnswerError | null)?.status === 409;

export type Tone = 'ok' | 'warn' | 'neutral';
export interface Status { word: string; detail: string | null; tone: Tone }

/** A stored tool's status in words, from its fields (never from the reason's text): in use, not allowed (the owner's
 * permission; a stored "ask" is not allowed either), or skipped with the server's reason. A tool not allowed that could
 * not be offered anyway (no schema) carries the reason too, so allowing it is not a surprise. */
export function toolStatus(t: McpTool): Status {
  if (t.registered) return { word: 'in use', detail: null, tone: 'ok' };
  if (t.permission !== 'allow') {
    return { word: 'not allowed', detail: t.schema === null ? t.skip_reason : null, tone: 'neutral' };
  }
  return { word: 'skipped', detail: t.skip_reason ?? 'it is not registered', tone: 'warn' };
}

/** `toolStatus` as one line of text: "in use", "not allowed" or "skipped: <reason>". */
export function toolStatusText(t: McpTool): string {
  const s = toolStatus(t);
  return s.detail ? `${s.word}: ${s.detail}` : s.word;
}

/** A server's status in words. A server connects at its first call, so "not connected yet" is the usual state. */
export function serverStatus(s: McpServer): Status {
  if (!s.enabled) return { word: 'switched off', detail: null, tone: 'neutral' };
  if (s.connected) return { word: 'connected', detail: null, tone: 'ok' };
  return { word: 'not connected yet', detail: null, tone: 'neutral' };
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

/** The add request: every probed tool named, allowed only when the owner ticked it, and the probe's hash, so a
 * server that answers differently at the add is refused (409) with nothing stored. */
export function createPayload(name: string, url: string, probe: ProbeResult, allowed: Record<string, boolean>): CreatePayload {
  return {
    name,
    url,
    tools: Object.fromEntries(probe.tools.map((t) => [t.tool, allowed[t.tool] ? 'allow' : 'deny'])),
    expected_definitions_sha256: probe.definitions_sha256,
  };
}

/** What a refresh changed, in words. A changed definition keeps its permission, so it is named for review. */
export function refreshLines(r: Pick<RefreshResult, 'added' | 'removed' | 'changed'>): string[] {
  const lines: string[] = [];
  if (r.added.length) lines.push(`New, not allowed until you allow them: ${r.added.join(', ')}.`);
  if (r.removed.length) lines.push(`No longer listed by the server, now not allowed: ${r.removed.join(', ')}.`);
  if (r.changed.length) {
    lines.push(`Definition changed, permission kept: ${r.changed.join(', ')}. Read them again below.`);
  }
  if (!lines.length) lines.push('No change: the server lists the same tools.');
  return lines;
}

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
