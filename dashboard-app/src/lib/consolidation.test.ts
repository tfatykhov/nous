import { describe, it, expect } from 'vitest';
import {
  MEASURES, PREVIEW_CHARS, barReadout, byDay, defaultOpen, factChanges, factSentence, fmtMinutes, graphSentence, reportSections,
  sleepChart, sleepMinutes, sleepNumbers, sleepRows, sleepState, upkeepCounts,
} from './consolidation';
import type { ConsolidationAction, ConsolidationCycle } from './types/api';

// Every value below is made up. The totals are in the shape the sleep handler writes: counters beside whole-graph
// snapshots, nested objects, a boolean and `_attempted` (the sum of all of them).
const TOTALS = {
  facts_created: 4, clusters_merged: 4, stale_deactivated: 15, stale_examined: 3988, contradictions_found: 1,
  contradictions_resolved: 0, procedures_created: 0, censors_retired: 0, rubric_evolved: true,
  orphan_edges_created: 539, temporal_chain_edges: 1, comention_edges: 18, cooccurrence_edges: 67, bridge_edges_created: 0,
  dead_edges_pruned: 200, dead_edges_missing_endpoint: 0, comention_edges_deleted: 0, comention_edges_reweighted: 2000,
  f044_promoted: 24, f044_n_edges: 144750, f044_n_tagged: 125262, _attempted: 381291,
  tool_notes: { written: 1, detected: 10 }, profile_card: { outcome: 'unchanged', entries: 20 },
};
const cycle = (id: string, over: Partial<ConsolidationCycle> = {}): ConsolidationCycle => ({
  cycle_id: id, trace_id: `t-${id}`, started_at: '2026-11-13T11:55:00+00:00', finished_at: '2026-11-13T12:12:00+00:00',
  status: 'completed', phases_run: ['review', 'reflect'], totals: TOTALS, action_count: 24, ...over,
});
const action = (over: Partial<ConsolidationAction>): ConsolidationAction => ({
  action_id: 'a', phase: 'reflect', op: 'learn', target_ids: [], before: null, after: null, rationale: null,
  created_at: null, ...over,
});

describe('a sleep in numbers and words', () => {
  it('reads each number by name: a snapshot, a nested object and _attempted never reach a count', () => {
    expect(sleepNumbers(cycle('c'))).toEqual({
      newFacts: 4, merges: 4, retired: 15, contradictionsResolved: 0, keySupersessions: 0, procedures: 0, censorsRetired: 0,
      linksAdded: 625, linksPruned: 200,
    });
  });

  it('a missing counter, a string or a boolean in a counter\'s place counts as 0', () => {
    const n = sleepNumbers(cycle('c', { totals: { facts_created: '7', clusters_merged: true, stale_deactivated: null } }));
    expect([n.newFacts, n.merges, n.retired, n.linksAdded]).toEqual([0, 0, 0, 0]);
    expect(sleepNumbers(cycle('c', { totals: {} })).linksPruned).toBe(0);
  });

  it('says the fact changes in one line, leaves out what did not happen, and says so when nothing did', () => {
    expect(factSentence(sleepNumbers(cycle('c')))).toBe('4 new facts · 4 merges · 15 retired as stale');
    expect(factSentence(sleepNumbers(cycle('c', { totals: { facts_created: 1, clusters_merged: 1, procedures_created: 1, censors_retired: 2, contradictions_resolved: 1 } }))))
      .toBe('1 new fact · 1 merge · 1 contradiction resolved · 1 new procedure · 2 censors retired');
    expect(factSentence(sleepNumbers(cycle('c', { totals: {} })))).toBe('No fact changes');
  });

  // Codex round 1 on #922: the key-conflict sweep supersedes facts and counts them only in key_supersessions_written
  // (it writes no change row), so a sleep whose only fact change was that read "No fact changes".
  it('a fact superseded by the key sweep is a fact change, in the sentence and in the count of changes', () => {
    const n = sleepNumbers(cycle('c', { totals: { key_conflicts_found: 25, key_supersessions_written: 2 } }));
    expect(n.keySupersessions).toBe(2);
    expect(factSentence(n)).toBe('2 superseded by key');
    expect(factChanges(n)).toBe(2);
    expect(factChanges(sleepNumbers(cycle('c', { totals: { key_conflicts_found: 25, stale_examined: 3988, orphan_edges_created: 9 } })))).toBe(0);
    expect(factChanges(sleepNumbers(cycle('c')))).toBe(23);
  });

  it('says the graph changes with thousands separators, and nothing when the graph was left alone', () => {
    expect(graphSentence(sleepNumbers(cycle('c')))).toBe('625 links added · 200 pruned');
    expect(graphSentence(sleepNumbers(cycle('c', { totals: { orphan_edges_created: 1492 } })))).toBe('1,492 links added');
    expect(graphSentence(sleepNumbers(cycle('c', { totals: {} })))).toBe('');
  });
});

