import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/svelte';
import SecondJudge from './SecondJudge.svelte';
import type { SecondState } from '../../lib/outside_judge';

// F118 PR 3: the card "A second opinion on decisions". The fetch stand-in answers the first GET with `first` (status
// `getStatus`), each later GET with the next of `later` (else `first`), and each change with the next of `replies`
// (else the state). Dates are fixed UTC strings, and the re-reads run on fake timers: no test depends on the clock.

const KEY = 'ts-typed-' + 'K'.repeat(24);
const SENTENCE = 'Send a copy of each decision Nous resolves to TypeSafe (api.typesafe.ai), an outside company? At most 100 copies a day.';

const STATE = (over: Partial<SecondState> = {}): SecondState => ({
  enabled: true,
  locked: null,
  loaded: true,
  running: false,
  on: { value: false, source: 'server' },
  model: { value: 'jev-1.14.0', source: 'server' },
  daily_calls: { value: 100, most: 100, source: 'server' },
  key: { source: 'none', set_at: null, problem: null, server_has_key: false },
  can_store_key: true,
  sends_to: 'api.typesafe.ai',
  confirm_text: SENTENCE,
  updated_at: null,
  updated_by: null,
  ...over,
});
const SAVED_KEY = { source: 'stored' as const, set_at: '2026-05-03T12:00:00Z', problem: null, server_has_key: false };

interface Reply { status: number; body: unknown }
let first: unknown = STATE();
let later: unknown[] = [];
let getStatus = 200;
let replies: Reply[] = [];
let sent: { method: string; url: string; body: unknown }[] = [];
let failLoad = false;

function install() {
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = init.method ?? 'GET';
    const readsBefore = sent.filter((s) => s.method === 'GET').length;
    sent.push({ method, url, body: init.body === undefined ? undefined : JSON.parse(String(init.body)) });
    if (method === 'GET' && failLoad) throw new TypeError('network');
    let reply: Reply;
    if (method !== 'GET') reply = replies.shift() ?? { status: 200, body: first };
    else if (readsBefore === 0) reply = { status: getStatus, body: getStatus < 400 ? first : { error: 'forbidden' } };
    else reply = { status: 200, body: later.length ? later.shift() : first };
    return { ok: reply.status < 400, status: reply.status, text: async () => JSON.stringify(reply.body) } as Response;
  }));
}
const needsConfirm = (calls = 100, text = SENTENCE): Reply =>
  ({ status: 409, body: { error: text, needs_confirm: true, confirm_daily_calls: calls } });
const reads = () => sent.filter((s) => s.method === 'GET').length;
const changes = () => sent.filter((s) => s.method !== 'GET');
/** Render the card and find its panel. */
const card = async () => {
  render(SecondJudge);
  return (await screen.findByRole('heading', { name: 'A second opinion on decisions' })).closest('section')!;
};
const within = (box: HTMLElement) => ({ cancel: Array.from(box.querySelectorAll('button')).find((b) => b.textContent?.trim() === 'Cancel')! });
const keyInput = () => document.getElementById('jev-key-input') as HTMLInputElement | null;

