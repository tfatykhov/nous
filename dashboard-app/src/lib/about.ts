// The About screen: what this Nous is and where it runs. The answer is that of nous/api/about_dashboard.py, behind the
// owner guard (the login proxy adds the secret, the dashboard sends none). Every sentence the owner reads is here
// (pinned by about.test.ts). Values the server sends are shown as text nodes only.

import { fmtUtc } from './harness';
import { spanText } from './goals';

export const ABOUT_PATH = '/dashboard/about';

export interface AboutAnswer {
  name: string;
  version: string;
  build: { commit: string | null; time: string | null };
  started_at: string;
  uptime_seconds: number;
  python: string;
  schema: { latest: number | null };
  database: { server: string | null; pgvector: string | null };
  agent: { id: string; name: string };
  models: { main: string; background: string; critic: string; heartbeat: string; embeddings: string };
}

export const RELEASES_URL = 'https://github.com/tfatykhov/nous/releases';
export const SOURCE_URL = 'https://github.com/tfatykhov/nous';
export const LINKS: { label: string; href: string }[] = [
  { label: 'Release notes', href: RELEASES_URL },
  { label: 'Source code', href: SOURCE_URL },
];

export const SUBTITLE = 'What this Nous is and where it runs.';
export const LOADING = 'Loading…';
export const FAILED = 'This page could not be loaded.';
export const TRY_AGAIN = 'Try again';
export const NOT_AVAILABLE = 'Not available';
export const DB_DID_NOT_ANSWER = 'The database did not answer';
export const NEW_TAB = 'Opens in a new tab';

export const CARDS = { nous: 'Nous', agent: 'Agent', runningOn: 'Running on', links: 'Links' };
export const LABELS = {
  version: 'Version',
  build: 'Build',
  runningSince: 'Running since',
  name: 'Name',
  id: 'Id',
  python: 'Python',
  postgres: 'PostgreSQL',
  pgvector: 'pgvector',
  schema: 'Database schema',
};
export const MODELS_HEADING = 'Configured models';
export const MODEL_ROWS: [keyof AboutAnswer['models'], string][] = [
  ['main', 'Main'],
  ['background', 'Background work'],
  ['critic', 'Critic'],
  ['heartbeat', 'Heartbeat'],
  ['embeddings', 'Embeddings'],
];

/** A time fmtUtc can show, or null (it shows a dash for one it cannot read). */
function shownTime(iso: string | null): string | null {
  const text = fmtUtc(iso);
  return text === fmtUtc(null) ? null : text;
}

/** The Build row in pieces, the commit apart (`mono`) so the screen sets it in the code font; null when neither the
 * commit nor a time it can show is known. */
export function buildPieces(build: AboutAnswer['build']): { text: string; mono: boolean }[] | null {
  const built = shownTime(build.time);
  if (!build.commit && !built) return null;
  const pieces = build.commit ? [{ text: 'Commit ', mono: false }, { text: build.commit, mono: true }] : [];
  if (built) pieces.push({ text: build.commit ? `, built ${built}` : `Built ${built}`, mono: false });
  return pieces;
}

/** "Commit 2b97aa31c0de, built Nov 23 14:02 UTC", either half alone, or null when neither is known. */
export function buildLine(build: AboutAnswer['build']): string | null {
  return buildPieces(build)?.map((p) => p.text).join('') ?? null;
}

/** "Nov 23 14:02 UTC, up 3 days". */
export function runningSince(startedAt: string, uptimeSeconds: number): string {
  return `${fmtUtc(startedAt)}, up ${spanText(uptimeSeconds * 1000)}`;
}

/** "Migration 122", or "Not available". */
export function schemaLine(schema: AboutAnswer['schema']): string {
  return schema.latest === null ? NOT_AVAILABLE : `Migration ${schema.latest}`;
}

/** The whole database part failed: no schema, no server version, no pgvector version. */
export function databaseDown(a: Pick<AboutAnswer, 'schema' | 'database'>): boolean {
  return a.schema.latest === null && a.database.server === null && a.database.pgvector === null;
}
