// The Graph screen's 3D drawing, the parts that need no WebGL: what is drawn, how activation spreads from a tapped
// memory, and the small sums behind colour and framing. lib/viz/BrainGraph.svelte draws with them.
import type { GraphData } from './types/api';

export interface BrainNode {
  id: string;
  type: string;
  label: string;
  category: string | null;
  /** Links drawn to or from this memory. */
  degree: number;
  /** Whole days since the memory was created; null when the row carries no date. */
  ageDays: number | null;
  x?: number;
  y?: number;
  z?: number;
}

export interface BrainLink {
  id: string;
  /** A memory id; the drawing library swaps in the node object once it has laid the graph out. */
  source: string | BrainNode;
  target: string | BrainNode;
  relation: string;
}

const DAY_MS = 86_400_000;

/** The drawing's nodes and links from the endpoint's data. An edge with an end that is not a node is left out. */
export function buildBrain(data: GraphData, now: number = Date.now()): { nodes: BrainNode[]; links: BrainLink[] } {
  const ids = new Set(data.nodes.map((n) => n.id));
  const degree = new Map<string, number>();
  const links: BrainLink[] = [];
  for (const e of data.edges) {
    if (!ids.has(e.source) || !ids.has(e.target)) continue;
    degree.set(e.source, (degree.get(e.source) ?? 0) + 1);
    degree.set(e.target, (degree.get(e.target) ?? 0) + 1);
    links.push({ id: e.id, source: e.source, target: e.target, relation: e.relation });
  }
  const nodes = data.nodes.map((n) => {
    const t = n.created_at ? Date.parse(n.created_at) : NaN;
    return {
      id: n.id,
      type: n.type,
      label: n.label ?? '',
      category: n.category ?? null,
      degree: degree.get(n.id) ?? 0,
      ageDays: Number.isFinite(t) ? Math.max(0, Math.floor((now - t) / DAY_MS)) : null,
    };
  });
  return { nodes, links };
}

export const endId = (end: string | BrainNode): string => (typeof end === 'string' ? end : end.id);

export interface Activation {
  /** The tapped memory. */
  origin: string;
  /** Memories one link away, and those a second link away (capped). */
  hop1: Set<string>;
  hop2: Set<string>;
  /** The links the activation crosses, in the two waves it crosses them. */
  first: BrainLink[];
  second: BrainLink[];
  /** The crossed links the activation travels against, from target to source: a link is followed either way. */
  backward: Set<BrainLink>;
  /** The second step stopped at the cap. */
  capped: boolean;
}

/** Spreading activation from one memory over the drawn links: its neighbours, then theirs, at most `cap` of those. */
export function activate(origin: string, links: BrainLink[], cap = 150): Activation {
  const near = new Map<string, [string, BrainLink][]>();
  const add = (a: string, b: string, l: BrainLink) => {
    const list = near.get(a);
    if (list) list.push([b, l]);
    else near.set(a, [[b, l]]);
  };
  for (const l of links) {
    const s = endId(l.source);
    const t = endId(l.target);
    add(s, t, l);
    add(t, s, l);
  }
  const hop1 = new Set<string>();
  const hop2 = new Set<string>();
  const first: BrainLink[] = [];
  const second: BrainLink[] = [];
  const backward = new Set<BrainLink>();
  let capped = false;
  for (const [m, l] of near.get(origin) ?? []) {
    if (m === origin) continue;
    hop1.add(m);
    first.push(l);
    if (endId(l.target) === origin) backward.add(l);
  }
  for (const m of hop1) {
    for (const [k, l] of near.get(m) ?? []) {
      if (k === origin || hop1.has(k) || hop2.has(k)) continue;
      if (hop2.size >= cap) {
        capped = true;
        continue;
      }
      hop2.add(k);
      second.push(l);
      if (endId(l.target) === m) backward.add(l);
    }
  }
  return { origin, hop1, hop2, first, second, backward, capped };
}

const ESCAPES: Record<string, string> = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

/** Memory text is untrusted, and the drawing library writes a hover label as HTML: every label goes through here. */
export function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (c) => ESCAPES[c]);
}

/** A memory's hover label: its type and the start of its text, safe to write as HTML. */
export function hoverLabel(n: Pick<BrainNode, 'type' | 'label'>, max = 80): string {
  const text = n.label.length > max ? n.label.slice(0, max - 1) + '…' : n.label;
  return escapeHtml(text ? `${n.type}: ${text}` : n.type);
}

/** How far a memory's colour fades toward the ground with age: none when new, half at 60 days and beyond. */
export function ageFade(ageDays: number | null): number {
  return ageDays == null ? 0.25 : Math.min(0.5, ageDays / 120);
}

/** The production build shortens a token's colour where it can (white ships as three digits), so both forms are read. */
function channels(hex: string): number[] | null {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return null;
  const h = m[1].length === 3 ? [...m[1]].map((c) => c + c).join('') : m[1];
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
}

/** `a` mixed `t` of the way toward `b`, both opaque hex colours (three or six digits); `a` unchanged when either is
 * anything else. */
export function mixHex(a: string, b: string, t: number): string {
  const x = channels(a);
  const y = channels(b);
  if (!x || !y) return a;
  return '#' + x.map((v, i) => Math.round(v + (y[i] - v) * t).toString(16).padStart(2, '0')).join('');
}

export interface Bbox {
  x: [number, number];
  y: [number, number];
  z: [number, number];
}

/** Where the camera stands to hold the whole mass in view: its centre, and the distance in front of it. */
export function frameFor(box: Bbox, fovDegrees: number, aspect: number): { center: { x: number; y: number; z: number }; distance: number } {
  const t = Math.tan((fovDegrees * Math.PI) / 360);
  const w = box.x[1] - box.x[0];
  const h = box.y[1] - box.y[0];
  const d = box.z[1] - box.z[0];
  return {
    center: { x: (box.x[0] + box.x[1]) / 2, y: (box.y[0] + box.y[1]) / 2, z: (box.z[0] + box.z[1]) / 2 },
    distance: Math.max(h / 2 / t, w / 2 / t / Math.max(aspect, 0.1)) * 0.92 + d / 2,
  };
}