beforeEach(() => { first = STATE(); later = []; getStatus = 200; replies = []; sent = []; failLoad = false; install(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('loading', () => {
  it('with the settings off on the server it draws nothing and asks once', async () => {
    first = { enabled: false };
    const { container } = render(SecondJudge);
    await waitFor(() => expect(sent).toHaveLength(1));
    await new Promise((r) => setTimeout(r, 20));
    expect(container.textContent?.trim()).toBe('');
    expect(sent).toEqual([{ method: 'GET', url: '/dashboard/outside-judge/second', body: undefined }]);
  });

  it.each([
    ['a refused first read', () => { getStatus = 403; }],
    ['a first read that fails', () => { failLoad = true; }],
  ])('%s draws nothing and asks no more', async (_name, arrange) => {
    arrange();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { container } = render(SecondJudge);
      await waitFor(() => expect(sent).toHaveLength(1));
      await vi.advanceTimersByTimeAsync(5 * 60_000);
      expect(container.textContent?.trim()).toBe('');
      expect(sent).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('while the saved settings could not be read it shows the status and no control', async () => {
    first = STATE({ loaded: false });
    const section = await card();
    expect(screen.getByTestId('jev-status').textContent).toBe('Waiting for the saved settings');
    expect(section.querySelectorAll('button, input')).toHaveLength(0);
  });

  it('while the saved settings could not be read it asks again every 30 s, and stops once they are read', async () => {
    first = STATE({ loaded: false });
    later = [STATE({ loaded: false }), STATE()];
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      await card();
      expect(reads()).toBe(1);
      await vi.advanceTimersByTimeAsync(29_000);
      expect(reads()).toBe(1);
      await vi.advanceTimersByTimeAsync(1_000);
      await waitFor(() => expect(reads()).toBe(2));
      expect(screen.getByTestId('jev-status').textContent).toBe('Waiting for the saved settings');
      await vi.advanceTimersByTimeAsync(30_000);
      await waitFor(() => expect(screen.getByTestId('jev-status').textContent).toBe('Off'));
      expect(reads()).toBe(3);
      await vi.advanceTimersByTimeAsync(10 * 60_000);
      expect(reads()).toBe(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it.each([
    ['loaded', STATE()],
    ['off on the server', { enabled: false }],
  ])('%s, it never asks again', async (_name, answer) => {
    first = answer;
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(SecondJudge);
      await waitFor(() => expect(reads()).toBe(1));
      await vi.advanceTimersByTimeAsync(10 * 60_000);
      expect(reads()).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('a read again that fails, after one that said the settings are on, says so in the card and goes on asking', async () => {
    first = STATE({ loaded: false });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      await card();
      failLoad = true;
      await vi.advanceTimersByTimeAsync(30_000);
      expect((await screen.findByRole('alert')).textContent).toBe('These settings could not be read. Reload the page to try again.');
      failLoad = false;
      later = [STATE()];
      await vi.advanceTimersByTimeAsync(30_000);
      await waitFor(() => expect(screen.getByTestId('jev-status').textContent).toBe('Off'));
      expect(screen.queryByRole('alert')).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('the status and the rows', () => {
  it.each([
    [{}, 'Off'],
    [{ on: { value: true, source: 'stored' as const } }, 'On, but no key'],
    [{ on: { value: true, source: 'stored' as const }, key: SAVED_KEY, daily_calls: { value: 0, most: 100, source: 'stored' as const } },
      'On, but the daily limit is 0'],
    [{ on: { value: true, source: 'stored' as const }, key: SAVED_KEY }, 'On, but not running. Save again or restart Nous.'],
    [{ on: { value: true, source: 'stored' as const }, key: SAVED_KEY, running: true }, 'On'],
    [{ locked: 'off' as const }, 'Switched off by the server'],
  ])('%j reads "%s"', async (over, text) => {
    first = STATE(over);
    await card();
    expect(screen.getByTestId('jev-status').textContent).toBe(text);
  });

  it('shows each value, where it comes from, the most the server allows and the help line', async () => {
    first = STATE({ key: SAVED_KEY, daily_calls: { value: 30, most: 40, source: 'stored' } });
    const section = await card();
    expect((screen.getByLabelText('Model') as HTMLInputElement).value).toBe('jev-1.14.0');
    expect((screen.getByLabelText('Most copies a day') as HTMLInputElement).value).toBe('30');
    expect(section.textContent).toContain('Most the server allows: 40');
    expect(section.textContent).toContain('A version like jev-1.14.0');
    expect(screen.getByTestId('jev-key').textContent).toBe('A key is saved (3 May)');
    expect(section.textContent).toContain('A change applies at once. Decisions waiting in line for Jev when you save are dropped from the line.');
    expect(section.textContent).toContain("gives a second opinion on Nous's decisions, beside Nous's own judge.");
    expect(screen.getByRole('button', { name: 'Replace key' })).toBeTruthy();
    expect(section.textContent).not.toMatch(/undefined|null|NaN/);
  });

  it('without a key file the key buttons are off and the card says why', async () => {
    first = STATE({ can_store_key: false });
    const section = await card();
    expect((screen.getByRole('button', { name: 'Add key' }) as HTMLButtonElement).disabled).toBe(true);
    expect(section.textContent).toContain('This server has no key file, so a key cannot be saved here.');
  });

  it('switched off by the server, the rows are read-only and only the removal of a saved key is offered', async () => {
    first = STATE({ locked: 'off', key: SAVED_KEY });
    const section = await card();
    for (const input of section.querySelectorAll('input')) expect((input as HTMLInputElement).disabled).toBe(true);
    const names = Array.from(section.querySelectorAll('button')).filter((b) => !b.disabled).map((b) => b.textContent?.trim());
    expect(names).toEqual(['Remove key']);
  });
});

describe('a change that starts the sending', () => {
  it('opens the box with the server\'s sentence and sends nothing more before "Yes"', async () => {
    first = STATE({ key: SAVED_KEY });
    replies = [needsConfirm(), { status: 200, body: STATE({ key: SAVED_KEY, on: { value: true, source: 'stored' }, running: true }) }];
    await card();
    await fireEvent.click(screen.getByLabelText('Ask Jev as well'));
    const box = await screen.findByRole('group', { name: 'Confirm sending to api.typesafe.ai' });
    expect(box.textContent).toContain(SENTENCE);
    expect((screen.getByLabelText('Ask Jev as well') as HTMLInputElement).checked).toBe(false);
    expect(changes()).toEqual([{ method: 'PATCH', url: '/dashboard/outside-judge/second', body: { on: true } }]);
    await fireEvent.click(screen.getByRole('button', { name: 'Yes, send them' }));
    await waitFor(() => expect(screen.getByTestId('jev-status').textContent).toBe('On'));
    expect(changes()[1]).toEqual({
      method: 'PATCH', url: '/dashboard/outside-judge/second', body: { on: true, confirm_send: true, confirm_daily_calls: 100 },
    });
    expect(screen.queryByRole('group', { name: 'Confirm sending to api.typesafe.ai' })).toBeNull();
    expect((screen.getByLabelText('Ask Jev as well') as HTMLInputElement).checked).toBe(true);
  });

  it('a yes answered with a new sentence (the limit changed meanwhile) asks again, and the next yes sends the new number', async () => {
    const NEWER = 'Send a copy of each decision Nous resolves to TypeSafe (api.typesafe.ai), an outside company? At most 400 copies a day.';
    first = STATE({ key: SAVED_KEY });
    replies = [
      needsConfirm(100),
      needsConfirm(400, NEWER),
      { status: 200, body: STATE({ key: SAVED_KEY, on: { value: true, source: 'stored' }, running: true }) },
    ];
    await card();
    await fireEvent.click(screen.getByLabelText('Ask Jev as well'));
    await fireEvent.click(await screen.findByRole('button', { name: 'Yes, send them' }));
    await waitFor(() => expect(screen.getByRole('group', { name: 'Confirm sending to api.typesafe.ai' }).textContent).toContain(NEWER));
    await fireEvent.click(screen.getByRole('button', { name: 'Yes, send them' }));
    await waitFor(() => expect(screen.getByTestId('jev-status').textContent).toBe('On'));
    expect(changes().map((c) => c.body)).toEqual([
      { on: true },
      { on: true, confirm_send: true, confirm_daily_calls: 100 },
      { on: true, confirm_send: true, confirm_daily_calls: 400 },
    ]);
  });

  it('Cancel closes the box and sends nothing', async () => {
    first = STATE({ key: SAVED_KEY });
    replies = [needsConfirm()];
    await card();
    await fireEvent.click(screen.getByLabelText('Ask Jev as well'));
    await fireEvent.click(within(await screen.findByRole('group', { name: 'Confirm sending to api.typesafe.ai' })).cancel);
    expect(screen.queryByRole('group', { name: 'Confirm sending to api.typesafe.ai' })).toBeNull();
    expect(changes()).toHaveLength(1);
  });

  it('a refused change shows the server\'s sentence', async () => {
    replies = [{ status: 400, body: { error: 'model must be a version like jev-1.14.0, or null for the server\'s value.' } }];
    await card();
    await fireEvent.input(screen.getByLabelText('Model'), { target: { value: 'jev-latest' } });
    await fireEvent.click(screen.getAllByRole('button', { name: 'Save' })[0]);
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.getByRole('alert').textContent).toBe("model must be a version like jev-1.14.0, or null for the server's value.");
    expect(changes()).toEqual([{ method: 'PATCH', url: '/dashboard/outside-judge/second', body: { model: 'jev-latest' } }]);
  });

  it('a limit that is not a whole number up to the server\'s most is refused here and not sent', async () => {
    first = STATE({ daily_calls: { value: 10, most: 40, source: 'server' } });
    await card();
    await fireEvent.input(screen.getByLabelText('Most copies a day'), { target: { value: '41' } });
    await fireEvent.click(screen.getAllByRole('button', { name: 'Save' })[1]);
    expect((await screen.findByRole('alert')).textContent).toBe('Enter a whole number from 0 to 40.');
    expect(changes()).toEqual([]);
    await fireEvent.input(screen.getByLabelText('Most copies a day'), { target: { value: '40' } });
    await fireEvent.click(screen.getAllByRole('button', { name: 'Save' })[1]);
    await waitFor(() => expect(changes()).toEqual([{ method: 'PATCH', url: '/dashboard/outside-judge/second', body: { daily_calls: 40 } }]));
  });
});


describe('the key', () => {
  async function typeKey(): Promise<HTMLElement> {
    const section = await card();
    await fireEvent.click(screen.getByRole('button', { name: 'Add key' }));
    const input = keyInput()!;
    expect(input.type).toBe('password');
    expect(input.getAttribute('autocomplete')).toBe('new-password');
    await fireEvent.input(input, { target: { value: KEY } });
    return section;
  }

  it('is sent once, then the field is gone and the key is nowhere on the page', async () => {
    replies = [{ status: 200, body: STATE({ key: SAVED_KEY }) }];
    const section = await typeKey();
    expect(section.textContent).not.toContain(KEY);
    await fireEvent.click(screen.getByRole('button', { name: 'Save key' }));
    await waitFor(() => expect(screen.getByTestId('jev-key').textContent).toBe('A key is saved (3 May)'));
    expect(changes()).toEqual([{ method: 'PUT', url: '/dashboard/outside-judge/second/key', body: { key: KEY } }]);
    expect(keyInput()).toBeNull();
    expect(document.body.innerHTML).not.toContain(KEY);
  });

  it('a refused key is cleared from the field and the refusal names no value', async () => {
    replies = [{ status: 400, body: { error: 'key must be 16 to 200 characters: letters, digits and . _ ~ + / = : -' } }];
    await typeKey();
    await fireEvent.click(screen.getByRole('button', { name: 'Save key' }));
    expect((await screen.findByRole('alert')).textContent).toContain('key must be 16 to 200 characters');
    expect(keyInput()!.value).toBe('');
    expect(document.body.innerHTML).not.toContain(KEY);
  });

  it('waits in the field while the box asks, and is cleared on Cancel', async () => {
    replies = [needsConfirm()];
    await typeKey();
    await fireEvent.click(screen.getByRole('button', { name: 'Save key' }));
    const box = await screen.findByRole('group', { name: 'Confirm sending to api.typesafe.ai' });
    expect(box.textContent).not.toContain(KEY);
    await fireEvent.click(within(box).cancel);
    expect(keyInput()!.value).toBe('');
    expect(changes()).toHaveLength(1);
    expect(document.body.innerHTML).not.toContain(KEY);
  });

  it('is sent again with the confirmation on "Yes", then cleared, whether it is saved or refused', async () => {
    replies = [needsConfirm(), { status: 200, body: STATE({ key: SAVED_KEY, on: { value: true, source: 'stored' }, running: true }) }];
    await typeKey();
    await fireEvent.click(screen.getByRole('button', { name: 'Save key' }));
    await fireEvent.click(await screen.findByRole('button', { name: 'Yes, send them' }));
    await waitFor(() => expect(screen.getByTestId('jev-status').textContent).toBe('On'));
    expect(changes()).toEqual([
      { method: 'PUT', url: '/dashboard/outside-judge/second/key', body: { key: KEY } },
      { method: 'PUT', url: '/dashboard/outside-judge/second/key', body: { key: KEY, confirm_send: true, confirm_daily_calls: 100 } },
    ]);
    expect(keyInput()).toBeNull();
    expect(document.body.innerHTML).not.toContain(KEY);
    cleanup();
    sent = [];
    first = STATE();
    replies = [needsConfirm(), { status: 500, body: { error: 'The change could not be saved.' } }];
    await typeKey();
    await fireEvent.click(screen.getByRole('button', { name: 'Save key' }));
    await fireEvent.click(await screen.findByRole('button', { name: 'Yes, send them' }));
    expect((await screen.findByRole('alert')).textContent).toBe('The change could not be saved.');
    expect(keyInput()!.value).toBe('');
    expect(screen.queryByRole('group', { name: 'Confirm sending to api.typesafe.ai' })).toBeNull();
  });

  it("a change of another row keeps the typed key, and the key form's Cancel waits while the box asks", async () => {
    replies = [{ status: 200, body: STATE({ model: { value: 'jev-1.15.0', source: 'stored' } }) }, needsConfirm()];
    await typeKey();
    await fireEvent.input(screen.getByLabelText('Model'), { target: { value: 'jev-1.15.0' } });
    await fireEvent.click(screen.getAllByRole('button', { name: 'Save' })[0]);
    await waitFor(() => expect(changes()).toHaveLength(1));
    await waitFor(() => expect(screen.getByRole('status').textContent).toBe('Saved.'));
    expect(keyInput()!.value).toBe(KEY);
    await fireEvent.click(screen.getByRole('button', { name: 'Save key' }));
    await screen.findByRole('group', { name: 'Confirm sending to api.typesafe.ai' });
    const form = keyInput()!.closest('.key-field') as HTMLElement;
    expect((within(form).cancel as HTMLButtonElement).disabled).toBe(true);
    expect(keyInput()!.value).toBe(KEY);
  });

  it.each([
    [false, 'Remove the saved key? Jev stops until a key is set again.'],
    [true, "Remove the saved key? Jev will use the server's own key."],
  ])('asks before the removal (server key %s): "%s"', async (serverHasKey, question) => {
    first = STATE({ key: { ...SAVED_KEY, server_has_key: serverHasKey } });
    replies = [{ status: 200, body: STATE() }];
    await card();
    await fireEvent.click(screen.getByRole('button', { name: 'Remove key' }));
    const box = screen.getByRole('group', { name: 'Confirm removing the key' });
    expect(box.textContent).toContain(question);
    expect(changes()).toEqual([]);
    await fireEvent.click(screen.getByRole('button', { name: 'Yes, remove the key' }));
    await waitFor(() => expect(screen.getByTestId('jev-key').textContent).toBe('No key'));
    expect(changes()).toEqual([{ method: 'DELETE', url: '/dashboard/outside-judge/second/key', body: {} }]);
  });

  it.each([
    ['ok', 'Jev answered'],
    ['refused', 'Jev refused the key or the model'],
    ['unreachable', 'Jev could not be reached'],
  ])('a check that finds %s says "%s"', async (result, words) => {
    first = STATE({ key: SAVED_KEY });
    replies = [{ status: 200, body: { result } }];
    await card();
    await fireEvent.click(screen.getByRole('button', { name: 'Check' }));
    expect((await screen.findByRole('status')).textContent).toBe(words);
    expect(changes()).toEqual([{ method: 'POST', url: '/dashboard/outside-judge/second/check', body: {} }]);
  });
});
