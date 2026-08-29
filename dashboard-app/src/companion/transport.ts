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
      // 2. Snapshot every live surface (full createSurface envelopes). The
      //    X-A2UI-Upto-Seq header is the surface's own watermark, read in
      //    the same DB statement as its state — envelopes at/below it must
      //    never be re-applied over local edits (codex P2).
      for (const { surface_id } of index.surfaces) {
        const res = await this.fetchImpl(`/a2ui/surfaces/${encodeURIComponent(surface_id)}`);
        if (!res.ok) throw new Error(`HTTP ${res.status} for surface ${surface_id}`);
        const snapshot = await res.json();
        const upto = Number(res.headers.get('x-a2ui-upto-seq') ?? 0);
        // Re-applying createSurface for an existing surface replaces it —
        // idempotent by construction.
        store.apply(null, snapshot as never);
        if (upto > 0) store.setSurfaceUpto(surface_id, upto);
      }
      store.setDeliveredFloor(index.latest_seq);
      // 3. Tail the stream from the index watermark.
      this.stream = this.streamFactory(`/a2ui/stream?since=${store.lastSeq}`, {
        onA2ui: (seq, envelope) => {
          store.apply(seq, envelope as never);
        },
        onControl: (data) => {
          if (data.type === 'resync') void this.reconnect();
        },
        onError: () => {
          // NEVER let EventSource auto-reconnect (codex P1): its
          // Last-Event-ID resume treats the highest seen seq as a
          // contiguous floor, so a later-committing LOWER seq that was
          // in flight when the connection dropped would be skipped by
          // both replay and the delivered-set floor. Every stream error
          // instead closes the source and re-enters the hydration-first
          // cycle, which re-fetches the live index and snapshots — the
          // one path that is always correct. (This also caps the
          // oauth2-proxy expired-session case, where auto-reconnect
          // would chase a login redirect forever.)
          store.connection = 'error';
          this.stream?.close();
          this.stream = null;
          this.attempt += 1;
          const delay = Math.min(this.backoffMs * 2 ** Math.min(this.attempt, 4), 30000);
          if (!this.stopped) setTimeout(() => void this.cycle(), delay);
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

  /**
   * POST a callAgentFunction RPC. Spec's HTTP request-response pattern: the
   * agentFunctionResponse envelope rides back in the HTTP response body, so
   * there is no functionCallId correlation over SSE. Errors resolve (not
   * throw) so callers paint them inline.
   */
  async callAgentFunction(
    surfaceId: string,
    call: string,
    args: Record<string, unknown>,
  ): Promise<{ ok: boolean; value?: unknown; message: string }> {
    const surface = store.surfaces[surfaceId];
    const body = {
      version: 'v1.0',
      callAgentFunction: {
        surfaceId,
        functionCallId: `fc-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        callFunction: { call, args },
      },
      metadata: { extensions: { com_nous_nonce: surface?.nonce ?? '' } },
    };
    try {
      const res = await this.fetchImpl('/a2ui/call', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const payload = (await res.json()) as {
        agentFunctionResponse?: { value?: unknown; error?: { message?: string } };
      };
      const afr = payload.agentFunctionResponse;
      if (!res.ok || afr?.error) {
        return { ok: false, message: afr?.error?.message ?? `HTTP ${res.status}` };
      }
      return { ok: true, value: afr?.value, message: '' };
    } catch (err) {
      return { ok: false, message: err instanceof Error ? err.message : 'network error' };
    }
  }
}

export const transport = new Transport();