describe('a sleep\'s state and duration', () => {
  it('a sleep with no finish is running while it is the newest, and unfinished once a later sleep started', () => {
    const open = cycle('c', { status: 'running', finished_at: null });
    expect(sleepState(open, false)).toBe('running');
    expect(sleepState(open, true)).toBe('unfinished');
    expect(sleepState(cycle('c'), true)).toBe('finished');
    expect(sleepState(cycle('c', { status: 'failed' }), false)).toBe('failed');
  });

  it('the duration is start to finish; none when an end is missing', () => {
    expect(sleepMinutes(cycle('c'))).toBe(17);
    expect(sleepMinutes(cycle('c', { finished_at: null }))).toBeNull();
    expect(sleepMinutes(cycle('c', { started_at: 'nope' }))).toBeNull();
    expect([fmtMinutes(0.2), fmtMinutes(16.6), fmtMinutes(65), fmtMinutes(null)]).toEqual(['under 1 min', '17 min', '1 h 05 min', '']);
  });

  it('rows keep the endpoint\'s order (newest first); only the newest can be running', () => {
    const rows = sleepRows([
      cycle('new', { status: 'running', finished_at: null, started_at: '2026-11-13T21:38:00+00:00' }),
      cycle('mid'),
      cycle('old', { status: 'running', finished_at: null, started_at: '2026-11-12T03:00:00+00:00' }),
    ]);
    expect(rows.map((r) => r.state)).toEqual(['running', 'finished', 'unfinished']);
    expect(defaultOpen(rows)).toBe('mid'); // the newest finished sleep, not the running one
    expect(defaultOpen(rows.slice(0, 1))).toBeNull();
  });

  it('groups rows under their UTC day', () => {
    const rows = sleepRows([
      cycle('a', { started_at: '2026-11-13T11:55:00+00:00' }),
      cycle('b', { started_at: '2026-11-13T00:10:00+00:00' }),
      cycle('c', { started_at: '2026-11-12T23:50:00+00:00' }),
      cycle('d', { started_at: null }),
    ]);
    expect(byDay(rows).map((g) => [g.day, g.rows.map((r) => r.cycle.cycle_id)])).toEqual([
      ['13 November', ['a', 'b']], ['12 November', ['c']], ['No start time recorded', ['d']],
    ]);
  });
});

describe('the chart', () => {
  const rows = sleepRows(Array.from({ length: 14 }, (_, i) =>
    cycle(`c${i}`, { started_at: `2026-11-${String(14 - i).padStart(2, '0')}T04:00:00+00:00`, finished_at: `2026-11-${String(14 - i).padStart(2, '0')}T04:${10 + i}:00+00:00`,
      totals: { facts_created: i, stale_deactivated: 2 * i, orphan_edges_created: 100 * i, dead_edges_pruned: 10 } })));

  it('draws the oldest sleep first, with what was gained above the line and what was removed below it', () => {
    const facts = sleepChart(rows, MEASURES[0]);
    expect(facts.bars.map((b) => b.id)[0]).toBe('c13');
    expect(facts.bars.at(-1)).toMatchObject({ id: 'c0', up: 0, down: 0 });
    expect([facts.maxUp, facts.maxDown]).toEqual([13, 26]);
    const graph = sleepChart(rows, MEASURES[1]);
    expect([graph.maxUp, graph.maxDown]).toEqual([1300, 10]);
    expect(barReadout(graph.bars[0], MEASURES[1])).toBe('Links added 1,300 · Links pruned 10');
  });

  it('the duration has no lower half', () => {
    const d = sleepChart(rows, MEASURES[2]);
    expect(d.maxDown).toBe(0);
    expect(d.bars[0]).toMatchObject({ up: 23, down: 0 });
    expect(barReadout(d.bars[0], MEASURES[2])).toBe('Minutes 23');
  });

  it('labels a day under its first sleep only where the label has room to its right and from the label before', () => {
    // 14 sleeps, one a day: a label needs 16% of the plot (2.24 bars) from the one before and to the right edge.
    const ticks = sleepChart(rows, MEASURES[0]).bars.map((b) => b.tick);
    expect(ticks).toEqual(['Nov 1', '', '', 'Nov 4', '', '', 'Nov 7', '', '', 'Nov 10', '', '', '', '']);
    const sameDay = sleepRows([cycle('x'), cycle('y')]);
    expect(sleepChart(sameDay, MEASURES[0]).bars.map((b) => b.tick)).toEqual(['Nov 13', '']);
    // 30 sleeps, three a day: every second day is labelled, and the last day (no room before the edge) is not.
    const thirty = sleepRows(Array.from({ length: 30 }, (_, i) =>
      cycle(`t${i}`, { started_at: `2026-11-${String(10 - Math.floor(i / 3)).padStart(2, '0')}T0${8 - 2 * (i % 3)}:00:00+00:00` })));
    expect(sleepChart(thirty, MEASURES[0]).bars.map((b) => b.tick).filter(Boolean)).toEqual(['Nov 1', 'Nov 3', 'Nov 5', 'Nov 7', 'Nov 9']);
  });

  it('an empty list has no bars and no scale', () => {
    expect(sleepChart([], MEASURES[0])).toEqual({ bars: [], maxUp: 0, maxDown: 0 });
  });
});

