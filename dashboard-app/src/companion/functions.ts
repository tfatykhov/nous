// F092: renderer-side function engine + dynamic value resolution.
//
// Contracts locked by the plan review against the vendored basic catalog:
// - checks conditions may evaluate to a boolean OR a ValidationResult;
//   and/or/not apply toBool() to each of args.values.
// - ValidationResult has NO upstream schema; local shape is
//   {valid: boolean, message?: string} — a deliberate local choice.
// - formatString needs a real scanner: nested ${}, named args with spaces,
//   commas inside quoted literals (conformance fixture 32), and the \${
//   escape. formatDate takes CLDR/ICU patterns, not strftime.
// - @index errors outside collection scope; its offset can be a binding.

import { absolute, getPointer, type Scope } from './pointer';

export interface ValidationResult {
  valid: boolean;
  message?: string;
  severity?: 'error' | 'warning' | 'info';
}

export interface EvalContext {
  dataModel: Record<string, unknown>;
  scope: Scope | null;
}

type FunctionCall = { call: string; args?: Record<string, unknown>; catalogId?: string };

export function isFunctionCall(v: unknown): v is FunctionCall {
  return typeof v === 'object' && v !== null && typeof (v as FunctionCall).call === 'string';
}

export function isDataBinding(v: unknown): v is { path: string } {
  return (
    typeof v === 'object' &&
    v !== null &&
    typeof (v as { path?: unknown }).path === 'string' &&
    !isFunctionCall(v)
  );
}

/** Resolve any Dynamic* value: literal | {path} | FunctionCall. */
export function resolveDynamic(value: unknown, ctx: EvalContext): unknown {
  if (isDataBinding(value)) {
    return getPointer(ctx.dataModel, absolute(value.path, ctx.scope));
  }
  if (isFunctionCall(value)) {
    return callFunction(value.call, value.args ?? {}, ctx);
  }
  return value;
}

/** A2UI type conversion for display: null/undefined -> "", objects -> JSON. */
export function toDisplayString(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return JSON.stringify(value);
}

function toBool(value: unknown): boolean {
  if (typeof value === 'object' && value !== null && 'valid' in value) {
    return Boolean((value as ValidationResult).valid);
  }
  return Boolean(value);
}

function resolveArg(args: Record<string, unknown>, key: string, ctx: EvalContext): unknown {
  return resolveDynamic(args[key], ctx);
}

