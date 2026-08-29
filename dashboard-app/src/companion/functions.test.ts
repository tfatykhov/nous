import { describe, it, expect } from 'vitest';
import {
  callFunction,
  formatDateCldr,
  formatString,
  isDataBinding,
  isFunctionCall,
  resolveDynamic,
  runChecks,
  toDisplayString,
  type EvalContext,
  type ValidationResult,
} from './functions';
import type { Scope } from './pointer';

// Locale/timezone note: the CLDR month/weekday tokens delegate to
// toLocaleDateString(undefined, …), so these assertions read as en-US. Dates
// are built with the local-time Date constructor (or a Z-LESS ISO string,
// which parses as local) — never a "…Z" literal, which would render a
// different weekday depending on the machine's zone.
const FIXED = new Date(2025, 11, 15, 14, 5, 9); // Mon 15 Dec 2025, 14:05:09 local

function ctxOf(dataModel: Record<string, unknown>, scope: Scope | null = null): EvalContext {
  return { dataModel, scope };
}

describe('type guards', () => {
  it('distinguishes DataBinding from FunctionCall', () => {
    expect(isDataBinding({ path: '/a' })).toBe(true);
    expect(isDataBinding({ call: 'required' })).toBe(false);
    expect(isDataBinding('literal')).toBe(false);
    expect(isFunctionCall({ call: 'required' })).toBe(true);
    expect(isFunctionCall({ path: '/a' })).toBe(false);
  });
});

describe('toDisplayString', () => {
  it('renders null and undefined as the empty string', () => {
    expect(toDisplayString(null)).toBe('');
    expect(toDisplayString(undefined)).toBe('');
  });
  it('passes strings through and stringifies scalars', () => {
    expect(toDisplayString('hi')).toBe('hi');
    expect(toDisplayString(42)).toBe('42');
    expect(toDisplayString(false)).toBe('false');
  });
  it('JSON-encodes objects and arrays', () => {
    expect(toDisplayString({ a: 1 })).toBe('{"a":1}');
    expect(toDisplayString([1, 'x'])).toBe('[1,"x"]');
  });
});

describe('resolveDynamic', () => {
  it('resolves a literal, a binding, and a function call', () => {
    const ctx = ctxOf({ a: { b: 'deep' } });
    expect(resolveDynamic('plain', ctx)).toBe('plain');
    expect(resolveDynamic({ path: '/a/b' }, ctx)).toBe('deep');
    expect(resolveDynamic({ call: 'required', args: { value: 'x' } }, ctx)).toEqual({
      valid: true,
      message: undefined,
    });
  });

  it('resolves a relative binding against the scope', () => {
    const ctx = ctxOf({ rows: [{ n: 'first' }, { n: 'second' }] }, { base: '/rows/1', index: 1 });
    expect(resolveDynamic({ path: 'n' }, ctx)).toBe('second');
  });
});

describe('validators', () => {
  const ctx = ctxOf({});

  it('required rejects null, undefined, empty string and empty array', () => {
    for (const v of [null, undefined, '', []]) {
      expect((callFunction('required', { value: v }, ctx) as ValidationResult).valid).toBe(false);
    }
    for (const v of ['x', 0, false, ['a']]) {
      expect((callFunction('required', { value: v }, ctx) as ValidationResult).valid).toBe(true);
    }
  });

  it('regex applies the pattern and fails closed on an invalid pattern', () => {
    // Fixture 32's zip rule.
    const zip = (v: string) =>
      (callFunction('regex', { value: v, pattern: '^[0-9]{5}$' }, ctx) as ValidationResult).valid;
    expect(zip('94103')).toBe(true);
    expect(zip('9410')).toBe(false);
    expect(zip('941030')).toBe(false);
    expect(
      (callFunction('regex', { value: 'x', pattern: '([' }, ctx) as ValidationResult).valid,
    ).toBe(false);
  });

  it('length honours min and max independently', () => {
    const len = (value: string, args: Record<string, unknown>) =>
      (callFunction('length', { value, ...args }, ctx) as ValidationResult).valid;
    expect(len('abc', { min: 2 })).toBe(true);
    expect(len('a', { min: 2 })).toBe(false);
    expect(len('abcd', { max: 3 })).toBe(false);
    expect(len('abc', { min: 2, max: 3 })).toBe(true);
    expect(len('', {})).toBe(true);
  });

  it('numeric bounds values and rejects non-numbers', () => {
    const num = (value: unknown, args: Record<string, unknown> = {}) =>
      (callFunction('numeric', { value, ...args }, ctx) as ValidationResult).valid;
    expect(num(5, { min: 1, max: 10 })).toBe(true);
    expect(num(0, { min: 1 })).toBe(false);
    expect(num(11, { max: 10 })).toBe(false);
    expect(num('abc')).toBe(false);
  });

  it('email accepts a plain address and rejects malformed ones', () => {
    const email = (v: string) =>
      (callFunction('email', { value: v }, ctx) as ValidationResult).valid;
    expect(email('a@b.co')).toBe(true);
    expect(email('a@b')).toBe(false);
    expect(email('nope')).toBe(false);
    expect(email('')).toBe(false);
  });
});

