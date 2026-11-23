import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/svelte';
import GoalPrompt from './GoalPrompt.svelte';
import {
  BLANK, CONFIRM_RESET, CONFIRM_SAVE, RESET_DONE, SAVED, SHOWING_DEFAULT, UNSAVED, savedLine, type PromptState,
} from '../../lib/goal_prompt';

// The card "Default goal prompt". The fetch stand-in answers the GET with `first` and each change with the next of
// `replies` (else the state the change asked for). A change can be held open with `hold` to see the card while it is
// sent. Dates are fixed UTC strings.

const DEFAULT = '- A goal session is a chain.\n- Aim to close at least one plan step each session.';
const MINE = 'Work in small steps and say what each one showed.';
const AT = '2026-11-20T09:05:00Z';

const STATE = (over: Partial<PromptState> = {}): PromptState => ({
  enabled: true,
  text: DEFAULT,
  is_default: true,
  default: DEFAULT,
  max_chars: 1500,
  updated_at: null,
  updated_by: null,
  ...over,
});
const SAVED_STATE = (text = MINE) => STATE({ text, is_default: false, updated_at: AT, updated_by: 'owner' });

interface Reply { status: number; body: unknown }
let first: unknown;
let failRead: boolean;
let replies: Reply[];
let sent: { method: string; url: string; body: unknown }[];
let asked: string[];
let answer: boolean;
let hold: Promise<void> | null;

