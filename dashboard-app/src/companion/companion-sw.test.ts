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

  // The upgrade warm (codex P2 on PR 6). The fakes behave like the browser:
  // `put` consumes the body, `match` hands back a fresh Response each time, and
  // the global `match` searches the caches in the order they were created.
  type Req = string | { url: string };
  const key = (request: Req) => {
    const url = new URL(typeof request === 'string' ? request : request.url, 'https://t');
    return url.pathname + url.search;
  };

  class FakeCache {
    readonly entries = new Map<string, string>();
    async match(request: Req) {
      const body = this.entries.get(key(request));
      return body === undefined ? undefined : new Response(body);
    }
    async put(request: Req, response: Response) {
      this.entries.set(key(request), await response.text());
    }
  }

  class FakeCaches {
    readonly stores = new Map<string, FakeCache>();
    async open(name: string) {
      if (!this.stores.has(name)) this.stores.set(name, new FakeCache());
      return this.stores.get(name)!;
    }
    async keys() {
      return [...this.stores.keys()];
    }
    async delete(name: string) {
      return this.stores.delete(name);
    }
    async match(request: Req) {
      for (const cache of this.stores.values()) {
        const hit = await cache.match(request);
        if (hit) return hit;
      }
      return undefined;
    }
    entry(name: string, path: string) {
      return this.stores.get(name)?.entries.get(path);
    }
  }

  // A route answers with its body, or with that status (a mid-deploy 404); a
  // missing route is a dropped connection. Read at call time, so a test can
  // take the network away or bring it back between lifecycle events.
  function network(routes: Record<string, string | number>) {
    return vi.fn(async (request: Req) => {
      const answer = routes[key(request)];
      if (answer === undefined) throw new TypeError('Failed to fetch');
      return typeof answer === 'number' ? new Response('not found', { status: answer }) : new Response(answer);
    });
  }

  const V1 = 'nous-companion-v1';
  const V2 = 'nous-companion-v2';
  const SHELL = '/dashboard/v2/companion.html';
  const INDEX = '/a2ui/surfaces';
  const SURFACE = '/a2ui/surfaces/s1';
  const JS = '/dashboard/v2/assets/companion-new.js';
  const CSS = '/dashboard/v2/assets/companion-new.css';
  // An asset whose hash did not change between the two builds: the runtime
  // serves it from v1 and never writes it into v2.
  const SHARED = '/dashboard/v2/assets/disclose-version-same.js';
  const OLD_JS = '/dashboard/v2/assets/companion-old.js';
  const OLD_CSS = '/dashboard/v2/assets/companion-old.css';
  // The built entry's shape: favicon, manifest and apple icon are best-effort;
  // the module script, its preload and the stylesheet are what a launch needs.
  const shellHtml = (js: string, css: string) =>
    '<!doctype html><link rel="icon" href="/dashboard/v2/favicon.svg">' +
    '<link rel="manifest" href="/dashboard/v2/companion.webmanifest">' +
    '<link rel="apple-touch-icon" href="/dashboard/v2/icons/apple-touch-icon.png">' +
    `<script type="module" crossorigin src="${js}"></script>` +
    `<link rel="modulepreload" crossorigin href="${SHARED}">` +
    `<link rel="stylesheet" crossorigin href="${css}">`;
  const OLD_HTML = shellHtml(OLD_JS, OLD_CSS);
  const NEW_HTML = shellHtml(JS, CSS);
  const OLD_INDEX = { surfaces: [{ surface_id: 's1' }], latest_seq: 7 };
  const NEW_INDEX = { surfaces: [{ surface_id: 's1' }], latest_seq: 9 };

  /** An installed v1: a launchable shell and the offline feed. */
  async function withV1() {
    const storage = new FakeCaches();
    const v1 = await storage.open(V1);
    for (const [path, body] of [
      [SHELL, OLD_HTML],
      [OLD_JS, 'old js'],
      [SHARED, 'shared js'],
      [OLD_CSS, 'old css'],
      [INDEX, JSON.stringify(OLD_INDEX)],
      [SURFACE, 'old snapshot'],
    ]) {
      v1.entries.set(path, body);
    }
    return storage;
  }

  const online = (): Record<string, string | number> => ({
    [SHELL]: NEW_HTML,
    [JS]: 'new js',
    [SHARED]: 'shared js',
    [CSS]: 'new css',
    '/dashboard/v2/favicon.svg': '<svg/>',
    '/dashboard/v2/companion.webmanifest': '{}',
    '/dashboard/v2/icons/apple-touch-icon.png': 'png',
    [INDEX]: JSON.stringify(NEW_INDEX),
    [SURFACE]: 'new snapshot',
  });

  function goOffline(routes: Record<string, string | number>) {
    for (const path of Object.keys(routes)) delete routes[path];
  }

  /** Dispatch a lifecycle event and wait on what it handed to waitUntil; a
   * rejected warm fails the test (a failed warm must never fail installation). */
  async function lifecycle(listeners: Record<string, Listener[]>, type: 'install' | 'activate') {
    const pending: Promise<unknown>[] = [];
    for (const l of listeners[type] ?? []) l({ waitUntil: (p: Promise<unknown>) => pending.push(p) });
    await Promise.all(pending);
  }

  async function upgrade(listeners: Record<string, Listener[]>) {
    await lifecycle(listeners, 'install');
    await lifecycle(listeners, 'activate');
  }

  async function launch(listeners: Record<string, Listener[]>, path = SHELL) {
    const event = dispatchFetch(listeners, path, {}, 'document');
    const response = (await event.respondWith.mock.calls[0][0]) as Response;
    await new Promise((resolve) => setTimeout(resolve, 0)); // let the runtime's cache.put land
    return response.text();
  }

  it('the shell cache is v2: a changed worker re-runs install, re-warms the new shell and drops v1 (PR 6)', async () => {
    expect(source).toMatch(/const CACHE = 'nous-companion-v2';/);
    const storage = await withV1();
    const routes = online();
    const listeners = loadWorker(storage, network(routes));
    await upgrade(listeners);
    expect(await storage.keys()).toEqual([V2]);
    for (const path of [JS, SHARED, CSS]) expect(storage.entry(V2, path)).toBeDefined();
    expect(storage.entry(V2, SHELL)).toBe(NEW_HTML);
    expect(storage.entry(V2, SURFACE)).toBe('new snapshot');
    expect(JSON.parse(storage.entry(V2, INDEX)!)).toMatchObject({ ...NEW_INDEX, partial: false });
    goOffline(routes);
    expect(await launch(listeners)).toBe(NEW_HTML);
  });

  it('a failed warm (no shell in v2) keeps the old cache, so an offline launch still has a shell (review M6)', async () => {
    const storage = await withV1();
    const listeners = loadWorker(storage, network({}));
    await upgrade(listeners);
    expect(await storage.keys()).toEqual([V1, V2]);
    expect(await launch(listeners)).toBe(OLD_HTML);
  });

  it.each([
    ['a dropped connection', undefined],
    ['a mid-deploy 404', 404],
  ])('interrupted after the HTML (%s): v2 stores no shell, v1 stays and launches offline', async (_, answer) => {
    const storage = await withV1();
    const routes = online();
    for (const path of [JS, SHARED, CSS]) {
      if (answer === undefined) delete routes[path];
      else routes[path] = answer;
    }
    const listeners = loadWorker(storage, network(routes));
    await upgrade(listeners);
    expect(await storage.keys()).toContain(V1);
    expect(storage.entry(V2, SHELL)).toBeUndefined();
    goOffline(routes);
    expect(await launch(listeners)).toBe(OLD_HTML);
  });

  it('interrupted after some assets: v1 stays, and a partial v2 never serves its shell, even once the runtime cached the new HTML', async () => {
    const storage = await withV1();
    const routes = online();
    delete routes[CSS];
    const listeners = loadWorker(storage, network(routes));
    await upgrade(listeners);
    expect(storage.entry(V2, JS)).toBe('new js');
    expect(storage.entry(V2, SHELL)).toBeUndefined();
    expect(await storage.keys()).toContain(V1);
    // An online visit: the network-first shell writes the new HTML into v2,
    // but its stylesheet is still nowhere.
    expect(await launch(listeners)).toBe(NEW_HTML);
    expect(storage.entry(V2, SHELL)).toBe(NEW_HTML);
    goOffline(routes);
    await lifecycle(listeners, 'activate');
    expect(await storage.keys()).toContain(V1);
    expect(await launch(listeners)).toBe(OLD_HTML);
  });

  it('an asset whose hash did not change counts only once v2 itself holds it: v1 is not retired on the strength of its own copy', async () => {
    const storage = await withV1();
    const routes = online();
    delete routes[SHARED];
    const listeners = loadWorker(storage, network(routes));
    await upgrade(listeners);
    expect(await storage.keys()).toContain(V1);
    // The runtime fills v2 with the new HTML and the changed assets; the
    // shared one is answered from v1 and never lands in v2.
    await launch(listeners);
    for (const path of [JS, CSS]) await launch(listeners, path);
    expect(storage.entry(V2, SHARED)).toBeUndefined();
    goOffline(routes);
    await lifecycle(listeners, 'activate');
    expect(await storage.keys()).toContain(V1);
    // Offline the new shell launches: every asset it needs is in some cache.
    expect(await launch(listeners)).toBe(NEW_HTML);
  });

  it('a 200 that is not the shell (no hashed assets, e.g. a sign-in page) never counts as a warm', async () => {
    const storage = await withV1();
    const routes = online();
    const signIn = '<!doctype html><p>Sign in</p>';
    routes[SHELL] = signIn;
    const listeners = loadWorker(storage, network(routes));
    await upgrade(listeners);
    expect(await storage.keys()).toContain(V1);
    expect(storage.entry(V2, SHELL)).toBeUndefined();
    // The runtime's network-first fetch does store it as v2's shell; that
    // still neither retires v1 nor launches offline.
    expect(await launch(listeners)).toBe(signIn);
    expect(storage.entry(V2, SHELL)).toBe(signIn);
    goOffline(routes);
    await lifecycle(listeners, 'activate');
    expect(await storage.keys()).toContain(V1);
    expect(await launch(listeners)).toBe(OLD_HTML);
  });

  it('the shell warms but a snapshot fails: the previous snapshot is carried into v2, and the index says partial', async () => {
    const storage = await withV1();
    const routes = online();
    delete routes[SURFACE];
    const listeners = loadWorker(storage, network(routes));
    await upgrade(listeners);
    expect(await storage.keys()).toEqual([V2]);
    expect(storage.entry(V2, SURFACE)).toBe('old snapshot');
    // A carried snapshot is older than the fresh watermark: the index must not
    // claim to be complete (the transport re-hydrates on `partial`).
    expect(JSON.parse(storage.entry(V2, INDEX)!)).toMatchObject({
      surfaces: [{ surface_id: 's1' }],
      partial: true,
      latest_seq: 0,
    });
    goOffline(routes);
    expect(await launch(listeners, SURFACE)).toBe('old snapshot');
  });

  it('the shell warms but the snapshot index fails: the previous index and its snapshots are carried into v2', async () => {
    const storage = await withV1();
    const routes = online();
    delete routes[INDEX];
    delete routes[SURFACE];
    const listeners = loadWorker(storage, network(routes));
    await upgrade(listeners);
    expect(await storage.keys()).toEqual([V2]);
    expect(storage.entry(V2, SURFACE)).toBe('old snapshot');
    expect(JSON.parse(storage.entry(V2, INDEX)!)).toMatchObject({ surfaces: [{ surface_id: 's1' }] });
  });

  it('retiring v1 never drops the offline feed: an index only v1 holds is carried into v2 first', async () => {
    const storage = await withV1();
    const v2 = await storage.open(V2);
    for (const [path, body] of [
      [SHELL, NEW_HTML],
      [JS, 'new js'],
      [SHARED, 'shared js'],
      [CSS, 'new css'],
    ]) {
      v2.entries.set(path, body);
    }
    const listeners = loadWorker(storage, network({}));
    await lifecycle(listeners, 'activate');
    expect(await storage.keys()).toEqual([V2]);
    expect(storage.entry(V2, SURFACE)).toBe('old snapshot');
    expect(JSON.parse(storage.entry(V2, INDEX)!)).toMatchObject({ ...OLD_INDEX, partial: false });
  });

  it.each([
    ['a snapshot only v1 holds is carried, and the index says partial', ['s1'], ['s1']],
    ['a snapshot no cache holds is dropped from the index', ['s1', 's2'], ['s1']],
  ])(
    "retiring v1 checks v2's own index too: %s",
    async (_, listed, kept) => {
      // v2's index came from the runtime's network-first fetch, which stored it
      // without its snapshots (the page closed, or they were served from v1).
      const storage = await withV1();
      const v2 = await storage.open(V2);
      for (const [path, body] of [
        [SHELL, NEW_HTML],
        [JS, 'new js'],
        [SHARED, 'shared js'],
        [CSS, 'new css'],
        [INDEX, JSON.stringify({ surfaces: listed.map((surface_id) => ({ surface_id })), latest_seq: 9 })],
      ]) {
        v2.entries.set(path, body);
      }
      const listeners = loadWorker(storage, network({}));
      await lifecycle(listeners, 'activate');
      expect(await storage.keys()).toEqual([V2]);
      expect(storage.entry(V2, SURFACE)).toBe('old snapshot');
      // The carried snapshot is older than v2's watermark.
      expect(JSON.parse(storage.entry(V2, INDEX)!)).toMatchObject({
        surfaces: kept.map((surface_id) => ({ surface_id })),
        partial: true,
        latest_seq: 0,
      });
    },
  );

  it('a second activation after a failed one completes the warm, and only then retires v1', async () => {
    const storage = await withV1();
    const routes: Record<string, string | number> = {};
    const listeners = loadWorker(storage, network(routes));
    await upgrade(listeners);
    expect(await storage.keys()).toEqual([V1, V2]);
    Object.assign(routes, online());
    await lifecycle(listeners, 'activate');
    expect(await storage.keys()).toEqual([V2]);
    expect(storage.entry(V2, SHELL)).toBe(NEW_HTML);
    for (const path of [JS, SHARED, CSS]) expect(storage.entry(V2, path)).toBeDefined();
    // The feed the offline install carried over is still there.
    expect(storage.entry(V2, SURFACE)).toBe('old snapshot');
    goOffline(routes);
    expect(await launch(listeners)).toBe(NEW_HTML);
  });

  it('with no complete shell in any cache, offline still serves the cached HTML (no old cache to fall back to)', async () => {
    const storage = new FakeCaches();
    (await storage.open(V2)).entries.set(SHELL, NEW_HTML);
    expect(await launch(loadWorker(storage, network({})))).toBe(NEW_HTML);
  });

  it('with an entry in both caches, v2 answers first: cache-first and the network-first fallback (review M6)', async () => {
    const storage = await withV1();
    const v2 = await storage.open(V2);
    for (const [path, body] of [
      [SHELL, NEW_HTML],
      [JS, 'new js'],
      [SHARED, 'shared js, v2 copy'],
      [CSS, 'new css'],
    ]) {
      v2.entries.set(path, body);
    }
    const listeners = loadWorker(storage, network({}));
    expect(await launch(listeners, SHARED)).toBe('shared js, v2 copy'); // the global match would answer v1's
    expect(await launch(listeners)).toBe(NEW_HTML);
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
