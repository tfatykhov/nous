import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, fireEvent, cleanup } from '@testing-library/svelte';
import { tick } from 'svelte';
import AppHeaderView from './AppHeaderView.svelte';
import AppFooterView from './AppFooterView.svelte';
import SectionView from './SectionView.svelte';
import StatRowView from './StatRowView.svelte';
import TimelineView from './TimelineView.svelte';
import { store } from '../store.svelte';
import { transport } from '../transport';

// F092.1 micro-app adapter tests. Same harness discipline as phase2.test.ts:
// module-singleton store reset per test, transport spied, settle() after
// async click handlers (setTimeout(0) then tick — one alone is not enough).

const SURFACE = 'microapp-test-surface';

async function settle(): Promise<void> {
  await new Promise((r) => setTimeout(r, 0));
  await tick();
}

function seedSurface(
  dataModel: Record<string, unknown>,
  components: Record<string, unknown>[] = [],
) {
  store.reset();
  store.apply(null, {
    version: 'v1.0',
    createSurface: {
      surfaceId: SURFACE,
      catalogId: 'nous-core',
      components,
      dataModel,
      metadata: { extensions: { com_nous_nonce: 'test-nonce' } },
    },
  } as never);
}

beforeEach(() => store.reset());
afterEach(() => {
  // globals:false disables testing-library's automatic cleanup, and the
  // render-result queries bind to document.body — without this, test 1's
  // footer is still mounted when test 2 queries for "refresh".
  cleanup();
  vi.restoreAllMocks();
});

describe('AppHeaderView', () => {
  it('resolves the bound composedAt and renders a fresh stamp', () => {
    seedSurface({ meta: { composedAt: new Date(Date.now() - 5 * 60_000).toISOString() } });
    const { container } = render(AppHeaderView, {
      props: {
        surfaceId: SURFACE,
        comp: {
          id: 'header',
          component: 'AppHeader',
          title: 'Italy — Sep 5-20',
          subtitle: 'departs in 7 days',
          composedAt: { path: '/meta/composedAt' },
          staleAfterS: 3600,
        },
      },
    });

    expect(container.querySelector('h2')?.textContent).toBe('Italy — Sep 5-20');
    expect(container.textContent).toContain('departs in 7 days');
    const stamp = container.querySelector('.stamp')!;
    expect(stamp.textContent).toContain('5m ago');
    expect(stamp.classList.contains('stale')).toBe(false);
  });

  it('degrades the stamp to amber past staleAfterS', () => {
    seedSurface({ meta: { composedAt: new Date(Date.now() - 2 * 3600_000).toISOString() } });
    const { container } = render(AppHeaderView, {
      props: {
        surfaceId: SURFACE,
        comp: {
          id: 'header',
          component: 'AppHeader',
          title: 'T',
          composedAt: { path: '/meta/composedAt' },
          staleAfterS: 3600,
        },
      },
    });

    expect(container.querySelector('.stamp')?.classList.contains('stale')).toBe(true);
  });
});

