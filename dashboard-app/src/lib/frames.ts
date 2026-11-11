// The frame vocabulary of the Work trace and the Overview timeline: each frame's colour, why it was chosen, the
// sentence said when it changes, and the critic's verdict. Pure functions. Every field they read may be null, or
// absent on an API older than the redesign, so they compare with `== null`.
import type { WorkTurn } from './types/api';

export const FRAME_IDS = ['conversation', 'question', 'task', 'decision', 'debug', 'creative', 'initiation'] as const;
const KNOWN: ReadonlySet<string> = new Set(FRAME_IDS);

/** The frame's colour token, `var(--f-task)`; a frame id outside the seven reads the neutral `--f-other` (so it never
 * passes for conversation), and a missing frame the conversation grey. */
export function frameVar(id: string | null | undefined): string {
  if (id == null || id === '') return 'var(--f-conversation)';
  return KNOWN.has(id) ? `var(--f-${id})` : 'var(--f-other)';
}

/** The frame's name (its id), or `No frame recorded`. */
export function frameLabel(id: string | null | undefined): string {
  return id == null || id === '' ? 'No frame recorded' : id;
}

/** `chosen by referent`, or `reason not recorded` (rows older than migration 106). */
export function chosenBy(reason: string | null | undefined): string {
  return reason == null || reason === '' ? 'reason not recorded' : `chosen by ${reason}`;
}

export type Verdict = 'agreed' | 'disagreed' | 'overrode' | null;

/**
 * The critic's verdict on a turn's frame, or null when there is nothing to say. `critic` as the frame reason is an
 * override (and then the critic's frame is the one that stood, so it is tested first); no model recommendation
 * (`critic_frame` null or `unknown`), no recorded reason, or no frame give no verdict.
 */
export function criticVerdict(t: Pick<WorkTurn, 'frame_id' | 'frame_reason' | 'critic_frame'>): Verdict {
  if (t.frame_reason == null) return null;
  if (t.frame_reason === 'critic') return 'overrode';
  if (t.critic_frame == null || t.critic_frame === 'unknown' || t.frame_id == null) return null;
  return t.critic_frame === t.frame_id ? 'agreed' : 'disagreed';
}

/** The verdict pill's words. `disagreed` names the frame that was kept. */
export function verdictText(v: Exclude<Verdict, null>, frameId: string | null | undefined): string {
  if (v === 'agreed') return 'Critic agreed';
  if (v === 'overrode') return 'Critic overrode it';
  return `Critic disagreed, kept ${frameLabel(frameId)}`;
}

/** Why the frame changed, by the new turn's frame reason. */
function changeWhy(cur: WorkTurn): string {
  const reason = cur.frame_reason;
  if (reason == null) return 'The reason was not recorded.';
  switch (reason) {
    case 'referent':
      return 'Your short reply answered my offer.';
    case 'critic': {
      const beat = cur.critic?.heuristic_frame;
      return beat ? `The critic chose ${cur.frame_id} over ${beat}.` : `The critic chose ${cur.frame_id}.`;
    }
    case 'pattern':
      return 'A new topic.';
    case 'direct':
      return 'Set directly, not chosen from your message.';
    case 'default':
      return 'No pattern matched, so the default frame applied.';
    default:
      return `Chosen by ${reason}.`;
  }
}

/**
 * The frame change between two adjacent turns, said in one sentence, or null: no previous turn, a missing frame on
 * either side, the same frame, or a carried frame (carry keeps the frame, so it never changes one).
 */
export function frameChange(prev: WorkTurn | undefined, cur: WorkTurn): { from: string; to: string; why: string } | null {
  if (!prev || prev.frame_id == null || cur.frame_id == null) return null;
  if (prev.frame_id === cur.frame_id || cur.frame_reason === 'carried') return null;
  return { from: prev.frame_id, to: cur.frame_id, why: changeWhy(cur) };
}

const msOf = (iso: string | null | undefined): number | null => {
  if (!iso) return null;
  const ms = Date.parse(iso);
  return Number.isNaN(ms) ? null : ms;
};

/** Seconds from the turn's start to its end; null when it has not ended, or the times are unreadable or reversed. */
export function replySeconds(t: Pick<WorkTurn, 'started_at' | 'ended_at'>): number | null {
  const a = msOf(t.started_at);
  const b = msOf(t.ended_at);
  if (a === null || b === null || b < a) return null;
  return (b - a) / 1000;
}

/** `7.8 s`, `0.0 s`; `—` for none. */
export function fmtSeconds(s: number | null): string {
  return s == null ? '—' : `${s.toFixed(1)} s`;
}

/** `HH:MM:SS` in UTC; `—` for none or an unreadable time. */
export function clockTime(iso: string | null | undefined): string {
  const ms = msOf(iso);
  return ms === null ? '—' : new Date(ms).toISOString().slice(11, 19);
}
