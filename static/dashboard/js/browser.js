/**
 * Nous Dashboard — Memory Browser View
 *
 * Tabbed interface for browsing facts, episodes, decisions, procedures, censors.
 * Each tab has search, filters, paginated table, click-to-expand rows.
 */

/* global Dashboard, escapeHtml */

Dashboard.registerView('browser', async function(container) {
    container.innerHTML =
        '<div class="view-header"><h1>Memory Browser</h1><p>Search and explore all memory types</p></div>' +
        '<div class="tab-bar" id="browser-tabs">' +
            '<button class="tab-btn active" data-tab="facts">Facts</button>' +
            '<button class="tab-btn" data-tab="episodes">Episodes</button>' +
            '<button class="tab-btn" data-tab="decisions">Decisions</button>' +
            '<button class="tab-btn" data-tab="procedures">Procedures</button>' +
            '<button class="tab-btn" data-tab="censors">Censors</button>' +
        '</div>' +
        '<div id="browser-content"></div>';

    var currentTab = 'facts';
    var tabState = {};

    // Tab switching
    document.getElementById('browser-tabs').addEventListener('click', function(e) {
        var btn = e.target.closest('.tab-btn');
        if (!btn) return;
        document.querySelectorAll('#browser-tabs .tab-btn').forEach(function(b) { b.classList.remove('active'); });
        btn.classList.add('active');
        currentTab = btn.dataset.tab;
        loadTab(currentTab);
    });

    function getState(tab) {
        if (!tabState[tab]) {
            tabState[tab] = { offset: 0, limit: 50, total: 0, search: '', filters: {} };
        }
        return tabState[tab];
    }

    async function loadTab(tab) {
        var content = document.getElementById('browser-content');
        var state = getState(tab);

        // Build controls + loading
        content.innerHTML = buildTabControls(tab, state) +
            '<div id="tab-table-area"><div class="skeleton skeleton-row"></div><div class="skeleton skeleton-row"></div><div class="skeleton skeleton-row"></div></div>' +
            '<div id="tab-pagination"></div>';

        // Wire up controls
        wireControls(tab, state, content);

        // Fetch data
        try {
            var result = await fetchTabData(tab, state);
            renderTable(tab, result, content, state);
        } catch (err) {
            document.getElementById('tab-table-area').innerHTML =
                '<div class="error-banner"><div class="error-icon">&#x26A0;</div>' +
                '<div class="error-msg">Failed to load ' + tab + '.</div></div>';
        }
    }

    function buildTabControls(tab, state) {
        var html = '<div class="controls-bar">';
        html += '<input type="text" class="search-input" id="tab-search" placeholder="Search ' + tab + '..." value="' + escapeHtml(state.search) + '">';

        if (tab === 'facts') {
            html += buildSelect('filter-category', 'Category', ['', 'preference', 'technical', 'person', 'tool', 'concept', 'rule'], state.filters.category);
        } else if (tab === 'episodes') {
            html += buildSelect('filter-outcome', 'Outcome', ['', 'success', 'partial', 'failure', 'pending'], state.filters.outcome);
        } else if (tab === 'decisions') {
            html += buildSelect('filter-category', 'Category', ['', 'architecture', 'process', 'tooling', 'security', 'integration'], state.filters.category);
            html += buildSelect('filter-stakes', 'Stakes', ['', 'low', 'medium', 'high'], state.filters.stakes);
            html += buildSelect('filter-outcome', 'Outcome', ['', 'success', 'partial', 'failure', 'pending'], state.filters.outcome);
        } else if (tab === 'censors') {
            html += buildSelect('filter-action', 'Action', ['', 'warn', 'block', 'absolute'], state.filters.action);
        }

        html += '<button class="btn" id="tab-search-btn">Search</button>';
        html += '</div>';
        return html;
    }

    function buildSelect(id, label, options, selected) {
        var html = '<select class="filter-select" id="' + id + '">';
        options.forEach(function(opt) {
            var lbl = opt ? opt.charAt(0).toUpperCase() + opt.slice(1) : 'All ' + label + 's';
            html += '<option value="' + opt + '"' + (selected === opt ? ' selected' : '') + '>' + lbl + '</option>';
        });
        html += '</select>';
        return html;
    }

    function wireControls(tab, state, content) {
        var searchBtn = document.getElementById('tab-search-btn');
        var searchInput = document.getElementById('tab-search');

        var doSearch = function() {
            state.search = searchInput.value;
            state.offset = 0;

            // Collect filters
            state.filters = {};
            var catSel = document.getElementById('filter-category');
            var outSel = document.getElementById('filter-outcome');
            var stakesSel = document.getElementById('filter-stakes');
            var actionSel = document.getElementById('filter-action');
            if (catSel && catSel.value) state.filters.category = catSel.value;
            if (outSel && outSel.value) state.filters.outcome = outSel.value;
            if (stakesSel && stakesSel.value) state.filters.stakes = stakesSel.value;
            if (actionSel && actionSel.value) state.filters.action = actionSel.value;

            loadTab(tab);
        };

        if (searchBtn) searchBtn.addEventListener('click', doSearch);
        if (searchInput) searchInput.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') doSearch();
        });
    }

    async function fetchTabData(tab, state) {
        var params = 'limit=' + state.limit + '&offset=' + state.offset;

        if (state.search) params += '&q=' + encodeURIComponent(state.search);

        Object.keys(state.filters).forEach(function(k) {
            if (state.filters[k]) params += '&' + k + '=' + encodeURIComponent(state.filters[k]);
        });

        var endpoints = {
            facts: '/facts',
            episodes: '/episodes',
            decisions: '/decisions',
            procedures: '/procedures',
            censors: '/censors'
        };

        return await Dashboard.apiGet(endpoints[tab] + '?' + params);
    }

    function renderTable(tab, result, content, state) {
        var tableArea = document.getElementById('tab-table-area');
        var paginationArea = document.getElementById('tab-pagination');

        var items = result[tab] || result.items || [];
        state.total = result.total || items.length;

        if (items.length === 0) {
            tableArea.innerHTML = '<div class="empty-state"><p>No ' + tab + ' found.</p></div>';
            paginationArea.innerHTML = '';
            return;
        }

        var tableHtml = '<div class="data-table-wrap"><table class="data-table"><thead><tr>';
        var columns = getColumns(tab);
        columns.forEach(function(col) {
            tableHtml += '<th>' + col.label + '</th>';
        });
        tableHtml += '</tr></thead><tbody>';

        items.forEach(function(item, idx) {
            var rowId = 'row-' + tab + '-' + idx;
            tableHtml += '<tr class="data-row" data-row-id="' + rowId + '">';
            columns.forEach(function(col) {
                tableHtml += '<td class="' + (col.cls || '') + '">' + formatCell(col, item) + '</td>';
            });
            tableHtml += '</tr>';
            // Expansion row
            tableHtml += '<tr class="row-detail" id="' + rowId + '"><td colspan="' + columns.length + '">' +
                buildDetailContent(tab, item) + '</td></tr>';
        });

        tableHtml += '</tbody></table></div>';
        tableArea.innerHTML = tableHtml;

        // Wire row clicks
        tableArea.querySelectorAll('.data-row').forEach(function(row) {
            row.addEventListener('click', function() {
                var detailRow = document.getElementById(this.dataset.rowId);
                if (detailRow) detailRow.classList.toggle('expanded');
            });
        });

        // Pagination
        var totalPages = Math.ceil(state.total / state.limit);
        var currentPage = Math.floor(state.offset / state.limit) + 1;

        paginationArea.innerHTML = '<div class="pagination">' +
            '<button class="btn" id="page-prev"' + (currentPage <= 1 ? ' disabled' : '') + '>Previous</button>' +
            '<span>Page ' + currentPage + ' of ' + totalPages + ' (' + state.total + ' items)</span>' +
            '<button class="btn" id="page-next"' + (currentPage >= totalPages ? ' disabled' : '') + '>Next</button>' +
            '</div>';

        document.getElementById('page-prev').addEventListener('click', function() {
            if (state.offset >= state.limit) {
                state.offset -= state.limit;
                loadTab(currentTab);
            }
        });
        document.getElementById('page-next').addEventListener('click', function() {
            if (state.offset + state.limit < state.total) {
                state.offset += state.limit;
                loadTab(currentTab);
            }
        });
    }

    function getColumns(tab) {
        switch (tab) {
            case 'facts':
                return [
                    { key: 'content', label: 'Content', cls: 'cell-truncate', truncate: 80 },
                    { key: 'category', label: 'Category', badge: 'badge-fact' },
                    { key: 'subject', label: 'Subject' },
                    { key: 'confidence', label: 'Confidence', format: 'percent' },
                    { key: 'active', label: 'Active', format: 'boolean' }
                ];
            case 'episodes':
                return [
                    { key: 'title', label: 'Title', cls: 'cell-truncate', truncate: 60 },
                    { key: 'summary', label: 'Summary', cls: 'cell-truncate', truncate: 80 },
                    { key: 'outcome', label: 'Outcome', format: 'outcome' },
                    { key: 'started_at', label: 'Started', format: 'date' }
                ];
            case 'decisions':
                return [
                    { key: 'description', label: 'Description', cls: 'cell-truncate', truncate: 60 },
                    { key: 'category', label: 'Category', badge: 'badge-decision' },
                    { key: 'stakes', label: 'Stakes', format: 'stakes' },
                    { key: 'confidence', label: 'Confidence', format: 'percent' },
                    { key: 'outcome', label: 'Outcome', format: 'outcome' },
                    { key: 'created_at', label: 'Created', format: 'date' }
                ];
            case 'procedures':
                return [
                    { key: 'name', label: 'Name', cls: 'cell-truncate', truncate: 40 },
                    { key: 'domain', label: 'Domain' },
                    { key: 'activation_count', label: 'Activations' },
                    { key: 'success_rate', label: 'Success Rate', format: 'percent' },
                    { key: 'last_activated', label: 'Last Activated', format: 'date' }
                ];
            case 'censors':
                return [
                    { key: 'trigger_pattern', label: 'Trigger', cls: 'cell-truncate', truncate: 60 },
                    { key: 'action', label: 'Action', format: 'action' },
                    { key: 'reason', label: 'Reason', cls: 'cell-truncate', truncate: 60 },
                    { key: 'domain', label: 'Domain' },
                    { key: 'activation_count', label: 'Activations' },
                    { key: 'active', label: 'Active', format: 'boolean' }
                ];
            default:
                return [];
        }
    }

    function formatCell(col, item) {
        var val = item[col.key];
        if (val == null || val === '') return '<span class="cell-muted">-</span>';

        if (col.truncate) val = Dashboard.truncate(String(val), col.truncate);

        if (col.format === 'percent') {
            var pct = typeof val === 'number' ? (val <= 1 ? (val * 100).toFixed(0) : val.toFixed(0)) : val;
            return pct + '%';
        }
        if (col.format === 'date') return Dashboard.formatDate(val);
        if (col.format === 'boolean') return val ? '<span class="text-green">Yes</span>' : '<span class="text-muted">No</span>';
        if (col.format === 'outcome') return '<span class="badge ' + Dashboard.outcomeBadge(val) + '">' + escapeHtml(val) + '</span>';
        if (col.format === 'stakes') {
            var cls = val === 'high' ? 'badge-high' : val === 'medium' ? 'badge-medium' : 'badge-low';
            return '<span class="badge ' + cls + '">' + escapeHtml(val) + '</span>';
        }
        if (col.format === 'action') {
            var actionCls = val === 'block' || val === 'absolute' ? 'badge-censor' : 'badge-partial';
            return '<span class="badge ' + actionCls + '">' + escapeHtml(val) + '</span>';
        }
        if (col.badge) return '<span class="badge ' + col.badge + '">' + escapeHtml(String(val)) + '</span>';

        return escapeHtml(String(val));
    }

    function buildDetailContent(tab, item) {
        var html = '<div class="detail-grid">';

        switch (tab) {
            case 'facts':
                html += detailRow('Content', item.content);
                html += detailRow('Category', item.category);
                html += detailRow('Subject', item.subject);
                html += detailRow('Confidence', item.confidence != null ? (item.confidence * 100).toFixed(0) + '%' : '-');
                html += detailRow('ID', '<span class="mono">' + escapeHtml(item.id || '') + '</span>');
                if (item.tags && item.tags.length) html += detailRow('Tags', item.tags.join(', '));
                break;
            case 'episodes':
                html += detailRow('Title', item.title);
                html += detailRow('Summary', item.summary);
                html += detailRow('Outcome', item.outcome);
                html += detailRow('Started', Dashboard.formatDateTime(item.started_at));
                if (item.structured_summary) {
                    var ss = item.structured_summary;
                    if (ss.key_points) html += detailRow('Key Points', Array.isArray(ss.key_points) ? ss.key_points.join('; ') : String(ss.key_points));
                    if (ss.outcome_rationale) html += detailRow('Rationale', ss.outcome_rationale);
                    if (ss.lessons) html += detailRow('Lessons', Array.isArray(ss.lessons) ? ss.lessons.join('; ') : String(ss.lessons));
                }
                if (item.tags && item.tags.length) html += detailRow('Tags', item.tags.join(', '));
                break;
            case 'decisions':
                html += detailRow('Description', item.description);
                html += detailRow('Category', item.category);
                html += detailRow('Stakes', item.stakes);
                html += detailRow('Confidence', item.confidence != null ? (item.confidence * 100).toFixed(0) + '%' : '-');
                html += detailRow('Outcome', item.outcome);
                html += detailRow('Context', item.context);
                html += detailRow('Pattern', item.pattern);
                if (item.reasons && item.reasons.length) {
                    var reasonStr = item.reasons.map(function(r) { return (r.type || 'reason') + ': ' + (r.text || r.content || ''); }).join('; ');
                    html += detailRow('Reasons', reasonStr);
                }
                html += detailRow('ID', '<span class="mono">' + escapeHtml(item.id || '') + '</span>');
                break;
            case 'procedures':
                html += detailRow('Name', item.name);
                html += detailRow('Domain', item.domain);
                html += detailRow('Description', item.description);
                if (item.goals) html += detailRow('Goals', Array.isArray(item.goals) ? item.goals.join('; ') : String(item.goals));
                if (item.core_patterns) html += detailRow('Patterns', Array.isArray(item.core_patterns) ? item.core_patterns.join('; ') : String(item.core_patterns));
                if (item.core_tools) html += detailRow('Tools', Array.isArray(item.core_tools) ? item.core_tools.join(', ') : String(item.core_tools));
                break;
            case 'censors':
                html += detailRow('Trigger', item.trigger_pattern);
                html += detailRow('Action', item.action);
                html += detailRow('Reason', item.reason);
                html += detailRow('Domain', item.domain);
                html += detailRow('Activations', item.activation_count);
                html += detailRow('False Positives', item.false_positive_count);
                html += detailRow('ID', '<span class="mono">' + escapeHtml(item.id || '') + '</span>');
                break;
        }

        html += '</div>';
        return html;
    }

    function detailRow(label, value) {
        if (value == null || value === '') return '';
        return '<div class="detail-label">' + escapeHtml(label) + '</div>' +
               '<div class="detail-value">' + (typeof value === 'string' && value.startsWith('<') ? value : escapeHtml(String(value))) + '</div>';
    }

    // Load initial tab
    loadTab('facts');
});
