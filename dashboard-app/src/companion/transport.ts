// F092: companion transport — hydration-first connect, resumable SSE, and
// the action POST path.
//
// Reconnect protocol (zombie-surface fix from the plan review): on EVERY
// connect — cold start, manual reconnect, or server-forced resync — first
// fetch the live index, drop local surfaces not in it, hydrate missing ones
// from snapshots, and only then open the stream from the index's latest_seq.
// A live-filtered replay alone would drop the deleteSurface teardown for a
// surface that resolved while we were offline.
//
// EventSource is never constructed at module scope and is injectable: jsdom
// has no EventSource, so tests inject a fake stream factory and drive
// events synchronously.

import { store } from './store.svelte';

export interface StreamHandlers {
  onA2ui(seq: number | null, envelope: unknown): void;
  onControl(data: { type: string }): void;
  onError(): void;
}

export interface A2uiStream {
  close(): void;
}

export type StreamFactory = (url: string, handlers: StreamHandlers) => A2uiStream;

const defaultStreamFactory: StreamFactory = (url, handlers) => {
  const es = new EventSource(url);
  es.addEventListener('a2ui', (e: MessageEvent) => {
    const seq = e.lastEventId ? Number(e.lastEventId) : null;
    handlers.onA2ui(seq, JSON.parse(e.data));
  });
  es.addEventListener('control', (e: MessageEvent) => {
    handlers.onControl(JSON.parse(e.data));
  });
  es.onerror = () => handlers.onError();
  return { close: () => es.close() };
};

export interface TransportOptions {
  streamFactory?: StreamFactory;
  fetchImpl?: typeof fetch;
  /** Base backoff in ms between full reconnect cycles. */
  backoffMs?: number;
}

export class Transport {
  private streamFactory: StreamFactory;
  private fetchImpl: typeof fetch;
  private backoffMs: number;
  private stream: A2uiStream | null = null;
  private stopped = false;
  private attempt = 0;
  private streamErrors = 0;

  constructor(opts: TransportOptions = {}) {
    this.streamFactory = opts.streamFactory ?? defaultStreamFactory;
    this.fetchImpl = opts.fetchImpl ?? ((...args) => fetch(...args));
    this.backoffMs = opts.backoffMs ?? 2000;
  }

  async connect(): Promise<void> {
    this.stopped = false;
    await this.cycle();
  }

  stop(): void {
    this.stopped = true;
    this.stream?.close();
    this.stream = null;
  }

  private async cycle(): Promise<void> {
    if (this.stopped) return;
    this.stream?.close();
    this.stream = null;
    store.connection = this.attempt === 0 ? 'connecting' : 'resyncing';
    try {
      // 1. Hydrate the live index; prune anything the server no longer lists.
      const index = (await this.getJson('/a2ui/surfaces')) as {
        latest_seq: number;
        surfaces: { surface_id: string }[];
      };
      const liveIds = new Set(index.surfaces.map((s) => s.surface_id));
      store.pruneAbsent(liveIds);
      // 2. Snapshot every live surface (full createSurface envelopes).
      for (const { surface_id } of index.surfaces) {
        const snapshot = await this.getJson(`/a2ui/surfaces/${encodeURIComponent(surface_id)}`);
        // Re-applying createSurface for an existing surface replaces it —
        // idempotent by construction.
        store.apply(null, snapshot as never);
      }
      store.lastSeq = Math.max(store.lastSeq, index.latest_seq);
      // 3. Tail the stream from the index watermark.
      this.stream = this.streamFactory(`/a2ui/stream?since=${store.lastSeq}`, {
        onA2ui: (seq, envelope) => {
          this.streamErrors = 0;
          store.apply(seq, envelope as never);
        },
        onControl: (data) => {
          this.streamErrors = 0;
          if (data.type === 'resync') void this.reconnect();
        },
        onError: () => {
          // EventSource auto-reconnects with Last-Event-ID for short gaps.
          // But behind oauth2-proxy an expired session 302s the reconnect to
          // a login page — EventSource then errors and retries FOREVER. After
          // several consecutive errors with no event in between, stop the
          // stream and fall back to the full hydration cycle with backoff.
          store.connection = 'error';
          this.streamErrors += 1;
          if (this.streamErrors >= 5) {
            this.streamErrors = 0;
            this.stream?.close();
            this.stream = null;
            this.attempt += 1;
            const delay = Math.min(this.backoffMs * 2 ** Math.min(this.attempt, 4), 30000);
            if (!this.stopped) setTimeout(() => void this.cycle(), delay);
          }
        },
      });
      store.connection = 'live';
      this.attempt = 0;
    } catch {
      store.connection = 'error';
      this.attempt += 1;
      const delay = Math.min(this.backoffMs * 2 ** Math.min(this.attempt, 4), 30000);
      if (!this.stopped) setTimeout(() => void this.cycle(), delay);
    }
  }

  private async reconnect(): Promise<void> {
    store.reset();
    await this.cycle();
  }

  private async getJson(path: string): Promise<unknown> {
    const res = await this.fetchImpl(path);
    if (!res.ok) throw new Error(`HTTP ${res.status} for ${path}`);
    return res.json();
  }

  /**
   * POST a user action. Returns {ok, message} — a rejection resolves (not
   * throws) so callers paint it inline; the surface stays interactive.
   */
  async postAction(
    surfaceId: string,
    name: string,
    sourceComponentId: string,
    context: Record<string, unknown>,
  ): Promise<{ ok: boolean; message: string; resolved?: boolean }> {
    const surface = store.surfaces[surfaceId];
    const body = {
      version: 'v1.0',
      action: {
        name,
        surfaceId,
        sourceComponentId,
        timestamp: new Date().toISOString(),
        context,
        metadata: { extensions: { com_nous_nonce: surface?.nonce ?? '' } },
      },
      // sendDataModel: every surface we create sets it, so attach the model.
      a2uiRendererDataModel: {
        version: 'v1.0',
        surfaces: { [surfaceId]: surface?.dataModel ?? {} },
      },
    };
    try {
      const res = await this.fetchImpl('/a2ui/action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const payload = (await res.json()) as {
        ok?: boolean;
        message?: string;
        resolved?: boolean;
        error?: { message?: string };
      };
      if (!res.ok) {
        return { ok: false, message: payload.error?.message ?? `HTTP ${res.status}` };
      }
      return { ok: true, message: payload.message ?? '', resolved: payload.resolved };
    } catch (err) {
      return { ok: false, message: err instanceof Error ? err.message : 'network error' };
    }
  }
}

export const transport = new Transport();
