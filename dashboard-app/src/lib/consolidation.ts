// The Consolidation screen's reading of a sleep: its numbers, its sentence, its state, and its change rows turned into
// the sections of a report. Pure: no DOM, no fetch (consolidation.test.ts).
//
// A cycle's `totals` is the sleep handler's whole stats dict: counters this sleep changed, whole-graph snapshots
// (`f044_n_*`), nested objects, a boolean, and `_attempted`, which sums all of them. So nothing here walks it: every
// number is read by name from the counters the handler audits as changes (`count_keys` in nous/handlers/sleep_handler.py),
// and the rest stays in the raw record.
import { fmtDay, fmtDayMonth } from './harness';
import type { ConsolidationAction, ConsolidationCycle } from './types/api';

type Totals = Record<string, unknown>;

/** A counter in totals: its number, or 0 when it is missing or not a finite number (a boolean, an object). */
function num(totals: Totals, key: string): number {
  const v = totals[key];
  return typeof v === 'number' && Number.isFinite(v) ? v : 0;
}

const sum = (totals: Totals, keys: readonly string[]) => keys.reduce((t, k) => t + num(totals, k), 0);

/** The counters of links a sleep added to the graph. */
export const LINKS_ADDED = ['orphan_edges_created', 'temporal_chain_edges', 'comention_edges', 'cooccurrence_edges',
  'bridge_edges_created', 'episode_relink_edges'] as const;
/** The counters of links a sleep deleted from the graph. */
export const LINKS_PRUNED = ['dead_edges_pruned', 'dead_edges_missing_endpoint', 'comention_edges_deleted',
  'cooccurrence_edges_deleted'] as const;

/** What one sleep changed, in the numbers its row and the chart show. */
export interface SleepNumbers {
  /** Fact rows written: learned facts and the results of merges and updates. */
  newFacts: number;
  merges: number;
  retired: number;
  contradictionsResolved: number;
  /** Facts superseded by the key-conflict sweep, which writes no change row: this counter is its only record. */
  keySupersessions: number;
  procedures: number;
  censorsRetired: number;
  linksAdded: number;
  linksPruned: number;
}

/** How many fact changes the totals count: more than none means the sleep changed facts, whatever rows are left to read. */
export function factChanges(n: SleepNumbers): number {
  return n.newFacts + n.merges + n.retired + n.contradictionsResolved + n.keySupersessions;
}

export function sleepNumbers(cycle: ConsolidationCycle): SleepNumbers {
  const t: Totals = cycle.totals ?? {};
  return {
    newFacts: num(t, 'facts_created'),
    merges: num(t, 'clusters_merged'),
    retired: num(t, 'stale_deactivated'),
    contradictionsResolved: num(t, 'contradictions_resolved'),
    keySupersessions: num(t, 'key_supersessions_written'),
    procedures: num(t, 'procedures_created'),
    censorsRetired: num(t, 'censors_retired'),
    linksAdded: sum(t, LINKS_ADDED),
    linksPruned: sum(t, LINKS_PRUNED),
  };
}

/** "1 merge", "4 merges", "1,492 links added". */
function count(n: number, one: string, many: string): string {
  return `${n.toLocaleString('en-US')} ${n === 1 ? one : many}`;
}

/** The fact changes of a sleep in one line: "4 new facts · 4 merges · 15 retired as stale"; "No fact changes" for none. */
export function factSentence(n: SleepNumbers): string {
  const parts = [
    n.newFacts && count(n.newFacts, 'new fact', 'new facts'),
    n.merges && count(n.merges, 'merge', 'merges'),
    n.retired && `${n.retired.toLocaleString('en-US')} retired as stale`,
    n.contradictionsResolved && count(n.contradictionsResolved, 'contradiction resolved', 'contradictions resolved'),
    n.keySupersessions && `${n.keySupersessions.toLocaleString('en-US')} superseded by key`,
    n.procedures && count(n.procedures, 'new procedure', 'new procedures'),
    n.censorsRetired && count(n.censorsRetired, 'censor retired', 'censors retired'),
  ].filter(Boolean);
  return parts.length ? parts.join(' · ') : 'No fact changes';
}

/**
 * A sleep's row in words. The totals are written when a sleep ends, so a sleep with no finish has none: its row says
 * that instead of "No fact changes", which would read as a finished sleep that changed nothing.
 */
export function sleepSentence(row: SleepRow): string {
  if (row.state === 'finished' || Object.keys(row.cycle.totals ?? {}).length > 0) return factSentence(row.numbers);
  return row.state === 'running' ? 'Totals are written when the sleep ends' : 'No totals recorded';
}

