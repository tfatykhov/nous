import { describe, it, expect } from 'vitest';
import { fmtWhen, ledgerTarget, recipients, ruleVerdict, claimVerdict, relUntil } from './harness';

const NOW = new Date('2026-09-25T16:31:00Z').getTime();

describe('fmtWhen — absolute UTC first, relative second', () => {
  it('formats an ISO timestamp', () => {
    expect(fmtWhen('2026-09-25T14:30:07+00:00', NOW)).toBe('Sep 25 14:30 UTC · 2h ago');
  });
  it('is a dash for null', () => {
    expect(fmtWhen(null, NOW)).toBe('—');
  });
});

describe('relUntil', () => {
  it('counts down to a deadline and says when it has passed', () => {
    expect(relUntil('2026-09-25T19:33:00Z', NOW)).toBe('in 3 h 02 m');
    expect(relUntil('2026-09-25T16:00:00Z', NOW)).toBe('past the deadline — the default applies on the next tick');
  });
});

describe('ledgerTarget — only shapes the store keeps', () => {
  it('shows a path, recipients or a chat', () => {
    expect(ledgerTarget({ path: '/w/a.md' })).toBe('/w/a.md');
    expect(ledgerTarget({ to: ['a@x.io', 'b@x.io'] })).toBe('to a@x.io, b@x.io');
    expect(ledgerTarget({ chat_id: '7412', file_path: '/w/r.pdf' })).toBe('chat 7412 · /w/r.pdf');
  });
  it('shows a hashed free-text field as its hash and length, never the text', () => {
    expect(ledgerTarget({ command_sha256: '9b0e1234abcd', command_len: 41 }))
      .toBe('command sha256:9b0e1234… (41 chars)');
  });
  it('says so when retention removed the details', () => {
    expect(ledgerTarget({}, true)).toBe('details removed by retention');
    expect(ledgerTarget({})).toBe('—');
  });
});

describe('recipients', () => {
  it('flattens to/cc', () => {
    expect(recipients({ to: ['a@x.io'], cc: ['b@x.io'] })).toEqual(['a@x.io', 'b@x.io']);
    expect(recipients({})).toEqual([]);
  });
});

describe('ruleVerdict — never a green light without evidence', () => {
  const base = { mode: 'warn', first_event_at: '2026-09-24T08:10:00+00:00', by_mode: { warn: 12 } };
  const top = { context: 'heartbeat_callback', tool: 'send_file', violation: 'not offered' };

  it('says "not measured" when event persistence is off', () => {
    expect(ruleVerdict({ ...base, by_mode: {} }, '7 d', false, null).title).toBe('Not measured');
  });
  it('says "not checking" when the rule is off', () => {
    expect(ruleVerdict({ ...base, mode: 'off', by_mode: {} }, '7 d', true, null).title).toBe('Not checking');
  });
  it('says what enforce would refuse, from the top pattern', () => {
    const v = ruleVerdict(base, '7 d', true, top);
    expect(v.title).toBe('Enforce would refuse calls like these');
    expect(v.detail).toBe('12 in 7 d. Most: heartbeat_callback · send_file · not offered.');
    expect(v.tone).toBe('warn');
  });
  it('never claims "nothing would be refused" — only that nothing was flagged in the window', () => {
    const v = ruleVerdict({ ...base, by_mode: {} }, '7 d', true, null);
    expect(v.title).toBe('No flags in 7 d');
    expect(v.detail).toBe('First flag Sep 24 08:10 UTC.');
    const never = ruleVerdict({ ...base, first_event_at: null, by_mode: {} }, '7 d', true, null);
    expect(never.title).toBe('No flags recorded yet');
  });
  it('reports what enforce refused', () => {
    const v = ruleVerdict({ ...base, mode: 'enforce', by_mode: { enforce: 3 } }, '7 d', true, top);
    expect(v.title).toBe('Enforcing');
    expect(v.tone).toBe('ok');
    expect(v.detail).toContain('Refused 3 in 7 d');
  });
});

describe('claimVerdict', () => {
  const claims = { mode: 'enforce', by_evidence: { exact: 118, plausible: 24, none: 4 },
    legacy: { events: 0, violations: 0 }, evidence_since: '2026-09-24T08:10:00+00:00' };
  it('counts corrections under enforce and would-be corrections under warn', () => {
    expect(claimVerdict(claims, true).detail).toBe('4 claims had no evidence; a correction was queued for the next turn.');
    expect(claimVerdict({ ...claims, mode: 'warn' }, true).detail)
      .toBe('4 claims had no evidence and would have got a correction.');
  });
  it('is "not measured" when nothing is recorded', () => {
    expect(claimVerdict(claims, false).title).toBe('Not measured');
  });
});
