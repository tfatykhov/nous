// Memory write health (P4): pure helpers for the view. Tiles, labels and the daily table rows.
import type { MwhCheck, MwhDayMetrics, MwhRuleId, MwhState } from './types/api';

export const RULE_ORDER: MwhRuleId[] = ['H1', 'F1', 'S1', 'P1', 'R1', 'A1'];

export const RULE_LABEL: Record<MwhRuleId, string> = {
  H1: 'Hard delete',
  F1: 'Source flood',
  S1: 'Tier-1 stale scan',
  P1: 'Profile learning',
  R1: 'Rule learning',
  A1: 'Admission rejects',
};

export const RULE_HINT: Record<MwhRuleId, string> = {
  H1: 'Fact or episode rows gone (ledger ids with no row, row totals that fell, dangling edges). Stays until acknowledged.',
  F1: "A source's facts in a day against its own 14-day median (today or yesterday).",
  S1: 'The stale scan deactivated a person, preference or rule fact (today or yesterday).',
  P1: 'Person and preference facts over 14 days against the four 14-day windows before.',
  R1: 'Rule facts over 14 days against the four 14-day windows before.',
  A1: 'Share of facts admission rejected over 3 days.',
};

export const STATE_LABEL: Record<MwhState, string> = {
  tripped: 'Tripped',
  acknowledged: 'Acknowledged',
  ok: 'OK',
  no_data: 'No data yet',
};

export type Tone = 'bad' | 'warn' | 'ok' | 'muted';

/** Tripped is bad; an acknowledged trip is a warning until its window has passed; no data is muted. */
export function stateTone(state: MwhState): Tone {
  return state === 'tripped' ? 'bad' : state === 'acknowledged' ? 'warn' : state === 'ok' ? 'ok' : 'muted';
}

const RANK: Record<MwhState, number> = { tripped: 3, acknowledged: 2, ok: 1, no_data: 0 };

export interface RuleTile {
  id: MwhRuleId;
  label: string;
  hint: string;
  state: MwhState;
  /** Every tripped summary (F1 can trip once per source). */
  summaries: string[];
  value: number | null;
  threshold: number | null;
  urgency: string;
  acknowledged: MwhCheck['acknowledged'];
}

/** One tile per rule, in rule order: F1's states fold into one, at the worst state. A rule the endpoint did not
 * evaluate shows as no data. */
export function ruleTiles(checks: MwhCheck[]): RuleTile[] {
  return RULE_ORDER.map((id) => {
    const mine = checks.filter((c) => c.id === id);
    const worst = mine.reduce<MwhCheck | null>((a, c) => (a === null || RANK[c.state] > RANK[a.state] ? c : a), null);
    return {
      id,
      label: RULE_LABEL[id],
      hint: RULE_HINT[id],
      state: worst?.state ?? 'no_data',
      summaries: mine.filter((c) => c.state === 'tripped' && c.summary).map((c) => c.summary),
      value: worst?.value ?? null,
      threshold: worst?.threshold ?? null,
      urgency: worst?.urgency ?? 'normal',
      acknowledged: worst?.acknowledged ?? null,
    };
  });
}

/** A number for a tile: rates as a percentage, others as they are; a dash for none. */
export function fmtValue(id: MwhRuleId, v: number | null): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  if (id === 'A1') return `${Math.round(v * 100)}%`;
  return Number.isInteger(v) ? String(v) : v.toFixed(1);
}

export const SOURCE_COLUMNS = [
  'enumerative_extractor', 'user_direct', 'reflection', 'sleep_reflection',
  'contradiction_resolution', 'cluster_consolidation', 'episode_summarizer', 'other',
] as const;

export interface DayRow {
  day: string;
  facts: number;
  bySource: Record<string, number>;
  person: number;
  preference: number;
  rule: number;
  deactivated: number;
  tier1Stale: number;
  merged: number;
  rejected: number;
  started: number;
  ended: number;
  cardRewrites: number;
}

const sum = (o: Record<string, number> | undefined) => Object.values(o ?? {}).reduce((a, b) => a + b, 0);

/** The daily table, newest first. Tier-1 columns count non-enumerative facts, the series P1 and R1 read. */
export function dayRows(days: { day: string; metrics: MwhDayMetrics }[]): DayRow[] {
  return [...days].reverse().map(({ day, metrics: m }) => ({
    day,
    facts: sum(m.facts_by_source),
    bySource: m.facts_by_source ?? {},
    person: m.tier1_inflow?.person?.non_enum ?? 0,
    preference: m.tier1_inflow?.preference?.non_enum ?? 0,
    rule: m.tier1_inflow?.rule?.non_enum ?? 0,
    deactivated: sum(m.deactivated),
    tier1Stale: m.deactivated_tier1_stale ?? 0,
    merged: (m.deactivated?.merged ?? 0) + (m.deactivated?.superseded ?? 0),
    rejected: m.admission_rejected ?? 0,
    started: m.episodes_started ?? 0,
    ended: m.episodes_ended ?? 0,
    cardRewrites: m.profile_card_rewrites ?? 0,
  }));
}
