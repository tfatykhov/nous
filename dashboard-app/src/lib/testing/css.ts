// Test helpers that read CSS as text (the token and hex guards). Not imported by the app.

/** Every innermost `selector { declarations }` block of a stylesheet, keyed by its whitespace-collapsed selector.
 * Blocks with one selector are merged, later declarations winning. Comments are removed first. */
export function parseBlocks(css: string): Map<string, Record<string, string>> {
  const blocks = new Map<string, Record<string, string>>();
  const text = css.replace(/\/\*[\s\S]*?\*\//g, '');
  for (const m of text.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selector = m[1].replace(/\s+/g, ' ').trim();
    const decls: Record<string, string> = { ...(blocks.get(selector) ?? {}) };
    for (const part of m[2].split(';')) {
      const at = part.indexOf(':');
      if (at < 0) continue;
      const name = part.slice(0, at).trim();
      if (name) decls[name] = part.slice(at + 1).replace(/\s+/g, ' ').trim();
    }
    blocks.set(selector, decls);
  }
  return blocks;
}

/** The text of every <style> block of a Svelte component, CSS comments removed. */
export function styleBlocks(source: string): string {
  return [...source.matchAll(/<style\b[^>]*>([\s\S]*?)<\/style>/g)]
    .map((m) => m[1].replace(/\/\*[\s\S]*?\*\//g, ''))
    .join('\n');
}

/** The text of every <script> block of a Svelte component, comments removed (block comments, and line comments
 * that start a line or follow whitespace, so a URL's '//' stays). */
export function scriptBlocks(source: string): string {
  return [...source.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)]
    .map((m) => m[1].replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|\s)\/\/.*$/gm, '$1'))
    .join('\n');
}

/** A Svelte component's markup: the file without its <script> and <style> blocks, HTML comments removed. */
export function markupOf(source: string): string {
  return source
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '')
    .replace(/<style\b[^>]*>[\s\S]*?<\/style>/g, '')
    .replace(/<!--[\s\S]*?-->/g, '');
}

/** The token blocks of app.css: the one look's at `:root`, the Atlantic-blue rail's at `.rail`. */
export const ROOT_SEL = ':root';
export const RAIL_SEL = '.rail';

/** A block of parseBlocks' map that must exist and hold at least `minDecls` declarations. A missing or emptied selector
 * throws, where `blocks.get(sel) ?? {}` would hand every comparison an empty object (passing vacuously or failing
 * confusingly). */
export function block(blocks: Map<string, Record<string, string>>, selector: string, minDecls: number): Record<string, string> {
  const found = blocks.get(selector);
  if (!found) throw new Error(`app.css has no block for ${selector}`);
  const n = Object.keys(found).length;
  if (n < minDecls) throw new Error(`app.css's ${selector} has ${n} declarations, fewer than ${minDecls}`);
  return found;
}
