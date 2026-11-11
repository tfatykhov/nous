// F108 Work and Model calls screens: tool lanes and the formatters they share.
import { fmtUtc } from './harness';

/**
 * The tool lanes (what a tool touches). The names are the endpoints' (`TOOL_LANES` in nous/api/work_view_dashboard.py);
 * any tool not in a lane is `other`, the neutral colour. Each lane's colour is its app.css token, `--lane-<name>`.
 */
export const LANES = ['memory', 'live', 'workspace', 'acts', 'other'] as const;
const LANE_SET = new Set<string>(LANES);

/** The lane's colour token, `var(--lane-memory)`; `other` for any lane not listed. */
export function laneVar(lane: string): string {
  return `var(--lane-${LANE_SET.has(lane) ? lane : 'other'})`;
}

/** `"40 ms"` under a second, `"1.4 s"` from a second; `"—"` when there is no duration. */
export function fmtDuration(ms: number | null): string {
  if (ms == null) return '—';
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

/** `"999"`, `"54.5K"` from 1,000, `"2.5M"` from 1,000,000; `"—"` when there is no count. */
export function fmtTokens(n: number | null): string {
  if (n == null) return '—';
  if (n < 1000) return String(n);
  // 999,950 and up round to 1.0M, never "1000.0K": the unit is chosen from the rounded value.
  if (n < 999_950) return `${(n / 1000).toFixed(1)}K`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

/** A window's length: `"1 day"`, `"7 days"`. */
export function fmtDays(n: number): string {
  return `${n} day${n === 1 ? '' : 's'}`;
}

/** `"Nov 7 00:07 UTC"` from a UTC ISO timestamp, the same text as the harness screens (`fmtUtc`); `"—"` for null, empty or unparsable input. */
export function fmtTime(iso: string | null): string {
  return fmtUtc(iso);
}

/** One chip per tool, in first-seen order, its calls summed: `"bash ×3"` when called more than once. */
export function toolChips(tools: { tool: string; calls: number; lane: string }[]): { label: string; lane: string }[] {
  const byTool = new Map<string, { calls: number; lane: string }>();
  for (const t of tools) {
    const seen = byTool.get(t.tool);
    if (seen) seen.calls += t.calls;
    else byTool.set(t.tool, { calls: t.calls, lane: t.lane });
  }
  return [...byTool].map(([tool, { calls, lane }]) => ({ label: calls > 1 ? `${tool} ×${calls}` : tool, lane }));
}

/** How many model calls a model-call row stands for: a minute bucket (embedding, rerank) holds `call_count`. */
export function callsOf(c: { call_count: number }): number {
  return c.call_count;
}

/** What a `#/work/<sub>` path shows. `notfound` covers an unknown path and a malformed `%` escape. */
export type WorkView =
  | { view: 'sessions' }
  | { view: 'patterns' }
  | { view: 'changes' }
  | { view: 'trace'; sessionId: string; turn: string | null }
  | { view: 'notfound' };

/** `decodeURIComponent` that gives null, not an exception, for a malformed `%` escape. */
export function safeDecode(segment: string): string | null {
  try {
    return decodeURIComponent(segment);
  } catch {
    return null;
  }
}

/**
 * The view of a Work sub path (still URL-encoded, as `currentSub` holds it): `''` the sessions list, `patterns`,
 * `changes`, `s/<session id>[/<turn id>]` the trace. A trailing slash is ignored; anything else is `notfound`.
 */
export function parseWorkSub(sub: string): WorkView {
  const segs = sub.split('/');
  while (segs.length > 0 && segs[segs.length - 1] === '') segs.pop();
  if (segs.length === 0) return { view: 'sessions' };
  if (segs.length === 1 && segs[0] === 'patterns') return { view: 'patterns' };
  if (segs.length === 1 && segs[0] === 'changes') return { view: 'changes' };
  if (segs[0] === 's' && (segs.length === 2 || segs.length === 3)) {
    const sessionId = safeDecode(segs[1]);
    const turn = segs.length === 3 ? safeDecode(segs[2]) : null;
    if (!sessionId || (segs.length === 3 && !turn)) return { view: 'notfound' };
    return { view: 'trace', sessionId, turn };
  }
  return { view: 'notfound' };
}

let lastListQuery: Record<string, string> = {};
/** Remember the Work list's filters (its hash query) so the trace's Back link can restore them. */
export function setLastListQuery(q: Record<string, string>): void {
  lastListQuery = { ...q };
}
/** The list filters last shown by the Work list; `{}` before it was opened. */
export function getLastListQuery(): Record<string, string> {
  return { ...lastListQuery };
}

/** What a `#/model-calls/<sub>` path shows. `notfound` covers an unknown path and a malformed `%` escape. */
export type ModelCallsView =
  | { view: 'list'; callId: string | null }
  | { view: 'cache' }
  | { view: 'notfound' };

/** The view of a Model calls sub path (still URL-encoded): `''` the list, `c/<call id>` the list with that call open, `cache`. */
export function parseModelCallsSub(sub: string): ModelCallsView {
  const segs = sub.split('/');
  while (segs.length > 0 && segs[segs.length - 1] === '') segs.pop();
  if (segs.length === 0) return { view: 'list', callId: null };
  if (segs.length === 1 && segs[0] === 'cache') return { view: 'cache' };
  if (segs.length === 2 && segs[0] === 'c') {
    const callId = safeDecode(segs[1]);
    return callId ? { view: 'list', callId } : { view: 'notfound' };
  }
  return { view: 'notfound' };
}

/** The Model calls filters of a hash query. `days` is the window, kept as the link gave it (1 to 30), else ''. */
export interface ModelCallFilters {
  purpose: string;
  model: string;
  session_id: string;
  turn_id: string;
  status: string;
  days: string;
}

export const MODEL_CALL_STATUSES = ['ok', 'error', 'cancelled', 'problem'] as const;

export function modelCallFilters(query: URLSearchParams): ModelCallFilters {
  const status = query.get('status') ?? '';
  const days = query.get('days') ?? '';
  const n = Number(days);
  return {
    purpose: query.get('purpose') ?? '',
    model: query.get('model') ?? '',
    session_id: query.get('session_id') ?? '',
    turn_id: query.get('turn_id') ?? '',
    status: (MODEL_CALL_STATUSES as readonly string[]).includes(status) ? status : '',
    days: /^\d+$/.test(days) && n >= 1 && n <= 30 ? String(n) : '',
  };
}

/** The non-empty filters as a query record: the list request's parameters and a link's query alike. */
export function modelCallQuery(f: ModelCallFilters): Record<string, string> {
  const q: Record<string, string> = {};
  for (const k of ['purpose', 'model', 'session_id', 'turn_id', 'status', 'days'] as const) {
    if (f[k]) q[k] = f[k];
  }
  return q;
}
