<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { Button } from '../../lib/ui/button';
  import { count, pct } from '../../lib/autonomy';
  import type { ApiAnswerError } from '../../lib/api';
  import { fmtUtc } from '../../lib/harness';
  import {
    AGAINST_AUDIT, AGAINST_FINISH, GRADER_COLUMNS, GRADER_NOTE, INTRO, LOAD_FAILED, NO_DISAGREEMENTS, NO_NOT_SUPPORTED,
    NO_REPORTS, NO_SENTENCE, NOT_SUPPORTED_HEADING, NOTHING_GRADED, NOTHING_JUDGED, PAIRS_SHOWN, SHOW_FEWER, SHOWN,
    SKIPPED_HEADING, STRIP_HINT, SWEEP_INTRO, TITLE,
    agreementLine, auditWord, betweenRate, betweenText, confidenceText, costRows, disagreementCount, disagreementLine,
    finishWord, graderRows, labelWord, legendText, notJudgedLines, otherClaimText, pairLine, pairsShownText, readJudge, reportCostText,
    reportSegments, reportsSub, scopeText, secondText, showAllText, statedConfidenceText, statusTitle, stripRows,
    sweepText, tiles, titleOrId, verdictSegments, verdictSummary,
    type JudgeData, type Segment,
  } from '../../lib/outside_judge_numbers';

  // F118: the outside judge's numbers (GET /dashboard/outside-judge), read-only. It reads the page's window and asks
  // again on the page's Refresh. With the judge off on the server (any answer whose `enabled` is not true), or a first
  // read the server refuses (403), nothing is drawn and nothing more is asked. Any other first read that fails draws the
  // title and one sentence; a later one keeps the numbers on screen and adds the sentence. Only the latest read is
  // drawn. Server text is bound as text only.

  let { days, refresh }: { days: string; refresh: number } = $props();

  let answer = $state<JudgeData | null>(null);
  let failed = $state(false);
  let loading = $state(false);
  let showAll = $state(false);
  let off = false; // a read said the judge is off: nothing more is asked
  let seq = 0; // the latest read's number: an older one's answer is dropped
  let mounted = true;
  let reading: AbortController | null = null;

  async function load() {
    const mine = ++seq;
    reading?.abort();
    const controller = new AbortController();
    reading = controller;
    loading = true;
    try {
      const next = await readJudge(days, controller.signal);
      if (!mounted || mine !== seq) return;
      failed = false;
      if (next?.enabled !== true) {
        off = true;
        answer = null;
        return;
      }
      answer = next;
    } catch (err) {
      if (!mounted || mine !== seq) return;
      if (answer === null && (err as ApiAnswerError)?.status === 403) {
        off = true;
        failed = false;
        return;
      }
      failed = true;
    } finally {
      if (mounted && mine === seq) loading = false;
    }
  }

  $effect(() => {
    void days;
    void refresh;
    untrack(() => {
      if (!off) void load();
    });
  });

  onMount(() => () => {
    mounted = false;
    reading?.abort();
  });

  const fill = (token: string) => `var(${token})`;
</script>

