// Test-only: answers of the /autonomy routes (nous/api/autonomy_routes.py) in their real shape, with made-up values.
import type { Change, LimitRow, LimitsAnswer, Place, ToolRow, ToolsAnswer } from '../autonomy_settings';

export function limitRow(over: Partial<LimitRow> = {}): LimitRow {
  return {
    key: 'subtask_tool_call_limit',
    label: 'Tool calls per background task',
    unit: 'tool calls',
    low: 1,
    high: 200,
    value: 50,
    host_value: 50,
    baseline: 50,
    source: 'host',
    ceiling: null,
    description:
      'How many tool calls one run of a background task may make: a spawned task, a workflow step, a scheduled task, a companion action, or one decision in a chain. Heartbeat triage, heartbeat checks and their callbacks and workflow summaries are not limited by it.',
    applies: 'From the next task that starts.',
    last_change: null,
    ...over,
  };
}

export const TURNS = (over: Partial<LimitRow> = {}) =>
  limitRow({
    key: 'max_turns',
    label: 'Rounds of tool use per turn',
    unit: 'rounds',
    low: 5,
    high: 1000,
    value: 600,
    host_value: 600,
    baseline: 600,
    description:
      'How many rounds of tool calls one turn may take before Nous must answer, in chat and in background work alike.',
    applies: 'From the next turn.',
    ...over,
  });

export function limitsAnswer(rows: LimitRow[] = [TURNS(), limitRow()], over: Partial<LimitsAnswer> = {}): LimitsAnswer {
  return { enabled: true, loaded: true, tool_guard: true, ceilings_set: false, limits: rows, ...over };
}

export const NOT_LOADED_MESSAGE =
  "The saved settings could not be read; background work is held from tools until they can. Every limit is the server's own value until then.";

export function change(over: Partial<Change> = {}): Change {
  return {
    key: 'subtask_tool_call_limit',
    label: 'Tool calls per background task',
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

/** The places the guard holds, as the server lists them (nous/autonomy/tool_guard.held_places). */
export const HELD_IN = [
  'background tasks',
  'workflow steps',
  'scheduled tasks',
  'chain decisions',
  'heartbeat checks',
  'heartbeat check callbacks',
  'heartbeat triage',
  'workflow summaries',
  'other background turns',
  'a chat or MCP turn in a session background work started',
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
  return { enabled: true, loaded: true, recorded: false, held_in: HELD_IN, locks_set: false, places: PLACES, tools, ...over };
}
