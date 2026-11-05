import { describe, it, expect } from 'vitest';
import { dayRows, fmtValue, ruleTiles, stateTone } from './memoryWriteHealth';
import type { MwhCheck, MwhDayMetrics } from './types/api';

const check = (over: Partial<MwhCheck>): MwhCheck => ({
  id: 'H1', name: 'hard_delete', state: 'ok', value: 0, threshold: 1, urgency: 'high', summary: '', detail: {},
  acknowledged: null, ...over,
});

describe('memory write health tiles', () => {
  it('gives one tile per rule in rule order, no data for a rule not evaluated', () => {
    const tiles = ruleTiles([check({ id: 'A1', state: 'ok', value: 0.1 })]);
    expect(tiles.map((t) => t.id)).toEqual(['H1', 'F1', 'S1', 'P1', 'R1', 'A1']);
    expect(tiles[0].state).toBe('no_data');
    expect(tiles[5].state).toBe('ok');
  });

  it('folds several F1 sources into one tile at the worst state with every tripped summary', () => {
    const tiles = ruleTiles([
      check({ id: 'F1', state: 'tripped', summary: 'fact flood from source user_direct', value: 400, threshold: 360 }),
      check({ id: 'F1', state: 'tripped', summary: 'fact flood from source other', value: 120, threshold: 100 }),
    ]);
    const f1 = tiles.find((t) => t.id === 'F1')!;
    expect(f1.state).toBe('tripped');
    expect(f1.summaries).toEqual(['fact flood from source user_direct', 'fact flood from source other']);
  });

  it('maps states to tones', () => {
    expect(stateTone('tripped')).toBe('bad');
    expect(stateTone('acknowledged')).toBe('warn');
    expect(stateTone('ok')).toBe('ok');
    expect(stateTone('no_data')).toBe('muted');
  });

  it('shows A1 as a percentage and a dash for no value', () => {
    expect(fmtValue('A1', 0.756)).toBe('76%');
    expect(fmtValue('P1', 5.6)).toBe('5.6');
    expect(fmtValue('H1', 3)).toBe('3');
    expect(fmtValue('H1', null)).toBe('—');
  });
});

describe('memory write health daily rows', () => {
  const metrics = (over: Partial<MwhDayMetrics> = {}): MwhDayMetrics => ({
    facts_by_source: { user_direct: 3, other: 2 }, max_facts_one_episode: 2,
    tier1_inflow: { person: { all: 2, non_enum: 1 }, preference: { all: 0, non_enum: 0 }, rule: { all: 1, non_enum: 1 } },
    deactivated: { stale: 1, superseded: 2, merged: 3, removed: 0, user: 0, rollback: 0, unknown: 0 },
    deactivated_tier1_stale: 1, admission_rejected: 4, episodes_started: 2, episodes_ended: 1,
    sleep_missing_endpoint: 0, profile_card_rewrites: 1, ...over,
  });

  it('lists the newest day first with its totals', () => {
    const rows = dayRows([{ day: '2026-11-04', metrics: metrics() }, { day: '2026-11-05', metrics: metrics({ facts_by_source: {} }) }]);
    expect(rows.map((r) => r.day)).toEqual(['2026-11-05', '2026-11-04']);
    expect(rows[1]).toMatchObject({ facts: 5, person: 1, rule: 1, deactivated: 6, merged: 5, tier1Stale: 1, rejected: 4 });
    expect(rows[0].facts).toBe(0);
  });
});
