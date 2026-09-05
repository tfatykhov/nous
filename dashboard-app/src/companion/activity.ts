// F092.4: micro-app activity indicator — the pure part.
//
// Three components render "the agent is working" for a micro-app: the header
// (live stamp + rail), the footer (pressed control) and every section
// (dimming). They must agree on WHAT counts as working, so the definitions
// live here, once, and each view imports them — the F092.2 lesson was that
// two inline copies of the pendingAction freshness rule drift apart.
//
// Two sources of activity:
//   - refresh / refine: synchronous transport calls the footer brackets in
//     the store (`store.beginActivity` / `store.endActivity`);
//   - app.act: the SERVER stamps /meta/pendingAction {id, label, at,
//     timeout_s?}; the activity is derived from the data model so it survives
//     a reload and ends when the recompose replaces the model.

export type ActivityKind = 'refresh' | 'refine' | 'act';

export interface Activity {
  kind: ActivityKind;
  /** Which control was pressed: the refine option's id, the agent action's
   *  id, or 'refresh'. Ids, not labels — two agent actions may share a
   *  label (`_normalize_agent_actions` makes only ids unique), and a
   *  label-keyed record would light both spinners. */
  id: string;
  /** Epoch ms; elapsed time is measured from here. */
  startedAt: number;
  /** Identity of the begin, handed back by `store.beginActivity` so the
   *  footer that began a record can release THAT record and no other —
   *  a late response from a destroyed footer must not end what the
   *  replacement footer has since begun. Stamp-derived activity is 0:
   *  nobody owns it. */
  token: number;
  /** The call's HTTP response said ok, but what it promised still has to
   *  arrive over SSE: the server's pendingAction stamp for an agent action
   *  ('stamp'), or the model patches for refresh / refine ('model' — the
   *  response names the outbox seq of its last envelope, see `seq`). The
   *  record is HELD from this epoch ms for at most HOLD_WAIT_MS; the store
   *  ends it when it observes the arrival, a mounted footer ends it on
   *  timeout. On the record, not in a component, so it survives remounts. */
  holdSince?: number;
  holdFor?: 'stamp' | 'model';
  /** The last stamp identity the store had observed for this surface when
   *  the record began. A different one since means the stamp the app.act
   *  response promised has been seen — possibly before the response even
   *  landed. */
  stampSeenBefore?: string;
  /** The completion revision an ok refresh / refine response reported: the
   *  outbox seq of the call's LAST envelope (app.refresh: the /meta
   *  restamp; app.refine: the whole-model update). Set by holdForModel.
   *  The update has arrived once the store has seen that seq for the
   *  surface — delivered on the stream (SSE id = seq) or covered by a
   *  hydration snapshot's X-A2UI-Upto-Seq watermark. Never a timestamp:
   *  composedAt is second-precision, so two refreshes in one second share
   *  it, and a reconnect snapshot carries the refreshed data under the
   *  same stamp while suppressing the envelope. */
  seq?: number;
}

/** The completion revision carried by an app.refresh / app.refine ok
 *  response (`value.seq`), or undefined when the response names none — a
 *  record held without one can only time out: bounded busy, no claimed
 *  success, because nothing observable identifies its arrival. */
export function responseSeq(value: unknown): number | undefined {
  const seq = (value as { seq?: unknown } | null | undefined)?.seq;
  return typeof seq === 'number' && Number.isInteger(seq) && seq > 0 ? seq : undefined;
}

/** Present-tense verb the header shows next to the elapsed time. */
export const ACTIVITY_VERBS: Record<ActivityKind, string> = {
  refresh: 'refreshing',
  refine: 'rethinking layout',
  act: 'agent working',
};

/** How long the "updated just now" flash stays after a successful call. */
export const DONE_FLASH_MS = 3000;

/** What a successful HTTP response promises — the pendingAction stamp for
 *  an agent action, the model patches for refresh / refine — arrives on a
 *  DIFFERENT channel (SSE) and can land after the response. The record is
 *  held until the store observes it, for at most this long; past it the
 *  controls release without claiming an update (the server's own guard
 *  still refuses a duplicate action). */
export const HOLD_WAIT_MS = 10_000;

/** F092.2 fallback only: the pending stamp carries the server's own
 *  timeout_s; this is used when a stamp predates that field. */
export const PENDING_STALE_FALLBACK_MS = 5 * 60 * 1000;

export interface PendingAction {
  id: string;
  label: string;
  at: string;
  staleMs: number;
  /** Identity of THIS stamp: the server's subtask_id, else id@at. `at` alone
   *  is second-precision, so two stamps can share it. */
  key: string;
}

/** Parse /meta/pendingAction. Anything malformed is "no pending action". */
export function pendingActionOf(meta: unknown): PendingAction | null {
  if (!meta || typeof meta !== 'object') return null;
  const raw = (meta as { pendingAction?: unknown }).pendingAction;
  if (!raw || typeof raw !== 'object') return null;
  const p = raw as { id?: unknown; label?: unknown; at?: unknown; timeout_s?: unknown; subtask_id?: unknown };
  if (typeof p.id !== 'string' || typeof p.at !== 'string') return null;
  const timeoutS = typeof p.timeout_s === 'number' && p.timeout_s > 0 ? p.timeout_s : null;
  return {
    id: p.id,
    label: typeof p.label === 'string' ? p.label : p.id,
    at: p.at,
    staleMs: timeoutS !== null ? timeoutS * 1000 : PENDING_STALE_FALLBACK_MS,
    key: typeof p.subtask_id === 'string' && p.subtask_id ? p.subtask_id : `${p.id}@${p.at}`,
  };
}

/** A pending action is fresh while its stamp is younger than the server's
 *  timeout — past that the in-process watcher may have died with a restart,
 *  and an honest "no update" beats an infinite spinner. An unparsable `at`
 *  is never fresh. */
export function pendingIsFresh(pending: PendingAction | null, nowMs: number): boolean {
  if (!pending) return false;
  const at = Date.parse(pending.at);
  return Number.isFinite(at) && nowMs - at < pending.staleMs;
}

/** The activity a fresh pending action represents, or null. */
export function pendingActivity(meta: unknown, nowMs: number): Activity | null {
  const pending = pendingActionOf(meta);
  if (!pendingIsFresh(pending, nowMs)) return null;
  return { kind: 'act', id: pending!.id, startedAt: Date.parse(pending!.at), token: 0 };
}

/** Did a recompose land AFTER the tap at `tappedAtMs`? The recompose is the
 *  only path that advances /meta/composedAt: the failure watcher clears the
 *  pending stamp and writes /meta/actionError without touching it. Both
 *  values are second-precision server times, so strict > is deliberate —
 *  a same-second recompose is not a real LLM turn, and a missed flash is
 *  the benign failure. */
export function recomposedAfter(composedAt: string, tappedAtMs: number): boolean {
  const composed = Date.parse(composedAt);
  return Number.isFinite(composed) && composed > tappedAtMs;
}

/** "4s", "1m 12s", "1h 03m" — coarse on purpose; this is a wait, not a lap time. */
export function formatElapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, '0')}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${String(m % 60).padStart(2, '0')}m`;
}