describe('and / or / not — toBool over mixed boolean and ValidationResult args', () => {
  // Fixture 32's submit-button guard: args.values mixes a raw boolean binding
  // with nested calls returning ValidationResult.
  const model = { formData: { email: '', phone: '5551234567', zip: '94103', agree: true } };
  const ctx = ctxOf(model);

  const submitGuard = {
    call: 'and',
    args: {
      values: [
        { path: '/formData/agree' },
        {
          call: 'or',
          args: {
            values: [
              { call: 'required', args: { value: { path: '/formData/email' } } },
              { call: 'required', args: { value: { path: '/formData/phone' } } },
            ],
          },
        },
        { call: 'required', args: { value: { path: '/formData/zip' } } },
      ],
    },
  };

  it('passes when the boolean is true and one of the required legs holds', () => {
    expect(resolveDynamic(submitGuard, ctx)).toBe(true);
  });

  it('fails when the raw boolean leg is false', () => {
    expect(resolveDynamic(submitGuard, ctxOf({ formData: { ...model.formData, agree: false } }))).toBe(
      false,
    );
  });

  it('fails when both or-legs are empty (ValidationResult unwrapped to false)', () => {
    expect(
      resolveDynamic(submitGuard, ctxOf({ formData: { ...model.formData, phone: '' } })),
    ).toBe(false);
  });

  it('and/or default to the identity element with no values', () => {
    expect(callFunction('and', {}, ctx)).toBe(true);
    expect(callFunction('or', {}, ctx)).toBe(false);
  });

  it('not unwraps a ValidationResult as well as a boolean', () => {
    expect(callFunction('not', { value: true }, ctx)).toBe(false);
    expect(callFunction('not', { value: { valid: false } }, ctx)).toBe(true);
    expect(callFunction('not', { value: { call: 'required', args: { value: '' } } }, ctx)).toBe(true);
  });
});

describe('@index', () => {
  it('returns the scope index plus the offset', () => {
    const ctx = ctxOf({}, { base: '/rows/3', index: 3 });
    expect(callFunction('@index', {}, ctx)).toBe(3);
    expect(callFunction('@index', { offset: 1 }, ctx)).toBe(4);
    expect(callFunction('@index', { offset: -1 }, ctx)).toBe(2);
  });

  it('resolves a bound offset (DynamicNumber)', () => {
    const ctx = ctxOf({ off: 10 }, { base: '/rows/0', index: 0 });
    expect(callFunction('@index', { offset: { path: '/off' } }, ctx)).toBe(10);
  });

  it('throws outside a collection scope', () => {
    expect(() => callFunction('@index', {}, ctxOf({}))).toThrow(/collection scope/);
  });
});

