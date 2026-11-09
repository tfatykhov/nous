<script lang="ts">
  import { apiGet, type ApiError } from '../../lib/api';
  import { makePollStore } from '../../lib/stores/registry';
  import { usePoll } from '../../lib/poll';
  import { routeHref, ROUTES, type RouteName } from '../../lib/router';
  import { fmtDuration, fmtTime, fmtTokens, getLastListQuery, laneColor } from '../../lib/work';
  import type { WorkCall, WorkTrace, WorkTurn } from '../../lib/types/api';
  import StaleBadge from '../../lib/ui/StaleBadge.svelte';
  import HoldUndo from '../../lib/ui/HoldUndo.svelte';

  // F108 Work trace: one session, turn by turn. Every string here (messages, reasons, inputs, results, replies,
  // critic rationale) came from a model or a user and is shown as a text node only: no {@html}, no markdown.
  // Turns come oldest first, a page at a time. While more pages remain nothing is polled (loaded history does not
  // change); once every turn is loaded, each 15 s poll asks for the turns after the second-to-last one, so the
  // newest turn is refreshed and new turns appear. Results merge by turn id.
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
  const LANES: { id: string; name: string }[] = [
    { id: 'memory', name: 'Memory' },
    { id: 'live', name: 'Live state' },
    { id: 'workspace', name: 'Workspace' },
    { id: 'acts', name: 'Acts' },
  ];
  const laneName = (lane: string) => LANES.find((l) => l.id === lane)?.name ?? 'Other';
  const railLanes = (lane: string) => (LANES.some((l) => l.id === lane) ? LANES.map((l) => l.id) : [...LANES.map((l) => l.id), 'other']);

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

  const modelCallsReady = () => (ROUTES as readonly string[]).includes('model-calls');
  const modelCallsHref = (sub: string[] | undefined, query?: Record<string, string>) => routeHref('model-calls' as RouteName, sub, query);
  const num = (n: number) => n.toLocaleString('en-US');
  const errorsText = (n: number) => (n === 0 ? 'No errors' : plural(n, 'error', 'errors'));
</script>

