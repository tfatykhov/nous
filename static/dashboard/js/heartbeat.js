/**
 * Nous Dashboard — Heartbeat View (F034)
 *
 * Heartbeat status, check health, token budget, findings timeline,
 * and cognitive session log. Auto-refreshes every 30 seconds.
 * Fetches from GET /dashboard/heartbeat
 */

/* global Dashboard, Chart, escapeHtml */

var _hbRefreshInterval = null;
var _hbRefreshInFlight = false;
var _hbAbortController = null;

Dashboard.registerView('heartbeat', async function (container) {
    Dashboard.showLoading(container);

    try {
        var data = await hbFetch();
        renderHeartbeat(container, data);
        startHbAutoRefresh(container);
    } catch (err) {
        Dashboard.showError(container, 'Failed to load heartbeat data.', function () {
            Dashboard.reloadView('heartbeat');
        });
    }
});

function hbFetch() {
    if (_hbAbortController) {
        _hbAbortController.abort();
    }
    _hbAbortController = new AbortController();
    return fetch('/dashboard/heartbeat', { signal: _hbAbortController.signal })
        .then(function (res) {
            if (!res.ok) throw new Error(res.status + ' ' + res.statusText);
            return res.json();
        })
        .finally(function () {
            _hbAbortController = null;
        });
}

function startHbAutoRefresh(container) {
    stopHbAutoRefresh();
    _hbRefreshInterval = setInterval(async function () {
        if (Dashboard.currentView !== 'heartbeat') {
            stopHbAutoRefresh();
            return;
        }
        if (_hbRefreshInFlight) return;
        _hbRefreshInFlight = true;
        try {
            var data = await hbFetch();
            renderHeartbeat(container, data);
        } catch (err) {
            // Silently skip (including aborted requests)
        } finally {
            _hbRefreshInFlight = false;
        }
    }, 30000);
}

function stopHbAutoRefresh() {
    if (_hbRefreshInterval) {
        clearInterval(_hbRefreshInterval);
        _hbRefreshInterval = null;
    }
    if (_hbAbortController) {
        _hbAbortController.abort();
        _hbAbortController = null;
    }
}

// ── Helpers ─────────────────────────────────────────────────────────

function hbHumanizeInterval(seconds) {
    if (seconds == null) return '--';
    if (seconds < 60) return seconds + 's';
    if (seconds < 3600) return Math.round(seconds / 60) + 'm';
    return Math.round(seconds / 3600) + 'h';
}

function hbHumanizeAgo(isoString) {
    if (!isoString) return '--';
    var now = new Date();
    var then = new Date(isoString);
    var diffMs = now - then;
    var diffSecs = Math.floor(diffMs / 1000);
    if (diffSecs < 30) return 'just now';
    var diffMins = Math.floor(diffSecs / 60);
    if (diffMins < 1) return diffSecs + 's ago';
    if (diffMins < 60) return diffMins + 'm ago';
    var diffHours = Math.floor(diffMins / 60);
    if (diffHours < 24) return diffHours + 'h ago';
    var diffDays = Math.floor(diffHours / 24);
    return diffDays + 'd ago';
}

function hbHumanizeCountdown(isoString) {
    if (!isoString) return '--';
    var now = new Date();
    var target = new Date(isoString);
    var diffMs = target - now;
    if (diffMs <= 0) {
        if (diffMs > -60000) return 'now';
        return 'overdue';
    }
    var diffMins = Math.ceil(diffMs / 60000);
    if (diffMins < 60) return 'in ' + diffMins + 'm';
    var diffHours = Math.round(diffMins / 60);
    return 'in ' + diffHours + 'h';
}

// ── Main render ─────────────────────────────────────────────────────

