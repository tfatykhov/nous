/**
 * Nous Dashboard — Execution Ledger View (F032)
 *
 * Displays per-session execution details: tool calls, statuses,
 * side-effect classifications, and action gating results.
 */

/* global Dashboard, Chart, escapeHtml */

var _ledgerRefreshInterval = null;
var _ledgerRefreshInFlight = false;
var _ledgerAbortController = null;

Dashboard.registerView('execution', async function (container) {
    Dashboard.showLoading(container);

    try {
        var data = await ledgerFetch();
        renderLedger(container, data);
        startAutoRefresh(container);
    } catch (err) {
        Dashboard.showError(container, 'Failed to load execution ledger.', function () {
            Dashboard.reloadView('execution');
        });
    }
});

function ledgerFetch() {
    if (_ledgerAbortController) {
        _ledgerAbortController.abort();
    }
    _ledgerAbortController = new AbortController();
    return fetch('/dashboard/ledger', { signal: _ledgerAbortController.signal })
        .then(function (res) {
            if (!res.ok) throw new Error(res.status + ' ' + res.statusText);
            return res.json();
        })
        .finally(function () {
            _ledgerAbortController = null;
        });
}

function startAutoRefresh(container) {
    stopAutoRefresh();
    _ledgerRefreshInterval = setInterval(async function () {
        if (Dashboard.currentView !== 'execution') {
            stopAutoRefresh();
            return;
        }
        if (_ledgerRefreshInFlight) return;
        _ledgerRefreshInFlight = true;
        try {
            var data = await ledgerFetch();
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
            // Silently skip (including aborted requests)
        } finally {
            _ledgerRefreshInFlight = false;
        }
    }, 15000);
}

function stopAutoRefresh() {
    if (_ledgerRefreshInterval) {
        clearInterval(_ledgerRefreshInterval);
        _ledgerRefreshInterval = null;
    }
    if (_ledgerAbortController) {
        _ledgerAbortController.abort();
        _ledgerAbortController = null;
    }
}

// ── Main render ─────────────────────────────────────────────────────

function renderLedger(container, data, expandedState, filterState, effectFilterState) {
    expandedState = expandedState || {};
    filterState = filterState || {};
    effectFilterState = effectFilterState || {};

    var enabled = data.enabled || {};
    var modes = data.modes || {};
    var sessions = data.sessions || [];

    // Aggregate totals
    var totalActions = 0, totalBlocked = 0, totalErrors = 0, totalTimeouts = 0;
    sessions.forEach(function (s) {
        totalActions += s.total_actions || 0;
        totalBlocked += s.blocked_actions || 0;
        totalErrors += s.error_actions || 0;
        totalTimeouts += s.timeout_actions || 0;
    });

    var html = '<div class="view-header">' +
        '<h1>Execution Ledger</h1>' +
        '<p class="view-subtitle">Real-time tool execution tracking and action gating</p>' +
        '</div>';

    // ── Status banner ──
    var claimMode = modes.claim_verification || 'off';
    var gateMode = modes.action_gating || 'off';
    var bannerClass = 'ledger-status-banner';
    if (!enabled.ledger) bannerClass += ' disabled';
    else if (totalBlocked > 0) bannerClass += ' has-blocked';

    html += '<div class="' + bannerClass + '">';
    html += '<div class="ledger-banner-left">';
    html += '<div class="ledger-banner-dot"></div>';
    html += '<span class="ledger-banner-label">' + (enabled.ledger ? 'Ledger Active' : 'Ledger Disabled') + '</span>';
    html += '</div>';
    html += '<div class="ledger-banner-modes">';
    html += renderModePill('Claim Verification', claimMode, !enabled.claim_verification);
    html += renderModePill('Action Gating', gateMode, !enabled.action_gating);
    html += '</div>';
    html += '</div>';

    // ── Stat cards ──
    html += '<div class="stat-grid">';
    html += buildLedgerStat('Active Sessions', sessions.length, 'var(--accent)');
    html += buildLedgerStat('Total Actions', totalActions, '#60a5fa');
    html += buildLedgerStatIndicator('Blocked', totalBlocked, totalBlocked > 0 ? 'bad' : 'good');
    html += buildLedgerStatIndicator('Errors', totalErrors, totalErrors > 0 ? 'warn' : 'good');
    html += buildLedgerStatIndicator('Timeouts', totalTimeouts, totalTimeouts > 0 ? 'warn' : 'good');
    html += buildLedgerStat('Success Rate',
        totalActions > 0 ? Math.round(((totalActions - totalBlocked - totalErrors - totalTimeouts) / totalActions) * 100) + '%' : '—',
        'var(--green)');
    html += '</div>';

    // ── Sessions ──
    if (sessions.length === 0) {
        html += '<div class="ledger-empty">' +
            '<div class="ledger-empty-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="48" height="48">' +
            '<path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"/>' +
            '</svg></div>' +
            '<h3>No Active Sessions</h3>' +
            '<p>Execution data appears here when sessions are active.<br>Auto-refreshes every 15 seconds.</p>' +
            '</div>';
    } else {
        html += '<div class="section-header"><h2>Sessions (' + sessions.length + ')</h2></div>';
        html += '<div class="ledger-sessions">';
        sessions.forEach(function (session) {
            var isExpanded = expandedState[session.session_id] || false;
            html += buildSessionCard(session, isExpanded, filterState[session.session_id] || 'all', effectFilterState[session.session_id] || 'all-effects');
        });
        html += '</div>';
    }

    container.innerHTML = html;
    bindSessionHandlers(container);
}

