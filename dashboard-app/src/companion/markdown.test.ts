import { describe, it, expect } from 'vitest';
import { parseInline, parseMarkdown, type BlockNode, type InlineNode } from './markdown';

// Inlined rather than imported across the repo root: this is the exact `text`
// value of the `markdown_content` component in
// tests/fixtures/a2ui/examples/35_markdown-text.json.
const FIXTURE_35 =
  '# Heading 1\n\nThis is **bold** text and *italic* text.\n\n- List item 1\n- List item 2\n\n[Link to Google](https://google.com)';

// Annotated so the literal narrows to the 'text' member of the union rather
// than widening `type` to string.
const text = (value: string): InlineNode => ({ type: 'text', value });

describe('parseMarkdown — conformance fixture 35', () => {
  const blocks = parseMarkdown(FIXTURE_35);

  it('produces the expected four blocks', () => {
    expect(blocks.map((b) => b.type)).toEqual(['heading', 'paragraph', 'list', 'paragraph']);
  });

  it('parses the whole fixture to the expected AST', () => {
    expect(blocks).toEqual([
      { type: 'heading', level: 1, children: [text('Heading 1')] },
      {
        type: 'paragraph',
        children: [
          text('This is '),
          { type: 'strong', children: [text('bold')] },
          text(' text and '),
          { type: 'em', children: [text('italic')] },
          text(' text.'),
        ],
      },
      {
        type: 'list',
        ordered: false,
        items: [[text('List item 1')], [text('List item 2')]],
      },
      {
        type: 'paragraph',
        children: [
          { type: 'link', href: 'https://google.com', children: [text('Link to Google')] },
        ],
      },
    ] satisfies BlockNode[]);
  });

  it('keeps the sibling title_text "### Markdown Rendering" as a level-3 heading', () => {
    expect(parseMarkdown('### Markdown Rendering')).toEqual([
      { type: 'heading', level: 3, children: [text('Markdown Rendering')] },
    ]);
  });
});

describe('parseMarkdown — blocks', () => {
  it('reads heading levels 1 through 6', () => {
    for (let level = 1; level <= 6; level++) {
      expect(parseMarkdown('#'.repeat(level) + ' T')).toEqual([
        { type: 'heading', level, children: [text('T')] },
      ]);
    }
  });

  it('requires a space after the hashes', () => {
    expect(parseMarkdown('#NotAHeading')[0].type).toBe('paragraph');
  });

  it('keeps soft line breaks inside one paragraph', () => {
    expect(parseMarkdown('one\ntwo')).toEqual([{ type: 'paragraph', children: [text('one\ntwo')] }]);
  });

  it('splits paragraphs on a blank line', () => {
    expect(parseMarkdown('one\n\ntwo').map((b) => b.type)).toEqual(['paragraph', 'paragraph']);
  });

  it('parses unordered lists with any bullet marker', () => {
    for (const marker of ['-', '*', '+']) {
      expect(parseMarkdown(`${marker} a\n${marker} b`)).toEqual([
        { type: 'list', ordered: false, items: [[text('a')], [text('b')]] },
      ]);
    }
  });

  it('parses ordered lists and marks them ordered', () => {
    expect(parseMarkdown('1. first\n2) second')).toEqual([
      { type: 'list', ordered: true, items: [[text('first')], [text('second')]] },
    ]);
  });

  it('parses inline markup inside list items', () => {
    expect(parseMarkdown('- has **bold**')).toEqual([
      {
        type: 'list',
        ordered: false,
        items: [[text('has '), { type: 'strong', children: [text('bold')] }]],
      },
    ]);
  });

  it('ends a list at the first non-item line', () => {
    expect(parseMarkdown('- a\nplain').map((b) => b.type)).toEqual(['list', 'paragraph']);
  });

  it('parses a fenced code block and keeps its body verbatim', () => {
    expect(parseMarkdown('```\nnot **bold** here\n```')).toEqual([
      { type: 'codeblock', value: 'not **bold** here' },
    ]);
  });

  it('records the fence language when present', () => {
    expect(parseMarkdown('```python\nx = 1\n```')).toEqual([
      { type: 'codeblock', value: 'x = 1', lang: 'python' },
    ]);
  });

  it('closes an unterminated fence at end of input', () => {
    expect(parseMarkdown('```\ndangling')).toEqual([{ type: 'codeblock', value: 'dangling' }]);
  });

  it('returns no blocks for empty or whitespace-only input', () => {
    expect(parseMarkdown('')).toEqual([]);
    expect(parseMarkdown('\n\n')).toEqual([]);
  });
});

describe('parseInline', () => {
  it('parses inline code and does not look for markup inside it', () => {
    expect(parseInline('use `a **b** c` now')).toEqual([
      text('use '),
      { type: 'code', value: 'a **b** c' },
      text(' now'),
    ]);
  });

  it('nests inline code inside strong', () => {
    expect(parseInline('**bold and `code`**')).toEqual([
      {
        type: 'strong',
        children: [text('bold and '), { type: 'code', value: 'code' }],
      },
    ]);
  });

  it('parses *** as strong wrapping em', () => {
    expect(parseInline('***both***')).toEqual([
      { type: 'strong', children: [{ type: 'em', children: [text('both')] }] },
    ]);
  });

  // Documented limitation: mixed-width nesting needs CommonMark's delimiter
  // stack. It must degrade to visible asterisks, never to dropped content.
  it('keeps all text when mixed-width emphasis cannot be resolved', () => {
    const flat = JSON.stringify(parseInline('**bold *and italic***'));
    expect(flat).toContain('bold');
    expect(flat).toContain('and italic');
  });

  it('leaves an unmatched marker as literal text', () => {
    expect(parseInline('2 * 3 = 6')).toEqual([text('2 * 3 = 6')]);
    expect(parseInline('**unclosed')).toEqual([text('**unclosed')]);
    expect(parseInline('`unclosed')).toEqual([text('`unclosed')]);
  });

  it('does NOT treat underscores as emphasis (snake_case survives)', () => {
    expect(parseInline('com_nous_nonce')).toEqual([text('com_nous_nonce')]);
  });

  it('leaves a bracket run that is not a link alone', () => {
    expect(parseInline('[just brackets]')).toEqual([text('[just brackets]')]);
  });
});

describe('parseInline — link scheme allowlist', () => {
  it('keeps http, https and mailto links', () => {
    for (const href of ['https://example.com', 'http://example.com', 'mailto:a@b.co']) {
      expect(parseInline(`[go](${href})`)).toEqual([
        { type: 'link', href, children: [text('go')] },
      ]);
    }
  });

  it('renders a javascript: link as plain text, never as a link node', () => {
    const nodes = parseInline('[click](javascript:alert)');
    expect(nodes).toEqual([text('click')]);
    expect(nodes.some((n) => n.type === 'link')).toBe(false);
  });

  it('rejects data: and relative hrefs too', () => {
    for (const href of ['data:text/html,<script>', '/local/path', 'file:///etc/passwd']) {
      const nodes = parseInline(`[x](${href})`);
      expect(nodes.some((n) => n.type === 'link')).toBe(false);
    }
  });

  it('drops no label content when the scheme is rejected', () => {
    expect(parseInline('before [**bold label**](javascript:x) after')).toEqual([
      text('before '),
      { type: 'strong', children: [text('bold label')] },
      text(' after'),
    ]);
  });

  it('rejects a scheme hidden behind leading whitespace', () => {
    expect(parseInline('[x](  javascript:alert)').some((n) => n.type === 'link')).toBe(false);
  });
});