/** The graph changes of a sleep: "625 links added · 200 pruned"; '' when the graph was left as it was. */
export function graphSentence(n: SleepNumbers): string {
  const parts = [
    n.linksAdded && count(n.linksAdded, 'link added', 'links added'),
    n.linksPruned && `${n.linksPruned.toLocaleString('en-US')} pruned`,
  ].filter(Boolean);
  return parts.join(' · ');
}

// ── State and duration ──────────────────────────────────────────────────────

/** finished and failed are the recorded statuses. A sleep with no finish is `running` while it is the newest one, and
 * `unfinished` once a later sleep has started: one sleep runs at a time, so it can no longer be running. */
export type SleepState = 'finished' | 'failed' | 'running' | 'unfinished';

export function sleepState(cycle: ConsolidationCycle, hasLater: boolean): SleepState {
  if (cycle.status === 'completed') return 'finished';
  if (cycle.status === 'failed') return 'failed';
  return hasLater ? 'unfinished' : 'running';
}

export const STATE_WORD: Record<SleepState, string> = {
  finished: 'Finished',
  failed: 'Failed',
  running: 'Running',
  unfinished: 'No finish recorded',
};

/** Minutes from start to finish; null when either end is missing or unreadable. */
export function sleepMinutes(cycle: ConsolidationCycle): number | null {
  if (!cycle.started_at || !cycle.finished_at) return null;
  const ms = new Date(cycle.finished_at).getTime() - new Date(cycle.started_at).getTime();
  return Number.isFinite(ms) && ms >= 0 ? ms / 60000 : null;
}

