import { describe, it, expect, beforeEach } from 'vitest';
import { SurfaceStore } from './store.svelte';

function createSurface(surfaceId = 's1', extras: Record<string, unknown> = {}) {
  return {
    version: 'v1.0',
    createSurface: {
      surfaceId,
      catalogId: 'https://a2ui.org/specification/v1_0/catalogs/basic/catalog.json',
      components: [
        { id: 'root', component: 'Card', child: 'body' },
        { id: 'body', component: 'Text', text: 'hello' },
      ],
      dataModel: { formData: { email: '' } },
      ...extras,
    },
  };
}

describe('SurfaceStore — envelope application', () => {
  let store: SurfaceStore;
  beforeEach(() => {
    store = new SurfaceStore();
  });

  it('createSurface indexes components by id and seeds the data model', () => {
    store.apply(1, createSurface());
    const s = store.surfaces.s1;
    expect(Object.keys(s.components)).toEqual(['root', 'body']);
    expect(s.components.root.component).toBe('Card');
    expect(s.dataModel).toEqual({ formData: { email: '' } });
    expect(s.catalogId).toContain('basic/catalog.json');
  });

  it('createSurface tolerates absent components/dataModel (fixture 32 shape)', () => {
    store.apply(1, {
      version: 'v1.0',
      createSurface: { surfaceId: 'bare', catalogId: 'c', sendDataModel: true },
    } as never);
    expect(store.surfaces.bare.components).toEqual({});
    expect(store.surfaces.bare.dataModel).toEqual({});
  });

  it('extracts nonce and priority from metadata.extensions', () => {
    store.apply(
      1,
      createSurface('s1', {
        metadata: { extensions: { com_nous_nonce: 'abc123', com_nous_priority: 2 } },
      }),
    );
    expect(store.surfaces.s1.nonce).toBe('abc123');
    expect(store.surfaces.s1.priority).toBe(2);
  });

  it('defaults nonce to "" and priority to 0 when extensions are absent', () => {
    store.apply(1, createSurface());
    expect(store.surfaces.s1.nonce).toBe('');
    expect(store.surfaces.s1.priority).toBe(0);
  });

  it('updateComponents upserts by id, leaving untouched components alone', () => {
    store.apply(1, createSurface());
    store.apply(2, {
      updateComponents: {
        surfaceId: 's1',
        components: [
          { id: 'body', component: 'Text', text: 'changed' },
          { id: 'extra', component: 'Divider' },
        ],
      },
    });
    expect(store.surfaces.s1.components.body.text).toBe('changed');
    expect(store.surfaces.s1.components.extra.component).toBe('Divider');
    expect(store.surfaces.s1.components.root.component).toBe('Card');
  });

  it('ignores updates addressed to an unknown surface', () => {
    store.apply(1, { updateComponents: { surfaceId: 'ghost', components: [] } });
    store.apply(2, { updateDataModel: { surfaceId: 'ghost', path: '/a', value: 1 } });
    expect(store.surfaces.ghost).toBeUndefined();
  });

  it('updateDataModel with a path patches in place', () => {
    store.apply(1, createSurface());
    store.apply(2, { updateDataModel: { surfaceId: 's1', path: '/formData/email', value: 'a@b.co' } });
    expect(store.surfaces.s1.dataModel).toEqual({ formData: { email: 'a@b.co' } });
  });

  it('updateDataModel with a null value deletes the key', () => {
    store.apply(1, createSurface());
    store.apply(2, { updateDataModel: { surfaceId: 's1', path: '/formData/email', value: null } });
    expect(store.surfaces.s1.dataModel).toEqual({ formData: {} });
  });

  it('updateDataModel replaces the whole model when path is OMITTED (fixture 32)', () => {
    store.apply(1, createSurface());
    store.apply(2, { updateDataModel: { surfaceId: 's1', value: { now: '2025-12-15' } } });
    expect(store.surfaces.s1.dataModel).toEqual({ now: '2025-12-15' });
  });

  it('updateDataModel replaces the whole model when path is "/"', () => {
    store.apply(1, createSurface());
    store.apply(2, { updateDataModel: { surfaceId: 's1', path: '/', value: { fresh: true } } });
    expect(store.surfaces.s1.dataModel).toEqual({ fresh: true });
  });

  it('deleteSurface removes the surface', () => {
    store.apply(1, createSurface());
    store.apply(2, { deleteSurface: { surfaceId: 's1' } });
    expect(store.surfaces.s1).toBeUndefined();
  });
});

describe('SurfaceStore — seq dedupe', () => {
  let store: SurfaceStore;
  beforeEach(() => {
    store = new SurfaceStore();
  });

  it('applies the same seq twice only once (replay/live-tail overlap)', () => {
    store.apply(1, createSurface());
    const patch = { updateDataModel: { surfaceId: 's1', path: '/count', value: 5 } };
    store.apply(2, patch);
    // A redelivery of seq 2 must be a no-op even though the envelope differs.
    store.apply(2, { updateDataModel: { surfaceId: 's1', path: '/count', value: 99 } });
    expect(store.surfaces.s1.dataModel.count).toBe(5);
    expect(store.lastSeq).toBe(2);
  });

  it('drops out-of-order lower seqs', () => {
    store.apply(5, createSurface());
    store.apply(3, { deleteSurface: { surfaceId: 's1' } });
    expect(store.surfaces.s1).toBeDefined();
    expect(store.lastSeq).toBe(5);
  });

  it('seq=null always applies and never moves the watermark (snapshot hydration)', () => {
    store.apply(7, createSurface());
    store.apply(null, createSurface('s2'));
    expect(store.surfaces.s2).toBeDefined();
    expect(store.lastSeq).toBe(7);
  });
});

describe('SurfaceStore — pruneAbsent / reset / ordered', () => {
  let store: SurfaceStore;
  beforeEach(() => {
    store = new SurfaceStore();
  });

  it('pruneAbsent drops local surfaces the live index no longer lists', () => {
    store.apply(null, createSurface('keep'));
    store.apply(null, createSurface('zombie'));
    store.pruneAbsent(new Set(['keep']));
    expect(store.surfaces.keep).toBeDefined();
    expect(store.surfaces.zombie).toBeUndefined();
  });

  it('pruneAbsent with an empty index clears everything', () => {
    store.apply(null, createSurface('a'));
    store.pruneAbsent(new Set());
    expect(Object.keys(store.surfaces)).toEqual([]);
  });

  it('reset clears surfaces and the seq watermark', () => {
    store.apply(9, createSurface());
    store.reset();
    expect(Object.keys(store.surfaces)).toEqual([]);
    expect(store.lastSeq).toBe(0);
  });

  it('ordered() sorts by priority descending', () => {
    store.apply(null, createSurface('low', { metadata: { extensions: { com_nous_priority: 0 } } }));
    store.apply(null, createSurface('high', { metadata: { extensions: { com_nous_priority: 2 } } }));
    store.apply(null, createSurface('mid', { metadata: { extensions: { com_nous_priority: 1 } } }));
    expect(store.ordered().map((s) => s.surfaceId)).toEqual(['high', 'mid', 'low']);
  });
});
