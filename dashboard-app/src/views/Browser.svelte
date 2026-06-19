<script lang="ts">
  import { apiGet, apiSend } from '../lib/api';
  import DataTable from '../lib/ui/DataTable.svelte';
  import type {
    FactsResponse,
    EpisodesResponse,
    DecisionsResponse,
    ProceduresResponse,
    CensorsResponse,
    ChunksResponse,
    BrowserFact,
    BrowserEpisode,
    BrowserDecision,
    BrowserProcedure,
    BrowserCensor,
    BrowserChunk,
  } from '../lib/types/api';

  // ── Tab definitions ───────────────────────────────────────────────────────

  type TabId = 'facts' | 'episodes' | 'decisions' | 'procedures' | 'censors' | 'chunks';

  const TABS: { id: TabId; label: string }[] = [
    { id: 'facts',      label: 'Facts' },
    { id: 'episodes',   label: 'Episodes' },
    { id: 'decisions',  label: 'Decisions' },
    { id: 'procedures', label: 'Procedures' },
    { id: 'censors',    label: 'Censors' },
    { id: 'chunks',     label: 'Chunks' },
  ];

  const PAGE_SIZE = 20;

  // ── Component-local state (survives tab switches) ─────────────────────────

  let activeTab = $state<TabId>('facts');

  // Per-tab: search query
  let searchFacts      = $state('');
  let searchEpisodes   = $state('');
  let searchDecisions  = $state('');
  let searchProcedures = $state('');
  let searchCensors    = $state('');
  let searchChunks     = $state('');

  // Per-tab: pagination offset
  let offsetFacts      = $state(0);
  let offsetEpisodes   = $state(0);
  let offsetDecisions  = $state(0);
  let offsetProcedures = $state(0);
  let offsetCensors    = $state(0);
  let offsetChunks     = $state(0);

  // Per-tab: data + status
  let dataFacts       = $state<FactsResponse | null>(null);
  let dataEpisodes    = $state<EpisodesResponse | null>(null);
  let dataDecisions   = $state<DecisionsResponse | null>(null);
  let dataProcedures  = $state<ProceduresResponse | null>(null);
  let dataCensors     = $state<CensorsResponse | null>(null);
  let dataChunks      = $state<ChunksResponse | null>(null);

  let loadingFacts      = $state(false);
  let loadingEpisodes   = $state(false);
  let loadingDecisions  = $state(false);
  let loadingProcedures = $state(false);
  let loadingCensors    = $state(false);
  let loadingChunks     = $state(false);

  let errorFacts      = $state<string | null>(null);
  let errorEpisodes   = $state<string | null>(null);
  let errorDecisions  = $state<string | null>(null);
  let errorProcedures = $state<string | null>(null);
  let errorCensors    = $state<string | null>(null);
  let errorChunks     = $state<string | null>(null);

  // ── Fetch helpers ─────────────────────────────────────────────────────────

  function buildParams(q: string, off: number): URLSearchParams {
    const p = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(off) });
    if (q) p.set('q', q);
    return p;
  }

  async function loadFacts() {
    loadingFacts = true; errorFacts = null;
    try { dataFacts = await apiGet<FactsResponse>(`/facts?${buildParams(searchFacts, offsetFacts)}`); }
    catch (e) { errorFacts = e instanceof Error ? e.message : 'Failed to load'; }
    finally { loadingFacts = false; }
  }

  async function loadEpisodes() {
    loadingEpisodes = true; errorEpisodes = null;
    try { dataEpisodes = await apiGet<EpisodesResponse>(`/episodes?${buildParams(searchEpisodes, offsetEpisodes)}`); }
    catch (e) { errorEpisodes = e instanceof Error ? e.message : 'Failed to load'; }
    finally { loadingEpisodes = false; }
  }

  async function loadDecisions() {
    loadingDecisions = true; errorDecisions = null;
    try { dataDecisions = await apiGet<DecisionsResponse>(`/decisions?${buildParams(searchDecisions, offsetDecisions)}`); }
    catch (e) { errorDecisions = e instanceof Error ? e.message : 'Failed to load'; }
    finally { loadingDecisions = false; }
  }

  async function loadProcedures() {
    loadingProcedures = true; errorProcedures = null;
    try { dataProcedures = await apiGet<ProceduresResponse>(`/procedures?${buildParams(searchProcedures, offsetProcedures)}`); }
    catch (e) { errorProcedures = e instanceof Error ? e.message : 'Failed to load'; }
    finally { loadingProcedures = false; }
  }

  async function loadCensors() {
    loadingCensors = true; errorCensors = null;
    try { dataCensors = await apiGet<CensorsResponse>(`/censors?${buildParams(searchCensors, offsetCensors)}`); }
    catch (e) { errorCensors = e instanceof Error ? e.message : 'Failed to load'; }
    finally { loadingCensors = false; }
  }

  async function loadChunks() {
    loadingChunks = true; errorChunks = null;
    try { dataChunks = await apiGet<ChunksResponse>(`/chunks?${buildParams(searchChunks, offsetChunks)}`); }
    catch (e) { errorChunks = e instanceof Error ? e.message : 'Failed to load'; }
    finally { loadingChunks = false; }
  }

  function switchTab(tab: TabId) {
    activeTab = tab;
    // Load on first open; cached until search/offset changes
    if (tab === 'facts'      && !dataFacts      && !loadingFacts)      loadFacts();
    if (tab === 'episodes'   && !dataEpisodes   && !loadingEpisodes)   loadEpisodes();
    if (tab === 'decisions'  && !dataDecisions  && !loadingDecisions)  loadDecisions();
    if (tab === 'procedures' && !dataProcedures && !loadingProcedures) loadProcedures();
    if (tab === 'censors'    && !dataCensors    && !loadingCensors)    loadCensors();
    if (tab === 'chunks'     && !dataChunks     && !loadingChunks)     loadChunks();
  }

  // Load initial tab on mount
  $effect(() => { loadFacts(); });

  // ── Formatters ────────────────────────────────────────────────────────────

  function fmtDate(iso: string | null | undefined): string {
    if (!iso) return '—';
    return new Date(iso).toLocaleDateString([], { dateStyle: 'short' });
  }

  function fmtPct(v: number | null | undefined): string {
    if (v == null) return '—';
    const n = v <= 1 ? v * 100 : v;
    return n.toFixed(0) + '%';
  }

  function trunc(s: string | null | undefined, n: number): string {
    if (!s) return '';
    return s.length > n ? s.slice(0, n) + '…' : s;
  }

  // ── Pagination helpers ────────────────────────────────────────────────────

  function pageInfo(off: number, total: number) {
    const currentPage = Math.floor(off / PAGE_SIZE) + 1;
    const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    return { currentPage, totalPages };
  }

  // ── Censors write interaction ─────────────────────────────────────────────

  // Per-censor pending edit state
  let censorEdits      = $state<Record<string, { action: string; active: boolean }>>({});
  let censorSaveStatus = $state<Record<string, 'idle' | 'saving' | 'saved' | 'error'>>({});
  let censorSaveMsg    = $state<Record<string, string>>({});

  function initCensorEdit(c: BrowserCensor) {
    if (!censorEdits[c.id]) {
      censorEdits[c.id] = { action: c.action, active: c.active };
    }
  }

  async function saveCensor(id: string) {
    const edit = censorEdits[id];
    if (!edit) return;
    censorSaveStatus[id] = 'saving';
    censorSaveMsg[id] = 'Saving…';
    try {
      await apiSend<BrowserCensor>(`/censors/${id}`, { action: edit.action, active: edit.active });
      censorSaveStatus[id] = 'saved';
      censorSaveMsg[id] = 'Saved';
      loadCensors(); // Refresh after save
    } catch (e) {
      censorSaveStatus[id] = 'error';
      censorSaveMsg[id] = e instanceof Error ? e.message : 'Error';
    }
  }
