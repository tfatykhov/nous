import type { GraphStats } from './types/api';

const fmt = (n: number) => n.toLocaleString('en-US');

/** "Nov 9", "Nov 7–9" or "Oct 30 – Nov 2" (UTC days); '' when a date is missing or unreadable. */
export function dateSpan(from: string | null, to: string | null): string {
  if (!from || !to) return '';
  const a = new Date(from);
  const b = new Date(to);
  if (Number.isNaN(a.getTime()) || Number.isNaN(b.getTime())) return '';
  const month = (d: Date) => d.toLocaleDateString('en-US', { month: 'short', timeZone: 'UTC' });
  const day = (d: Date) => d.getUTCDate();
  if (month(a) === month(b) && a.getUTCFullYear() === b.getUTCFullYear()) {
    return day(a) === day(b) ? `${month(a)} ${day(a)}` : `${month(a)} ${day(a)}–${day(b)}`;
  }
  return `${month(a)} ${day(a)} – ${month(b)} ${day(b)}`;
}

/** What part of the memory graph the page draws, in one sentence. */
export function graphSliceText(s: GraphStats): string {
  if (s.displayed_edges === 0) return 'Memory nodes and the links between them.';
  if (s.linked_edges === 0 && s.displayed_edges === s.total_edges) {
    return `All ${fmt(s.total_edges)} links between memories.`;
  }
  const span = dateSpan(s.recent_from, s.recent_to);
  const recent = `The newest ${fmt(s.recent_edges)} links${span ? ` (${span})` : ''}`;
  const shown = `${fmt(s.displayed_edges)} of ${fmt(s.total_edges)} links shown.`;
  if (s.linked_edges === 0) return `${recent}. ${shown}`;
  return (
    `${recent}, plus up to ${s.links_per_memory} links from each of their memories ` +
    `to its best-linked neighbours. ${shown}`
  );
}