describe('formatString', () => {
  it('passes a template with no interpolation through unchanged', () => {
    expect(formatString('just text', ctxOf({}))).toBe('just text');
  });

  it('interpolates an absolute path', () => {
    expect(formatString('Hi ${/user/name}!', ctxOf({ user: { name: 'Ada' } }))).toBe('Hi Ada!');
  });

  it('interpolates a relative path against the scope', () => {
    const ctx = ctxOf(
      { employees: [{ name: 'Ada' }, { name: 'Grace' }] },
      { base: '/employees/1', index: 1 },
    );
    expect(formatString('Hi ${name}', ctx)).toBe('Hi Grace');
  });

  it('honours the \\${ escape and emits a literal ${', () => {
    expect(formatString('cost: \\${100}', ctxOf({}))).toBe('cost: ${100}');
  });

  it('renders a missing path as the empty string rather than "undefined"', () => {
    expect(formatString('[${/nope}]', ctxOf({}))).toBe('[]');
  });

  it('interpolates @index inside a collection scope', () => {
    const ctx = ctxOf({ rows: [1, 2] }, { base: '/rows/0', index: 0 });
    expect(formatString('#${@index(offset: 1)}', ctx)).toBe('#1');
  });

  // Fixture 32's welcome_text: a nested ${} argument AND a comma inside a
  // quoted literal — the two things a naive split(',') scanner gets wrong.
  it('parses a nested ${} arg and a comma inside a quoted arg (fixture 32)', () => {
    const ctx = ctxOf({ now: '2025-12-15T12:00:00' });
    const template = "Hello! Today is ${formatDate(value: ${/now}, format: 'EEEE, MMMM d')}.";
    expect(formatString(template, ctx)).toBe('Hello! Today is Monday, December 15.');
  });

  it('reaches formatString through a FunctionCall value, as the fixture declares it', () => {
    const ctx = ctxOf({ now: '2025-12-15T12:00:00' });
    const call = {
      call: 'formatString',
      args: { value: "Today is ${formatDate(value: ${/now}, format: 'MMMM d, yyyy')}" },
    };
    expect(resolveDynamic(call, ctx)).toBe('Today is December 15, 2025');
  });

  it('handles several interpolations in one template', () => {
    const ctx = ctxOf({ a: 1, b: 2 });
    expect(formatString('${/a} then ${/b}', ctx)).toBe('1 then 2');
  });
});

describe('formatting helpers', () => {
  const ctx = ctxOf({});
  it('formatNumber honors the catalog decimals + grouping args', () => {
    // Catalog arg names are `decimals` and `grouping` (codex P2 pinned:
    // `fractionDigits` is not a catalog key).
    expect(callFunction('formatNumber', { value: 3.14159, decimals: 2 }, ctx)).toBe('3.14');
    expect(callFunction('formatNumber', { value: 1234.5, decimals: 0, grouping: false }, ctx)).toBe(
      '1235',
    );
    expect(String(callFunction('formatNumber', { value: 1234, decimals: 0 }, ctx))).toContain(',');
  });
  it('formatCurrency renders a currency and falls back on a bad code', () => {
    expect(String(callFunction('formatCurrency', { value: 5, currency: 'USD' }, ctx))).toContain('5');
    expect(callFunction('formatCurrency', { value: 5, currency: 'NOTACODE' }, ctx)).toBe(
      'NOTACODE 5',
    );
  });
  it('pluralize reads the count from `value` and selects via CLDR', () => {
    // The catalog's count arg is `value` (codex P2 pinned: `count` is not a
    // catalog key, so 1 rendered as plural). English CLDR: 1 -> one,
    // everything else -> other.
    const args = { one: 'item', other: 'items' };
    expect(callFunction('pluralize', { value: 1, ...args }, ctx)).toBe('item');
    expect(callFunction('pluralize', { value: 0, ...args }, ctx)).toBe('items');
    expect(callFunction('pluralize', { value: 2, ...args }, ctx)).toBe('items');
  });
  it('formatString resolves a bound template before interpolating', () => {
    const bound = ctxOf({ tmpl: 'Hi ${/name}!', name: 'Ada' });
    expect(callFunction('formatString', { value: { path: '/tmpl' } }, bound)).toBe('Hi Ada!');
  });
  it('an unknown function resolves to undefined instead of throwing', () => {
    expect(callFunction('noSuchFunction', {}, ctx)).toBeUndefined();
  });
});

