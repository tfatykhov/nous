/**
 * Nous Dashboard — Execution Ledger View (F032)
 *
 * Displays per-session execution details: tool calls, statuses,
 * side-effect classifications, and action gating results.
 */

/* global Dashboard, escapeHtml */

var _ledgerRefreshInterval = null;

Dashboard.registerView('execution', async function (container) {
    Dashboard.showLoading(container);

    try {
        var data = await Dashboard.apiGet('/dashboard/ledger');
        renderLedger(container, data);
        startAutoRefresh(container);
    } catch (err) {
        Dashboard.showError(container, 'Failed to load execution ledger.', function () {
            Dashboard.reloadView('execution');
        });
    }
});

function startAutoRefresh(container) {
    stopAutoRefresh();
    _ledgerRefreshInterval = setInterval(async function () {
        if (Dashboard.currentView !== 'execution') {
            stopAutoRefresh();
            return;
        }
        try {
            var data = await Dashboard.apiGet('/dashboard/ledger');
            // Preserve expanded state
            var expanded = {};
            container.querySelectorAll('.ledger-session.expanded').forEach(function (el) {
                var sid = el.dataset.sessionId;
                if (sid) expanded[sid] = true;
            });
            var activeFilter = {};
            var activeEffectFilter = {};
            container.querySelectorAll('.ledger-filter-bar').forEach(function (bar) {
                var sid = bar.closest('.ledger-session').dataset.sessionId;
                if (!sid) return;
                var statusBtn = bar.querySelector('.filter-btn:not(.effect-filter).active');
                if (statusBtn) activeFilter[sid] = statusBtn.dataset.filter;
                var effectBtn = bar.querySelector('.filter-btn.effect-filter.active');
                if (effectBtn) activeEffectFilter[sid] = effectBtn.dataset.effectFilter;
            });
            renderLedger(container, data, expanded, activeFilter, activeEffectFilter);
        } catch (err) {
            // Silently skip refresh on error
        }
    }, 15000);
}

function stopAutoRefresh() {
    if (_ledgerRefreshInterval) {
        clearInterval(_ledgerRefreshInterval);
        _ledgerRefreshInterval = null;
    }
}

function renderLedger(container, data, expandedState, filterState, effectFilterState) {
    expandedState = expandedState || {};
    filterState = filterState || {};
    effectFilterState = effectFilterState || {};

    var enabled = data.enabled || {};
    var modes = data.modes || {};
    var sessions = data.sessions || [];

    // Aggregate totals across sessions
    var totalActions = 0, totalBlocked = 0, totalErrors = 0, totalSessions = sessions.length;
    sessions.forEach(function (s) {
        totalActions += s.total_actions || 0;
        totalBlocked += s.blocked_actions || 0;
        totalErrors += s.error_actions || 0;
    });

    var html = '<div class="view-header">' +
        '<h1>Execution Ledger</h1>' +
        '<p class="view-subtitle">F026 — Real-time tool execution tracking and action gating</p>' +
        '</div>';

    // Stat cards
    html += '<div class="stat-grid">';
    html += buildStatCard('Active Sessions', totalSessions, null, 'var(--muted)');
    html += buildStatCard('Total Actions', totalActions, null, '#60a5fa');
    html += buildIndicatorCard('Blocked', totalBlocked, totalBlocked > 0 ? 'bad' : 'good');
    html += buildIndicatorCard('Errors', totalErrors, totalErrors > 0 ? 'warn' : 'good');
    html += buildModeCard('Claim Verification', modes.claim_verification || 'off', !enabled.claim_verification);
    html += buildModeCard('Action Gating', modes.action_gating || 'off', !enabled.action_gating);
    html += '</div>';

    // Sessions list
    if (sessions.length === 0) {
        html += '<div class="empty-state">' +
            '<div class="empty-icon">&#x1D6B9;</div>' +
            '<h3>No Active Sessions</h3>' +
            '<p>Execution data will appear here when sessions are active. Data is auto-refreshed every 15 seconds.</p>' +
            '</div>';
    } else {
        html += '<div class="section-header"><h2>Sessions</h2></div>';
        html += '<div class="ledger-sessions">';
        sessions.forEach(function (session) {
            var isExpanded = expandedState[session.session_id] || false;
            html += buildSessionCard(session, isExpanded, filterState[session.session_id] || 'all', effectFilterState[session.session_id] || 'all-effects');
        });
        html += '</div>';
    }

    container.innerHTML = html;

    // Bind expand/collapse handlers
    container.querySelectorAll('.ledger-session-header').forEach(function (header) {
        header.addEventListener('click', function () {
            var card = header.closest('.ledger-session');
            card.classList.toggle('expanded');
            var detail = card.querySelector('.ledger-session-detail');
            if (detail) detail.style.display = card.classList.contains('expanded') ? 'block' : 'none';
        });
    });

    // Bind status filter handlers
    container.querySelectorAll('.filter-btn:not(.effect-filter)').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var bar = btn.closest('.ledger-filter-bar');
            bar.querySelectorAll('.filter-btn:not(.effect-filter)').forEach(function (b) { b.classList.remove('active'); });
            btn.classList.add('active');
            var sessionCard = btn.closest('.ledger-session');
            applyFilters(sessionCard);
        });
    });

    // Bind effect filter handlers
    container.querySelectorAll('.filter-btn.effect-filter').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var bar = btn.closest('.ledger-filter-bar');
            bar.querySelectorAll('.filter-btn.effect-filter').forEach(function (b) { b.classList.remove('active'); });
            btn.classList.add('active');
            var sessionCard = btn.closest('.ledger-session');
            applyFilters(sessionCard);
        });
    });
}