describe('AppFooterView', () => {
  function renderFooter(refineOptions: unknown = [{ id: 'blockers', label: 'Just the blockers' }]) {
    seedSurface({});
    return render(AppFooterView, {
      props: {
        surfaceId: SURFACE,
        comp: { id: 'footer', component: 'AppFooter', refineOptions, showRefresh: true },
      },
    });
  }

  it('refine button calls app.refine with the option id over the RPC channel', async () => {
    const spy = vi
      .spyOn(transport, 'callAgentFunction')
      .mockResolvedValue({ ok: true, message: '', value: {} });
    const { getByText } = renderFooter();

    await fireEvent.click(getByText('Just the blockers'));
    await settle();

    expect(spy).toHaveBeenCalledWith(SURFACE, 'app.refine', { id: 'blockers' });
  });

  it('refresh calls app.refresh and close posts the app.close action', async () => {
    const rpc = vi
      .spyOn(transport, 'callAgentFunction')
      .mockResolvedValue({ ok: true, message: '', value: {} });
    const act = vi
      .spyOn(transport, 'postAction')
      .mockResolvedValue({ ok: true, message: '', resolved: true });
    const { getByText } = renderFooter([]);

    await fireEvent.click(getByText('refresh'));
    await settle();
    await fireEvent.click(getByText('close'));
    await settle();

    expect(rpc).toHaveBeenCalledWith(SURFACE, 'app.refresh', {});
    expect(act).toHaveBeenCalledWith(SURFACE, 'app.close', 'footer', {});
  });

  it('paints a rejected call inline and stays interactive', async () => {
    vi.spyOn(transport, 'callAgentFunction').mockResolvedValue({
      ok: false,
      message: 'too many calls; slow down',
    });
    const { getByText, container } = renderFooter([]);

    await fireEvent.click(getByText('refresh'));
    await settle();

    expect(container.querySelector('[role="alert"]')?.textContent).toContain('too many calls');
    expect((getByText('refresh') as HTMLButtonElement).disabled).toBe(false);
  });

  it('ignores malformed refine options', () => {
    const { container } = renderFooter([{ id: 'ok', label: 'OK' }, { bad: true }, 'junk']);
    expect(container.querySelectorAll('.controls button').length).toBe(3); // ok + refresh + close
  });
});

describe('SectionView', () => {
  it('renders the title and its child component', () => {
    seedSurface({}, [{ id: 'body', component: 'Text', text: 'Flight details here' }]);
    const { container } = render(SectionView, {
      props: {
        surfaceId: SURFACE,
        comp: { id: 'sec1', component: 'Section', title: 'Flights', child: 'body' },
      },
    });

    expect(container.querySelector('h3')?.textContent).toBe('Flights');
    expect(container.textContent).toContain('Flight details here');
    expect(container.querySelector('.chip')).toBeNull();
  });

  it('marks model-supplied sections amber with a chip', () => {
    seedSurface({}, [{ id: 'body', component: 'Text', text: 'x' }]);
    const { container } = render(SectionView, {
      props: {
        surfaceId: SURFACE,
        comp: {
          id: 'sec1',
          component: 'Section',
          title: 'Where you sleep',
          child: 'body',
          provenance: 'model',
        },
      },
    });

    expect(container.querySelector('.app-section')?.classList.contains('model')).toBe(true);
    expect(container.querySelector('.chip')?.textContent).toBe('model-supplied');
  });
});

describe('StatRowView', () => {
  it('renders its StatTile children through the walker', () => {
    seedSurface({}, [
      { id: 't1', component: 'StatTile', label: 'Days out', value: '7' },
      { id: 't2', component: 'StatTile', label: 'Booked', value: '5/5' },
    ]);
    const { container } = render(StatRowView, {
      props: {
        surfaceId: SURFACE,
        comp: { id: 'stats', component: 'StatRow', children: ['t1', 't2'] },
      },
    });

    expect(container.textContent).toContain('Days out');
    expect(container.textContent).toContain('5/5');
  });
});

describe('TimelineView', () => {
  it('renders time-ordered items and highlights flagged rows', () => {
    seedSurface({
      stays: [
        { at: 'Sep 5', label: 'Venice', detail: '2 nights' },
        { at: 'Sep 7', label: 'GAP — nothing booked', flag: true },
      ],
    });
    const { container } = render(TimelineView, {
      props: {
        surfaceId: SURFACE,
        comp: { id: 'tl', component: 'Timeline', items: { path: '/stays' } },
      },
    });

    const rows = container.querySelectorAll('li');
    expect(rows.length).toBe(2);
    expect(rows[0].textContent).toContain('Venice');
    expect(rows[1].classList.contains('flag')).toBe(true);
  });

  it('resolves non-array data to no rows, never throws', () => {
    seedSurface({});
    const { container } = render(TimelineView, {
      props: {
        surfaceId: SURFACE,
        comp: { id: 'tl', component: 'Timeline', items: { path: '/missing' } },
      },
    });

    expect(container.querySelectorAll('li').length).toBe(0);
  });
});
