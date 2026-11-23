import { describe, it, expect, vi, afterEach } from 'vitest';
import {
  AGAINST_AUDIT, AGAINST_FINISH, DISAGREEMENTS_MAX, GRADER_COLUMNS, GRADER_NOTE, INTRO, LABEL_WORDS, LOAD_FAILED,
  NO_DISAGREEMENTS, NO_NOT_SUPPORTED, NO_REPORTS, NO_SENTENCE, NOT_ENOUGH, NOT_ENOUGH_COMPARED, NOT_SUPPORTED_HEADING,
  NOTHING_GRADED, NOTHING_JUDGED, NOTHING_LEFT_OUT, SHOW_FEWER, SHOWN, STRIP_HINT, SWEEP_INTRO, SWEEP_NO_NUMBERS,
  SWEEP_NONE_YET, TITLE,
  agreementLine, auditWord, betweenRate, betweenText, cannotTellTile, confidenceText, costRows, directionWord,
  disagreementCount, disagreementLine, finishWord, graderRows, graderWords, labelWord, latencyText, legendText,
  notJudgedLines, notJudgedText, otherClaimText, outcomeTile, pairLine, readJudge, reportCostText, reportSegments,
  reportsSub, scopeText, secondText, showAllText, statedConfidenceText, statusTitle, stripRows, sweepText, tiles,
  titleOrId, verdictSegments, verdictSummary, workerText,
  type Between, type GraderGroup, type JudgeData, type SecondNumbers, type Sweep, type SweepRuntime,
} from './outside_judge_numbers';

// F118: the words of the panel "The outside judge". Every sentence is pinned here; the fixtures are made up, in the
// shape nous/api/rest.py answers (the thin state: few decisions graded, most verdicts "could not tell").

const group = (over: Partial<GraderGroup> = {}): GraderGroup =>
  ({ n: 11, agree: 1, judge_harsher: 1, judge_kinder: 0, cannot_tell: 9, agree_rate: 0.5, ...over });
const RUNTIME: SweepRuntime = {
  passes: 3, queued_decisions: { a2ui: 2, auto: 1 }, queued_reports: 2, held_at_half_limit: 1, failed: 0,
  last_pass_at: '2026-11-20T16:56:00+00:00',
};
const SWEEP = (over: Partial<Sweep> = {}): Sweep =>
  ({ agreement: { outcome_grades: group(), stated_confidence: group({ n: 0, agree: 0, judge_harsher: 0, cannot_tell: 0, agree_rate: null }) }, runtime: RUNTIME, ...over });
const THIN = (over: Partial<JudgeData> = {}): JudgeData => ({
  enabled: true, mode: 'shadow', judge: 'background_model', version: 'dj-2', model: 'claude-test-model', days: 7,
  decisions: {
    judged: 11, by_verdict: { cannot_tell: 9, success: 1, partial: 1 }, skipped_by_label: { evidence_missing: 3 }, errors: 1,
    by_grader: { a2ui: group() },
  },
  cost: { calls: 14, tokens_in: 80_000, tokens_out: 4_900, tokens_per_verdict_avg: 7_107.4, latency_ms_p50: 3_631.5, today: { calls: 16, cap: 100 } },
  disagreements: [{
    subject_id: '1a2b3c4d-0000-4000-8000-000000000001', title: 'Retry the upload', against_grader: 'a2ui', against_grade: 'success',
    verdict: 'partial', confidence: 0.55, labels: ['evidence_partial'], reason: 'Only one of two files was written.',
    judged_at: '2026-11-20T10:00:00+00:00',
  }],
  runtime: { queued: 0, dropped: 0, over_cap: 0, worker_alive: true },
  reports: {
    judge: 'background_model', version: 'rj-1', judged: 4, by_verdict: { supported: 3, not_supported: 1 },
    skipped_by_label: { evidence_missing: 2 }, errors: 0,
    vs_finish_check: { n: 4, compared: 4, agree: 3, agree_rate: 0.75, matrix: {} },
    vs_audit: { n: 0, compared: 0, agree: 0, agree_rate: null, matrix: {} },
    cost: { calls: 4, tokens_in: 10_734, tokens_out: 826, tokens_per_verdict_avg: 2_890, latency_ms_p50: 1_889.5 },
    not_supported: [],
  },
  sweep: SWEEP(),
  ...over,
});

