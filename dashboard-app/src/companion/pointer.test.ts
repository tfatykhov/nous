import { describe, it, expect } from 'vitest';
import {
  absolute,
  escapeToken,
  getPointer,
  setPointer,
  unescapeToken,
  type Scope,
} from './pointer';

describe('pointer — escaping', () => {
  it('unescapes ~1 to / and ~0 to ~', () => {
    expect(unescapeToken('a~1b~0c')).toBe('a/b~c');
  });

  // ORDER GUARD (RFC 6901 §4). The token above passes under BOTH orders, so it
  // cannot detect a regression on its own: ~0-first turns "a~1b~0c" into
  // "a~1b~c" and then into "a/b~c" — the same answer. "~01" is the input that
  // discriminates: correct order yields "~1", reversed yields "/".
  it('applies ~1 BEFORE ~0 (the only order-sensitive input)', () => {
    expect(unescapeToken('x~01y')).toBe('x~1y');
    expect(unescapeToken('x~01y')).not.toBe('x/y');
  });

  it('escapeToken round-trips through unescapeToken', () => {
    for (const raw of ['plain', 'a/b', 'a~b', 'a~1b', '~0', 'm/n~o/p', '~']) {
      expect(unescapeToken(escapeToken(raw))).toBe(raw);
    }
  });

  it('escapeToken escapes ~ before / so the pair survives', () => {
    expect(escapeToken('a~1b')).toBe('a~01b');
  });
});

describe('pointer — absolute() with scope', () => {
  const scope: Scope = { base: '/employees/2', index: 2 };

  it('leaves absolute paths untouched, with or without scope', () => {
    expect(absolute('/a/b', null)).toBe('/a/b');
    expect(absolute('/a/b', scope)).toBe('/a/b');
  });

  it('roots a bare path at / when there is no scope', () => {
    expect(absolute('name', null)).toBe('/name');
  });

  it('resolves a bare path against the scope base', () => {
    expect(absolute('name', scope)).toBe('/employees/2/name');
  });
});

describe('pointer — getPointer', () => {
  const model = {
    now: '2025-12-15',
    formData: { email: 'a@b.co', tags: ['x', 'y'] },
    'weird/key': 1,
    'tilde~key': 2,
  };

  it('reads nested object and array members', () => {
    expect(getPointer(model, '/formData/email')).toBe('a@b.co');
    expect(getPointer(model, '/formData/tags/1')).toBe('y');
  });

  it('returns the whole model for "" and "/"', () => {
    expect(getPointer(model, '')).toBe(model);
    expect(getPointer(model, '/')).toBe(model);
  });

  it('unescapes tokens when reading', () => {
    expect(getPointer(model, '/weird~1key')).toBe(1);
    expect(getPointer(model, '/tilde~0key')).toBe(2);
  });

  it('returns undefined for missing keys and non-integer array indices', () => {
    expect(getPointer(model, '/nope/deeper')).toBeUndefined();
    expect(getPointer(model, '/formData/tags/notanindex')).toBeUndefined();
    expect(getPointer(model, '/now/tooDeep')).toBeUndefined();
  });
});

describe('pointer — setPointer', () => {
  it('writes a nested value', () => {
    const model: Record<string, unknown> = { formData: { email: '' } };
    setPointer(model, '/formData/email', 'x@y.z');
    expect(model).toEqual({ formData: { email: 'x@y.z' } });
  });

  it('deletes an object key when the value is null (A2UI semantics)', () => {
    const model: Record<string, unknown> = { a: 1, b: 2 };
    setPointer(model, '/b', null);
    expect(model).toEqual({ a: 1 });
    expect('b' in model).toBe(false);
  });

  it('splices an array member out when the value is null', () => {
    const model: Record<string, unknown> = { list: ['a', 'b', 'c'] };
    setPointer(model, '/list/1', null);
    expect(model.list).toEqual(['a', 'c']);
  });

  it('creates an OBJECT intermediate when the next token is not numeric', () => {
    const model: Record<string, unknown> = {};
    setPointer(model, '/a/b/c', 7);
    expect(model).toEqual({ a: { b: { c: 7 } } });
    expect(Array.isArray((model.a as Record<string, unknown>).b)).toBe(false);
  });

  it('creates an ARRAY intermediate when the next token is numeric', () => {
    const model: Record<string, unknown> = {};
    setPointer(model, '/rows/0/name', 'first');
    expect(Array.isArray(model.rows)).toBe(true);
    expect(model).toEqual({ rows: [{ name: 'first' }] });
  });

  it('pads an array with nulls when writing past its end', () => {
    const model: Record<string, unknown> = { list: ['a'] };
    setPointer(model, '/list/2', 'c');
    expect(model.list).toEqual(['a', null, 'c']);
  });

  it('unescapes tokens when writing', () => {
    const model: Record<string, unknown> = {};
    setPointer(model, '/weird~1key', 9);
    expect(model['weird/key']).toBe(9);
  });

  it('ignores whole-model pointers — replacement is the store’s job', () => {
    const model: Record<string, unknown> = { a: 1 };
    setPointer(model, '', { b: 2 });
    setPointer(model, '/', { b: 2 });
    expect(model).toEqual({ a: 1 });
  });
});
