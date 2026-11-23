// F118: the panel "The outside judge" on the Autonomy screen: its types, its request and its words. The answer is that
// of GET /dashboard/outside-judge (nous/api/rest.py), which needs the owner's or the proxy's secret (the login proxy adds
// it, the dashboard sends none). Read-only. Every sentence the owner reads on the panel is here (pinned by
// outside_judge_numbers.test.ts); the helpers are pure and return text, which the panel binds as text.

import { apiRequest } from './api';
import { timed } from './goals';
import { fmtUtc } from './harness';
import { count, pct, tokens } from './autonomy';

// ── Answers ─────────────────────────────────────────────────────────────────

export interface GraderGroup { n: number; agree: number; judge_harsher: number; judge_kinder: number; cannot_tell: number; agree_rate: number | null }
export interface Cost { calls: number; tokens_in: number; tokens_out: number; tokens_per_verdict_avg: number | null; latency_ms_p50: number | null }
export interface DecisionNumbers {
  judged: number; by_verdict: Record<string, number>; skipped_by_label: Record<string, number>; errors: number;
  by_grader: Record<string, GraderGroup>;
}
export interface Disagreement {
  subject_id: string; title: string | null; against_grader: string | null; against_grade: string | null; verdict: string;
  confidence: number | null; labels: string[]; reason: string | null; judged_at: string;
}
export interface Runtime { queued: number; dropped: number; over_cap: number; worker_alive: boolean; second?: Runtime }
export interface Against { n: number; compared: number; agree: number; agree_rate: number | null; matrix: Record<string, Record<string, number>> }
export interface NotSupported {
  subject_id: string; title: string | null; confidence: number | null; labels: string[]; reason: string | null;
  finish_check: string | null; audit: string | null; judged_at: string;
}
export interface ReportNumbers {
  judge: string; version: string; judged: number; by_verdict: Record<string, number>; skipped_by_label: Record<string, number>;
  errors: number; vs_finish_check: Against; vs_audit: Against; cost: Cost; not_supported: NotSupported[];
}
export interface SweepRuntime {
  passes: number; queued_decisions: Record<string, number>; queued_reports: number; held_at_half_limit: number; failed: number;
  last_pass_at: string | null;
}
export interface Sweep { agreement: { outcome_grades: GraderGroup; stated_confidence: GraderGroup }; runtime: SweepRuntime | null }
export type TodayCost = Cost & { today: { calls: number; cap: number } };
/** The second judge's own numbers. Its own `disagreements` arrive too but are not read. */
export interface SecondNumbers { judge: string; version: string; model: string | null; decisions: DecisionNumbers; cost: TodayCost }
export interface PairDisagreement {
  subject_id: string; title: string | null; claim_at: string; against_grader: string | null; against_grade: string | null;
  first_verdict: string; second_verdict: string; first_confidence: number | null; second_confidence: number | null;
  first_labels: string[]; second_labels: string[]; evidence_differs: boolean;
}
export interface Between {
  pairs: number; agree: number; either_cannot_tell: number; agree_rate: number | null; matrix: Record<string, Record<string, number>>;
  evidence_differs: number; other_claim: number; disagreements: PairDisagreement[];
}
export interface JudgeData {
  enabled: true; mode: string; judge: string; version: string; model: string | null; days: number;
  decisions: DecisionNumbers; cost: TodayCost; disagreements: Disagreement[]; runtime: Runtime;
  /** Arrive together; absent when no second judge runs or its numbers could not be read. */
  second?: SecondNumbers; between_judges?: Between;
  /** Arrive together; absent when task reports are not judged or their numbers could not be read. */
  reports?: ReportNumbers; sweep?: Sweep;
}
export type JudgeAnswer = { enabled: false } | JudgeData;

// ── Request ─────────────────────────────────────────────────────────────────

export const readJudge = (days: string, signal?: AbortSignal) =>
  timed((sig) => apiRequest<JudgeAnswer>(`/dashboard/outside-judge?days=${days}`, 'GET', undefined, sig), signal);

/** What the server lists at most. */
export const DISAGREEMENTS_MAX = 50;
/** The list's first screen. */
export const SHOWN = 10;
/** The pairs of the two judges shown. */
export const PAIRS_SHOWN = 5;

