import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import {
  FRAME_IDS,
  chosenBy,
  clockTime,
  criticVerdict,
  fmtSeconds,
  frameChange,
  frameLabel,
  frameVar,
  lastFramed,
  replySeconds,
  verdictText,
} from './frames';
import { laneVar } from './work';
import { DARK_SEL, PAPER_SEL, parseBlocks } from './testing/css';
import type { WorkTurn } from './types/api';

const turn = (over: Partial<WorkTurn> = {}): WorkTurn => ({
  turn_id: 't', turn_number: 1, has_turn_row: true, started_at: '2026-11-10T09:12:04+00:00', ended_at: '2026-11-10T09:12:11.8+00:00',
  frame_id: 'question', trigger: null, memory: null, critic: null, claim_check: null, reply: null, error: null,
  tool_call_count: 0, model_calls: [], calls: [],
  frame_reason: 'pattern', critic_frame: null, had_error: false, input_tokens: null, model_call_count: 0,
  ...over,
});

describe('critic verdict', () => {
  it.each([
    // [frame_reason, critic_frame, frame_id, verdict]
    ['critic', 'decision', 'decision', 'overrode'],   // an override: the critic's frame is the one that stood
    ['critic', null, 'decision', 'overrode'],
    ['pattern', null, 'question', null],              // no model recommendation
    ['pattern', 'unknown', 'question', null],         // the critic named no offered frame
    ['pattern', 'question', 'question', 'agreed'],
    ['referent', 'task', 'task', 'agreed'],
    ['pattern', 'task', 'question', 'disagreed'],     // it recommended another frame and the frame stood
    [null, 'question', 'question', null],             // a row older than migration 106
    [null, 'task', 'question', null],
    ['pattern', 'task', null, null],                  // no frame: nothing was kept
  ] as const)('reason %s, critic %s, frame %s -> %s', (frame_reason, critic_frame, frame_id, want) => {
    expect(criticVerdict({ frame_reason, critic_frame, frame_id })).toBe(want);
  });

  it('an older API sends none of the new keys: no verdict', () => {
    expect(criticVerdict({ frame_id: 'question' } as Pick<WorkTurn, 'frame_id' | 'frame_reason' | 'critic_frame'>)).toBeNull();
  });

  it('the pill text says agreed, overrode it, or disagreed and kept the frame; never "passed it through"', () => {
    expect(verdictText('agreed', 'question')).toBe('Critic agreed');
    expect(verdictText('overrode', 'decision')).toBe('Critic overrode it');
    expect(verdictText('disagreed', 'question')).toBe('Critic disagreed, kept question');
  });
});

describe('frame change sentence', () => {
  const prev = turn({ frame_id: 'question' });
  it.each([
    ['referent', 'Your short reply answered my offer.'],
    ['pattern', 'A new topic.'],
    ['direct', 'Set directly, not chosen from your message.'],
    ['default', 'No pattern matched, so the default frame applied.'],
    [null, 'The reason was not recorded.'],
    ['something_new', 'Chosen by something_new.'],
  ] as const)('reason %s', (frame_reason, why) => {
    expect(frameChange(prev, turn({ frame_id: 'task', frame_reason }))).toEqual({ from: 'question', to: 'task', why });
  });

  it('critic: names the heuristic frame it beat, and leaves it out when the critic JSON has none', () => {
    const cur = turn({ frame_id: 'decision', frame_reason: 'critic', critic: { heuristic_frame: 'task', critic_frame: 'decision' } });
    expect(frameChange(prev, cur)?.why).toBe('The critic chose decision over task.');
    expect(frameChange(prev, turn({ frame_id: 'decision', frame_reason: 'critic', critic: null }))?.why).toBe('The critic chose decision.');
    expect(frameChange(prev, turn({ frame_id: 'decision', frame_reason: 'critic', critic: { critic_frame: 'decision' } }))?.why)
      .toBe('The critic chose decision.');
  });

  it('no sentence: the first turn, a carried frame, an unchanged frame, a missing frame on either side', () => {
    expect(frameChange(undefined, turn())).toBeNull();
    expect(frameChange(prev, turn({ frame_id: 'task', frame_reason: 'carried' }))).toBeNull();
    expect(frameChange(prev, turn({ frame_id: 'question', frame_reason: 'pattern' }))).toBeNull();
    expect(frameChange(prev, turn({ frame_id: null, has_turn_row: false }))).toBeNull();
    expect(frameChange(turn({ frame_id: null }), turn({ frame_id: 'task' }))).toBeNull();
  });

  it('the previous turn to compare with is the last earlier one that has a frame (review M7)', () => {
    const list = [turn({ turn_id: 'a', frame_id: 'question' }), turn({ turn_id: 'b', frame_id: null, has_turn_row: false }), turn({ turn_id: 'c', frame_id: 'task' })];
    expect(lastFramed(list, 2)?.turn_id).toBe('a');
    expect(lastFramed(list, 1)?.turn_id).toBe('a');
    expect(lastFramed(list, 0)).toBeUndefined();
    expect(frameChange(lastFramed(list, 2), list[2])).toEqual({ from: 'question', to: 'task', why: 'A new topic.' });
  });

  it('an older API (no frame_reason key) still gets a sentence, with the reason not recorded', () => {
    const cur = turn({ frame_id: 'task' });
    delete (cur as Partial<WorkTurn>).frame_reason;
    expect(frameChange(prev, cur)?.why).toBe('The reason was not recorded.');
  });
});

