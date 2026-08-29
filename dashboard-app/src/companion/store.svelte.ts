// F092: reactive surface store (Svelte 5 runes class, single instance).
//
// Components are a plain Record, NOT a Map — $state deep-proxies plain
// objects/arrays only, and a Record keeps the whole store JSON-serializable
// for snapshot hydration and test assertions.
//
// The store dedupes by seq: replay and live-tail legitimately overlap, and
// EventSource auto-reconnect can redeliver. Envelopes without a seq (direct
// snapshot application) pass `null` and always apply.

import { setPointer } from './pointer';

export interface A2uiComponent {
  id: string;
  component: string;
  [key: string]: unknown;
}

export interface SurfaceState {
  surfaceId: string;
  catalogId: string;
  components: Record<string, A2uiComponent>;
  dataModel: Record<string, unknown>;
  nonce: string;
  priority: number;
}

export type Connection = 'connecting' | 'live' | 'resyncing' | 'error';

interface Envelope {
  version?: string;
  createSurface?: {
    surfaceId: string;
    catalogId?: string;
    components?: A2uiComponent[];
    dataModel?: Record<string, unknown>;
    metadata?: { extensions?: Record<string, unknown> };
  };
  updateComponents?: { surfaceId: string; components: A2uiComponent[] };
  updateDataModel?: { surfaceId: string; path?: string; value: unknown };
  deleteSurface?: { surfaceId: string };
}

export class SurfaceStore {
  surfaces = $state<Record<string, SurfaceState>>({});
  connection = $state<Connection>('connecting');
  /** Highest seq seen — the resume point, NOT the dedupe test. */
  lastSeq = 0;
  /** Membership dedupe: seqs can legitimately arrive OUT OF ORDER (two
   * overlapping server transactions can commit 12 before 11), so a
   * monotonic watermark would drop 11 forever. Bounded by pruning below
   * the floor everything at/below which counts as seen. */
  private seenFloor = 0;
  private seen = new Set<number>();
  /** Per-surface snapshot watermarks: an envelope for surface S at
   * seq <= surfaceUpto[S] is already reflected in S's hydrated state
   * (the server reads state + watermark in one statement), so replaying
   * it would clobber input typed since hydration. */
  private surfaceUpto: Record<string, number> = {};

  private markSeen(seq: number): boolean {
    if (seq <= this.seenFloor || this.seen.has(seq)) return false;
    this.seen.add(seq);
    // Advance the floor ONLY through the contiguous prefix (codex P2):
    // jumping to min(seen) could hop a still-uncommitted lower seq and then
    // reject it forever. A persistent hole just lets the set grow — a few
    // thousand integers is nothing in a browser, and every stream error
    // re-hydrates anyway.
    while (this.seen.has(this.seenFloor + 1)) {
      this.seenFloor += 1;
      this.seen.delete(this.seenFloor);
    }
    return true;
  }

  /** Record a surface's snapshot watermark (from X-A2UI-Upto-Seq).
   *
   * Deliberately does NOT raise `lastSeq` (codex P1): the stream cursor
   * must stay at the INDEX watermark — a per-surface upto of 12 says
   * nothing about seq 11, which may be a create for a surface the index
   * missed. Uptos only SUPPRESS re-application in apply(); the stream
   * replays the in-between range and suppression handles the overlap. */
  setSurfaceUpto(surfaceId: string, uptoSeq: number): void {
    this.surfaceUpto[surfaceId] = Math.max(this.surfaceUpto[surfaceId] ?? 0, uptoSeq);
  }

  private targetOf(envelope: Envelope): string | null {
    return (
      envelope.createSurface?.surfaceId ??
      envelope.updateComponents?.surfaceId ??
      envelope.updateDataModel?.surfaceId ??
      envelope.deleteSurface?.surfaceId ??
      null
    );
  }

  /** Apply one envelope; seq=null bypasses dedupe (snapshot hydration). */
  apply(seq: number | null, envelope: Envelope): void {
    if (seq !== null) {
      if (!this.markSeen(seq)) return;
      this.lastSeq = Math.max(this.lastSeq, seq);
      const target = this.targetOf(envelope);
      if (target !== null && seq <= (this.surfaceUpto[target] ?? 0)) {
        // Already reflected in this surface's snapshot — applying it again
        // would clobber local edits made since hydration (codex P2).
        return;
      }
    }
    if (envelope.createSurface) {
      const cs = envelope.createSurface;
      const ext = cs.metadata?.extensions ?? {};
      const components: Record<string, A2uiComponent> = {};
      for (const comp of cs.components ?? []) components[comp.id] = comp;
      this.surfaces[cs.surfaceId] = {
        surfaceId: cs.surfaceId,
        catalogId: cs.catalogId ?? '',
        components,
        dataModel: (cs.dataModel ?? {}) as Record<string, unknown>,
        nonce: String(ext['com_nous_nonce'] ?? ''),
        priority: Number(ext['com_nous_priority'] ?? 0),
      };
    } else if (envelope.updateComponents) {
      const uc = envelope.updateComponents;
      const surface = this.surfaces[uc.surfaceId];
      if (!surface) return;
      for (const comp of uc.components) surface.components[comp.id] = comp;
    } else if (envelope.updateDataModel) {
      const ud = envelope.updateDataModel;
      const surface = this.surfaces[ud.surfaceId];
      if (!surface) return;
      if (ud.path === undefined || ud.path === '' || ud.path === '/') {
        surface.dataModel = (ud.value ?? {}) as Record<string, unknown>;
      } else {
        setPointer(surface.dataModel, ud.path, ud.value);
      }
    } else if (envelope.deleteSurface) {
      delete this.surfaces[envelope.deleteSurface.surfaceId];
    }
  }

  /** Hydration-first reconnect: drop local surfaces the index no longer lists. */
  pruneAbsent(liveIds: Set<string>): void {
    for (const id of Object.keys(this.surfaces)) {
      if (!liveIds.has(id)) {
        delete this.surfaces[id];
        delete this.surfaceUpto[id];
      }
    }
  }

  /**
   * Mark everything at/below the hydration watermark as delivered (codex
   * P2): snapshots already contain those envelopes' effects. Without this,
   * a long-lived connection to a DB with history sits above a permanent
   * hole — the contiguous-prefix loop can never advance and `seen` grows
   * with every future event.
   */
  setDeliveredFloor(seq: number): void {
    if (seq <= this.seenFloor) return;
    this.seenFloor = seq;
    this.lastSeq = Math.max(this.lastSeq, seq);
    this.seen = new Set([...this.seen].filter((s) => s > seq));
  }

  reset(): void {
    this.surfaces = {};
    this.lastSeq = 0;
    this.seenFloor = 0;
    this.seen = new Set();
    this.surfaceUpto = {};
  }

  /** Feed order: priority desc, then surface age via insertion order. */
  ordered(): SurfaceState[] {
    return Object.values(this.surfaces).sort((a, b) => b.priority - a.priority);
  }
}

export const store = new SurfaceStore();