// ── Session card ────────────────────────────────────────────────────

function buildSessionCard(session, isExpanded, activeFilter, activeEffectFilter) {
    var sid = session.session_id;
    var shortId = sid;  // Show full session ID — no truncation
    var successCount = session.success_actions || 0;

    var html = '<div class="ledger-session' + (isExpanded ? ' expanded' : '') + '" data-session-id="' + escapeHtml(sid) + '">';

    // ── Header ──
    html += '<div class="ledger-session-header">';
    html += '<div class="ledger-header-top">';
    html += '<div class="ledger-session-id"><code>' + escapeHtml(shortId) + '</code></div>';
    html += '<div class="ledger-header-badges">';
    html += '<span class="ledger-pill pill-turn">Turn ' + (session.current_turn || 0) + '</span>';
    html += '<span class="ledger-pill pill-count">' + (session.total_actions || 0) + ' actions</span>';
    if (session.blocked_actions > 0) {
        html += '<span class="ledger-pill pill-blocked">' + session.blocked_actions + ' blocked</span>';
    }
    if (session.error_actions > 0) {
        html += '<span class="ledger-pill pill-error">' + session.error_actions + ' error</span>';
    }
    if (session.actions_truncated) {
        html += '<span class="ledger-pill pill-muted">truncated</span>';
    }
    html += '</div>';
    html += '<svg class="ledger-chevron" viewBox="0 0 20 20" fill="currentColor" width="18" height="18"><path fill-rule="evenodd" d="M5.293 7.293a1 1 0 011.414 0L10 10.586l3.293-3.293a1 1 0 111.414 1.414l-4 4a1 1 0 01-1.414 0l-4-4a1 1 0 010-1.414z" clip-rule="evenodd"/></svg>';
    html += '</div>'; // header-top

    // Summary bar
    html += '<div class="ledger-header-summary">';
    html += '<span class="text-muted">' + escapeHtml(session.summary || 'No actions') + '</span>';

    // Mini progress bar showing success/blocked/error ratio
    if (session.total_actions > 0) {
        var total = session.total_actions;
        var pctSuccess = (successCount / total * 100).toFixed(1);
        var pctBlocked = ((session.blocked_actions || 0) / total * 100).toFixed(1);
        var pctError = ((session.error_actions || 0) / total * 100).toFixed(1);
        html += '<div class="ledger-minibar">';
        if (pctSuccess > 0) html += '<div class="minibar-segment minibar-success" style="width:' + pctSuccess + '%" title="' + successCount + ' success"></div>';
        if (pctBlocked > 0) html += '<div class="minibar-segment minibar-blocked" style="width:' + pctBlocked + '%" title="' + session.blocked_actions + ' blocked"></div>';
        if (pctError > 0) html += '<div class="minibar-segment minibar-error" style="width:' + pctError + '%" title="' + session.error_actions + ' error"></div>';
        html += '</div>';
    }
    html += '</div>'; // header-summary
    html += '</div>'; // header

    // ── Detail ──
    html += '<div class="ledger-session-detail" style="display:' + (isExpanded ? 'block' : 'none') + '">';

    // Filter bar
    html += '<div class="ledger-filter-bar">';
    html += '<div class="ledger-filter-group">';
    html += '<span class="ledger-filter-label">Status</span>';
    ['all', 'success', 'blocked', 'error', 'timeout'].forEach(function (f) {
        var cls = f === activeFilter ? ' active' : '';
        html += '<button class="filter-btn' + cls + '" data-filter="' + f + '">' + f + '</button>';
    });
    html += '</div>';
    html += '<div class="ledger-filter-group">';
    html += '<span class="ledger-filter-label">Effect</span>';
    ['all-effects', 'none', 'write', 'external', 'irreversible'].forEach(function (f) {
        var label = f === 'all-effects' ? 'all' : f;
        var cls = f === (activeEffectFilter || 'all-effects') ? ' active' : '';
        html += '<button class="filter-btn effect-filter' + cls + '" data-effect-filter="' + f + '">' + label + '</button>';
    });
    html += '</div>';
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
        html += '<div class="ledger-no-actions">No actions recorded in this session.</div>';
    } else {
        html += '<div class="ledger-timeline">';
        turnNums.forEach(function (turn) {
            var turnActions = turnGroups[turn];
            html += '<div class="ledger-turn">';
            html += '<div class="ledger-turn-header">';
            html += '<span class="ledger-turn-marker"></span>';
            html += '<span class="ledger-turn-label">Turn ' + turn + '</span>';
            html += '<span class="ledger-turn-count">' + turnActions.length + ' action' + (turnActions.length !== 1 ? 's' : '') + '</span>';
            html += '</div>';
            html += '<div class="ledger-turn-actions">';
            turnActions.forEach(function (a) {
                html += buildActionRow(a);
            });
            html += '</div>';
            html += '</div>';
        });
        html += '</div>';
    }

    html += '</div>'; // detail
    html += '</div>'; // session

    return html;
}