function renderHeartbeat(container, data) {
    // Destroy existing charts before re-render
    if (Dashboard.charts['heartbeat']) {
        Dashboard.charts['heartbeat'].forEach(function (c) {
            try { c.destroy(); } catch (e) { /* ignore */ }
        });
        Dashboard.charts['heartbeat'] = [];
    }

    var status = data.status || {};
    var checks = data.checks || [];
    var budget = data.budget || {};
    var quietHours = data.quiet_hours || {};
    var totals = data.totals || {};
    var findings = data.findings || [];
    var cogSessions = data.cognitive_sessions || [];
    var findingsByDay = data.findings_by_day || [];
    var enabled = status.enabled !== false;

    var html = '<div class="view-header">' +
        '<h1>Heartbeat</h1>' +
        '<p class="view-subtitle">Autonomous monitoring, checks, and cognitive triage</p>' +
        '</div>';

    // ── Status Banner ──
    var activeChecks = 0;
    var totalChecks = checks.length;
    var trippedBreakers = 0;
    checks.forEach(function (c) {
        if (c.active) activeChecks++;
        if (c.circuit_breaker_tripped) trippedBreakers++;
    });

    var bannerDotClass = 'heartbeat-banner-dot';
    if (!enabled) bannerDotClass += ' inactive';
    else if (trippedBreakers > 0) bannerDotClass += ' warning';

    html += '<div class="heartbeat-banner">';
    html += '<div class="' + bannerDotClass + '"></div>';
    html += '<div class="heartbeat-banner-label">' + (enabled ? 'Heartbeat Active' : 'Heartbeat Disabled') + '</div>';
    html += '<div class="heartbeat-banner-pills">';

    // Quiet hours pill
    if (quietHours.active) {
        html += '<span class="heartbeat-pill warn">Quiet Hours: active (' + escapeHtml(String(quietHours.start || '')) + '-' + escapeHtml(String(quietHours.end || '')) + ')</span>';
    } else {
        html += '<span class="heartbeat-pill">Quiet Hours: inactive</span>';
    }

    // Budget pill
    var budgetPct = budget.limit > 0 ? Math.round((budget.used / budget.limit) * 100) : 0;
    var budgetCls = 'heartbeat-pill';
    if (budgetPct >= 100) budgetCls += ' bad';
    else if (budgetPct > 80) budgetCls += ' warn';
    else budgetCls += ' ok';
    var budgetLabel = budgetPct >= 100 ? 'exhausted' : budgetPct + '% ok';
    html += '<span class="' + budgetCls + '">Budget: ' + budgetLabel + '</span>';

    html += '</div>'; // pills
    html += '</div>'; // banner

    // ── Disabled state ──
    if (!enabled) {
        html += '<div class="stat-grid">';
        html += hbBuildStatCard('Total Runs', '--', '', 'var(--heartbeat-color)');
        html += hbBuildStatCard('Findings 24h', '--', '', 'var(--yellow)');
        html += hbBuildStatCard('Cognitive Sessions', '--', '', 'var(--accent)');
        html += hbBuildStatCard('Checks Active', '--', '', 'var(--green)');
        html += hbBuildStatCard('Circuit Breakers', '--', '', 'var(--muted)');
        html += '</div>';
        html += '<div class="empty-state" style="padding:40px"><h3>Heartbeat is not running</h3><p>Enable the heartbeat to see monitoring data here.</p></div>';
        container.innerHTML = html;
        return;
    }

    // ── Stat Cards ──
    html += '<div class="stat-grid">';
    html += hbBuildStatCard('Total Runs', Dashboard.formatNumber(status.run_count || totals.tick_count || 0), '', 'var(--heartbeat-color)');
    html += hbBuildStatCard('Findings 24h', Dashboard.formatNumber(totals.findings_24h || 0), '', 'var(--yellow)');
    html += hbBuildStatCard('Cognitive Sessions', Dashboard.formatNumber(cogSessions.length), '', 'var(--accent)');
    html += hbBuildStatCard('Checks Active', activeChecks + ' / ' + totalChecks, '', 'var(--green)');
    var breakerColor = trippedBreakers > 0 ? 'var(--red)' : 'var(--muted)';
    html += hbBuildStatCard('Circuit Breakers', String(trippedBreakers), trippedBreakers > 0 ? 'tripped' : 'none', breakerColor);
    html += '</div>';

    // ── Charts ──
    html += '<div class="chart-grid">';
    html += '<div class="chart-card"><h3>Token Budget</h3><div class="chart-container" style="position:relative;max-height:220px">' +
        '<div class="budget-gauge-wrap" style="max-width:200px;margin:0 auto"><canvas id="chart-hb-budget"></canvas>' +
        '<div class="budget-gauge-center"><div class="budget-gauge-value">' + Dashboard.formatNumber(budget.used || 0) + '</div>' +
        '<div class="budget-gauge-label">of ' + Dashboard.formatNumber(budget.limit || 0) + '</div></div>' +
        '</div></div></div>';
    html += '<div class="chart-card"><h3>Findings by Urgency (7d)</h3><div class="chart-container"><canvas id="chart-hb-findings"></canvas></div></div>';
    html += '</div>';

    // ── Check Status Table ──
    html += '<div class="chart-card mb-24"><h3>Check Status</h3>';
    if (checks.length > 0) {
        checks.forEach(function (check) {
            var dotCls = 'check-dot';
            if (check.circuit_breaker_tripped) dotCls += ' bad';
            else if ((check.consecutive_failures || 0) > 0) dotCls += ' warn';
            else dotCls += ' good';

            var nameHtml = escapeHtml(check.name || check.id || '');
            if (check.permanent) nameHtml += ' <span title="Permanent check" style="opacity:0.5">&#x1F512;</span>';

            html += '<div class="check-row">';
            html += '<div class="' + dotCls + '"></div>';
            html += '<div class="check-name">' + nameHtml + '</div>';
            html += '<div class="check-detail">' + hbHumanizeInterval(check.interval_seconds) + '</div>';
            html += '<div class="check-detail">' + hbHumanizeAgo(check.last_run) + '</div>';
            html += '<div class="check-detail">' + hbHumanizeCountdown(check.next_due) + '</div>';
            html += '</div>';
        });
    } else {
        html += '<p class="text-muted" style="font-size:12px;padding:12px 0">No checks configured</p>';
    }
    html += '</div>';

    // ── Two columns: Findings Timeline + Cognitive Sessions ──
    html += '<div style="display:grid;grid-template-columns:1fr 360px;gap:24px;align-items:start">';

    // Findings Timeline
    html += '<div class="chart-card"><h3>Findings (Last 24h)</h3>';
    if (findings.length > 0) {
        html += '<div class="timeline" style="padding-left:20px">';
        findings.forEach(function (f) {
            var urgency = f.urgency || 'low';
            var urgencyClass = urgency === 'high' ? 'high' : urgency === 'normal' ? 'normal' : 'low';
            html += '<div class="timeline-item">';
            html += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">';
            html += '<div class="finding-dot ' + urgencyClass + '"></div>';
            html += '<span class="finding-source">' + escapeHtml(f.source || 'unknown') + '</span>';
            html += '<span class="event-time">' + hbHumanizeAgo(f.timestamp) + '</span>';
            html += '</div>';
            html += '<div class="event-summary">' + escapeHtml(f.summary || '') + '</div>';
            html += '</div>';
        });
        html += '</div>';
    } else {
        html += '<div class="empty-state" style="padding:24px"><p>All clear — no findings in the last 24 hours</p></div>';
    }
    html += '</div>';

    // Cognitive Sessions Log
    html += '<div class="chart-card"><h3>Cognitive Sessions</h3>';
    if (cogSessions.length > 0) {
        html += '<div style="font-size:13px">';
        cogSessions.forEach(function (s) {
            html += '<div style="padding:8px 0;border-bottom:1px solid var(--border)">';
            html += '<div style="display:flex;justify-content:space-between;align-items:center">';
            html += '<code style="font-size:11px;color:var(--accent)">' + escapeHtml(String(s.session_id || '').slice(0, 12)) + '</code>';
            html += '<span class="text-muted" style="font-size:11px">' + hbHumanizeAgo(s.timestamp) + '</span>';
            html += '</div>';
            html += '<div class="text-muted" style="font-size:11px;margin-top:2px">' +
                (s.findings_count || 0) + ' findings &middot; ' +
                Dashboard.formatNumber(s.tokens_used || 0) + ' tokens</div>';
            html += '</div>';
        });
        html += '</div>';
    } else {
        html += '<div class="empty-state" style="padding:24px"><p>No cognitive sessions today</p></div>';
    }
    html += '</div>';

    html += '</div>'; // grid

    container.innerHTML = html;

    // ── Create Charts ──
    createHbBudgetChart(budget);
    createHbFindingsChart(findingsByDay);
}