describe('formatDateCldr', () => {
  it('formats each supported token', () => {
    expect(formatDateCldr(FIXED, 'yyyy')).toBe('2025');
    expect(formatDateCldr(FIXED, 'MM')).toBe('12');
    expect(formatDateCldr(FIXED, 'MMM')).toBe('Dec');
    expect(formatDateCldr(FIXED, 'MMMM')).toBe('December');
    expect(formatDateCldr(FIXED, 'd')).toBe('15');
    expect(formatDateCldr(FIXED, 'dd')).toBe('15');
    expect(formatDateCldr(FIXED, 'EEE')).toBe('Mon');
    expect(formatDateCldr(FIXED, 'EEEE')).toBe('Monday');
    expect(formatDateCldr(FIXED, 'HH')).toBe('14');
    expect(formatDateCldr(FIXED, 'mm')).toBe('05');
    expect(formatDateCldr(FIXED, 'ss')).toBe('09');
  });

  it('composes tokens with literal separators', () => {
    expect(formatDateCldr(FIXED, 'yyyy-MM-dd HH:mm')).toBe('2025-12-15 14:05');
    expect(formatDateCldr(FIXED, 'EEEE, MMMM d')).toBe('Monday, December 15');
  });

  it('accepts an ISO string as well as a Date', () => {
    expect(formatDateCldr('2025-12-15T14:05:09', 'yyyy-MM-dd')).toBe('2025-12-15');
  });

  it('returns the empty string for an unparseable value', () => {
    expect(formatDateCldr('not a date', 'yyyy')).toBe('');
    expect(formatDateCldr(null, 'yyyy')).toBe('');
  });
});

describe('runChecks', () => {
  const ctx = ctxOf({ formData: { email: '', agree: false } });

  it('returns no failures when there are no checks', () => {
    expect(runChecks(undefined, ctx)).toEqual([]);
    expect(runChecks([], ctx)).toEqual([]);
  });

  it('reports the validator’s own message when it supplies one', () => {
    const failures = runChecks(
      [{ condition: { call: 'email', args: { value: { path: '/formData/email' } } }, message: 'Invalid email format' }],
      ctx,
    );
    expect(failures).toHaveLength(1);
    expect(failures[0].message).toBe('Invalid email address.');
  });

  // A boolean-valued condition (fixture 32's `and` guard) carries no message,
  // so the rule's own message is the only thing left to show the user.
  it('falls back to rule.message when the condition is a bare boolean', () => {
    const failures = runChecks(
      [{ condition: { call: 'and', args: { values: [{ path: '/formData/agree' }] } }, message: 'You must agree to terms.' }],
      ctx,
    );
    expect(failures).toEqual([{ valid: false, message: 'You must agree to terms.' }]);
  });

  it('falls back to a generic message when neither side supplies one', () => {
    expect(runChecks([{ condition: false }], ctx)[0].message).toBe('Invalid.');
  });

  it('treats a throwing condition as a failure rather than propagating', () => {
    // @index outside a collection scope throws; the form must still render.
    const failures = runChecks([{ condition: { call: '@index' }, message: 'boom' }], ctx);
    expect(failures).toEqual([{ valid: false, message: 'boom' }]);
  });

  it('keeps only the failing rules, in order', () => {
    const failures = runChecks(
      [
        { condition: true },
        { condition: false, message: 'second' },
        { condition: { valid: false, message: 'third' } },
      ],
      ctx,
    );
    expect(failures.map((f) => f.message)).toEqual(['second', 'third']);
  });
});

describe('formatDateCldr — date-only calendar semantics', () => {
  it('treats a date-only string as a local calendar date, not a UTC instant', () => {
    // new Date('2025-12-15') parses as midnight UTC; users west of UTC would
    // see Dec 14 and the wrong weekday (codex P2). The formatter must build
    // date-only values in LOCAL time.
    expect(formatDateCldr('2025-12-15', 'yyyy-MM-dd')).toBe('2025-12-15');
    expect(formatDateCldr('2025-12-15', 'd')).toBe('15');
  });
});
