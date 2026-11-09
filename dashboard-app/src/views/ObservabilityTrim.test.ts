import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/svelte';
import Observability from './Observability.svelte';

// F108 Phase 2 T10: the last-call token breakdown and the recent API calls moved to the Model calls screen. The
// endpoint still returns `context_log`; the view no longer shows it or fetches a call's text or payload.
const ENTRY = {
  id: 'ctx-1', session_id: 's-1', turn_number: 2, timestamp: '2026-11-07T00:07:00+00:00', call_type: 'main_loop',
  model: 'claude-opus-5-5', frame_id: 'task', trace_id: null, token_breakdown: { messages: 3000, identity: 500 },
  total_tokens_est: 3500, context_window_size: 200000, utilization_pct: 1.75, sections_present: ['messages'],
  tools_count: 2, tool_names: ['bash', 'recall_deep'], messages_count: 4, loaded_facts: 1, loaded_decisions: 0,
  input_tokens_actual: 1200, output_tokens: 300, cache_read: 2000, duration_ms: 4200, stop_reason: 'end_turn',
};
const OBS = {
  event_bus: {
    total_processed: 12, total_dropped: 0, queue_depth: 0, uptime_seconds: 3700, event_counts: {},
    handlers: { 'nous.handlers.fact_extractor.handle': { invocations: 4, successes: 4, errors: 0, error_rate: 0, avg_duration_ms: 3.2 } },
  },
  recent_traces: [{ trace_id: 't-1', root_type: 'turn_completed', timestamp: '2026-11-07T00:07:00+00:00', event_count: 3, has_modifications: false }],
  recent_modifications: [],
  drift: null,
  drift_trends: {},
  context_log: [ENTRY],
};

let urls: string[] = [];

describe('Observability after the Model calls move', () => {
  beforeEach(() => {
    urls = [];
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      urls.push(url);
      return { ok: true, status: 200, json: async () => (url.startsWith('/dashboard/observability') ? OBS : {}) } as Response;
    }));
    vi.stubGlobal('Chart', class { destroy() {} update() {} data = {}; options = {}; });
  });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it('no longer shows the last-call token breakdown or the recent API calls', async () => {
    render(Observability);
    await screen.findByRole('heading', { level: 2, name: 'Handler Health' });
    expect(screen.queryByText('Context Token Breakdown (Last Call)')).toBeNull();
    expect(screen.queryByText('Recent API Calls')).toBeNull();
  });

  it('keeps the other sections', async () => {
    render(Observability);
    for (const name of ['Handler Health', 'Recent Causal Traces', 'Autonomous Modifications (24h)']) {
      expect(await screen.findByRole('heading', { level: 2, name })).toBeTruthy();
    }
  });

  it('never fetches a call\'s context text or raw payload', async () => {
    render(Observability);
    await screen.findByRole('heading', { level: 2, name: 'Handler Health' });
    // The buttons that would fetch a call's text or payload are gone from the screen, so there is nothing to press.
    expect(screen.queryAllByRole('button', { name: /Show details for/ })).toEqual([]);
    for (const name of ['View Context Text', 'View Raw Payload']) expect(screen.queryByRole('button', { name })).toBeNull();
    await new Promise((r) => setTimeout(r, 0));
    expect(urls.filter((u) => u.includes('/context/log/'))).toEqual([]);
    expect(urls.every((u) => u.startsWith('/dashboard/observability'))).toBe(true);
  });

  it('points to the Model calls screen in one line', async () => {
    render(Observability);
    await screen.findByRole('heading', { level: 2, name: 'Handler Health' });
    const link = screen.getByRole('link', { name: 'Model calls' });
    expect(link.getAttribute('href')).toBe('#/model-calls');
    expect(link.closest('p')?.textContent?.trim()).toBe('Model calls moved to Model calls');
  });
});
