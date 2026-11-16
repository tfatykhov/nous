import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, within } from '@testing-library/svelte';
import Devices from './Devices.svelte';
import Integrations from '../Integrations.svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { initRouter } from '../../lib/router';
import { parseBlocks, styleBlocks } from '../../lib/testing/css';
import { OPEN, deviceList, deviceRow } from '../../lib/testing/deviceFixtures';
import type { DeviceList } from '../../lib/devices';

// The Devices section of the Integrations screen over the /a2ui/devices routes. The fixtures have the shapes the
// routes answer with; every value is made up.

type Answer = [number, unknown];
/** A status for a request that never answers: it ends only when its signal aborts. */
const HANG = -1;
type Call = { url: string; method: string; headers: Record<string, string> | undefined; raw: string | undefined };

let calls: Call[] = [];
let handlers: Record<string, () => Answer | Promise<Answer>> = {};
/** What GET /a2ui/devices answers, unless a handler says otherwise. */
let listed: DeviceList = deviceList();
/** When the server's open code was made (null: none made here): its `expires_in` counts down from then, as the
 * server's does. Setting it again is another tab making a new code. */
let openedAt: number | null = null;
/** The list as the server answers it at this moment. */
function serverList(): DeviceList {
  if (listed.pairing?.state !== 'open' || openedAt === null) return listed;
  return { ...listed, pairing: { ...listed.pairing, expires_in: Math.max(0, Math.floor(300 - (Date.now() - openedAt) / 1000)) } };
}

const CODE = 'K7QM-2XRD';
const NOW_ISO = () => new Date(Date.now()).toISOString();
const PIXEL = () => deviceRow({ id: 'd-1', name: 'Pixel 7', created_at: '2026-11-12T09:00:00Z', last_seen_at: NOW_ISO() });

beforeEach(() => {
  calls = [];
  handlers = {};
  listed = deviceList();
  openedAt = null;
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = init.method ?? 'GET';
    calls.push({ url, method, headers: init.headers as Record<string, string> | undefined, raw: typeof init.body === 'string' ? init.body : undefined });
    const handler = handlers[`${method} ${url}`];
    const [status, payload] = await (handler
      ? handler()
      : method === 'GET' && url === '/a2ui/devices' ? ([200, serverList()] as Answer) : ([500, { error: `unexpected ${method} ${url}` }] as Answer));
    if (status === 0) throw new TypeError('Failed to fetch');
    if (status === HANG) {
      return new Promise<Response>((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => reject(new DOMException('The operation was aborted.', 'AbortError')));
      });
    }
    return new Response(payload === undefined ? '' : JSON.stringify(payload), { status });
  }));
});
// globals: false (vite.config.ts): testing-library cannot register its own cleanup.
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  vi.restoreAllMocks();
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
});

const section = () => screen.getByRole('region', { name: 'Devices' });
const reads = () => calls.filter((c) => c.method === 'GET' && c.url === '/a2ui/devices').length;
const changes = () => calls.filter((c) => c.method !== 'GET');
/** A POST of a new code that answers `code`, after which the list holds it open. */
function opening(code = CODE) {
  handlers['POST /a2ui/devices/pairings'] = () => {
    listed = { ...listed, pairing: OPEN() };
    openedAt = Date.now();
    return [200, { code, expires_in: 300 }];
  };
}
async function shown() {
  render(Devices);
  return within(await screen.findByRole('region', { name: 'Devices' }));
}
async function showCode(name = '') {
  const s = await shown();
  await screen.findByText('Pair a device');
  if (name) await fireEvent.input(s.getByLabelText('Name (optional)'), { target: { value: name } });
  await fireEvent.click(s.getByRole('button', { name: 'Show a code' }));
  return s;
}