describe('fixed sentences', () => {
  it('pins every constant the owner reads', () => {
    expect(TITLE).toBe('The outside judge');
    expect(INTRO).toBe('The outside judge only records its own verdicts beside the grades Nous already has, and changes no grade.');
    expect(LOAD_FAILED).toBe("The outside judge's numbers could not be read. Use Refresh at the top of the page to try again.");
    expect(NOT_ENOUGH).toBe('Not enough graded yet');
    expect(NOT_ENOUGH_COMPARED).toBe('Not enough compared yet');
    expect(NOTHING_JUDGED(7)).toBe('No decision has been judged in the last 7 days.');
    expect(NOTHING_GRADED).toBe('No verdict has been compared with a grade yet.');
    expect(NO_DISAGREEMENTS(30)).toBe('The judge has not disagreed with a grade in the last 30 days.');
    expect(NO_REPORTS(90)).toBe('No task report has been judged in the last 90 days.');
    expect(NO_NOT_SUPPORTED).toBe('The judge found every judged report supported or could not tell.');
    expect(NOTHING_LEFT_OUT).toBe('Nothing was left unjudged.');
    expect(NO_SENTENCE).toBe('The judge gave no sentence.');
    expect(GRADER_NOTE).toBe('Agreement leaves out the verdicts that said “could not tell”.');
    expect(STRIP_HINT).toBe('Waiting is the queue now. Dropped and over the limit count since this server process started.');
    expect(SWEEP_INTRO).toBe(
      "Once an hour the judge picks up decisions and reports that have no final verdict yet. A pass queues nothing once today's calls reach half the daily limit.");
    expect(SWEEP_NO_NUMBERS).toBe('The hourly pass has no numbers in this server process.');
    expect(SWEEP_NONE_YET).toBe('No hourly pass has run yet in this server process.');
    expect(SHOW_FEWER).toBe('Show fewer');
    expect(showAllText(12)).toBe('Show all 12');
    expect(NOT_SUPPORTED_HEADING).toBe('Latest reports the judge did not find supported');
    expect(GRADER_COLUMNS).toEqual(['Graded by', 'Judged', 'Agree', 'Judge harsher', 'Judge kinder', 'Could not tell', 'Agreement']);
    expect([DISAGREEMENTS_MAX, SHOWN]).toEqual([50, 10]);
  });
});

describe('the status strip', () => {
  it('says shadow mode in words, the scope of the numbers, and each value beside its label', () => {
    expect(statusTitle(THIN())).toBe('Shadow mode: it records verdicts and changes nothing');
    expect(statusTitle({ mode: 'other' })).toBe('Mode: other');
    expect(workerText(true)).toBe('Running');
    expect(workerText(false)).toBe('Not running');
    expect(scopeText(THIN())).toBe(
      'These numbers cover only judge background_model, instructions dj-2, over the last 7 days. A new instruction version starts them again from zero.');
    expect(stripRows(THIN({ runtime: { queued: 1200, dropped: 2, over_cap: 3, worker_alive: false } }))).toEqual([
      ['Judge', 'background_model'], ['Instructions', 'dj-2'], ['Model', 'claude-test-model'], ['Worker', 'Not running'],
      ['Waiting', '1,200'], ['Dropped', '2'], ['Over the daily limit', '3'],
    ]);
    expect(stripRows(THIN({ model: null }))[2]).toEqual(['Model', 'not named']);
  });
});

