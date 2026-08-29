import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { Transport, type StreamFactory, type StreamHandlers } from './transport';
import { store } from './store.svelte';

// Transport talks to the MODULE-SINGLETON store, so every test resets it.
// EventSource and fetch are both injected: jsdom has no EventSource, and a
// fake fetch lets the whole connect cycle resolve without timers. Note the
// success path schedules nothing — only the catch arms a backoff setTimeout,
// so these tests never leave a pending timer behind.

interface FakeResponse {
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
  headers: { get: (name: string) => string | null };
}

function response(body: unknown, status = 200, headers: Record<string, string> = {}): FakeResponse {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    headers: { get: (name: string) => headers[name.toLowerCase()] ?? null },
  };
}

function fakeFetch(handler: (url: string, init?: RequestInit) => FakeResponse) {
  const calls: { url: string; init?: RequestInit }[] = [];
  const impl = ((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, init });
    return Promise.resolve(handler(url, init) as unknown as Response);
  }) as typeof fetch;
  return { impl, calls };
}

function fakeStream() {
  const opened: string[] = [];
  const state: { handlers: StreamHandlers | null; closed: number } = { handlers: null, closed: 0 };
  const factory: StreamFactory = (url, handlers) => {
    opened.push(url);
    state.handlers = handlers;
    return {
      close: () => {
        state.closed += 1;
      },
    };
  };
  return { factory, opened, state };
}

function createEnvelope(surfaceId: string, nonce = 'nonce-' + surfaceId, priority = 0) {
  return {
    version: 'v1.0',
    createSurface: {
      surfaceId,
      catalogId: 'https://a2ui.org/specification/v1_0/catalogs/basic/catalog.json',
      sendDataModel: true,
      metadata: { extensions: { com_nous_nonce: nonce, com_nous_priority: priority } },
      components: [{ id: 'root', component: 'Text', text: surfaceId }],
      dataModel: { formData: { choice: 'yes' } },
    },
  };
}

/** Let queued microtasks and 0ms macrotasks settle (reconnect is fire-and-forget). */
async function flush(): Promise<void> {
  for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0));
}

let active: Transport | null = null;

beforeEach(() => {
  store.reset();
  store.connection = 'connecting';
});

afterEach(() => {
  active?.stop();
  active = null;
});

describe('Transport — hydration-first connect', () => {
  it('prunes a local surface the live index no longer lists (zombie surface)', async () => {
    // The regression this exists for: we go offline, the surface resolves and
    // is deleted server-side, and the deleteSurface envelope ages out of the
    // replay window. A live-filtered replay alone would leave it on screen.
    store.apply(null, createEnvelope('zombie'));
    expect(store.surfaces.zombie).toBeDefined();

    const stream = fakeStream();
    const { impl, calls } = fakeFetch((url) => {
      if (url === '/a2ui/surfaces') return response({ latest_seq: 5, surfaces: [{ surface_id: 'live1' }] });
      if (url === '/a2ui/surfaces/live1') return response(createEnvelope('live1'));
      throw new Error('unexpected url ' + url);
    });
    active = new Transport({ streamFactory: stream.factory, fetchImpl: impl });
    await active.connect();

    expect(store.surfaces.zombie).toBeUndefined();
    expect(store.surfaces.live1).toBeDefined();
    expect(calls[0].url).toBe('/a2ui/surfaces');
  });

  it('applies every listed surface snapshot before opening the stream', async () => {
    const stream = fakeStream();
    const { impl } = fakeFetch((url) => {
      if (url === '/a2ui/surfaces')
        return response({
          latest_seq: 12,
          surfaces: [{ surface_id: 'a' }, { surface_id: 'b' }],
        });
      if (url === '/a2ui/surfaces/a') return response(createEnvelope('a', 'na', 2));
      if (url === '/a2ui/surfaces/b') return response(createEnvelope('b', 'nb', 0));
      throw new Error('unexpected url ' + url);
    });
    active = new Transport({ streamFactory: stream.factory, fetchImpl: impl });
    await active.connect();

    expect(Object.keys(store.surfaces).sort()).toEqual(['a', 'b']);
    expect(store.surfaces.a.components.root.text).toBe('a');
    expect(store.surfaces.a.nonce).toBe('na');
    expect(store.surfaces.a.priority).toBe(2);
    expect(store.connection).toBe('live');
  });

  it('opens the stream at the index watermark', async () => {
    const stream = fakeStream();
    const { impl } = fakeFetch((url) =>
      url === '/a2ui/surfaces' ? response({ latest_seq: 42, surfaces: [] }) : response({}),
    );
    active = new Transport({ streamFactory: stream.factory, fetchImpl: impl });
    await active.connect();

    expect(stream.opened).toEqual(['/a2ui/stream?since=42']);
    expect(store.lastSeq).toBe(42);
  });

  it('URL-encodes surface ids in the snapshot request', async () => {
    const stream = fakeStream();
    const { impl, calls } = fakeFetch((url) => {
      if (url === '/a2ui/surfaces') return response({ latest_seq: 1, surfaces: [{ surface_id: 'a/b c' }] });
      return response(createEnvelope('a/b c'));
    });
    active = new Transport({ streamFactory: stream.factory, fetchImpl: impl });
    await active.connect();

    expect(calls[1].url).toBe('/a2ui/surfaces/' + encodeURIComponent('a/b c'));
  });

  it('applies live envelopes arriving on the stream', async () => {
    const stream = fakeStream();
    const { impl } = fakeFetch((url) =>
      url === '/a2ui/surfaces' ? response({ latest_seq: 3, surfaces: [] }) : response({}),
    );
    active = new Transport({ streamFactory: stream.factory, fetchImpl: impl });
    await active.connect();

    stream.state.handlers!.onA2ui(4, createEnvelope('pushed'));
    expect(store.surfaces.pushed).toBeDefined();
    expect(store.lastSeq).toBe(4);
  });

  it('marks the connection errored when the stream reports an error', async () => {
    const stream = fakeStream();
    const { impl } = fakeFetch((url) =>
      url === '/a2ui/surfaces' ? response({ latest_seq: 0, surfaces: [] }) : response({}),
    );
    active = new Transport({ streamFactory: stream.factory, fetchImpl: impl });
    await active.connect();
    stream.state.handlers!.onError();
    expect(store.connection).toBe('error');
  });
});

