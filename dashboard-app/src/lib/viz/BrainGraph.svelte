<script lang="ts">
  // The memory graph as one mass in three dimensions: memories are bodies sized by their links, links are thin curved
  // fibres, and distance fades into the card. Tapping a memory moves in on it and sends activation along its links,
  // one step and then a second. The drawing library (3d-force-graph, on three.js) is loaded only when this mounts.
  import { onMount, untrack } from 'svelte';
  import { cssColor, resolveColors } from '../chartColors';
  import {
    activate,
    ageFade,
    endId,
    frameFor,
    hoverLabel,
    mixHex,
    type Activation,
    type BrainLink,
    type BrainNode,
  } from '../brainGraph';

  let {
    nodes,
    links,
    colorFor,
    onNodeClick,
    searchQuery = '',
    hiddenTypes = new Set<string>(),
    minEdges = 0,
    turn = true,
    covered = false,
  }: {
    nodes: BrainNode[];
    links: BrainLink[];
    /** A node's (by type) or a link's (by relation) colour as a token, `var(--x)`; resolved here for the canvas. */
    colorFor: (kind: 'node' | 'edge', key: string) => string;
    onNodeClick?: (node: BrainNode) => void;
    searchQuery?: string;
    hiddenTypes?: Set<string>;
    minEdges?: number;
    /** Turn the mass slowly so its depth reads. Never on under reduced motion. */
    turn?: boolean;
    /** Something (the detail sheet) is over the graph: the activation waits, and plays when it is uncovered. */
    covered?: boolean;
  } = $props();

  let el: HTMLDivElement;
  let graph: any = null; // plain let: the library's instance is not reactive state
  let failed = $state(false);
  let ready = $state(false);
  let active: Activation | null = null;
  let matches: Set<string> | null = null;
  let pulsed = false; // the active memory's activation has been sent
  let framed = false; // this layout has been put in view
  let radius = 400; // half the mass's width, for the fog

  const media = (q: string) => typeof matchMedia === 'function' && matchMedia(q).matches;
  const reduced = media('(prefers-reduced-motion: reduce)');
  const phone = media('(max-width: 768px)');

  const colors = new Map<string, string>();
  function color(kind: 'node' | 'edge', key: string): string {
    const k = `${kind}:${key}`;
    if (!colors.has(k)) colors.set(k, resolveColors(colorFor(kind, key), el));
    return colors.get(k)!;
  }
  // The card's surface and the fibre ink, read once (a token read is a style computation, and a repaint asks per node).
  let ground = '';
  let fibre = '';

  function nodeColor(n: BrainNode): string {
    const own = color('node', n.type);
    const base = mixHex(own, ground, ageFade(n.ageDays));
    if (active) {
      if (n.id === active.origin || active.hop1.has(n.id)) return own;
      if (active.hop2.has(n.id)) return mixHex(own, ground, 0.3);
      return mixHex(base, ground, 0.74);
    }
    if (matches) return matches.has(n.id) ? own : mixHex(base, ground, 0.8);
    return base;
  }
  /** The links the active memory's activation crosses. */
  let crossed = new Set<BrainLink>();
  function linkColor(l: BrainLink): string {
    if (crossed.has(l)) return color('edge', l.relation);
    return active || matches ? mixHex(fibre, ground, 0.7) : fibre;
  }
  /** New colours. A setter makes the library redraw, so only the accessors that changed are set. */
  const repaint = () => graph?.nodeColor(nodeColor).linkColor(linkColor);
  /** The active memory changed: the crossed links change width too (which rebuilds every link, so only here). */
  function setActive(next: Activation | null) {
    active = next;
    crossed = new Set(next ? [...next.first, ...next.second] : []);
    pulsed = false;
    repaint();
    graph?.linkWidth((l: BrainLink) => (crossed.has(l) ? 0.9 : 0));
  }

  const shown = (n: BrainNode) => !hiddenTypes.has(n.type) && n.degree >= minEdges;
  /** The memories whose text holds the search words; null with nothing searched. */
  function matchesOf(query: string): Set<string> | null {
    const q = query.toLowerCase().trim();
    return q ? new Set(nodes.filter((n) => n.label.toLowerCase().includes(q)).map((n) => n.id)) : null;
  }
  // A link's ends are memory ids until the library has laid the graph out, and the node objects after.
  let byId = new Map<string, BrainNode>();
  const linkShown = (l: BrainLink) => {
    const s = byId.get(endId(l.source));
    const t = byId.get(endId(l.target));
    return !!s && !!t && shown(s) && shown(t);
  };

  /** Pulls the web into one rounded mass: wider than tall on a wide screen, taller than wide on a narrow one. */
  function shapeForce() {
    let held: any[] = [];
    const tall = el.clientHeight > el.clientWidth;
    const kx = tall ? 0.085 : 0.03;
    const ky = tall ? 0.03 : 0.085;
    const force = (alpha: number) => {
      for (const n of held) {
        n.vx -= n.x * kx * alpha;
        n.vy -= n.y * ky * alpha;
        n.vz -= n.z * 0.06 * alpha;
      }
    };
    force.initialize = (list: any[]) => { held = list; };
    return force;
  }

  function frame(ms: number) {
    const box = graph?.getGraphBbox(shown);
    if (!box) return;
    const cam = graph.camera();
    const { center, distance } = frameFor(box, cam.fov, cam.aspect);
    graph.cameraPosition({ x: center.x, y: center.y, z: center.z + distance }, center, ms);
  }

  /** The fog follows the camera (the controls report every move, and every frame of the turn). */
  function updateFog() {
    const fog = graph?.scene().fog;
    if (!fog) return;
    const d = graph.camera().position.length();
    fog.near = Math.max(1, d - radius * 0.6);
    fog.far = d + radius * 1.9;
  }

  function clearActive() {
    if (!active) return;
    setActive(null);
    frame(reduced ? 0 : 700);
  }

  function tap(node: BrainNode) {
    const origin = node.id;
    setActive(activate(origin, graph.graphData().links.filter(linkShown)));
    // Close enough to see the neighbourhood the activation crosses.
    const far = Math.hypot(node.x ?? 0, node.y ?? 0, node.z ?? 0) || 1;
    const k = 1 + 300 / far;
    graph.cameraPosition({ x: (node.x ?? 0) * k, y: (node.y ?? 0) * k, z: (node.z ?? 0) * k }, node, reduced ? 0 : 900);
    onNodeClick?.(node);
    // The tap usually opens the detail sheet over the graph; then the waves wait for it to close (the effect below).
    setTimeout(() => { if (!covered) pulse(origin); }, 900);
  }

  /** Activation crossing the links: the first step now, the second a second later. */
  function pulse(origin: string) {
    if (reduced || !graph || active?.origin !== origin || pulsed) return;
    pulsed = true;
    const wave = (list: BrainLink[]) => { if (graph && active?.origin === origin) for (const l of list) graph.emitParticle(l); };
    wave(active.first);
    const second = active.second;
    setTimeout(() => wave(second), 1000);
  }

  function load() {
    if (!graph) return;
    framed = false;
    matches = matchesOf(searchQuery);
    setActive(null);
    // Copies: the library writes positions onto the objects it is given.
    const copies = nodes.map((n) => ({ ...n }));
    byId = new Map(copies.map((n) => [n.id, n]));
    graph.graphData({ nodes: copies, links: links.map((l) => ({ ...l })) });
    if (reduced) graph.warmupTicks(160).cooldownTicks(0);
    else graph.warmupTicks(20).cooldownTicks(170);
  }

  onMount(() => {
    let gone = false;
    let observer: ResizeObserver | null = null;
    (async () => {
      try {
        const [{ default: ForceGraph3D }, three] = await Promise.all([import('3d-force-graph'), import('three')]);
        if (gone) return;
        const size = () => ({ width: el.clientWidth || 300, height: el.clientHeight || 300 });
        ground = cssColor(el, '--surface');
        fibre = cssColor(el, '--gv-other');
        // The library types its accessors on its own bare node and link objects; ours carry more, so the chain is untyped.
        graph = (new ForceGraph3D(el, { controlType: 'orbit', rendererConfig: { antialias: !phone, alpha: false } }) as any)
          .width(size().width)
          .height(size().height)
          .backgroundColor(ground)
          .showNavInfo(false)
          .enableNodeDrag(false)
          .nodeRelSize(2.3)
          .nodeVal((n: BrainNode) => 1.5 + n.degree)
          .nodeResolution(phone ? 6 : 10)
          .nodeOpacity(0.96)
          .nodeLabel((n: BrainNode) => hoverLabel(n))
          .nodeVisibility(shown)
          .linkVisibility(linkShown)
          .linkOpacity(0.26)
          .linkCurvature(0.16)
          .linkCurveRotation((l: BrainLink) => endId(l.source).charCodeAt(0) % 6)
          .linkResolution(phone ? 5 : 8)
          .linkHoverPrecision(0)
          .linkDirectionalParticleWidth(2.4)
          .linkDirectionalParticleSpeed(0.018)
          .linkDirectionalParticleColor(() => cssColor(el, '--text'))
          .onNodeClick(tap)
          .onBackgroundClick(clearActive)
          .onEngineStop(() => {
            // The library also reports a stop after every redraw (a new colour, a new width): the mass is put in
            // view once per layout, or each repaint would undo the owner's own turn and zoom, and a tap's move in.
            if (framed) return;
            framed = true;
            const box = graph.getGraphBbox();
            if (box) radius = Math.max(box.x[1] - box.x[0], box.z[1] - box.z[0]) / 2;
            frame(reduced ? 0 : 700);
            setTimeout(updateFog, reduced ? 0 : 750);
          });
        graph.d3Force('charge').strength(-42);
        graph.d3Force('link').distance(24);
        graph.d3Force('center', null);
        graph.d3Force('shape', shapeForce());
        graph.renderer().setPixelRatio(Math.min(globalThis.devicePixelRatio || 1, 2));
        graph.scene().fog = new three.Fog(ground, 600, 3000);
        graph.controls().addEventListener('change', updateFog);
        graph.controls().autoRotateSpeed = 0.45;
        repaint();
        if (typeof ResizeObserver === 'function') {
          observer = new ResizeObserver(() => graph?.width(size().width).height(size().height));
          observer.observe(el);
        }
        ready = true;
      } catch {
        failed = true; // no WebGL, or the library did not load
      }
    })();
    return () => {
      gone = true;
      observer?.disconnect();
      graph?.renderer?.().forceContextLoss?.(); // a browser allows only a few WebGL contexts: give this one back now
      graph?._destructor?.();
      graph = null;
    };
  });

  // The data arrived or changed: the only place the layout runs.
  $effect(() => {
    void nodes;
    void links;
    if (ready) untrack(load);
  });

  $effect(() => {
    void hiddenTypes;
    void minEdges;
    if (!ready) return;
    untrack(() => {
      graph.nodeVisibility(shown).linkVisibility(linkShown);
      setActive(null);
    });
  });

  $effect(() => {
    const q = searchQuery;
    if (!ready) return;
    untrack(() => {
      matches = matchesOf(q);
      repaint();
    });
  });

  // Uncovered again with a memory still active: its activation plays where it can be seen.
  let wasCovered = false;
  $effect(() => {
    const now = covered;
    untrack(() => {
      if (wasCovered && !now && active) pulse(active.origin);
      wasCovered = now;
    });
  });

  // The mass holds still under the pointer, so a memory can be read and tapped.
  let hovering = $state(false);
  $effect(() => {
    const on = turn && !reduced && !hovering;
    if (ready) graph.controls().autoRotate = on;
  });
</script>

{#if failed}
  <p class="brain-failed">The 3D graph could not start in this browser. It needs WebGL.</p>
{/if}
<div
  class="brain"
  class:hidden={failed}
  bind:this={el}
  role="application"
  aria-label="Memory graph in three dimensions"
  onpointerenter={(e) => { if (e.pointerType === 'mouse') hovering = true; }}
  onpointerleave={() => { hovering = false; }}
></div>

<style>
  .brain {
    width: 100%;
    height: 68vh;
    min-height: 360px;
    overflow: hidden;
  }
  .brain.hidden {
    display: none;
  }
  /* The library's hover label: memory text on the card's own surface. */
  .brain :global(.float-tooltip-kap) {
    font-family: var(--font-ui);
    font-size: 12px;
    color: var(--text);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--ctl-radius);
    padding: 3px 8px;
    max-width: 320px;
  }
  .brain-failed {
    color: var(--muted);
    font-size: 0.875rem;
    padding: 3rem 1.25rem;
    text-align: center;
    margin: 0;
  }
  @media (max-width: 768px) {
    .brain {
      height: 62vh;
    }
  }
</style>