// ── Words ───────────────────────────────────────────────────────────────────

export const TITLE = 'The outside judge';
export const INTRO = 'The outside judge only records its own verdicts beside the grades Nous already has, and changes no grade.';
export const LOAD_FAILED = "The outside judge's numbers could not be read. Use Refresh at the top of the page to try again.";
export const NOT_ENOUGH = 'Not enough graded yet';
export const NOT_ENOUGH_COMPARED = 'Not enough compared yet';
export const NOTHING_JUDGED = (days: number) => `No decision has been judged in the last ${days} days.`;
export const NOTHING_GRADED = 'No verdict has been compared with a grade yet.';
export const NO_DISAGREEMENTS = (days: number) => `The judge has not disagreed with a grade in the last ${days} days.`;
export const NO_REPORTS = (days: number) => `No task report has been judged in the last ${days} days.`;
export const NO_NOT_SUPPORTED = 'The judge found every judged report supported or could not tell.';
export const NOTHING_LEFT_OUT = 'Nothing was left unjudged.';
export const NO_SENTENCE = 'The judge gave no sentence.';
export const GRADER_NOTE = 'Agreement leaves out the verdicts that said “could not tell”.';
export const STRIP_HINT = 'Waiting is the queue now. Dropped and over the limit count since this server process started.';
export const SWEEP_INTRO =
  "Once an hour the judge picks up decisions and reports that have no final verdict yet. A pass queues nothing once today's calls reach half the daily limit.";
export const SWEEP_NO_NUMBERS = 'The hourly pass has no numbers in this server process.';
export const SWEEP_NONE_YET = 'No hourly pass has run yet in this server process.';
export const SHOW_FEWER = 'Show fewer';
export const NOT_SUPPORTED_HEADING = 'Latest reports the judge did not find supported';
export const AGAINST_FINISH = 'Agreement with the finish checks';
export const AGAINST_AUDIT = 'Agreement with your spot checks';
export const GRADER_COLUMNS = ['Graded by', 'Judged', 'Agree', 'Judge harsher', 'Judge kinder', 'Could not tell', 'Agreement'] as const;

/** A key the server sent, looked up in a table of words without reaching the object's prototype. */
const own = <T>(table: Record<string, T>, key: string): T | undefined => (Object.hasOwn(table, key) ? table[key] : undefined);
const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;
/** A rate as a share, or the words when there was nothing to divide (never 0% or NaN). */
export const rateOr = (rate: number | null, words: string) => (rate == null ? words : pct(rate));

export const statusTitle = (d: Pick<JudgeData, 'mode'>) =>
  d.mode === 'shadow' ? 'Shadow mode: it records verdicts and changes nothing' : `Mode: ${d.mode}`;
export const workerText = (alive: boolean) => (alive ? 'Running' : 'Not running');
export const scopeText = (d: Pick<JudgeData, 'judge' | 'version' | 'days'>) =>
  `These numbers cover only judge ${d.judge}, instructions ${d.version}, over the last ${d.days} days. A new instruction version starts them again from zero.`;

/** The status strip's labelled values, in order. */
export const stripRows = (d: JudgeData): [string, string][] => [
  ['Judge', d.judge],
  ['Instructions', d.version],
  ['Model', d.model ?? 'not named'],
  ['Worker', workerText(d.runtime.worker_alive)],
  ['Waiting', count(d.runtime.queued)],
  ['Dropped', count(d.runtime.dropped)],
  ['Over the daily limit', count(d.runtime.over_cap)],
];

// ── Tiles ───────────────────────────────────────────────────────────────────

/** One tile: `words` when the value is a phrase, not a number (set smaller). */
export interface Tile { label: string; value: string; sub: string; words: boolean }

/** The agreement with grades from outcomes (the owner's taps and the reviewer's outcome signals); null without `sweep`. */
export function outcomeTile(sweep: Sweep | undefined): Tile | null {
  if (!sweep) return null;
  const g = sweep.agreement.outcome_grades;
  const graded = g.n - g.cannot_tell;
  const label = 'Agreement with outcomes';
  if (g.agree_rate == null) return { label, value: NOT_ENOUGH, sub: `${graded} graded so far`, words: true };
  return {
    label, value: pct(g.agree_rate), words: false,
    sub: `${g.agree} of ${graded} graded verdicts match your taps and the reviewer's outcome signals`,
  };
}