describe('Devices: the states', () => {
  it('says it is loading until the list comes, then that no device is paired', async () => {
    let answer!: (a: Answer) => void;
    handlers['GET /a2ui/devices'] = () => new Promise<Answer>((r) => { answer = r; });
    const s = await shown();
    expect(s.getByText('Loading the devices…')).toBeTruthy();
    answer([200, deviceList()]);
    expect(await s.findByText('No device is paired.')).toBeTruthy();
    expect(s.queryByText('Loading the devices…')).toBeNull();
    expect(s.getByRole('button', { name: 'Show a code' })).toBeTruthy();
  });

  it('a live device: its name, when it was paired, when it was last seen, and Revoke; a revoked one below, in words, with no button', async () => {
    listed = deviceList({ devices: [
      PIXEL(),
      deviceRow({ id: 'd-2', name: 'Old phone', created_at: '2026-10-01T09:00:00Z', last_seen_at: null }),
      deviceRow({ id: 'd-3', name: 'Lost tablet', created_at: '2026-09-01T09:00:00Z', revoked_at: '2026-11-14T10:00:00Z' }),
    ] });
    const s = await shown();
    const pixel = (await s.findByText('Pixel 7')).closest('li') as HTMLElement;
    expect(within(pixel).getByText('Paired on Nov 12. Last seen today.')).toBeTruthy();
    expect(within(pixel).getByRole('button', { name: 'Revoke Pixel 7' })).toBeTruthy();
    const old = s.getByText('Old phone').closest('li') as HTMLElement;
    expect(within(old).getByText('Paired on Oct 1. Not seen yet.')).toBeTruthy();
    const gone = s.getByText('Lost tablet').closest('li') as HTMLElement;
    expect(gone.closest('ul')?.getAttribute('aria-label')).toBe('Revoked devices');
    expect(within(gone).getByText('Revoked on Nov 14')).toBeTruthy();
    expect(within(gone).queryByRole('button')).toBeNull();
    expect(s.queryByText('No device is paired.')).toBeNull();
  });

  it('days are the browser\'s: at 19:30 in UTC-5 a phone seen an hour ago was seen today', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date('2026-11-16T00:30:00Z'));
    vi.spyOn(Date.prototype, 'getTimezoneOffset').mockReturnValue(300);
    listed = deviceList({ devices: [
      deviceRow({ name: 'Pixel 7', created_at: '2026-11-13T02:00:00Z', last_seen_at: '2026-11-15T23:30:00Z' }),
      deviceRow({ name: 'Old phone', created_at: '2026-11-01T12:00:00Z', last_seen_at: '2026-11-14T22:30:00Z' }),
    ] });
    const s = await shown();
    expect(await s.findByText('Paired on Nov 12. Last seen today.')).toBeTruthy();
    expect(s.getByText('Paired on Nov 1. Last seen yesterday.')).toBeTruthy();
  });

  it('only revoked devices: says none is paired, and lists the revoked ones', async () => {
    listed = deviceList({ devices: [deviceRow({ name: 'Lost tablet', revoked_at: '2026-11-14T10:00:00Z' })] });
    const s = await shown();
    expect(await s.findByText('No device is paired.')).toBeTruthy();
    expect(s.getByText('Revoked on Nov 14')).toBeTruthy();
  });

  it('a name with markup is shown as text, never as markup', async () => {
    const hostile = '<img src=x onerror="window.__pwned=1"><b>Pixel</b>';
    listed = deviceList({ devices: [deviceRow({ name: hostile, last_seen_at: null })] });
    const s = await shown();
    expect(await s.findByText(hostile)).toBeTruthy();
    expect(section().querySelector('img, b')).toBeNull();
  });

  it('403: asks to sign in through the login page, and offers nothing to change', async () => {
    handlers['GET /a2ui/devices'] = () => [403, { error: 'forbidden' }];
    const s = await shown();
    const note = await s.findByText('Sign in through the login page to manage devices.');
    expect(note.getAttribute('role')).toBe('status');
    expect(s.queryByRole('button')).toBeNull();
    expect(s.queryByRole('textbox')).toBeNull();
  });

  it('404: says this server does not support pairing yet, and offers nothing to change', async () => {
    handlers['GET /a2ui/devices'] = () => [404, undefined];
    const s = await shown();
    expect(await s.findByText('This server does not support device pairing yet.')).toBeTruthy();
    expect(s.queryByRole('button')).toBeNull();
  });

  it('a first read that fails says so in words', async () => {
    handlers['GET /a2ui/devices'] = () => [500, { error: 'the request could not be processed' }];
    const s = await shown();
    expect((await s.findByRole('alert')).textContent).toBe('Could not load the devices: The request could not be processed.');
  });

  it('a later read that fails keeps the last list and says it is from the last read', async () => {
    listed = deviceList({ devices: [PIXEL()] });
    const { component } = render(Devices);
    const s = within(await screen.findByRole('region', { name: 'Devices' }));
    await s.findByText('Pixel 7');
    handlers['GET /a2ui/devices'] = () => [500, { error: 'the request could not be processed' }];
    await component.refresh();
    expect((await s.findByRole('alert')).textContent)
      .toBe('Could not read the list again: The request could not be processed. What is shown is from the last read.');
    expect(s.getByText('Pixel 7')).toBeTruthy();
    expect(s.getByRole('button', { name: 'Revoke Pixel 7' })).toBeTruthy();
  });

  it('a later read with no answer is given up after 15 s, and the last list stays', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    listed = deviceList({ devices: [PIXEL()] });
    const { component } = render(Devices);
    const s = within(await screen.findByRole('region', { name: 'Devices' }));
    await s.findByText('Pixel 7');
    handlers['GET /a2ui/devices'] = () => [HANG, null];
    void component.refresh();
    await vi.advanceTimersByTimeAsync(16_000);
    await vi.waitFor(() => expect(s.getByRole('alert').textContent)
      .toBe('Could not read the list again: Could not reach the server. What is shown is from the last read.'));
    expect(s.getByText('Pixel 7')).toBeTruthy();
  });

  it('a 403 a later read finds is not hidden by the list an earlier one loaded', async () => {
    listed = deviceList({ devices: [PIXEL()] });
    const { component } = render(Devices);
    const s = within(await screen.findByRole('region', { name: 'Devices' }));
    await s.findByText('Pixel 7');
    handlers['GET /a2ui/devices'] = () => [403, { error: 'forbidden' }];
    await component.refresh();
    expect(await s.findByText('Sign in through the login page to manage devices.')).toBeTruthy();
    expect(s.queryByText('Pixel 7')).toBeNull();
  });
});

