// F108 Work and Model calls screens: tool lanes and the formatters they share.
import { fmtUtc } from './harness';

/**
 * A tool lane (what the tool touches) to its colour, from the mockups. The lane names are the endpoints'
 * (`TOOL_LANES` in nous/api/work_view_dashboard.py); any tool not in a lane is `other`, the neutral colour.
 */
export const LANE_COLORS: Record<string, string> = {
  memory: '#60a5fa',
  live: '#fbbf24',
  workspace: '#9ca3af',
  acts: '#f472b6',
  other: '#8e8eab',
};

/** The colour of a lane; the neutral `other` colour for any lane not listed. */
export function laneColor(lane: string): string {
  return Object.hasOwn(LANE_COLORS, lane) ? LANE_COLORS[lane] : LANE_COLORS.other;
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
