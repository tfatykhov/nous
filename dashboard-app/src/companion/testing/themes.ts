// Test-only reader of companion.css's theme blocks. Not imported by the app.
//
// A surface carries `data-theme="<id>"` on its own element, inside a page whose :root holds the default tokens. The
// browser computes a custom property where it is DECLARED: a `var()` in :root is substituted at :root (with :root's
// values), and the already-substituted value is what a themed element inherits. A `var()` declared in a theme block
// (or in a selector group that names the theme) is substituted at the themed element, with that element's values.
// `resolved(id)` follows exactly that cascade, so an alias that resolves to the wrong theme's value shows up here.

export type Decls = Record<string, string>;

export interface ThemeSheet {
  /** :root's own declarations. */
  root: Decls;
  /** Selector lists naming two or more themes (`[data-theme='a'], [data-theme='b']`), keyed by the list. */
  groups: Map<string, Decls>;
  /** Each theme's OWN single-selector block (`[data-theme='x']`), merged if it appears twice. */
  blocks: Map<string, Decls>;
  /** Every other plain rule (`html`, `body`, `[data-theme]` ...), keyed by its selector. */
  rules: Map<string, Decls>;
  /** Every theme id named by a block or a group. */
  ids: string[];
  /** Everything declared ON a themed element: the bare `[data-theme]` rule, the groups naming it and its own block,
   * merged in source order (equal specificity, so the later declaration wins). */
  declared(id: string): Decls;
  /** The computed custom properties of a surface with `data-theme="<id>"` (or of :root for an id with no block):
   * literals, every `var()` substituted where it was declared. */
  resolved(id: string): Decls;
}

const THEME_SEL = /^\[data-theme='([\w-]+)'\]$/;

function parseDecls(body: string): Decls {
  const out: Decls = {};
  for (const part of body.split(';')) {
    const at = part.indexOf(':');
    if (at < 0) continue;
    const name = part.slice(0, at).trim();
    if (name) out[name] = part.slice(at + 1).replace(/\s+/g, ' ').trim();
  }
  return out;
}

/** Replace each `var(--x)` / `var(--x, fallback)` in `value` by `lookup('--x')` (or the fallback). */
function substitute(value: string, lookup: (name: string) => string | undefined, depth = 0): string {
  if (depth > 20) throw new Error(`var() cycle or depth in: ${value}`);
  let out = '';
  let i = 0;
  while (i < value.length) {
    const at = value.indexOf('var(', i);
    if (at < 0) {
      out += value.slice(i);
      break;
    }
    out += value.slice(i, at);
    // Find the matching close paren.
    let level = 0;
    let end = at + 3;
    for (; end < value.length; end++) {
      if (value[end] === '(') level++;
      else if (value[end] === ')' && --level === 0) break;
    }
    const inner = value.slice(at + 4, end);
    const comma = inner.indexOf(',');
    const name = (comma < 0 ? inner : inner.slice(0, comma)).trim();
    const fallback = comma < 0 ? undefined : inner.slice(comma + 1).trim();
    const got = lookup(name) ?? (fallback === undefined ? undefined : substitute(fallback, lookup, depth + 1));
    if (got === undefined) throw new Error(`var(${name}) is not defined (in: ${value})`);
    out += got;
    i = end + 1;
  }
  return out;
}

/** Resolve a set of declarations against itself, falling back to `inherited` for names it does not declare. */
function computeAt(decls: Decls, inherited: Decls): Decls {
  const out: Decls = { ...inherited };
  const done = new Set<string>();
  const visiting = new Set<string>();
  const resolveName = (name: string): string | undefined => {
    if (!(name in decls)) return inherited[name];
    if (done.has(name)) return out[name];
    if (visiting.has(name)) throw new Error(`var() cycle at ${name}`);
    visiting.add(name);
    out[name] = substitute(decls[name], resolveName);
    visiting.delete(name);
    done.add(name);
    return out[name];
  };
  for (const name of Object.keys(decls)) if (name.startsWith('--')) resolveName(name);
  return out;
}

function customOnly(d: Decls): Decls {
  return Object.fromEntries(Object.entries(d).filter(([k]) => k.startsWith('--')));
}

export function parseThemes(css: string): ThemeSheet {
  // Comments out, and statement at-rules (`@import url(...);`), which would otherwise prefix the next selector.
  // (The URL itself holds semicolons: `wght@400;500`.)
  const text = css
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/@import\s+(?:url\([^)]*\)|'[^']*'|"[^"]*")[^;]*;/g, '');
  const root: Decls = {};
  const groups = new Map<string, Decls>();
  const blocks = new Map<string, Decls>();
  const rules = new Map<string, Decls>();
  // Source-ordered list of (ids it applies to, its declarations); `null` = every themed element.
  const onThemed: { ids: string[] | null; decls: Decls }[] = [];
  const ids = new Set<string>();

  for (const m of text.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selector = m[1].replace(/\s+/g, ' ').trim();
    if (selector.startsWith('@')) continue;
    const decls = parseDecls(m[2]);
    if (selector === ':root') {
      Object.assign(root, decls);
      continue;
    }
    if (selector === '[data-theme]') {
      onThemed.push({ ids: null, decls });
      rules.set(selector, { ...(rules.get(selector) ?? {}), ...decls });
      continue;
    }
    const parts = selector.split(',').map((s) => s.trim());
    const themed = parts.map((p) => THEME_SEL.exec(p)?.[1]);
    if (themed.every((t) => t !== undefined)) {
      const list = themed as string[];
      list.forEach((id) => ids.add(id));
      onThemed.push({ ids: list, decls });
      if (list.length === 1) blocks.set(list[0], { ...(blocks.get(list[0]) ?? {}), ...decls });
      else groups.set(parts.join(', '), { ...(groups.get(parts.join(', ')) ?? {}), ...decls });
      continue;
    }
    rules.set(selector, { ...(rules.get(selector) ?? {}), ...decls });
  }

  const rootComputed = computeAt(customOnly(root), {});

  const declared = (id: string): Decls => {
    const out: Decls = {};
    for (const { ids: list, decls } of onThemed) if (list === null || list.includes(id)) Object.assign(out, decls);
    return out;
  };

  return {
    root,
    groups,
    blocks,
    rules,
    ids: [...ids],
    declared,
    resolved: (id: string) => computeAt(customOnly(declared(id)), rootComputed),
  };
}
