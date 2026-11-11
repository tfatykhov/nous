<script lang="ts">
  import { apiGet, type ApiError } from '../../lib/api';
  import { makePollStore } from '../../lib/stores/registry';
  import { usePoll } from '../../lib/poll';
  import { routeHref } from '../../lib/router';
  import { fmtDuration, fmtTime, fmtTokens, getLastListQuery, laneVar } from '../../lib/work';
  import { chosenBy, clockTime, criticVerdict, fmtSeconds, frameChange, frameLabel, frameVar, replySeconds, verdictText } from '../../lib/frames';
  import type { WorkCall, WorkTrace, WorkTurn } from '../../lib/types/api';
  import StaleBadge from '../../lib/ui/StaleBadge.svelte';
  import HoldUndo from '../../lib/ui/HoldUndo.svelte';
  import { Button } from '../../lib/ui/button';
  import FullResult from './FullResult.svelte';

  // F108 Work trace: one session, turn by turn. Every string here (messages, reasons, inputs, results, replies,
  // critic rationale, frame ids and reasons) came from a model or a user and is shown as a text node only: no
  // raw-HTML directive, no markdown. Turns come oldest first, a page at a time. While more pages remain nothing is polled
  // (loaded history does not change); once every turn is loaded, each 15 s poll asks for the turns after the
  // second-to-last one, so the newest turn is refreshed and new turns appear. Results merge by turn id.
  // Redesign: each turn has a frame column tinted in its frame's colour (time, frame, why it was chosen, its numbers
  // and the critic's verdict), a sentence says where the frame changed and why, and a replay control steps through
  // the loaded turns.
  let { sessionId, turnId }: { sessionId: string; turnId?: string } = $props();

  const base = () => `/dashboard/work/sessions/${encodeURIComponent(sessionId)}`;
  let turns = $state<WorkTurn[]>([]);
  let nextCursor = $state<string | null>(null);
  let head = $state<WorkTrace | null>(null);

  /** The endpoint's own cursor format, `<started_at>|<turn id>` (format_cursor), for the turns after `t`. */
  const cursorOf = (t: WorkTurn) => `${t.started_at}|${t.turn_id}`;

  function absorb(r: WorkTrace) {
    head = r;
    nextCursor = r.next_after;
    const next = [...turns];
    for (const t of r.turns) {
      const i = next.findIndex((x) => x.turn_id === t.turn_id);
      if (i >= 0) next[i] = t;
      else next.push(t);
    }
    turns = next;
  }

  // A 4xx (the session is gone) ends the polling: later ticks rethrow it without a request.
  let gone: Error | null = null;
  const poll = makePollStore<WorkTrace>(async (signal) => {
    if (gone) throw gone;
    if (head && nextCursor !== null) return head; // more pages remain: loaded history does not change
    const anchor = turns.length >= 2 ? turns[turns.length - 2] : null;
    const url = anchor ? `${base()}?${new URLSearchParams({ after: cursorOf(anchor) })}` : base();
    try {
      const r = await apiGet<WorkTrace>(url, { signal });
      absorb(r);
      return r;
    } catch (e) {
      const status = (e as ApiError).status;
      if (status !== undefined && status >= 400 && status < 500) gone = e as Error;
      throw e;
    }
  }, 15000);
  const store = usePoll(poll);

  const KIND_LABEL: Record<string, string> = { conversation: 'Conversation', task: 'Task', background: 'Background' };
  const LANE_NAME: Record<string, string> = { memory: 'Memory', live: 'Live state', workspace: 'Workspace', acts: 'Acts' };
  const laneName = (lane: string) => (Object.hasOwn(LANE_NAME, lane) ? LANE_NAME[lane] : 'Other');

  let moreBusy = $state(false);
  let moreError = $state(false);

  let data = $derived(head);
  let notFound = $derived(($store.error as ApiError | null)?.status === 404 && !head);

  async function loadMore() {
    if (nextCursor === null || moreBusy) return;
    moreBusy = true;
    moreError = false;
    try {
      absorb(await apiGet<WorkTrace>(`${base()}?${new URLSearchParams({ after: nextCursor })}`));
    } catch {
      moreError = true;
    } finally {
      moreBusy = false;
    }
  }

  // Reload every loaded turn from the start (a send settled elsewhere changes an older turn's ledger status, which
  // the poll never refetches). Asks for as many turns as are loaded, a page at a time, and merges them by id.
  let refreshing = $state(false);
  async function refresh() {
    if (refreshing || moreBusy) return;
    refreshing = true;
    moreError = false;
    try {
      const want = Math.max(turns.length, 1);
      let got = 0;
      let after: string | null = null;
      do {
        const q = new URLSearchParams({ limit: String(Math.min(200, want - got)) });
        if (after !== null) q.set('after', after);
        const r = await apiGet<WorkTrace>(`${base()}?${q}`);
        absorb(r);
        got += r.turns.length;
        after = r.next_after;
      } while (after !== null && got < want);
    } catch {
      moreError = true;
    } finally {
      refreshing = false;
    }
  }

  // A linked turn on a page not loaded yet: load the next pages until it shows, at most 10 of them.
  let autoPages = 0;
  let gaveUp = $state(false);
  $effect(() => {
    if (!turnId || !head || moreBusy || moreError || nextCursor === null) return;
    if (turns.some((t) => t.turn_id === turnId)) return;
    if (autoPages >= 10) { gaveUp = true; return; }
    autoPages += 1;
    void loadMore();
  });
  let turnMissing = $derived(!!turnId && !!head && !turns.some((t) => t.turn_id === turnId) && (nextCursor === null || gaveUp));

  // Scroll to the linked turn once, when it is first on screen.
  let scrolledTo = '';
  $effect(() => {
    if (!turnId || scrolledTo === turnId || !turns.some((t) => t.turn_id === turnId)) return;
    const el = document.getElementById(`turn-${turnId}`);
    if (!el) return;
    scrolledTo = turnId;
    el.scrollIntoView({ block: 'start' });
  });

  // Replay: one step per loaded turn (index-based, not time-based: a session can span days). Nothing is marked
  // until the owner moves it. It scrolls only from the slider itself, never from a poll or an effect.
  let replay = $state<number | null>(null);
  let replayAt = $derived(turns.length === 0 ? 0 : Math.min(replay ?? 0, turns.length - 1));
  const reducedMotion = () => typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  function onReplay(e: Event) {
    const i = Number((e.currentTarget as HTMLInputElement).value);
    if (!Number.isInteger(i) || i < 0 || i >= turns.length) return;
    replay = i;
    const el = document.getElementById(`turn-${turns[i].turn_id}`);
    el?.scrollIntoView?.({ block: 'nearest', behavior: reducedMotion() ? 'auto' : 'smooth' });
  }

  const turnLabel = (t: WorkTurn, i: number) => (t.has_turn_row ? `Turn ${t.turn_number ?? i + 1}` : 'Turn record missing (single call)');

  function plural(n: number, one: string, many: string) {
    return `${n} ${n === 1 ? one : many}`;
  }

  function contextChips(t: WorkTurn): string[] {
    const m = t.memory;
    if (!m) return [];
    const out: string[] = [];
    if (m.fact.length) out.push(plural(m.fact.length, 'fact', 'facts'));
    if (m.episode.length) out.push(plural(m.episode.length, 'past conversation', 'past conversations'));
    if (m.procedure.length) out.push(plural(m.procedure.length, 'procedure', 'procedures'));
    if (m.decision.length) out.push(plural(m.decision.length, 'decision', 'decisions'));
    return out;
  }

  function claimLine(t: WorkTurn): string {
    const c = t.claim_check;
    if (!c) return '';
    if ((c.violation_count ?? 0) > 0) return `Claim check found ${plural(c.violation_count!, 'problem', 'problems')}`;
    if (c.verified && (c.claim_count ?? 0) > 0) return 'Claim check passed';
    return '';
  }

  // The runner writes ran, error, refused, gated, suppressed, retired and bad_input (migration 100). Only ran and
  // error are ordinary (an error shows its text); every other outcome gets a badge, with who refused when known.
  function badge(c: WorkCall): string {
    const ordinary = c.outcome === 'ran' || c.outcome === 'error';
    if (c.refused_by) return `${ordinary ? 'refused' : c.outcome} by ${c.refused_by}`;
    return ordinary ? '' : c.outcome;
  }

  // One entry per tool, first-seen order: how many calls and the reason given in each turn.
  let used = $derived.by(() => {
    const by = new Map<string, { lane: string; calls: number; why: string[] }>();
    turns.forEach((t, i) => {
      for (const c of t.calls) {
        const u = by.get(c.tool) ?? { lane: c.lane, calls: 0, why: [] };
        u.calls += 1;
        if (c.reason) u.why.push(`${t.has_turn_row ? `Turn ${t.turn_number ?? i + 1}` : 'Turn record missing'}: ${c.reason}`);
        by.set(c.tool, u);
      }
    });
    return [...by].map(([tool, u]) => ({ tool, ...u }));
  });

  // The frames among the loaded turns, first-seen order: the key above the turns.
  let frames = $derived([...new Set(turns.map((t) => t.frame_id).filter((f): f is string => f != null && f !== ''))]);

  const modelCallsHref = (sub: string[] | undefined, query?: Record<string, string>) => routeHref('model-calls', sub, { ...query, days: '30' }); // the Work log keeps 30 days; the list defaults to 7
  const num = (n: number) => n.toLocaleString('en-US');
  const errorsText = (n: number) => (n === 0 ? 'No errors' : plural(n, 'error', 'errors'));
