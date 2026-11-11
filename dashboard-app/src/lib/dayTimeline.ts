// The Overview's day timeline: today's turns by time and kind of work, one bar each, from GET /dashboard/work/today.
// Pure layout and wording; the component draws it. Not drawn, for want of data: sleep (it writes no turn rows) and
// deploy or flag-change lines (no event data).
import type { WorkTodayTurn } from './types/api';
import { routeHref } from './router';

export type RowId = 'owner' | 'heartbeat' | 'subtasks' | 'workflows';

export const ROWS: readonly { id: RowId; label: string; kinds: readonly string[] }[] = [
  { id: 'owner', label: 'Your conversations', kinds: ['conversation'] },
  { id: 'heartbeat', label: 'Heartbeat and background', kinds: ['heartbeat', 'background_check'] },
  { id: 'subtasks', label: 'Subtasks', kinds: ['subtask', 'intention'] },
  { id: 'workflows', label: 'Workflows', kinds: ['workflow_step'] },
];

/** The row of a turn's kind of work; an unknown kind goes to the heartbeat and background row. */
export function rowOf(kind: string | null | undefined): RowId {
  const row = ROWS.find((r) => kind != null && r.kinds.includes(kind));
  return row ? row.id : 'heartbeat';
}

/** The narrowest bar, in px: a turn of a few seconds still shows. */
export const MIN_BAR = 5;

/** Minutes after 00:00 UTC of a timestamp, seconds as a fraction; null when unreadable. */
export function utcMinute(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return null;
  const d = new Date(ms);
  return d.getUTCHours() * 60 + d.getUTCMinutes() + d.getUTCSeconds() / 60;
}

/** The minutes the axis spans: from 00:00 to the 3-hour mark at least an hour past now, 3 hours to the whole day. */
export function spanMinutes(nowMinute: number): number {
  return Math.min(1440, Math.max(180, Math.ceil((nowMinute + 60) / 180) * 180));
}

/** `HH:MM` of a minute of the day. */
export function hhmm(minute: number): string {
  const m = Math.max(0, Math.floor(minute));
  return `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
}

export interface Bar {
  id: string;
  row: RowId;
  /** px from the axis start, and the width (at least MIN_BAR). */
  x: number;
  w: number;
  frame: string | null;
  /** error wins over gated (the underline shows the red one). */
  status: 'error' | 'gated' | null;
  href: string;
  title: string;
  /** Minutes after 00:00 UTC of its start. */
  minute: number;
}

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

function title(minute: number, frame: string | null, calls: number, status: Bar['status']): string {
  const f = frame ? `${frame} frame` : 'No frame recorded';
  const s = status === 'error' ? ' An error.' : status === 'gated' ? ' A gated call.' : '';
  return `${hhmm(minute)} UTC. ${f}. ${plural(calls, 'tool call', 'tool calls')}.${s}`;
}

/**
 * The bars of each row over an axis `width` px wide spanning `spanMinutes` of now's UTC day. A turn's bar is as
 * wide as it ran (to now, if it has not ended), at least MIN_BAR; turns that start after now, or before the day, or
 * with an unreadable start are left out.
 */
export function layout(turns: readonly WorkTodayTurn[], nowMs: number, width: number): Record<RowId, Bar[]> {
  const now = new Date(nowMs);
  const dayStart = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  const span = spanMinutes((nowMs - dayStart) / 60000);
  const px = (minutes: number) => (minutes / span) * width;
  const rows: Record<RowId, Bar[]> = { owner: [], heartbeat: [], subtasks: [], workflows: [] };
  for (const t of turns) {
    const start = Date.parse(t.started_at);
    if (Number.isNaN(start) || start > nowMs || start < dayStart) continue;
    const endRaw = t.ended_at ? Date.parse(t.ended_at) : NaN;
    const end = Number.isNaN(endRaw) ? start : Math.min(Math.max(endRaw, start), nowMs);
    const minute = (start - dayStart) / 60000;
    const status = t.error ? 'error' : t.gated ? 'gated' : null;
    const row = rowOf(t.work_kind);
    rows[row].push({
      id: t.id,
      row,
      x: px(minute),
      w: Math.max(MIN_BAR, px((end - start) / 60000)),
      frame: t.frame_id,
      status,
      href: routeHref('work', ['s', t.session_id, t.id]),
      title: title(minute, t.frame_id, t.tool_calls, status),
      minute,
    });
  }
  return rows;
}

export interface DaySummary {
  turns: number;
  yours: number;
  background: number;
  errors: number;
  gated: number;
}

/** The turns that started up to and including `minute` (a turn at 09:14:30 counts at 09:14). */
export function summaryAt(turns: readonly WorkTodayTurn[], minute: number): DaySummary {
  const s: DaySummary = { turns: 0, yours: 0, background: 0, errors: 0, gated: 0 };
  for (const t of turns) {
    const m = utcMinute(t.started_at);
    if (m === null || Math.floor(m) > minute) continue;
    s.turns += 1;
    if (rowOf(t.work_kind) === 'owner') s.yours += 1;
    else s.background += 1;
    if (t.error) s.errors += 1;
    if (t.gated) s.gated += 1;
  }
  return s;
}

/** `09:14 UTC. 3 turns so far: 1 yours, 2 background; 2 errors, 1 gated.` Derived from the data, never authored. */
export function sentence(minute: number, s: DaySummary, truncated: boolean): string {
  const n = truncated ? `${s.turns} of the turns shown` : plural(s.turns, 'turn', 'turns');
  return `${hhmm(minute)} UTC. ${n} so far: ${s.yours} yours, ${s.background} background; ${plural(s.errors, 'error', 'errors')}, ${s.gated} gated.`;
}