describe('Devices: a pairing code', () => {
  it('Show a code sends a POST of JSON with no name, then shows the code once, large, with its time in words', async () => {
    opening();
    const s = await showCode();
    const code = await s.findByText(CODE);
    expect(code.classList.contains('code')).toBe(true);
    expect(s.getByText('Valid for 5 minutes')).toBeTruthy();
    expect(s.getByText('Type this code in the app under Settings, Pair this device. It works once.')).toBeTruthy();
    expect(changes().map((c) => ({ method: c.method, url: c.url, headers: c.headers, raw: c.raw }))).toEqual([
      { method: 'POST', url: '/a2ui/devices/pairings', headers: { 'Content-Type': 'application/json' }, raw: '{}' },
    ]);
    await vi.waitFor(() => expect(document.activeElement).toBe(code));
    expect(s.getByRole('button', { name: 'Show a new code' })).toBeTruthy();
  });

  it('a typed name goes with the request, and the field is cleared once the code is made', async () => {
    opening();
    const s = await showCode('Pixel 7');
    await s.findByText(CODE);
    expect(changes()[0].raw).toBe('{"name":"Pixel 7"}');
    expect((s.getByLabelText('Name (optional)') as HTMLInputElement).value).toBe('');
    expect((s.getByLabelText('Name (optional)') as HTMLInputElement).maxLength).toBe(60);
  });

  it('counts down, and at zero the code goes and the sentence says it expired', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    opening();
    const s = await showCode();
    await s.findByText(CODE);
    await vi.advanceTimersByTimeAsync(61_000);
    await vi.waitFor(() => expect(s.getByText('Valid for 4 minutes')).toBeTruthy());
    await vi.advanceTimersByTimeAsync(180_000);
    await vi.waitFor(() => expect(s.getByText('Valid for less than a minute')).toBeTruthy());
    await vi.advanceTimersByTimeAsync(60_000);
    await vi.waitFor(() => expect(s.getByText('This code has expired. Make a new one.')).toBeTruthy());
    expect(s.queryByText(CODE)).toBeNull();
  });

  it.each([
    [{ state: 'used' }, 'A code was used. The new device is in the list below.'],
    [{ state: 'closed_tries' }, 'This code was closed after too many wrong tries. Make a new one.'],
    [{ state: 'expired' }, 'This code has expired. Make a new one.'],
    [null, 'This code no longer works. Make a new one.'],
  ] as const)('a read that says %j takes the code away and says why', async (pairing, said) => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    opening();
    const s = await showCode();
    await s.findByText(CODE);
    listed = { ...listed, pairing };
    await vi.advanceTimersByTimeAsync(15_500);
    await vi.waitFor(() => expect(s.getByText(said)).toBeTruthy());
    expect(s.queryByText(CODE)).toBeNull();
    expect(s.getByRole('button', { name: 'Show a code' })).toBeTruthy();
    // Over: the list is no longer read on a timer.
    const after = reads();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(reads()).toBe(after);
  });

  it('a code made elsewhere replaces the one on screen: it leaves the screen, said so, and is never called used', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    opening();
    const s = await showCode();
    await s.findByText(CODE);
    await vi.advanceTimersByTimeAsync(30_500); // two reads of this very code: it stays
    expect(s.getByText(CODE)).toBeTruthy();
    openedAt = Date.now(); // another tab makes a new code: the server's open code has 300 s again
    await vi.advanceTimersByTimeAsync(15_000);
    await vi.waitFor(() => expect(s.getByText('A newer code was made. This one no longer works.')).toBeTruthy());
    expect(s.queryByText(CODE)).toBeNull();
    // The newer code is used afterwards: nothing more is said about the code that was on screen.
    listed = { ...listed, pairing: { state: 'used' } };
    await vi.advanceTimersByTimeAsync(30_000);
    expect(s.queryByText('A code was used. The new device is in the list below.')).toBeNull();
  });

  it.each([3_000, 12_000])('a read that answers %i ms after it was asked does not end a good code', async (late) => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    opening();
    const s = await showCode();
    await s.findByText(CODE);
    // From now on the server answers `late` ms after it is asked, with the time left as of when it was asked.
    let answered = 0;
    handlers['GET /a2ui/devices'] = () => {
      const answer = serverList();
      return new Promise<Answer>((r) => setTimeout(() => { answered += 1; r([200, answer]); }, late));
    };
    await vi.advanceTimersByTimeAsync(15_000 + late + 500);
    await vi.waitFor(() => expect(answered).toBeGreaterThanOrEqual(1));
    await vi.advanceTimersByTimeAsync(100);
    expect(s.getByText(CODE)).toBeTruthy();
    expect(s.queryByText(/no longer works/)).toBeNull();
  });

  it('when a read ends the code, the focus that was on it goes to the sentence in its place', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    opening();
    const s = await showCode();
    const code = await s.findByText(CODE);
    await vi.waitFor(() => expect(document.activeElement).toBe(code));
    listed = { ...listed, pairing: { state: 'used' } };
    await vi.advanceTimersByTimeAsync(15_500);
    const said = await s.findByText('A code was used. The new device is in the list below.');
    await vi.waitFor(() => expect(document.activeElement).toBe(said));
  });

  it('when a read ends the code, focus elsewhere stays where it is', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    opening();
    const s = await showCode();
    const code = await s.findByText(CODE);
    await vi.waitFor(() => expect(document.activeElement).toBe(code)); // the new code took the focus; the owner moves on
    const field = s.getByLabelText('Name (optional)') as HTMLInputElement;
    field.focus();
    listed = { ...listed, pairing: { state: 'used' } };
    await vi.advanceTimersByTimeAsync(15_500);
    await s.findByText('A code was used. The new device is in the list below.');
    await vi.advanceTimersByTimeAsync(50);
    expect(document.activeElement).toBe(field);
  });

  it('the code and the sentence that replaces it show the same focus outline as the Revoke sentence', () => {
    const css = parseBlocks(styleBlocks(readFileSync(join(__dirname, 'Devices.svelte'), 'utf-8')));
    const ring = css.get('.confirm p:focus');
    expect(ring?.outline).toBe('2px solid var(--focus-ring)');
    for (const sel of ['.code:focus', '.ended p:focus']) expect(css.get(sel), sel).toEqual(ring);
  });

  it('while the code is open, the tries left are said once a wrong code was tried', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    opening();
    const s = await showCode();
    await s.findByText(CODE);
    expect(s.queryByText(/tries left/)).toBeNull();
    listed = { ...listed, pairing: OPEN(3) };
    await vi.advanceTimersByTimeAsync(15_500);
    await vi.waitFor(() => expect(s.getByText('3 tries left before this code closes.')).toBeTruthy());
    expect(s.getByText(CODE)).toBeTruthy();
  });

  it('the list is read every 15 s while a code is open, and only on demand otherwise', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const s = await shown();
    await s.findByText('No device is paired.');
    const idle = reads();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(reads()).toBe(idle);
    opening();
    await fireEvent.click(s.getByRole('button', { name: 'Show a code' }));
    await s.findByText(CODE);
    await vi.waitFor(() => expect(reads()).toBe(idle + 1)); // the read after the change
    await vi.advanceTimersByTimeAsync(15_500);
    await vi.waitFor(() => expect(reads()).toBe(idle + 2));
    await vi.advanceTimersByTimeAsync(15_000);
    await vi.waitFor(() => expect(reads()).toBe(idle + 3));
  });

  it('pauses while the tab is hidden, and reads at once when it is shown again', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    opening();
    const s = await showCode();
    await s.findByText(CODE);
    await vi.waitFor(() => expect(reads()).toBe(2));
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
    document.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(60_000);
    expect(reads()).toBe(2);
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
    document.dispatchEvent(new Event('visibilitychange'));
    await vi.waitFor(() => expect(reads()).toBe(3));
  });

  it('a read that was in flight when the code was made does not take the code away', async () => {
    const { component } = render(Devices);
    const s = within(await screen.findByRole('region', { name: 'Devices' }));
    await s.findByText('No device is paired.');
    // A read starts and holds; the code is made while it is in flight; then the old read answers "no pairing".
    let late: ((a: Answer) => void) | undefined;
    handlers['GET /a2ui/devices'] = () => new Promise<Answer>((r) => { late = r; });
    void component.refresh();
    await vi.waitFor(() => expect(late).toBeDefined());
    delete handlers['GET /a2ui/devices'];
    opening();
    await fireEvent.click(s.getByRole('button', { name: 'Show a code' }));
    expect(await s.findByText(CODE)).toBeTruthy();
    late!([200, deviceList()]);
    await new Promise((r) => setTimeout(r, 50));
    expect(s.getByText(CODE)).toBeTruthy();
    expect(s.queryByText('This code no longer works. Make a new one.')).toBeNull();
  });

  it('a second code replaces the first, and says the earlier one no longer works', async () => {
    opening();
    const s = await showCode();
    await s.findByText(CODE);
    expect(s.queryByText('The earlier code no longer works.')).toBeNull();
    opening('P2QR-8XYZ');
    await fireEvent.click(s.getByRole('button', { name: 'Show a new code' }));
    expect(await s.findByText('P2QR-8XYZ')).toBeTruthy();
    expect(s.queryByText(CODE)).toBeNull();
    expect(s.getByText('The earlier code no longer works.')).toBeTruthy();
  });

  it('Done takes the code off the screen and gives the focus back to Show a code', async () => {
    opening();
    const s = await showCode();
    await s.findByText(CODE);
    await fireEvent.click(s.getByRole('button', { name: 'Done' }));
    expect(s.queryByText(CODE)).toBeNull();
    await vi.waitFor(() => expect(document.activeElement).toBe(s.getByRole('button', { name: 'Show a code' })));
  });

  it('the code is not kept: not in storage, not in the address, and gone when the section is opened again', async () => {
    const stored = vi.spyOn(Storage.prototype, 'setItem');
    opening();
    const s = await showCode();
    await s.findByText(CODE);
    expect(stored.mock.calls.filter((args) => args.join(' ').includes(CODE))).toEqual([]);
    expect(location.href).not.toContain(CODE);
    expect(JSON.stringify({ ...localStorage })).not.toContain(CODE);
    expect(JSON.stringify({ ...sessionStorage })).not.toContain(CODE);
    cleanup();
    const again = await shown();
    await again.findByText(/A code made earlier is still open/);
    expect(again.queryByText(CODE)).toBeNull();
    expect(document.body.textContent).not.toContain(CODE);
  });

  it('says when redeeming is held back for a minute', async () => {
    listed = deviceList({ limited: true });
    const s = await shown();
    expect(await s.findByText('Pairing is being held back for a minute because of too many tries.')).toBeTruthy();
  });

  it('a list answer without its redeem field shows the list and reads as not held back', async () => {
    handlers['GET /a2ui/devices'] = () => [200, { devices: [PIXEL()], pairing: null }];
    const s = await shown();
    expect(await s.findByText('Pixel 7')).toBeTruthy();
    expect(s.queryByText('Pairing is being held back for a minute because of too many tries.')).toBeNull();
    expect(s.queryByRole('alert')).toBeNull();
  });

  it('with no code on screen, says what the server holds', async () => {
    listed = deviceList({ pairing: { state: 'closed_tries' } });
    const s = await shown();
    expect(await s.findByText('The last code was closed after too many wrong tries. If this was not you, check who can reach this host.')).toBeTruthy();
  });

  it('409: says the server\'s sentence in plain words, and shows no code', async () => {
    handlers['POST /a2ui/devices/pairings'] = () => [409, { error: 'revoke a device first' }];
    const s = await showCode();
    expect((await s.findByRole('alert')).textContent).toBe('Revoke a device first.');
    expect(s.queryByText(CODE)).toBeNull();
  });

  it('a new try drops the sentence about the last code, whatever it answers', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    opening();
    const s = await showCode();
    await s.findByText(CODE);
    listed = { ...listed, pairing: { state: 'used' } };
    await vi.advanceTimersByTimeAsync(15_500);
    await vi.waitFor(() => expect(s.getByText('A code was used. The new device is in the list below.')).toBeTruthy());
    handlers['POST /a2ui/devices/pairings'] = () => [409, { error: 'revoke a device first' }];
    await fireEvent.click(s.getByRole('button', { name: 'Show a code' }));
    await vi.waitFor(() => expect(s.getByRole('alert').textContent).toBe('Revoke a device first.'));
    expect(s.queryByText('A code was used. The new device is in the list below.')).toBeNull();
  });

  it('the sentence about a code that ended goes after two minutes, and no line about the used code takes its place', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const USED = 'A code was used. The new device is in the list below.';
    opening();
    const s = await showCode();
    await s.findByText(CODE);
    listed = { ...listed, pairing: { state: 'used' } };
    await vi.advanceTimersByTimeAsync(15_500);
    await vi.waitFor(() => expect(s.getByText(USED)).toBeTruthy());
    await vi.advanceTimersByTimeAsync(117_000);
    expect(s.getByText(USED)).toBeTruthy();
    await vi.advanceTimersByTimeAsync(4_000);
    await vi.waitFor(() => expect(s.queryByText(USED)).toBeNull());
    expect(s.queryByText(/code/i, { selector: '.line' })).toBeNull();
    expect(s.queryByRole('button', { name: 'Done' })).toBeNull();
  });

  it('Done takes the sentence about a code that ended away at once, and gives the focus to Show a code', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    opening();
    const s = await showCode();
    await s.findByText(CODE);
    listed = { ...listed, pairing: { state: 'expired' } };
    await vi.advanceTimersByTimeAsync(15_500);
    await vi.waitFor(() => expect(s.getByText('This code has expired. Make a new one.')).toBeTruthy());
    await fireEvent.click(s.getByRole('button', { name: 'Done' }));
    expect(s.queryByText('This code has expired. Make a new one.')).toBeNull();
    await vi.waitFor(() => expect(document.activeElement).toBe(s.getByRole('button', { name: 'Show a code' })));
  });

  it('a code that a read ends while a new one is being made is not said once the new one is shown', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    opening();
    const s = await showCode();
    await s.findByText(CODE);
    let answer!: (a: Answer) => void;
    handlers['POST /a2ui/devices/pairings'] = () => new Promise<Answer>((r) => { answer = r; });
    await fireEvent.click(s.getByRole('button', { name: 'Show a new code' }));
    listed = { ...listed, pairing: { state: 'used' } };
    await vi.advanceTimersByTimeAsync(15_500);
    await vi.waitFor(() => expect(s.getByText('A code was used. The new device is in the list below.')).toBeTruthy());
    listed = { ...listed, pairing: OPEN() };
    answer([200, { code: 'P2QR-8XYZ', expires_in: 300 }]);
    expect(await s.findByText('P2QR-8XYZ')).toBeTruthy();
    await fireEvent.click(s.getByRole('button', { name: 'Done' }));
    expect(s.queryByText('A code was used. The new device is in the list below.')).toBeNull();
  });

  it('a try refused while a read ended the code on screen says only the refusal', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    opening();
    const s = await showCode();
    await s.findByText(CODE);
    let answer!: (a: Answer) => void;
    handlers['POST /a2ui/devices/pairings'] = () => new Promise<Answer>((r) => { answer = r; });
    await fireEvent.click(s.getByRole('button', { name: 'Show a new code' }));
    listed = { ...listed, pairing: { state: 'used' } };
    await vi.advanceTimersByTimeAsync(15_500);
    await vi.waitFor(() => expect(s.getByText('A code was used. The new device is in the list below.')).toBeTruthy());
    answer([409, { error: 'revoke a device first' }]);
    await vi.waitFor(() => expect(s.getByRole('alert').textContent).toBe('Revoke a device first.'));
    expect(s.queryByText('A code was used. The new device is in the list below.')).toBeNull();
  });

  it('a new code with no answer takes the code on screen away: a new one may have replaced it', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    opening();
    const s = await showCode();
    await s.findByText(CODE);
    handlers['POST /a2ui/devices/pairings'] = () => [HANG, null];
    await fireEvent.click(s.getByRole('button', { name: 'Show a new code' }));
    await vi.advanceTimersByTimeAsync(16_000);
    await vi.waitFor(() => expect(s.getByRole('alert').textContent).toBe('No answer came back. Check the list before trying again.'));
    expect(s.queryByText(CODE)).toBeNull();
  });

  it('a refused new code leaves the code on screen as it was', async () => {
    opening();
    const s = await showCode();
    await s.findByText(CODE);
    handlers['POST /a2ui/devices/pairings'] = () => [409, { error: 'revoke a device first' }];
    await fireEvent.click(s.getByRole('button', { name: 'Show a new code' }));
    expect((await s.findByRole('alert')).textContent).toBe('Revoke a device first.');
    expect(s.getByText(CODE)).toBeTruthy();
  });

  it('a new code with no answer is given up after 15 s: it says to check the list, and the button works again', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    handlers['POST /a2ui/devices/pairings'] = () => [HANG, null];
    const s = await showCode();
    await vi.advanceTimersByTimeAsync(16_000);
    await vi.waitFor(() => expect(s.getByRole('alert').textContent).toBe('No answer came back. Check the list before trying again.'));
    expect((s.getByRole('button', { name: 'Show a code' }) as HTMLButtonElement).disabled).toBe(false);
  });
});

