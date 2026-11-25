// F119 PR 2: the Profiles tab of Browse. The answers are those of the /entity-profiles routes in nous/api/rest.py
// (rows built by list_row and detail_body in nous/heart/entity_profile.py); the changes need the owner's or the proxy's
// secret (the login proxy adds it, the dashboard sends none). Every sentence the owner reads is here (pinned by
// entity_profiles.test.ts). A name, a line or a fact the server sends is shown as a text node only. Nothing on this tab
// sends a profile to the model.

import { apiGet, apiRequest } from './api';
import { ACTOR_WORDS, n } from './autonomy_settings';
import { fmtUtc } from './harness';
import type { GraphNodeDetail } from './types/api';

// ── Answers ─────────────────────────────────────────────────────────────────

/** The writer's status of a row. */
export type WriterStatus = 'profile' | 'not_entity' | 'owner' | 'asked' | 'failed';
/** What the list can filter on: a writer status, or the profiles the owner removed. */
export type ListStatus = WriterStatus | 'removed';

/** The owner said the key is not an entity: who (the proof the call carried) and when. */
export interface OwnerVerdict { verdict: 'not_entity'; by: string | null; at: string }

/** One row of GET /entity-profiles. `lines` counts the lines whose cited facts are all live now. */
export interface ProfileRow {
  key: string;
  display_name: string | null;
  kind: string | null;
  status: WriterStatus;
  fact_count: number;
  source_count: number;
  tokens: number;
  written_at: string | null;
  active: boolean;
  owner_verdict: OwnerVerdict | null;
  lines: number;
}

export interface ProfileList {
  writer_enabled: boolean;
  profiles: ProfileRow[];
  /** Rows per status that match the search, whatever the status filter. */
  counts: Record<ListStatus, number>;
  /** Rows that match the search and the status filter. */
  total: number;
  limit: number;
  offset: number;
}

/** One line of a profile: its date comes from the facts it cites, never from the model. */
export interface ProfileLine {
  id: string;
  text: string;
  fact_ids: string[];
  said_on: string;
  date_from: string;
}

/** A line the owner removed, with who and when. */
export interface RemovedLine extends ProfileLine {
  removed_by: string | null;
  removed_at: string;
}

/** GET /entity-profiles/{key}: the live lines, and the lines the owner removed (newest first). */
export interface ProfileDetail {
  key: string;
  display_name: string | null;
  kind: string | null;
  status: WriterStatus;
  lines: ProfileLine[];
  rejected: string[];
  fact_count: number;
  source_count: number;
  tokens: number;
  written_at: string | null;
  owner_verdict: OwnerVerdict | null;
  removed_lines: RemovedLine[];
  unkeyed_mentions: number;
}

/** POST /entity-profiles/rebuild: what the one run did (more counts come with it). */
export interface RebuildAnswer { outcome: string; key: string | null }

// ── Requests ────────────────────────────────────────────────────────────────

export const PAGE_SIZE = 20;
const BASE = '/entity-profiles';

export function listPath(opts: { q?: string; status?: ListStatus | ''; offset?: number }): string {
  const p = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(opts.offset ?? 0) });
  if (opts.q) p.set('q', opts.q);
  if (opts.status) p.set('status', opts.status);
  return `${BASE}?${p}`;
}
export const profilePath = (key: string) => `${BASE}/${encodeURIComponent(key)}`;
export const restorePath = (key: string) => `${profilePath(key)}/restore`;
export const notEntityPath = (key: string) => `${profilePath(key)}/not-entity`;
export const linePath = (key: string, lineId: string) => `${profilePath(key)}/lines/${encodeURIComponent(lineId)}`;
export const restoreLinePath = (key: string, lineId: string) => `${linePath(key, lineId)}/restore`;
export const REBUILD_PATH = `${BASE}/rebuild`;
/** A fact's text, through the graph's node route (the one route that reads a fact by its id). */
export const factPath = (id: string) => `/dashboard/graph/node/${encodeURIComponent(id)}?type=fact`;

