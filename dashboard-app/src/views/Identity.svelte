<script lang="ts">
  import { onMount } from 'svelte';
  import { apiGet, apiSend } from '../lib/api';
  import DataTable from '../lib/ui/DataTable.svelte';
  import type {
    IdentityResponse,
    ProfileFactsResponse,
    FactUpdateResponse,
    BrowserFact,
  } from '../lib/types/api';

  // ── Panel 1: Agent Identity ───────────────────────────────────────────────

  // Order matches nous/identity/manager.py SECTIONS (status is internal, excluded).
  const SECTIONS = ['character', 'values', 'protocols', 'preferences', 'boundaries', 'environment'];

  // Drafts bound to the textareas; originals track the last-loaded value so Save
  // can gate on "unchanged". Both seeded from GET /identity (''.for absent sections).
  let sectionDrafts = $state<Record<string, string>>({});
  let sectionOriginal = $state<Record<string, string>>({});
  let sectionStatus = $state<Record<string, 'idle' | 'saving' | 'saved' | 'error'>>({});
  let sectionMsg = $state<Record<string, string>>({});
  let identityError = $state<string | null>(null);

  async function loadIdentity() {
    identityError = null;
    try {
      const data = await apiGet<IdentityResponse>('/identity');
      for (const name of SECTIONS) {
        const val = data.sections[name] ?? '';
        sectionDrafts[name] = val;
        sectionOriginal[name] = val;
      }
    } catch (e) {
      identityError = e instanceof Error ? e.message : 'Failed to load identity';
    }
  }

  async function saveSection(name: string) {
    const content = (sectionDrafts[name] ?? '').trim();
    if (!content) return;
    if (!confirm(`Overwrite the "${name}" identity section? There is no undo UI.`)) return;
    sectionStatus[name] = 'saving';
    sectionMsg[name] = 'Saving…';
    try {
      await apiSend(`/identity/${name}`, { content, updated_by: 'dashboard' });
      sectionStatus[name] = 'saved';
      sectionMsg[name] = 'Saved';
      await loadIdentity(); // refresh originals so Save re-gates on unchanged
    } catch (e) {
      sectionStatus[name] = 'error';
      sectionMsg[name] = e instanceof Error ? e.message : 'Error';
    }
  }

  // ── Panel 2: User Profile Facts (Tier-1) ──────────────────────────────────

  let includeInactive = $state(false);
  let factsData = $state<ProfileFactsResponse | null>(null);
  let loadingFacts = $state(false);
  let errorFacts = $state<string | null>(null);

  // Per-row edit state (Browser.svelte censor idiom).
  let factDrafts = $state<Record<string, string>>({});
  let factStatus = $state<Record<string, 'idle' | 'saving' | 'saved' | 'error'>>({});
  let factMsg = $state<Record<string, string>>({});
  // Panel-level notice — used for the merged_into_existing case, where the row is
  // about to disappear so a per-row message would vanish with it.
  let factsPanelMsg = $state<string | null>(null);

  function truncate(s: string, n: number): string {
    if (!s) return '';
    return s.length > n ? s.slice(0, n) + '…' : s;
  }

  async function loadFacts() {
    loadingFacts = true;
    errorFacts = null;
    try {
      const params = new URLSearchParams({ limit: '200' });
      if (includeInactive) params.set('active', 'false');
      factsData = await apiGet<ProfileFactsResponse>(`/profile/facts?${params}`);
    } catch (e) {
      errorFacts = e instanceof Error ? e.message : 'Failed to load facts';
    } finally {
      loadingFacts = false;
    }
  }

  // Precompute display fields — DataTable renders row[c.key] verbatim (no formatters).
  const factRows = $derived(
    (factsData?.facts ?? []).map((f: BrowserFact) => ({
      ...f,
      content_display: truncate(f.content, 120),
      confidence_display: f.confidence.toFixed(2),
      active_display: f.active ? 'yes' : 'no',
    })),
  );

  function initFactDraft(row: BrowserFact) {
    if (factDrafts[row.id] === undefined) factDrafts[row.id] = row.content;
  }

  // Clear per-row edit state after a mutation — never leave a row editable against
  // a retired id (edit gives the fact a NEW id; deactivate removes it). Reloading
  // the list re-keys DataTable's rows, which collapses the stale expanded row.
  function clearFactRow(id: string) {
    delete factDrafts[id];
    delete factStatus[id];
    delete factMsg[id];
  }

  async function saveFact(row: BrowserFact) {
    const draft = (factDrafts[row.id] ?? '').trim();
    if (!draft) return;
    factStatus[row.id] = 'saving';
    factMsg[row.id] = 'Saving…';
    factsPanelMsg = null;
    try {
      const resp = await apiSend<FactUpdateResponse>(
        `/facts/${row.id}`,
        { content: draft, subject: row.subject, confidence: row.confidence },
        'PUT',
      );
      if (resp.status === 'merged_into_existing') {
        factsPanelMsg =
          'Your edit matched an existing fact — the old entry was retired and linked to it. Stored: "' +
          truncate(resp.stored_content ?? '', 120) +
          '"';
      }
      clearFactRow(row.id);
      await loadFacts();
    } catch (e) {
      factStatus[row.id] = 'error';
      factMsg[row.id] = e instanceof Error ? e.message : 'Error';
    }
  }

  async function deactivateFact(row: BrowserFact) {
    if (!confirm('Deactivate this fact? It will be soft-deleted and no longer shown to the agent.')) return;
    factStatus[row.id] = 'saving';
    factMsg[row.id] = 'Deactivating…';
    factsPanelMsg = null;
    try {
      await apiSend(`/facts/${row.id}`, undefined, 'DELETE');
      clearFactRow(row.id);
      await loadFacts();
    } catch (e) {
      factStatus[row.id] = 'error';
      factMsg[row.id] = e instanceof Error ? e.message : 'Error';
    }
  }

  onMount(() => {
    loadIdentity();
    loadFacts();
  });