export function callFunction(
  name: string,
  args: Record<string, unknown>,
  ctx: EvalContext,
): unknown {
  switch (name) {
    case '@index': {
      if (ctx.scope === null) throw new Error('@index used outside a collection scope');
      const offset = Number(resolveArg(args, 'offset', ctx) ?? 0);
      return ctx.scope.index + offset;
    }
    case 'required': {
      const v = resolveArg(args, 'value', ctx);
      const ok = !(v === null || v === undefined || v === '' || (Array.isArray(v) && v.length === 0));
      return { valid: ok, message: ok ? undefined : 'Required.' };
    }
    case 'regex': {
      const v = toDisplayString(resolveArg(args, 'value', ctx));
      const pattern = String(resolveArg(args, 'pattern', ctx) ?? '');
      let ok = false;
      try {
        ok = new RegExp(pattern).test(v);
      } catch {
        ok = false;
      }
      return { valid: ok, message: ok ? undefined : 'Invalid format.' };
    }
    case 'length': {
      const v = toDisplayString(resolveArg(args, 'value', ctx));
      const min = args.min !== undefined ? Number(resolveArg(args, 'min', ctx)) : -Infinity;
      const max = args.max !== undefined ? Number(resolveArg(args, 'max', ctx)) : Infinity;
      const ok = v.length >= min && v.length <= max;
      return { valid: ok, message: ok ? undefined : 'Length out of range.' };
    }
    case 'numeric': {
      const v = Number(resolveArg(args, 'value', ctx));
      const min = args.min !== undefined ? Number(resolveArg(args, 'min', ctx)) : -Infinity;
      const max = args.max !== undefined ? Number(resolveArg(args, 'max', ctx)) : Infinity;
      const ok = Number.isFinite(v) && v >= min && v <= max;
      return { valid: ok, message: ok ? undefined : 'Out of range.' };
    }
    case 'email': {
      const v = toDisplayString(resolveArg(args, 'value', ctx));
      const ok = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v);
      return { valid: ok, message: ok ? undefined : 'Invalid email address.' };
    }
    case 'and': {
      const values = (args.values as unknown[]) ?? [];
      return values.every((v) => toBool(resolveDynamic(v, ctx)));
    }
    case 'or': {
      const values = (args.values as unknown[]) ?? [];
      return values.some((v) => toBool(resolveDynamic(v, ctx)));
    }
    case 'not': {
      return !toBool(resolveArg(args, 'value', ctx));
    }
    case 'formatString': {
      // value is DynamicString — a binding or nested call must resolve to
      // the template BEFORE interpolation (codex P2: it stringified the
      // descriptor object to "[object Object]").
      return formatString(toDisplayString(resolveArg(args, 'value', ctx)), ctx);
    }
    case 'formatNumber': {
      // Catalog args are `decimals` + `grouping` (codex P2: the previous
      // `fractionDigits` key does not exist in the catalog).
      const v = Number(resolveArg(args, 'value', ctx));
      const digits = args.decimals !== undefined ? Number(resolveArg(args, 'decimals', ctx)) : undefined;
      const grouping = args.grouping !== undefined ? Boolean(resolveArg(args, 'grouping', ctx)) : true;
      return new Intl.NumberFormat(undefined, {
        useGrouping: grouping,
        maximumFractionDigits: digits,
        minimumFractionDigits: digits,
      }).format(v);
    }
    case 'formatCurrency': {
      const v = Number(resolveArg(args, 'value', ctx));
      const currency = String(resolveArg(args, 'currency', ctx) ?? 'USD');
      // decimals + grouping are catalog args here too (codex P2).
      const digits = args.decimals !== undefined ? Number(resolveArg(args, 'decimals', ctx)) : undefined;
      const grouping = args.grouping !== undefined ? Boolean(resolveArg(args, 'grouping', ctx)) : true;
      try {
        return new Intl.NumberFormat(undefined, {
          style: 'currency',
          currency,
          useGrouping: grouping,
          maximumFractionDigits: digits,
          minimumFractionDigits: digits,
        }).format(v);
      } catch {
        return `${currency} ${v}`;
      }
    }
    case 'formatDate': {
      const v = resolveArg(args, 'value', ctx);
      const format = String(resolveArg(args, 'format', ctx) ?? 'yyyy-MM-dd');
      return formatDateCldr(v, format);
    }
    case 'pluralize': {
      // The count arrives as `value` (codex P2: `count` is not a catalog
      // key, so every count was NaN and singular rendered as plural).
      // Category selection is CLDR via Intl.PluralRules, so the catalog's
      // zero/two/few/many forms work in locales that have them.
      const count = Number(resolveArg(args, 'value', ctx));
      const category = Number.isFinite(count)
        ? new Intl.PluralRules().select(count)
        : 'other';
      const chosen = args[category] !== undefined ? args[category] : args.other;
      return toDisplayString(resolveDynamic(chosen, ctx));
    }
    case 'openUrl': {
      const url = toDisplayString(resolveArg(args, 'url', ctx));
      openUrlSafe(url);
      return undefined;
    }
    default:
      // Unknown function: agent-RPC fallback is a later phase; resolve to
      // undefined so bindings render as empty rather than crashing the tree.
      return undefined;
  }
}

/**
 * `weight` -> flex-grow, narrowed to what a style directive accepts.
 *
 * Every property on A2uiComponent is `unknown` (index signature), so the
 * obvious `comp.weight ?? null` types as `{} | null` and fails svelte-check
 * at every call site. Narrowing here also drops non-numeric weights, which
 * the catalog forbids and which would otherwise reach CSS verbatim. A weight
 * of 0 is preserved — it is a meaningful flex-grow.
 */
export function flexGrow(weight: unknown): number | null {
  return typeof weight === 'number' && Number.isFinite(weight) ? weight : null;
}

/** Scheme allowlist shared with the markdown renderer (XSS control). */
export function isSafeUrl(url: string): boolean {
  return /^(https?:|mailto:)/i.test(url.trim());
}

