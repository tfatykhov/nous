import { describe, it, expect, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

// Drives the real worker file (public/companion-sw.js) with fake fetch events.
// Media must pass straight through to the network: a cached 200 answering a
// Range request breaks playback on iOS, and `cache.put` of a 206 rejects. An
// agent app directory named `assets` puts a video under the cache-first
// prefix, so the guard has to be on the request, not the path.

const source = readFileSync(resolve(__dirname, '../../public/companion-sw.js'), 'utf-8');

type Listener = (event: unknown) => void;

function loadWorker(caches: unknown = {}, fetchImpl: unknown = vi.fn()): Record<string, Listener[]> {
  const listeners: Record<string, Listener[]> = {};
  const self = {
    location: { origin: 'https://t' },
    addEventListener(type: string, fn: Listener) {
      (listeners[type] ??= []).push(fn);
    },
    skipWaiting: vi.fn(),
    clients: { claim: vi.fn() },
  };
  new Function('self', 'caches', 'fetch', source)(self, caches, fetchImpl);
  return listeners;
}

function dispatchFetch(listeners: Record<string, Listener[]>, path: string, headers: HeadersInit, destination: string) {
  const event = {
    request: { method: 'GET', url: `https://t${path}`, headers: new Headers(headers), destination },
    respondWith: vi.fn(),
  };
  for (const listener of listeners.fetch ?? []) listener(event);
  return event;
}

describe('companion service worker', () => {
  it('does not intercept a Range request under the cache-first prefix', () => {
    const event = dispatchFetch(loadWorker(), '/dashboard/v2/assets/x.mp4', { range: 'bytes=0-1' }, '');
    expect(event.respondWith).not.toHaveBeenCalled();
  });

  it.each(['video', 'audio'])('does not intercept destination %s even without a Range header', (destination) => {
    const event = dispatchFetch(loadWorker(), '/dashboard/v2/assets/x.mp4', {}, destination);
    expect(event.respondWith).not.toHaveBeenCalled();
  });

  it('does not intercept a video file opened as a document without a Range header', () => {
    // A long-press "open video" navigates to the file itself.
    const event = dispatchFetch(loadWorker(), '/dashboard/v2/assets/x.mp4', {}, 'document');
    expect(event.respondWith).not.toHaveBeenCalled();
  });

  it('still intercepts an immutable asset', async () => {
    const cached = new Response('cached');
    const listeners = loadWorker({
      match: vi.fn().mockResolvedValue(undefined),
      open: vi.fn().mockResolvedValue({ match: vi.fn().mockResolvedValue(cached), put: vi.fn() }),
    });
    const event = dispatchFetch(listeners, '/dashboard/v2/assets/app.js', {}, 'script');
    expect(event.respondWith).toHaveBeenCalledTimes(1);
    await expect(event.respondWith.mock.calls[0][0]).resolves.toBe(cached);
  });

  async function activate(v2HasShell: boolean) {
    const keys = vi.fn().mockResolvedValue(['nous-companion-v1', 'nous-companion-v2']);
    const del = vi.fn().mockResolvedValue(true);
    const v2 = { match: vi.fn().mockResolvedValue(v2HasShell ? new Response('shell') : undefined) };
    const open = vi.fn().mockResolvedValue(v2);
    const listeners = loadWorker({ keys, delete: del, open });
    let done: Promise<unknown> = Promise.resolve();
    for (const l of listeners.activate ?? []) l({ waitUntil: (p: Promise<unknown>) => (done = p) });
    await done;
    return { del, open, v2 };
  }

  it('the shell cache is v2: a changed worker re-runs install, re-warms the new shell and drops v1 (PR 6)', async () => {
    expect(source).toMatch(/const CACHE = 'nous-companion-v2';/);
    const { del, open, v2 } = await activate(true);
    expect(open).toHaveBeenCalledWith('nous-companion-v2');
    expect(v2.match).toHaveBeenCalledWith('/dashboard/v2/companion.html');
    expect(del).toHaveBeenCalledWith('nous-companion-v1');
    expect(del).not.toHaveBeenCalledWith('nous-companion-v2');
  });

  it('a failed warm (no shell in v2) keeps the old cache, so an offline launch still has a shell (review M6)', async () => {
    const { del } = await activate(false);
    expect(del).not.toHaveBeenCalled();
  });

  it('with an entry in both caches, v2 answers first: cache-first and the network-first fallback (review M6)', async () => {
    const v1 = new Response('v1');
    const v2entry = new Response('v2');
    const caches = {
      match: vi.fn().mockResolvedValue(v1), // caches.match searches the older v1 first
      open: vi.fn().mockResolvedValue({ match: vi.fn().mockResolvedValue(v2entry), put: vi.fn() }),
    };
    const asset = dispatchFetch(loadWorker(caches), '/dashboard/v2/assets/app.js', {}, 'script');
    await expect(asset.respondWith.mock.calls[0][0]).resolves.toBe(v2entry);
    const offline = vi.fn().mockRejectedValue(new TypeError('offline'));
    const shell = dispatchFetch(loadWorker(caches, offline), '/dashboard/v2/companion.html', {}, 'document');
    await expect(shell.respondWith.mock.calls[0][0]).resolves.toBe(v2entry);
  });

  it.each(['/dashboard/v2/companion.html', '/dashboard/v2/companion.webmanifest'])(
    '%s is network-first, so a new theme colour reaches an installed app',
    async (path) => {
      const fresh = new Response('fresh');
      const stale = new Response('stale');
      const put = vi.fn();
      const caches = { match: vi.fn().mockResolvedValue(stale), open: vi.fn().mockResolvedValue({ put }) };
      const listeners = loadWorker(caches, vi.fn().mockResolvedValue(fresh));
      const event = dispatchFetch(listeners, path, {}, 'document');
      await expect(event.respondWith.mock.calls[0][0]).resolves.toBe(fresh);
      expect(caches.open).toHaveBeenCalledWith('nous-companion-v2');
    },
  );
});
