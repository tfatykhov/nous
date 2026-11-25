import { describe, it, expect } from 'vitest';
import {
  EMPTY_TEXT, LABELS, STATUS_WORDS, WRITER_OFF_TEXT, factPath, filterOptions, linePath, listPath, nameText,
  nextWriteAt, notEntityPath, profilePath, rebuildText, removedText, restoreLinePath, restorePath, statusText,
  verdictText, writtenText,
  type ListStatus,
} from './entity_profiles';

// F119 PR 2: the words and the paths of Browse's Profiles tab. Every value below is made up.

const T = '2026-11-24T10:00:00+00:00';
const COUNTS: Record<ListStatus, number> = { profile: 12, not_entity: 3, owner: 2, asked: 1, failed: 1, removed: 4 };

describe('paths', () => {
  it('the list sends the page, and the search and the status only when set', () => {
    expect(listPath({})).toBe('/entity-profiles?limit=20&offset=0');
    expect(listPath({ q: 'quil lon', status: 'removed', offset: 40 })).toBe(
      '/entity-profiles?limit=20&offset=40&q=quil+lon&status=removed',
    );
    expect(listPath({ q: '', status: '' })).toBe('/entity-profiles?limit=20&offset=0');
  });

  it('a key or a line id is one encoded path segment', () => {
    expect(profilePath('quillon marsh')).toBe('/entity-profiles/quillon%20marsh');
    expect(restorePath('a/b')).toBe('/entity-profiles/a%2Fb/restore');
    expect(notEntityPath('x')).toBe('/entity-profiles/x/not-entity');
    expect(linePath('x', 'ab12')).toBe('/entity-profiles/x/lines/ab12');
    expect(restoreLinePath('x', 'ab12')).toBe('/entity-profiles/x/lines/ab12/restore');
  });

  it("a fact's text is read through the graph's node route", () => {
    expect(factPath('0a1b')).toBe('/dashboard/graph/node/0a1b?type=fact');
  });
});

describe('words', () => {
  it('each status in the brief\'s plain words', () => {
    expect(STATUS_WORDS).toEqual({
      profile: 'Profile', not_entity: 'Not an entity', owner: 'You', asked: 'Waiting for a reply',
      failed: 'Could not be written', removed: 'Removed',
    });
  });

  it('the filter: All counts every row not removed; each status its own; no counts before the first read', () => {
    expect(filterOptions(COUNTS).map((o) => [o.value, o.label])).toEqual([
      ['', 'All (19)'], ['profile', 'Profile (12)'], ['not_entity', 'Not an entity (3)'], ['owner', 'You (2)'],
      ['asked', 'Waiting for a reply (1)'], ['failed', 'Could not be written (1)'], ['removed', 'Removed (4)'],
    ]);
    expect(filterOptions(null).map((o) => o.label)).toEqual([
      'All', 'Profile', 'Not an entity', 'You', 'Waiting for a reply', 'Could not be written', 'Removed',
    ]);
  });

  it("a row's status: removed first, then the owner's verdict, then the writer's", () => {
    const verdict = { verdict: 'not_entity' as const, by: 'proxy', at: T };
    expect(statusText({ status: 'profile', active: false, owner_verdict: verdict })).toBe('Removed');
    expect(statusText({ status: 'profile', active: true, owner_verdict: verdict })).toBe('Not an entity, you said so');
    expect(statusText({ status: 'asked', active: true, owner_verdict: null })).toBe('Waiting for a reply');
  });

  it('the name is the name the facts write, else the key', () => {
    expect(nameText({ key: 'quillon', display_name: 'Quillon Marsh' })).toBe('Quillon Marsh');
    expect(nameText({ key: 'quillon', display_name: null })).toBe('quillon');
  });

  it('when and from how many facts a profile was written', () => {
    expect(writtenText({ written_at: T, source_count: 40, fact_count: 320, status: 'profile' })).toBe(
      'Written Nov 24 10:00 UTC from the newest 40 of its 320 facts.',
    );
    expect(writtenText({ written_at: T, source_count: 12, fact_count: 12, status: 'profile' })).toBe(
      'Written Nov 24 10:00 UTC from its 12 facts.',
    );
    expect(writtenText({ written_at: T, source_count: 12, fact_count: 12, status: 'not_entity' })).toBe(
      'Last asked Nov 24 10:00 UTC.',
    );
    // An owner row is stored with no model call.
    expect(writtenText({ written_at: T, source_count: 0, fact_count: 12, status: 'owner' })).toBe('Stored Nov 24 10:00 UTC.');
  });

  it('who and when, for a verdict and a removed line', () => {
    expect(verdictText({ verdict: 'not_entity', by: 'proxy', at: T })).toBe(
      'You said this is not an entity from the dashboard, Nov 24 10:00 UTC. Nous neither writes nor asks about it until you take that back.',
    );
    expect(removedText({ removed_by: 'owner', removed_at: T })).toBe(
      "Removed with the owner's secret, Nov 24 10:00 UTC. No later write brings back this line, or one citing exactly the same facts.",
    );
    expect(removedText({ removed_by: null, removed_at: T })).toMatch(/^Removed, Nov 24 10:00 UTC\./);
  });

  it('what one "Write it again now" did, and an unknown outcome as it came', () => {
    expect(rebuildText('written')).toBe('Written again now.');
    expect(rebuildText('owner_not_entity')).toBe('You said this is not an entity. Take that back first.');
    expect(rebuildText('inactive')).toBe('This profile is removed. Restore it first.');
    expect(rebuildText('something_new')).toBe('Done (something_new).');
    expect(rebuildText('held', '2026-11-25T09:00:00+00:00')).toBe(
      "Not written: it was written in the last 24 hours, and the day's limit has no exception. It can be written again after Nov 25 09:00 UTC.",
    );
    expect(rebuildText('skipped')).toBe('Nothing was written: the profile was removed, or called not an entity, while it was being written.');
    expect(rebuildText('all_rejected')).toBe(
      'Every line Nous wrote is one you removed, so the profile keeps only its own lines that are still live. It is written again when its facts change.',
    );
  });

  it('a profile written with a call in the last 24 hours can be written again only once they have passed', () => {
    const now = Date.parse('2026-11-24T12:00:00Z');
    expect(nextWriteAt({ written_at: '2026-11-24T10:00:00+00:00', status: 'profile' }, now)).toBe('2026-11-25T10:00:00.000Z');
    expect(nextWriteAt({ written_at: '2026-11-23T11:59:00+00:00', status: 'not_entity' }, now)).toBeNull();
    // An owner row stands for no call.
    expect(nextWriteAt({ written_at: '2026-11-24T10:00:00+00:00', status: 'owner' }, now)).toBeNull();
    expect(nextWriteAt({ written_at: null, status: 'asked' }, now)).toBeNull();
    expect(LABELS.rebuildAfter('2026-11-25T10:00:00.000Z')).toBe('Write it again after Nov 25 10:00 UTC');
  });

  it('the two sentences the brief asks for: writer off, and no profiles yet', () => {
    expect(WRITER_OFF_TEXT).toContain('NOUS_ENTITY_PROFILE_WRITER_ENABLED=false');
    expect(EMPTY_TEXT).toBe('No profiles yet. They are written while Nous sleeps.');
  });
});