function buildSessionCard(session, isExpanded, activeFilter, activeEffectFilter) {
    var sid = session.session_id;
    var shortId = sid.length > 16 ? sid.slice(0, 16) + '\u2026' : sid;

    var html = '<div class="ledger-session' + (isExpanded ? ' expanded' : '') + '" data-session-id="' + escapeHtml(sid) + '">';

    // Header
    html += '<div class="ledger-session-header">';
    html += '<div class="ledger-session-id"><code>' + escapeHtml(shortId) + '</code></div>';
    html += '<div class="ledger-session-meta">';
    html += '<span class="ledger-badge badge-turn">Turn ' + (session.current_turn || 0) + '</span>';
    html += '<span class="ledger-badge badge-actions">' + (session.total_actions || 0) + ' actions</span>';
    if (session.blocked_actions > 0) {
        html += '<span class="ledger-badge badge-blocked">' + session.blocked_actions + ' blocked</span>';
    }
    if (session.error_actions > 0) {
        html += '<span class="ledger-badge badge-error">' + session.error_actions + ' errors</span>';
    }
    if (session.actions_truncated) {
        html += '<span class="ledger-badge badge-truncated">truncated</span>';
    }
    html += '</div>';
    html += '<div class="ledger-session-summary text-muted">' + escapeHtml(session.summary || '') + '</div>';
    html += '<svg class="ledger-expand-icon" viewBox="0 0 20 20" fill="currentColor" width="16" height="16"><path fill-rule="evenodd" d="M5.293 7.293a1 1 0 011.414 0L10 10.586l3.293-3.293a1 1 0 111.414 1.414l-4 4a1 1 0 01-1.414 0l-4-4a1 1 0 010-1.414z" clip-rule="evenodd"/></svg>';
    html += '</div>';

    // Detail (hidden unless expanded)
    html += '<div class="ledger-session-detail" style="display:' + (isExpanded ? 'block' : 'none') + '">';

    // Filter bar
    html += '<div class="ledger-filter-bar">';
    var filters = ['all', 'success', 'blocked', 'error', 'timeout'];
    filters.forEach(function (f) {
        var cls = f === activeFilter ? ' active' : '';
        html += '<button class="filter-btn' + cls + '" data-filter="' + f + '">' + f + '</button>';
    });

    // Side-effect filter
    html += '<span class="filter-separator">|</span>';
    var sideEffects = ['all-effects', 'none', 'write', 'external', 'irreversible'];
    sideEffects.forEach(function (f) {
        var label = f === 'all-effects' ? 'all effects' : f;
        var cls = f === (activeEffectFilter || 'all-effects') ? ' active' : '';
        html += '<button class="filter-btn effect-filter' + cls + '" data-effect-filter="' + f + '">' + label + '</button>';
    });
    html += '<span class="ledger-filter-count"></span>';
    html += '</div>';

    // Actions grouped by turn
    var actions = session.actions || [];
    var turnGroups = {};
    actions.forEach(function (a) {
        var t = a.turn;
        if (!turnGroups[t]) turnGroups[t] = [];
        turnGroups[t].push(a);
    });

    var turnNums = Object.keys(turnGroups).map(Number).sort(function (a, b) { return a - b; });

    if (turnNums.length === 0) {
        html += '<div class="text-muted" style="padding:12px">No actions recorded.</div>';
    } else {
        turnNums.forEach(function (turn) {
            html += '<div class="ledger-turn-group">';
            html += '<div class="ledger-turn-label">Turn ' + turn + '</div>';
            turnGroups[turn].forEach(function (a) {
                html += buildActionRow(a);
            });
            html += '</div>';
        });
    }

    html += '</div>'; // session-detail
    html += '</div>'; // session card

    return html;
}