describe('tiles', () => {
  it('the thin state: judged, the agreement with outcomes, could not tell, and calls today', () => {
    expect(tiles(THIN())).toEqual([
      { label: 'Decisions judged', value: '11', sub: 'in the last 7 days', words: false },
      { label: 'Agreement with outcomes', value: '50%', sub: "1 of 2 graded verdicts match your taps and the reviewer's outcome signals", words: false },
      { label: 'Could not tell', value: '82%', sub: '9 of 11 verdicts', words: false },
      { label: 'Calls today', value: '16 of 100', sub: 'today, every instructions version, decisions and reports', words: false },
    ]);
  });

  it('a rate with nothing graded is words, never 0%; without the sweep the tile is not there', () => {
    expect(outcomeTile(SWEEP({ agreement: { outcome_grades: group({ agree: 0, judge_harsher: 0, cannot_tell: 11, agree_rate: null }), stated_confidence: group() } })))
      .toEqual({ label: 'Agreement with outcomes', value: 'Not enough graded yet', sub: '0 graded so far', words: true });
    expect(outcomeTile(undefined)).toBeNull();
    expect(tiles(THIN({ sweep: undefined })).map((t) => t.label)).toEqual(['Decisions judged', 'Could not tell', 'Calls today']);
  });

  it('with nothing judged the share of "could not tell" is a dash', () => {
    expect(cannotTellTile({ judged: 0, by_verdict: {}, skipped_by_label: {}, errors: 0, by_grader: {} }))
      .toEqual({ label: 'Could not tell', value: '—', sub: 'nothing judged yet', words: false });
  });
});

describe('verdict bars', () => {
  it('in the fixed order, counts above zero only, any other key after them', () => {
    const thin = verdictSegments({ cannot_tell: 9, success: 1, partial: 1 });
    expect(thin).toEqual([
      { key: 'success', label: 'Success', count: 1, share: '9%', token: '--green' },
      { key: 'partial', label: 'Partial', count: 1, share: '9%', token: '--warn-text' },
      { key: 'cannot_tell', label: 'Could not tell', count: 9, share: '82%', token: '--muted' },
    ]);
    expect(verdictSummary(thin)).toBe('Success 1, Partial 1, Could not tell 9');
    expect(legendText(thin[0])).toBe('Success 1 (9%)');
    expect(verdictSegments({ failure: 2, success: 0, maybe: 2 }).map((s) => [s.label, s.count, s.token]))
      .toEqual([['Failure', 2, '--red'], ['maybe', 2, '--muted']]);
    expect(verdictSegments({})).toEqual([]);
  });

  it('the reports\' verdicts', () => {
    expect(reportSegments({ cannot_tell: 1, not_supported: 1, supported: 2 }).map((s) => [s.label, s.count, s.share, s.token])).toEqual([
      ['Supported', 2, '50%', '--green'], ['Not supported', 1, '25%', '--red'], ['Could not tell', 1, '25%', '--muted'],
    ]);
  });
});

describe('who graded', () => {
  it('names every grader, and an unknown one as other', () => {
    const rows = graderRows({
      auto: group({ n: 3 }), a2ui: group({ n: 5 }), agent: group({ n: 3 }), external: group({ n: 1 }),
      'auto:stated_confidence': group({ n: 1 }), None: group({ n: 1 }), weekly_job: group({ n: 1 }),
    });
    expect(rows.map((r) => [r.key, r.label])).toEqual([
      ['a2ui', 'Your tap on a card'],
      ['agent', 'Nous itself, in a conversation'],
      ['auto', "The reviewer's fixed signals"],
      ['None', 'Not recorded'],
      ['auto:stated_confidence', "Nous's stated confidence (not an outcome)"],
      ['external', 'The review route'],
      ['weekly_job', 'Other: weekly_job'],
    ]);
    expect(graderWords(null)).toEqual({ label: 'Not recorded', by: 'an unrecorded grader' });
    expect(graderWords('constructor')).toEqual({ label: 'Other: constructor', by: 'constructor' });
  });

  it('a row: its counts, and its rate in words when nothing was graded (a real 0 is 0%)', () => {
    const [thin] = graderRows({ a2ui: group() });
    expect(thin).toEqual({ key: 'a2ui', label: 'Your tap on a card', n: 11, agree: 1, harsher: 1, kinder: 0, cannotTell: 9, rate: '50%' });
    expect(graderRows({ a2ui: group({ agree_rate: null }) })[0].rate).toBe('Not enough graded yet');
    expect(graderRows({ a2ui: group({ agree: 0, agree_rate: 0 }) })[0].rate).toBe('0%');
  });
});

