import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within, cleanup } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { markupOf, parseBlocks, styleBlocks } from '../lib/testing/css';
import Identity from './Identity.svelte';

// Render smoke for the Identity view: mocks fetch so both panels load and a
// section save round-trips to the right endpoint. Doubles as the Task-4 manual
// smoke's frontend half (the API half is tests/test_profile_facts_api.py).

const IDENTITY = {
  agent_id: 'test-agent',
  is_initiated: true,
  sections: { character: 'Curious and precise', values: 'Honesty above all' },
};

const FACTS = {
  facts: [
    {
      id: 'fact-1',
      content: 'Tim prefers Celsius for temperature readings',
      category: 'preference',
      subject: 'Tim',
      confidence: 0.8,
      active: true,
      tags: [],
      superseded_by: null,
      actionable: null,
      event_date: null,
      source: null,
      source_episode_id: null,
      learned_at: '2026-01-01T00:00:00Z',
      created_at: '2026-01-01T00:00:00Z',
    },
  ],
  total: 1,
};

function jsonResp(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as Response;
}

let calls: Array<{ url: string; method: string; body: unknown }>;

function installFetch() {
  calls = [];
  const fetchMock = vi.fn(async (url: string, opts?: RequestInit) => {
    const method = opts?.method ?? 'GET';
    const body = opts?.body ? JSON.parse(opts.body as string) : undefined;
    calls.push({ url, method, body });
    if (url.startsWith('/identity/') && method === 'PUT') {
      return jsonResp({ status: 'updated', section: url.split('/').pop() });
    }
    if (url === '/identity') return jsonResp(IDENTITY);
    if (url.startsWith('/profile/facts')) return jsonResp(FACTS);
    if (url.startsWith('/facts/') && method === 'PUT') {
      return jsonResp({ status: 'superseded', new_fact_id: 'fact-2' });
    }
    throw new Error(`unexpected ${method} ${url}`);
  });
  vi.stubGlobal('fetch', fetchMock);
}

