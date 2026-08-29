import { describe, it, expect } from 'vitest';
import { formatFreshness } from './freshness';

// Pure-function tests with a hand-fed clock — no fake timers (rev-ui #10).

const T0 = Date.parse('2026-08-29T13:34:00Z');

describe('formatFreshness', () => {
  it('reads "just now" within the first minute and is not stale', () => {
    const f = formatFreshness('2026-08-29T13:34:00Z', T0 + 30_000, 3600);
    expect(f.label).toBe('composed 13:34 UTC · just now');
    expect(f.stale).toBe(false);
  });

  it('formats minutes and hours', () => {
    expect(formatFreshness('2026-08-29T13:34:00Z', T0 + 5 * 60_000, 3600).label).toContain(
      '5m ago',
    );
    expect(formatFreshness('2026-08-29T13:34:00Z', T0 + 2 * 3600_000, 7200 + 1).label).toContain(
      '2h ago',
    );
  });

  it('degrades to stale exactly at the threshold', () => {
    expect(formatFreshness('2026-08-29T13:34:00Z', T0 + 3599_000, 3600).stale).toBe(false);
    expect(formatFreshness('2026-08-29T13:34:00Z', T0 + 3600_000, 3600).stale).toBe(true);
  });

  it('treats an unparseable stamp as stale, never throws', () => {
    const f = formatFreshness('garbage', T0);
    expect(f.stale).toBe(true);
    expect(f.label).toContain('unknown');
  });

  it('clamps a future stamp to zero age instead of negative time', () => {
    const f = formatFreshness('2026-08-29T13:34:00Z', T0 - 60_000, 3600);
    expect(f.label).toContain('just now');
    expect(f.stale).toBe(false);
  });
});