/** The share of the judged decisions whose verdict was "could not tell". */
export function cannotTellTile(decisions: DecisionNumbers): Tile {
  const label = 'Could not tell';
  if (decisions.judged === 0) return { label, value: '—', sub: 'nothing judged yet', words: false };
  const cannot = own(decisions.by_verdict, 'cannot_tell') ?? 0;
  return { label, value: pct(cannot / decisions.judged), sub: `${cannot} of ${decisions.judged} verdicts`, words: false };
}

export function tiles(d: JudgeData): Tile[] {
  const outcome = outcomeTile(d.sweep);
  return [
    { label: 'Decisions judged', value: count(d.decisions.judged), sub: `in the last ${d.days} days`, words: false },
    ...(outcome ? [outcome] : []),
    cannotTellTile(d.decisions),
    {
      label: 'Calls today', value: `${count(d.cost.today.calls)} of ${count(d.cost.today.cap)}`, words: false,
      sub: 'today, every instructions version, decisions and reports',
    },
  ];
}

// ── Verdict bars ────────────────────────────────────────────────────────────

/** One part of a verdict bar: sized by `count`, painted with the token `token`. */
export interface Segment { key: string; label: string; count: number; share: string; token: string }

const VERDICTS: [string, string, string][] = [
  ['success', 'Success', '--green'], ['partial', 'Partial', '--warn-text'], ['failure', 'Failure', '--red'],
  ['cannot_tell', 'Could not tell', '--muted'],
];
const REPORT_VERDICTS: [string, string, string][] = [
  ['supported', 'Supported', '--green'], ['not_supported', 'Not supported', '--red'], ['cannot_tell', 'Could not tell', '--muted'],
];

function segments(by: Record<string, number>, known: [string, string, string][]): Segment[] {
  const keys = [...known.map(([k]) => k), ...Object.keys(by).filter((k) => !known.some(([w]) => w === k))];
  const present = keys.map((k) => [k, own(by, k) ?? 0] as const).filter(([, n]) => n > 0);
  const total = present.reduce((sum, [, n]) => sum + n, 0);
  return present.map(([key, n]) => {
    const words = known.find(([k]) => k === key);
    return { key, label: words?.[1] ?? key, count: n, share: pct(n / total), token: words?.[2] ?? '--muted' };
  });
}
/** The decisions' verdicts in order success, partial, failure, could not tell, then any other key; only counts above 0. */
export const verdictSegments = (by: Record<string, number>) => segments(by, VERDICTS);
export const reportSegments = (by: Record<string, number>) => segments(by, REPORT_VERDICTS);
/** The bar's accessible name: "Success 1, Partial 1, Could not tell 9". */
export const verdictSummary = (segs: Segment[]) => segs.map((s) => `${s.label} ${s.count}`).join(', ');
/** A legend entry: "Success 1 (9%)". */
export const legendText = (s: Segment) => `${s.label} ${s.count} (${s.share})`;

// ── Who graded ──────────────────────────────────────────────────────────────

/** A grader's name as a row label and inside a sentence. The key "None" is a claim with no grader recorded. */
export const GRADER_WORDS: Record<string, { label: string; by: string }> = {
  a2ui: { label: 'Your tap on a card', by: 'your tap on a card' },
  agent: { label: 'Nous itself, in a conversation', by: 'Nous itself' },
  external: { label: 'The review route', by: 'the review route' },
  auto: { label: "The reviewer's fixed signals", by: "the reviewer's fixed signals" },
  'auto:stated_confidence': { label: "Nous's stated confidence (not an outcome)", by: "Nous's stated confidence" },
  None: { label: 'Not recorded', by: 'an unrecorded grader' },
};
export const graderWords = (key: string | null) =>
  own(GRADER_WORDS, key ?? 'None') ?? { label: `Other: ${key}`, by: `${key}` };

export interface GraderRow { key: string; label: string; n: number; agree: number; harsher: number; kinder: number; cannotTell: number; rate: string }

