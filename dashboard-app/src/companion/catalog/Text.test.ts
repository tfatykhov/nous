import { describe, it, expect, beforeEach } from 'vitest';
import { render } from '@testing-library/svelte';
import Text from './Text.svelte';
import { store } from '../store.svelte';

// Render-level companion to markdown.test.ts. The parser tests prove the AST
// is right; these prove the AST reaches the DOM as real elements — and, in
// particular, that MarkdownInline leaks no whitespace. Paragraphs render with
// `white-space: pre-wrap`, so a newline between two inline nodes in the
// template would show up as a visible gap mid-sentence; that is invisible to
// an AST test and obvious here.

const SURFACE = 'text-test-surface';

function renderText(text: unknown, extra: Record<string, unknown> = {}) {
  store.reset();
  store.apply(null, {
    version: 'v1.0',
    createSurface: { surfaceId: SURFACE, catalogId: 'basic', components: [], dataModel: { who: 'Ada' } },
  } as never);
  return render(Text, {
    props: { surfaceId: SURFACE, comp: { id: 't', component: 'Text', text, ...extra } },
  });
}

describe('Text — markdown rendering', () => {
  beforeEach(() => store.reset());

  it('renders bold and italic without inserting stray whitespace', () => {
    const { container } = renderText('This is **bold** text and *italic* text.');
    const p = container.querySelector('p')!;
    expect(p.querySelector('strong')?.textContent).toBe('bold');
    expect(p.querySelector('em')?.textContent).toBe('italic');
    expect(p.textContent).toBe('This is bold text and italic text.');
  });

  it('renders headings at the right level', () => {
    expect(renderText('# One').container.querySelector('h1')?.textContent).toBe('One');
    expect(renderText('### Markdown Rendering').container.querySelector('h3')?.textContent).toBe(
      'Markdown Rendering',
    );
  });

  it('renders a bullet list as a real ul/li', () => {
    const { container } = renderText('- List item 1\n- List item 2');
    const items = Array.from(container.querySelectorAll('ul li')).map((li) => li.textContent);
    expect(items).toEqual(['List item 1', 'List item 2']);
  });

  it('renders an allowed link with target and rel', () => {
    const { container } = renderText('[Link to Google](https://google.com)');
    const a = container.querySelector('a')!;
    expect(a.getAttribute('href')).toBe('https://google.com');
    expect(a.getAttribute('target')).toBe('_blank');
    expect(a.getAttribute('rel')).toBe('noopener noreferrer');
    expect(a.textContent).toBe('Link to Google');
  });

  it('renders a javascript: link as text, emitting no anchor at all', () => {
    const { container } = renderText('[click](javascript:alert)');
    expect(container.querySelector('a')).toBeNull();
    expect(container.textContent).toContain('click');
  });

  it('never emits raw HTML from the text body', () => {
    const { container } = renderText('<img src=x onerror=alert(1)> plain');
    expect(container.querySelector('img')).toBeNull();
    expect(container.textContent).toContain('<img src=x onerror=alert(1)>');
  });

  it('renders inline code and fenced code blocks', () => {
    expect(renderText('use `npm run test`').container.querySelector('code')?.textContent).toBe(
      'npm run test',
    );
    expect(renderText('```\nx = 1\n```').container.querySelector('pre code')?.textContent).toBe(
      'x = 1',
    );
  });

  it('resolves a DynamicString binding before parsing it as markdown', () => {
    const { container } = renderText({ call: 'formatString', args: { value: '**${/who}**' } });
    expect(container.querySelector('strong')?.textContent).toBe('Ada');
  });

  it('applies the caption variant to the block wrapper', () => {
    const { container } = renderText('small print', { variant: 'caption' });
    expect(container.querySelector('.md')?.classList.contains('caption')).toBe(true);
  });
});
