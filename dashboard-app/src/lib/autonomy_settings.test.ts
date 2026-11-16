import { describe, it, expect } from 'vitest';
import {
  ACTOR_WORDS, ALREADY_SETTING_TEXT, ALREADY_TEXT, FORBIDDEN_TEXT, GROUPS, NOT_JSON_TEXT,
  NOT_LISTED_TEXT, NOT_RECORDED_TEXT, NO_ANSWER_TEXT, OFF_TEXT, PREFERENCES_TEXT, READING_TEXT,
  SAME_TEXT, SETTING_WORDS, TOOL_FILTERS, UNREACHABLE_TEXT, UNSUPPORTED_TEXT, GUARD_OFF_TEXT,
  callsText, changeLine, changeText, classesText, factsText, heldText, limitPath, listWords, lockedHeldText, logPath,
  notHeldText, notOfferedLines,
  problemText, rangeText, readAtText, readGate, readStateText, resetText, toolChangeText, toolPath, toolShown,
  typedValue,
} from './autonomy_settings';
import { HELD_IN, LOCKED_ALSO_IN, NOT_HELD, PLACES, TURNS, change, limitRow, toolRow } from './testing/autonomyFixtures';

const refused = (status: number | undefined, body?: unknown) => Object.assign(new Error('x'), { status, body });

