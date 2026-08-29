// F092: markdown-lite parser for the basic catalog's Text component.
//
// Produces a small AST that Text.svelte renders with ordinary Svelte
// elements. There is deliberately no `{@html}` anywhere on this path: Text
// content is agent-authored and reaches us over the wire, so it is never
// trusted as markup.
//
// UPSTREAM CONTRADICTION: the basic catalog describes Text as supporting
// "simple Markdown formatting (i.e. without HTML, images, or links)", but the
// official conformance fixture 35_markdown-text.json renders
// "[Link to Google](https://google.com)". The fixture wins — we parse links,
// and gate them on the same scheme allowlist `openUrl` uses.
//
// Emphasis is `*…*` only, NOT `_…_`: agent text is full of snake_case
// identifiers (com_nous_nonce, surface_id) that `_` emphasis would shred.

import { isSafeUrl } from './functions';

export type InlineNode =
  | { type: 'text'; value: string }
  | { type: 'strong'; children: InlineNode[] }
  | { type: 'em'; children: InlineNode[] }
  | { type: 'code'; value: string }
  | { type: 'link'; href: string; children: InlineNode[] };

export type BlockNode =
  | { type: 'heading'; level: number; children: InlineNode[] }
  | { type: 'paragraph'; children: InlineNode[] }
  | { type: 'list'; ordered: boolean; items: InlineNode[][] }
  | { type: 'codeblock'; value: string; lang?: string };

const UNORDERED = /^\s*[-*+]\s+(.*)$/;
const ORDERED = /^\s*\d+[.)]\s+(.*)$/;
const HEADING = /^(#{1,6})\s+(.*)$/;
const FENCE = /^\s*```(.*)$/;
const LINK = /^\[([^\]]*)\]\(([^)]*)\)/;

export function parseInline(src: string): InlineNode[] {
  const out: InlineNode[] = [];
  let buffer = '';
  const flush = () => {
    if (buffer !== '') {
      out.push({ type: 'text', value: buffer });
      buffer = '';
    }
  };

  let i = 0;
  while (i < src.length) {
    const rest = src.slice(i);

    // Inline code wins over every other marker — nothing nests inside it.
    if (rest[0] === '`') {
      const end = rest.indexOf('`', 1);
      if (end > 0) {
        flush();
        out.push({ type: 'code', value: rest.slice(1, end) });
        i += end + 1;
        continue;
      }
    }

    if (rest[0] === '[') {
      const match = LINK.exec(rest);
      if (match) {
        flush();
        const label = parseInline(match[1]);
        const href = match[2].trim();
        // Scheme gate at PARSE time: a javascript:/data: href never becomes a
        // link node at all, so no renderer can accidentally honour it.
        if (isSafeUrl(href)) out.push({ type: 'link', href, children: label });
        else out.push(...label);
        i += match[0].length;
        continue;
      }
    }

    // `***both***` is handled as its own opener. MIXED nesting of the two
    // emphasis widths (`**bold *and italic***`) is NOT supported: resolving it
    // needs CommonMark's delimiter-run stack, which is far more machinery than
    // agent-authored status text warrants. Such input degrades to literal
    // asterisks rather than being dropped.
    if (rest.startsWith('***')) {
      const end = rest.indexOf('***', 3);
      if (end > 3) {
        flush();
        out.push({
          type: 'strong',
          children: [{ type: 'em', children: parseInline(rest.slice(3, end)) }],
        });
        i += end + 3;
        continue;
      }
    }

    if (rest.startsWith('**')) {
      const end = rest.indexOf('**', 2);
      if (end > 2) {
        flush();
        out.push({ type: 'strong', children: parseInline(rest.slice(2, end)) });
        i += end + 2;
        continue;
      }
    }

    if (rest[0] === '*') {
      const end = rest.indexOf('*', 1);
      if (end > 1) {
        flush();
        out.push({ type: 'em', children: parseInline(rest.slice(1, end)) });
        i += end + 1;
        continue;
      }
    }

    // Unmatched marker: fall through and keep it as literal text.
    buffer += src[i];
    i += 1;
  }
  flush();
  return out;
}

export function parseMarkdown(src: string): BlockNode[] {
  const lines = src.split(/\r?\n/);
  const blocks: BlockNode[] = [];
  let paragraph: string[] = [];

  const flushParagraph = () => {
    if (paragraph.length === 0) return;
    // Soft line breaks are preserved (joined with \n, rendered pre-wrap)
    // rather than collapsed to spaces: agents lay text out deliberately.
    blocks.push({ type: 'paragraph', children: parseInline(paragraph.join('\n')) });
    paragraph = [];
  };

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    const fence = FENCE.exec(line);
    if (fence) {
      flushParagraph();
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !FENCE.test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1; // consume the closing fence (or run off the end unterminated)
      const lang = fence[1].trim();
      blocks.push({ type: 'codeblock', value: body.join('\n'), ...(lang ? { lang } : {}) });
      continue;
    }

    const heading = HEADING.exec(line);
    if (heading) {
      flushParagraph();
      blocks.push({
        type: 'heading',
        level: heading[1].length,
        children: parseInline(heading[2].trim()),
      });
      i += 1;
      continue;
    }

    if (UNORDERED.test(line) || ORDERED.test(line)) {
      flushParagraph();
      const ordered = !UNORDERED.test(line);
      const items: InlineNode[][] = [];
      while (i < lines.length) {
        const match = (ordered ? ORDERED : UNORDERED).exec(lines[i]);
        if (!match) break;
        items.push(parseInline(match[1]));
        i += 1;
      }
      blocks.push({ type: 'list', ordered, items });
      continue;
    }

    if (line.trim() === '') {
      flushParagraph();
      i += 1;
      continue;
    }

    paragraph.push(line);
    i += 1;
  }
  flushParagraph();
  return blocks;
}
