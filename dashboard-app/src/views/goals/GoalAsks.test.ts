import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/svelte';
import { tick } from 'svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import GoalAsks from './GoalAsks.svelte';
import { awaitingProposal, awaitingQuestion } from '../../lib/testing/goalFixtures';
import { parseBlocks, styleBlocks } from '../../lib/testing/css';
import type { Awaiting } from '../../lib/goals';

// What a goal's sessions wait on the owner for: each approval request with Approve and Reject, each question with a
// field and Send, over the routes the Telegram bot calls. The answers have the routes' shapes
// (nous/api/intention_routes.py); every value is made up.

type Reply = [number, unknown];
type Call = { url: string; method: string; body: unknown };
let calls: Call[] = [];
/** The answer to each request; a string payload is sent as it is (a gateway's page). */
let reply: (call: Call) => Reply | Promise<Reply> = () => [200, {}];

function install() {
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    const call = { url, method: init.method ?? 'GET', body: typeof init.body === 'string' ? JSON.parse(init.body) : undefined };
    calls.push(call);
    const [status, payload] = await reply(call);
    if (status === 0) throw new TypeError('Failed to fetch');
    return new Response(typeof payload === 'string' ? payload : JSON.stringify(payload), { status });
  }));
}

/** A request held until `answer` is called. */
function held(): { reply: () => Promise<Reply>; answer: (r: Reply) => void } {
  let answer!: (r: Reply) => void;
  const done = new Promise<Reply>((resolve) => { answer = resolve; });
  return { answer, reply: () => done };
}

const P = awaitingProposal();
const Q = awaitingQuestion();
const DECIDE = `/intentions/proposals/${P.id}/decide`;
const ANSWER = `/intentions/questions/${Q.id}/answer`;
const decided = (state: string, changed = true) => ({
  proposal_id: P.id, short_id: P.short_id, state, result: 'the tool said something', error: null, changed, woke: false,
});
const RECORDED = { question_id: Q.id, arrival_id: '00000000-0000-4000-8000-0000000000cc', woke: true };

let onChanged = vi.fn(async () => {});
const awaiting = (over: Partial<Awaiting> = {}): Awaiting => ({ proposals: [P], questions: [Q], ...over });
const props = (a: Awaiting | null = awaiting()) => ({ goal: { ref: 'G-0001', awaiting: a }, onChanged });
const button = (name: string | RegExp) => screen.getByRole('button', { name }) as HTMLButtonElement;
const field = () => screen.getByLabelText('Your answer') as HTMLTextAreaElement;
const live = () => document.querySelector('.sr-only[role="status"]');
const settle = () => new Promise((r) => setTimeout(r, 0));
const posts = () => calls.filter((c) => c.method === 'POST');