{#if answer || failed}
  <section class="panel" aria-labelledby="oj-title" aria-busy={loading ? 'true' : undefined}>
    <div class="panel-head">
      <h2 id="oj-title">{TITLE}</h2>
    </div>
    <p class="small muted intro">{INTRO}</p>
    {#if failed}<p class="note-err" role="alert">{LOAD_FAILED}</p>{/if}

    {#if answer}
      {@const d = answer}
      {@const segs = verdictSegments(d.decisions.by_verdict)}
      {@const graders = graderRows(d.decisions.by_grader)}
      {@const listed = showAll ? d.disagreements : d.disagreements.slice(0, SHOWN)}

      <div class="strip verdict-{d.runtime.worker_alive ? 'ok' : 'warn'}" data-testid="oj-strip">
        <div>
          <div class="verdict-title">{statusTitle(d)}</div>
          <div class="verdict-detail">{scopeText(d)}</div>
        </div>
        <dl class="strip-meta">
          {#each stripRows(d) as [label, value] (label)}<div><dt>{label}</dt><dd>{value}</dd></div>{/each}
        </dl>
      </div>
      <p class="small muted hint">{STRIP_HINT}</p>

      <dl class="tiles">
        {#each tiles(d) as t (t.label)}
          <div><dt>{t.label}</dt><dd class:words={t.words}>{t.value}</dd><dd class="small muted tile-sub">{t.sub}</dd></div>
        {/each}
      </dl>

      <h3 class="label">Verdicts</h3>
      {#if segs.length === 0}
        <p class="empty">{NOTHING_JUDGED(d.days)}</p>
      {:else}
        {@render bar(segs)}
      {/if}

      <h3 class="label">Agreement by who graded</h3>
      {#if graders.length === 0}
        <p class="empty">{NOTHING_GRADED(d.days)}</p>
      {:else}
        <div class="table-wrap table-scroll">
          <table class="stats nums">
            <caption class="small muted">{GRADER_NOTE}</caption>
            <thead><tr>{#each GRADER_COLUMNS as c (c)}<th scope="col">{c}</th>{/each}</tr></thead>
            <tbody>
              {#each graders as r (r.key)}
                <tr>
                  <th scope="row">{r.label}</th><td>{count(r.n)}</td><td>{count(r.agree)}</td><td>{count(r.harsher)}</td>
                  <td>{count(r.kinder)}</td><td>{count(r.cannotTell)}</td><td>{r.rate}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}

      <div class="sub-head">
        <h3 class="label">Where the judge disagreed</h3>
        {#if d.disagreements.length > 0}<span class="small muted">{disagreementCount(d.disagreements.length, d.days)}</span>{/if}
      </div>
      {#if d.disagreements.length === 0}
        <p class="empty">{NO_DISAGREEMENTS(d.days)}</p>
      {:else}
        <ul class="items" data-testid="oj-disagreements">
          {#each listed as r, i (`${r.subject_id}-${i}`)}
            <li class="item">
              <div class="item-title">{titleOrId(r.title, r.subject_id, 'Decision')}</div>
              <div class="small">{disagreementLine(r)}</div>
              <div class="small reason">{r.reason ?? NO_SENTENCE}</div>
              {@render meta(r.judged_at, r.confidence, r.labels)}
            </li>
          {/each}
        </ul>
        {#if d.disagreements.length > SHOWN}
          <div class="more">
            <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" onclick={() => { showAll = !showAll; }}>
              {showAll ? SHOW_FEWER : showAllText(d.disagreements.length)}
            </Button>
          </div>
        {/if}
      {/if}

      {#if d.reports}
        {@const r = d.reports}
        {@const rsegs = reportSegments(r.by_verdict)}
        <div class="sub-head">
          <h3 class="label">Task reports</h3>
          <span class="small muted">{reportsSub(r)}</span>
        </div>
        <dl class="detail"><div><dt>Reports judged</dt><dd>{count(r.judged)}</dd></div></dl>
        {#if rsegs.length === 0}
          <p class="empty">{NO_REPORTS(d.days)}</p>
        {:else}
          {@render bar(rsegs)}
        {/if}
        <p class="small line">{agreementLine(AGAINST_FINISH, r.vs_finish_check)}</p>
        <p class="small line">{agreementLine(AGAINST_AUDIT, r.vs_audit)}</p>
        <p class="small muted line">{reportCostText(r.cost)}</p>
        {#if r.not_supported.length > 0}
          <h4 class="label">{NOT_SUPPORTED_HEADING}</h4>
          <ul class="items" data-testid="oj-not-supported">
            {#each r.not_supported as n, i (`${n.subject_id}-${i}`)}
              <li class="item">
                <div class="item-title">{titleOrId(n.title, n.subject_id, 'Task')}</div>
                <div class="small reason">{n.reason ?? NO_SENTENCE}</div>
                <div class="small">{finishWord(n.finish_check)} · {auditWord(n.audit)}</div>
                {@render meta(n.judged_at, n.confidence, n.labels)}
              </li>
            {/each}
          </ul>
        {:else if r.judged > 0}
          <p class="small muted line">{NO_NOT_SUPPORTED}</p>
        {/if}
      {/if}

      <h3 class="label">{SKIPPED_HEADING}</h3>
      {#each notJudgedLines(d) as line (line)}<p class="small line">{line}</p>{/each}

      {#if d.sweep}
        <h3 class="label">The hourly pass</h3>
        {#if d.sweep.runtime}<p class="small muted line">{SWEEP_INTRO}</p>{/if}
        <p class="small line">{sweepText(d.sweep.runtime)}</p>
        <p class="small line">{statedConfidenceText(d.sweep)}</p>
      {/if}

      {#if d.second}
        <h3 class="label">The second judge</h3>
        <p class="small line">{secondText(d.second)}</p>
        {#if d.between_judges}
          {@const b = d.between_judges}
          <p class="small line">{betweenText(b)}</p>
          <p class="small line">{betweenRate(b)}</p>
          {#if b.other_claim > 0}<p class="small muted line">{otherClaimText(b.other_claim)}</p>{/if}
          {#if b.disagreements.length > 0}
            {@const more = pairsShownText(b)}
            {#if more}<p class="small muted line">{more}</p>{/if}
            <ul class="items" data-testid="oj-pairs">
              {#each b.disagreements.slice(0, PAIRS_SHOWN) as p, i (`${p.subject_id}-${i}`)}
                <li class="item">
                  <div class="item-title">{titleOrId(p.title, p.subject_id, 'Decision')}</div>
                  <div class="small">{pairLine(p)}</div>
                </li>
              {/each}
            </ul>
          {/if}
        {/if}
      {/if}

      <h3 class="label">Cost of judging decisions</h3>
      <dl class="detail">
        {#each costRows(d.cost) as [label, value] (label)}<div><dt>{label}</dt><dd>{value}</dd></div>{/each}
      </dl>
    {/if}
  </section>
{/if}

{#snippet bar(parts: Segment[])}
  <div class="stack wide" role="img" aria-label={verdictSummary(parts)}>
    {#each parts as s (s.key)}<div style:flex-grow={s.count} style:--fill={fill(s.token)}></div>{/each}
  </div>
  <ul class="legend">
    {#each parts as s (s.key)}<li><i style:--fill={fill(s.token)}></i>{legendText(s)}</li>{/each}
  </ul>
{/snippet}

{#snippet meta(at: string, confidence: number | null, labels: string[])}
  <div class="meta small muted">
    <span>{fmtUtc(at)}</span>
    {#if confidence != null}<span>{confidenceText(confidence)}</span>{/if}
    {#each labels as l, i (`${l}-${i}`)}<span class="status-pill">{labelWord(l)}</span>{/each}
  </div>
{/snippet}

<style>
  .panel { background: var(--surface); border: var(--card-border); border-radius: var(--card-radius); box-shadow: var(--card-shadow);
    padding: 1.25rem; margin-top: 1.25rem; min-width: 0; }
  .panel-head { display: flex; align-items: baseline; justify-content: space-between; gap: 0.5rem 1rem; flex-wrap: wrap; margin-bottom: 0.5rem; }
  h2 { font-size: 0.9375rem; font-weight: 600; margin: 0; }
  h3, h4 { margin: 1.25rem 0 0.5rem; }
  .intro { margin: 0 0 0.75rem; max-width: 72ch; }
  .note-err { font-size: 0.8125rem; color: var(--save-err); margin: 0 0 0.75rem; overflow-wrap: anywhere; }
  .strip { display: flex; justify-content: space-between; align-items: center; gap: 1rem 2rem; flex-wrap: wrap;
    padding: 0.875rem 1.125rem; border-radius: var(--card-radius); border: 1px solid; }
  .verdict-warn { color: var(--warn-text); border-color: color-mix(in srgb, var(--warn-text) 35%, transparent);
    background: color-mix(in srgb, var(--warn-text) 8%, transparent); }
  .verdict-ok { color: var(--ok-text); border-color: color-mix(in srgb, var(--ok-text) 35%, transparent);
    background: color-mix(in srgb, var(--ok-text) 8%, transparent); }
  .verdict-title { font-size: 0.875rem; font-weight: 700; }
  .verdict-detail { font-size: 0.8125rem; color: var(--text); margin-top: 0.125rem; max-width: 60ch; }
  .strip-meta { display: flex; gap: 0.5rem 1.5rem; flex-wrap: wrap; margin: 0; }
  .strip-meta dt, .tiles dt, .detail dt, .label { font-size: 0.6875rem; font-weight: 600; color: var(--muted);
    text-transform: none; letter-spacing: 0; }
  .strip-meta dd { margin: 0.125rem 0 0; font-size: 0.8125rem; color: var(--text); overflow-wrap: anywhere; }
  .hint { margin: 0.375rem 0 1rem; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 0.75rem; margin: 0; }
  .tiles dd { margin: 0.125rem 0 0; font-size: 1.5rem; font-weight: 700; }
  .tiles dd.words { font-size: 0.9375rem; padding: 0.3rem 0; }
  .tiles dd.tile-sub { font-size: 0.75rem; font-weight: 400; }
  .sub-head { display: flex; align-items: baseline; justify-content: space-between; gap: 0.25rem 1rem; flex-wrap: wrap; }
  .sub-head .label { margin-bottom: 0.5rem; }
  .stack { display: flex; height: 10px; border-radius: 4px; background: var(--border); overflow: hidden; }
  .stack.wide { width: 100%; }
  .stack > div { flex-basis: 0; min-width: 2px; background: var(--fill); }
  .legend { display: flex; gap: 0.25rem 1rem; flex-wrap: wrap; font-size: 0.75rem; color: var(--text); margin: 0.5rem 0 0; padding: 0;
    list-style: none; }
  .legend i { display: inline-block; width: 0.625rem; height: 0.625rem; border-radius: 2px; margin-right: 0.375rem; vertical-align: -1px;
    background: var(--fill); }
  /* contain: a wide table scrolls in its own box instead of widening the page (main is a flex item). */
  .table-wrap { overflow-x: auto; contain: inline-size; }
  .stats { width: 100%; border-collapse: collapse; font-size: 0.8125rem; }
  .stats caption { text-align: left; padding-bottom: 0.375rem; }
  .stats th, .stats td { padding: 0.375rem 0.5rem; text-align: right; border-bottom: 1px solid var(--border); white-space: nowrap; }
  .stats th:first-child, .stats td:first-child { text-align: left; }
  .stats thead th { font-size: 12px; font-weight: 600; color: var(--muted); }
  .nums td:not(:first-child) { font-stretch: 85%; }
  .items { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.5rem; }
  .item { padding: 0.625rem 0.75rem; border-radius: var(--card-radius-sm); background: var(--wash); min-width: 0; }
  .item > * + * { margin-top: 0.25rem; }
  /* Two lines at most. It wraps anywhere, because an unbreakable line would widen the page on a phone (min-content). */
  .item-title { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-clamp: 2; overflow: hidden;
    overflow-wrap: anywhere; font-size: 0.8125rem; font-weight: 600; }
  .reason { overflow-wrap: anywhere; }
  .meta { display: flex; align-items: center; gap: 0.25rem 0.75rem; flex-wrap: wrap; }
  .more { margin-top: 0.5rem; }
  .line { margin: 0.25rem 0 0; max-width: 72ch; }
  .detail { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 0.5rem 1.5rem; margin: 0; padding: 0.25rem 0; }
  .detail dd { margin: 0.125rem 0 0; font-size: 0.8125rem; overflow-wrap: anywhere; }
  .empty { color: var(--muted); font-size: 0.875rem; text-align: center; padding: 1rem 0; margin: 0; }
  .small { font-size: 0.75rem; }
  .muted { color: var(--muted); }
  @media (max-width: 640px) {
    .tiles > div:last-child:nth-child(odd) { grid-column: 1 / -1; }
    /* The grader's name stays in view while the numbers scroll: it wraps in a narrow column instead of filling the box. */
    .stats tbody th { white-space: normal; width: 8rem; min-width: 8rem; }
  }
</style>
