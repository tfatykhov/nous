import type { IntentionNode } from './types/api';

/** Latency in plain words; null is "no sample", never zero. */
export function fmtSeconds(s: number | null): string {
  if (s === null) return '—';
  const whole = Math.round(s);
  if (whole < 60) return `${whole} s`;
  return `${Math.floor(whole / 60)} min ${whole % 60} s`;
}

/** Every descendant of a node, itself excluded. */
export function countNodes(node: Pick<IntentionNode, 'children'>): number {
  return node.children.reduce((n, c) => n + 1 + countNodes(c), 0);
}
