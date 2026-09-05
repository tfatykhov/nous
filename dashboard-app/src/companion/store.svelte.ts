// F092: reactive surface store (Svelte 5 runes class, single instance).
//
// Components are a plain Record, NOT a Map — $state deep-proxies plain
// objects/arrays only, and a Record keeps the whole store JSON-serializable
// for snapshot hydration and test assertions.
//
// The store dedupes by seq: replay and live-tail legitimately overlap, and
// EventSource auto-reconnect can redeliver. Envelopes without a seq (direct
// snapshot application) pass `null` and always apply.

import { pendingActionOf, recomposedAfter } from './activity';
import type { Activity, ActivityKind } from './activity';
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
  /** F093 §3.2 — theme id stamped as data-theme on this surface's root, so
   * two apps in the switcher theme independently. Empty = nous-default. */
  theme: string;
  /** Human title, used to label this surface's switcher chip. Empty = fall
   * back to the curated kind label. */
  title: string;
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
  /** F092.4: what a micro-app's footer has in flight, keyed by surface.
   * refresh / refine are synchronous transport calls the footer brackets
   * with beginActivity/endActivity; an agent action's activity is instead
   * derived from the server's /meta/pendingAction stamp (activity.ts), so
   * it survives a reload — this record only covers the POST itself. */
  activity = $state<Record<string, Activity>>({});
  /** When a surface's last call finished successfully — the header shows
   * "updated just now" for DONE_FLASH_MS after it. */
  doneAt = $state<Record<string, number>>({});
  /** The `at` (epoch ms) of the /meta/pendingAction stamp last observed
   * on each surface — the tap an agent action started from. Recorded and
   * resolved in noteStamp() at envelope arrival, so completion is detected
   * whether or not the app is on screen: a successful action is delivered
   * as deleteSurface + createSurface for the same id (the header is
   * destroyed and remounted), and the user may have focused another
   * surface entirely. A header mounted later only READS doneAt. */
  tappedAt = $state<Record<string, number>>({});
  /** Identity (subtask_id) of the latest /meta/pendingAction stamp
   * observed on each surface, recorded in apply() — so it does not depend
   * on which components are mounted — and never cleared when the stamp is
   * (only by reset). The footer compares it before and after its app.act
   * POST: a stamp seen since the tap already came, and may already have
   * gone. Identity, not `at`: stamps are second-precision, and an action
   * finishing plus a retap can land in the same server second. */
  stampSeen = $state<Record<string, string>>({});
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
        theme: String(ext['com_nous_theme'] ?? ''),
        title: String(ext['com_nous_title'] ?? ''),
      };
      this.noteStamp(cs.surfaceId);
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
      this.noteStamp(ud.surfaceId);
    } else if (envelope.deleteSurface) {
      delete this.surfaces[envelope.deleteSurface.surfaceId];
      // A removed surface has nothing in flight any more: the record ends
      // HERE, not in a footer's teardown — the deletion can arrive while
      // the user is on another surface and no footer is mounted, and a
      // record left behind would lock the replacement until a POST that
      // may never settle. tappedAt/doneAt stay: a replacement's
      // createSurface follows this and resolves them (noteStamp).
      delete this.activity[envelope.deleteSurface.surfaceId];
    }
  }

  /** Runs after every envelope that can change a surface's data model.
   * A stamp present: remember it (stampSeen for the footer, tappedAt as
   * the tap the action started from). No stamp but a remembered tap: the
   * action is over one way or the other — it COMPLETED only if the app
   * was recomposed after the tap (recomposedAfter: the failure watcher
   * clears the stamp and writes actionError without moving composedAt),
   * and doneAt is stamped with the ARRIVAL time, so a header mounted long
   * after cannot flash "updated just now" for it. */
  private noteStamp(surfaceId: string): void {
    const meta = (this.surfaces[surfaceId]?.dataModel as Record<string, unknown> | undefined)?.meta as
      | Record<string, unknown>
      | undefined;
    const p = pendingActionOf(meta);
    if (p) {
      this.stampSeen[surfaceId] = p.key;
      const at = Date.parse(p.at);
      if (Number.isFinite(at)) this.tappedAt[surfaceId] = at;
      return;
    }
    const tapped = this.tappedAt[surfaceId];
    if (tapped === undefined) return;
    delete this.tappedAt[surfaceId];
    const composedAt = meta?.composedAt;
    if (typeof composedAt === 'string' && recomposedAfter(composedAt, tapped)) {
      this.doneAt[surfaceId] = Date.now();
    }
  }

  /** Hydration-first reconnect: drop local surfaces the index no longer lists. */
  pruneAbsent(liveIds: Set<string>): void {
    for (const id of Object.keys(this.surfaces)) {
      if (!liveIds.has(id)) {
        delete this.surfaces[id];
        delete this.surfaceUpto[id];
        delete this.activity[id];
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

  private activitySeq = 0;

  /** Returns the record's token; pass it to endActivityIf to release only
   *  this record. */
  beginActivity(surfaceId: string, kind: ActivityKind, id: string): number {
    const token = ++this.activitySeq;
    this.activity[surfaceId] = { kind, id, startedAt: Date.now(), token };
    // A new call supersedes the previous call's "updated just now" flash.
    delete this.doneAt[surfaceId];
    return token;
  }

  /** End the surface's activity only if it is still the record `token`
   *  identifies. */
  endActivityIf(surfaceId: string, token: number, ok: boolean): void {
    if (this.activity[surfaceId]?.token === token) this.endActivity(surfaceId, ok);
  }

  /** The app.act POST succeeded before any stamp was observed: hold the
   *  record for the stamp (the footer's hold effect ends it). Returns
   *  false when the record is no longer `token` — nothing to hold. */
  holdForStamp(surfaceId: string, token: number): boolean {
    const a = this.activity[surfaceId];
    if (a?.token !== token) return false;
    a.holdSince = Date.now();
    return true;
  }

  endActivity(surfaceId: string, ok: boolean): void {
    delete this.activity[surfaceId];
    if (ok) this.doneAt[surfaceId] = Date.now();
  }

  /** Success reached by another route (an agent action's recompose). */
  markDone(surfaceId: string): void {
    this.doneAt[surfaceId] = Date.now();
  }

  reset(): void {
    this.surfaces = {};
    this.activity = {};
    this.doneAt = {};
    this.tappedAt = {};
    this.stampSeen = {};
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
