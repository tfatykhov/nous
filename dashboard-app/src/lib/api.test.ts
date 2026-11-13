import { describe, it, expect, vi, beforeEach } from 'vitest';
import { apiGet, apiRequest } from './api';

describe('apiGet', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('returns parsed JSON on success', async () => {
    vi.stubGlobal('fetch', vi.fn(async () =>
      new Response(JSON.stringify({ ok: 1 }), { status: 200 })));
    expect(await apiGet<{ ok: number }>('/status')).toEqual({ ok: 1 });
  });

  it('retries then succeeds', async () => {
    const f = vi.fn()
      .mockResolvedValueOnce(new Response('', { status: 500 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: 2 }), { status: 200 }));
    vi.stubGlobal('fetch', f);
    expect(await apiGet('/status', { retries: 1, backoffMs: 1 })).toEqual({ ok: 2 });
    expect(f).toHaveBeenCalledTimes(2);
  });

  it('does NOT retry a 4xx and exposes the status', async () => {
    const f = vi.fn(async () => new Response('', { status: 404 }));
    vi.stubGlobal('fetch', f);
    await expect(apiGet('/context/log/x/payload', { retries: 3, backoffMs: 1 })).rejects.toMatchObject({
      status: 404,
    });
    expect(f).toHaveBeenCalledTimes(1); // definitive — no retries
  });

  it('honors an AbortSignal', async () => {
    const ac = new AbortController(); ac.abort();
    vi.stubGlobal('fetch', vi.fn(async (_u, o) => {
      if (o?.signal?.aborted) throw new DOMException('aborted', 'AbortError');
      return new Response('{}', { status: 200 });
    }));
    await expect(apiGet('/status', { signal: ac.signal })).rejects.toThrow();
  });
});

describe('apiRequest', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('a GET sends no content type and returns the parsed JSON', async () => {
    const f = vi.fn(async () => new Response(JSON.stringify({ ok: 1 }), { status: 200 }));
    vi.stubGlobal('fetch', f);
    expect(await apiRequest('/x')).toEqual({ ok: 1 });
    const init = (f.mock.calls[0] as unknown[])[1] as RequestInit;
    expect(init.method).toBe('GET');
    expect(init.headers).toBeUndefined();
  });

  it('every other method sends the JSON content type, with a body or without one', async () => {
    const f = vi.fn(async () => new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', f);
    await apiRequest('/x', 'PATCH', { a: 1 });
    await apiRequest('/x', 'DELETE');
    await apiRequest('/x/refresh', 'POST');
    const inits = f.mock.calls.map((c) => (c as unknown[])[1] as RequestInit);
    expect(inits.map((i) => i.headers)).toEqual(Array(3).fill({ 'Content-Type': 'application/json' }));
    expect(inits.map((i) => i.body)).toEqual(['{"a":1}', undefined, undefined]);
  });

  it('a refused answer throws with its status and parsed body, once (no retry)', async () => {
    const f = vi.fn(async () => new Response(JSON.stringify({ error: 'no such server' }), { status: 404 }));
    vi.stubGlobal('fetch', f);
    await expect(apiRequest('/x', 'DELETE')).rejects.toMatchObject({ status: 404, body: { error: 'no such server' } });
    expect(f).toHaveBeenCalledTimes(1);
  });

  it('a refused answer that is not JSON has no body', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('Not Found', { status: 404 })));
    await expect(apiRequest('/x')).rejects.toMatchObject({ status: 404, body: undefined });
  });
});