describe('the report of one sleep', () => {
  const LONG = 'w'.repeat(PREVIEW_CHARS);
  const ACTIONS: ConsolidationAction[] = [
    action({ action_id: 's1', phase: 'stale_scan', op: 'deactivate', target_ids: ['11111111-aaaa'], before: { subject: 'old plan', content_preview: 'the March plan' }, rationale: 'stale: aged > 60d, no recall in window' }),
    action({ action_id: 's2', phase: 'stale_scan', op: 'deactivate', before: { subject: 'old route', content_preview: LONG }, rationale: 'stale: aged > 60d, no recall in window' }),
    action({ action_id: 'l1', after: { subject: 'kettle', content_preview: 'the kettle is blue' }, rationale: 'sleep_reflection fact' }),
    action({ action_id: 'u1', op: 'supersede', before: { content_preview: 'the kettle is red' }, after: { content_preview: 'the kettle is blue' }, rationale: 'UPDATES: prefix (score 0.91)' }),
    action({ action_id: 'm1', phase: 'f027_consolidate', op: 'merge', target_ids: ['a', 'b', 'c'],
      before: [{ id: 'a', content_preview: 'uses Postgres' }, { id: 'b', content_preview: 'uses pgvector' }],
      after: { id: 'c', content_preview: 'uses Postgres with pgvector' }, rationale: "cluster merge of 2 facts on 'storage'" }),
    action({ action_id: 'x1', phase: 'f031_contradiction', op: 'supersede', target_ids: ['22222222-bbbb', '33333333-cccc'], before: [{ id: 'p' }, { id: 'q' }], rationale: 'SUPERSEDE_A (conf 0.90): the newer one is dated' }),
    action({ action_id: 'g1', phase: 'graph_densify', op: 'edge_add', after: { counts: { orphan_edges_created: 539 } }, rationale: 'graph_densify phase summary' }),
    action({ action_id: 'o1', phase: 'new_phase', op: 'reshape', before: { anything: 1 }, after: 'a string', rationale: 'a later feature' }),
  ];
  const sections = reportSections(ACTIONS);
  const get = (key: string) => sections.find((s) => s.key === key)!;

  it('puts the sections in reading order whatever order the rows came in, and leaves phase summary rows to the totals', () => {
    expect(sections.map((s) => [s.title, s.items.length])).toEqual([
      ['Learned', 1], ['Updated', 1], ['Merged', 1], ['Contradictions resolved', 1], ['Retired as stale', 2], ['Other changes', 1],
    ]);
    expect(sections.flatMap((s) => s.items.map((i) => i.id))).not.toContain('g1');
    expect(get('merged').source).toBe('f027_consolidate · merge');
  });

  it('a learned fact reads as its subject and text, with the rationale as recorded', () => {
    expect(get('learned').items[0]).toMatchObject({ title: 'kettle', text: 'the kettle is blue', earlier: [], why: 'sleep_reflection fact', raw: null });
  });

  it('an update keeps the old text beside the new; a merge keeps every source', () => {
    expect(get('updated').items[0]).toMatchObject({ text: 'the kettle is blue', earlier: ['the kettle is red'] });
    expect(get('updated').earlierLabel).toBe('Was');
    expect(get('merged').items[0]).toMatchObject({ title: '2 facts became one', text: 'uses Postgres with pgvector', earlier: ['uses Postgres', 'uses pgvector'], ids: ['a', 'b', 'c'] });
  });

  it('a contradiction says what the record keeps (ids, the rationale) and that it keeps no text', () => {
    const s = get('contradictions');
    expect(s.note).toBe('The audit keeps the ids of the two facts, not their text.');
    expect(s.items[0]).toMatchObject({ title: 'One fact superseded the other', text: null, ids: ['22222222', '33333333'], why: 'SUPERSEDE_A (conf 0.90): the newer one is dated', raw: null });
  });

  it('a rationale every row shares is said once under the heading; a cut text is marked and flagged', () => {
    const s = get('retired');
    expect(s.note).toBe('stale: aged > 60d, no recall in window');
    expect(s.items.map((i) => i.why)).toEqual([null, null]);
    expect(s.items[0]).toMatchObject({ title: 'old plan', text: 'the March plan', ids: ['11111111'] });
    expect(s.items[1].text).toBe(`${LONG}…`);
    expect(s.cut).toBe(true);
    expect(get('learned').cut).toBe(false);
  });

  it('a row in an unknown shape keeps its recorded before and after', () => {
    expect(get('other').items[0]).toMatchObject({ title: 'new_phase · reshape', why: 'a later feature', raw: { before: { anything: 1 }, after: 'a string' } });
    // A known phase whose row lost its text is not dropped either.
    const odd = reportSections([action({ action_id: 'l2', after: { subject: 'no text here' } })]);
    expect(odd.map((s) => s.key)).toEqual(['other']);
    expect(odd[0].items[0].raw).toEqual({ before: null, after: { subject: 'no text here' } });
  });

  it('no rows, no sections', () => {
    expect(reportSections([])).toEqual([]);
  });
});