describe('frame colour, name and reason', () => {
  it('a known frame reads its token; an id outside the seven reads the neutral; a missing one the conversation grey', () => {
    expect(frameVar('task')).toBe('var(--f-task)');
    for (const f of FRAME_IDS) expect(frameVar(f)).toBe(`var(--f-${f})`);
    expect(frameVar(null)).toBe('var(--f-conversation)');
    expect(frameVar(undefined)).toBe('var(--f-conversation)');
    // Review M1: a frame outside the seven is not conversation; it reads its own neutral token.
    expect(frameVar('x-custom')).toBe('var(--f-other)');
    expect(frameVar('toString')).toBe('var(--f-other)');
  });

  it('a frame name is its id; a missing one says so', () => {
    expect(frameLabel('debug')).toBe('debug');
    expect(frameLabel('x-custom')).toBe('x-custom');
    expect(frameLabel(null)).toBe('No frame recorded');
    expect(frameLabel(undefined)).toBe('No frame recorded');
  });

  it('chosen by the reason, or not recorded', () => {
    expect(chosenBy('referent')).toBe('chosen by referent');
    expect(chosenBy(null)).toBe('reason not recorded');
    expect(chosenBy(undefined)).toBe('reason not recorded');
  });

  it('the seven frame tokens and the neutral exist on the dark block and on the paper ground', () => {
    const blocks = parseBlocks(readFileSync(join(__dirname, '..', 'app.css'), 'utf-8'));
    for (const sel of [DARK_SEL, PAPER_SEL]) {
      const b = blocks.get(sel) ?? {};
      for (const f of [...FRAME_IDS, 'other']) expect(b[`--f-${f}`], `${sel} --f-${f}`).toMatch(/^#[0-9a-f]{6}$/);
    }
  });
});

describe('times', () => {
  it('seconds to reply', () => {
    expect(replySeconds({ started_at: '2026-11-10T09:12:04+00:00', ended_at: '2026-11-10T09:12:11.8+00:00' })).toBeCloseTo(7.8, 5);
    expect(replySeconds({ started_at: '2026-11-10T09:12:04+00:00', ended_at: null })).toBeNull();
    expect(replySeconds({ started_at: '2026-11-10T09:12:04+00:00', ended_at: '2026-11-10T09:12:03+00:00' })).toBeNull();
    expect(replySeconds({ started_at: 'nonsense', ended_at: '2026-11-10T09:12:03+00:00' })).toBeNull();
  });

  it('formats seconds with one decimal, a dash for none', () => {
    expect(fmtSeconds(7.8)).toBe('7.8 s');
    expect(fmtSeconds(0.4)).toBe('0.4 s');
    expect(fmtSeconds(0.04)).toBe('0.0 s');
    expect(fmtSeconds(null)).toBe('—');
  });

  it('clock time in UTC, a dash for none or nonsense', () => {
    expect(clockTime('2026-11-10T09:12:04Z')).toBe('09:12:04');
    expect(clockTime('2026-11-10T09:12:04.999+00:00')).toBe('09:12:04');
    expect(clockTime('2026-11-10T10:12:04+01:00')).toBe('09:12:04');
    expect(clockTime(null)).toBe('—');
    expect(clockTime('')).toBe('—');
    expect(clockTime('not a time')).toBe('—');
  });
});

describe('lane tokens', () => {
  it('a lane reads its token; anything else reads the other lane', () => {
    expect(laneVar('memory')).toBe('var(--lane-memory)');
    expect(laneVar('acts')).toBe('var(--lane-acts)');
    expect(laneVar('toString')).toBe('var(--lane-other)');
    expect(laneVar('')).toBe('var(--lane-other)');
  });
});
