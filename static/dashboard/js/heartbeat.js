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
    var findings = data.findings_timeline || [];
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
    // Tracked findings count from FindingStore
    var lifecycleData = data.finding_lifecycle;
    var trackedTotal = lifecycleData ? (lifecycleData.stats || {}).total || 0 : 0;
    var trackedByState = lifecycleData ? (lifecycleData.stats || {}).by_state || {} : {};
    var trackedActive = (trackedByState['new'] || 0) + (trackedByState['acknowledged'] || 0);

    html += '<div class="stat-grid">';
    html += hbBuildStatCard('Total Runs', Dashboard.formatNumber(status.run_count || totals.tick_count || 0), '', 'var(--heartbeat-color)');
    html += hbBuildStatCard('Findings 24h', Dashboard.formatNumber(totals.total || 0), '', 'var(--yellow)');
    html += hbBuildStatCard('Tracked Active', Dashboard.formatNumber(trackedActive), trackedTotal + ' total', 'var(--accent)');
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

    // ── Finding Lifecycle (F034.1) ──
    var lifecycle = data.finding_lifecycle;
    if (lifecycle) {
        var lcStats = lifecycle.stats || {};
        var byState = lcStats.by_state || {};
        var lcFindings = lifecycle.findings || [];
        var escalationPolicy = lifecycle.escalation_policy || {};

        html += '<div class="chart-card mb-24"><h3>Finding Lifecycle</h3>';

        // State summary pills
        html += '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px">';
        var stateColors = { new: '#22d3ee', acknowledged: '#fbbf24', resolved: '#4ade80', suppressed: '#6b6b8a' };
        ['new', 'acknowledged', 'resolved', 'suppressed'].forEach(function (st) {
            var cnt = byState[st] || 0;
            html += '<span class="heartbeat-pill" style="border-color:' + (stateColors[st] || '#6b6b8a') + '">' +
                escapeHtml(st) + ': ' + cnt + '</span>';
        });
        html += '<span class="heartbeat-pill" style="opacity:0.6">total: ' + (lcStats.total || 0) + '</span>';
        html += '</div>';

        // Tracked findings table (only show non-resolved, limit to 15)
        var activeFindings = lcFindings.filter(function (f) { return f.state !== 'resolved'; }).slice(0, 15);
        if (activeFindings.length > 0) {
            html += '<div style="font-size:13px">';
            html += '<div class="check-row" style="font-weight:600;opacity:0.7;font-size:11px;text-transform:uppercase;letter-spacing:0.5px">';
            html += '<div style="flex:0 0 90px">State</div>';
            html += '<div style="flex:0 0 80px">Check</div>';
            html += '<div style="flex:1">Summary</div>';
            html += '<div style="flex:0 0 60px;text-align:right">Seen</div>';
            html += '<div style="flex:0 0 80px;text-align:right">Age</div>';
            html += '</div>';
            activeFindings.forEach(function (f) {
                var stateColor = stateColors[f.state] || '#6b6b8a';
                var escalatedBadge = f.escalated ? ' <span style="color:#f87171;font-size:10px" title="Escalated">&#x26A0;</span>' : '';
                html += '<div class="check-row" style="padding:6px 0">';
                html += '<div style="flex:0 0 90px"><span style="color:' + stateColor + '">' + escapeHtml(f.state) + '</span>' + escalatedBadge + '</div>';
                html += '<div style="flex:0 0 80px;font-size:11px;color:var(--muted)">' + escapeHtml(f.check_name || '') + '</div>';
                html += '<div style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + escapeHtml(f.summary || '') + '">' + escapeHtml((f.summary || '').slice(0, 80)) + '</div>';
                html += '<div style="flex:0 0 60px;text-align:right">' + (f.seen_count || 1) + '</div>';
                html += '<div style="flex:0 0 80px;text-align:right">' + hbHumanizeAgo(f.first_seen) + '</div>';
                html += '</div>';
            });
            html += '</div>';
            if (lcFindings.filter(function (f) { return f.state !== 'resolved'; }).length > 15) {
                html += '<p class="text-muted" style="font-size:11px;padding:4px 0">... and ' +
                    (lcFindings.filter(function (f) { return f.state !== 'resolved'; }).length - 15) + ' more</p>';
            }
        } else {
            html += '<div class="empty-state" style="padding:16px"><p>No active findings tracked</p></div>';
        }

        // Escalation policy (compact)
        html += '<div style="margin-top:12px;font-size:11px;color:var(--muted)">';
        html += 'Escalation: low&#x2192;normal ' + (escalationPolicy.low_to_normal_hours || 72) + 'h, ';
        html += 'normal&#x2192;high ' + (escalationPolicy.normal_to_high_hours || 24) + 'h, ';
        html += 'high re-alert ' + (escalationPolicy.high_realert_hours || 12) + 'h, ';
        html += 'accumulation threshold ' + (escalationPolicy.accumulation_threshold || 5);
        html += '</div>';

        html += '</div>';
    }

    // ── Tuning Status (F034.3) ──
    var tuning = data.tuning;
    if (tuning) {
        html += '<div class="chart-card mb-24"><h3>Self-Tuning</h3>';
        if (!tuning.enabled) {
            html += '<div class="empty-state" style="padding:16px"><p>Tuning disabled (set NOUS_HEARTBEAT_TUNING_ENABLED=true)</p></div>';
        } else if (tuning.last_report) {
            var tr = tuning.last_report;
            html += '<div style="font-size:13px">';
            html += '<div style="display:flex;gap:16px;margin-bottom:8px">';
            html += '<span>Last run: <strong>' + hbHumanizeAgo(tr.timestamp) + '</strong></span>';
            html += '<span>Adjustments: <strong>' + tr.adjustments + '</strong></span>';
            if (tr.skipped_checks && tr.skipped_checks.length > 0) {
                html += '<span class="text-muted">Skipped: ' + escapeHtml(tr.skipped_checks.join(', ')) + '</span>';
            }
            html += '</div>';
            html += '<pre style="font-size:11px;background:rgba(255,255,255,0.03);padding:12px;border-radius:6px;overflow-x:auto;white-space:pre-wrap">' +
                escapeHtml(tr.summary || 'No changes') + '</pre>';
            html += '</div>';
        } else {
            html += '<div class="empty-state" style="padding:16px"><p>No tuning runs yet</p></div>';
        }
        html += '</div>';
    }

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
