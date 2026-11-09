import type { GraphStats } from './types/api';

const fmt = (n: number) => n.toLocaleString('en-US');
const links = (n: number) => `${fmt(n)} ${n === 1 ? 'link' : 'links'}`;

/** "Nov 9", "Nov 7–9", "Oct 30 – Nov 2" or, across years, "Dec 30, 2025 – Jan 2, 2026" (UTC days); '' when a
 *  date is missing or unreadable. */
export function dateSpan(from: string | null, to: string | null): string {
  if (!from || !to) return '';
  const a = new Date(from);
  const b = new Date(to);
  if (Number.isNaN(a.getTime()) || Number.isNaN(b.getTime())) return '';
  const month = (d: Date) => d.toLocaleDateString('en-US', { month: 'short', timeZone: 'UTC' });
  const day = (d: Date) => `${month(d)} ${d.getUTCDate()}`;
  if (a.getUTCFullYear() !== b.getUTCFullYear()) {
    return `${day(a)}, ${a.getUTCFullYear()} – ${day(b)}, ${b.getUTCFullYear()}`;
  }
  if (month(a) !== month(b)) return `${day(a)} – ${day(b)}`;
  return a.getUTCDate() === b.getUTCDate() ? day(a) : `${day(a)}–${b.getUTCDate()}`;
}

/** What part of the memory graph the page draws, in one sentence. */
export function graphSliceText(s: GraphStats): string {
  if (s.displayed_edges === 0) return 'Memory nodes and the links between them.';
  if (s.all_links) return `Every link between memories: ${links(s.displayed_edges)}.`;
  const span = dateSpan(s.recent_from, s.recent_to);
  const recent = `The newest ${links(s.recent_edges)}${span ? ` (${span})` : ''}`;
  const shown = `${fmt(s.displayed_edges)} of ${links(s.total_edges)} shown.`;
  if (s.linked_edges === 0) return `${recent}. ${shown}`;
  return (
    `${recent}, plus up to ${s.links_per_memory} links from each of their memories ` +
    `to its best-linked neighbours. ${shown}`
  );
}