describe('Transport — stream errors re-enter hydration', () => {
  // Every stream error closes the source and re-runs the hydration-first
  // cycle (codex P1): EventSource's own Last-Event-ID resume treats the
  // highest seen seq as a contiguous floor, so a later-committing LOWER seq
  // in flight at disconnect would be lost by an auto-reconnect. Hydration
  // is the one always-correct path (and it also caps the oauth2-proxy
  // login-redirect retry loop).
  function connected(backoffMs = 0) {
    const stream = fakeStream();
    const counts = { index: 0 };
    const { impl } = fakeFetch((url) => {
      if (url === '/a2ui/surfaces') {
        counts.index += 1;
        return response({ latest_seq: 1, surfaces: [] });
      }
      return response({});
    });
    return { stream, counts, transport: new Transport({ streamFactory: stream.factory, fetchImpl: impl, backoffMs }) };
  }

  it('closes the stream and re-hydrates on the FIRST error', async () => {
    const { stream, counts, transport } = connected();
    active = transport;
    await transport.connect();
    expect(counts.index).toBe(1);

    stream.state.handlers!.onError();
    await flush();

    expect(stream.state.closed).toBeGreaterThanOrEqual(1);
    expect(counts.index).toBe(2);
  });
});

describe('Transport — control: resync', () => {
  it('resets the store and re-hydrates from scratch', async () => {
    const stream = fakeStream();
    let listed = [{ surface_id: 'first' }];
    const { impl } = fakeFetch((url) => {
      if (url === '/a2ui/surfaces') return response({ latest_seq: 7, surfaces: listed });
      const id = url.slice('/a2ui/surfaces/'.length);
      return response(createEnvelope(decodeURIComponent(id)));
    });
    active = new Transport({ streamFactory: stream.factory, fetchImpl: impl });
    await active.connect();
    expect(store.surfaces.first).toBeDefined();

    // Server forces a resync; meanwhile the surface set changed entirely.
    listed = [{ surface_id: 'second' }];
    stream.state.handlers!.onControl({ type: 'resync' });
    await flush();

    expect(store.surfaces.first).toBeUndefined();
    expect(store.surfaces.second).toBeDefined();
    expect(stream.opened).toEqual(['/a2ui/stream?since=7', '/a2ui/stream?since=7']);
    expect(stream.state.closed).toBeGreaterThanOrEqual(1);
  });

  it('ignores control frames it does not understand', async () => {
    const stream = fakeStream();
    const { impl } = fakeFetch((url) =>
      url === '/a2ui/surfaces' ? response({ latest_seq: 1, surfaces: [] }) : response({}),
    );
    active = new Transport({ streamFactory: stream.factory, fetchImpl: impl });
    await active.connect();
    stream.state.handlers!.onControl({ type: 'somethingElse' });
    await flush();
    expect(stream.opened).toHaveLength(1);
  });
});