describe('where the judge disagreed', () => {
  it('says how the verdict stands to the grade', () => {
    expect(directionWord('partial', 'success')).toBe('harsher than that grade');
    expect(directionWord('success', 'failure')).toBe('kinder than that grade');
    expect(directionWord('success', 'abandoned')).toBe('different from that grade');
    expect(directionWord('success', null)).toBe('different from that grade');
  });

  it('one line per disagreement', () => {
    expect(disagreementLine(THIN().disagreements[0])).toBe(
      'Graded success by your tap on a card. The judge says partial, harsher than that grade.');
    expect(disagreementLine({ against_grade: null, against_grader: null, verdict: 'failure' })).toBe(
      'Graded with no grade by an unrecorded grader. The judge says failure, different from that grade.');
  });

  it('a title, or the noun and the start of the id', () => {
    expect(titleOrId('Retry the upload', '1a2b3c4d-0000', 'Decision')).toBe('Retry the upload');
    expect(titleOrId(null, '1a2b3c4d-0000', 'Decision')).toBe('Decision 1a2b3c4d');
    expect(titleOrId('  ', '9f8e7d6c-0000', 'Task')).toBe('Task 9f8e7d6c');
  });

  it('the count, which says "the latest 50" when the server cut the list', () => {
    expect(disagreementCount(1, 7)).toBe('1 in the last 7 days, newest first');
    expect(disagreementCount(49, 30)).toBe('49 in the last 30 days, newest first');
    expect(disagreementCount(50, 30)).toBe('The latest 50, newest first');
  });

  it('labels in words, an unknown one as it came', () => {
    expect(Object.keys(LABEL_WORDS)).toHaveLength(14);
    expect(labelWord('evidence_partial')).toBe('Evidence covers part of it');
    expect(labelWord('asks_for_verdict')).toBe('The record tried to set the verdict');
    expect(labelWord('scope_gap')).toBe('Covers less than the task asked');
    expect(labelWord('brand_new_label')).toBe('brand_new_label');
    expect(confidenceText(0.55)).toBe("Judge's confidence 55%");
  });
});

describe('task reports', () => {
  it('the judge, the agreement lines, the cost', () => {
    const r = THIN().reports!;
    expect(reportsSub(r)).toBe('Judge background_model, instructions rj-1');
    expect(agreementLine(AGAINST_FINISH, r.vs_finish_check)).toBe('Agreement with the finish checks: 75% (3 of 4 compared)');
    expect(agreementLine(AGAINST_AUDIT, r.vs_audit)).toBe('Agreement with your spot checks: Not enough compared yet');
    expect(reportCostText(r.cost)).toBe('Cost: 4 calls, 10,734 tokens in, 826 tokens out.');
  });

  it('the finish check and the spot check in words', () => {
    expect(['clean', 'flagged', 'not_checked', null, 'odd'].map((v) => finishWord(v))).toEqual([
      'Finish check: clean', 'Finish check: flagged', 'Finish check: not checked', 'No finish check', 'Finish check: odd',
    ]);
    expect(['right', 'wrong', null, 'odd'].map((v) => auditWord(v))).toEqual([
      'Your spot check: right', 'Your spot check: wrong', 'No spot check yet', 'odd',
    ]);
  });
});

describe('not judged', () => {
  it('skipped by label and failed judgements, or nothing left out', () => {
    expect(notJudgedText({}, 0)).toBe('Nothing was left unjudged.');
    expect(notJudgedText({ evidence_missing: 3 }, 0)).toBe('3 skipped (no record of what happened was found: 3)');
    expect(notJudgedText({}, 1)).toBe('1 judgement failed or ran out of time');
    expect(notJudgedText({ evidence_missing: 0 }, 2)).toBe('2 judgements failed or ran out of time');
    expect(notJudgedText({ evidence_missing: 1, odd_label: 2 }, 1)).toBe(
      '3 skipped (no record of what happened was found: 1, odd_label: 2); 1 judgement failed or ran out of time');
  });

  it('a line for the decisions, and one for the reports when they are judged', () => {
    expect(notJudgedLines(THIN())).toEqual([
      'Decisions: 3 skipped (no record of what happened was found: 3); 1 judgement failed or ran out of time',
      'Reports: 2 skipped (no record of what happened was found: 2)',
    ]);
    expect(notJudgedLines(THIN({ reports: undefined }))).toHaveLength(1);
  });
});

