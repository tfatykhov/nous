import { describe, it, expect, vi, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import {
  ALREADY_TEXT, CORE_HELP, KINDS_HELP, MODE_HELP, MODE_WORDS, MODEL_CALLS_OFF_TEXT, NOT_REGISTERED_TEXT, OFF_TEXT,
  SAVED_TEXT, SUBTITLE, SWITCH_WORDS, TOOL_CALLS_OFF_TEXT, WEIGHED_TEXT, WINDOW_OPTIONS,
  changeLoading, draftChanged, draftList, kindLines, lastChangeText, pinnedText, problemLine, readLoading, readPath,
  sourceText, toolNumbersText, totalsText,
  type KindNumbers, type ToolRow,
} from './tool_loading';

// F120 tool loading, watch only: the card's words and requests (nous/api/tool_loading_routes.py). Every value is made up.

const kind = (over: Partial<KindNumbers> = {}): KindNumbers => ({
  key: 'chat', words: 'Chat and calls from other agents', on: true, calls: 12, read_calls: 9, written_calls: 2,
  deferred_avg: 95.5, deferred_tokens_avg: 37500, index_tokens_avg: 1800, saved_per_call: 9000, turns: 4,
  turns_used_left_out: 1, calls_used_left_out: 2, ...over,
});
const tool = (over: Partial<ToolRow> = {}): ToolRow => ({
  name: 'send_email', server: null, registered: true, tokens_est: 420, calls_30d: 7, sessions_30d: 3,
  state: 'left_out', why: null, ...over,
});

afterEach(() => { vi.unstubAllGlobals(); });

describe('tool loading: the words', () => {
  it('pins the sentences the owner reads', () => {
    expect(SUBTITLE).toContain('Watch only changes nothing that is sent');
    expect(MODE_WORDS).toEqual({ off: 'Off', watch: 'Watch only' });
    expect(MODE_HELP.watch).toBe(
      'Every tool is still sent in full. Each model call of a kind switched on below records what it would have left out.',
    );
    expect(MODE_HELP.off).toBe('Every tool is sent in full, as now, and nothing is recorded.');
    expect(SWITCH_WORDS).toEqual({ on: 'On', off: 'Off' });
    expect(OFF_TEXT).toContain('NOUS_TOOL_LOADING_ENABLED=true');
    expect(MODEL_CALLS_OFF_TEXT).toContain('NOUS_MODEL_CALL_LOG_ENABLED');
    expect(TOOL_CALLS_OFF_TEXT).toContain('NOUS_TOOL_CALL_LOG_ENABLED');
    expect(CORE_HELP).toContain('Changes to the core set are saved together with Save');
    expect(KINDS_HELP).toContain('never watched');
    expect(WEIGHED_TEXT).toContain('approximations, not prices');
    expect(NOT_REGISTERED_TEXT).toMatch(/^Not registered now\./);
    expect(SAVED_TEXT).toBe('Saved. It applies from the next model call.');
    expect(ALREADY_TEXT).toBe('That was already the saved setting.');
    expect(WINDOW_OPTIONS).toEqual([
      { value: '1', label: 'Last day' }, { value: '7', label: 'Last 7 days' }, { value: '30', label: 'Last 30 days' },
    ]);
  });

  it('a kind of call over the window, in lines', () => {
    expect(kindLines(kind())).toEqual([
      '12 calls recorded. Each would have left out 95.5 tools on average, about 37,500 tokens, listed by name in about 1,800.',
      'Weighed, that is worth about 9,000 uncached input tokens a call (9 read the tools from the cache and 2 wrote them).',
      '1 of 4 turns then used a tool that would have been left out.',
    ]);
    expect(kindLines(kind({ saved_per_call: null }))[1]).toBe('No call reported its cache state, so the saving is not weighed.');
    expect(kindLines(kind({ turns_used_left_out: null, calls_used_left_out: null }))).toHaveLength(2);
    expect(kindLines(kind({ calls: 0 }))).toEqual(['No call recorded in this window.']);
    expect(kindLines(kind({ calls: 0, on: false }))).toEqual(['Off, and no call recorded in this window.']);
    expect(kindLines(kind({ calls: 1, turns: 1, turns_used_left_out: 0 }))[0]).toMatch(/^1 call recorded\./);
  });

  it('a tool, its numbers and where it comes from', () => {
    expect(toolNumbersText(tool())).toBe('About 420 tokens. 7 calls in 3 sessions in 30 days.');
    expect(toolNumbersText(tool({ calls_30d: 1, sessions_30d: 1 }))).toBe('About 420 tokens. 1 call in 1 session in 30 days.');
    expect(toolNumbersText(tool({ calls_30d: 0, sessions_30d: 0 }))).toBe('About 420 tokens. No calls in 30 days.');
    expect(toolNumbersText(tool({ calls_30d: null, sessions_30d: null }))).toBe('About 420 tokens.');
    expect(toolNumbersText(tool({ tokens_est: null, calls_30d: null, sessions_30d: null }))).toBe('');
    expect(sourceText(tool())).toBe('From Nous');
    expect(sourceText(tool({ name: 'mcp__ha__HassTurnOn', server: 'ha' }))).toBe('From the server ha');
    expect(pinnedText(tool({ state: 'pinned', why: 'The instructions tell the model to call it by name.' })))
      .toBe('Always sent in full: The instructions tell the model to call it by name.');
  });

  it('the totals, the last change and an unreadable setting', () => {
    const totals = { tools: 110, kept: 16, left_out: 94, tokens_est: 46485, left_out_tokens_est: 38000, index_tokens_est: 1700 };
    expect(totalsText(totals)).toBe(
      'Every registered tool together: 110 tools, about 46,485 tokens. With this core set 16 would be sent in full and 94 left out (about 38,000 tokens), listed by name in about 1,700 tokens.',
    );
    expect(totalsText({ ...totals, left_out: 0 })).toBe(
      'Every registered tool together: 110 tools, about 46,485 tokens. With this core set all of them are sent in full.',
    );
    expect(totalsText({ ...totals, tools: 0 })).toBe('No tool is registered now.');
    expect(lastChangeText({ updated_at: null, updated_by: null })).toBe('Not changed yet.');
    expect(lastChangeText({ updated_at: '2026-11-24T10:05:00+00:00', updated_by: 'proxy' }))
      .toBe('Last changed from the dashboard, Nov 24 10:05 UTC.');
    expect(problemLine('the stored mode is not off or watch')).toBe(
      'The saved setting could not be read (the stored mode is not off or watch), so tool loading is off. Saving any change replaces it.',
    );
  });

  it('the core set being edited', () => {
    expect(draftList(new Set(['web_search', 'db_query']))).toEqual(['db_query', 'web_search']);
    expect(draftChanged(new Set(['a', 'b']), ['b', 'a'])).toBe(false);
    expect(draftChanged(new Set(['a']), ['a', 'b'])).toBe(true);
    expect(draftChanged(new Set(['a', 'c']), ['a', 'b'])).toBe(true);
  });
});

describe('tool loading: the requests', () => {
  it('reads with the window and changes with PUT and only the fields given', async () => {
    const calls: { url: string; method: string; body: unknown }[] = [];
    vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
      calls.push({ url, method: init.method ?? 'GET', body: typeof init.body === 'string' ? JSON.parse(init.body) : undefined });
      return new Response(JSON.stringify({ enabled: true, changed: true }), { status: 200 });
    }));
    expect(readPath(30)).toBe('/dashboard/tool-loading?days=30');
    await readLoading(7);
    await changeLoading({ kinds: { chat: true } });
    await changeLoading({ core: null });
    expect(calls).toEqual([
      { url: '/dashboard/tool-loading?days=7', method: 'GET', body: undefined },
      { url: '/dashboard/tool-loading', method: 'PUT', body: { kinds: { chat: true } } },
      { url: '/dashboard/tool-loading', method: 'PUT', body: { core: null } },
    ]);
  });

  it('the requests go through apiRequest only', () => {
    const lib = readFileSync(join(__dirname, 'tool_loading.ts'), 'utf-8');
    expect(lib).not.toMatch(/\bfetch\(/);
    expect(lib.match(/apiRequest</g)?.length).toBe(2);
  });
});
