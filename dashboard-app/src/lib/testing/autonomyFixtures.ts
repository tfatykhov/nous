// Test-only: answers of the /autonomy routes (nous/api/autonomy_routes.py) in their real shape, with made-up values.
import type { Change, LimitRow, LimitsAnswer, Place, ToolRow, ToolsAnswer } from '../autonomy_settings';

export function limitRow(over: Partial<LimitRow> = {}): LimitRow {
  return {
    key: 'subtask_tool_call_limit',
    label: 'Tool calls per background turn',
    unit: 'tool calls',
    low: 1,
    high: 200,
    value: 50,
    host_value: 50,
    baseline: 50,
    source: 'host',
    ceiling: null,
    applies: 'From the next background turn.',
    last_change: null,
    ...over,
  };
}

export const TURNS = (over: Partial<LimitRow> = {}) =>
  limitRow({
    key: 'max_turns',
    label: 'Tool-loop iterations per turn',
    unit: 'iterations',
    low: 5,
    high: 1000,
    value: 600,
    host_value: 600,
    baseline: 600,
    applies: 'From the next turn. This applies to every turn, chat included.',
    ...over,
  });

export function limitsAnswer(rows: LimitRow[] = [TURNS(), limitRow()], over: Partial<LimitsAnswer> = {}): LimitsAnswer {
  return { enabled: true, tool_guard: true, ceilings_set: false, limits: rows, ...over };
}

export function change(over: Partial<Change> = {}): Change {
  return {
    key: 'subtask_tool_call_limit',
    label: 'Tool calls per background turn',
    tool: null,
    old: null,
    new: 80,
    actor: 'proxy',
    at: '2026-11-16T14:31:00+00:00',
    ...over,
  };
}

export const PLACES: Place[] = [
  { key: 'chain_decisions', words: 'Chain decisions' },
  { key: 'chain_work', words: 'Work in a chain' },
  { key: 'tasks', words: 'Background tasks' },
  { key: 'workflow_steps', words: 'Workflow steps' },
  { key: 'schedules', words: 'Scheduled tasks' },
  { key: 'checks', words: 'Heartbeat checks' },
  { key: 'triage', words: 'Heartbeat triage' },
];

export function toolRow(over: Partial<ToolRow> = {}): ToolRow {
  return {
    name: 'web_search',
    summary: 'Search the web.',
    registered: true,
    group: 'reading',
    classes: ['Reads only', 'Brings in outside text'],
    setting: 'allowed',
    source: 'default',
    locked: false,
    not_offered: [],
    calls_30d: null,
    last_call: null,
    ...over,
  };
}

export function toolsAnswer(tools: ToolRow[], over: Partial<ToolsAnswer> = {}): ToolsAnswer {
  return { enabled: true, recorded: false, locks_set: false, places: PLACES, tools, ...over };
}