export const readList = (opts: { q?: string; status?: ListStatus | ''; offset?: number }) =>
  apiGet<ProfileList>(listPath(opts));
export const readProfile = (key: string) => apiRequest<ProfileDetail>(profilePath(key));
export const readFact = (id: string) => apiRequest<GraphNodeDetail>(factPath(id));
export const removeLine = (key: string, lineId: string) => apiRequest(linePath(key, lineId), 'DELETE');
export const restoreLine = (key: string, lineId: string) => apiRequest(restoreLinePath(key, lineId), 'POST');
export const removeProfile = (key: string) => apiRequest(profilePath(key), 'DELETE');
export const restoreProfile = (key: string) => apiRequest(restorePath(key), 'POST');
export const markNotEntity = (key: string) => apiRequest(notEntityPath(key), 'POST');
export const unmarkNotEntity = (key: string) => apiRequest(notEntityPath(key), 'DELETE');
export const rebuild = (key: string) => apiRequest<RebuildAnswer>(REBUILD_PATH, 'POST', { key });

// ── Words ───────────────────────────────────────────────────────────────────

export const TAB_LABEL = 'Profiles';
export const INTRO_TEXT =
  'A short, dated profile of each person, place or thing the facts talk about, written while Nous sleeps. Each line cites the facts it comes from. Profiles are not used in replies yet.';
export const WRITER_OFF_TEXT =
  'The profile writer is off on this server (NOUS_ENTITY_PROFILE_WRITER_ENABLED=false): no profile is written while Nous sleeps, and Write it again now is refused. The profiles already written are shown.';
export const EMPTY_TEXT = 'No profiles yet. They are written while Nous sleeps.';
export const NO_MATCH_TEXT = 'No profile matches.';
export const SEARCH_PLACEHOLDER = 'Search by name…';

export const STATUS_WORDS: Record<ListStatus, string> = {
  profile: 'Profile',
  not_entity: 'Not an entity',
  owner: 'You',
  asked: 'Waiting for a reply',
  failed: 'Could not be written',
  removed: 'Removed',
};
export const ALL_LABEL = 'All';
const FILTER_ORDER: ListStatus[] = ['profile', 'not_entity', 'owner', 'asked', 'failed', 'removed'];

/** The status filter: All, then each status with how many rows match the search. */
export function filterOptions(counts: Record<ListStatus, number> | null): { value: string; label: string }[] {
  const all = counts ? FILTER_ORDER.filter((s) => s !== 'removed').reduce((sum, s) => sum + (counts[s] ?? 0), 0) : null;
  const label = (words: string, count: number | null) => (count === null ? words : `${words} (${n(count)})`);
  return [
    { value: '', label: label(ALL_LABEL, all) },
    ...FILTER_ORDER.map((s) => ({ value: s, label: label(STATUS_WORDS[s], counts ? counts[s] ?? 0 : null) })),
  ];
}

/** A row's status in words: removed first, then the owner's verdict, then the writer's status. */
export function statusText(row: Pick<ProfileRow, 'status' | 'active' | 'owner_verdict'>): string {
  if (!row.active) return STATUS_WORDS.removed;
  if (row.owner_verdict) return `${STATUS_WORDS.not_entity}, you said so`;
  return STATUS_WORDS[row.status] ?? row.status;
}

/** The name the list shows: the name the facts write, else the key. */
export const nameText = (row: Pick<ProfileRow, 'key' | 'display_name'>) => row.display_name || row.key;

/** "Written Nov 24 10:00 UTC from the newest 40 of its 320 facts." */
export function writtenText(p: Pick<ProfileDetail, 'written_at' | 'source_count' | 'fact_count' | 'status'>): string {
  const when = fmtUtc(p.written_at);
  if (p.status !== 'profile' || p.source_count === 0) return `Last asked ${when}.`;
  if (p.source_count >= p.fact_count) return `Written ${when} from its ${n(p.fact_count)} facts.`;
  return `Written ${when} from the newest ${n(p.source_count)} of its ${n(p.fact_count)} facts.`;
}

