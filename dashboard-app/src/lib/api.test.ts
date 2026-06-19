import { describe, it, expect, vi, beforeEach } from 'vitest';
import { apiGet } from './api';

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

  it('honors an AbortSignal', async () => {
    const ac = new AbortController(); ac.abort();
    vi.stubGlobal('fetch', vi.fn(async (_u, o) => {
      if (o?.signal?.aborted) throw new DOMException('aborted', 'AbortError');
      return new Response('{}', { status: 200 });
    }));
    await expect(apiGet('/status', { signal: ac.signal })).rejects.toThrow();
  });
});
