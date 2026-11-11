import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within, cleanup } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { parseBlocks, styleBlocks } from '../lib/testing/css';
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
  });

  it('an open fact\'s Save, Set core and Deactivate are shared buttons; Deactivate hovers in the error colour', async () => {
    render(Identity);
    await screen.findByText('Tim prefers Celsius for temperature readings');
    await fireEvent.click(screen.getByRole('button', { name: 'Show details for fact-1' }));
    const edit = document.querySelector('.fact-edit') as HTMLElement;
    for (const name of ['Save', 'Set core', 'Deactivate']) {
      expect(within(edit).getByRole('button', { name }).getAttribute('data-slot'), name).toBe('button');
    }
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