</script>

<div class="trace">
  <a class="back" href={routeHref('work', '', getLastListQuery())}>Back to Work</a>

  {#if notFound}
    <p class="gone" role="status">This session is not in the work log (it may be older than 30 days).</p>
  {:else if data && !data.enabled}
    <div class="notice" role="status">Tool-call recording is off. Set NOUS_TOOL_CALL_LOG_ENABLED to record what Nous does.</div>
  {:else if data && data.session}
    {@const s = data.session}
    {@const gated = s.gated ?? 0}
    <header class="head">
      <div class="head-top">
        <h1 class="title">{s.title}</h1>
        <span class="head-right">
          <StaleBadge state={$store} />
          <Button variant="outline" size="lg" class="px-4 max-md:min-h-11" disabled={refreshing} onclick={refresh}>Refresh</Button>
        </span>
      </div>
      <div class="meta">
        <span>{KIND_LABEL[s.kind] ?? s.kind}</span>
        <span>{fmtTime(s.started_at)}{s.started_at && s.ended_at ? ` to ${fmtTime(s.ended_at)}` : ''}</span>
        <span>{plural(s.turns, 'turn', 'turns')}</span>
        <span>{plural(s.tool_calls, 'tool call', 'tool calls')}</span>
        <span class:bad={s.errors > 0}>{errorsText(s.errors)}</span>
        {#if gated > 0}<span class="warn">{num(gated)} gated</span>{/if}
        <a href={modelCallsHref(undefined, { session_id: s.session_id })}>all model calls of this conversation</a>
      </div>
    </header>

    {#if turns.length > 0}
      <div class="replay card">
        <label for="replay-turn">Replay</label>
        <input id="replay-turn" type="range" min="0" max={turns.length - 1} step="1" value={replayAt}
          aria-label="Replay turn" aria-valuetext={`${turnLabel(turns[replayAt], replayAt)}, ${clockTime(turns[replayAt].started_at)} UTC`}
          oninput={onReplay} />
        <output for="replay-turn" class="readout">{clockTime(turns[replayAt].started_at)} UTC</output>
      </div>
    {/if}

    <div class="layout">
      <section class="turns card" aria-label="Turns">
        <div class="list-top">
          <span class="muted">Each turn is tinted with the colour of the frame Nous was in.</span>
          {#if frames.length > 0}
            <ul class="key" aria-label="Frames in these turns">
              {#each frames as f (f)}<li><span class="kdot" aria-hidden="true" style:--fc={frameVar(f)}></span>{f}</li>{/each}
            </ul>
          {/if}
        </div>

        {#if turnMissing}
          <p class="gone" role="status">{nextCursor === null ? 'That turn is not in this session.' : 'That turn is further on. Use Load more to reach it.'}</p>
        {/if}

        {#each turns as t, i (t.turn_id)}
          {@const change = frameChange(turns[i - 1], t)}
          {@const chips = contextChips(t)}
          {@const claim = claimLine(t)}
          {@const verdict = criticVerdict(t)}
          {@const calls = t.tool_call_count}
          {#if change}
            <div class="change" style:--from={frameVar(change.from)} style:--to={frameVar(change.to)}>
              <span class="grad" aria-hidden="true"></span>
              <p>Frame changed from <b class="from">{change.from}</b> to <b class="to">{change.to}</b>. {change.why}</p>
            </div>
          {/if}
          <article class="turn" class:target={t.turn_id === turnId} class:hl={replay === i} id="turn-{t.turn_id}" style:--fc={frameVar(t.frame_id)}>
            <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
            <div class="gutter" onclick={() => (replay = i)}>
              <div class="when"><span class="tlabel">{turnLabel(t, i)}</span> <span class="clock">{clockTime(t.started_at)}</span></div>
              <div class="frame">{frameLabel(t.frame_id)}</div>
              <div class="chosen">{chosenBy(t.frame_reason)}</div>
              <dl>
                <div><dt>{fmtTokens(t.input_tokens ?? null)}</dt> <dd>tokens in</dd></div>
                <div><dt>{fmtSeconds(replySeconds(t))}</dt> <dd>to reply</dd></div>
                <div><dt>{num(calls)}</dt> <dd>{calls === 1 ? 'tool call' : 'tool calls'}</dd></div>
              </dl>
              {#if verdict}<span class="crit" class:no={verdict === 'overrode'}>{verdictText(verdict, t.frame_id)}</span>{/if}
              {#if t.critic?.rationale}
                <details class="crit-why"><summary>{verdict ? 'Why' : "Critic's reasoning"}</summary><p class="wrap">{t.critic.rationale}</p></details>
              {/if}
            </div>

            <div class="rows">
              {#if t.trigger}
                <div class="bubble line"><span class="who">You</span><div class="wrap said">{t.trigger}</div></div>
              {/if}

              {#if chips.length > 0}
                <div class="chips ctx"><span>Already in context before acting:</span>{#each chips as c}<span class="chip">{c}</span>{/each}</div>
              {/if}

              {#each t.calls as c (c.id)}
                {@const b = badge(c)}
                <div class="step" class:err={c.is_error}>
                  {#if c.reason}<p class="why reason wrap">{c.reason}</p>{:else}<p class="why muted">No reason recorded</p>{/if}
                  <div class="toolline">
                    <span class="tool">{c.tool}</span>
                    {#if c.tool_called && c.tool_called !== c.tool}<span class="muted small">called as {c.tool_called}</span>{/if}
                    {#if b}<span class="badge">{b}</span>{/if}
                    {#if c.ledger}<span class="pill pill-{c.ledger.status}">{c.ledger.status}</span>{/if}
                  </div>
                  <span class="lane">{laneName(c.lane)}</span>
                  <span class="ms">{fmtDuration(c.duration_ms)}</span>
                  <div class="input wrap">{c.input}</div>
                  {#if c.error}<p class="errtext wrap">{c.error}</p>{/if}
                  {#if c.result}<p class="got wrap"><span class="muted">Got:</span> {c.result}</p>{/if}
                  {#if c.result_full}<FullResult callId={c.id} />{/if}
                  {#if c.ledger && c.ledger.status === 'unknown' && c.ledger.held}
                    <HoldUndo id={c.ledger.id} />
                  {/if}
                </div>
              {:else}
                <p class="muted none">No tools used this turn</p>
              {/each}

              {#if t.reply || claim || t.error}
                <div class="reply line">
                  <span class="who">{t.reply ? 'Nous replied' : 'Nous'}</span>
                  <div class="reply-body">
                    {#if t.reply}<div class="wrap said">{t.reply}</div>{/if}
                    {#if t.error}<p class="errtext wrap">{t.error}</p>{/if}
                    {#if claim}<div class="claim" class:bad={(t.claim_check?.violation_count ?? 0) > 0}>{claim}</div>{/if}
                  </div>
                </div>
              {/if}

              {#if t.model_calls.length > 0}
                <div class="mcalls">
                  <a href={modelCallsHref(undefined, { turn_id: t.turn_id })}>{plural(t.model_calls.length, 'main-loop model call', 'main-loop model calls')}</a>
                  <span class="muted">·</span>
                  <a href={modelCallsHref(undefined, { session_id: s.session_id })}>all model calls of this conversation</a>
                  <details>
                    <summary>List them</summary>
                    <ul>
                      {#each t.model_calls as m (m.id)}
                        <li><a href={modelCallsHref(['c', m.id])}>{m.model ?? 'unknown model'}</a> <span class="muted">{m.purpose} · {m.status} · {fmtTokens(m.input_total)} in · {fmtTokens(m.output_tokens)} out</span></li>
                      {/each}
                    </ul>
                  </details>
                </div>
              {/if}
            </div>
          </article>
        {/each}

        {#if nextCursor !== null}
          <div class="more">
            <Button variant="outline" class="max-md:min-h-11" disabled={moreBusy} onclick={loadMore}>Load more turns</Button>
            <span class="muted">{`Showing ${num(turns.length)} of ${num(Math.max(s.turns, turns.length))} turns`}</span>
            {#if moreError}<span class="errtext" role="alert">Could not load more.</span>{/if}
          </div>
        {/if}
      </section>

      {#if used.length > 0}
        <section class="used card" aria-labelledby="used-h">
          <h2 id="used-h">What it used, and why</h2>
          {#each used as u (u.tool)}
            <div class="used-row">
              <span class="used-tool"><span class="dot" aria-hidden="true" style:--dot={laneVar(u.lane)}></span>{u.tool}<span class="count">{plural(u.calls, 'call', 'calls')}</span></span>
              <ul>
                {#each u.why.slice(0, 3) as w}<li class="wrap reason">{w}</li>{/each}
                {#if u.why.length > 3}
                  <li class="more-reasons"><details><summary>and {u.why.length - 3} more</summary><ul>{#each u.why.slice(3) as w}<li class="wrap">{w}</li>{/each}</ul></details></li>
                {/if}
              </ul>
            </div>
          {/each}
        </section>
      {/if}
    </div>
  {:else if $store.error}
    <p class="errtext" role="alert">Could not load this session.</p>
  {:else}
    <p class="muted" role="status">Loading…</p>
  {/if}
</div>

<style>
  /* Colours are tokens only (hexGuard): the dark look reads today's values, the paper ground its own. */
  .trace { display: flex; flex-direction: column; gap: 1.25rem; --gw: 168px; --lw: 86px; }
  .back { color: var(--accent-text); font-size: 0.8125rem; text-decoration: none; align-self: flex-start; text-underline-offset: 3px; }
  .back:hover { text-decoration: underline; }
  a:focus-visible, summary:focus-visible, input:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
  .gone, .muted { color: var(--muted); }
  .small { font-size: 0.75rem; }
  .notice { padding: 0.75rem 1rem; border-radius: var(--radius-sm); background: var(--notice-bg); border: 1px solid var(--notice-border); font-size: 0.875rem; }
  .wrap { overflow-wrap: anywhere; white-space: pre-wrap; }
  .card { background: var(--surface); border: var(--card-border); border-radius: var(--card-radius); box-shadow: var(--card-shadow); }

  /* Header: the title, Refresh top right, the facts as pills */
  .head { display: flex; flex-direction: column; gap: 0.75rem; }
  .head-top { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
  .title { margin: 0; font-size: 1.625rem; font-weight: 700; letter-spacing: -0.02em; line-height: 1.2; max-width: 60ch; overflow-wrap: anywhere; min-width: 0; }
  :global(html[data-look="paper"]) .title { font-size: 30px; font-weight: 650; letter-spacing: -0.022em; line-height: 1.15; }
  .head-right { display: flex; align-items: center; gap: 0.75rem; margin-left: auto; }
  .meta { display: flex; flex-wrap: wrap; gap: 0.5rem; font-size: 0.8125rem; }
  .meta > * { padding: 3px 11px; border-radius: 999px; background: var(--surface); border: var(--card-border); box-shadow: var(--card-shadow); color: var(--text); text-decoration: none; }
  .meta .bad { color: var(--err-text); background: var(--err-bg); border-color: transparent; box-shadow: none; font-weight: 600; }
  .meta .warn { color: var(--warn-text); background: var(--warn-bg); border-color: transparent; box-shadow: none; font-weight: 600; }
  .meta a { color: var(--accent-text); }
  .meta a:hover { text-decoration: underline; }

  /* Replay: one round handle steps through the loaded turns */
  .replay { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 1.125rem; padding: 0.75rem 1.25rem; }
  .replay label { font-size: 0.8125rem; color: var(--muted); }
  .readout { font-size: 0.9375rem; font-weight: 650; font-stretch: 82%; min-width: 9ch; text-align: right; }
  .replay input { width: 100%; height: 24px; margin: 0; background: transparent; appearance: none; -webkit-appearance: none; cursor: pointer; }
  .replay input::-webkit-slider-runnable-track { height: 4px; border-radius: 2px; background: var(--border); }
  .replay input::-moz-range-track { height: 4px; border-radius: 2px; background: var(--border); }
  .replay input::-webkit-slider-thumb { -webkit-appearance: none; appearance: none; width: 18px; height: 18px; margin-top: -7px; border-radius: 50%;
    background: hsl(var(--primary)); border: 3px solid var(--surface); box-shadow: 0 0 0 1px var(--border); }
  .replay input::-moz-range-thumb { width: 12px; height: 12px; border-radius: 50%; background: hsl(var(--primary)); border: 3px solid var(--surface); box-shadow: 0 0 0 1px var(--border); }

  /* The turns card and the "What it used" card beside it */
  .layout { display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: 1.5rem; align-items: start; }
  .turns { padding: 0.375rem 0 0.75rem; display: flex; flex-direction: column; min-width: 0; }
  .list-top { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 0.5rem 1.25rem; padding: 0.75rem 1.375rem 0.625rem; font-size: 0.8125rem; }
  .key { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 0.375rem 1rem; color: var(--muted); overflow-wrap: anywhere; min-width: 0; }
  .key li { display: inline-flex; align-items: center; gap: 6px; }
  .kdot { width: 10px; height: 10px; border-radius: 50%; background: var(--fc); flex: none; }

  .turn { display: grid; grid-template-columns: var(--gw) minmax(0, 1fr); margin: 0.375rem 0.625rem; border-radius: var(--card-radius-sm); scroll-margin-top: 1rem;
    transition: background var(--transition); }
  .turn.hl { background: var(--surface-hover); }
  .turn.target { outline: 2px solid var(--accent); outline-offset: 2px; }

  /* The frame column: the frame's colour at 7% (debug's name on its tint is 4.56:1), a ring on hover */
  .gutter { border-radius: var(--card-radius-sm); background: color-mix(in srgb, var(--fc) 7%, transparent); padding: 0.75rem 0.875rem; cursor: pointer;
    overflow-wrap: anywhere; min-width: 0; font-size: 0.78125rem; color: var(--muted); }
  .gutter:hover { box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--fc) 35%, transparent); }
  .when { font-stretch: 82%; }
  .tlabel { margin-right: 0.375rem; }
  .frame { font-size: 1.0625rem; font-weight: 650; color: var(--fc); margin-top: 2px; line-height: 1.2; }
  .chosen { margin-bottom: 0.625rem; }
  .gutter dl { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 1px 8px; margin: 0; }
  .gutter dl > div { display: contents; }
  .gutter dt { font-weight: 650; font-stretch: 82%; text-align: right; color: var(--text); }
  .gutter dd { margin: 0; }
  .crit { display: inline-block; margin-top: 0.625rem; font-size: 0.75rem; font-weight: 600; padding: 1px 9px; border-radius: 999px; background: var(--surface); color: var(--muted); }
  .crit.no { color: var(--err-text); background: var(--err-bg); }
  .crit-why { margin-top: 0.375rem; }
  .crit-why summary { cursor: pointer; color: var(--accent-text); }
  .crit-why p { margin: 4px 0 0; color: var(--text); }

  /* The rows beside it: your message, each call (reason first), the reply */
  .rows { min-width: 0; padding: 0.5rem 0.5rem 0.5rem 1.375rem; display: flex; flex-direction: column; }
  .line { display: grid; grid-template-columns: var(--lw) minmax(0, 1fr); gap: 0.75rem; padding: 0.375rem 0 0.625rem; }
  .who { font-size: 0.78125rem; color: var(--muted); padding-top: 2px; }
  .said { font-size: 0.9375rem; line-height: 1.5; max-width: 72ch; overflow-wrap: anywhere; }
  .reply { border-top: 1px solid var(--border); padding-top: 0.625rem; }
  .reply-body { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
  .chips { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; font-size: 0.8125rem; color: var(--muted); padding: 0 0 0.625rem calc(var(--lw) + 0.75rem); }
  .chip { padding: 3px 10px; border-radius: 999px; background: var(--surface-hover); color: var(--text); }
  .none { margin: 0; font-size: 0.875rem; padding: 0.5rem 0 0.5rem calc(var(--lw) + 0.75rem); border-top: 1px solid var(--border); }

  .step { display: grid; grid-template-columns: var(--lw) minmax(0, 1fr) auto; gap: 4px 0.75rem; padding: 0.625rem 0; border-top: 1px solid var(--border); align-items: start; }
  .step > :global(*) { grid-column: 2 / -1; min-width: 0; max-width: 100%; }
  .step > .why { grid-column: 2; grid-row: 1; }
  .step > .toolline { grid-column: 2; grid-row: 2; }
  .step > .lane { grid-column: 1; grid-row: 1; }
  .step > .ms { grid-column: 3; grid-row: 1; }
  .why { margin: 0; font-size: 0.9375rem; line-height: 1.5; max-width: 72ch; }
  .reason { overflow-wrap: anywhere; }
  .toolline { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: center; }
  .tool { font-family: var(--font-mono); font-size: 0.8125rem; font-weight: 500; overflow-wrap: anywhere; }
  .lane { font-size: 0.78125rem; color: var(--muted); padding-top: 2px; }
  .ms { font-size: 0.78125rem; color: var(--muted); font-stretch: 82%; text-align: right; padding-top: 2px; white-space: nowrap; }
  .badge { font-size: 0.71875rem; font-weight: 650; padding: 1px 9px; border-radius: 999px; color: var(--warn-text); background: var(--warn-bg); border: 1px solid var(--warn-border); }
  .pill { font-size: 0.71875rem; padding: 1px 9px; border-radius: 999px; border: 1px solid var(--border); color: var(--muted); }
  .pill-unknown { color: var(--warn-text); border-color: var(--warn-border); }
  .pill-error, .pill-blocked { color: var(--err-text); }
  .pill-success { color: var(--ok-text); }
  .input { font-family: var(--font-mono); font-size: 0.78125rem; line-height: 1.5; background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius-sm);
    padding: 0.375rem 0.625rem; overflow-x: auto; overflow-wrap: anywhere; max-width: 100%; justify-self: stretch; }
  .got { margin: 0; justify-self: start; max-width: 100%; font-family: var(--font-mono); font-size: 0.78125rem; line-height: 1.5; color: var(--text);
    background: var(--surface-hover); border-radius: var(--radius-sm); padding: 0.3125rem 0.625rem; overflow-wrap: anywhere; }
  .step.err .got { color: var(--err-text); background: var(--err-bg); }
  .errtext { color: var(--err-text); font-size: 0.875rem; margin: 0; }
  .claim { font-size: 0.8125rem; color: var(--ok-text); }
  .claim.bad { color: var(--err-text); }
  .mcalls { font-size: 0.8125rem; display: flex; flex-wrap: wrap; gap: 4px 8px; align-items: baseline; padding: 0.25rem 0 0.25rem calc(var(--lw) + 0.75rem); }
  .mcalls a { color: var(--accent-text); }
  .mcalls details { flex-basis: 100%; }
  .mcalls ul { margin: 4px 0 0; padding-left: 1.125rem; display: flex; flex-direction: column; gap: 2px; }

  /* Between turns: a bar from the old frame's colour to the new one's, and the sentence */
  .change { display: flex; align-items: center; gap: 0.625rem; margin: 2px 0.625rem; padding: 0.375rem 0.75rem; font-size: 0.8125rem; color: var(--muted); overflow-wrap: anywhere; }
  .change p { margin: 0; min-width: 0; }
  .grad { width: calc(var(--gw) - 24px); height: 6px; border-radius: 3px; background: linear-gradient(90deg, var(--from), var(--to)); flex: none; }
  .change b { font-weight: 650; }
  .change .from { color: var(--from); }
  .change .to { color: var(--to); }

  .more { display: flex; align-items: center; flex-wrap: wrap; gap: 0.75rem; padding: 0.75rem 1.375rem 0; }

  /* What it used, and why */
  .used { padding: 1.125rem 1.25rem 0.5rem; display: flex; flex-direction: column; min-width: 0; }
  .used h2 { margin: 0 0 0.5rem; font-size: 0.9375rem; font-weight: 650; }
  .used-row { border-top: 1px solid var(--border); padding: 0.625rem 0; display: flex; flex-direction: column; gap: 4px; }
  .used-tool { display: flex; align-items: center; gap: 8px; font-family: var(--font-mono); font-size: 0.8125rem; font-weight: 500; overflow-wrap: anywhere; }
  .count { font-family: var(--font-ui); font-weight: 400; font-size: 0.78125rem; color: var(--muted); margin-left: auto; white-space: nowrap; }
  .used ul { margin: 0; padding-left: 1.125rem; display: flex; flex-direction: column; gap: 2px; font-size: 0.8125rem; line-height: 1.45; color: var(--muted); }
  .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; flex: none; background: var(--dot); }
  .more-reasons { list-style: none; margin-left: -1.125rem; }

  @media (max-width: 1180px) {
    .layout { grid-template-columns: minmax(0, 1fr); }
  }
  @media (max-width: 768px) {
    summary, .mcalls a, .back { min-height: 44px; display: inline-flex; align-items: center; }
    .replay { gap: 0.75rem; padding: 0.625rem 1rem; }
    .replay input { height: 44px; }
  }
  @media (max-width: 640px) {
    .turn { grid-template-columns: minmax(0, 1fr); }
    .gutter { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.25rem 0.75rem; }
    .gutter .frame, .gutter .chosen { margin: 0; }
    .gutter dl { display: flex; flex-wrap: wrap; gap: 0 0.75rem; }
    .gutter dl > div { display: inline-flex; gap: 4px; }
    .crit, .crit-why { margin-top: 0; }
    .rows { padding: 0.5rem 0.25rem; }
    .line { grid-template-columns: minmax(0, 1fr); gap: 2px; }
    .step { grid-template-columns: minmax(0, 1fr) auto; }
    .step > :global(*) { grid-column: 1 / -1; }
    .step > .lane { grid-column: 1; grid-row: 1; }
    .step > .ms { grid-column: 2; grid-row: 1; }
    .step > .why { grid-column: 1 / -1; grid-row: 2; }
    .step > .toolline { grid-column: 1 / -1; grid-row: 3; }
    .chips, .none, .mcalls { padding-left: 0; }
    .change { flex-wrap: wrap; }
    .grad { width: 48px; }
    .list-top, .more { padding-left: 0.875rem; padding-right: 0.875rem; }
    .turn, .change { margin-left: 0.375rem; margin-right: 0.375rem; }
  }
</style>