describe('the hourly pass', () => {
  it('null is "no numbers in this process", not "no pass has run"', () => {
    expect(sweepText(null)).toBe('The hourly pass has no numbers in this server process.');
    expect(sweepText({ ...RUNTIME, passes: 0 })).toBe('No hourly pass has run yet in this server process.');
  });

  it('what the passes did', () => {
    expect(sweepText(RUNTIME)).toBe(
      "Since this process started: 3 passes, the last at Nov 20 16:56 UTC. It queued 3 decisions (your tap on a card 2, the reviewer's fixed signals 1) "
      + 'and 2 reports. 1 pass held back at half the daily limit, 0 failed.');
    expect(sweepText({ ...RUNTIME, passes: 1, last_pass_at: null, queued_decisions: {}, queued_reports: 1, held_at_half_limit: 0, failed: 1 })).toBe(
      'Since this process started: 1 pass. It queued 0 decisions and 1 report. 0 passes held back at half the daily limit, 1 failed.');
  });

  it('the stated-confidence agreement, in words when nothing was graded', () => {
    expect(statedConfidenceText(SWEEP())).toBe("Agreement with Nous's stated confidence, which is not an outcome: Not enough graded yet");
    expect(statedConfidenceText(SWEEP({ agreement: { outcome_grades: group(), stated_confidence: group({ agree_rate: 0.25 }) } })))
      .toBe("Agreement with Nous's stated confidence, which is not an outcome: 25%");
  });
});

describe('the second judge', () => {
  const second: SecondNumbers = {
    judge: 'jev', version: 'jq-2:jev-1.14.0', model: null,
    decisions: { judged: 6, by_verdict: {}, skipped_by_label: {}, errors: 0, by_grader: {} },
    cost: { calls: 6, tokens_in: 1, tokens_out: 1, tokens_per_verdict_avg: null, latency_ms_p50: null, today: { calls: 2, cap: 50 } },
  };
  const between: Between = {
    pairs: 6, agree: 3, either_cannot_tell: 2, agree_rate: 0.75, matrix: {}, evidence_differs: 0, other_claim: 2,
    disagreements: [],
  };

  it('its numbers and how the two compare', () => {
    expect(secondText(second)).toBe('Judge jev, instructions jq-2:jev-1.14.0, model not named. It judged 6 decisions; calls today 2 of 50.');
    expect(betweenText(between)).toBe(
      'On the same claim, both judges judged 6 decisions: they agreed on 3, one could not tell on 2, and they differed on 1.');
    expect(betweenRate(between)).toBe('Agreement: 75%');
    expect(betweenRate({ ...between, agree_rate: null })).toBe('Agreement: Not enough compared yet');
    expect(otherClaimText(2)).toBe('2 more were judged on different claims and are not compared.');
    expect(pairLine({ first_verdict: 'success', second_verdict: 'failure' })).toBe('The first judge says success, the second says failure.');
  });
});

describe('cost', () => {
  it('the median time in ms or s, a dash with nothing measured', () => {
    expect(latencyText(null)).toBe('—');
    expect(latencyText(812.4)).toBe('812 ms');
    expect(latencyText(3_631.5)).toBe('3.6 s');
  });

  it('the decisions\' cost rows', () => {
    expect(costRows(THIN().cost)).toEqual([
      ['Calls', '14'], ['Tokens in', '80,000'], ['Tokens out', '4,900'], ['Average tokens per verdict', '7,107'],
      ['Median time per verdict', '3.6 s'],
    ]);
    expect(costRows({ ...THIN().cost, tokens_per_verdict_avg: null })[3]).toEqual(['Average tokens per verdict', '—']);
  });
});

describe('readJudge', () => {
  afterEach(() => { vi.unstubAllGlobals(); });

  it('asks GET /dashboard/outside-judge with the window, no body', async () => {
    const fetchMock = vi.fn(async () => ({ ok: true, status: 200, text: async () => JSON.stringify({ enabled: false }) }) as Response);
    vi.stubGlobal('fetch', fetchMock);
    expect(await readJudge('7')).toEqual({ enabled: false });
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/dashboard/outside-judge?days=7');
    expect(init.method).toBe('GET');
    expect(init.body).toBeUndefined();
  });
});
