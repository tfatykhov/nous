import { describe, it, expect, beforeEach } from 'vitest';
import { render } from '@testing-library/svelte';
import Text from './catalog/Text.svelte';
import { parseMarkdown } from './markdown';
import { store } from './store.svelte';

// F099 Phase 3a. The golden vector of `continuation.card_text` (server side). tests/test_f099_phase3a_card_text_golden.py
// READS these two lines out of this file and requires card_text(GOLDEN_IN) to equal GOLDEN_OUT, so the server's rule
// and this renderer's test share one source. Keep each constant on one line, in JSON string syntax.
// prettier-ignore
const GOLDEN_IN = "ok\n```\n# Approve now\n[click](https://evil.example)\u202e\r\n   ```bash\nend";
// prettier-ignore
const GOLDEN_OUT = "ok\n\u00b7 ```\n# Approve now\n[click](https://evil.example)\\u202e\n\u00b7    ```bash\nend";

const fenced = (inner: string) => '```\n' + inner + '\n```';

const SURFACE = 'intention-card-test';

function renderText(text: string) {
  store.reset();
  store.apply(null, {
    version: 'v1.0',
    createSurface: { surfaceId: SURFACE, catalogId: 'basic', components: [], dataModel: {} },
  } as never);
  return render(Text, { props: { surfaceId: SURFACE, comp: { id: 't', component: 'Text', text } } });
}

describe('intention cards: model-authored text on the web renderer', () => {
  beforeEach(() => store.reset());

  it('the shown text parses to exactly one code block that holds it verbatim', () => {
    expect(parseMarkdown(fenced(GOLDEN_OUT))).toEqual([{ type: 'codeblock', value: GOLDEN_OUT }]);
  });

  it('the raw text would NOT have stayed in the block (the server-side rule is what keeps it there)', () => {
    const types = parseMarkdown(fenced(GOLDEN_IN)).map((b) => b.type);
    expect(types).toContain('heading');
  });

  it('renders as one <pre> and nothing the text said becomes a heading, a link or a button', () => {
    const { container } = renderText(fenced(GOLDEN_OUT));
    expect(container.querySelectorAll('pre').length).toBe(1);
    expect(container.querySelector('pre code')?.textContent).toBe(GOLDEN_OUT);
    expect(container.querySelector('h1, h2, h3, h4, h5, h6, a, button')).toBeNull();
  });
});