describe('Transport — postAction', () => {
  beforeEach(() => {
    store.apply(null, createEnvelope('s1', 'secret-nonce'));
  });

  it('posts the A2UI action envelope with the nonce and the data model', async () => {
    const { impl, calls } = fakeFetch(() => response({ ok: true, message: 'approved', resolved: true }));
    active = new Transport({ fetchImpl: impl });

    const result = await active.postAction('s1', 'approve', 'btn_yes', { choice: 'yes' });

    expect(result).toEqual({ ok: true, message: 'approved', resolved: true });
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe('/a2ui/action');
    expect(calls[0].init?.method).toBe('POST');
    // Content-Type is the actual CSRF control on this endpoint, not decoration.
    expect(calls[0].init?.headers).toEqual({ 'Content-Type': 'application/json' });

    const body = JSON.parse(String(calls[0].init?.body));
    expect(body.version).toBe('v1.0');
    expect(body.action.name).toBe('approve');
    expect(body.action.surfaceId).toBe('s1');
    expect(body.action.sourceComponentId).toBe('btn_yes');
    expect(body.action.context).toEqual({ choice: 'yes' });
    expect(body.action.metadata.extensions.com_nous_nonce).toBe('secret-nonce');
    expect(Number.isNaN(Date.parse(body.action.timestamp))).toBe(false);
    expect(body.a2uiRendererDataModel).toEqual({
      version: 'v1.0',
      surfaces: { s1: { formData: { choice: 'yes' } } },
    });
  });

  it('sends the CURRENT data model, including local two-way edits', async () => {
    store.surfaces.s1.dataModel.formData = { choice: 'no', note: 'edited locally' };
    const { impl, calls } = fakeFetch(() => response({ ok: true }));
    active = new Transport({ fetchImpl: impl });
    await active.postAction('s1', 'approve', 'btn', {});
    const body = JSON.parse(String(calls[0].init?.body));
    expect(body.a2uiRendererDataModel.surfaces.s1.formData).toEqual({
      choice: 'no',
      note: 'edited locally',
    });
  });

  it('resolves (never throws) on a non-2xx, surfacing the server message', async () => {
    const { impl } = fakeFetch(() => response({ error: { message: 'nonce mismatch' } }, 409));
    active = new Transport({ fetchImpl: impl });
    const result = await active.postAction('s1', 'approve', 'btn', {});
    expect(result).toEqual({ ok: false, message: 'nonce mismatch' });
  });

  it('falls back to the status code when the error body carries no message', async () => {
    const { impl } = fakeFetch(() => response({}, 500));
    active = new Transport({ fetchImpl: impl });
    const result = await active.postAction('s1', 'approve', 'btn', {});
    expect(result).toEqual({ ok: false, message: 'HTTP 500' });
  });

  it('resolves with the failure when the network throws', async () => {
    const impl = (() => Promise.reject(new Error('offline'))) as unknown as typeof fetch;
    active = new Transport({ fetchImpl: impl });
    const result = await active.postAction('s1', 'approve', 'btn', {});
    expect(result).toEqual({ ok: false, message: 'offline' });
  });

  it('still posts for an unknown surface, with an empty nonce and model', async () => {
    const { impl, calls } = fakeFetch(() => response({ ok: true }));
    active = new Transport({ fetchImpl: impl });
    await active.postAction('ghost', 'approve', 'btn', {});
    const body = JSON.parse(String(calls[0].init?.body));
    expect(body.action.metadata.extensions.com_nous_nonce).toBe('');
    expect(body.a2uiRendererDataModel.surfaces.ghost).toEqual({});
  });
});

