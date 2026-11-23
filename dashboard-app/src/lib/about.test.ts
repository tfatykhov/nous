import { describe, it, expect } from 'vitest';
import * as about from './about';
import {
  ABOUT_PATH, CARDS, DB_NOT_READ, FAILED, LABELS, LINKS, LOADING, MODELS_HEADING, MODEL_ROWS, NEW_TAB,
  NOT_AVAILABLE, RELEASES_URL, SOURCE_URL, SUBTITLE, TRY_AGAIN,
  buildPieces, databaseUnread, runningSince, schemaLine,
} from './about';

/** The Build row as the screen reads it. */
const buildText = (build: about.AboutAnswer['build']) => buildPieces(build)?.map((p) => p.text).join('') ?? null;

describe('about: the words', () => {
  it('pins every sentence the owner reads', () => {
    expect(ABOUT_PATH).toBe('/dashboard/about');
    expect(SUBTITLE).toBe('What this Nous is and where it runs.');
    expect(LOADING).toBe('Loading…');
    expect(FAILED).toBe('This page could not be loaded.');
    expect(TRY_AGAIN).toBe('Try again');
    expect(NOT_AVAILABLE).toBe('Not available');
    expect(DB_NOT_READ).toBe('The database could not be read');
    expect(NEW_TAB).toBe('Opens in a new tab');
    expect(CARDS).toEqual({ nous: 'Nous', agent: 'Agent', runningOn: 'Running on', links: 'Links' });
    expect(LABELS).toEqual({
      version: 'Version', build: 'Build', runningSince: 'Running since', name: 'Name', id: 'Id', python: 'Python',
      postgres: 'PostgreSQL', pgvector: 'pgvector', schema: 'Database schema',
    });
    expect(MODELS_HEADING).toBe('Configured models');
    expect(MODEL_ROWS).toEqual([
      ['main', 'Main'], ['background', 'Background work'], ['critic', 'Critic'], ['heartbeat', 'Heartbeat'],
      ['embeddings', 'Embeddings'],
    ]);
  });

  it('links to the release notes and the source, exactly', () => {
    expect(RELEASES_URL).toBe('https://github.com/tfatykhov/nous/releases');
    expect(SOURCE_URL).toBe('https://github.com/tfatykhov/nous');
    expect(LINKS).toEqual([
      { label: 'Release notes', href: 'https://github.com/tfatykhov/nous/releases' },
      { label: 'Source code', href: 'https://github.com/tfatykhov/nous' },
    ]);
  });

  it('no constant holds a colour, an arrow or an emoji', () => {
    const text = JSON.stringify(Object.values(about).filter((v) => typeof v !== 'function'));
    expect(text).not.toMatch(/#[0-9a-f]{3,8}\b/i);
    expect(text).not.toMatch(/[\u2190-\u21FF\u25A0-\u25FF\u2600-\u27BF]|[\u{1F300}-\u{1FAFF}]/u);
  });
});

describe('about: the formatters', () => {
  it.each([
    [{ commit: '2b97aa31c0de', time: '2026-11-23T14:02:05Z' }, 'Commit 2b97aa31c0de, built Nov 23 14:02 UTC'],
    [{ commit: '2b97aa31c0de', time: null }, 'Commit 2b97aa31c0de'],
    [{ commit: null, time: '2026-11-23T14:02:05Z' }, 'Built Nov 23 14:02 UTC'],
    [{ commit: null, time: null }, null],
    [{ commit: null, time: 'not a time' }, null],
    [{ commit: '2b97aa31c0de', time: 'not a time' }, 'Commit 2b97aa31c0de'],
  ])('the build row of %j reads %j', (build, line) => {
    expect(buildText(build)).toBe(line);
  });

  it('sets the commit apart, and only the commit', () => {
    expect(buildPieces({ commit: '2b97aa31c0de', time: '2026-11-23T14:02:05Z' })).toEqual([
      { text: 'Commit ', mono: false },
      { text: '2b97aa31c0de', mono: true },
      { text: ', built Nov 23 14:02 UTC', mono: false },
    ]);
    expect(buildPieces({ commit: null, time: '2026-11-23T14:02:05Z' })).toEqual([
      { text: 'Built Nov 23 14:02 UTC', mono: false },
    ]);
    expect(buildPieces({ commit: null, time: null })).toBeNull();
  });

  it.each([
    [0, 'Nov 23 14:02 UTC, up under a minute'],
    [90061, 'Nov 23 14:02 UTC, up 25 hours'],
    [200000, 'Nov 23 14:02 UTC, up 2 days'],
  ])('running for %i seconds reads %s', (seconds, text) => {
    expect(runningSince('2026-11-23T14:02:05Z', seconds)).toBe(text);
  });

  it('names the schema by its highest migration', () => {
    expect(schemaLine({ latest: 122 })).toBe('Migration 122');
    expect(schemaLine({ latest: null })).toBe('Not available');
  });

  it('calls the database unread only when all three of its values are missing', () => {
    expect(databaseUnread({ schema: { latest: null }, database: { server: null, pgvector: null } })).toBe(true);
    expect(databaseUnread({ schema: { latest: 122 }, database: { server: '17.2', pgvector: '0.8.0' } })).toBe(false);
    expect(databaseUnread({ schema: { latest: null }, database: { server: '17.2', pgvector: null } })).toBe(false);
  });
});
