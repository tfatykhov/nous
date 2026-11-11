import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { scriptBlocks, markupOf } from './lib/testing/css';
import { sourceFiles } from './lib/testing/sources';
import { fmtDay, fmtDayMonth, fmtUtc } from './lib/harness';

// Finish review fix 4: one timestamp format everywhere, "Nov 10 09:31 UTC" (lib/harness.ts fmtUtc; Work's fmtTime is
// the same). A browser-locale date ("11/10/2026, 10:20:00 AM") was printed on five screens. Numbers keep
// toLocaleString (thousands separators).
//
// How the guard tells a date from a number: toLocaleDateString and toLocaleTimeString only exist on a Date, so both
// are forbidden outright; toLocaleString is forbidden when its receiver is a Date (`new Date(...)`, or a name the file
// assigns from `new Date(`), or when its options name a date or time field (dateStyle, timeStyle, hour, minute, day,
// month, year, weekday). Any other toLocaleString is a number's.

const DATE_FIELD = /\b(?:dateStyle|timeStyle|hour|minute|second|day|month|year|weekday)\s*:/;

/** The bare date formatting calls in a piece of code. */
export function dateLocaleCalls(code: string): string[] {
  const dateNames = [...code.matchAll(/\b(?:const|let|var)\s+(\w+)\s*=\s*new Date\(/g)].map((m) => m[1]);
  const receiver = new RegExp(String.raw`(?:new Date\([^()]*(?:\([^()]*\))?[^()]*\)` + (dateNames.length ? `|\\b(?:${dateNames.join('|')})` : '') + String.raw`)\.toLocaleString\(`);
  return code.split('\n').filter((line) => /\.toLocale(?:Date|Time)String\(/.test(line) || receiver.test(line)
    || (/\.toLocaleString\(/.test(line) && DATE_FIELD.test(line.slice(line.indexOf('.toLocaleString(')))))
    .map((l) => l.trim());
}

const code = (file: string) => {
  const src = readFileSync(join(__dirname, file), 'utf-8');
  return file.endsWith('.svelte') ? [scriptBlocks(src), markupOf(src)].join('\n') : scriptBlocks(`<script>${src}</script>`);
};

describe('one timestamp format', () => {
  it('fmtUtc prints "Nov 10 09:31 UTC"; the day forms print the same month and day', () => {
    expect(fmtUtc('2026-11-10T09:31:45Z')).toBe('Nov 10 09:31 UTC');
    expect(fmtDay('2026-10-20T23:59:00Z')).toBe('Oct 20');
    expect(fmtDayMonth('2026-11-10T09:31:00Z')).toBe('10 November');
    expect([fmtUtc(null), fmtDay('nope'), fmtDayMonth('')]).toEqual(['—', '—', '—']);
  });

  it('the guard catches date formatting and spares a number', () => {
    for (const bad of ['return new Date(iso).toLocaleString();', "d.toLocaleTimeString([], { hour: '2-digit' })",
      "x.toLocaleDateString('en-US', { month: 'short' })", "const d = new Date(s);\nreturn d.toLocaleString();",
      "n.toLocaleString([], { dateStyle: 'short', timeStyle: 'short' })"]) {
      expect(dateLocaleCalls(bad), bad).not.toEqual([]);
    }
    for (const ok of ['total.toLocaleString()', "n.toLocaleString('en-US')", '{agg.attempted.toLocaleString()}',
      "const d = new Date(s);\nreturn count.toLocaleString();"]) {
      expect(dateLocaleCalls(ok), ok).toEqual([]);
    }
  });

  it.each(sourceFiles().filter((f) => f.startsWith('views/') || f.startsWith('lib/')))('%s formats no date with toLocale*', (file) => {
    expect(dateLocaleCalls(code(file))).toEqual([]);
  });
});