export function openUrlSafe(url: string): void {
  if (!isSafeUrl(url)) return;
  window.open(url, '_blank', 'noopener,noreferrer');
}

// ------------------------------------------------------------ formatString

/**
 * Recursive-descent scanner for `${...}` interpolation. Handles nesting,
 * quoted literals containing commas/colons, function calls with named args,
 * and the \${ escape.
 */
export function formatString(template: string, ctx: EvalContext): string {
  let out = '';
  let i = 0;
  while (i < template.length) {
    if (template[i] === '\\' && template.slice(i + 1, i + 3) === '${') {
      out += '${';
      i += 3;
    } else if (template[i] === '$' && template[i + 1] === '{') {
      const [expr, next] = scanBraced(template, i + 2);
      out += toDisplayString(evalExpression(expr.trim(), ctx));
      i = next;
    } else {
      out += template[i];
      i += 1;
    }
  }
  return out;
}

/** Scan from after '${' to its matching '}', respecting nesting + quotes. */
function scanBraced(s: string, start: number): [string, number] {
  let depth = 1;
  let i = start;
  let quote: string | null = null;
  while (i < s.length) {
    const c = s[i];
    if (quote !== null) {
      if (c === quote) quote = null;
    } else if (c === "'" || c === '"') {
      quote = c;
    } else if (c === '{') {
      depth += 1;
    } else if (c === '}') {
      depth -= 1;
      if (depth === 0) return [s.slice(start, i), i + 1];
    }
    i += 1;
  }
  return [s.slice(start), i];
}

/** Evaluate one interpolated expression: path, literal, or function call. */
function evalExpression(expr: string, ctx: EvalContext): unknown {
  if (expr === '') return '';
  // Nested wrapper: ${/path} inside an argument arrives with ${ } intact.
  if (expr.startsWith('${') && expr.endsWith('}')) {
    return evalExpression(expr.slice(2, -1).trim(), ctx);
  }
  // Quoted literal
  if ((expr.startsWith("'") && expr.endsWith("'")) || (expr.startsWith('"') && expr.endsWith('"'))) {
    return expr.slice(1, -1);
  }
  // Function call: name(arg: value, ...)
  const fnMatch = /^(@?[A-Za-z_][\w]*)\((.*)\)$/s.exec(expr);
  if (fnMatch) {
    const [, name, argsSrc] = fnMatch;
    const args: Record<string, unknown> = {};
    for (const [key, valueSrc] of splitNamedArgs(argsSrc)) {
      args[key] = evalExpression(valueSrc.trim(), ctx);
    }
    return callFunction(name, args, ctx);
  }
  // Number literal
  if (/^-?\d+(\.\d+)?$/.test(expr)) return Number(expr);
  if (expr === 'true') return true;
  if (expr === 'false') return false;
  // Path (absolute or relative)
  return getPointer(ctx.dataModel, absolute(expr, ctx.scope));
}

/** Split "a: x, b: 'y,z'" into [key, rawValue] pairs, quote/nest-aware. */
function splitNamedArgs(src: string): [string, string][] {
  const pairs: [string, string][] = [];
  let depth = 0;
  let quote: string | null = null;
  let current = '';
  const parts: string[] = [];
  for (const c of src) {
    if (quote !== null) {
      if (c === quote) quote = null;
      current += c;
    } else if (c === "'" || c === '"') {
      quote = c;
      current += c;
    } else if (c === '(' || c === '{') {
      depth += 1;
      current += c;
    } else if (c === ')' || c === '}') {
      depth -= 1;
      current += c;
    } else if (c === ',' && depth === 0) {
      parts.push(current);
      current = '';
    } else {
      current += c;
    }
  }
  if (current.trim() !== '') parts.push(current);
  for (const part of parts) {
    const idx = indexOfTopLevelColon(part);
    if (idx === -1) continue;
    pairs.push([part.slice(0, idx).trim(), part.slice(idx + 1)]);
  }
  return pairs;
}

function indexOfTopLevelColon(s: string): number {
  let depth = 0;
  let quote: string | null = null;
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (quote !== null) {
      if (c === quote) quote = null;
    } else if (c === "'" || c === '"') {
      quote = c;
    } else if (c === '(' || c === '{') {
      depth += 1;
    } else if (c === ')' || c === '}') {
      depth -= 1;
    } else if (c === ':' && depth === 0) {
      return i;
    }
  }
  return -1;
}