<div class="trace">
  <a class="back" href={routeHref('work', '', getLastListQuery())}>← Back to Work</a>

  {#if notFound}
    <p class="gone" role="status">This session is not in the work log (it may be older than 30 days).</p>
  {:else if data && !data.enabled}
    <div class="notice" role="status">Tool-call recording is off. Set NOUS_TOOL_CALL_LOG_ENABLED to record what Nous does.</div>
  {:else if data && data.session}
    {@const s = data.session}
    <header class="head">
      <div class="head-top">
        <h1>{s.title}</h1>
        <StaleBadge state={$store} />
      </div>
      <div class="meta">
        <span>{KIND_LABEL[s.kind] ?? s.kind}</span>
        <span>{fmtTime(s.started_at)}{s.started_at && s.ended_at ? ` to ${fmtTime(s.ended_at)}` : ''}</span>
        <span>{plural(s.turns, 'turn', 'turns')}</span>
        <span>{plural(s.tool_calls, 'tool call', 'tool calls')}</span>
        <span class:bad={s.errors > 0}>{errorsText(s.errors)}</span>
        {#if modelCallsReady()}<a href={modelCallsHref(undefined, { session_id: s.session_id })}>all model calls of this conversation</a>{/if}
      </div>
    </header>

    {#if used.length > 0}
      <section class="used" aria-labelledby="used-h">
        <h2 id="used-h">What it used, and why</h2>
        {#each used as u (u.tool)}
          <div class="used-row">
            <span class="used-tool"><span class="dot" aria-hidden="true" style:background={laneColor(u.lane)}></span>{u.tool}<span class="count">{plural(u.calls, 'call', 'calls')}</span></span>
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

    <section class="turns" aria-label="Turns">
      <div class="legend">
        <span class="lane-keys" aria-hidden="true">
          {#each LANES as l (l.id)}<span style:color={laneColor(l.id)}>{l.name[0]}</span>{/each}
        </span>
        <span>The rail shows which lane each step used: memory, live state, workspace, acts. Read it top to bottom to see the path Nous took.</span>
      </div>

      {#if turnMissing}
        <p class="gone" role="status">{nextCursor === null ? 'That turn is not in this session.' : 'That turn is further on. Use Load more to reach it.'}</p>
      {/if}

      {#each turns as t, i (t.turn_id)}
        {@const chips = contextChips(t)}
        {@const claim = claimLine(t)}
        <article class="turn" class:target={t.turn_id === turnId} id="turn-{t.turn_id}">
          <div class="row">
            <div class="side"><span>{turnLabel(t, i)}</span><br />{fmtTime(t.started_at)}</div>
            {#if t.trigger}
              <div class="bubble"><div class="who">You</div><div class="wrap body">{t.trigger}</div></div>
            {/if}
          </div>

          {#if chips.length > 0}
            <div class="row ctx">
              <div class="side"></div>
              <div class="chips"><span>Already in context before acting:</span>{#each chips as c}<span class="chip">{c}</span>{/each}</div>
            </div>
          {/if}

          {#each t.calls as c (c.id)}
            {@const b = badge(c)}
            <div class="row">
              <div class="rail" aria-hidden="true">
                {#each railLanes(c.lane) as l (l)}
                  <span class="col" style:--lane={laneColor(l)}>{#if l === (LANES.some((x) => x.id === c.lane) ? c.lane : 'other')}<span class="seg" style:background={laneColor(c.lane)}></span>{/if}</span>
                {/each}
              </div>
              <div class="step" style:box-shadow={`inset 3px 0 0 ${laneColor(c.lane)}`}>
                {#if c.reason}<p class="why wrap">{c.reason}</p>{:else}<p class="why muted">No reason recorded</p>{/if}
                <div class="toolline">
                  <span class="tool">{c.tool}</span>
                  {#if c.tool_called && c.tool_called !== c.tool}<span class="muted small">called as {c.tool_called}</span>{/if}
                  <span class="lane" style:color={laneColor(c.lane)}>{laneName(c.lane)}</span>
                  {#if b}<span class="badge">{b}</span>{/if}
                  {#if c.ledger}<span class="pill pill-{c.ledger.status}">{c.ledger.status}</span>{/if}
                  <span class="ms">{fmtDuration(c.duration_ms)}</span>
                </div>
                <div class="input wrap">{c.input}</div>
                {#if c.error}<p class="err wrap">{c.error}</p>{/if}
                {#if c.result}<p class="got wrap"><span class="muted">Got:</span> {c.result}</p>{/if}
                {#if c.ledger && c.ledger.status === 'unknown'}
                  <HoldUndo id={c.ledger.id} />
                {/if}
              </div>
            </div>
          {:else}
            <div class="row"><div class="side"></div><p class="muted none">No tools used this turn</p></div>
          {/each}

          {#if t.reply || claim || t.critic || t.error}
            <div class="row">
              <div class="side"></div>
              <div class="reply">
                {#if t.reply}<div class="who">Nous replied</div><div class="wrap body">{t.reply}</div>{/if}
                {#if t.error}<p class="err wrap">{t.error}</p>{/if}
                {#if claim}<div class="claim" class:bad={(t.claim_check?.violation_count ?? 0) > 0}>{claim}</div>{/if}
                {#if t.critic}
                  <div class="critic">
                    Critic: {t.critic.critic_frame ?? 'unknown'}{#if t.critic.agreed === false && t.critic.heuristic_frame} (heuristic said {t.critic.heuristic_frame}){/if}
                    {#if t.critic.rationale}
                      <details><summary>Why</summary><p class="wrap">{t.critic.rationale}</p></details>
                    {/if}
                  </div>
                {/if}
              </div>
            </div>
          {/if}

          {#if t.model_calls.length > 0}
            <div class="row">
              <div class="side"></div>
              <div class="mcalls">
                {#if modelCallsReady()}
                  <a href={modelCallsHref(undefined, { turn_id: t.turn_id })}>{plural(t.model_calls.length, 'main-loop model call', 'main-loop model calls')}</a>
                  <span class="muted">·</span>
                  <a href={modelCallsHref(undefined, { session_id: s.session_id })}>all model calls of this conversation</a>
                {:else}
                  <span>{plural(t.model_calls.length, 'main-loop model call', 'main-loop model calls')}</span>
                {/if}
                <details>
                  <summary>List them</summary>
                  <ul>
                    {#each t.model_calls as m (m.id)}
                      <li>{#if modelCallsReady()}<a href={modelCallsHref(['c', m.id])}>{m.model ?? 'unknown model'}</a>{:else}<span>{m.model ?? 'unknown model'}</span>{/if} <span class="muted">{m.purpose} · {m.status} · {fmtTokens(m.input_total)} in · {fmtTokens(m.output_tokens)} out</span></li>
                    {/each}
                  </ul>
                </details>
              </div>
            </div>
          {/if}
        </article>
      {/each}

      {#if nextCursor !== null}
        <div class="more">
          <button type="button" disabled={moreBusy} onclick={loadMore}>Load more turns</button>
          <span class="muted">{`Showing ${num(turns.length)} of ${num(Math.max(s.turns, turns.length))} turns`}</span>
          {#if moreError}<span class="err" role="alert">Could not load more.</span>{/if}
        </div>
      {/if}
    </section>
  {:else if $store.error}
    <p class="err" role="alert">Could not load this session.</p>
  {:else}
    <p class="muted" role="status">Loading…</p>
  {/if}
</div>

<style>
  .trace { display: flex; flex-direction: column; gap: 1.5rem; }
  .back { color: var(--accent-text); font-size: 0.875rem; text-decoration: none; align-self: flex-start; }
  .back:hover { text-decoration: underline; }
  a:focus-visible, button:focus-visible, summary:focus-visible { outline: 2px solid var(--accent-text); outline-offset: 2px; }
  .gone, .muted { color: var(--muted); }
  .small { font-size: 0.75rem; }
  .notice { padding: 0.75rem 1rem; border-radius: 8px; background: rgba(245, 158, 11, 0.08); border: 1px solid rgba(245, 158, 11, 0.4); font-size: 0.875rem; }
  .wrap { overflow-wrap: anywhere; white-space: pre-wrap; }
  .head { display: flex; flex-direction: column; gap: 0.625rem; }
  .head-top { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
  h1 { margin: 0; font-size: 1.625rem; font-weight: 700; letter-spacing: -0.02em; max-width: 60ch; overflow-wrap: anywhere; }
  .meta { display: flex; flex-wrap: wrap; gap: 0.5rem 1.25rem; font-size: 0.875rem; color: var(--muted); }
  .meta .bad { color: var(--red); }
  .meta a { color: var(--accent-text); }
  .used { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 1.125rem 1.25rem; display: flex; flex-direction: column; gap: 0.875rem; }
  .used h2 { margin: 0; font-size: 1.0625rem; font-weight: 600; }
  .used-row { display: grid; grid-template-columns: minmax(160px, 220px) minmax(0, 1fr); gap: 0.25rem 1rem; align-items: baseline; border-top: 1px solid var(--border); padding-top: 0.75rem; }
  .used-tool { display: flex; align-items: center; gap: 8px; font-family: var(--font-mono); font-size: 0.875rem; overflow-wrap: anywhere; }
  .count { font-family: inherit; font-size: 0.8125rem; color: var(--muted); }
  .used ul { margin: 0; padding-left: 1.125rem; display: flex; flex-direction: column; gap: 4px; font-size: 0.875rem; line-height: 1.5; }
  .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; flex: none; }
  .turns { display: flex; flex-direction: column; gap: 1.75rem; }
  .legend { display: flex; align-items: center; gap: 1rem; font-size: 0.8125rem; color: var(--muted); flex-wrap: wrap; }
  .lane-keys { display: flex; }
  .lane-keys span { width: 28px; text-align: center; font-weight: 600; }
  .turn { display: flex; flex-direction: column; gap: 0.625rem; scroll-margin-top: 1rem; border-radius: 12px; }
  .turn.target { outline: 2px solid var(--accent); outline-offset: 6px; }
  .row { display: flex; gap: 1rem; align-items: flex-start; }
  .side { width: 112px; flex: none; font-size: 0.8125rem; color: var(--muted); padding-top: 0.75rem; }
  .ctx .side { padding: 0; }
  .bubble { flex: 1 1 auto; min-width: 0; background: var(--surface-hover); border: 1px solid var(--border); border-radius: 12px; padding: 0.75rem 1rem; }
  .who { font-size: 0.75rem; color: var(--muted); margin-bottom: 4px; }
  .body { font-size: 0.9375rem; line-height: 1.5; }
  .chips { flex: 1 1 auto; min-width: 0; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; font-size: 0.8125rem; color: var(--muted); }
  .chip { padding: 4px 10px; border-radius: 999px; border: 1px solid var(--border); background: var(--surface); color: var(--text); }
  .rail { width: 112px; flex: none; display: flex; align-self: stretch; }
  .col { width: 28px; display: flex; justify-content: center; padding-top: 1rem;
    background: linear-gradient(color-mix(in srgb, var(--lane) 13%, transparent), color-mix(in srgb, var(--lane) 13%, transparent)) center / 2px 100% no-repeat; }
  .seg { width: 12px; height: 12px; border-radius: 50%; box-shadow: 0 0 0 3px var(--bg); }
  .step { flex: 1 1 auto; min-width: 0; background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 0.75rem 1rem; display: flex; flex-direction: column; gap: 8px; }
  .why { margin: 0; font-size: 0.9375rem; line-height: 1.5; font-weight: 500; }
  .why.muted { font-weight: 400; }
  .toolline { display: flex; flex-wrap: wrap; gap: 8px 14px; align-items: baseline; }
  .tool { font-family: var(--font-mono); font-size: 0.875rem; font-weight: 500; }
  .lane { font-size: 0.75rem; font-weight: 600; }
  .ms { font-size: 0.8125rem; color: var(--muted); margin-left: auto; }
  .badge { font-size: 0.75rem; font-weight: 600; padding: 2px 8px; border-radius: 6px; color: #fbbf24; border: 1px solid rgba(251, 191, 36, 0.5); background: rgba(251, 191, 36, 0.08); }
  .pill { font-size: 0.75rem; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--border); color: var(--muted); }
  .pill-unknown { color: #fbbf24; border-color: rgba(251, 191, 36, 0.5); }
  .pill-error, .pill-blocked { color: var(--red); }
  .pill-success { color: #34d399; }
  .input { font-family: var(--font-mono); font-size: 0.8125rem; line-height: 1.5; background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 0.5rem 0.625rem; overflow-x: auto; }
  .got { margin: 0; font-size: 0.875rem; line-height: 1.5; }
  .err { color: var(--red); font-size: 0.875rem; margin: 0; }
  .none { margin: 0; font-size: 0.875rem; padding-top: 0.25rem; }
  .reply { flex: 1 1 auto; min-width: 0; border-left: 2px solid var(--accent); padding: 6px 0 6px 1rem; display: flex; flex-direction: column; gap: 6px; }
  .reply .body { line-height: 1.55; max-width: 90ch; }
  .claim { font-size: 0.8125rem; color: #34d399; }
  .claim.bad { color: var(--red); }
  .critic { font-size: 0.8125rem; color: var(--muted); }
  .critic details p { margin: 4px 0 0; }
  .mcalls { flex: 1 1 auto; min-width: 0; font-size: 0.8125rem; display: flex; flex-wrap: wrap; gap: 4px 8px; align-items: baseline; }
  .mcalls a, .back { color: var(--accent-text); }
  .mcalls details { flex-basis: 100%; }
  .mcalls ul { margin: 4px 0 0; padding-left: 1.125rem; display: flex; flex-direction: column; gap: 2px; }
  .more button { font: inherit; cursor: pointer; background: var(--surface); color: var(--text); border: 1px solid var(--border); border-radius: 8px; padding: 0.5rem 0.875rem; min-height: 36px; }
  .more button:disabled { opacity: 0.6; cursor: default; }
  .more { display: flex; align-items: center; gap: 0.75rem; }
  .more-reasons { list-style: none; margin-left: -1.125rem; }
  @media (max-width: 768px) {
    .more button, summary, .mcalls a, .back { min-height: 44px; display: inline-flex; align-items: center; }
    .side, .rail { width: 56px; }
    .col { width: 14px; }
    .lane-keys span { width: 14px; }
    .used-row { grid-template-columns: minmax(0, 1fr); }
  }
</style>
