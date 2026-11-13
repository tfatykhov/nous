const API_BASE = ''; // same-origin; /dashboard/* and /status are root-relative

export interface ApiOpts { retries?: number; backoffMs?: number; signal?: AbortSignal; }

/** Error carrying the HTTP status so callers can branch (e.g. 404 = not captured). */
export interface ApiError extends Error { status?: number; }

export async function apiGet<T>(path: string, opts: ApiOpts = {}): Promise<T> {
  const { retries = 3, backoffMs = 1000, signal } = opts;
  let lastErr: unknown;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const res = await fetch(API_BASE + path, { signal });
      if (!res.ok) {
        const err: ApiError = new Error(`HTTP ${res.status} for ${path}`);
        err.status = res.status;
        throw err;
      }
      return (await res.json()) as T;
    } catch (err) {
      if (signal?.aborted) throw err;
      // 4xx responses are definitive (not-found, bad-request) — retrying won't
      // help, and burns the full backoff before surfacing the error. Only
      // retry 5xx and network/transport failures (no status).
      const status = (err as ApiError).status;
      if (status !== undefined && status >= 400 && status < 500) throw err;
      lastErr = err;
      if (attempt < retries) await sleep(backoffMs * 2 ** attempt);
    }
  }
  throw lastErr;
}

export async function apiSend<T>(path: string, body: unknown, method = 'PUT'): Promise<T> {
  const res = await fetch(API_BASE + path, {
    method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${path}`);
  return (await res.json()) as T;
}

/** A refused answer: its status and its parsed JSON body (undefined when it had none), so the route's message shows. */
export interface ApiAnswerError extends ApiError { body?: unknown; }

/**
 * One JSON request, no retry. Any method but GET sends `Content-Type: application/json`, with or without a body: a
 * route that refuses another type answers 415. A non-2xx answer throws an ApiAnswerError with `status` and `body`.
 */
export async function apiRequest<T>(path: string, method = 'GET', body?: unknown, signal?: AbortSignal): Promise<T> {
  const init: RequestInit = { method, signal };
  if (method !== 'GET') {
    init.headers = { 'Content-Type': 'application/json' };
    if (body !== undefined) init.body = JSON.stringify(body);
  }
  const res = await fetch(API_BASE + path, init);
  let parsed: unknown;
  try {
    parsed = await res.json();
  } catch {
    parsed = undefined; // an empty or non-JSON body (a plain-text 404)
  }
  if (!res.ok) {
    const err: ApiAnswerError = new Error(`HTTP ${res.status} for ${path}`);
    err.status = res.status;
    err.body = parsed;
    throw err;
  }
  return parsed as T;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