// ── Action row ──────────────────────────────────────────────────────

function buildActionRow(action) {
    var statusClass = 'status-' + (action.status || 'success');

    var html = '<div class="ledger-action ' + statusClass + '" data-status="' + escapeHtml(action.status || '') + '" data-effect="' + escapeHtml(action.side_effect_type || 'none') + '">';

    // Left: status dot + tool name
    html += '<div class="action-left">';
    html += '<span class="action-dot ' + statusClass + '"></span>';
    html += '<span class="action-tool">' + escapeHtml(action.tool_name) + '</span>';
    html += '</div>';

    // Center: key args
    var args = action.key_args || {};
    var argParts = Object.keys(args).map(function (k) {
        return '<span class="arg-key">' + escapeHtml(k) + '</span><span class="arg-eq">=</span><span class="arg-val">' + escapeHtml(args[k]) + '</span>';
    });
    if (argParts.length > 0) {
        html += '<div class="action-args">' + argParts.join(' ') + '</div>';
    }

    // Right: badges + timestamp
    html += '<div class="action-right">';
    if (action.side_effect_type && action.side_effect_type !== 'none') {
        html += '<span class="action-effect-pill effect-' + action.side_effect_type + '">' + escapeHtml(action.side_effect_type) + '</span>';
    }
    html += '<span class="action-status-pill ' + statusClass + '">' + escapeHtml(action.status) + '</span>';
    if (action.timestamp) {
        var ts = new Date(action.timestamp);
        html += '<span class="action-time">' + ts.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) + '</span>';
    }
    html += '</div>';

    // Error/blocked detail
    if (action.status !== 'success' && action.result_summary) {
        html += '<div class="action-detail">' + escapeHtml(action.result_summary) + '</div>';
    }

    html += '</div>';
    return html;
}

// ── Event binding ───────────────────────────────────────────────────

function bindSessionHandlers(container) {
    container.querySelectorAll('.ledger-session-header').forEach(function (header) {
        header.addEventListener('click', function () {
            var card = header.closest('.ledger-session');
            card.classList.toggle('expanded');
            var detail = card.querySelector('.ledger-session-detail');
            if (detail) detail.style.display = card.classList.contains('expanded') ? 'block' : 'none';
        });
    });

    container.querySelectorAll('.filter-btn:not(.effect-filter)').forEach(function (btn) {
        btn.addEventListener('click', function (e) {
            e.stopPropagation();
            var bar = btn.closest('.ledger-filter-bar');
            bar.querySelectorAll('.filter-btn:not(.effect-filter)').forEach(function (b) { b.classList.remove('active'); });
            btn.classList.add('active');
            applyFilters(btn.closest('.ledger-session'));
        });
    });

    container.querySelectorAll('.filter-btn.effect-filter').forEach(function (btn) {
        btn.addEventListener('click', function (e) {
            e.stopPropagation();
            var bar = btn.closest('.ledger-filter-bar');
            bar.querySelectorAll('.filter-btn.effect-filter').forEach(function (b) { b.classList.remove('active'); });
            btn.classList.add('active');
            applyFilters(btn.closest('.ledger-session'));
        });
    });
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
        countEl.textContent = shown < total ? 'Showing ' + shown + ' of ' + total : '';
    }
}

// ── Helper components ───────────────────────────────────────────────

function renderModePill(label, mode, disabled) {
    if (disabled) {
        return '<span class="ledger-mode-pill mode-off">' + escapeHtml(label) + ': off</span>';
    }
    var cls = mode === 'enforce' ? 'mode-enforce' : mode === 'warn' ? 'mode-warn' : 'mode-shadow';
    return '<span class="ledger-mode-pill ' + cls + '">' + escapeHtml(label) + ': ' + escapeHtml(mode) + '</span>';
}

function buildLedgerStat(label, value, color) {
    var displayVal = typeof value === 'number' ? Dashboard.formatNumber(value) : value;
    return '<div class="stat-card">' +
        '<div class="stat-value" style="color:' + (color || 'var(--text)') + '">' + displayVal + '</div>' +
        '<div class="stat-label">' + escapeHtml(label) + '</div>' +
        '</div>';
}

function buildLedgerStatIndicator(label, value, cls) {
    return '<div class="stat-card">' +
        '<div class="stat-value"><span class="stat-indicator ' + cls + '"></span>' + Dashboard.formatNumber(value) + '</div>' +
        '<div class="stat-label">' + escapeHtml(label) + '</div>' +
        '</div>';
}
