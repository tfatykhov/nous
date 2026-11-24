import { describe, it, expect } from 'vitest';
import { get } from 'svelte/store';
import { mergeCounts, attentionOverride, pushCounts } from './attention';

describe('attention counts — the newest source wins', () => {
  it('uses the poll when no view has reported anything newer', () => {
    expect(mergeCounts({ questions_waiting: 2, sends_in_doubt: 1, goal_asks_waiting: 3 }, 1000, {})).toEqual({ questions: 2, sends: 1, goals: 3 });
  });

  it('lets a tab that just loaded fresher numbers override the badge', () => {
    const merged = mergeCounts({ questions_waiting: 2, sends_in_doubt: 1 }, 1000, {
      questions: { value: 0, at: 2000 },
    });
    expect(merged).toEqual({ questions: 0, sends: 1, goals: 0 });
  });

  it('ignores an override older than the poll', () => {
    expect(mergeCounts({ questions_waiting: 3, sends_in_doubt: 0 }, 5000, {
      questions: { value: 9, at: 4000 },
    })).toEqual({ questions: 3, sends: 0, goals: 0 });
  });

  it('is zero before anything has loaded', () => {
    expect(mergeCounts(null, null, {})).toEqual({ questions: 0, sends: 0, goals: 0 });
  });

  it('records a pushed count with its time', () => {
    pushCounts({ sends: 4 });
    expect(get(attentionOverride).sends?.value).toBe(4);
  });

  it('the goals count: a count the server could not read is no badge, and the Goals page can push a newer one', () => {
    expect(mergeCounts({ questions_waiting: 0, sends_in_doubt: 0, goal_asks_waiting: null }, 1000, {}).goals).toBe(0);
    expect(mergeCounts({ questions_waiting: 0, sends_in_doubt: 0 }, 1000, {}).goals).toBe(0); // an older server
    expect(mergeCounts({ questions_waiting: 0, sends_in_doubt: 0, goal_asks_waiting: 2 }, 1000, {
      goals: { value: 0, at: 2000 },
    }).goals).toBe(0);
    pushCounts({ goals: 1 });
    expect(get(attentionOverride).goals?.value).toBe(1);
    attentionOverride.set({});
  });
});
