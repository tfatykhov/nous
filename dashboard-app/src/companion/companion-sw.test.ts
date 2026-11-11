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

function loadWorker(caches: unknown = {}): Record<string, Listener[]> {
  const listeners: Record<string, Listener[]> = {};
  const self = {
    location: { origin: 'https://t' },
    addEventListener(type: string, fn: Listener) {
      (listeners[type] ??= []).push(fn);
    },
    skipWaiting: vi.fn(),
    clients: { claim: vi.fn() },
  };
  new Function('self', 'caches', 'fetch', source)(self, caches, vi.fn());
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
    const listeners = loadWorker({ match: vi.fn().mockResolvedValue(cached) });
    const event = dispatchFetch(listeners, '/dashboard/v2/assets/app.js', {}, 'script');
    expect(event.respondWith).toHaveBeenCalledTimes(1);
    await expect(event.respondWith.mock.calls[0][0]).resolves.toBe(cached);
  });
});