// -------------------------------------------------------------- formatDate

// Longest variants MUST precede their prefixes (yyyy before yy, HH before H,
// hh before h) — the scanner takes the first match (codex P2: the catalog
// permits yy, hh and H, which previously fell through or mis-rendered).
const CLDR_TOKENS: [RegExp, (d: Date) => string][] = [
  [/^yyyy/, (d) => String(d.getFullYear()).padStart(4, '0')],
  [/^yy/, (d) => String(d.getFullYear() % 100).padStart(2, '0')],
  [/^MMMM/, (d) => d.toLocaleDateString(undefined, { month: 'long' })],
  [/^MMM/, (d) => d.toLocaleDateString(undefined, { month: 'short' })],
  [/^MM/, (d) => String(d.getMonth() + 1).padStart(2, '0')],
  [/^M/, (d) => String(d.getMonth() + 1)],
  [/^dd/, (d) => String(d.getDate()).padStart(2, '0')],
  [/^d/, (d) => String(d.getDate())],
  [/^EEEE/, (d) => d.toLocaleDateString(undefined, { weekday: 'long' })],
  [/^EEE|^E/, (d) => d.toLocaleDateString(undefined, { weekday: 'short' })],
  [/^HH/, (d) => String(d.getHours()).padStart(2, '0')],
  [/^H/, (d) => String(d.getHours())],
  [/^hh/, (d) => String(((d.getHours() + 11) % 12) + 1).padStart(2, '0')],
  [/^h/, (d) => String(((d.getHours() + 11) % 12) + 1)],
  [/^mm/, (d) => String(d.getMinutes()).padStart(2, '0')],
  [/^ss/, (d) => String(d.getSeconds()).padStart(2, '0')],
  [/^a/, (d) => (d.getHours() < 12 ? 'AM' : 'PM')],
];

/** Minimal CLDR/ICU date pattern formatter (the tokens fixtures use). */
export function formatDateCldr(value: unknown, pattern: string): string {
  let date: Date;
  if (value instanceof Date) {
    date = value;
  } else {
    const raw = String(value);
    // A date-only string is a CALENDAR date: new Date('2025-12-15') parses
    // as midnight UTC, so local getters shift users west of UTC to Dec 14
    // and the wrong weekday (codex P2). Construct it in local time.
    const dateOnly = /^(\d{4})-(\d{2})-(\d{2})$/.exec(raw);
    date = dateOnly
      ? new Date(Number(dateOnly[1]), Number(dateOnly[2]) - 1, Number(dateOnly[3]))
      : new Date(raw);
  }
  if (Number.isNaN(date.getTime())) return '';
  let out = '';
  let i = 0;
  while (i < pattern.length) {
    if (pattern[i] === "'") {
      const end = pattern.indexOf("'", i + 1);
      out += pattern.slice(i + 1, end === -1 ? undefined : end);
      i = end === -1 ? pattern.length : end + 1;
      continue;
    }
    const rest = pattern.slice(i);
    const hit = CLDR_TOKENS.find(([re]) => re.test(rest));
    if (hit) {
      const match = hit[0].exec(rest)!;
      out += hit[1](date);
      i += match[0].length;
    } else {
      out += pattern[i];
      i += 1;
    }
  }
  return out;
}

// ------------------------------------------------------------------ checks

export interface CheckRule {
  condition: unknown;
  message?: string;
}

/** Evaluate a component's checks; returns failures (empty = all pass). */
export function runChecks(checks: CheckRule[] | undefined, ctx: EvalContext): ValidationResult[] {
  if (!checks?.length) return [];
  const failures: ValidationResult[] = [];
  for (const rule of checks) {
    let result: unknown;
    try {
      result = resolveDynamic(rule.condition, ctx);
    } catch {
      result = { valid: false };
    }
    const normalized: ValidationResult =
      typeof result === 'object' && result !== null && 'valid' in result
        ? (result as ValidationResult)
        : { valid: Boolean(result) };
    if (!normalized.valid) {
      failures.push({ ...normalized, message: normalized.message ?? rule.message ?? 'Invalid.' });
    }
  }
  return failures;
}