/** The agreement table's rows, the most judged first. */
export function graderRows(by: Record<string, GraderGroup>): GraderRow[] {
  return Object.entries(by)
    .sort(([a, x], [b, y]) => y.n - x.n || (a < b ? -1 : a > b ? 1 : 0))
    .map(([key, g]) => ({
      key, label: graderWords(key).label, n: g.n, agree: g.agree, harsher: g.judge_harsher, kinder: g.judge_kinder,
      cannotTell: g.cannot_tell, rate: rateOr(g.agree_rate, NOT_ENOUGH),
    }));
}

// ── Where the judge disagreed ───────────────────────────────────────────────

const RANK: Record<string, number> = { failure: 0, partial: 1, success: 2 };

/** How a verdict stands to the grade, in the order failure < partial < success. */
export function directionWord(verdict: string, grade: string | null): string {
  const v = own(RANK, verdict);
  const g = grade === null ? undefined : own(RANK, grade);
  if (v === undefined || g === undefined || v === g) return 'different from that grade';
  return v < g ? 'harsher than that grade' : 'kinder than that grade';
}

export const disagreementLine = (d: Pick<Disagreement, 'against_grade' | 'against_grader' | 'verdict'>) =>
  `Graded ${d.against_grade ?? 'with no grade'} by ${graderWords(d.against_grader).by}. The judge says ${d.verdict}, ${directionWord(d.verdict, d.against_grade)}.`;

/** A listed subject's title, or its noun and the first 8 characters of its id when it has none. */
export const titleOrId = (title: string | null, id: string, noun: 'Decision' | 'Task') =>
  title && title.trim() ? title : `${noun} ${id.slice(0, 8)}`;

export const confidenceText = (c: number) => `Judge's confidence ${pct(c)}`;

export const disagreementCount = (total: number, days: number) =>
  total >= DISAGREEMENTS_MAX ? `The latest ${DISAGREEMENTS_MAX}, newest first` : `${total} in the last ${days} days, newest first`;
export const showAllText = (total: number) => `Show all ${total}`;

/** The judge's labels in words; one this list does not know is shown as it came. */
export const LABEL_WORDS: Record<string, string> = {
  evidence_confirms: 'Evidence confirms it',
  evidence_contradicts: 'Evidence contradicts it',
  evidence_partial: 'Evidence covers part of it',
  evidence_missing: 'No evidence found',
  claim_unsupported: 'Claims more than the evidence shows',
  result_not_checked: 'Result not checked',
  no_action_found: 'No action found',
  asks_for_verdict: 'The record tried to set the verdict',
  claim_backed: 'Report backed by the evidence',
  claim_not_backed: 'Report claims more than the evidence shows',
  error_in_evidence: 'A call the report relies on failed',
  files_unverified: 'A named file or change was not verified',
  scope_gap: 'Covers less than the task asked',
  evidence_thin: 'Thin evidence',
};
export const labelWord = (label: string) => own(LABEL_WORDS, label) ?? label;

// ── Task reports ────────────────────────────────────────────────────────────

export const reportsSub = (r: Pick<ReportNumbers, 'judge' | 'version'>) => `Judge ${r.judge}, instructions ${r.version}`;
export const agreementLine = (label: string, a: Against) =>
  a.agree_rate == null ? `${label}: ${NOT_ENOUGH_COMPARED}` : `${label}: ${pct(a.agree_rate)} (${a.agree} of ${a.compared} compared)`;
export const reportCostText = (c: Cost) =>
  `Cost: ${count(c.calls)} calls, ${tokens(c.tokens_in)} tokens in, ${tokens(c.tokens_out)} tokens out.`;

const FINISH_WORDS: Record<string, string> = {
  clean: 'Finish check: clean', flagged: 'Finish check: flagged', not_checked: 'Finish check: not checked',
};
export const finishWord = (value: string | null) =>
  value === null ? 'No finish check' : own(FINISH_WORDS, value) ?? `Finish check: ${value}`;
const AUDIT_WORDS: Record<string, string> = { right: 'Your spot check: right', wrong: 'Your spot check: wrong' };
export const auditWord = (value: string | null) => (value === null ? 'No spot check yet' : own(AUDIT_WORDS, value) ?? value);

// ── Not judged ──────────────────────────────────────────────────────────────

const SKIP_WORDS: Record<string, string> = { evidence_missing: 'no record of what happened was found' };
const skipWord = (label: string) => own(SKIP_WORDS, label) ?? labelWord(label);