describe('Devices: Revoke', () => {
  beforeEach(() => {
    listed = deviceList({ devices: [PIXEL()] });
  });

  it('asks one sentence first, with the focus on the sentence, not on Yes; nothing is sent until Yes', async () => {
    const s = await shown();
    await fireEvent.click(await s.findByRole('button', { name: 'Revoke Pixel 7' }));
    const group = s.getByRole('group', { name: 'Confirm revoking Pixel 7' });
    const sentence = within(group).getByText('This device will stop working with Nous until it is paired again.');
    await vi.waitFor(() => expect(document.activeElement).toBe(sentence));
    expect(document.activeElement).not.toBe(within(group).getByRole('button', { name: 'Yes, revoke' }));
    expect(changes()).toEqual([]);
  });

  it('Escape closes the question and gives the focus back to Revoke; Cancel does the same', async () => {
    const s = await shown();
    const revoke = await s.findByRole('button', { name: 'Revoke Pixel 7' });
    await fireEvent.click(revoke);
    s.getByRole('group', { name: 'Confirm revoking Pixel 7' });
    await fireEvent.keyDown(window, { key: 'Escape' });
    expect(s.queryByRole('group', { name: 'Confirm revoking Pixel 7' })).toBeNull();
    await vi.waitFor(() => expect(document.activeElement).toBe(revoke));
    await fireEvent.click(revoke);
    await fireEvent.click(s.getByRole('button', { name: 'Cancel' }));
    expect(s.queryByRole('group', { name: 'Confirm revoking Pixel 7' })).toBeNull();
    expect(changes()).toEqual([]);
  });

  it('Yes sends a DELETE that says it is JSON, reads the list again, and the device moves to the revoked ones', async () => {
    handlers['DELETE /a2ui/devices/d-1'] = () => {
      listed = deviceList({ devices: [{ ...PIXEL(), revoked_at: '2026-11-15T10:00:00Z' }] });
      return [200, { revoked: true }];
    };
    const s = await shown();
    await fireEvent.click(await s.findByRole('button', { name: 'Revoke Pixel 7' }));
    const before = reads();
    await fireEvent.click(s.getByRole('button', { name: 'Yes, revoke' }));
    expect((await s.findByRole('status')).textContent).toBe('Revoked Pixel 7.');
    expect(changes().map((c) => ({ method: c.method, url: c.url, headers: c.headers, raw: c.raw }))).toEqual([
      { method: 'DELETE', url: '/a2ui/devices/d-1', headers: { 'Content-Type': 'application/json' }, raw: undefined },
    ]);
    expect(reads()).toBe(before + 1);
    expect(s.getByText('Revoked on Nov 15')).toBeTruthy();
    expect(s.queryByRole('button', { name: 'Revoke Pixel 7' })).toBeNull();
    expect(s.getByText('No device is paired.')).toBeTruthy();
    // Its Revoke is gone: the focus goes to the section's title.
    await vi.waitFor(() => expect(document.activeElement).toBe(s.getByRole('heading', { name: 'Devices' })));
  });

  it('a question about a device that a read no longer lists as live is closed', async () => {
    const { component } = render(Devices);
    const s = within(await screen.findByRole('region', { name: 'Devices' }));
    await fireEvent.click(await s.findByRole('button', { name: 'Revoke Pixel 7' }));
    s.getByRole('group', { name: 'Confirm revoking Pixel 7' });
    listed = deviceList({ devices: [{ ...PIXEL(), revoked_at: '2026-11-15T10:00:00Z' }] });
    await component.refresh();
    await vi.waitFor(() => expect(s.queryByRole('group', { name: 'Confirm revoking Pixel 7' })).toBeNull());
    expect(changes()).toEqual([]);
  });

  it('a pairing refused for too many devices is no longer said once a device is revoked', async () => {
    handlers['POST /a2ui/devices/pairings'] = () => [409, { error: 'revoke a device first' }];
    handlers['DELETE /a2ui/devices/d-1'] = () => [200, { revoked: true }];
    const s = await shown();
    await fireEvent.click(await s.findByRole('button', { name: 'Show a code' }));
    expect((await s.findByRole('alert')).textContent).toBe('Revoke a device first.');
    await fireEvent.click(s.getByRole('button', { name: 'Revoke Pixel 7' }));
    await fireEvent.click(s.getByRole('button', { name: 'Yes, revoke' }));
    expect((await s.findByRole('status')).textContent).toBe('Revoked Pixel 7.');
    expect(s.queryByText('Revoke a device first.')).toBeNull();
  });

  it('a device that was already revoked (from elsewhere) is said to be, not said revoked now', async () => {
    handlers['DELETE /a2ui/devices/d-1'] = () => [200, { revoked: false }];
    const s = await shown();
    await fireEvent.click(await s.findByRole('button', { name: 'Revoke Pixel 7' }));
    await fireEvent.click(s.getByRole('button', { name: 'Yes, revoke' }));
    expect((await s.findByRole('status')).textContent).toBe('Pixel 7 was already revoked.');
  });

  it('a revoke with no answer is given up after 15 s and says to check the list', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    handlers['DELETE /a2ui/devices/d-1'] = () => [HANG, null];
    const s = await shown();
    await fireEvent.click(await s.findByRole('button', { name: 'Revoke Pixel 7' }));
    await fireEvent.click(s.getByRole('button', { name: 'Yes, revoke' }));
    await vi.advanceTimersByTimeAsync(16_000);
    await vi.waitFor(() => expect(s.getByRole('alert').textContent).toBe('No answer came back. Check the list before trying again.'));
    expect((s.getByRole('button', { name: 'Revoke Pixel 7' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('a device the server no longer knows: its sentence, not "no pairing on this server"', async () => {
    handlers['DELETE /a2ui/devices/d-1'] = () => [404, { error: 'no such device' }];
    const s = await shown();
    await fireEvent.click(await s.findByRole('button', { name: 'Revoke Pixel 7' }));
    await fireEvent.click(s.getByRole('button', { name: 'Yes, revoke' }));
    expect((await s.findByRole('alert')).textContent).toBe('No such device.');
  });
});

describe('Devices on the Integrations screen', () => {
  it('is shown below the MCP servers when they are listed', async () => {
    location.hash = '#/integrations';
    initRouter();
    handlers['GET /integrations/mcp/servers'] = () =>
      [200, { ceilings: { pinned_servers: false, background_servers: false }, secrets_available: true, oauth_available: false, servers: [] }];
    listed = deviceList({ devices: [PIXEL()] });
    render(Integrations);
    expect(await screen.findByRole('navigation', { name: 'MCP servers' })).toBeTruthy();
    expect(await within(section()).findByText('Pixel 7')).toBeTruthy();
  });

  it('is shown while the MCP servers are not available, and the page Refresh reads it again', async () => {
    location.hash = '#/integrations';
    initRouter();
    handlers['GET /integrations/mcp/servers'] = () => [503, { error: 'no MCP client is running' }];
    listed = deviceList({ devices: [PIXEL()] });
    render(Integrations);
    expect(await screen.findByRole('heading', { name: 'MCP servers are not available here' })).toBeTruthy();
    const s = within(section());
    expect(await s.findByText('Pixel 7')).toBeTruthy();
    const before = reads();
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await vi.waitFor(() => expect(reads()).toBe(before + 1));
  });
});