/** "under 1 min", "17 min", "1 h 05 min"; '' when there is no duration. */
export function fmtMinutes(minutes: number | null): string {
  if (minutes == null) return '';
  const m = Math.round(minutes);
  if (m < 1) return 'under 1 min';
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, '0')} min`;
}

// ── The list, by day ────────────────────────────────────────────────────────

export interface SleepRow {
  cycle: ConsolidationCycle;
  state: SleepState;
  numbers: SleepNumbers;
  minutes: number | null;
}

/** The cycles as rows, in the order given (newest first, as the endpoint returns them). */
export function sleepRows(cycles: ConsolidationCycle[]): SleepRow[] {
  return cycles.map((cycle, i) => ({
    cycle,
    state: sleepState(cycle, i > 0),
    numbers: sleepNumbers(cycle),
    minutes: sleepMinutes(cycle),
  }));
}

/** The UTC day of a timestamp as a key ("2026-11-13"); '' when there is none. */
function dayKey(iso: string | null): string {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : d.toISOString().slice(0, 10);
}

/** Rows grouped under their UTC day ("13 November"), keeping the order given. */
export function byDay(rows: SleepRow[]): { key: string; day: string; rows: SleepRow[] }[] {
  const groups: { key: string; day: string; rows: SleepRow[] }[] = [];
  for (const row of rows) {
    const key = dayKey(row.cycle.started_at);
    const last = groups[groups.length - 1];
    if (last && last.key === key) last.rows.push(row);
    else groups.push({ key, day: key ? fmtDayMonth(row.cycle.started_at) : 'No start time recorded', rows: [row] });
  }
  return groups;
}

/** The sleep the page opens on: the newest finished one (a running sleep has little to read yet). */
export function defaultOpen(rows: SleepRow[]): string | null {
  return rows.find((r) => r.state === 'finished')?.cycle.cycle_id ?? null;
}

// ── The chart ───────────────────────────────────────────────────────────────

/** One side of a measure: its words, its colour token, and its value for a sleep. */
export interface Series { label: string; token: string; value: (row: SleepRow) => number }
/** A chart measure: what was gained above the line and, for the two that have one, what was removed below it. */
export interface Measure { key: string; label: string; up: Series; down: Series | null }

export const MEASURES: Measure[] = [
  { key: 'facts', label: 'Facts',
    up: { label: 'New facts', token: '--series-2', value: (r) => r.numbers.newFacts },
    down: { label: 'Retired as stale', token: '--series-7', value: (r) => r.numbers.retired } },
  { key: 'graph', label: 'Graph',
    up: { label: 'Links added', token: '--series-9', value: (r) => r.numbers.linksAdded },
    down: { label: 'Links pruned', token: '--series-6', value: (r) => r.numbers.linksPruned } },
  { key: 'duration', label: 'Duration',
    up: { label: 'Minutes', token: '--series-0', value: (r) => Math.round(r.minutes ?? 0) },
    down: null },
];

export interface ChartBar { id: string; row: SleepRow; up: number; down: number; /** "Nov 13" under the bar, or ''. */ tick: string }
export interface SleepChart { bars: ChartBar[]; maxUp: number; maxDown: number }

/** A day label ("Nov 13") runs to the right of its bar, so it needs room: this share of the plot's width between two
 * labels and to the plot's right edge (at phone width the plot is about 290px and a label about 42px). A day whose
 * first sleep has no such room goes unlabelled; the readout above the plot names any bar's time. */
const TICK_ROOM = 0.16;

/** The chart of a measure: one bar per sleep, oldest first, with a day label under the first sleep of a day where it fits. */
export function sleepChart(rows: SleepRow[], measure: Measure): SleepChart {
  const ordered = [...rows].reverse();
  const room = ordered.length * TICK_ROOM;
  let seen = '';
  let lastTick = -Infinity;
  const bars = ordered.map((row, i) => {
    const key = dayKey(row.cycle.started_at);
    const first = key !== seen;
    seen = key;
    const fits = first && key !== '' && i - lastTick >= room && ordered.length - i >= room;
    if (fits) lastTick = i;
    return {
      id: row.cycle.cycle_id,
      row,
      up: measure.up.value(row),
      down: measure.down ? measure.down.value(row) : 0,
      tick: fits ? fmtDay(row.cycle.started_at) : '',
    };
  });
  return { bars, maxUp: Math.max(0, ...bars.map((b) => b.up)), maxDown: Math.max(0, ...bars.map((b) => b.down)) };
}

/** What a bar says: "Links added 625 · Links pruned 200" (the label first, so one and many read alike). */
export function barReadout(bar: ChartBar, measure: Measure): string {
  const side = (n: number, s: Series) => `${s.label} ${n.toLocaleString('en-US')}`;
  return [side(bar.up, measure.up), measure.down && side(bar.down, measure.down)].filter(Boolean).join(' · ');
}

// ── The report of one sleep ─────────────────────────────────────────────────

/** The audit keeps the first 200 characters of a fact's text (`_PREVIEW_CHARS`, nous/handlers/consolidation_audit.py). */
export const PREVIEW_CHARS = 200;

/** One change, as the report reads it. */
export interface ReportItem {
  id: string;
  /** What happened or what it is about: a fact's subject, "3 facts became one". */
  title: string | null;
  /** The text this change wrote or, for a retired fact, the text that was retired. */
  text: string | null;
  /** The texts this change replaced: a merge's sources, an update's old text. */
  earlier: string[];
  /** The recorded rationale. */
  why: string | null;
  /** The ids of the rows it touched, shortened. */
  ids: string[];
  /** The recorded before and after, for a change whose shape this screen does not know. */
  raw: { before: unknown; after: unknown } | null;
}

export interface ReportSection {
  key: string;
  title: string;
  /** The phase and operation names the rows carry ("f027_consolidate · merge"). */
  source: string;
  /** One line under the heading: a rationale every row shares, or what the record does not keep. */
  note: string | null;
  earlierLabel: string;
  items: ReportItem[];
  /** True when a text in this section is cut at the audit's preview length. */
  cut: boolean;
}

const isObject = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === 'object' && !Array.isArray(v);
const str = (v: unknown): string | null => (typeof v === 'string' && v.trim() ? v : null);

/** A preview as shown: an ellipsis marks one the audit cut. */
function shown(text: string | null): string | null {
  return text && text.length >= PREVIEW_CHARS ? `${text}…` : text;
}

const shortId = (id: string) => (id.length > 8 ? id.slice(0, 8) : id);

type Read = Pick<ReportItem, 'title' | 'text' | 'earlier'> | null;
interface SectionDef {
  key: string; title: string; earlierLabel: string; note?: string;
  /** The item's reading, or null when the row is not in the shape this section knows. */
  read: (a: ConsolidationAction) => Read;
}

const preview = (v: unknown) => (isObject(v) ? str(v.content_preview) : null);

const LEARNED: SectionDef = {
  key: 'learned', title: 'Learned', earlierLabel: '',
  read: (a) => (preview(a.after) ? { title: isObject(a.after) ? str(a.after.subject) : null, text: preview(a.after), earlier: [] } : null),
};
const UPDATED: SectionDef = {
  key: 'updated', title: 'Updated', earlierLabel: 'Was',
  read: (a) => {
    const [was, now] = [preview(a.before), preview(a.after)];
    return was && now ? { title: 'One fact replaced an older one', text: now, earlier: [was] } : null;
  },
};
const MERGED: SectionDef = {
  key: 'merged', title: 'Merged', earlierLabel: 'From',
  read: (a) => {
    const sources = Array.isArray(a.before) ? a.before.map(preview).filter((t): t is string => t !== null) : [];
    const text = preview(a.after);
    return text && sources.length ? { title: `${sources.length} facts became one`, text, earlier: sources } : null;
  },
};
const CONTRADICTION_TITLE: Record<string, string> = {
  merge: 'Two facts merged into one',
  supersede: 'One fact superseded the other',
  deactivate: 'One fact deactivated',
};
const CONTRADICTIONS: SectionDef = {
  key: 'contradictions', title: 'Contradictions resolved', earlierLabel: '',
  note: 'The audit keeps the ids of the two facts, not their text.',
  read: (a) => (CONTRADICTION_TITLE[a.op] ? { title: CONTRADICTION_TITLE[a.op], text: preview(a.after), earlier: [] } : null),
};
const RETIRED: SectionDef = {
  key: 'retired', title: 'Retired as stale', earlierLabel: '',
  read: (a) => (preview(a.before) ? { title: isObject(a.before) ? str(a.before.subject) : null, text: preview(a.before), earlier: [] } : null),
};
const OTHER: SectionDef = { key: 'other', title: 'Other changes', earlierLabel: '', read: () => null };

/** Which section a row belongs to, by its phase and operation; the order here is the report's reading order. */
const SECTIONS: { phase: string; op: string | null; def: SectionDef }[] = [
  { phase: 'reflect', op: 'learn', def: LEARNED },
  { phase: 'reflect', op: 'supersede', def: UPDATED },
  { phase: 'f027_consolidate', op: 'merge', def: MERGED },
  { phase: 'f031_contradiction', op: null, def: CONTRADICTIONS },
  { phase: 'stale_scan', op: 'deactivate', def: RETIRED },
];
const ORDER = [...SECTIONS.map((s) => s.def.key), OTHER.key];

/** A phase's summary row: `after` is `{ counts: {...} }`, the same numbers the cycle's totals carry once they are written. */
const isSummary = (a: ConsolidationAction) => isObject(a.after) && isObject(a.after.counts);

/**
 * A sleep's change rows as the sections of its report, in reading order. A phase summary row is left out here: it is
 * a count, not a change to read, and `upkeepCounts` shows its numbers (from the cycle's totals, or from the row
 * itself where the totals lack them). A row in a shape no section knows goes to "Other changes" with its recorded
 * before and after, so nothing recorded is hidden.
 */
export function reportSections(actions: ConsolidationAction[]): ReportSection[] {
  const byKey = new Map<string, ReportSection>();
  for (const a of actions) {
    if (isSummary(a)) continue;
    const match = SECTIONS.find((s) => s.phase === a.phase && (s.op === null || s.op === a.op));
    const read = match?.def.read(a) ?? null;
    const def = read ? match!.def : OTHER;
    let section = byKey.get(def.key);
    if (!section) {
      section = { key: def.key, title: def.title, source: '', note: def.note ?? null, earlierLabel: def.earlierLabel, items: [], cut: false };
      byKey.set(def.key, section);
    }
    const texts = read ? [read.text, ...read.earlier] : [];
    if (texts.some((t) => t !== null && t.length >= PREVIEW_CHARS)) section.cut = true;
    section.items.push({
      id: a.action_id,
      title: read ? read.title : `${a.phase} · ${a.op}`,
      text: read ? shown(read.text) : null,
      earlier: read ? read.earlier.map((t) => shown(t)!) : [],
      why: str(a.rationale),
      ids: (a.target_ids ?? []).map(shortId),
      raw: read || (a.before == null && a.after == null) ? null : { before: a.before, after: a.after },
    });
    const source = `${a.phase} · ${a.op}`;
    if (!section.source.split(', ').includes(source)) section.source = section.source ? `${section.source}, ${source}` : source;
  }
  const sections = ORDER.map((k) => byKey.get(k)).filter((s): s is ReportSection => s !== undefined);
  // A rationale every row of a section shares is said once, under the heading.
  for (const s of sections) {
    const why = s.items[0]?.why ?? null;
    if (why && s.items.length > 1 && s.items.every((i) => i.why === why)) {
      s.note = s.note ? `${s.note} ${why}` : why;
      for (const i of s.items) i.why = null;
    }
  }
  return sections;
}

// ── Upkeep counts (from the cycle's totals) ─────────────────────────────────

export interface CountRow { key: string; label: string; value: string }

/** The counters each list shows, in order, with their plain words. A list shows the ones a sleep recorded as non-zero. */
const GRAPH_COUNTS: [string, string][] = [
  ['orphan_edges_created', 'Links for memories that had none'],
  ['temporal_chain_edges', 'Temporal chain links'],
  ['comention_edges', 'Co-mention links'],
  ['cooccurrence_edges', 'Co-occurrence links'],
  ['bridge_edges_created', 'Bridge links between clusters'],
  ['episode_relink_edges', 'Episode links added'],
  ['dead_edges_pruned', 'Links to inactive memories pruned'],
  ['dead_edges_missing_endpoint', 'Links to missing rows pruned'],
  ['comention_edges_deleted', 'Co-mention links deleted'],
  ['cooccurrence_edges_deleted', 'Co-occurrence links deleted'],
  ['comention_edges_reweighted', 'Co-mention links reweighted'],
  ['cooccurrence_edges_reweighted', 'Co-occurrence links reweighted'],
  ['f044_promoted', 'Links promoted to consolidated'],
  ['f044_recall_touches_flushed', 'Recall touches written to links'],
  ['f044_downscaled', 'Link weights downscaled'],
];
const HOUSEKEEPING_COUNTS: [string, string][] = [
  ['stale_examined', 'Facts examined for staleness'],
  ['contradictions_found', 'Contradictions found'],
  ['contradictions_resolved', 'Contradictions resolved'],
  ['key_conflicts_found', 'Key conflicts found'],
  ['key_supersessions_written', 'Key supersessions written'],
  ['episodes_recovered', 'Abandoned episodes recovered'],
  ['episodes_recovery_closed', 'Recovered episodes closed'],
  ['episodes_marked_abandoned', 'Episodes marked abandoned'],
  ['episodes_relinked', 'Episodes relinked'],
  ['procedures_created', 'Procedures created'],
  ['censors_retired', 'Censors retired'],
  ['profile_card_rewritten', 'Profile card rewritten'],
  ['rubric_evolved', 'Rubric evolved'],
];

function countRows(totals: Totals, defs: [string, string][]): CountRow[] {
  const rows: CountRow[] = [];
  for (const [key, label] of defs) {
    const v = totals[key];
    if (v === true) rows.push({ key, label, value: 'yes' });
    else if (typeof v === 'number' && Number.isFinite(v) && v !== 0) rows.push({ key, label, value: v.toLocaleString('en-US') });
  }
  return rows;
}

/** The counters a sleep's phase summary rows carry, each summed over its rows (a flag stays true). */
function summaryCounts(actions: ConsolidationAction[]): Totals {
  const out: Totals = {};
  for (const a of actions) {
    if (!isObject(a.after) || !isObject(a.after.counts)) continue;
    for (const [key, v] of Object.entries(a.after.counts)) {
      const sofar = out[key];
      if (v === true) out[key] = true;
      else if (typeof v === 'number' && Number.isFinite(v)) out[key] = (typeof sofar === 'number' ? sofar : 0) + v;
    }
  }
  return out;
}

/**
 * The graph and housekeeping counters a sleep recorded as non-zero, by name, with their plain words. The cycle's
 * totals are the source. They are written when a sleep ends, so a sleep with no finish (or one whose closing write
 * failed) has none: there the phase summary rows, which each phase writes as it ends, fill in what the totals lack.
 * A counter a summary row carries that neither list names is shown under its own name, so no recorded count is hidden.
 */
export function upkeepCounts(cycle: ConsolidationCycle, actions: ConsolidationAction[] = []): { graph: CountRow[]; housekeeping: CountRow[] } {
  const fromRows = summaryCounts(actions);
  const t: Totals = { ...fromRows, ...(cycle.totals ?? {}) };
  const named = new Set([...GRAPH_COUNTS, ...HOUSEKEEPING_COUNTS].map(([key]) => key));
  const unnamed: [string, string][] = Object.keys(fromRows).filter((key) => !named.has(key)).map((key) => [key, key.replaceAll('_', ' ')]);
  return { graph: countRows(t, GRAPH_COUNTS), housekeeping: [...countRows(t, HOUSEKEEPING_COUNTS), ...countRows(fromRows, unnamed)] };
}