describe('autonomy settings: the words', () => {
  it('pins the sentences the owner reads', () => {
    // Nothing in Nous reads a provider's limit, so the card never leans on one.
    expect(PREFERENCES_TEXT).not.toMatch(/provider|spend/i);
    expect(PREFERENCES_TEXT).toBe(
      "You can set each limit within the range shown, and each change is recorded. Where the server sets a ceiling, the ceiling wins, and a tool the server locks stays off.",
    );
    // Nothing on the screen says the server's own settings always win: a limit may go above the server's value.
    expect(PREFERENCES_TEXT).not.toMatch(/always win/);
    expect(SETTING_WORDS).toEqual({ allowed: 'Allowed', denied: 'Not allowed' });
    expect(ACTOR_WORDS).toEqual({ proxy: 'from the dashboard', owner: "with the owner's secret" });
    expect(GROUPS.map((g) => g.words)).toEqual(['Reading', 'Writing', 'Starting work', 'Reaching outside', 'Tools from other servers']);
    expect(TOOL_FILTERS.map((f) => f.label)).toEqual(['All', 'Changed', 'Not allowed']);
    expect(OFF_TEXT).toContain('NOUS_AUTONOMY_SETTINGS_ENABLED=true');
    expect(GUARD_OFF_TEXT).toContain('NOUS_TOOL_GUARD_ENABLED=true');
    expect(NOT_LISTED_TEXT).toBe(
      "A tool of a server that is not connected now is not listed, so it cannot be switched off ahead of time, and a newly added server's tools start Allowed.",
    );
    expect(ALREADY_TEXT).toBe('That was already the saved value.');
    expect(ALREADY_SETTING_TEXT).toBe('That was already the saved setting.');
  });

  it('where a value comes from, in one statement each', () => {
    expect(factsText(limitRow())).toBe("Server's value: 50.");
    expect(factsText(limitRow({ ceiling: 60 }))).toBe("Server's value: 50. Ceiling: 60.");
    expect(factsText(limitRow({ host_value: 600, baseline: 300, value: 300, ceiling: 300, source: 'clamped' })))
      .toBe("Server's value 600, lowered to its ceiling of 300.");
    // a host value equal to its ceiling was not lowered
    expect(factsText(limitRow({ host_value: 60, baseline: 60, value: 60, ceiling: 60 }))).toBe("Server's value: 60. Ceiling: 60.");
    expect(factsText(limitRow({ value: 80, source: 'owner' }))).toBe("Your value. The server's value is 50.");
    expect(factsText(limitRow({ value: 100, source: 'owner', ceiling: 120 }))).toBe("Your value. The server's value is 50. Ceiling: 120.");
    expect(factsText(limitRow({ host_value: 600, baseline: 300, value: 200, ceiling: 300, source: 'owner' })))
      .toBe("Your value. The server's value is 600, lowered to its ceiling of 300.");
    for (const row of [limitRow(), limitRow({ ceiling: 60 }), limitRow({ value: 80, source: 'owner' })]) {
      // one statement each: no word said twice
      expect(factsText(row).match(/value/g)?.length ?? 0).toBeLessThanOrEqual(2);
      expect(factsText(row)).not.toMatch(/Host value/);
    }
    expect(rangeText(TURNS())).toBe('From 5 to 1,000 rounds');
  });

  it('a saved change says what it does: raising, lowering, the same', () => {
    expect(changeText(limitRow({ value: 100 }), 50)).toBe('This lets background tasks do more: up to 100 tool calls per run.');
    expect(changeText(limitRow({ value: 20 }), 50)).toBe('This stops background tasks sooner.');
    expect(changeText(limitRow({ value: 50 }), 50)).toBe(SAME_TEXT);
    expect(changeText(TURNS({ value: 1000 }), 600)).toContain('chat included');
    expect(changeText(limitRow({ key: 'some_new_limit', value: 9 }), 5)).toBe('Saved: 9 tool calls.');
    expect(resetText(limitRow({ value: 50 }), true)).toBe("Back to the server's value: 50 tool calls.");
    expect(resetText(limitRow({ value: 50 }), false)).toBe("This already uses the server's value: 50 tool calls.");
  });

  it('a change in the list, in words, with who made it', () => {
    expect(changeLine(change())).toBe("Tool calls per background task: the server's value to 80, from the dashboard, Nov 16 14:31 UTC");
    expect(changeLine(change({ old: 80, new: null, actor: 'owner' })))
      .toBe("Tool calls per background task: 80 to the server's value, with the owner's secret, Nov 16 14:31 UTC");
    expect(changeLine(change({ key: 'tool_guard:background:web_search', label: null, tool: 'web_search', old: null, new: 'denied' })))
      .toBe('web_search: default to Not allowed, from the dashboard, Nov 16 14:31 UTC');
  });

  it('lists and classes in plain words, not symbols', () => {
    expect(listWords(['a'])).toBe('a');
    expect(listWords(['a', 'b'])).toBe('a and b');
    expect(listWords(['a', 'b', 'c'])).toBe('a, b, and c');
    expect(classesText(['Reads only', 'Brings in outside text'])).toBe('Reads only, and brings in outside text.');
    expect(classesText(['Changes things in Nous', 'Starts other work', 'Brings in outside text']))
      .toBe('Changes things in Nous, starts other work, and brings in outside text.');
    expect(classesText(['Reaches outside Nous'])).toBe('Reaches outside Nous.');
    expect(classesText([])).toBe('');
    expect(classesText(['Reads only', 'Brings in outside text'])).not.toMatch(/·/);
  });

  it('where Not allowed applies comes from the server\'s own list', () => {
    expect(heldText(HELD_IN)).toBe(
      'Not allowed applies to background tasks, workflow steps, scheduled tasks, chain decisions, heartbeat checks, heartbeat check callbacks, heartbeat triage, workflow summaries, other background turns, and a chat or MCP turn in a session background work started.',
    );
    expect(heldText(['scheduled tasks', 'chain decisions'])).toBe('Not allowed applies to scheduled tasks and chain decisions.');
    expect(heldText([])).toBe('');
  });

  it('where Not allowed does not apply, and where a locked tool is off, come from the server\'s own lists', () => {
    expect(notHeldText(NOT_HELD)).toBe(
      'It does not apply to chat in an ordinary session, an MCP call from another agent in an ordinary session, a tap on a companion card, and an action you approved.',
    );
    expect(notHeldText(['chat'])).toBe('It does not apply to chat.');
    expect(notHeldText([])).toBe('');
    expect(lockedHeldText(LOCKED_ALSO_IN)).toBe(
      'A tool the server locks is off in the same places, and also for a tap on a companion card and an action you approved.',
    );
    expect(lockedHeldText([])).toBe('A tool the server locks is off in the same places.');
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
    expect(logPath('limits')).toBe('/autonomy/log?kind=limits');
    expect(logPath('tools')).toBe('/autonomy/log?kind=tools');
  });

  it('a refusal in words: each its own sentence, the server\'s own first', () => {
    expect(problemText(refused(undefined), 'see')).toBe(UNREACHABLE_TEXT);
    expect(problemText(refused(undefined), 'change')).toBe(NO_ANSWER_TEXT);
    expect(problemText(refused(403, { error: 'forbidden' }), 'change')).toBe(FORBIDDEN_TEXT);
    expect(problemText(refused(415, { error: 'Content-Type must be application/json' }), 'change')).toBe(NOT_JSON_TEXT);
    expect(problemText(refused(400, { error: 'Tool calls per background task must be a whole number from 1 to 200 (tool calls).' }), 'change'))
      .toBe('Tool calls per background task must be a whole number from 1 to 200 (tool calls).');
    expect(problemText(refused(409, { error: 'web_search is locked by the server' }), 'change')).toBe('web_search is locked by the server.');
    expect(problemText(refused(503, { error: 'The saved settings could not be read, so nothing can be changed until they can.' }), 'change'))
      .toBe('The saved settings could not be read, so nothing can be changed until they can.');
    expect(problemText(refused(500, { error: 'The request could not be processed.' }), 'change')).toBe('The request could not be processed.');
    expect(problemText(refused(404), 'see')).toBe(UNSUPPORTED_TEXT);
    expect(problemText(refused(500), 'see')).toBe('The request failed (HTTP 500).');
    expect(readGate(refused(403))).toBe('forbidden');
    expect(readGate(refused(404))).toBe('unsupported');
    expect(readGate(refused(500))).toBeNull();
  });

  it('the read line says what happened: reading, the failure, or when the last good read was', () => {
    const at = Date.UTC(2026, 10, 16, 9, 5);
    expect(readAtText(at)).toBe('Read at 09:05 UTC.');
    const state = (over: object) => ({ loading: false, error: null, lastUpdated: null, data: null, ...over });
    expect(readStateText(state({ loading: true }), 'limits')).toBe(READING_TEXT);
    expect(readStateText(state({ lastUpdated: at, data: {} }), 'limits')).toBe('Read at 09:05 UTC.');
    expect(readStateText(state({ error: refused(undefined) }), 'limits')).toBe('Could not read the limits: Could not reach the server.');
    expect(readStateText(state({ error: refused(500), data: {}, lastUpdated: at }), 'tools'))
      .toBe('Could not read the tools: The request failed (HTTP 500). What is shown is from the last read.');
    // never the shared badge's words
    for (const over of [{ loading: true }, { lastUpdated: at, data: {} }, { error: refused(500) }]) {
      expect(readStateText(state(over), 'limits')).not.toMatch(/retrying|updated|ago/);
    }
  });
});
