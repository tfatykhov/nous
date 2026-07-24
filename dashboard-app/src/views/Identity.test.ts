import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';
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