beforeEach(() => {
  calls = [];
  reply = () => [200, {}];
  onChanged = vi.fn(async () => {});
  install();
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('what the block shows', () => {
  it('nothing, with nothing waiting; the live region is there and silent', () => {
    const { container } = render(GoalAsks, props(awaiting({ proposals: [], questions: [] })));
    expect(container.querySelector('section')).toBeNull();
    expect(screen.queryByRole('button')).toBeNull();
    expect(live()?.textContent).toBe('');
  });

  it('an approval request: the tool, why, the call exactly as it will run, the expiry, Approve and Reject', () => {
    render(GoalAsks, props(awaiting({ questions: [] })));
    const box = screen.getByRole('region', { name: 'Waiting for you' });
    expect(within(box).getByText('1 approval request').classList.contains('status-pill--warn')).toBe(true);
    expect(within(box).getByText('Nous asks to run send_email')).toBeTruthy();
    expect(within(box).getByText('The owner asked me to share the snow report.')).toBeTruthy();
    const call = box.querySelector('pre')!;
    expect(call.textContent).toBe(P.arguments);
    expect(within(box).getByText('Expires Nov 16 09:00 UTC. If you do nothing, it is rejected.')).toBeTruthy();
    expect(button('Approve').disabled).toBe(false);
    expect(button('Reject').disabled).toBe(false);
    expect(live()?.textContent).toBe('Waiting for your answer: 1 approval request.');
  });

  it('a question: its text, until when it can be answered, a field and Send', () => {
    render(GoalAsks, props(awaiting({ proposals: [] })));
    expect(screen.getByText('Nous asks')).toBeTruthy();
    expect(screen.getByText('Which folder holds the newest notes?')).toBeTruthy();
    expect(screen.getByText('You can answer until Nov 16 10:30 UTC.')).toBeTruthy();
    expect(field().maxLength).toBe(8000);
    expect(button('Send').disabled).toBe(true); // nothing typed yet
    expect(screen.queryByText('Write an answer first.')).toBeNull();
  });

  it('both: the count says each, in words', () => {
    render(GoalAsks, props(awaiting({ proposals: [P, awaitingProposal({ id: 'aa11bb22-0000-4000-8000-000000000003', short_id: 'aa11bb22' })] })));
    expect(screen.getByText('2 approval requests and 1 question')).toBeTruthy();
    expect(live()?.textContent).toBe('Waiting for your answer: 2 approval requests and 1 question.');
  });

  it('the call, the reason and the question are text, never markup', () => {
    render(GoalAsks, props({
      proposals: [awaitingProposal({ arguments: '{\n  "subject": "<b>bold</b>"\n}', why: 'Because <i>it</i> helps.' })],
      questions: [awaitingQuestion({ text: 'Use the <b>2024</b> folder?' })],
    }));
    const box = screen.getByRole('region', { name: 'Waiting for you' });
    expect(box.querySelector('pre')!.textContent).toContain('"subject": "<b>bold</b>"');
    expect(within(box).getByText('Because <i>it</i> helps.')).toBeTruthy();
    expect(within(box).getByText('Use the <b>2024</b> folder?')).toBeTruthy();
    expect(box.querySelector('b, i')).toBeNull();
  });

  it("an expiry long past by this browser's clock disables nothing and says nothing: the server decides", () => {
    render(GoalAsks, props({
      proposals: [awaitingProposal({ expires_at: '2020-01-01T00:00:00+00:00' })],
      questions: [awaitingQuestion({ expires_at: '2020-01-01T00:00:00+00:00' })],
    }));
    expect(button('Approve').disabled).toBe(false);
    expect(field().disabled).toBe(false);
    expect(screen.queryByText(/expired/)).toBeNull();
  });

  it('a read that failed says so; what was shown stays, usable', async () => {
    render(GoalAsks, props(null));
    expect(screen.getByText('Could not read what is waiting for you.')).toBeTruthy();
    cleanup();
    const shown = render(GoalAsks, props());
    await shown.rerender(props(null));
    expect(screen.getByText('Could not read what is waiting for you.')).toBeTruthy();
    expect(button('Approve').disabled).toBe(false);
    expect(screen.queryByText(/no longer waiting/)).toBeNull();
  });
});

describe('answering', () => {
  it('Approve sends the decision as the bot does, says what happened, and the page reads again once', async () => {
    reply = () => [200, decided('executed')];
    render(GoalAsks, props());
    await fireEvent.click(button('Approve'));
    expect(await screen.findByText('Approved and executed (cfd2afea).')).toBeTruthy();
    expect(posts()).toEqual([{ url: DECIDE, method: 'POST', body: { decision: 'approve', actor: 'dashboard' } }]);
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Reject' })).toBeNull();
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(screen.queryByText('the tool said something')).toBeNull();
    expect(live()?.textContent).toBe('Waiting for your answer: 1 question.');
  });

  it('Reject sends a reject', async () => {
    reply = () => [200, decided('rejected')];
    render(GoalAsks, props());
    await fireEvent.click(button('Reject'));
    expect(await screen.findByText('Rejected (cfd2afea).')).toBeTruthy();
    expect(posts()[0].body).toEqual({ decision: 'reject', actor: 'dashboard' });
  });

  it.each([
    ['failed', 'Approved, but the call failed (cfd2afea). Nothing was retried.'],
    ['executing', 'Already running (cfd2afea).'],
    ['approved', 'Approved (cfd2afea).'],
    ['something new', 'Done (cfd2afea).'],
  ])('an approve that answers %s says: %s', async (state, sentence) => {
    reply = () => [200, decided(state, false)];
    render(GoalAsks, props());
    await fireEvent.click(button('Approve'));
    expect(await screen.findByText(sentence)).toBeTruthy();
  });

  it('Send sends the text as the bot does; the answer is recorded and the field goes', async () => {
    reply = () => [200, RECORDED];
    render(GoalAsks, props(awaiting({ proposals: [] })));
    await fireEvent.input(field(), { target: { value: 'The 2024 folder.' } });
    await fireEvent.click(button('Send'));
    expect(await screen.findByText('Answer recorded.')).toBeTruthy();
    expect(posts()).toEqual([{ url: ANSWER, method: 'POST', body: { text: 'The 2024 folder.', actor: 'dashboard' } }]);
    expect(screen.queryByLabelText('Your answer')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Send' })).toBeNull();
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  const EXPIRED = 'That proposal expired before it was decided, so it did not run.';
  it.each([
    ['409 expired', 409, { error: EXPIRED, state: 'expired', refusal: 'expired' }, EXPIRED, true],
    ['409 ended', 409, { error: 'That work has already ended, so nothing ran.', state: 'cancelled', refusal: 'ended' },
      'That work has already ended, so nothing ran.', true],
    ['409 decided the other way', 409, { error: 'That proposal was already decided the other way.', state: 'rejected', refusal: 'not_pending' },
      'That proposal was already decided the other way.', true],
    ['409 with no sentence', 409, {}, 'That proposal can no longer be decided.', true],
    ['404', 404, { error: 'no such proposal' }, 'That proposal is no longer available.', true],
    ['403', 403, { error: 'forbidden' }, 'Your sign-in is needed to change goals. Open the dashboard through the login page.', false],
    ['415', 415, { error: 'Content-Type must be application/json' }, 'The request was not sent as JSON, so it was refused.', false],
    ['503', 503, { error: 'continuation is not running' }, 'Nous is not running its follow-up work.', false],
  ])('a decision refused with %s says so; the buttons stay only when it can still be decided', async (_name, status, body, sentence, final) => {
    reply = () => [status as number, body];
    render(GoalAsks, props());
    await fireEvent.click(button('Approve'));
    expect(await screen.findByText(sentence as string)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Approve' }) === null).toBe(final);
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['409 answered', 409, { error: 'That question was already answered.', reason: 'answered' }, 'That question was already answered.', true],
    ['409 expired', 409, { error: 'That question expired before it was answered.', reason: 'expired' },
      'That question expired before it was answered.', true],
    ['409 ended', 409, { error: 'That work has already ended, so your answer was not recorded.', reason: 'ended' },
      'That work has already ended, so your answer was not recorded.', true],
    ['404', 404, { error: 'no such question' }, 'That question is no longer available.', true],
    ['400', 400, { error: 'text is required (at most 8000 characters)' },
      'The answer could not be read as text. Remove any unusual characters and send it again.', false],
    ['503', 503, { error: 'continuation is not running' }, 'Nous is not running its follow-up work.', false],
  ])('an answer refused with %s says so; the text typed is kept', async (_name, status, body, sentence, final) => {
    reply = () => [status as number, body];
    render(GoalAsks, props(awaiting({ proposals: [] })));
    await fireEvent.input(field(), { target: { value: 'The 2024 folder.' } });
    await fireEvent.click(button('Send'));
    expect(await screen.findByText(sentence as string)).toBeTruthy();
    if (final) {
      const kept = screen.getByLabelText('What you wrote (not sent)') as HTMLTextAreaElement;
      expect(kept.readOnly).toBe(true);
      expect(kept.value).toBe('The 2024 folder.');
      expect(screen.queryByRole('button', { name: 'Send' })).toBeNull();
    } else {
      expect(field().value).toBe('The 2024 folder.');
      expect(button('Send').disabled).toBe(false);
    }
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['no answer at all', 0, null],
    ["a gateway's 502 page", 502, '<html>Bad gateway</html>'],
    ["a gateway's 504 page", 504, '<html>Gateway timeout</html>'],
  ])('a lost answer (%s) says to check, keeps the buttons and does not read again', async (_name, status, payload) => {
    reply = () => [status as number, payload];
    render(GoalAsks, props());
    await fireEvent.click(button('Approve'));
    expect(await screen.findByText('No answer came back. Check the goal before trying again.')).toBeTruthy();
    expect(button('Approve').disabled).toBe(false);
    expect(onChanged).not.toHaveBeenCalled();
  });

  it('a 500 keeps its sentence and the buttons, and counts as a lost answer: the decision may have been stored', async () => {
    reply = () => [500, { error: 'the decision could not be processed' }];
    const view = render(GoalAsks, props());
    await fireEvent.click(button('Approve'));
    expect(await screen.findByText('The decision could not be processed.')).toBeTruthy();
    expect(button('Approve').disabled).toBe(false);
    expect(onChanged).not.toHaveBeenCalled();
    await view.rerender(props(awaiting({ proposals: [] })));
    expect(screen.getByText("No answer came back, and this is no longer waiting. Check the goal's journal for what happened.")).toBeTruthy();
    expect(screen.queryByText(/answered somewhere else/)).toBeNull();
  });

  it('a double click sends once; both buttons wait while it is on its way', async () => {
    const hold = held();
    reply = hold.reply;
    render(GoalAsks, props());
    const approve = button('Approve');
    await fireEvent.click(approve);
    await fireEvent.click(approve);
    await fireEvent.click(button('Reject'));
    expect(posts().length).toBe(1);
    expect(button('Approving…').disabled).toBe(true);
    expect(button('Reject').disabled).toBe(true);
    expect(field().disabled).toBe(false); // the question is another item
    hold.answer([200, decided('executed')]);
    expect(await screen.findByText('Approved and executed (cfd2afea).')).toBeTruthy();
    expect(posts().length).toBe(1);
  });

  it('Send waits for text: a hint once the field was emptied, and another at the limit', async () => {
    render(GoalAsks, props(awaiting({ proposals: [] })));
    await fireEvent.input(field(), { target: { value: 'x' } });
    expect(button('Send').disabled).toBe(false);
    await fireEvent.input(field(), { target: { value: '   ' } });
    expect(button('Send').disabled).toBe(true);
    expect(screen.getByText('Write an answer first.')).toBeTruthy();
    await fireEvent.input(field(), { target: { value: 'y'.repeat(8000) } });
    expect(screen.getByText('At most 8,000 characters.')).toBeTruthy();
    await fireEvent.click(button('Send'));
    await waitFor(() => expect(posts().length).toBe(1));
  });

  it('text typed survives a poll that sends the same question again', async () => {
    const view = render(GoalAsks, props(awaiting({ proposals: [] })));
    await fireEvent.input(field(), { target: { value: 'Half an answer' } });
    await view.rerender(props(awaiting({ proposals: [], questions: [{ ...Q }] })));
    await view.rerender(props(null));
    await view.rerender(props(awaiting({ proposals: [], questions: [{ ...Q }] })));
    expect(field().value).toBe('Half an answer');
  });
});

describe('when an item leaves the list', () => {
  it('one being answered stays as it is, then shows its answer', async () => {
    const hold = held();
    reply = hold.reply;
    const view = render(GoalAsks, props());
    await fireEvent.click(button('Approve'));
    await view.rerender(props(awaiting({ proposals: [] }))); // the poll no longer lists it: it is running
    expect(button('Approving…').disabled).toBe(true);
    expect(screen.queryByText(/no longer waiting/)).toBeNull();
    hold.answer([200, decided('executed')]);
    expect(await screen.findByText('Approved and executed (cfd2afea).')).toBeTruthy();
    expect(screen.queryByText(/no longer waiting/)).toBeNull();
  });

  it('one answered here keeps its line after it leaves', async () => {
    reply = () => [200, decided('rejected')];
    const view = render(GoalAsks, props());
    await fireEvent.click(button('Reject'));
    await screen.findByText('Rejected (cfd2afea).');
    await view.rerender(props(awaiting({ proposals: [] })));
    expect(screen.getByText('Rejected (cfd2afea).')).toBeTruthy();
    expect(screen.queryByText(/no longer waiting/)).toBeNull();
  });

  it('one that leaves with no press from here says why it went, where it was, with no buttons', async () => {
    const view = render(GoalAsks, props());
    await view.rerender(props(awaiting({ proposals: [] })));
    expect(screen.getByText('This is no longer waiting for you. It was answered somewhere else, expired, or its work ended.')).toBeTruthy();
    expect(screen.getByText('Nous asks to run send_email')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull();
    expect(live()?.textContent).toBe('Waiting for your answer: 1 question.');
  });

  it('one whose answer was lost, and that then leaves, says to check the journal', async () => {
    reply = () => [0, null];
    const view = render(GoalAsks, props());
    await fireEvent.click(button('Approve'));
    await screen.findByText('No answer came back. Check the goal before trying again.');
    await view.rerender(props(awaiting({ proposals: [] })));
    expect(screen.getByText("No answer came back, and this is no longer waiting. Check the goal's journal for what happened.")).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull();
  });

  it('a question whose Send got no answer, and that then leaves, keeps the text and does not call it unsent', async () => {
    reply = () => [0, null];
    const view = render(GoalAsks, props(awaiting({ proposals: [] })));
    await fireEvent.input(field(), { target: { value: 'The 2024 folder.' } });
    await fireEvent.click(button('Send'));
    await screen.findByText('No answer came back. Check the goal before trying again.');
    await view.rerender(props(awaiting({ proposals: [], questions: [] })));
    const kept = screen.getByLabelText('What you wrote') as HTMLTextAreaElement;
    expect(kept.value).toBe('The 2024 folder.');
    expect(kept.readOnly).toBe(true);
    expect(screen.queryByLabelText('What you wrote (not sent)')).toBeNull();
    expect(screen.getByText("No answer came back, and this is no longer waiting. Check the goal's journal for what happened.")).toBeTruthy();
  });

  it('a second Send after a lost one, refused as already answered, does not call the text unsent', async () => {
    reply = () => [0, null];
    render(GoalAsks, props(awaiting({ proposals: [] })));
    await fireEvent.input(field(), { target: { value: 'The 2024 folder.' } });
    await fireEvent.click(button('Send'));
    await screen.findByText('No answer came back. Check the goal before trying again.');
    reply = () => [409, { error: 'That question was already answered.', reason: 'answered' }];
    await fireEvent.click(button('Send'));
    expect(await screen.findByText('That question was already answered.')).toBeTruthy();
    expect((screen.getByLabelText('What you wrote') as HTMLTextAreaElement).value).toBe('The 2024 folder.');
    expect(screen.queryByLabelText('What you wrote (not sent)')).toBeNull();
  });

  it('a question that leaves while text is typed keeps the text, to copy', async () => {
    const view = render(GoalAsks, props(awaiting({ proposals: [] })));
    await fireEvent.input(field(), { target: { value: 'Half an answer' } });
    await view.rerender(props(awaiting({ proposals: [], questions: [] })));
    const kept = screen.getByLabelText('What you wrote (not sent)') as HTMLTextAreaElement;
    expect(kept.value).toBe('Half an answer');
    expect(kept.readOnly).toBe(true);
    expect(screen.getByText(/^This is no longer waiting for you\./)).toBeTruthy();
  });

  it('a new item shows up on the next read, at the end, and the live region says what waits', async () => {
    const view = render(GoalAsks, props(awaiting({ questions: [] })));
    await view.rerender(props(awaiting()));
    const items = screen.getAllByRole('listitem');
    expect(items.map((li) => li.querySelector('.lead')?.textContent)).toEqual(['Nous asks to run send_email', 'Nous asks']);
    expect(live()?.textContent).toBe('Waiting for your answer: 1 approval request and 1 question.');
  });
});

describe('the line that stays in view', () => {
  type Watch = { callback: (entries: { isIntersecting: boolean }[]) => void; target: Element | null };
  let watches: Watch[] = [];
  beforeEach(() => {
    watches = [];
    vi.stubGlobal('IntersectionObserver', class {
      w: Watch;
      constructor(callback: Watch['callback']) { this.w = { callback, target: null }; watches.push(this.w); }
      observe(target: Element) { this.w.target = target; }
      disconnect() {}
    });
  });

  it('shows what waits once the card is scrolled out of view; Show brings the card back', async () => {
    const { container } = render(GoalAsks, props());
    await settle();
    expect(container.querySelector('.pin')).toBeNull();
    const card = screen.getByRole('region', { name: 'Waiting for you' });
    expect(watches.at(-1)?.target).toBe(card);
    watches.at(-1)!.callback([{ isIntersecting: false }]);
    await tick();
    const pin = container.querySelector('.pin')!;
    expect(pin.textContent).toContain('Waiting for your answer: 1 approval request and 1 question.');
    // A sibling of the card, not inside it: it sticks for the length of the pane.
    expect(pin.parentElement).toBe(card.parentElement);
    await fireEvent.click(within(pin as HTMLElement).getByRole('button', { name: 'Show' }));
    expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Waiting for you' }));
    watches.at(-1)!.callback([{ isIntersecting: true }]);
    await tick();
    expect(container.querySelector('.pin')).toBeNull();
  });

  it('sticks to the top with no height of its own, so showing it moves nothing', () => {
    const css = parseBlocks(styleBlocks(readFileSync(join(__dirname, 'GoalAsks.svelte'), 'utf-8')));
    expect(css.get('.pin')).toMatchObject({ position: 'sticky', height: '0' });
  });

  it("sticks below the phone's fixed top bar", () => {
    const source = readFileSync(join(__dirname, 'GoalAsks.svelte'), 'utf-8');
    expect(source).toMatch(/@media \(max-width: 768px\) \{ \.pin \{ top: 3\.75rem; \} \}/);
    expect(readFileSync(join(__dirname, '..', '..', 'App.svelte'), 'utf-8')).toMatch(/height: 3\.25rem/);
  });
});