</script>

<header class="view-head">
  <div>
    <h1>Memory Browser</h1>
    <p class="subtitle">Search and explore all memory types</p>
  </div>
</header>

<!-- ── Tab bar ─────────────────────────────────────────────────────────── -->
<div class="tab-bar" role="tablist">
  {#each TABS as tab}
    <button
      role="tab"
      aria-selected={activeTab === tab.id}
      class="tab-btn"
      class:active={activeTab === tab.id}
      onclick={() => switchTab(tab.id)}
    >
      {tab.label}
    </button>
  {/each}
</div>

<!-- ── Tab content ──────────────────────────────────────────────────────── -->
<div class="tab-body">

  <!-- ═══════════════════════ FACTS ═══════════════════════ -->
  {#if activeTab === 'facts'}
    <div class="controls-bar">
      <input
        class="search-input"
        type="text"
        placeholder="Search facts…"
        bind:value={searchFacts}
        onkeydown={(e) => e.key === 'Enter' && (offsetFacts = 0, loadFacts())}
      />
      <button class="btn" onclick={() => { offsetFacts = 0; loadFacts(); }}>Search</button>
    </div>

    {#if loadingFacts}
      <p class="status-msg">Loading…</p>
    {:else if errorFacts}
      <p class="status-msg error">{errorFacts}</p>
    {:else if dataFacts}
      {#if dataFacts.facts.length === 0}
        <p class="empty">No facts found.</p>
      {:else}
        <DataTable
          columns={[
            { key: 'content_short',  label: 'Content' },
            { key: 'category',       label: 'Category' },
            { key: 'subject',        label: 'Subject' },
            { key: 'confidence_fmt', label: 'Confidence' },
            { key: 'active_fmt',     label: 'Active' },
          ]}
          rows={dataFacts.facts.map((f: BrowserFact) => ({
            ...f,
            content_short:  trunc(f.content, 80),
            confidence_fmt: fmtPct(f.confidence),
            active_fmt:     f.active ? 'Yes' : 'No',
          }))}
          rowKey={(r: BrowserFact) => r.id}
        >
          {#snippet detail(row: BrowserFact)}
            <div class="detail-grid">
              <span class="dl">Content</span><span>{row.content}</span>
              <span class="dl">Category</span><span>{row.category ?? '—'}</span>
              <span class="dl">Subject</span><span>{row.subject ?? '—'}</span>
              <span class="dl">Confidence</span><span>{fmtPct(row.confidence)}</span>
              <span class="dl">ID</span><span class="mono">{row.id}</span>
              {#if row.tags?.length}<span class="dl">Tags</span><span>{row.tags.join(', ')}</span>{/if}
              {#if row.event_date}<span class="dl">Event date</span><span>{row.event_date}</span>{/if}
            </div>
          {/snippet}
        </DataTable>

        {@const pi = pageInfo(offsetFacts, dataFacts.total)}
        <div class="pagination">
          <button class="btn" disabled={pi.currentPage <= 1}
            onclick={() => { offsetFacts -= PAGE_SIZE; loadFacts(); }}>Previous</button>
          <span class="page-info">Page {pi.currentPage} of {pi.totalPages} ({dataFacts.total} items)</span>
          <button class="btn" disabled={pi.currentPage >= pi.totalPages}
            onclick={() => { offsetFacts += PAGE_SIZE; loadFacts(); }}>Next</button>
        </div>
      {/if}
    {/if}

  <!-- ═══════════════════════ EPISODES ═══════════════════════ -->
  {:else if activeTab === 'episodes'}
    <div class="controls-bar">
      <input
        class="search-input"
        type="text"
        placeholder="Search episodes…"
        bind:value={searchEpisodes}
        onkeydown={(e) => e.key === 'Enter' && (offsetEpisodes = 0, loadEpisodes())}
      />
      <button class="btn" onclick={() => { offsetEpisodes = 0; loadEpisodes(); }}>Search</button>
    </div>

    {#if loadingEpisodes}
      <p class="status-msg">Loading…</p>
    {:else if errorEpisodes}
      <p class="status-msg error">{errorEpisodes}</p>
    {:else if dataEpisodes}
      {#if dataEpisodes.episodes.length === 0}
        <p class="empty">No episodes found.</p>
      {:else}
        <DataTable
          columns={[
            { key: 'title_fmt',     label: 'Title' },
            { key: 'summary_short', label: 'Summary' },
            { key: 'outcome',       label: 'Outcome' },
            { key: 'started_fmt',   label: 'Started' },
          ]}
          rows={dataEpisodes.episodes.map((e: BrowserEpisode) => ({
            ...e,
            title_fmt:     trunc(e.title ?? '(untitled)', 60),
            summary_short: trunc(e.summary, 80),
            started_fmt:   fmtDate(e.started_at),
          }))}
          rowKey={(r: BrowserEpisode) => r.id}
        >
          {#snippet detail(row: BrowserEpisode)}
            <div class="detail-grid">
              <span class="dl">Title</span><span>{row.title ?? '—'}</span>
              <span class="dl">Summary</span><span>{row.summary}</span>
              <span class="dl">Outcome</span><span>{row.outcome ?? '—'}</span>
              <span class="dl">Started</span><span>{fmtDate(row.started_at)}</span>
              {#if row.structured_summary?.key_points?.length}
                <span class="dl">Key Points</span>
                <span>{(row.structured_summary.key_points as string[]).join('; ')}</span>
              {/if}
              {#if row.structured_summary?.outcome_rationale}
                <span class="dl">Rationale</span><span>{row.structured_summary.outcome_rationale as string}</span>
              {/if}
              {#if row.structured_summary?.lessons}
                <span class="dl">Lessons</span>
                <span>{Array.isArray(row.structured_summary.lessons) ? (row.structured_summary.lessons as string[]).join('; ') : String(row.structured_summary.lessons)}</span>
              {/if}
              {#if row.tags?.length}<span class="dl">Tags</span><span>{row.tags.join(', ')}</span>{/if}
            </div>
          {/snippet}
        </DataTable>

        {@const pi = pageInfo(offsetEpisodes, dataEpisodes.total)}
        <div class="pagination">
          <button class="btn" disabled={pi.currentPage <= 1}
            onclick={() => { offsetEpisodes -= PAGE_SIZE; loadEpisodes(); }}>Previous</button>
          <span class="page-info">Page {pi.currentPage} of {pi.totalPages} ({dataEpisodes.total} items)</span>
          <button class="btn" disabled={pi.currentPage >= pi.totalPages}
            onclick={() => { offsetEpisodes += PAGE_SIZE; loadEpisodes(); }}>Next</button>
        </div>
      {/if}
    {/if}

  <!-- ═══════════════════════ DECISIONS ═══════════════════════ -->
  {:else if activeTab === 'decisions'}
    <div class="controls-bar">
      <input
        class="search-input"
        type="text"
        placeholder="Search decisions…"
        bind:value={searchDecisions}
        onkeydown={(e) => e.key === 'Enter' && (offsetDecisions = 0, loadDecisions())}
      />
      <button class="btn" onclick={() => { offsetDecisions = 0; loadDecisions(); }}>Search</button>
    </div>

    {#if loadingDecisions}
      <p class="status-msg">Loading…</p>
    {:else if errorDecisions}
      <p class="status-msg error">{errorDecisions}</p>
    {:else if dataDecisions}
      {#if dataDecisions.decisions.length === 0}
        <p class="empty">No decisions found.</p>
      {:else}
        <DataTable
          columns={[
            { key: 'desc_short',  label: 'Description' },
            { key: 'category',    label: 'Category' },
            { key: 'stakes',      label: 'Stakes' },
            { key: 'conf_fmt',    label: 'Confidence' },
            { key: 'outcome',     label: 'Outcome' },
            { key: 'created_fmt', label: 'Created' },
          ]}
          rows={dataDecisions.decisions.map((dec: BrowserDecision) => ({
            ...dec,
            desc_short:  trunc(dec.description, 60),
            conf_fmt:    fmtPct(dec.confidence),
            created_fmt: fmtDate(dec.created_at),
          }))}
          rowKey={(r: BrowserDecision) => r.id}
        >
          {#snippet detail(row: BrowserDecision)}
            <div class="detail-grid">
              <span class="dl">Description</span><span>{row.description}</span>
              <span class="dl">Category</span><span>{row.category}</span>
              <span class="dl">Stakes</span><span>{row.stakes}</span>
              <span class="dl">Confidence</span><span>{fmtPct(row.confidence)}</span>
              <span class="dl">Outcome</span><span>{row.outcome}</span>
              {#if row.context}<span class="dl">Context</span><span>{row.context}</span>{/if}
              {#if row.pattern}<span class="dl">Pattern</span><span>{row.pattern}</span>{/if}
              {#if row.reasons?.length}
                <span class="dl">Reasons</span>
                <span>{row.reasons.map((r) => (r.type ?? 'reason') + ': ' + (r.text ?? r.content ?? '')).join('; ')}</span>
              {/if}
              <span class="dl">ID</span><span class="mono">{row.id}</span>
            </div>
          {/snippet}
        </DataTable>

        {@const pi = pageInfo(offsetDecisions, dataDecisions.total)}
        <div class="pagination">
          <button class="btn" disabled={pi.currentPage <= 1}
            onclick={() => { offsetDecisions -= PAGE_SIZE; loadDecisions(); }}>Previous</button>
          <span class="page-info">Page {pi.currentPage} of {pi.totalPages} ({dataDecisions.total} items)</span>
          <button class="btn" disabled={pi.currentPage >= pi.totalPages}
            onclick={() => { offsetDecisions += PAGE_SIZE; loadDecisions(); }}>Next</button>
        </div>
      {/if}
    {/if}

  <!-- ═══════════════════════ PROCEDURES ═══════════════════════ -->
  {:else if activeTab === 'procedures'}
    <div class="controls-bar">
      <input
        class="search-input"
        type="text"
        placeholder="Search procedures…"
        bind:value={searchProcedures}
        onkeydown={(e) => e.key === 'Enter' && (offsetProcedures = 0, loadProcedures())}
      />
      <button class="btn" onclick={() => { offsetProcedures = 0; loadProcedures(); }}>Search</button>
    </div>

    {#if loadingProcedures}
      <p class="status-msg">Loading…</p>
    {:else if errorProcedures}
      <p class="status-msg error">{errorProcedures}</p>
    {:else if dataProcedures}
      {#if dataProcedures.procedures.length === 0}
        <p class="empty">No procedures found.</p>
      {:else}
        <DataTable
          columns={[
            { key: 'name_short', label: 'Name' },
            { key: 'domain',     label: 'Domain' },
            { key: 'activation_count', label: 'Activations' },
            { key: 'eff_fmt',    label: 'Effectiveness' },
            { key: 'last_fmt',   label: 'Last Activated' },
          ]}
          rows={dataProcedures.procedures.map((p: BrowserProcedure) => ({
            ...p,
            name_short: trunc(p.name, 40),
            eff_fmt:    fmtPct(p.effectiveness),
            last_fmt:   fmtDate(p.last_activated),
          }))}
          rowKey={(r: BrowserProcedure) => r.id}
        >
          {#snippet detail(row: BrowserProcedure)}
            <div class="detail-grid">
              <span class="dl">Name</span><span>{row.name}</span>
              <span class="dl">Domain</span><span>{row.domain ?? '—'}</span>
              {#if row.description}<span class="dl">Description</span><span>{row.description}</span>{/if}
              {#if row.goals?.length}<span class="dl">Goals</span><span>{row.goals.join('; ')}</span>{/if}
              {#if row.core_patterns?.length}<span class="dl">Patterns</span><span>{row.core_patterns.join('; ')}</span>{/if}
              {#if row.core_tools?.length}<span class="dl">Tools</span><span>{row.core_tools.join(', ')}</span>{/if}
            </div>
          {/snippet}
        </DataTable>

        {@const pi = pageInfo(offsetProcedures, dataProcedures.total)}
        <div class="pagination">
          <button class="btn" disabled={pi.currentPage <= 1}
            onclick={() => { offsetProcedures -= PAGE_SIZE; loadProcedures(); }}>Previous</button>
          <span class="page-info">Page {pi.currentPage} of {pi.totalPages} ({dataProcedures.total} items)</span>
          <button class="btn" disabled={pi.currentPage >= pi.totalPages}
            onclick={() => { offsetProcedures += PAGE_SIZE; loadProcedures(); }}>Next</button>
        </div>
      {/if}
    {/if}

  <!-- ═══════════════════════ CENSORS ═══════════════════════ -->
  {:else if activeTab === 'censors'}
    <div class="controls-bar">
      <input
        class="search-input"
        type="text"
        placeholder="Search censors…"
        bind:value={searchCensors}
        onkeydown={(e) => e.key === 'Enter' && (offsetCensors = 0, loadCensors())}
      />
      <button class="btn" onclick={() => { offsetCensors = 0; loadCensors(); }}>Search</button>
    </div>

    {#if loadingCensors}
      <p class="status-msg">Loading…</p>
    {:else if errorCensors}
      <p class="status-msg error">{errorCensors}</p>
    {:else if dataCensors}
      {#if dataCensors.censors.length === 0}
        <p class="empty">No censors found.</p>
      {:else}
        <DataTable
          columns={[
            { key: 'trigger_short', label: 'Trigger' },
            { key: 'action',        label: 'Action' },
            { key: 'reason_short',  label: 'Reason' },
            { key: 'domain',        label: 'Domain' },
            { key: 'activation_count', label: 'Activations' },
            { key: 'active_fmt',    label: 'Active' },
          ]}
          rows={dataCensors.censors.map((c: BrowserCensor) => {
            initCensorEdit(c);
            return {
              ...c,
              trigger_short: trunc(c.trigger_pattern, 60),
              reason_short:  trunc(c.reason, 60),
              active_fmt:    c.active ? 'Yes' : 'No',
            };
          })}
          rowKey={(r: BrowserCensor) => r.id}
        >
          {#snippet detail(row: BrowserCensor)}
            <div class="detail-grid">
              <span class="dl">Trigger</span><span>{row.trigger_pattern}</span>
              <span class="dl">Action</span><span>{row.action}</span>
              <span class="dl">Provenance</span><span>{row.provenance}</span>
              <span class="dl">Reason</span><span>{row.reason}</span>
              <span class="dl">Domain</span><span>{row.domain ?? '—'}</span>
              <span class="dl">Activations</span><span>{row.activation_count}</span>
              <span class="dl">False Positives</span><span>{row.false_positive_count}</span>
              <span class="dl">ID</span><span class="mono">{row.id}</span>
            </div>

            <!-- F078: tier dropdown + active toggle + save -->
            <div
              class="censor-edit"
              onclick={(e) => e.stopPropagation()}
              onkeydown={(e) => e.stopPropagation()}
              role="presentation"
            >
              <label class="censor-label">
                Severity
                <select class="censor-select" bind:value={censorEdits[row.id].action}>
                  <option value="steer">steer</option>
                  <option value="refuse">refuse</option>
                  <option value="abort">abort</option>
                </select>
              </label>
              <label class="censor-label">
                <input type="checkbox" bind:checked={censorEdits[row.id].active} />
                Active
              </label>
              <button
                class="btn btn-sm"
                onclick={() => saveCensor(row.id)}
                disabled={censorSaveStatus[row.id] === 'saving'}
              >
                {censorSaveStatus[row.id] === 'saving' ? 'Saving…' : 'Save'}
              </button>
              {#if censorSaveMsg[row.id]}
                <span
                  class="save-status"
                  class:save-ok={censorSaveStatus[row.id] === 'saved'}
                  class:save-err={censorSaveStatus[row.id] === 'error'}
                >
                  {censorSaveMsg[row.id]}
                </span>
              {/if}
            </div>
          {/snippet}
        </DataTable>

        {@const pi = pageInfo(offsetCensors, dataCensors.total)}
        <div class="pagination">
          <button class="btn" disabled={pi.currentPage <= 1}
            onclick={() => { offsetCensors -= PAGE_SIZE; loadCensors(); }}>Previous</button>
          <span class="page-info">Page {pi.currentPage} of {pi.totalPages} ({dataCensors.total} items)</span>
          <button class="btn" disabled={pi.currentPage >= pi.totalPages}
            onclick={() => { offsetCensors += PAGE_SIZE; loadCensors(); }}>Next</button>
        </div>
      {/if}
    {/if}

  <!-- ═══════════════════════ CHUNKS ═══════════════════════ -->
  {:else if activeTab === 'chunks'}
    <div class="controls-bar">
      <input
        class="search-input"
        type="text"
        placeholder="Search chunks…"
        bind:value={searchChunks}
        onkeydown={(e) => e.key === 'Enter' && (offsetChunks = 0, loadChunks())}
      />
      <button class="btn" onclick={() => { offsetChunks = 0; loadChunks(); }}>Search</button>
    </div>

    {#if loadingChunks}
      <p class="status-msg">Loading…</p>
    {:else if errorChunks}
      <p class="status-msg error">{errorChunks}</p>
    {:else if dataChunks}
      {#if dataChunks.chunks.length === 0}
        <p class="empty">No chunks found.</p>
      {:else}
        <DataTable
          columns={[
            { key: 'content_short', label: 'Content' },
            { key: 'chunk_index',   label: 'Idx' },
            { key: 'episode_short', label: 'Episode' },
            { key: 'created_fmt',   label: 'Created' },
          ]}
          rows={dataChunks.chunks.map((c: BrowserChunk) => ({
            ...c,
            content_short: trunc(c.content, 100),
            episode_short: c.episode_id.slice(0, 8),
            created_fmt:   fmtDate(c.created_at),
          }))}
          rowKey={(r: BrowserChunk) => r.id}
        >
          {#snippet detail(row: BrowserChunk)}
            <div class="detail-grid">
              <span class="dl">Content</span><span>{row.content}</span>
              <span class="dl">Chunk Index</span><span>{row.chunk_index}</span>
              <span class="dl">Episode</span><span class="mono">{row.episode_id}</span>
              <span class="dl">Created</span><span>{fmtDate(row.created_at)}</span>
              <span class="dl">ID</span><span class="mono">{row.id}</span>
            </div>
          {/snippet}
        </DataTable>

        {@const pi = pageInfo(offsetChunks, dataChunks.total)}
        <div class="pagination">
          <button class="btn" disabled={pi.currentPage <= 1}
            onclick={() => { offsetChunks -= PAGE_SIZE; loadChunks(); }}>Previous</button>
          <span class="page-info">Page {pi.currentPage} of {pi.totalPages} ({dataChunks.total} items)</span>
          <button class="btn" disabled={pi.currentPage >= pi.totalPages}
            onclick={() => { offsetChunks += PAGE_SIZE; loadChunks(); }}>Next</button>
        </div>
      {/if}
    {/if}
  {/if}

</div>

<style>
  .view-head {
    margin-bottom: 1.25rem;
  }

  h1 {
    font-size: 1.4rem;
    font-weight: 700;
    color: var(--text);
    margin: 0 0 0.125rem;
  }

  .subtitle {
    font-size: 0.8125rem;
    color: var(--muted);
    margin: 0;
  }

  /* ── Tab bar ── */
  .tab-bar {
    display: flex;
    gap: 0.25rem;
    border-bottom: 2px solid var(--border);
    margin-bottom: 1rem;
    flex-wrap: wrap;
  }

  .tab-btn {
    padding: 0.5rem 1rem;
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    margin-bottom: -2px;
    font-size: 0.875rem;
    font-weight: 500;
    color: var(--muted);
    cursor: pointer;
    border-radius: 4px 4px 0 0;
    transition: color 0.15s, border-color 0.15s;
  }

  .tab-btn:hover {
    color: var(--text);
  }

  .tab-btn.active {
    color: var(--accent, #22d3ee);
    border-bottom-color: var(--accent, #22d3ee);
    font-weight: 600;
  }

  /* ── Controls bar ── */
  .controls-bar {
    display: flex;
    gap: 0.5rem;
    margin-bottom: 0.875rem;
    flex-wrap: wrap;
  }

  .search-input {
    flex: 1;
    min-width: 180px;
    padding: 0.4375rem 0.75rem;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm, 6px);
    color: var(--text);
    font-size: 0.875rem;
  }

  .search-input:focus {
    outline: none;
    border-color: var(--accent, #22d3ee);
  }

  /* ── Buttons ── */
  .btn {
    padding: 0.4375rem 0.875rem;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm, 6px);
    color: var(--text);
    font-size: 0.875rem;
    cursor: pointer;
    white-space: nowrap;
  }

  .btn:hover:not(:disabled) {
    border-color: var(--accent, #22d3ee);
    color: var(--accent, #22d3ee);
  }

  .btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .btn-sm {
    padding: 0.3125rem 0.625rem;
    font-size: 0.8125rem;
  }

  /* ── Pagination ── */
  .pagination {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin-top: 0.875rem;
    flex-wrap: wrap;
  }

  .page-info {
    font-size: 0.8125rem;
    color: var(--muted);
  }

  /* ── Status messages ── */
  .status-msg {
    color: var(--muted);
    font-size: 0.9375rem;
    padding: 3rem 2rem;
    text-align: center;
  }

  .status-msg.error {
    color: var(--red, #ef4444);
  }

  .empty {
    color: var(--muted);
    font-size: 0.875rem;
    padding: 2rem 0;
    text-align: center;
  }

  /* ── Detail grid (inside expanded rows) ── */
  :global(.detail-grid) {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 0.25rem 0.875rem;
    font-size: 0.8125rem;
    padding: 0.5rem 0;
  }

  :global(.dl) {
    color: var(--muted);
    font-weight: 600;
    white-space: nowrap;
    align-self: start;
  }

  :global(.mono) {
    font-family: monospace;
    font-size: 0.75rem;
  }

  /* ── Censor write controls ── */
  .censor-edit {
    display: flex;
    align-items: center;
    gap: 0.625rem;
    flex-wrap: wrap;
    margin-top: 0.625rem;
    padding-top: 0.625rem;
    border-top: 1px solid var(--border);
    font-size: 0.8125rem;
  }

  .censor-label {
    display: flex;
    align-items: center;
    gap: 0.375rem;
    color: var(--text);
    cursor: pointer;
  }

  .censor-select {
    padding: 0.25rem 0.5rem;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm, 6px);
    color: var(--text);
    font-size: 0.8125rem;
    cursor: pointer;
  }

  .save-status {
    font-size: 0.75rem;
    color: var(--muted);
  }

  .save-status.save-ok {
    color: #4ade80;
  }

  .save-status.save-err {
    color: #f87171;
  }
</style>
