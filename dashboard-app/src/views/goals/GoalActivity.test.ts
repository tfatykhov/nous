import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, within } from '@testing-library/svelte';
import GoalActivity from './GoalActivity.svelte';
import { NOW, activityAnswer } from '../../lib/testing/goalFixtures';
import { routeHref } from '../../lib/router';
import { parseBlocks, styleBlocks } from '../../lib/testing/css';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import type { GoalActivity as Answer } from '../../lib/goals';

// What a goal is doing now: the panel over GET /goals/{ref}/activity. It reads when it is shown, when the goal
// changes, and on the page poll's tick while a session is open; never on a timer of its own.

type Reply = [number, unknown];
type Call = { url: string; signal: AbortSignal | null | undefined };
let calls: Call[] = [];
/** Each request's answer, in order; past the list, the last one again. A function is called with the request. */
let replies: (Reply | ((call: Call) => Reply | Promise<Reply>))[] = [];

function install() {
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    const call = { url, signal: init.signal };
    calls.push(call);
    const next = replies[Math.min(calls.length - 1, replies.length - 1)];
    const [status, payload] = typeof next === 'function' ? await next(call) : next;
    if (status === 0) throw new TypeError('Failed to fetch');
    return new Response(JSON.stringify(payload), { status });
  }));
}

/** A request held until `answer` is called, or rejected when its signal aborts. */
function held(): { reply: (call: Call) => Promise<Reply>; answer: (r: Reply) => void } {
  let answer!: (r: Reply) => void;
  const done = new Promise<Reply>((resolve) => { answer = resolve; });
  return {
    answer,
    reply: (call) =>
      new Promise<Reply>((resolve, reject) => {
        call.signal?.addEventListener('abort', () => reject(new DOMException('The operation was aborted.', 'AbortError')));
        void done.then(resolve);
      }),
  };
}

const GOAL = { ref: 'G-0001', state: 'active', open_session: 4, updated_at: '2026-11-15T11:00:00+00:00' };
const props = (over: Record<string, unknown> = {}) => ({ goal: { ...GOAL }, now: NOW, tick: 1, ...over });
const panel = () => document.querySelector<HTMLElement>('section[aria-labelledby="goal-v.G-0001"]');
const settle = () => new Promise((r) => setTimeout(r, 0));
/** Lets a request that was answered finish its way through the panel (the answer's body is read, then shown). */
async function landed() {
  for (let i = 0; i < 5; i++) await settle();
}

