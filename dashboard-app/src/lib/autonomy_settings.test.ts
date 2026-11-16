import { describe, it, expect } from 'vitest';
import {
  FORBIDDEN_TEXT, GROUPS, NOT_JSON_TEXT, NOT_RECORDED_TEXT, NO_ANSWER_TEXT, OFF_TEXT, PREFERENCES_TEXT, SAME_TEXT,
  SETTING_WORDS, SOURCE_WORDS, TOOL_FILTERS, UNREACHABLE_TEXT, UNSUPPORTED_TEXT, GUARD_OFF_TEXT,
  callsText, ceilingText, changeLine, changeText, hostValueText, limitPath, notOfferedLines, problemText, rangeText,
  readGate, resetText, toolChangeText, toolPath, toolShown, typedValue,
} from './autonomy_settings';
import { PLACES, TURNS, change, limitRow, toolRow } from './testing/autonomyFixtures';

const refused = (status: number | undefined, body?: unknown) => Object.assign(new Error('x'), { status, body });

describe('autonomy settings: the words', () => {
  it('pins the sentences the owner reads', () => {
    expect(PREFERENCES_TEXT).toBe(
      "These are your preferences, kept with a record of each change. The server's own settings always win: they and your provider's spend limit are the limits that hold.",
    );
    expect(SOURCE_WORDS).toEqual({ owner: 'Your value', host: 'Host value', clamped: 'Lowered by the server' });
    expect(SETTING_WORDS).toEqual({ allowed: 'Allowed', denied: 'Not allowed' });
    expect(GROUPS.map((g) => g.words)).toEqual(['Reading', 'Writing', 'Starting work', 'Reaching outside', 'Tools from other servers']);
    expect(TOOL_FILTERS.map((f) => f.label)).toEqual(['All', 'Changed', 'Not allowed']);
    expect(OFF_TEXT).toContain('NOUS_AUTONOMY_SETTINGS_ENABLED=true');
    expect(GUARD_OFF_TEXT).toContain('NOUS_TOOL_GUARD_ENABLED=true');
  });

  it('a row says its host value, its range and its ceiling', () => {
    expect(hostValueText(limitRow())).toBe('Host value: 50');
    expect(rangeText(TURNS())).toBe('From 5 to 1,000 iterations');
    expect(ceilingText(limitRow())).toBeNull();
    expect(ceilingText(limitRow({ ceiling: 60 }))).toBe('The server allows at most 60');
    expect(ceilingText(limitRow({ host_value: 80, baseline: 60, value: 60, ceiling: 60, source: 'clamped' })))
      .toBe('Lowered by the server to 60');
    // the owner's value over a lowered host value still says the host's was lowered
    expect(ceilingText(limitRow({ host_value: 80, baseline: 60, value: 40, ceiling: 60, source: 'owner' })))
      .toBe('Lowered by the server to 60');
  });

  it('a saved change says what it does: raising, lowering, the same', () => {
    expect(changeText(limitRow({ value: 100 }), 50)).toBe('This lets background work run longer: up to 100 tool calls per turn.');
    expect(changeText(limitRow({ value: 20 }), 50)).toBe('This stops background turns sooner.');
    expect(changeText(limitRow({ value: 50 }), 50)).toBe(SAME_TEXT);
    expect(changeText(TURNS({ value: 1000 }), 600)).toContain('chat included');
    expect(changeText(limitRow({ key: 'some_new_limit', value: 9 }), 5)).toBe('Saved: 9 tool calls.');
    expect(resetText(limitRow({ value: 50 }), true)).toBe("Back to the host's value: 50 tool calls.");
    expect(resetText(limitRow({ value: 50 }), false)).toBe("This already uses the host's value: 50 tool calls.");
  });

  it('a change in the list, in words', () => {
    expect(changeLine(change())).toBe('Tool calls per background turn: host value to 80, by you, Nov 16 14:31 UTC');
    expect(changeLine(change({ old: 80, new: null, actor: 'owner' }))).toBe('Tool calls per background turn: 80 to host value, by you, Nov 16 14:31 UTC');
    expect(changeLine(change({ key: 'tool_guard:background:web_search', label: null, tool: 'web_search', old: null, new: 'denied' })))
      .toBe('web_search: default to Not allowed, by you, Nov 16 14:31 UTC');
  });

  it('the tool sentences', () => {
    expect(toolChangeText('web_search', 'denied')).toBe('web_search is not allowed for background work now.');
    expect(toolChangeText('web_search', 'allowed')).toBe('web_search is allowed for background work again.');
    expect(callsText(toolRow(), false)).toBe(NOT_RECORDED_TEXT);
    expect(callsText(toolRow({ calls_30d: 0 }), true)).toBe('No background calls in 30 days.');
    expect(callsText(toolRow({ calls_30d: 1, last_call: '2026-11-16T14:31:00+00:00' }), true))
      .toBe('1 background call in 30 days, the last Nov 16 14:31 UTC.');
    const row = toolRow({
      not_offered: [
        { place: 'chain_decisions', why: 'A chain keeps to a fixed set of tools that act inside Nous.' },
        { place: 'chain_work', why: 'A chain keeps to a fixed set of tools that act inside Nous.' },
        { place: 'triage', why: 'Triage may not start work or resolve decisions.' },
      ],
    });
    expect(notOfferedLines(row, PLACES)).toEqual([
      'Not offered here: Chain decisions, Work in a chain: A chain keeps to a fixed set of tools that act inside Nous.',
      'Not offered here: Heartbeat triage: Triage may not start work or resolve decisions.',
    ]);
  });

  it('the filter and the search', () => {
    const denied = toolRow({ name: 'bash', summary: 'Run a shell command.', setting: 'denied', source: 'owner' });
    const locked = toolRow({ name: 'send_email', summary: 'Send an email.', setting: 'denied', source: 'server', locked: true });
    const plain = toolRow();
    expect([denied, locked, plain].filter((r) => toolShown(r, 'changed', '')).map((r) => r.name)).toEqual(['bash']);
    expect([denied, locked, plain].filter((r) => toolShown(r, 'denied', '')).map((r) => r.name)).toEqual(['bash', 'send_email']);
    expect([denied, locked, plain].filter((r) => toolShown(r, 'all', 'SHELL')).map((r) => r.name)).toEqual(['bash']);
    expect([denied, locked, plain].filter((r) => toolShown(r, 'all', 'web')).map((r) => r.name)).toEqual(['web_search']);
  });

  it('what is sent: a whole number as a number, anything else as typed', () => {
    expect(typedValue(' 80 ')).toBe(80);
    expect(typedValue('8.5')).toBe('8.5');
    expect(typedValue('')).toBe('');
    expect(typedValue('1e3')).toBe('1e3');
    expect(limitPath('max_turns')).toBe('/autonomy/limits/max_turns');
    expect(toolPath('mcp__notes/x')).toBe('/autonomy/tools/mcp__notes%2Fx');
  });

  it('a refusal in words: each its own sentence, the server\'s own first', () => {
    expect(problemText(refused(undefined), 'see')).toBe(UNREACHABLE_TEXT);
    expect(problemText(refused(undefined), 'change')).toBe(NO_ANSWER_TEXT);
    expect(problemText(refused(403, { error: 'forbidden' }), 'change')).toBe(FORBIDDEN_TEXT);
    expect(problemText(refused(415, { error: 'Content-Type must be application/json' }), 'change')).toBe(NOT_JSON_TEXT);
    expect(problemText(refused(400, { error: 'Tool calls per background turn must be a whole number from 1 to 200 (tool calls).' }), 'change'))
      .toBe('Tool calls per background turn must be a whole number from 1 to 200 (tool calls).');
    expect(problemText(refused(409, { error: 'web_search is locked by the server' }), 'change')).toBe('web_search is locked by the server.');
    expect(problemText(refused(404), 'see')).toBe(UNSUPPORTED_TEXT);
    expect(problemText(refused(500), 'see')).toBe('The request failed (HTTP 500).');
    expect(readGate(refused(403))).toBe('forbidden');
    expect(readGate(refused(404))).toBe('unsupported');
    expect(readGate(refused(500))).toBeNull();
  });
});