function buildActionRow(action) {
    var statusClass = 'status-' + (action.status || 'success');
    var effectClass = action.side_effect_type !== 'none' ? 'effect-' + action.side_effect_type : '';

    var html = '<div class="ledger-action ' + statusClass + '" data-status="' + escapeHtml(action.status || '') + '" data-effect="' + escapeHtml(action.side_effect_type || 'none') + '">';

    // Tool name
    html += '<span class="ledger-tool-name">' + escapeHtml(action.tool_name) + '</span>';

    // Key args
    var args = action.key_args || {};
    var argParts = Object.keys(args).map(function (k) {
        return escapeHtml(k) + '=' + escapeHtml(Dashboard.truncate(args[k], 60));
    });
    if (argParts.length > 0) {
        html += '<span class="ledger-args text-muted">' + argParts.join(' ') + '</span>';
    }

    // Badges
    html += '<span class="ledger-status-badge ' + statusClass + '">' + escapeHtml(action.status) + '</span>';

    if (action.side_effect_type && action.side_effect_type !== 'none') {
        html += '<span class="ledger-effect-badge ' + effectClass + '">' + escapeHtml(action.side_effect_type) + '</span>';
    }

    // Timestamp
    if (action.timestamp) {
        var ts = new Date(action.timestamp);
        html += '<span class="ledger-timestamp text-muted">' + ts.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) + '</span>';
    }

    // Result summary for non-success
    if (action.status !== 'success' && action.result_summary) {
        html += '<div class="ledger-result-summary">' + escapeHtml(action.result_summary) + '</div>';
    }

    html += '</div>';
    return html;
}

function applyFilters(sessionCard) {
    var bar = sessionCard.querySelector('.ledger-filter-bar');
    var statusBtn = bar ? bar.querySelector('.filter-btn:not(.effect-filter).active') : null;
    var effectBtn = bar ? bar.querySelector('.filter-btn.effect-filter.active') : null;
    var statusFilter = statusBtn ? statusBtn.dataset.filter : 'all';
    var effectFilter = effectBtn ? effectBtn.dataset.effectFilter : 'all-effects';

    var actions = sessionCard.querySelectorAll('.ledger-action');
    var shown = 0, total = actions.length;
    actions.forEach(function (el) {
        var matchStatus = statusFilter === 'all' || el.dataset.status === statusFilter;
        var matchEffect = effectFilter === 'all-effects' || el.dataset.effect === effectFilter;
        var visible = matchStatus && matchEffect;
        el.style.display = visible ? '' : 'none';
        if (visible) shown++;
    });
    var countEl = sessionCard.querySelector('.ledger-filter-count');
    if (countEl) {
        countEl.textContent = shown < total ? 'Showing ' + shown + ' of ' + total + ' actions' : '';
    }
}

// Helper cards (matching overview.js patterns)
function buildStatCard(label, value, delta, color) {
    return '<div class="stat-card">' +
        '<div class="stat-value" style="color:' + (color || 'inherit') + '">' + Dashboard.formatNumber(value) + '</div>' +
        '<div class="stat-label">' + escapeHtml(label) + '</div>' +
        '</div>';
}

function buildIndicatorCard(label, value, cls) {
    return '<div class="stat-card">' +
        '<div class="stat-value"><span class="stat-indicator ' + cls + '"></span>' + Dashboard.formatNumber(value) + '</div>' +
        '<div class="stat-label">' + escapeHtml(label) + '</div>' +
        '</div>';
}

function buildModeCard(label, mode, disabled) {
    if (disabled) {
        return '<div class="stat-card">' +
            '<div class="stat-value" style="color:var(--muted)">off</div>' +
            '<div class="stat-label">' + escapeHtml(label) + '</div>' +
            '</div>';
    }
    var color = mode === 'enforce' ? 'var(--green)' : mode === 'warn' ? 'var(--yellow)' : 'var(--muted)';
    return '<div class="stat-card">' +
        '<div class="stat-value" style="color:' + color + '">' + escapeHtml(mode) + '</div>' +
        '<div class="stat-label">' + escapeHtml(label) + '</div>' +
        '</div>';
}
