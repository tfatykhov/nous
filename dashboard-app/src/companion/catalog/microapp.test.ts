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
    store.beginActivity(SURFACE, 'refine', 'Just the blockers');
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