describe('upkeep counts', () => {
  it('lists the non-zero counters by name with their plain words; a snapshot is never one of them', () => {
    const { graph, housekeeping } = upkeepCounts(cycle('c'));
    expect(graph.map((r) => [r.key, r.value])).toEqual([
      ['orphan_edges_created', '539'], ['temporal_chain_edges', '1'], ['comention_edges', '18'], ['cooccurrence_edges', '67'],
      ['dead_edges_pruned', '200'], ['comention_edges_reweighted', '2,000'], ['f044_promoted', '24'],
    ]);
    expect(graph[0].label).toBe('Links for memories that had none');
    expect(housekeeping.map((r) => [r.key, r.value])).toEqual([
      ['stale_examined', '3,988'], ['contradictions_found', '1'], ['rubric_evolved', 'yes'],
    ]);
    expect([...graph, ...housekeeping].map((r) => r.key)).not.toContain('f044_n_edges');
  });

  it('a sleep that recorded nothing has two empty lists', () => {
    expect(upkeepCounts(cycle('c', { totals: {} }))).toEqual({ graph: [], housekeeping: [] });
  });

  // Codex round 1 on #922: the totals are written when a sleep ends (and that write can fail), while each phase writes
  // its summary row as it ends. A sleep with no totals showed neither, and said nothing was recorded.
  const summary = (id: string, phase: string, counts: Record<string, unknown>) =>
    action({ action_id: id, phase, op: 'edge_add', after: { counts }, rationale: `${phase} phase summary` });
  const SUMMARIES = [
    summary('g1', 'graph_densify', { orphan_edges_created: 539, comention_edges: 18 }),
    summary('g2', 'graph_densify', { comention_edges_deleted: 149, comention_edges_reweighted: 1851 }),
    summary('g3', 'graph_densify', { comention_edges_reweighted: 149 }),
    summary('r1', 'evolve_rubric', { rubric_evolved: true }),
    summary('n1', 'later_phase', { later_things_done: 3, later_flag: true, later_text: 'ignored' }),
  ];

  it('with no totals, the counts come from the phase summary rows, summed by counter', () => {
    const { graph, housekeeping } = upkeepCounts(cycle('c', { status: 'running', finished_at: null, totals: {} }), SUMMARIES);
    expect(graph.map((r) => [r.key, r.value])).toEqual([
      ['orphan_edges_created', '539'], ['comention_edges', '18'], ['comention_edges_deleted', '149'], ['comention_edges_reweighted', '2,000'],
    ]);
    // A counter no list names is shown under its own name, after the named ones; a value that is no count is left out.
    expect(housekeeping.map((r) => [r.key, r.label, r.value])).toEqual([
      ['rubric_evolved', 'Rubric evolved', 'yes'], ['later_things_done', 'later things done', '3'], ['later_flag', 'later flag', 'yes'],
    ]);
  });

  it('where the totals have a counter they win over the summary rows; the rows fill in only what the totals lack', () => {
    const { graph, housekeeping } = upkeepCounts(cycle('c', { totals: { orphan_edges_created: 7, stale_examined: 10, f044_n_edges: 144750 } }), SUMMARIES);
    expect(graph.map((r) => [r.key, r.value])).toEqual([
      ['orphan_edges_created', '7'], ['comention_edges', '18'], ['comention_edges_deleted', '149'], ['comention_edges_reweighted', '2,000'],
    ]);
    expect(housekeeping.map((r) => r.key)).toEqual(['stale_examined', 'rubric_evolved', 'later_things_done', 'later_flag']);
  });
});