describe('Identity view', () => {
  beforeEach(() => {
    installFetch();
    vi.stubGlobal('confirm', () => true);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('renders both panels, seeds identity sections, and lists profile facts', async () => {
    render(Identity);

    expect(await screen.findByText('Agent Identity')).toBeTruthy();
    expect(screen.getByText('User Profile Facts (Tier-1)')).toBeTruthy();

    // Section textarea seeded from GET /identity (findByDisplayValue retries
    // until the async load flushes to the bound textarea).
    const character = (await screen.findByDisplayValue('Curious and precise')) as HTMLTextAreaElement;
    expect(character.getAttribute('id')).toBe('identity-character');

    // Fact content (truncated display field) is listed.
    expect(await screen.findByText('Tim prefers Celsius for temperature readings')).toBeTruthy();
  });

  it('saving an edited identity section PUTs to /identity/{section}', async () => {
    render(Identity);
    const character = (await screen.findByLabelText('character')) as HTMLTextAreaElement;

    await fireEvent.input(character, { target: { value: 'Curious, precise, and kind' } });
    // Save button lives in the same card as the character textarea.
    const card = character.closest('.section-card')!;
    const saveBtn = card.querySelector('button')! as HTMLButtonElement;
    expect(saveBtn.disabled).toBe(false);

    await fireEvent.click(saveBtn);

    await waitFor(() => {
      const put = calls.find((c) => c.url === '/identity/character' && c.method === 'PUT');
      expect(put).toBeTruthy();
      expect(put!.body).toMatchObject({ content: 'Curious, precise, and kind', updated_by: 'dashboard' });
    });
  });
});

// ── Redesign PR 5: Identity on paper ────────────────────────────────────────────────────────────────────────────────

const CORE_FACT = { ...FACTS.facts[0], id: 'fact-core', content: 'Tim lives in Lisbon', tags: ['profile_core'] };
const TWO_FACTS = { facts: [CORE_FACT, FACTS.facts[0]], total: 2 };

describe('Identity in the paper look', () => {
  let failSave = false;
  beforeEach(() => {
    cleanup(); // the smoke tests above leave their render in the body
    failSave = false;
    vi.stubGlobal('confirm', () => true);
    vi.stubGlobal('fetch', vi.fn(async (url: string, opts?: RequestInit) => {
      const method = opts?.method ?? 'GET';
      if (url.startsWith('/identity/') && method === 'PUT') {
        return failSave
          ? ({ ok: false, status: 405, json: async () => ({ error: 'read-only preview' }) } as Response)
          : jsonResp({ status: 'updated' });
      }
      if (url === '/identity') return jsonResp(IDENTITY);
      if (url.startsWith('/profile/facts')) return jsonResp(TWO_FACTS);
      throw new Error(`unexpected ${method} ${url}`);
    }));
  });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it('the Core column says "yes" for a core fact and nothing otherwise, with no star', async () => {
    render(Identity);
    await screen.findByText('Tim lives in Lisbon');
    const coreOf = (content: string) => screen.getByText(content).closest('tr')!.querySelector('td[data-label="Core"]')!;
    expect(coreOf('Tim lives in Lisbon').textContent?.trim()).toBe('yes');
    expect(coreOf('Tim prefers Celsius for temperature readings').textContent?.trim()).toBe('');
    expect(document.body.textContent).not.toContain('★');
  });

  it('the Content cell holds the fact\'s text in its own element; every other cell is the plain value', async () => {
    render(Identity);
    await screen.findByText('Tim lives in Lisbon');
    const row = screen.getByText('Tim lives in Lisbon').closest('tr')!;
    expect(row.querySelector('td[data-label="Content"] .fact-content')?.textContent).toBe('Tim lives in Lisbon');
    expect(row.querySelector('td[data-label="Subject"]')?.textContent?.trim()).toBe('Tim');
    expect(row.querySelector('td[data-label="Subject"] *')).toBeNull();
  });

  it('a section\'s Save is the shared button, disabled until the text changes and while it is blank', async () => {
    render(Identity);
    const character = (await screen.findByLabelText('character')) as HTMLTextAreaElement;
    await screen.findByDisplayValue('Curious and precise');
    const save = within(character.closest('.section-card') as HTMLElement).getByRole('button', { name: 'Save' }) as HTMLButtonElement;
    expect(save.tagName).toBe('BUTTON');
    expect(save.getAttribute('data-slot')).toBe('button');
    expect(save.disabled).toBe(true); // unchanged
    await fireEvent.input(character, { target: { value: 'Curious, precise, and kind' } });
    expect(save.disabled).toBe(false);
    await fireEvent.input(character, { target: { value: '   ' } });
    expect(save.disabled).toBe(true); // blank
  });

  it('a failed save says so in the error colour', async () => {
    failSave = true;
    render(Identity);
    const character = (await screen.findByLabelText('character')) as HTMLTextAreaElement;
    await screen.findByDisplayValue('Curious and precise');
    await fireEvent.input(character, { target: { value: 'Curious, precise, and kind' } });
    await fireEvent.click(within(character.closest('.section-card') as HTMLElement).getByRole('button', { name: 'Save' }));
    const msg = await waitFor(() => {
      const el = character.closest('.section-card')!.querySelector('.save-status');
      expect(el).not.toBeNull();
      return el!;
    });
    expect(msg.classList.contains('save-err')).toBe(true);
    // Review M4: what it says, and the edit is not lost.
    expect(msg.textContent?.trim()).toBe('HTTP 405 for /identity/character');
    expect(character.value).toBe('Curious, precise, and kind');
  });

  it('an open fact\'s Save, Set core and Deactivate are shared buttons; Deactivate hovers in the error colour', async () => {
    render(Identity);
    await screen.findByText('Tim prefers Celsius for temperature readings');
    await fireEvent.click(screen.getByRole('button', { name: 'Show details for fact-1' }));
    const edit = document.querySelector('.fact-edit') as HTMLElement;
    for (const name of ['Save', 'Set core', 'Deactivate']) {
      expect(within(edit).getByRole('button', { name }).getAttribute('data-slot'), name).toBe('button');
    }
    // At phone width the facts are cards, so an open fact's form is as wide as the screen, not as the table.
    expect(edit.closest('.dt')?.classList.contains('dt--cards')).toBe(true);
    const deactivate = within(edit).getByRole('button', { name: 'Deactivate' });
    const cls = deactivate.className.split(/\s+/);
    expect(cls).toContain('hover:text-[var(--err-text)]');
    expect(cls).toContain('hover:border-[var(--err-text)]');
    expect(cls).not.toContain('hover:border-[var(--muted)]'); // the outline variant's own hover border is merged away
  });
});

describe('Identity: the paper look\'s structure (source contracts)', () => {
  const src = readFileSync(join(__dirname, 'Identity.svelte'), 'utf-8');
  const rules = parseBlocks(styleBlocks(src));

  it('a text field has a visible boundary, and keyboard focus shows the ring instead of a border colour', () => {
    expect(rules.get('textarea')).toMatchObject({ border: '1px solid var(--field-border)', background: 'var(--bg)' });
    expect(rules.get('textarea:focus-visible')).toMatchObject({ outline: '2px solid var(--focus-ring)', 'outline-offset': '2px' });
    expect(rules.get('textarea:focus')).toBeUndefined();
    expect(styleBlocks(src)).not.toMatch(/outline:\s*none/);
  });

  it('the facts table sits in a card that reads the card tokens (review M6)', () => {
    expect(rules.get('.facts-card')).toMatchObject({ background: 'var(--surface)', border: 'var(--card-border)',
      'border-radius': 'var(--card-radius)', 'box-shadow': 'var(--card-shadow)' });
    expect(markupOf(src)).toMatch(/<div class="facts-card">\s*<DataTable/);
  });

  it('a fact\'s content may break anywhere, so a long unbroken fact cannot widen the table past its box (review M10)', () => {
    // It holds in cards mode as well: DataTable's reset of a cell's elements has no specificity (DataTable.test.ts).
    expect(rules.get('.facts-card .fact-content')).toMatchObject({ 'overflow-wrap': 'anywhere' });
  });

  it('a section card never asks for more than the screen\'s width (320 px or the full width, whichever is less)', () => {
    expect(rules.get('.section-grid')).toMatchObject({ 'grid-template-columns': 'repeat(auto-fill, minmax(min(320px, 100%), 1fr))' });
  });

  it('no dead colour fallback is left', () => {
    expect(src).not.toMatch(/var\(--(red|accent|bg|radius-sm),/);
  });

  it('cards, labels, save messages and the title read tokens', () => {
    expect(rules.get('.section-card')).toMatchObject({ border: 'var(--card-border)', 'border-radius': 'var(--card-radius-sm)', 'box-shadow': 'var(--card-shadow)' });
    expect(rules.get('.section-label')).toMatchObject({ 'text-transform': 'var(--label-transform)', 'letter-spacing': 'var(--label-tracking)' });
    expect(rules.get('.save-status.save-ok')).toMatchObject({ color: 'var(--save-ok)' });
    expect(rules.get('.save-status.save-err')).toMatchObject({ color: 'var(--save-err)' });
    expect(rules.get(':global(html[data-look="paper"] [data-ground="paper"]) h1')).toMatchObject({ 'font-size': '30px', 'font-weight': '650' });
  });

  it('the fact id takes the look\'s mono font through a scoped rule, leaving the shared global .mono alone', () => {
    expect(rules.get('.detail-grid .mono')).toMatchObject({ 'font-family': 'var(--font-mono)' });
    expect(rules.get(':global(.mono)')).toMatchObject({ 'font-family': 'monospace' });
  });
});

// ── PR 5 review M4 and M5: the fact write paths, by their requests and what the screen then shows ─────────────────
// (adapted from the reviewer's probes). Every one of these controls became the shared Button in PR 5; a swapped handler
// (Set core deactivating the fact) passed the earlier tests, which only read classes.

const wfact = (id: string, content: string, tags: string[] = []) => ({
  id, content, category: 'preference', subject: 'Tim', confidence: 0.8, active: true, tags, superseded_by: null,
  actionable: null, event_date: null, source: null, source_episode_id: null, learned_at: '2026-01-01T00:00:00Z', created_at: '2026-01-01T00:00:00Z',
});
const W_FACTS = { facts: [wfact('fact-1', 'Tim prefers Celsius'), wfact('fact-core', 'Tim lives in Lisbon', ['profile_core'])], total: 2 };

describe('Identity: the fact write paths (requests and the screen after them)', () => {
  let writes: { url: string; method: string; body: unknown }[];
  let factGets: number;
  let confirmAnswer: boolean;
  let confirmAsked: string[];
  let fail: boolean;
  let putReply: Record<string, unknown>;

  beforeEach(() => {
    cleanup();
    writes = []; factGets = 0; confirmAsked = []; confirmAnswer = true; fail = false;
    putReply = { status: 'superseded', new_fact_id: 'fact-2' };
    vi.stubGlobal('confirm', (q: string) => { confirmAsked.push(q); return confirmAnswer; });
    vi.stubGlobal('fetch', vi.fn(async (url: string, opts?: RequestInit) => {
      const method = opts?.method ?? 'GET';
      if (method !== 'GET') {
        writes.push({ url, method, body: opts?.body ? JSON.parse(opts.body as string) : undefined });
        if (fail) return { ok: false, status: 500, json: async () => ({}) } as Response;
      }
      if (url === '/identity') return jsonResp(IDENTITY);
      if (url.startsWith('/profile/facts')) { factGets++; return jsonResp(W_FACTS); }
      if (url.startsWith('/facts/') && method === 'PUT') return jsonResp(putReply);
      if (url.startsWith('/facts/')) return jsonResp({ status: 'ok' });
      if (url.startsWith('/identity/')) return jsonResp({ status: 'updated' });
      throw new Error(`unexpected ${method} ${url}`);
    }));
  });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  async function open(id: string): Promise<HTMLElement> {
    render(Identity);
    await screen.findByText('Tim prefers Celsius');
    await fireEvent.click(screen.getByRole('button', { name: `Show details for ${id}` }));
    return document.querySelector('.fact-edit') as HTMLElement;
  }
  const button = (edit: HTMLElement, name: string) => within(edit).getByRole('button', { name }) as HTMLButtonElement;

  it('the open fact\'s field is named "Fact content" and starts as the fact (M5)', async () => {
    const edit = await open('fact-1');
    const field = within(edit).getByRole('textbox', { name: 'Fact content' }) as HTMLTextAreaElement;
    expect(field.value).toBe('Tim prefers Celsius');
  });

  it('edit and save: a blank draft cannot be saved; Save PUTs the draft with the subject and confidence, then the list reloads', async () => {
    const edit = await open('fact-1');
    const field = edit.querySelector('textarea') as HTMLTextAreaElement;
    await fireEvent.input(field, { target: { value: '   ' } });
    expect(button(edit, 'Save').disabled).toBe(true);
    await fireEvent.input(field, { target: { value: 'Tim prefers Kelvin' } });
    expect(button(edit, 'Save').disabled).toBe(false);
    const before = factGets;
    await fireEvent.click(button(edit, 'Save'));
    await waitFor(() => expect(factGets).toBe(before + 1));
    expect(writes).toEqual([{ url: '/facts/fact-1', method: 'PUT', body: { content: 'Tim prefers Kelvin', subject: 'Tim', confidence: 0.8 } }]);
    expect(confirmAsked).toEqual([]); // an edit supersedes; it does not ask
    expect(document.body.textContent).not.toContain('Saving…');
  });

  it('a failed save shows the server\'s message, keeps the draft and leaves the three buttons usable', async () => {
    const edit = await open('fact-1');
    fail = true;
    const field = edit.querySelector('textarea') as HTMLTextAreaElement;
    await fireEvent.input(field, { target: { value: 'Tim prefers Kelvin' } });
    await fireEvent.click(button(edit, 'Save'));
    await waitFor(() => expect(within(edit).getByText('HTTP 500 for /facts/fact-1')).toBeTruthy());
    expect(field.value).toBe('Tim prefers Kelvin');
    for (const name of ['Save', 'Set core', 'Deactivate']) expect(button(edit, name).disabled, name).toBe(false);
  });

  it('a save the server merged into an existing fact says so above the list, with the stored text, and the list reloads', async () => {
    putReply = { status: 'merged_into_existing', stored_content: 'Tim prefers Celsius, always' };
    const edit = await open('fact-1');
    const before = factGets;
    await fireEvent.click(button(edit, 'Save'));
    await waitFor(() => expect(screen.getByText(/Your edit matched an existing fact/)).toBeTruthy());
    expect(screen.getByText(/Your edit matched an existing fact/).textContent).toContain('Stored: "Tim prefers Celsius, always"');
    await waitFor(() => expect(factGets).toBe(before + 1));
  });

  it('Set core POSTs core: true; a core fact\'s button reads Unset core and POSTs core: false', async () => {
    const edit = await open('fact-1');
    await fireEvent.click(button(edit, 'Set core'));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toEqual({ url: '/facts/fact-1/core', method: 'POST', body: { core: true } });
    expect(confirmAsked).toEqual([]); // reversible, so no question
    cleanup(); writes = [];
    const edit2 = await open('fact-core');
    await fireEvent.click(button(edit2, 'Unset core'));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toEqual({ url: '/facts/fact-core/core', method: 'POST', body: { core: false } });
  });

  it('cancel: Deactivate asks first, and a "no" sends nothing and leaves the fact open', async () => {
    const edit = await open('fact-1');
    confirmAnswer = false;
    await fireEvent.click(button(edit, 'Deactivate'));
    expect(confirmAsked).toHaveLength(1);
    expect(confirmAsked[0]).toMatch(/^Deactivate this fact\?/);
    expect(writes).toEqual([]);
    expect(within(edit).getByRole('textbox', { name: 'Fact content' })).toBeTruthy();
  });

  it('deactivate: a "yes" sends DELETE for that fact, and the list reloads', async () => {
    const edit = await open('fact-1');
    const before = factGets;
    await fireEvent.click(button(edit, 'Deactivate'));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toMatchObject({ url: '/facts/fact-1', method: 'DELETE' });
    await waitFor(() => expect(factGets).toBe(before + 1));
  });

  it('cancel: a section Save asks first, and a "no" sends nothing and keeps the edit', async () => {
    render(Identity);
    const character = (await screen.findByLabelText('character')) as HTMLTextAreaElement;
    await screen.findByDisplayValue('Curious and precise');
    await fireEvent.input(character, { target: { value: 'Kind' } });
    confirmAnswer = false;
    await fireEvent.click(within(character.closest('.section-card') as HTMLElement).getByRole('button', { name: 'Save' }));
    expect(confirmAsked).toHaveLength(1);
    expect(writes).toEqual([]);
    expect(character.value).toBe('Kind');
  });

  it('every control is a native focusable element, none out of the tab order, and the fact\'s three are type="button"', async () => {
    const edit = await open('fact-1');
    const controls = [...document.querySelectorAll('textarea, button, input')] as HTMLElement[];
    expect(controls.filter((c) => c.getAttribute('tabindex') === '-1')).toEqual([]);
    expect([...edit.querySelectorAll('button')].map((b) => b.getAttribute('type'))).toEqual(['button', 'button', 'button']);
  });
});