</script>

<header class="view-head">
  <div>
    <h1>Identity</h1>
    <p class="subtitle">Agent identity sections and Tier-1 user-profile facts</p>
  </div>
</header>

<!-- ═══════════════════════ AGENT IDENTITY ═══════════════════════ -->
<section class="panel">
  <h2>Agent Identity</h2>

  {#if identityError}
    <p class="status-msg error">{identityError}</p>
  {/if}

  <div class="section-grid">
    {#each SECTIONS as name}
      <div class="section-card">
        <label class="section-label" for={`identity-${name}`}>{name}</label>
        <textarea
          id={`identity-${name}`}
          rows={8}
          bind:value={sectionDrafts[name]}
        ></textarea>
        <div class="card-actions">
          <button
            class="btn btn-sm"
            onclick={() => saveSection(name)}
            disabled={
              sectionStatus[name] === 'saving' ||
              !(sectionDrafts[name] ?? '').trim() ||
              (sectionDrafts[name] ?? '') === (sectionOriginal[name] ?? '')
            }
          >
            {sectionStatus[name] === 'saving' ? 'Saving…' : 'Save'}
          </button>
          {#if sectionMsg[name]}
            <span
              class="save-status"
              class:save-ok={sectionStatus[name] === 'saved'}
              class:save-err={sectionStatus[name] === 'error'}
            >
              {sectionMsg[name]}
            </span>
          {/if}
        </div>
      </div>
    {/each}
  </div>

  <p class="info-note">
    Identity edits reach the agent's prompt within ~60s. Editing <strong>preferences</strong>
    also changes which User Profile facts below are shown to the agent (overlap dedup).
  </p>
</section>

<!-- ═══════════════════════ USER PROFILE FACTS ═══════════════════════ -->
<section class="panel">
  <h2>User Profile Facts (Tier-1)</h2>

  <div class="controls-bar">
    <label class="check-label">
      <input
        type="checkbox"
        bind:checked={includeInactive}
        onchange={() => loadFacts()}
      />
      Include inactive
    </label>
  </div>

  {#if factsPanelMsg}
    <p class="info-note info-highlight">{factsPanelMsg}</p>
  {/if}

  {#if loadingFacts}
    <p class="status-msg">Loading…</p>
  {:else if errorFacts}
    <p class="status-msg error">{errorFacts}</p>
  {:else if factsData}
    {#if factRows.length === 0}
      <p class="empty">No Tier-1 profile facts found.</p>
    {:else}
      <DataTable
        columns={[
          { key: 'subject',            label: 'Subject' },
          { key: 'category',           label: 'Category' },
          { key: 'content_display',    label: 'Content' },
          { key: 'confidence_display', label: 'Confidence' },
          { key: 'active_display',     label: 'Active' },
        ]}
        rows={factRows}
        rowKey={(r: BrowserFact) => r.id}
        onrowclick={(row: BrowserFact) => initFactDraft(row)}
      >
        {#snippet detail(row: BrowserFact)}
          <div class="detail-grid">
            <span class="dl">Subject</span><span>{row.subject ?? '—'}</span>
            <span class="dl">Category</span><span>{row.category ?? '—'}</span>
            <span class="dl">Confidence</span><span>{row.confidence.toFixed(2)}</span>
            <span class="dl">Active</span><span>{row.active ? 'yes' : 'no'}</span>
            <span class="dl">ID</span><span class="mono">{row.id}</span>
          </div>

          <!-- Edit = supersede (new versioned fact); Deactivate = soft delete -->
          <div
            class="fact-edit"
            onclick={(e) => e.stopPropagation()}
            onkeydown={(e) => e.stopPropagation()}
            role="presentation"
          >
            <textarea
              class="fact-textarea"
              rows={4}
              bind:value={factDrafts[row.id]}
            ></textarea>
            <div class="fact-actions">
              <button
                class="btn btn-sm"
                onclick={() => saveFact(row)}
                disabled={factStatus[row.id] === 'saving' || !(factDrafts[row.id] ?? '').trim()}
              >
                {factStatus[row.id] === 'saving' ? 'Saving…' : 'Save'}
              </button>
              <button
                class="btn btn-sm btn-danger"
                onclick={() => deactivateFact(row)}
                disabled={factStatus[row.id] === 'saving'}
              >
                Deactivate
              </button>
              {#if factMsg[row.id]}
                <span
                  class="save-status"
                  class:save-ok={factStatus[row.id] === 'saved'}
                  class:save-err={factStatus[row.id] === 'error'}
                >
                  {factMsg[row.id]}
                </span>
              {/if}
            </div>
          </div>
        {/snippet}
      </DataTable>
    {/if}
  {/if}

  <p class="info-note">
    Editing a fact <strong>supersedes</strong> it: a new versioned fact is created with a new id
    and re-embedded, and the old one is retired. Deactivate <strong>soft-deletes</strong> it.
    Both changes reach the agent on its next turn.
  </p>
</section>

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

  /* ── Panels ── */
  .panel {
    margin-bottom: 2rem;
  }

  h2 {
    font-size: 1.05rem;
    font-weight: 600;
    color: var(--text);
    margin: 0 0 0.875rem;
  }

  /* ── Identity section cards ── */
  .section-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: 1rem;
  }

  .section-card {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    padding: 0.875rem;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm, 6px);
  }

  .section-label {
    font-size: 0.75rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--muted);
  }

  textarea {
    width: 100%;
    box-sizing: border-box;
    padding: 0.5rem 0.625rem;
    background: var(--bg, var(--surface));
    border: 1px solid var(--border);
    border-radius: var(--radius-sm, 6px);
    color: var(--text);
    font-family: inherit;
    font-size: 0.8125rem;
    line-height: 1.5;
    resize: vertical;
  }

  textarea:focus {
    outline: none;
    border-color: var(--accent, #22d3ee);
  }

  .card-actions {
    display: flex;
    align-items: center;
    gap: 0.625rem;
  }

  /* ── Controls bar ── */
  .controls-bar {
    display: flex;
    gap: 0.75rem;
    margin-bottom: 0.875rem;
    flex-wrap: wrap;
  }

  .check-label {
    display: flex;
    align-items: center;
    gap: 0.375rem;
    font-size: 0.8125rem;
    color: var(--text);
    cursor: pointer;
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

  .btn-danger:hover:not(:disabled) {
    border-color: var(--red, #ef4444);
    color: var(--red, #ef4444);
  }

  /* ── Info notes ── */
  .info-note {
    font-size: 0.8125rem;
    color: var(--muted);
    margin: 0.875rem 0 0;
    line-height: 1.5;
  }

  .info-highlight {
    color: var(--text);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm, 6px);
    padding: 0.625rem 0.75rem;
    margin-bottom: 0.875rem;
  }

  /* ── Status messages ── */
  .status-msg {
    color: var(--muted);
    font-size: 0.9375rem;
    padding: 2rem;
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

  /* ── Fact edit controls ── */
  .fact-edit {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    margin-top: 0.625rem;
    padding-top: 0.625rem;
    border-top: 1px solid var(--border);
  }

  .fact-textarea {
    font-size: 0.8125rem;
  }

  .fact-actions {
    display: flex;
    align-items: center;
    gap: 0.625rem;
    flex-wrap: wrap;
  }
</style>
