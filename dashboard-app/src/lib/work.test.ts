import { describe, it, expect } from 'vitest';
import { LANE_COLORS, laneColor, fmtDuration, fmtTokens, toolChips, callsOf } from './work';

describe('fmtDuration', () => {
  it('shows a dash for no duration', () => { expect(fmtDuration(null)).toBe('—'); });
  it('shows milliseconds under a second', () => {
    expect(fmtDuration(40)).toBe('40 ms');
    expect(fmtDuration(0)).toBe('0 ms');
  });
  it('shows seconds with one decimal from a second', () => {
    expect(fmtDuration(1400)).toBe('1.4 s');
    expect(fmtDuration(1000)).toBe('1.0 s');
  });
});

describe('fmtTokens', () => {
  it('shows a dash for no count', () => { expect(fmtTokens(null)).toBe('—'); });
  it('shows the plain number under 1,000', () => {
    expect(fmtTokens(999)).toBe('999');
    expect(fmtTokens(0)).toBe('0');
  });
  it('shows K with one decimal from 1,000', () => {
    expect(fmtTokens(1000)).toBe('1.0K');
    expect(fmtTokens(54532)).toBe('54.5K');
    expect(fmtTokens(46100)).toBe('46.1K');
  });
  it('shows M with one decimal from 1,000,000', () => {
    expect(fmtTokens(1_000_000)).toBe('1.0M');
    expect(fmtTokens(2_500_000)).toBe('2.5M');
  });
});

describe('lanes', () => {
  it('has the mockup colour for each lane', () => {
    expect(LANE_COLORS).toEqual({
      memory: '#60a5fa',
      live: '#fbbf24',
      workspace: '#9ca3af',
      acts: '#f472b6',
      other: '#8e8eab',
    });
    expect(laneColor('memory')).toBe('#60a5fa');
    expect(laneColor('acts')).toBe('#f472b6');
  });
  it('gives an unknown lane the neutral colour, never undefined', () => {
    expect(laneColor('unknown-lane')).toBe('#8e8eab');
    expect(laneColor('')).toBe('#8e8eab');
    // An inherited property name is not a lane either.
    expect(laneColor('toString')).toBe('#8e8eab');
    expect(laneColor('__proto__')).toBe('#8e8eab');
  });
});

describe('toolChips', () => {
  it('labels a tool called more than once with its count', () => {
    expect(toolChips([
      { tool: 'bash', calls: 3, lane: 'workspace' },
      { tool: 'recall_deep', calls: 1, lane: 'memory' },
    ])).toEqual([
      { label: 'bash ×3', lane: 'workspace' },
      { label: 'recall_deep', lane: 'memory' },
    ]);
  });
  it('collapses repeats of one tool into one chip, in first-seen order', () => {
    expect(toolChips([
      { tool: 'bash', calls: 1, lane: 'workspace' },
      { tool: 'read_file', calls: 1, lane: 'workspace' },
      { tool: 'bash', calls: 1, lane: 'workspace' },
      { tool: 'bash', calls: 2, lane: 'workspace' },
    ])).toEqual([
      { label: 'bash ×4', lane: 'workspace' },
      { label: 'read_file', lane: 'workspace' },
    ]);
  });
  it('is empty for no tools', () => { expect(toolChips([])).toEqual([]); });
});

describe('callsOf', () => {
  it('counts a model-call row as its call_count (a minute bucket holds many calls)', () => {
    expect(callsOf({ call_count: 1 })).toBe(1);
    expect(callsOf({ call_count: 37 })).toBe(37);
  });
});