beforeEach(() => {
  first = STATE(); failRead = false; replies = []; sent = []; asked = []; answer = true; hold = null;
  vi.stubGlobal('confirm', (q: string) => { asked.push(q); return answer; });
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = init.method ?? 'GET';
    const body = init.body === undefined ? undefined : JSON.parse(String(init.body));
    sent.push({ method, url, body });
    if (method === 'GET' && failRead) throw new TypeError('network');
    if (method !== 'GET' && hold) await hold;
    let reply: Reply = { status: 200, body: first };
    if (method === 'PUT') reply = replies.shift() ?? { status: 200, body: SAVED_STATE(body.text.trim()) };
    if (method === 'DELETE') reply = replies.shift() ?? { status: 200, body: STATE() };
    return { ok: reply.status < 400, status: reply.status, text: async () => JSON.stringify(reply.body) } as Response;
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const card = async () => (await screen.findByRole('heading', { name: 'Default goal prompt' })).closest('section')!;
const field = () => screen.getByLabelText('Prompt text') as HTMLTextAreaElement;
const button = (name: string) => screen.getByRole('button', { name }) as HTMLButtonElement;
const changes = () => sent.filter((s) => s.method !== 'GET');

describe('loading', () => {
  it('with nothing saved it shows the built-in text and says so; Reset has nothing to undo', async () => {
    render(GoalPrompt);
    await card();
    expect(field().value).toBe(DEFAULT);
    expect(screen.getByTestId('goal-prompt-state').textContent?.trim()).toBe(SHOWING_DEFAULT);
    expect(button('Save').disabled).toBe(true); // unchanged
    expect(button('Reset to the default').disabled).toBe(true);
    expect(sent).toEqual([{ method: 'GET', url: '/goals/prompt', body: undefined }]);
  });

  it('with the owner\'s text saved it shows that text and when it was saved', async () => {
    first = SAVED_STATE();
    render(GoalPrompt);
    await card();
    expect(field().value).toBe(MINE);
    expect(screen.getByTestId('goal-prompt-state').textContent?.trim()).toBe(savedLine(AT));
    expect(button('Reset to the default').disabled).toBe(false);
  });

  it.each([
    ['goals off on the server', () => { first = { enabled: false }; }],
    ['a read that fails', () => { failRead = true; }],
    ['an answer without the prompt', () => { first = { error: 'forbidden' }; }],
  ])('%s draws nothing and asks once', async (_name, arrange) => {
    arrange();
    const { container } = render(GoalPrompt);
    await waitFor(() => expect(sent).toHaveLength(1));
    await new Promise((r) => setTimeout(r, 20));
    expect(container.textContent?.trim()).toBe('');
    expect(sent).toHaveLength(1);
  });
});

describe('editing', () => {
  it('a change says it is unsaved and may be saved', async () => {
    render(GoalPrompt);
    await card();
    await fireEvent.input(field(), { target: { value: MINE } });
    expect(screen.getByText(UNSAVED)).toBeTruthy();
    expect(button('Save').disabled).toBe(false);
    expect(screen.getByText(`${MINE.length} of 1500 characters`)).toBeTruthy();
  });

  it('a blank text says how to go back to the default and cannot be saved', async () => {
    render(GoalPrompt);
    await card();
    await fireEvent.input(field(), { target: { value: '  \n ' } });
    const said = screen.getByText(BLANK);
    expect(said.classList.contains('hint-err')).toBe(true);
    expect(button('Save').disabled).toBe(true);
  });

  it('a text over the limit says by how much, in the error colour, and cannot be saved', async () => {
    render(GoalPrompt);
    await card();
    await fireEvent.input(field(), { target: { value: 'a'.repeat(1502) } });
    const count = screen.getByText('1502 of 1500 characters: 2 too many. Shorten it to save.');
    expect(count.classList.contains('count-over')).toBe(true);
    expect(button('Save').disabled).toBe(true);
  });

  it('the limit is counted on the trimmed text', async () => {
    render(GoalPrompt);
    await card();
    await fireEvent.input(field(), { target: { value: 'a'.repeat(1500) + '\n' } });
    expect(screen.getByText('1500 of 1500 characters').classList.contains('count-over')).toBe(false);
    expect(button('Save').disabled).toBe(false);
  });
});

describe('saving', () => {
  it('asks first, sends the text alone, and shows the saved text', async () => {
    render(GoalPrompt);
    await card();
    await fireEvent.input(field(), { target: { value: MINE } });
    await fireEvent.click(button('Save'));
    expect(asked).toEqual([CONFIRM_SAVE]);
    const said = await screen.findByText(SAVED);
    expect(said.classList.contains('save-ok')).toBe(true);
    expect(changes()).toEqual([{ method: 'PUT', url: '/goals/prompt', body: { text: MINE } }]);
    expect(screen.getByTestId('goal-prompt-state').textContent?.trim()).toBe(savedLine(AT));
    expect(field().value).toBe(MINE);
    expect(button('Save').disabled).toBe(true); // nothing left to save
    expect(button('Reset to the default').disabled).toBe(false);
  });

  it('while it is sent both buttons wait', async () => {
    let release = () => {};
    hold = new Promise((r) => { release = r; });
    first = SAVED_STATE();
    render(GoalPrompt);
    await card();
    await fireEvent.input(field(), { target: { value: 'Work smaller.' } });
    await fireEvent.click(button('Save'));
    await waitFor(() => expect(screen.getByText('Saving…')).toBeTruthy());
    expect(button('Save').disabled).toBe(true);
    expect(button('Reset to the default').disabled).toBe(true);
    release();
    await screen.findByText(SAVED);
  });

  it('a refusal shows the server\'s sentence in the error colour and keeps the edit', async () => {
    replies = [{ status: 409, body: { error: 'The prompt was changed a moment ago. Look again.' } }];
    render(GoalPrompt);
    await card();
    await fireEvent.input(field(), { target: { value: MINE } });
    await fireEvent.click(button('Save'));
    const said = await screen.findByText('The prompt was changed a moment ago. Look again.');
    expect(said.classList.contains('save-err')).toBe(true);
    expect(field().value).toBe(MINE);
    expect(button('Save').disabled).toBe(false);
  });

  it('a "no" to the question sends nothing and keeps the edit', async () => {
    answer = false;
    render(GoalPrompt);
    await card();
    await fireEvent.input(field(), { target: { value: MINE } });
    await fireEvent.click(button('Save'));
    expect(asked).toEqual([CONFIRM_SAVE]);
    expect(changes()).toEqual([]);
    expect(field().value).toBe(MINE);
  });

  it('text is shown as text', async () => {
    first = SAVED_STATE('<b>bold</b>');
    const { container } = render(GoalPrompt);
    await card();
    expect(field().value).toBe('<b>bold</b>');
    expect(container.querySelector('b')).toBeNull();
  });
});

describe('reset', () => {
  it('asks first, sends DELETE with an empty object, and puts the built-in text back', async () => {
    first = SAVED_STATE();
    render(GoalPrompt);
    await card();
    await fireEvent.click(button('Reset to the default'));
    expect(asked).toEqual([CONFIRM_RESET]);
    await screen.findByText(RESET_DONE);
    expect(changes()).toEqual([{ method: 'DELETE', url: '/goals/prompt', body: {} }]);
    expect(field().value).toBe(DEFAULT);
    expect(screen.getByTestId('goal-prompt-state').textContent?.trim()).toBe(SHOWING_DEFAULT);
    expect(button('Reset to the default').disabled).toBe(true);
  });

  it('a "no" to the question sends nothing', async () => {
    first = SAVED_STATE();
    answer = false;
    render(GoalPrompt);
    await card();
    await fireEvent.click(button('Reset to the default'));
    expect(asked).toEqual([CONFIRM_RESET]);
    expect(changes()).toEqual([]);
    expect(field().value).toBe(MINE);
  });

  it('a failed reset says what failed and keeps the saved text', async () => {
    first = SAVED_STATE();
    replies = [{ status: 500, body: {} }];
    render(GoalPrompt);
    await card();
    await fireEvent.click(button('Reset to the default'));
    const said = await screen.findByText('Could not reset: HTTP 500 for /goals/prompt');
    expect(said.classList.contains('save-err')).toBe(true);
    expect(field().value).toBe(MINE);
  });
});

describe('the controls', () => {
  it('are native, labelled, in the tab order, and the buttons are type="button"', async () => {
    first = SAVED_STATE();
    const section = await (async () => { render(GoalPrompt); return card(); })();
    const box = within(section);
    expect(box.getByLabelText('Prompt text').tagName).toBe('TEXTAREA');
    expect(field().getAttribute('rows')).toBe('12');
    const buttons = [...section.querySelectorAll('button')];
    expect(buttons.map((b) => b.textContent?.trim())).toEqual(['Save', 'Reset to the default']);
    expect(buttons.map((b) => b.getAttribute('type'))).toEqual(['button', 'button']);
    expect([...section.querySelectorAll('textarea, button')].filter((c) => c.getAttribute('tabindex') === '-1')).toEqual([]);
  });
});