/** Why a row has no lines, in words, by the writer's status. */
export const STATUS_HELP: Record<WriterStatus, string> = {
  profile: '',
  not_entity: 'Nous decided this key does not name one person, place or thing, so it has no profile. It asks again when its facts double or after 30 days.',
  owner: 'This key names you or Nous, so it has no profile.',
  asked: 'A model call was made and no reply came back yet. It is asked again after 24 hours.',
  failed: 'The last reply could not be used. It is asked again after 7 days.',
};
export const NO_LINES_TEXT = 'No line of this profile is shown now: every line cites a fact that is no longer live.';
export const UNKEYED_TEXT = (count: number) =>
  `${n(count)} more active ${count === 1 ? 'fact names' : 'facts name'} it without being linked to it, so the profile does not see ${count === 1 ? 'it' : 'them'}.`;

/** Who made a change, in words. */
export const whoText = (by: string | null) => (by ? ACTOR_WORDS[by] ?? '' : '');

/** "You said this is not an entity from the dashboard, Nov 24 10:00 UTC. ..." */
export function verdictText(v: OwnerVerdict): string {
  const who = whoText(v.by);
  return `You said this is not an entity${who ? ` ${who}` : ''}, ${fmtUtc(v.at)}. Nous neither writes nor asks about it until you take that back.`;
}

/** "Removed from the dashboard, Nov 24 10:00 UTC." */
export function removedText(line: Pick<RemovedLine, 'removed_by' | 'removed_at'>): string {
  const who = whoText(line.removed_by);
  return `Removed${who ? ` ${who}` : ''}, ${fmtUtc(line.removed_at)}. No later write brings back this line, or one citing exactly the same facts.`;
}

export const LINES_TITLE = 'Lines';
export const REMOVED_TITLE = 'Lines you removed';
export const FACTS_TITLE = 'The facts it cites';
export const REMOVED_PROFILE_TEXT = 'You removed this profile. Nous neither writes nor asks about it until you restore it.';
export const FACT_UNREADABLE_TEXT = 'This fact could not be read.';
export const LOADING_TEXT = 'Reading…';
export const WORKING_TEXT = 'Working…';
export const WRITING_TEXT = 'Writing it again…';

export const LABELS = {
  showFacts: 'Show its facts',
  hideFacts: 'Hide its facts',
  removeLine: 'Remove this line',
  restoreLine: 'Put it back',
  rebuild: 'Write it again now',
  notEntity: 'This is not an entity',
  undoNotEntity: 'It is an entity after all',
  removeProfile: 'Remove this profile',
  restoreProfile: 'Restore this profile',
} as const;

export const DONE_TEXT = {
  removeLine: 'Line removed.',
  restoreLine: 'Line put back.',
  removeProfile: 'Profile removed. Find it under Removed to restore it.',
  restoreProfile: 'Profile restored.',
  notEntity: 'Marked as not an entity.',
  undoNotEntity: 'Taken back. Nous writes this profile again when its facts change, or now with Write it again now.',
} as const;

/** What one "Write it again now" did, in words. */
export function rebuildText(outcome: string): string {
  const words: Record<string, string> = {
    written: 'Written again now.',
    not_entity: 'Nous decided this key is not an entity.',
    failed_closed: 'The reply could not be used. The profile was kept as it was.',
    no_reply: 'No reply came back. It can be asked again after 24 hours.',
    held: 'This was written in the last 24 hours. It stays as it is until they have passed.',
    owner: 'This key names you or Nous, so it has no profile.',
    inactive: 'This profile is removed. Restore it first.',
    owner_not_entity: 'You said this is not an entity. Take that back first.',
    no_llm: 'No model is set up to write profiles on this server.',
    skipped: 'Nothing was written: the profile was changed while it was being written.',
  };
  return words[outcome] ?? `Done (${outcome}).`;
}
