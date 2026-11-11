import { describe, it, expect } from 'vitest';
import { MIN_BAR, ROWS, layout, rowOf, sentence, spanMinutes, summaryAt, utcMinute } from './dayTimeline';
import type { WorkTodayTurn } from './types/api';

const NOW = Date.parse('2026-11-10T09:40:00Z');
const t = (id: string, hhmm: string, over: Partial<WorkTodayTurn> = {}): WorkTodayTurn => ({
  id, session_id: 'sess-1', started_at: `2026-11-10T${hhmm}:00+00:00`, ended_at: `2026-11-10T${hhmm}:05+00:00`,
  work_kind: 'conversation', frame_id: 'question', error: false, gated: false, tool_calls: 2, ...over,
});

describe('rows', () => {
  it('four rows, in order', () => {
    expect(ROWS.map((r) => r.label)).toEqual(['Your conversations', 'Heartbeat and background', 'Subtasks', 'Workflows']);
  });

  it.each([
    ['conversation', 'owner'],
    ['heartbeat', 'heartbeat'],
    ['background_check', 'heartbeat'],
    ['subtask', 'subtasks'],
    ['intention', 'subtasks'],
    ['workflow_step', 'workflows'],
    ['something_new', 'heartbeat'],
    ['', 'heartbeat'],
    ['toString', 'heartbeat'],
  ] as const)('%s goes to %s', (kind, row) => {
    expect(rowOf(kind)).toBe(row);
  });
});

describe('span and minutes', () => {
  it('the span reaches past now to the next 3-hour mark, at least 3 hours, at most the day', () => {
    expect(spanMinutes(580)).toBe(720);    // 09:40 -> 12:00
    expect(spanMinutes(10)).toBe(180);
    expect(spanMinutes(1410)).toBe(1440);
    expect(spanMinutes(0)).toBe(180);
  });

  it('minute of the UTC day, from any offset; NaN-safe', () => {
    expect(utcMinute('2026-11-10T09:14:30Z')).toBe(554.5);
    expect(utcMinute('2026-11-10T10:14:30+01:00')).toBe(554.5);
    expect(utcMinute('nonsense')).toBeNull();
  });
});

describe('layout', () => {
  it('one bar per turn in its row, at its minute, at least 5 wide', () => {
    const bars = layout([t('a', '09:12'), t('b', '04:00', { work_kind: 'heartbeat' }), t('c', '06:00', { work_kind: 'workflow_step' })], NOW, 720);
    expect(bars.owner.map((b) => b.id)).toEqual(['a']);
    expect(bars.heartbeat.map((b) => b.id)).toEqual(['b']);
    expect(bars.workflows.map((b) => b.id)).toEqual(['c']);
    expect(bars.subtasks).toEqual([]);
    expect(bars.owner[0].x).toBeCloseTo(552, 5);   // 552 minutes of a 720-minute span over 720 px
    expect(bars.owner[0].minute).toBe(552);
    for (const b of [...bars.owner, ...bars.heartbeat, ...bars.workflows]) expect(b.w).toBeGreaterThanOrEqual(MIN_BAR);
  });

  it('a long turn is as wide as it ran; a turn with no end still gets a bar', () => {
    const bars = layout([
      t('long', '08:00', { ended_at: '2026-11-10T08:30:00+00:00' }),
      t('open', '09:00', { ended_at: null }),
    ], NOW, 720);
    expect(bars.owner.find((b) => b.id === 'long')!.w).toBeCloseTo(30, 5);
    expect(bars.owner.find((b) => b.id === 'open')!.w).toBe(MIN_BAR);
  });

  it('error wins over gated; a clean turn has no status', () => {
    const bars = layout([
      t('e', '01:00', { error: true }), t('g', '02:00', { gated: true }), t('eg', '03:00', { error: true, gated: true }), t('ok', '04:00'),
    ], NOW, 720);
    expect(bars.owner.map((b) => [b.id, b.status])).toEqual([['e', 'error'], ['g', 'gated'], ['eg', 'error'], ['ok', null]]);
  });

  it('turns after now, or unreadable, are left out', () => {
    const bars = layout([t('late', '09:41'), t('bad', '09:00', { started_at: 'nonsense' }), t('ok', '09:39')], NOW, 720);
    expect(bars.owner.map((b) => b.id)).toEqual(['ok']);
  });

  it('the link opens the turn in the trace, ids URL-encoded', () => {
    const bars = layout([t('turn/1', '09:00', { session_id: 'sess/one two' })], NOW, 720);
    expect(bars.owner[0].href).toBe('#/work/s/sess%2Fone%20two/turn%2F1');
  });

  it('the title says the time, the frame, the calls and an error or a gated call', () => {
    const bars = layout([
      t('a', '09:12', { error: true, tool_calls: 3 }),
      t('b', '09:13', { gated: true, tool_calls: 1, frame_id: null }),
      t('c', '09:14', { tool_calls: 0, frame_id: 'task' }),
    ], NOW, 720);
    expect(bars.owner.map((b) => b.title)).toEqual([
      '09:12 UTC. question frame. 3 tool calls. An error.',
      '09:13 UTC. No frame recorded. 1 tool call. A gated call.',
      '09:14 UTC. task frame. 0 tool calls.',
    ]);
  });
});

describe('summary at a minute', () => {
  const turns = [
    t('a', '09:14', { started_at: '2026-11-10T09:14:30+00:00' }),
    t('b', '04:00', { work_kind: 'heartbeat', error: true }),
    t('c', '05:00', { work_kind: 'subtask', gated: true, error: true }),
    t('d', '09:30'),
  ];

  it('counts every turn up to and including that minute', () => {
    expect(summaryAt(turns, 554)).toEqual({ turns: 3, yours: 1, background: 2, errors: 2, gated: 1 });
    expect(summaryAt(turns, 553)).toEqual({ turns: 2, yours: 0, background: 2, errors: 2, gated: 1 });
    expect(summaryAt(turns, 0)).toEqual({ turns: 0, yours: 0, background: 0, errors: 0, gated: 0 });
  });

  it('the sentence', () => {
    expect(sentence(554, summaryAt(turns, 554), false)).toBe('09:14 UTC. 3 turns so far: 1 yours, 2 background; 2 errors, 1 gated.');
    expect(sentence(240, summaryAt(turns, 240), false)).toBe('04:00 UTC. 1 turn so far: 0 yours, 1 background; 1 error, 0 gated.');
    expect(sentence(240, summaryAt(turns, 240), true)).toBe('04:00 UTC. 1 of the turns shown so far: 0 yours, 1 background; 1 error, 0 gated.');
  });
});