/** What was skipped (by its label) and how many judgements failed or ran out of time. */
export function notJudgedText(skipped: Record<string, number>, errors: number): string {
  const entries = Object.entries(skipped).filter(([, n]) => n > 0);
  const total = entries.reduce((sum, [, n]) => sum + n, 0);
  const parts: string[] = [];
  if (total > 0) parts.push(`${total} skipped (${entries.map(([k, n]) => `${skipWord(k)}: ${n}`).join(', ')})`);
  if (errors > 0) parts.push(`${errors} judgement${errors === 1 ? '' : 's'} failed or ran out of time`);
  return parts.length ? parts.join('; ') : NOTHING_LEFT_OUT;
}

/** One line for the decisions and, when task reports are judged, one for the reports. */
export const notJudgedLines = (d: JudgeData) => [
  `Decisions: ${notJudgedText(d.decisions.skipped_by_label, d.decisions.errors)}`,
  ...(d.reports ? [`Reports: ${notJudgedText(d.reports.skipped_by_label, d.reports.errors)}`] : []),
];

// ── The hourly pass ─────────────────────────────────────────────────────────

/** "3 decisions (your tap on a card 2, the reviewer's fixed signals 1)", or "0 decisions". */
function queuedDecisions(by: Record<string, number>): string {
  const entries = Object.entries(by).filter(([, n]) => n > 0);
  const total = entries.reduce((sum, [, n]) => sum + n, 0);
  if (total === 0) return '0 decisions';
  return `${plural(total, 'decision', 'decisions')} (${entries.map(([k, n]) => `${graderWords(k).by} ${n}`).join(', ')})`;
}

/** What the hourly pass did in this server process. Null: the process has no numbers for it (no pass is running, or
 * they could not be read), which is not "no pass has run". */
export function sweepText(rt: SweepRuntime | null): string {
  if (rt === null) return SWEEP_NO_NUMBERS;
  if (rt.passes === 0) return SWEEP_NONE_YET;
  const last = rt.last_pass_at ? `, the last at ${fmtUtc(rt.last_pass_at)}` : '';
  return `Since this process started: ${plural(rt.passes, 'pass', 'passes')}${last}. `
    + `It queued ${queuedDecisions(rt.queued_decisions)} and ${plural(rt.queued_reports, 'report', 'reports')}. `
    + `${plural(rt.held_at_half_limit, 'pass', 'passes')} held back at half the daily limit, ${rt.failed} failed.`;
}

export const statedConfidenceText = (sweep: Sweep) =>
  `Agreement with Nous's stated confidence, which is not an outcome: ${rateOr(sweep.agreement.stated_confidence.agree_rate, NOT_ENOUGH)}`;

// ── The second judge ────────────────────────────────────────────────────────

export const secondText = (s: SecondNumbers) =>
  `Judge ${s.judge}, instructions ${s.version}, model ${s.model ?? 'not named'}. It judged ${s.decisions.judged} decisions; calls today ${s.cost.today.calls} of ${s.cost.today.cap}.`;
export const betweenText = (b: Between) =>
  `On the same claim, both judges judged ${b.pairs} decisions: they agreed on ${b.agree}, one could not tell on ${b.either_cannot_tell}, and they differed on ${b.pairs - b.agree - b.either_cannot_tell}.`;
export const betweenRate = (b: Between) => `Agreement: ${rateOr(b.agree_rate, NOT_ENOUGH_COMPARED)}`;
export const otherClaimText = (n: number) => `${n} more were judged on different claims and are not compared.`;
export const pairLine = (p: Pick<PairDisagreement, 'first_verdict' | 'second_verdict'>) =>
  `The first judge says ${p.first_verdict}, the second says ${p.second_verdict}.`;

// ── Cost ────────────────────────────────────────────────────────────────────

/** A median time: "—" with nothing measured, milliseconds under a second, else seconds to one decimal. */
export function latencyText(ms: number | null): string {
  if (ms == null) return '—';
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export const costRows = (c: Cost): [string, string][] => [
  ['Calls', count(c.calls)],
  ['Tokens in', tokens(c.tokens_in)],
  ['Tokens out', tokens(c.tokens_out)],
  ['Average tokens per verdict', tokens(c.tokens_per_verdict_avg)],
  ['Median time per verdict', latencyText(c.latency_ms_p50)],
];
