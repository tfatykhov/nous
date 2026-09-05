import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, fireEvent, cleanup } from '@testing-library/svelte';
import { tick } from 'svelte';
import AppHeaderView from './AppHeaderView.svelte';
import AppFooterView from './AppFooterView.svelte';
import SectionView from './SectionView.svelte';
import StatRowView from './StatRowView.svelte';
import TimelineView from './TimelineView.svelte';
import ModalView from './ModalView.svelte';
import { store } from '../store.svelte';
import { transport } from '../transport';
import { ACT_STAMP_WAIT_MS } from '../activity';

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

  // F092.2 agent actions — server-stamped buttons that post app.act.
  function renderActionFooter(meta: Record<string, unknown> = {}) {
    seedSurface({ meta });
    return render(AppFooterView, {
      props: {
        surfaceId: SURFACE,
        comp: {
          id: 'footer',
          component: 'AppFooter',
          refineOptions: [],
          showRefresh: false,
          agentActions: [
            { id: 'rebalance', label: 'Rebalance' },
            { id: 'escalate', label: 'Escalate' },
          ],
        },
      },
    });
  }

  it('agent-action buttons post app.act with the actionId', async () => {
    const act = vi
      .spyOn(transport, 'postAction')
      .mockResolvedValue({ ok: true, message: '', resolved: false });
    const { getByText } = renderActionFooter();

    await fireEvent.click(getByText('Rebalance'));
    await settle();

    expect(act).toHaveBeenCalledWith(SURFACE, 'app.act', 'footer', { actionId: 'rebalance' });
  });

  it('a fresh pendingAction disables all agent-action buttons and marks the busy one', async () => {
    const act = vi.spyOn(transport, 'postAction');
    const { getByText } = renderActionFooter({
      pendingAction: { id: 'rebalance', label: 'Rebalance', at: new Date().toISOString() },
    });

    const busy = getByText('Rebalance') as HTMLButtonElement;
    const other = getByText('Escalate') as HTMLButtonElement;
    expect(busy.disabled).toBe(true);
    expect(busy.querySelector('.spin')).not.toBeNull();
    expect(other.querySelector('.spin')).toBeNull();
    expect(other.disabled).toBe(true);
    await fireEvent.click(other);
    await settle();
    expect(act).not.toHaveBeenCalled();
  });

  it('a stale pendingAction re-enables the buttons and renders an honest note', () => {
    // The watcher is in-process and dies with a restart — the timestamp is
    // what keeps a dead watcher from becoming an infinite spinner.
    const staleAt = new Date(Date.now() - 10 * 60 * 1000).toISOString();
    const { getByText, container } = renderActionFooter({
      pendingAction: { id: 'rebalance', label: 'Rebalance', at: staleAt },
    });

    expect((getByText('Rebalance') as HTMLButtonElement).disabled).toBe(false);
    expect(container.querySelector('.stale')?.textContent).toContain('no update');
  });

  it('derives the stale window from the stamp timeout_s, not a hardcoded client value', () => {
    // codex P2: the server's freshness guard uses the configurable
    // timeout; a hardcoded client window drifts from any non-default value.
    const twoMinAgo = new Date(Date.now() - 2 * 60 * 1000).toISOString();
    // timeout_s=60: 2 minutes ago is already stale despite the 5-min fallback.
    const short = renderActionFooter({
      pendingAction: { id: 'rebalance', label: 'Rebalance', at: twoMinAgo, timeout_s: 60 },
    });
    expect((short.getByText('Rebalance') as HTMLButtonElement).disabled).toBe(false);
    cleanup();
    // timeout_s=600: 2 minutes ago is still fresh well past nothing.
    const long = renderActionFooter({
      pendingAction: { id: 'rebalance', label: 'Rebalance', at: twoMinAgo, timeout_s: 600 },
    });
    expect((long.getByText('Rebalance') as HTMLButtonElement).disabled).toBe(true);
  });

  it('renders the server-written actionError', () => {
    const { container } = renderActionFooter({
      actionError: 'Rebalance: the action failed',
    });

    expect(container.querySelector('.err')?.textContent).toContain('the action failed');
  });

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
    // Two-tap confirmation on the destructive close: the first tap ARMS.
    await fireEvent.click(getByText('close'));
    await settle();
    expect(act).not.toHaveBeenCalled();
    await fireEvent.click(getByText('sure? close'));
    await settle();

    expect(rpc).toHaveBeenCalledWith(SURFACE, 'app.refresh', {});
    expect(act).toHaveBeenCalledWith(SURFACE, 'app.close', 'footer', {});
  });

  it('an armed close disarms by itself after the timeout', async () => {
    vi.useFakeTimers();
    const act = vi
      .spyOn(transport, 'postAction')
      .mockResolvedValue({ ok: true, message: '', resolved: true });
    const { getByText } = renderFooter([]);

    await fireEvent.click(getByText('close'));
    expect(getByText('sure? close')).toBeTruthy();
    vi.advanceTimersByTime(4500);
    await tick();
    expect(getByText('close')).toBeTruthy();
    expect(act).not.toHaveBeenCalled();
    vi.useRealTimers();
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

describe('ModalView', () => {
  // codex P2 on #626: the compose prompt advertises Text/Icon triggers,
  // which render non-focusable elements — so the wrapper itself must be a
  // keyboard-operable button, or modal-only detail is pointer-only.
  function renderModal() {
    seedSurface({}, [
      { id: 'mt', component: 'Text', text: 'details' },
      { id: 'mc', component: 'Text', text: 'the long detail' },
    ]);
    return render(ModalView, {
      props: {
        surfaceId: SURFACE,
        comp: { id: 'm', component: 'Modal', trigger: 'mt', content: 'mc' },
      },
    });
  }

  it('exposes the trigger as a focusable button with dialog semantics', () => {
    const { container } = renderModal();
    const trigger = container.querySelector('.trigger');
    expect(trigger?.getAttribute('role')).toBe('button');
    expect(trigger?.getAttribute('tabindex')).toBe('0');
    expect(trigger?.getAttribute('aria-haspopup')).toBe('dialog');
  });

  it('names a text trigger from its contents and a nameless one via fallback', async () => {
    // codex P2 round 2 on #626: an Icon trigger renders an aria-hidden SVG,
    // leaving a focusable button with NO accessible name. Fallback label
    // fires only when contents provide none — a Text trigger keeps its own
    // words as the name (a static aria-label would override them).
    const { container } = renderModal();
    await settle();
    expect(container.querySelector('.trigger')?.getAttribute('aria-label')).toBeNull();

    cleanup();
    seedSurface({}, [
      { id: 'mt', component: 'Icon', name: 'info' },
      { id: 'mc', component: 'Text', text: 'the long detail' },
    ]);
    const nameless = render(ModalView, {
      props: {
        surfaceId: SURFACE,
        comp: { id: 'm', component: 'Modal', trigger: 'mt', content: 'mc' },
      },
    });
    await settle();
    expect(nameless.container.querySelector('.trigger')?.getAttribute('aria-label')).toBe(
      'Show details',
    );
  });

  it('recomputes the fallback label when bound trigger text changes', async () => {
    // codex round-3 on #626: a data-bound Text trigger can resolve from
    // empty to populated via updateDataModel without comp changing — the
    // label logic watches the DOM, not just the component reference.
    seedSurface({ label: '' }, [
      { id: 'mt', component: 'Text', text: { path: '/label' } },
      { id: 'mc', component: 'Text', text: 'the long detail' },
    ]);
    const { container } = render(ModalView, {
      props: {
        surfaceId: SURFACE,
        comp: { id: 'm', component: 'Modal', trigger: 'mt', content: 'mc' },
      },
    });
    await settle();
    const trigger = container.querySelector('.trigger');
    expect(trigger?.getAttribute('aria-label')).toBe('Show details');

    store.apply(2, {
      updateDataModel: { surfaceId: SURFACE, path: '/label', value: 'Trip details' },
    } as never);
    await settle();
    expect(trigger?.getAttribute('aria-label')).toBeNull();
  });

  it('defers keyboard semantics to an interactive trigger child', async () => {
    // codex round-4 on #626: a Button trigger (legal outside micro-apps)
    // is itself a keyboard control — its key activation synthesizes a
    // click that bubbles to the wrapper's onclick. The wrapper stamping
    // role/tabindex on top would nest two controls, and preventDefault on
    // the bubbled keydown would suppress the button's own action.
    seedSurface({}, [
      {
        id: 'mt',
        component: 'Button',
        child: 'bl',
        action: { event: { name: 'x', context: {} } },
      },
      { id: 'bl', component: 'Text', text: 'Open' },
      { id: 'mc', component: 'Text', text: 'the long detail' },
    ]);
    const { container } = render(ModalView, {
      props: {
        surfaceId: SURFACE,
        comp: { id: 'm', component: 'Modal', trigger: 'mt', content: 'mc' },
      },
    });
    await settle();
    const trigger = container.querySelector('.trigger');
    expect(trigger?.getAttribute('role')).toBeNull();
    expect(trigger?.getAttribute('tabindex')).toBeNull();
    expect(trigger?.getAttribute('aria-label')).toBeNull();

    // Keydown on the child button must NOT be intercepted by the wrapper:
    // not prevented, and the modal does not open from the raw keydown.
    const childButton = container.querySelector('button') as HTMLElement;
    const notPrevented = await fireEvent.keyDown(childButton, { key: 'Enter' });
    await settle();
    expect(notPrevented).toBe(true);
    expect(container.textContent).not.toContain('the long detail');

    // The button's (synthesized) click bubbles to the wrapper and opens.
    await fireEvent.click(childButton);
    await settle();
    expect(container.textContent).toContain('the long detail');
  });

  it('opens on Enter and Space, not just click', async () => {
    for (const key of ['Enter', ' ']) {
      cleanup();
      const { container } = renderModal();
      const trigger = container.querySelector('.trigger') as HTMLElement;
      expect(container.textContent).not.toContain('the long detail');
      await fireEvent.keyDown(trigger, { key });
      await settle();
      expect(container.textContent).toContain('the long detail');
    }
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


// ---------------------------------------------------------------------------
// F092.4 activity indicator — header stamp, rail, pressed control, dimming
// ---------------------------------------------------------------------------

describe('activity indicator', () => {
  const header = {
    id: 'header',
    component: 'AppHeader',
    title: 'T',
    composedAt: { path: '/meta/composedAt' },
    staleAfterS: 3600,
  };

  function renderHeader(meta: Record<string, unknown> = {}) {
    seedSurface({ meta: { composedAt: new Date().toISOString(), ...meta } });
    return render(AppHeaderView, { props: { surfaceId: SURFACE, comp: header } });
  }

  it('an in-flight refresh turns the stamp into a live status and shows the rail', async () => {
    const { container } = renderHeader();
    // The live region exists BEFORE anything happens (a region inserted
    // together with its first message is not reliably announced) and
    // starts silent.
    const live = container.querySelector('[role="status"]')!;
    expect(live.getAttribute('aria-live')).toBe('polite');
    expect(live.textContent).toBe('');
    expect(container.querySelector('.rail')).toBeNull();

    store.beginActivity(SURFACE, 'refresh', 'refresh');
    await tick();

    const stamp = container.querySelector('.stamp.working')!;
    expect(stamp.textContent).toContain('refreshing');
    expect(stamp.textContent).toMatch(/[0-9]+s/);
    expect(container.querySelector('.rail')).not.toBeNull();
    // The transition is announced once, by the same persistent region; the
    // ticking elapsed value is outside it, and the visible stamp is hidden
    // from assistive tech so the words are not read twice.
    expect(container.querySelector('[role="status"]')).toBe(live);
    expect(live.textContent).toBe('refreshing');
    expect(live.querySelector('.elapsed')).toBeNull();
    expect(stamp.getAttribute('aria-hidden')).toBe('true');
  });

  it('a refine names what is happening, and success flashes then returns the stamp', async () => {
    const { container } = renderHeader();
    store.beginActivity(SURFACE, 'refine', 'blockers');
    await tick();
    expect(container.querySelector('.stamp.working')?.textContent).toContain('rethinking layout');

    store.endActivity(SURFACE, true);
    await tick();
    expect(container.querySelector('.rail')).toBeNull();
    expect(container.querySelector('.stamp.done')?.textContent).toContain('updated just now');
    expect(container.querySelector('[role="status"]')?.textContent).toBe('updated just now');

    // A failed call must not claim an update.
    store.beginActivity(SURFACE, 'refresh', 'refresh');
    store.endActivity(SURFACE, false);
    await tick();
    expect(container.querySelector('.stamp.done')).toBeNull();
    expect(container.querySelector('[role="status"]')?.textContent).toBe('');
  });

  it('a fresh agent-action stamp is "agent working" with elapsed time only, and goes amber when stale', async () => {
    const at = new Date(Date.now() - 125_000).toISOString();
    const { container } = renderHeader({ pendingAction: { id: 'rebalance', label: 'Rebalance', at, timeout_s: 300 } });
    const stamp = container.querySelector('.stamp.working')!;
    expect(stamp.textContent).toContain('agent working');
    expect(stamp.textContent).toContain('2m 0');
    expect(stamp.textContent).not.toContain('of');
    expect(container.querySelector('.rail')).not.toBeNull();

    cleanup();
    const staleAt = new Date(Date.now() - 10 * 60_000).toISOString();
    const stale = renderHeader({ pendingAction: { id: 'rebalance', label: 'Rebalance', at: staleAt } });
    expect(stale.container.querySelector('.rail')).toBeNull();
    expect(stale.container.querySelector('.stamp.stale')?.textContent).toContain('no update after 5m');
    expect(stale.container.querySelector('[role="status"]')?.textContent).toContain('no update after 5m');
  });

  it('an agent action completes when the app is recomposed after the tap', async () => {
    const at = new Date(Date.now() - 5_000).toISOString();
    const { container } = renderHeader({ pendingAction: { id: 'rebalance', label: 'Rebalance', at, timeout_s: 300 } });
    expect(container.querySelector('.stamp.working')).not.toBeNull();

    // The recompose replaces /meta wholesale: the stamp is gone AND
    // composedAt has moved past the tap.
    store.apply(null, {
      version: 'v1.0',
      updateDataModel: { surfaceId: SURFACE, path: '/meta', value: { composedAt: new Date().toISOString() } },
    } as never);
    await tick();
    expect(container.querySelector('.stamp.done')?.textContent).toContain('updated just now');
    expect(container.querySelector('[role="status"]')?.textContent).toBe('updated just now');
  });

  it('a late recompose after the stamp went stale still counts as the update', async () => {
    const at = new Date(Date.now() - 10 * 60_000).toISOString();
    const { container } = renderHeader({ pendingAction: { id: 'rebalance', label: 'Rebalance', at, timeout_s: 300 } });
    expect(container.querySelector('.stamp.stale')).not.toBeNull();

    store.apply(null, {
      version: 'v1.0',
      updateDataModel: { surfaceId: SURFACE, path: '/meta', value: { composedAt: new Date().toISOString() } },
    } as never);
    await tick();
    expect(container.querySelector('.stamp.done')?.textContent).toContain('updated just now');
  });

  it('completion survives the dedup replacement that destroys and remounts the header', async () => {
    const at = new Date(Date.now() - 5_000).toISOString();
    const header0 = renderHeader({ pendingAction: { id: 'rebalance', label: 'Rebalance', at, timeout_s: 300 } });
    expect(header0.container.querySelector('.stamp.working')).not.toBeNull();
    expect(store.tappedAt[SURFACE]).toBe(Date.parse(at));

    // service.py delivers a successful recompose as deleteSurface +
    // createSurface for the same id. The feed is keyed by id, so the
    // header is destroyed in between. Let its effect observe the absent
    // surface before it dies: nothing may be forgotten there.
    store.apply(null, { version: 'v1.0', deleteSurface: { surfaceId: SURFACE } } as never);
    await tick();
    expect(store.tappedAt[SURFACE]).toBe(Date.parse(at));
    cleanup();

    store.apply(null, {
      version: 'v1.0',
      createSurface: {
        surfaceId: SURFACE,
        catalogId: 'nous-core',
        components: [],
        dataModel: { meta: { composedAt: new Date().toISOString() } },
        metadata: { extensions: { com_nous_nonce: 'rotated-nonce' } },
      },
    } as never);
    const header1 = render(AppHeaderView, { props: { surfaceId: SURFACE, comp: header } });
    await tick();
    expect(header1.container.querySelector('.stamp.done')?.textContent).toContain('updated just now');
    expect(store.tappedAt[SURFACE]).toBeUndefined();
  });

  it('a failed agent action never reads as updated: the watcher clears the stamp without recomposing', async () => {
    const composedAt = new Date(Date.now() - 60_000).toISOString();
    const at = new Date(Date.now() - 5_000).toISOString();
    const { container } = renderHeader({
      composedAt,
      pendingAction: { id: 'rebalance', label: 'Rebalance', at, timeout_s: 300 },
    });
    expect(container.querySelector('.stamp.working')).not.toBeNull();

    // actions.py clears the stamp FIRST and writes actionError in a second
    // envelope. Neither state is a success: composedAt never moved.
    store.apply(null, {
      version: 'v1.0',
      updateDataModel: { surfaceId: SURFACE, path: '/meta/pendingAction', value: null },
    } as never);
    await tick();
    expect(container.querySelector('.stamp.working')).toBeNull();
    expect(container.querySelector('.rail')).toBeNull();
    expect(container.querySelector('.stamp.done')).toBeNull();

    store.apply(null, {
      version: 'v1.0',
      updateDataModel: { surfaceId: SURFACE, path: '/meta/actionError', value: 'Rebalance: the action failed' },
    } as never);
    await tick();
    expect(container.querySelector('.stamp.done')).toBeNull();
    expect(container.querySelector('[role="status"]')?.textContent).toBe('');
    // The ordinary freshness stamp is back — a minute-old compose, not "just now".
    expect(container.querySelector('.stamp')?.textContent).not.toContain('just now');
  });

  it('the pressed refresh control carries a spinner and a present-tense label while its call runs', async () => {
    let finish!: (v: { ok: boolean; message: string; value?: unknown }) => void;
    vi.spyOn(transport, 'callAgentFunction').mockImplementation(
      () => new Promise((resolve) => (finish = resolve)),
    );
    seedSurface({});
    const { getByText, container } = render(AppFooterView, {
      props: {
        surfaceId: SURFACE,
        comp: { id: 'footer', component: 'AppFooter', refineOptions: [], showRefresh: true },
      },
    });

    await fireEvent.click(getByText('refresh'));
    await tick();
    expect(store.activity[SURFACE]?.kind).toBe('refresh');
    const pressed = getByText('Refreshing') as HTMLButtonElement;
    expect(pressed.querySelector('.spin')).not.toBeNull();
    expect(pressed.classList.contains('pressed')).toBe(true);

    finish({ ok: true, message: '', value: {} });
    await settle();
    expect(store.activity[SURFACE]).toBeUndefined();
    expect(store.doneAt[SURFACE]).toBeGreaterThan(0);
    expect(container.querySelector('.spin')).toBeNull();
    expect((getByText('refresh') as HTMLButtonElement).disabled).toBe(false);
  });

  it('a successful action holds its activity until the server stamp arrives, then hands over', async () => {
    vi.spyOn(transport, 'postAction').mockResolvedValue({ ok: true, message: '' } as never);
    seedSurface({});
    const { getByText } = render(AppFooterView, {
      props: {
        surfaceId: SURFACE,
        comp: {
          id: 'footer',
          component: 'AppFooter',
          refineOptions: [],
          showRefresh: true,
          agentActions: [{ id: 'rebalance', label: 'Rebalance' }],
        },
      },
    });

    await fireEvent.click(getByText('Rebalance'));
    await settle();
    // The POST succeeded but no stamp has been delivered yet: the local
    // activity stays, the pressed control keeps its spinner, and every
    // control stays disabled — no gap for a second tap.
    expect(store.activity[SURFACE]).toMatchObject({ kind: 'act', id: 'rebalance' });
    const pressed = getByText('Rebalance') as HTMLButtonElement;
    expect(pressed.disabled).toBe(true);
    expect(pressed.querySelector('.spin')).not.toBeNull();
    expect((getByText('refresh') as HTMLButtonElement).disabled).toBe(true);

    // The SSE envelope lands: the stamp takes over and the local record ends.
    store.apply(null, {
      version: 'v1.0',
      updateDataModel: {
        surfaceId: SURFACE,
        path: '/meta/pendingAction',
        value: { id: 'rebalance', label: 'Rebalance', at: new Date().toISOString(), timeout_s: 300 },
      },
    } as never);
    await settle();
    expect(store.activity[SURFACE]).toBeUndefined();
    expect((getByText('Rebalance') as HTMLButtonElement).disabled).toBe(true);
    expect(getByText('Rebalance').querySelector('.spin')).not.toBeNull();
  });

  it('the hold is bounded: with no stamp in ACT_STAMP_WAIT_MS the controls release', async () => {
    vi.useFakeTimers();
    try {
      vi.spyOn(transport, 'postAction').mockResolvedValue({ ok: true, message: '' } as never);
      seedSurface({});
      const { getByText } = render(AppFooterView, {
        props: {
          surfaceId: SURFACE,
          comp: {
            id: 'footer',
            component: 'AppFooter',
            refineOptions: [],
            showRefresh: false,
            agentActions: [{ id: 'rebalance', label: 'Rebalance' }],
          },
        },
      });
      await fireEvent.click(getByText('Rebalance'));
      await vi.advanceTimersByTimeAsync(0);
      await tick();
      expect(store.activity[SURFACE]?.kind).toBe('act');

      await vi.advanceTimersByTimeAsync(ACT_STAMP_WAIT_MS + 1);
      await tick();
      expect(store.activity[SURFACE]).toBeUndefined();
      expect(store.doneAt[SURFACE]).toBeUndefined();
      expect((getByText('Rebalance') as HTMLButtonElement).disabled).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it('a footer destroyed mid-hold because its surface was removed releases the record', async () => {
    vi.spyOn(transport, 'postAction').mockResolvedValue({ ok: true, message: '' } as never);
    seedSurface({});
    const { getByText } = render(AppFooterView, {
      props: {
        surfaceId: SURFACE,
        comp: { id: 'footer', component: 'AppFooter', refineOptions: [], agentActions: [{ id: 'a', label: 'Go' }] },
      },
    });
    await fireEvent.click(getByText('Go'));
    await settle();
    expect(store.activity[SURFACE]?.holdSince).toBeGreaterThan(0);
    store.apply(null, { version: 'v1.0', deleteSurface: { surfaceId: SURFACE } } as never);
    cleanup();
    expect(store.activity[SURFACE]).toBeUndefined();
  });

  it('a route remount keeps the record: the new footer stays locked and the original response completes it', async () => {
    let finish!: (v: { ok: boolean; message: string; value?: unknown }) => void;
    vi.spyOn(transport, 'callAgentFunction').mockImplementation(
      () => new Promise((resolve) => (finish = resolve)),
    );
    seedSurface({});
    const footer = { id: 'footer', component: 'AppFooter', refineOptions: [], showRefresh: true };
    const first = render(AppFooterView, { props: { surfaceId: SURFACE, comp: footer } });
    await fireEvent.click(first.getByText('refresh'));
    await tick();
    const token = store.activity[SURFACE]?.token;
    expect(store.activity[SURFACE]?.kind).toBe('refresh');

    // Feed → focused view: the surface is unchanged, only the footer is
    // destroyed and mounted again.
    cleanup();
    expect(store.activity[SURFACE]?.token).toBe(token);
    const second = render(AppFooterView, { props: { surfaceId: SURFACE, comp: footer } });
    const pressed = second.getByText('Refreshing') as HTMLButtonElement;
    expect(pressed.disabled).toBe(true);
    expect(pressed.querySelector('.spin')).not.toBeNull();
    expect((second.getByText('close') as HTMLButtonElement).disabled).toBe(true);

    finish({ ok: true, message: '', value: {} });
    await settle();
    expect(store.activity[SURFACE]).toBeUndefined();
    expect(store.doneAt[SURFACE]).toBeGreaterThan(0);
    expect((second.getByText('refresh') as HTMLButtonElement).disabled).toBe(false);
  });

  it('an action handed off after a route remount is held by the new footer until the stamp', async () => {
    let finish!: (v: { ok: boolean; message: string }) => void;
    vi.spyOn(transport, 'postAction').mockImplementation(
      () => new Promise((resolve) => (finish = resolve)),
    );
    seedSurface({});
    const footer = {
      id: 'footer',
      component: 'AppFooter',
      refineOptions: [],
      showRefresh: false,
      agentActions: [{ id: 'a', label: 'Go' }],
    };
    const first = render(AppFooterView, { props: { surfaceId: SURFACE, comp: footer } });
    await fireEvent.click(first.getByText('Go'));
    await tick();
    cleanup();
    const second = render(AppFooterView, { props: { surfaceId: SURFACE, comp: footer } });
    finish({ ok: true, message: '' });
    await settle();
    expect(store.activity[SURFACE]?.holdSince).toBeGreaterThan(0);
    expect((second.getByText('Go') as HTMLButtonElement).disabled).toBe(true);

    store.apply(null, {
      version: 'v1.0',
      updateDataModel: {
        surfaceId: SURFACE,
        path: '/meta/pendingAction',
        value: { id: 'a', label: 'Go', at: new Date().toISOString(), timeout_s: 300 },
      },
    } as never);
    await settle();
    expect(store.activity[SURFACE]).toBeUndefined();
    // Still locked — by the fresh stamp now, not the record.
    expect((second.getByText('Go') as HTMLButtonElement).disabled).toBe(true);
  });

  it('a success response that lands after the stamp already came and went releases at once', async () => {
    let finish!: (v: { ok: boolean; message: string }) => void;
    vi.spyOn(transport, 'postAction').mockImplementation(
      () => new Promise((resolve) => (finish = resolve)),
    );
    seedSurface({});
    const { getByText, container } = render(AppFooterView, {
      props: {
        surfaceId: SURFACE,
        comp: {
          id: 'footer',
          component: 'AppFooter',
          refineOptions: [],
          showRefresh: true,
          agentActions: [{ id: 'a', label: 'Go' }],
        },
      },
    });
    await fireEvent.click(getByText('Go'));
    await tick();
    // Meanwhile over SSE: the stamp, then the watcher's failure — stamp
    // cleared, actionError written.
    const stamp = { id: 'a', label: 'Go', at: new Date().toISOString(), timeout_s: 300 };
    store.apply(null, { version: 'v1.0', updateDataModel: { surfaceId: SURFACE, path: '/meta/pendingAction', value: stamp } } as never);
    store.apply(null, { version: 'v1.0', updateDataModel: { surfaceId: SURFACE, path: '/meta/pendingAction', value: null } } as never);
    store.apply(null, { version: 'v1.0', updateDataModel: { surfaceId: SURFACE, path: '/meta/actionError', value: 'Go: the action failed' } } as never);
    await tick();

    finish({ ok: true, message: '' });
    await settle();
    expect(store.activity[SURFACE]).toBeUndefined();
    expect(store.doneAt[SURFACE]).toBeUndefined();
    expect((getByText('Go') as HTMLButtonElement).disabled).toBe(false);
    expect((getByText('refresh') as HTMLButtonElement).disabled).toBe(false);
    expect(container.textContent).toContain('Go: the action failed');
  });

  it('a footer destroyed while its POST is in flight releases its record; the late response resurrects nothing and clobbers no successor', async () => {
    let finish!: (v: { ok: boolean; message: string }) => void;
    vi.spyOn(transport, 'postAction').mockImplementation(
      () => new Promise((resolve) => (finish = resolve)),
    );
    seedSurface({});
    const first = render(AppFooterView, {
      props: {
        surfaceId: SURFACE,
        comp: { id: 'footer', component: 'AppFooter', refineOptions: [], agentActions: [{ id: 'a', label: 'Go' }] },
      },
    });
    await fireEvent.click(first.getByText('Go'));
    await tick();
    expect(store.activity[SURFACE]?.kind).toBe('act');

    // The whole action finished before the response arrived: the surface
    // was replaced (deleteSurface destroys this footer; createSurface
    // brings the successor).
    store.apply(null, { version: 'v1.0', deleteSurface: { surfaceId: SURFACE } } as never);
    cleanup();
    expect(store.activity[SURFACE]).toBeUndefined();
    store.apply(null, {
      version: 'v1.0',
      createSurface: {
        surfaceId: SURFACE,
        catalogId: 'nous-core',
        components: [],
        dataModel: { meta: { composedAt: new Date().toISOString() } },
        metadata: { extensions: { com_nous_nonce: 'rotated-nonce' } },
      },
    } as never);

    // The replacement footer begins its own record …
    let finishRefresh!: (v: { ok: boolean; message: string; value?: unknown }) => void;
    vi.spyOn(transport, 'callAgentFunction').mockImplementation(
      () => new Promise((resolve) => (finishRefresh = resolve)),
    );
    const second = render(AppFooterView, {
      props: { surfaceId: SURFACE, comp: { id: 'footer', component: 'AppFooter', refineOptions: [], showRefresh: true } },
    });
    await fireEvent.click(second.getByText('refresh'));
    await tick();
    expect(store.activity[SURFACE]?.kind).toBe('refresh');

    // … and the dead footer's response, when it finally lands, touches nothing.
    finish({ ok: true, message: '' });
    await settle();
    expect(store.activity[SURFACE]?.kind).toBe('refresh');
    expect(store.doneAt[SURFACE]).toBeUndefined();

    finishRefresh({ ok: true, message: '', value: {} });
    await settle();
    expect(store.activity[SURFACE]).toBeUndefined();
    expect(store.doneAt[SURFACE]).toBeGreaterThan(0);
  });

  it('the pressed control is identified by id, so two same-label controls never both spin', async () => {
    vi.spyOn(transport, 'postAction').mockResolvedValue({ ok: true, message: '' } as never);
    let finish!: (v: { ok: boolean; message: string; value?: unknown }) => void;
    vi.spyOn(transport, 'callAgentFunction').mockImplementation(
      () => new Promise((resolve) => (finish = resolve)),
    );
    seedSurface({});
    const { getAllByText } = render(AppFooterView, {
      props: {
        surfaceId: SURFACE,
        comp: {
          id: 'footer',
          component: 'AppFooter',
          showRefresh: false,
          refineOptions: [
            { id: 'week', label: 'Focus' },
            { id: 'month', label: 'Focus' },
          ],
          agentActions: [
            { id: 'sync-a', label: 'Sync' },
            { id: 'sync-b', label: 'Sync' },
          ],
        },
      },
    });

    const [syncA, syncB] = getAllByText('Sync') as HTMLButtonElement[];
    await fireEvent.click(syncB);
    await settle();
    expect(store.activity[SURFACE]).toMatchObject({ kind: 'act', id: 'sync-b' });
    expect(syncB.classList.contains('pressed')).toBe(true);
    expect(syncB.querySelector('.spin')).not.toBeNull();
    expect(syncA.classList.contains('pressed')).toBe(false);
    expect(syncA.querySelector('.spin')).toBeNull();

    // Release the hold, then a refine with a shared label.
    store.endActivity(SURFACE, false);
    await tick();
    cleanup();
    seedSurface({});
    const again = render(AppFooterView, {
      props: {
        surfaceId: SURFACE,
        comp: {
          id: 'footer',
          component: 'AppFooter',
          showRefresh: false,
          refineOptions: [
            { id: 'week', label: 'Focus' },
            { id: 'month', label: 'Focus' },
          ],
        },
      },
    });
    const [week, month] = again.getAllByText('Focus') as HTMLButtonElement[];
    await fireEvent.click(month);
    await tick();
    expect(store.activity[SURFACE]).toMatchObject({ kind: 'refine', id: 'month' });
    expect(month.classList.contains('pressed')).toBe(true);
    expect(week.classList.contains('pressed')).toBe(false);
    expect(week.querySelector('.spin')).toBeNull();
    finish({ ok: true, message: '', value: {} });
    await settle();
  });

  it('sections dim while the app is working and recover afterwards', async () => {
    seedSurface({}, [
      { id: 'sec', component: 'Section', title: 'S', child: 'txt' },
      { id: 'txt', component: 'Text', text: 'hello' },
    ]);
    const { container } = render(SectionView, {
      props: { surfaceId: SURFACE, comp: { id: 'sec', component: 'Section', title: 'S', child: 'txt' } },
    });
    const section = container.querySelector('.app-section')!;
    expect(section.classList.contains('dim')).toBe(false);

    store.beginActivity(SURFACE, 'refresh', 'refresh');
    await tick();
    expect(section.classList.contains('dim')).toBe(true);

    store.endActivity(SURFACE, true);
    await tick();
    expect(section.classList.contains('dim')).toBe(false);
  });
});
