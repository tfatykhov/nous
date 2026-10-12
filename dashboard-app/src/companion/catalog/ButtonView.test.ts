import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, fireEvent, cleanup } from '@testing-library/svelte';
import { tick } from 'svelte';
import Renderer from '../Renderer.svelte';
import { store } from '../store.svelte';
import { transport } from '../transport';

// #701: a findings card row holds Acknowledge / Resolve / Dismiss buttons for
// one finding. Each button kept its own success notice, so Acknowledge then
// Resolve left two contradictory notices under the same finding.

const SURFACE = 'button-test-surface';

async function settle(): Promise<void> {
  await new Promise((r) => setTimeout(r, 0));
  await tick();
}

function button(id: string, verb: string) {
  return [
    { id, component: 'Button', child: `${id}_l`, action: { event: { name: verb, context: {} } } },
    { id: `${id}_l`, component: 'Text', text: id },
  ];
}

function seed() {
  store.apply(null, {
    version: 'v1.0',
    createSurface: {
      surfaceId: SURFACE,
      catalogId: 'nous-core',
      components: [
        { id: 'root', component: 'Column', children: ['acts0', 'acts1'] },
        { id: 'acts0', component: 'Row', children: ['ack0', 'res0'] },
        { id: 'acts1', component: 'Row', children: ['ack1', 'dis1'] },
        ...button('ack0', 'heartbeat.acknowledge'),
        ...button('res0', 'heartbeat.resolve'),
        ...button('ack1', 'heartbeat.acknowledge'),
        ...button('dis1', 'heartbeat.dismiss'),
      ],
      dataModel: {},
      metadata: { extensions: { com_nous_nonce: 'test-nonce' } },
    },
  } as never);
}

beforeEach(() => {
  store.reset();
  vi.spyOn(transport, 'postAction').mockImplementation(async (_s, name) => ({
    ok: true,
    message: `done: ${name}`,
  }));
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('ButtonView notices', () => {
  it('a press clears the success notice of its sibling buttons only', async () => {
    seed();
    const { container, getByText } = render(Renderer, {
      props: { surfaceId: SURFACE, componentId: 'root' },
    });
    await settle();
    const notices = () => [...container.querySelectorAll('.ok')].map((n) => n.textContent);

    await fireEvent.click(getByText('ack0'));
    await settle();
    expect(notices()).toEqual(['done: heartbeat.acknowledge']);

    await fireEvent.click(getByText('res0'));
    await settle();
    expect(notices()).toEqual(['done: heartbeat.resolve']);

    // Another finding's row is not a sibling: its press leaves row 0 alone.
    await fireEvent.click(getByText('dis1'));
    await settle();
    expect(notices()).toEqual(['done: heartbeat.resolve', 'done: heartbeat.dismiss']);
  });
});
