import { MONTHS } from './harness';
import type { GraphStats } from './types/api';

const fmt = (n: number) => n.toLocaleString('en-US');
const links = (n: number) => `${fmt(n)} ${n === 1 ? 'link' : 'links'}`;

/** What part of the memory graph the page draws, in one or two sentences. */
export function graphSliceText(s: GraphStats): string {
  if (s.node_count === 0) return 'Memories and the links between them.';
  const chunks = s.chunks_included ? '' : ' Chunks are left out.';
  const capped = s.edges_truncated ? ' Only the strongest links among them are drawn.' : '';
  if (s.node_count >= s.total_nodes) {
    return `Every linked memory: ${fmt(s.node_count)}, with ${links(s.displayed_edges)}.${capped}${chunks}`;
  }
  return (
    `${fmt(s.node_count)} of ${fmt(s.total_nodes)} linked memories and ${fmt(s.displayed_edges)} of ` +
    `${links(s.total_edges)}, grown outward from the newest along their strongest links.${capped}${chunks}`
  );
}