// ── Stat card helper ────────────────────────────────────────────────

function hbBuildStatCard(label, value, period, color) {
    return '<div class="stat-card">' +
        '<div class="stat-value" style="color:' + color + '">' + escapeHtml(String(value)) + '</div>' +
        '<div class="stat-label">' + escapeHtml(label) + '</div>' +
        (period ? '<div style="font-size:11px;color:var(--muted);margin-top:2px">' + escapeHtml(period) + '</div>' : '') +
        '</div>';
}

// ── Token Budget Doughnut ───────────────────────────────────────────

function createHbBudgetChart(budget) {
    var canvas = document.getElementById('chart-hb-budget');
    if (!canvas) return;

    var used = budget.used || 0;
    var limit = budget.limit || 1;
    var remaining = Math.max(0, limit - used);
    var pct = limit > 0 ? Math.round((used / limit) * 100) : 0;

    var usedColor = pct >= 100 ? '#f87171' : pct > 80 ? '#fbbf24' : '#22d3ee';

    var chart = new Chart(canvas, {
        type: 'doughnut',
        data: {
            labels: ['Used', 'Remaining'],
            datasets: [{
                data: [used, remaining],
                backgroundColor: [usedColor, 'rgba(255,255,255,0.06)'],
                borderWidth: 0
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: true,
            cutout: '75%',
            plugins: {
                legend: { display: false }
            }
        }
    });
    Dashboard.trackChart(chart);
}

// ── Findings by Urgency 7-day stacked bar ───────────────────────────

function createHbFindingsChart(findingsByDay) {
    var canvas = document.getElementById('chart-hb-findings');
    if (!canvas || !findingsByDay || findingsByDay.length === 0) return;

    var labels = findingsByDay.map(function (d) { return d.date; });
    var highData = findingsByDay.map(function (d) { return (d.by_urgency || {}).high || 0; });
    var normalData = findingsByDay.map(function (d) { return (d.by_urgency || {}).normal || 0; });
    var lowData = findingsByDay.map(function (d) { return (d.by_urgency || {}).low || 0; });

    var chart = new Chart(canvas, {
        type: 'bar',
        data: {
            labels: labels,
            datasets: [
                {
                    label: 'High',
                    data: highData,
                    backgroundColor: '#f87171',
                    borderWidth: 0
                },
                {
                    label: 'Normal',
                    data: normalData,
                    backgroundColor: '#fbbf24',
                    borderWidth: 0
                },
                {
                    label: 'Low',
                    data: lowData,
                    backgroundColor: '#6b6b8a',
                    borderWidth: 0
                }
            ]
        },
        options: {
            scales: {
                x: {
                    stacked: true,
                    ticks: {
                        callback: function (val) {
                            var label = this.getLabelForValue(val);
                            return label ? String(label).slice(5) : '';
                        }
                    },
                    grid: { display: false }
                },
                y: {
                    stacked: true,
                    beginAtZero: true,
                    ticks: { precision: 0 }
                }
            },
            plugins: {
                legend: { position: 'bottom' }
            }
        }
    });
    Dashboard.trackChart(chart);
}