describe('Transport — partial offline-warm index (F092 Phase 4)', () => {
  it('re-hydrates once from the live index after the stream OPENS on a partial one', async () => {
    // First index is the SW's degraded warm (partial, one surface). The
    // refresh must wait for the stream's OPEN event — construction is not
    // connection (codex round 6): offline, the EventSource exists but
    // never opens, and re-cycling then would just consume the same cached
    // partial index.
    const stream = fakeStream();
    let indexCalls = 0;
    const { impl } = fakeFetch((url) => {
      if (url === '/a2ui/surfaces') {
        indexCalls += 1;
        if (indexCalls === 1) {
          return response({ latest_seq: 0, partial: true, surfaces: [{ surface_id: 'a' }] });
        }
        return response({
          latest_seq: 9,
          surfaces: [{ surface_id: 'a' }, { surface_id: 'b' }],
        });
      }
      if (url === '/a2ui/surfaces/a') return response(createEnvelope('a'));
      if (url === '/a2ui/surfaces/b') return response(createEnvelope('b'));
      throw new Error('unexpected url ' + url);
    });
    active = new Transport({ streamFactory: stream.factory, fetchImpl: impl });
    await active.connect();
    await flush();

    // No open yet (still "connecting") — NO refresh may have happened.
    expect(indexCalls).toBe(1);

    stream.state.handlers!.onOpen();
    await flush();

    expect(indexCalls).toBe(2);
    expect(Object.keys(store.surfaces).sort()).toEqual(['a', 'b']);
    expect(store.connection).toBe('live');

    // The refreshed (full) stream opening again must not re-trigger.
    stream.state.handlers!.onOpen();
    await flush();
    expect(indexCalls).toBe(2);
  });

  it('does not loop when the refreshed index is partial again', async () => {
    const stream = fakeStream();
    let indexCalls = 0;
    const { impl } = fakeFetch((url) => {
      if (url === '/a2ui/surfaces') {
        indexCalls += 1;
        return response({ latest_seq: 0, partial: true, surfaces: [{ surface_id: 'a' }] });
      }
      if (url === '/a2ui/surfaces/a') return response(createEnvelope('a'));
      throw new Error('unexpected url ' + url);
    });
    active = new Transport({ streamFactory: stream.factory, fetchImpl: impl });
    await active.connect();
    await flush();
    stream.state.handlers!.onOpen();
    await flush();
    stream.state.handlers!.onOpen();
    await flush();

    expect(indexCalls).toBe(2);
  });
});

describe('Transport — callAgentFunction', () => {
  // Phase 2 RPC: the agentFunctionResponse envelope rides back in the HTTP
  // response, and the surface nonce travels in metadata.extensions exactly
  // like actions — the server's trust pipeline is shared.
  beforeEach(() => {
    store.apply(null, createEnvelope('s1', 'nonce-s1'));
  });

  it('posts the callAgentFunction envelope with the surface nonce', async () => {
    const { impl, calls } = fakeFetch(() =>
      response({
        version: 'v1.0',
        agentFunctionResponse: { functionCallId: 'x', value: { nodes: [] } },
      }),
    );
    active = new Transport({ fetchImpl: impl });

    const result = await active.callAgentFunction('s1', 'expandGraphNode', { nodeId: 'n-1' });

    expect(result.ok).toBe(true);
    expect(result.value).toEqual({ nodes: [] });
    expect(calls[0].url).toBe('/a2ui/call');
    const body = JSON.parse(String(calls[0].init?.body));
    expect(body.callAgentFunction.surfaceId).toBe('s1');
    expect(body.callAgentFunction.functionCallId).toBeTruthy();
    expect(body.callAgentFunction.callFunction).toEqual({
      call: 'expandGraphNode',
      args: { nodeId: 'n-1' },
    });
    expect(body.metadata.extensions.com_nous_nonce).toBe('nonce-s1');
  });

  it('surfaces the error message from an agentFunctionResponse error', async () => {
    const { impl } = fakeFetch(() =>
      response(
        {
          version: 'v1.0',
          agentFunctionResponse: {
            functionCallId: 'x',
            error: { code: 'RATE_LIMITED', message: 'too many calls; slow down' },
          },
        },
        429,
      ),
    );
    active = new Transport({ fetchImpl: impl });

    const result = await active.callAgentFunction('s1', 'expandGraphNode', {});

    expect(result).toEqual({ ok: false, message: 'too many calls; slow down' });
  });

  it('falls back to the status code when the error body is bare', async () => {
    const { impl } = fakeFetch(() => response({}, 503));
    active = new Transport({ fetchImpl: impl });

    const result = await active.callAgentFunction('s1', 'expandGraphNode', {});

    expect(result).toEqual({ ok: false, message: 'HTTP 503' });
  });

  it('resolves with the failure when the network throws', async () => {
    const impl = (() => Promise.reject(new Error('offline'))) as unknown as typeof fetch;
    active = new Transport({ fetchImpl: impl });

    const result = await active.callAgentFunction('s1', 'expandGraphNode', {});

    expect(result).toEqual({ ok: false, message: 'offline' });
  });
});
