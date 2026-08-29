// F092: RFC 6901 JSON Pointer resolution with A2UI collection scopes.
//
// Absolute paths start with '/'; inside a list template, bare paths are
// relative to the current item. Unescape order is ~1 -> '/' THEN ~0 -> '~'
// (the classic place this breaks — RFC 6901 §4).

export interface Scope {
  /** Absolute pointer of the current collection item, e.g. "/employees/0". */
  base: string;
  /** 0-based index of the current item in its collection. */
  index: number;
}

export function unescapeToken(token: string): string {
  return token.replace(/~1/g, '/').replace(/~0/g, '~');
}

export function escapeToken(token: string): string {
  return token.replace(/~/g, '~0').replace(/\//g, '~1');
}

/** Resolve a possibly-relative pointer against the scope chain. */
export function absolute(path: string, scope: Scope | null): string {
  if (path.startsWith('/')) return path;
  if (scope === null) return '/' + path;
  return scope.base + '/' + path;
}

function tokens(pointer: string): string[] {
  if (pointer === '' || pointer === '/') return [];
  return pointer.replace(/^\//, '').split('/').map(unescapeToken);
}

export function getPointer(model: unknown, pointer: string): unknown {
  let node: unknown = model;
  for (const token of tokens(pointer)) {
    if (node === null || node === undefined) return undefined;
    if (Array.isArray(node)) {
      const idx = Number(token);
      if (!Number.isInteger(idx)) return undefined;
      node = node[idx];
    } else if (typeof node === 'object') {
      node = (node as Record<string, unknown>)[token];
    } else {
      return undefined;
    }
  }
  return node;
}

/**
 * Upsert into the model. `value === null` deletes the key (A2UI semantics).
 * Missing intermediates are created: numeric next-token -> array, else
 * object — mirrored by the Python side (nous/a2ui/service.py::_pointer_set).
 */
export function setPointer(model: Record<string, unknown>, pointer: string, value: unknown): void {
  const toks = tokens(pointer);
  if (toks.length === 0) return; // whole-model replace is the store's job
  let node: unknown = model;
  for (let i = 0; i < toks.length - 1; i++) {
    const token = toks[i];
    const nextIsIndex = /^\d+$/.test(toks[i + 1]);
    if (Array.isArray(node)) {
      const idx = Number(token);
      while (node.length <= idx) node.push(null);
      if (node[idx] === null || typeof node[idx] !== 'object') {
        node[idx] = nextIsIndex ? [] : {};
      }
      node = node[idx];
    } else {
      const obj = node as Record<string, unknown>;
      if (obj[token] === null || obj[token] === undefined || typeof obj[token] !== 'object') {
        obj[token] = nextIsIndex ? [] : {};
      }
      node = obj[token];
    }
  }
  const last = toks[toks.length - 1];
  if (Array.isArray(node)) {
    const idx = Number(last);
    if (value === null) {
      if (idx >= 0 && idx < node.length) node.splice(idx, 1);
    } else {
      while (node.length <= idx) node.push(null);
      node[idx] = value;
    }
  } else {
    const obj = node as Record<string, unknown>;
    if (value === null) {
      delete obj[last];
    } else {
      obj[last] = value;
    }
  }
}