beforeEach(() => {
  calls = [];
  replies = [[200, activityAnswer()]];
  install();
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('what the panel says', () => {
  it('says it is reading until the first answer lands, as plain text', async () => {
    const hold = held();
    replies = [hold.reply];
    render(GoalActivity, props());
    expect(await screen.findByText('Reading what it is doing.')).toBeTruthy();
    expect(screen.getByRole('heading', { level: 3, name: 'What it is doing now' })).toBeTruthy();
    hold.answer([200, activityAnswer()]);
    expect(await screen.findByText('Working on a task: Merge the duplicate pairs')).toBeTruthy();
    expect(screen.queryByText('Reading what it is doing.')).toBeNull();
  });

  it('a running task: what, since when, the last call, the counts and the newest calls in words', async () => {
    render(GoalActivity, props());
    const box = await waitFor(() => panel()!);
    await within(box).findByText('Working on a task: Merge the duplicate pairs');
    expect(within(box).getByText('The task started 10 minutes ago.')).toBeTruthy();
    expect(within(box).getByText('Last tool call 2 minutes ago (bash).')).toBeTruthy();
    expect(within(box).getByText('31 tool calls this session, 2 failed.')).toBeTruthy();
    const rows = within(box).getAllByRole('listitem');
    expect(rows.map((r) => r.textContent?.replace(/\s+/g, ' ').trim())).toEqual([
      'Nov 15 11:58 UTC bash done task',
      'Nov 15 11:57 UTC read_file failed task',
      'Nov 15 11:49 UTC spawn_task done decisions',
    ]);
    expect(rows[1].classList.contains('failed')).toBe(true);
    expect(rows[0].classList.contains('failed')).toBe(false);
  });

  it('a workflow waiting for a coding job, and the jobs it started', async () => {
    replies = [[200, activityAnswer({
      phase: 'waiting',
      current: {
        kind: 'workflow', name: 'Build the export', first_task: false, status: 'running', started_at: '2026-11-15T11:20:00+00:00',
        waiting_for: { kind: 'job', job_id: 'job-20261115-112000-ab12cd34' },
      },
      jobs: { launched: 1, last_launch_at: '2026-11-15T11:20:00+00:00', capped: false },
    })]];
    render(GoalActivity, props());
    expect(await screen.findByText('Waiting for a coding job (job-20261115-112000-ab12cd34) to finish.')).toBeTruthy();
    // The start time is the workflow's, and says so: the answer has no launch time of the job waited for.
    expect(screen.getByText('The workflow started 40 minutes ago.')).toBeTruthy();
    expect(screen.queryByText(/^Started /)).toBeNull();
    expect(screen.getByText('Coding jobs started this session: 1. The last one 40 minutes ago.')).toBeTruthy();
  });

  it('a long quiet says why in place of the last-call line, and an open session with no call yet says when it started', async () => {
    replies = [[200, activityAnswer({ last_call_at: '2026-11-15T11:18:00+00:00' })]];
    render(GoalActivity, props());
    expect(await screen.findByText(
      'No tool call for 42 minutes; the last one was bash. A long command or a coding job can take this long; a call shows here when it ends.',
    )).toBeTruthy();
    expect(screen.queryByText(/^Last tool call/)).toBeNull();
    cleanup();
    replies = [[200, activityAnswer({ last_call_at: null, calls: { total: 0, errors: 0, capped: false }, recent: [], work_sessions: [] })]];
    render(GoalActivity, props());
    expect(await screen.findByText('No tool call yet in this session. It started 12 minutes ago.')).toBeTruthy();
  });

  it('with the call log off: the current work, and why no call is listed', async () => {
    replies = [[200, activityAnswer({ log_enabled: false, last_call_at: null, calls: null, jobs: null, recent: [], work_sessions: [] })]];
    render(GoalActivity, props());
    expect(await screen.findByText('Working on a task: Merge the duplicate pairs')).toBeTruthy();
    expect(screen.getByText('The tool-call log is off, so individual calls are not listed.')).toBeTruthy();
    expect(screen.queryByRole('list')).toBeNull();
  });

  it('no session yet, and an ended session in one line with its links', async () => {
    replies = [[200, activityAnswer({ session: null, phase: 'none', current: null, running_count: 0, last_call_at: null, calls: null, jobs: null, recent: [], work_sessions: [] })]];
    render(GoalActivity, props({ goal: { ...GOAL, open_session: null } }));
    expect(await screen.findByText('No session has run yet.')).toBeTruthy();
    cleanup();
    replies = [[200, activityAnswer({
      session: { n: 4, open: false, started_at: '2026-11-15T08:00:00+00:00', ended_at: '2026-11-15T09:00:00+00:00' },
      phase: 'none', current: null, running_count: 0, jobs: null, recent: [],
    })]];
    render(GoalActivity, props({ goal: { ...GOAL, open_session: null } }));
    expect(await screen.findByText('Session 4 ended 3 hours ago after 31 tool calls.')).toBeTruthy();
    expect(screen.getByRole('heading', { level: 3, name: 'Last session' })).toBeTruthy();
    expect(screen.queryByRole('list')).toBeNull();
    expect(screen.queryByText(/^Coding jobs/)).toBeNull();
    expect(screen.getAllByRole('link').length).toBe(2);
  });

  it('each link opens that session on the Work screen, and the links are separated in words', async () => {
    render(GoalActivity, props());
    const box = await waitFor(() => panel()!);
    await within(box).findByText('See this session on the Work screen:', { exact: false });
    const links = within(box).getAllByRole('link');
    expect(links.map((a) => [a.textContent?.trim(), a.getAttribute('href')])).toEqual([
      ['the task', routeHref('work', ['s', 'subtask-1a2b3c4d'])],
      ['the decisions', routeHref('work', ['s', 'intent-00000000-0000-4000-8000-0000000000bb'])],
    ]);
    expect(links[0].parentElement?.textContent).toBe('See this session on the Work screen: the task and the decisions.');
    cleanup();
    replies = [[200, activityAnswer({ work_sessions: [
      { id: 'subtask-1', kind: 'task' }, { id: 'dag-1', kind: 'workflow' }, { id: 'subtask-2', kind: 'task' }, { id: 'intent-1', kind: 'decisions' },
    ] })]];
    render(GoalActivity, props());
    const line = (await screen.findByText('See this session on the Work screen:', { exact: false })) as HTMLElement;
    expect(line.textContent).toBe('See this session on the Work screen: task 1, the workflow step, task 2 and the decisions.');
    expect(within(line).getAllByRole('link').map((a) => a.textContent)).toEqual(['task 1', 'the workflow step', 'task 2', 'the decisions']);
  });

  it('a long tool name wraps in its own column; on a phone it takes a line of its own, above the time, outcome and kind', () => {
    const css = styleBlocks(readFileSync(join(__dirname, 'GoalActivity.svelte'), 'utf-8'));
    const at = css.indexOf('@media (max-width: 768px)');
    expect(at).toBeGreaterThan(-1);
    const wide = parseBlocks(css.slice(0, at));
    // Desktop: the rows share the list's columns, so the outcome sits just after the widest tool name.
    expect(wide.get('.calls')).toMatchObject({
      display: 'grid', 'grid-template-columns': '8rem fit-content(14rem) max-content max-content minmax(0, 1fr)',
    });
    expect(wide.get('.calls > li')).toMatchObject({ 'grid-template-columns': 'subgrid', 'grid-column': '1 / -1' });
    expect(wide.get('.tool')).toMatchObject({ 'overflow-wrap': 'anywhere', 'min-width': '0' });
    const phone = parseBlocks(css.slice(at));
    expect(phone.get('.calls')).toMatchObject({ display: 'block' });
    expect(phone.get('.calls > li')).toMatchObject({ display: 'flex', 'flex-wrap': 'wrap' });
    expect(phone.get('.calls > li > .tool')).toMatchObject({ order: '-1', 'flex-basis': '100%' });
  });

  it('a failed read with an earlier answer keeps it and says so; with none it says it could not read', async () => {
    replies = [[500, { error: 'the request could not be processed' }]];
    render(GoalActivity, props());
    expect(await screen.findByText('Could not read what the goal is doing.')).toBeTruthy();
    cleanup();
    calls = [];
    replies = [[200, activityAnswer()], [0, null]];
    const { rerender } = render(GoalActivity, props());
    await screen.findByText('Working on a task: Merge the duplicate pairs');
    await rerender(props({ tick: 2 }));
    expect(await screen.findByText('Could not read what the goal is doing. What is shown is from the last read.')).toBeTruthy();
    expect(screen.getByText('Working on a task: Merge the duplicate pairs')).toBeTruthy();
  });

  it('an answer saying the panel is off, or goals are off, or that the goal is gone, shows nothing', async () => {
    for (const reply of [[200, { enabled: true, activity_enabled: false }], [200, { enabled: false }], [404, { error: 'no goal G-0001' }]] as Reply[]) {
      replies = [reply];
      calls = [];
      render(GoalActivity, props());
      await waitFor(() => expect(calls.length).toBe(1));
      await settle();
      expect(panel(), JSON.stringify(reply)).toBeNull();
      cleanup();
    }
  });

  it('names are text, never markup', async () => {
    replies = [[200, activityAnswer({ current: { ...activityAnswer().current!, name: '<b>bold</b>' } })]];
    render(GoalActivity, props());
    expect(await screen.findByText('Working on a task: <b>bold</b>')).toBeTruthy();
    expect(panel()!.querySelector('b')).toBeNull();
  });
});

describe('when the panel reads', () => {
  const activityReads = () => calls.filter((c) => c.url === '/goals/G-0001/activity').length;

  it('once when it is shown, and again on each tick while a session is open', async () => {
    const { rerender } = render(GoalActivity, props());
    await waitFor(() => expect(activityReads()).toBe(1));
    await screen.findByText('Working on a task: Merge the duplicate pairs');
    await rerender(props({ tick: 2 }));
    await waitFor(() => expect(activityReads()).toBe(2));
    await landed();
    await rerender(props({ tick: 3 }));
    await waitFor(() => expect(activityReads()).toBe(3));
  });

  it('not on a tick while no session is open, nor when the same goal arrives as a new object', async () => {
    const idle = { ...GOAL, open_session: null };
    const { rerender } = render(GoalActivity, props({ goal: idle }));
    await waitFor(() => expect(activityReads()).toBe(1));
    await screen.findByText('Working on a task: Merge the duplicate pairs');
    await landed();
    await rerender(props({ goal: { ...idle }, tick: 2 }));
    await rerender(props({ goal: { ...idle }, tick: 3 }));
    await settle();
    expect(activityReads()).toBe(1);
    await rerender(props({ goal: { ...GOAL }, tick: 1 }));
    await waitFor(() => expect(activityReads()).toBe(2)); // a session opened: read at once
    await landed();
    await rerender(props({ goal: { ...GOAL }, tick: 1 }));
    await settle();
    expect(activityReads()).toBe(2);
  });

  it('after a failed read with no session open, the next tick reads again, and once a read lands the ticks stop', async () => {
    const idle = { ...GOAL, open_session: null };
    const none = activityAnswer({ session: null, phase: 'none', current: null, running_count: 0, last_call_at: null, calls: null, jobs: null, recent: [], work_sessions: [] });
    replies = [[500, { error: 'the request could not be processed' }], [200, none]];
    const { rerender } = render(GoalActivity, props({ goal: idle }));
    expect(await screen.findByText('Could not read what the goal is doing.')).toBeTruthy();
    await landed();
    expect(activityReads()).toBe(1); // no read again until the page's poll lands
    await rerender(props({ goal: { ...idle }, tick: 2 }));
    await waitFor(() => expect(activityReads()).toBe(2));
    expect(await screen.findByText('No session has run yet.')).toBeTruthy();
    await landed();
    await rerender(props({ goal: { ...idle }, tick: 3 }));
    await landed();
    expect(activityReads()).toBe(2);
  });

  it('a tick during a read of the same goal starts no second request', async () => {
    const hold = held();
    replies = [hold.reply, [200, activityAnswer()]];
    const { rerender } = render(GoalActivity, props());
    await waitFor(() => expect(activityReads()).toBe(1));
    await rerender(props({ tick: 2 }));
    await rerender(props({ tick: 3 }));
    await settle();
    expect(activityReads()).toBe(1);
    hold.answer([200, activityAnswer()]);
    await screen.findByText('Working on a task: Merge the duplicate pairs');
    await settle();
    expect(activityReads()).toBe(1);
  });

  it('a session that ends during a read is read once that read lands', async () => {
    const hold = held();
    const ended = activityAnswer({
      session: { n: 4, open: false, started_at: '2026-11-15T11:48:00+00:00', ended_at: '2026-11-15T11:59:00+00:00' },
      phase: 'none', current: null, running_count: 0, jobs: null, recent: [],
    });
    replies = [hold.reply, [200, ended]];
    const { rerender } = render(GoalActivity, props());
    await waitFor(() => expect(activityReads()).toBe(1));
    await rerender(props({ goal: { ...GOAL, open_session: null, updated_at: '2026-11-15T11:59:00+00:00' }, tick: 2 }));
    await settle();
    expect(activityReads()).toBe(1);
    hold.answer([200, activityAnswer()]);
    await waitFor(() => expect(activityReads()).toBe(2));
    expect(await screen.findByText('Session 4 ended 1 minute ago after 31 tool calls.')).toBeTruthy();
  });

  it('another goal: the old read is given up and its late answer never shown', async () => {
    const hold = held();
    replies = [hold.reply, [200, activityAnswer({ current: { ...activityAnswer().current!, name: 'Sort the photos' } })]];
    const { rerender } = render(GoalActivity, props());
    await waitFor(() => expect(calls.length).toBe(1));
    const first = calls[0];
    await rerender(props({ goal: { ...GOAL, ref: 'G-0002' } }));
    await waitFor(() => expect(calls.length).toBe(2));
    expect(first.signal?.aborted).toBe(true);
    expect(calls[1].url).toBe('/goals/G-0002/activity');
    expect(await screen.findByText('Working on a task: Sort the photos')).toBeTruthy();
    hold.answer([200, activityAnswer()]);
    await settle();
    expect(screen.queryByText('Working on a task: Merge the duplicate pairs')).toBeNull();
  });

  it('after it is gone, its read is given up and nothing starts another', async () => {
    const hold = held();
    replies = [hold.reply];
    const { unmount } = render(GoalActivity, props());
    await waitFor(() => expect(calls.length).toBe(1));
    unmount();
    expect(calls[0].signal?.aborted).toBe(true);
    await settle();
    expect(calls.length).toBe(1);
  });
});
