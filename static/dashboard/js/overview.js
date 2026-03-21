/**
 * Nous Dashboard — Overview View
 *
 * Stat cards + mini charts showing at-a-glance memory health.
 * Fetches from GET /status?dashboard=true
 */

/* global Dashboard, Chart, escapeHtml */

Dashboard.registerView('overview', async function(container) {
    Dashboard.showLoading(container);

    var data;
    try {
        data = await Dashboard.apiGet('/status?dashboard=true');
    } catch (err) {
        Dashboard.showError(container, 'Failed to load overview data.', function() {
            Dashboard.reloadView('overview');
        });
        return;
    }

    var db = data.dashboard || {};
    var mem = data.memory || {};
    var deltas = db.deltas || {};
    var distributions = db.distributions || {};
    var rawTimeseries = db.timeseries || {};
    var density = db.graph_density;

    // Normalize timeseries: API returns {facts: [{date, count}, ...], ...}
    // Charts expect {labels: [...], facts: [...], ...}
    var timeseries = normalizeTimeseries(rawTimeseries);

    // Check for empty state
    var totalItems = (mem.total_facts || 0) + (mem.total_episodes || 0) + (mem.total_decisions || 0);
    if (totalItems === 0) {
        Dashboard.showEmpty(container, 'Start a conversation with Nous to generate memories.');
        return;
    }

    // Extract 7-day deltas as scalars
    var factsDelta = deltas.facts ? deltas.facts.last_7_days : null;
    var episodesDelta = deltas.episodes ? deltas.episodes.last_7_days : null;
    var decisionsDelta = deltas.decisions ? deltas.decisions.last_7_days : null;

    // Build view
    var html = '<div class="view-header"><h1>Overview</h1><p>At-a-glance memory health and trends</p></div>';

    // Stat cards
    html += '<div class="stat-grid">';
    html += buildStatCard('Total Facts', mem.total_facts || 0, factsDelta, 'var(--fact-color)');
    html += buildStatCard('Total Episodes', mem.total_episodes || 0, episodesDelta, 'var(--episode-color)');
    html += buildStatCard('Total Decisions', mem.total_decisions || 0, decisionsDelta, 'var(--decision-color)');
    html += buildStatCard('Active Censors', mem.active_censors || 0, null, 'var(--censor-color)');
    html += buildDensityCard(density);
    html += buildBrierCard(data.calibration);
    html += buildStatCard('Procedures', mem.total_procedures || 0, null, 'var(--procedure-color)');
    html += buildStatCard('Active Schedules', mem.active_conversations || 0, null, 'var(--muted)');
    html += '</div>';

    // F026: Execution Integrity section
    var ei = data.execution_integrity || {};
    html += buildIntegritySection(ei);

    // Charts
    html += '<div class="chart-grid">';
    html += '<div class="chart-card"><h3>Memory Growth (30 days)</h3><div class="chart-container"><canvas id="chart-growth"></canvas></div></div>';
    html += '<div class="chart-card"><h3>Fact Categories</h3><div class="chart-container"><canvas id="chart-categories"></canvas></div></div>';
    html += '<div class="chart-card"><h3>Decision Outcomes</h3><div class="chart-container"><canvas id="chart-outcomes"></canvas></div></div>';
    html += '<div class="chart-card"><h3>Edge Types</h3><div class="chart-container"><canvas id="chart-edges"></canvas></div></div>';
    html += '</div>';

    container.innerHTML = html;

    // Create charts
    createGrowthChart(timeseries);
    createCategoriesChart(distributions.fact_categories);
    createOutcomesChart(distributions.decision_outcomes);
    createEdgeTypesChart(distributions.edge_relations);
});

function buildStatCard(label, value, delta, color) {
    var deltaHtml = '';
    if (delta != null) {
        var cls = delta > 0 ? 'positive' : delta < 0 ? 'negative' : 'neutral';
        var arrow = delta > 0 ? '&#x2191;' : delta < 0 ? '&#x2193;' : '';
        deltaHtml = '<span class="stat-delta ' + cls + '">' + arrow + ' ' + Math.abs(delta) + ' (7d)</span>';
    }
    return '<div class="stat-card">' +
        '<div class="stat-value" style="color:' + color + '">' + Dashboard.formatNumber(value) + deltaHtml + '</div>' +
        '<div class="stat-label">' + escapeHtml(label) + '</div>' +
        '</div>';
}

function buildDensityCard(density) {
    var val = density != null ? density.toFixed(1) : '-';
    var indicatorCls = 'warn';
    if (density != null) {
        if (density >= 3.0) indicatorCls = 'good';
        else if (density < 1.0) indicatorCls = 'bad';
    }
    return '<div class="stat-card">' +
        '<div class="stat-value"><span class="stat-indicator ' + indicatorCls + '"></span>' + val + '</div>' +
        '<div class="stat-label">Graph Density</div>' +
        '</div>';
}

function buildBrierCard(calibration) {
    var brier = calibration && calibration.brier_score != null ? calibration.brier_score : null;
    var val = brier != null ? brier.toFixed(3) : '-';
    var indicatorCls = 'neutral';
    if (brier != null) {
        if (brier <= 0.15) indicatorCls = 'good';
        else if (brier <= 0.25) indicatorCls = 'warn';
        else indicatorCls = 'bad';
    }
    return '<div class="stat-card">' +
        '<div class="stat-value"><span class="stat-indicator ' + indicatorCls + '"></span>' + val + '</div>' +
        '<div class="stat-label">Brier Score</div>' +
        '</div>';
}

function normalizeTimeseries(raw) {
    // Convert {facts: [{date, count}, ...], episodes: [...], ...}
    // to {labels: [...], facts: [...], episodes: [...], ...}
    if (!raw) return {};
    var firstKey = Object.keys(raw)[0];
    if (!firstKey || !Array.isArray(raw[firstKey])) return raw;
    // Already normalized?
    if (raw.labels) return raw;
    var labels = raw[firstKey].map(function(e) { return e.date; });
    var result = { labels: labels };
    Object.keys(raw).forEach(function(key) {
        result[key] = raw[key].map(function(e) { return e.count; });
    });
    return result;
}

function createGrowthChart(timeseries) {
    var canvas = document.getElementById('chart-growth');
    if (!canvas || !timeseries || !timeseries.labels) return;

    var chart = new Chart(canvas, {
        type: 'line',
        data: {
            labels: timeseries.labels,
            datasets: [
                {
                    label: 'Facts',
                    data: timeseries.facts || [],
                    borderColor: '#60a5fa',
                    backgroundColor: 'rgba(96, 165, 250, 0.1)',
                    fill: true
                },
                {
                    label: 'Episodes',
                    data: timeseries.episodes || [],
                    borderColor: '#34d399',
                    backgroundColor: 'rgba(52, 211, 153, 0.1)',
                    fill: true
                },
                {
                    label: 'Decisions',
                    data: timeseries.decisions || [],
                    borderColor: '#a78bfa',
                    backgroundColor: 'rgba(167, 139, 250, 0.1)',
                    fill: true
                }
            ]
        },
        options: {
            scales: {
                x: {
                    ticks: {
                        maxTicksLimit: 8,
                        callback: function(val, idx) {
                            var label = this.getLabelForValue(val);
                            if (!label) return '';
                            return label.slice(5);  // MM-DD
                        }
                    },
                    grid: { display: false }
                },
                y: {
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

function createCategoriesChart(categories) {
    var canvas = document.getElementById('chart-categories');
    if (!canvas) return;

    var labels = categories ? Object.keys(categories) : [];
    var values = categories ? Object.values(categories) : [];

    if (labels.length === 0) {
        canvas.parentElement.innerHTML = '<div class="empty-state" style="padding:40px 0"><p class="text-muted">No facts categorized yet</p></div>';
        return;
    }

    var colors = ['#60a5fa', '#34d399', '#a78bfa', '#fb923c', '#f87171', '#fbbf24', '#6b6b8a', '#e2e2f0'];

    var chart = new Chart(canvas, {
        type: 'doughnut',
        data: {
            labels: labels.map(function(l) { return l.charAt(0).toUpperCase() + l.slice(1); }),
            datasets: [{
                data: values,
                backgroundColor: colors.slice(0, labels.length),
                borderWidth: 0,
                hoverOffset: 4
            }]
        },
        options: {
            cutout: '60%',
            plugins: {
                legend: {
                    position: 'right',
                    labels: { font: { size: 11 } }
                }
            }
        }
    });
    Dashboard.trackChart(chart);
}

function createOutcomesChart(outcomes) {
    var canvas = document.getElementById('chart-outcomes');
    if (!canvas) return;

    var labels = outcomes ? Object.keys(outcomes) : [];
    var values = outcomes ? Object.values(outcomes) : [];

    if (labels.length === 0) {
        canvas.parentElement.innerHTML = '<div class="empty-state" style="padding:40px 0"><p class="text-muted">No decisions recorded yet</p></div>';
        return;
    }

    var outcomeColors = {
        success: '#34d399',
        partial: '#fbbf24',
        failure: '#f87171',
        pending: '#6b6b8a'
    };
    var bgColors = labels.map(function(l) { return outcomeColors[l] || '#6b6b8a'; });

    var chart = new Chart(canvas, {
        type: 'doughnut',
        data: {
            labels: labels.map(function(l) { return l.charAt(0).toUpperCase() + l.slice(1); }),
            datasets: [{
                data: values,
                backgroundColor: bgColors,
                borderWidth: 0,
                hoverOffset: 4
            }]
        },
        options: {
            cutout: '60%',
            plugins: {
                legend: {
                    position: 'right',
                    labels: { font: { size: 11 } }
                }
            }
        }
    });
    Dashboard.trackChart(chart);
}

function buildIntegritySection(ei) {
    var enabled = ei.enabled || {};
    var modes = ei.modes || {};
    var sessions = ei.sessions || {};
    var pending = ei.pending_corrections || {};

    var sessionIds = Object.keys(sessions);
    var totalActions = 0;
    var totalBlocked = 0;
    sessionIds.forEach(function(sid) {
        totalActions += sessions[sid].total_actions || 0;
        totalBlocked += sessions[sid].blocked_actions || 0;
    });
    var totalPending = Object.values(pending).reduce(function(sum, n) { return sum + n; }, 0);

    // Mode indicator: green for enforce, yellow for warn, grey for shadow
    var claimMode = modes.claim_verification || 'off';
    var gateMode = modes.action_gating || 'off';

    var html = '<div class="section-header"><h2>Execution Integrity</h2></div>';
    html += '<div class="stat-grid">';
    html += buildStatCard('Active Sessions', ei.active_ledgers || 0, null, 'var(--muted)');
    html += buildStatCard('Actions Recorded', totalActions, null, '#60a5fa');
    html += buildBlockedCard(totalBlocked);
    html += buildModeCard('Claim Verification', claimMode, !enabled.claim_verification);
    html += buildModeCard('Action Gating', gateMode, !enabled.action_gating);
    html += buildStatCard('Pending Corrections', totalPending, null, totalPending > 0 ? '#fbbf24' : 'var(--muted)');
    html += '</div>';

    // Per-session details (only if sessions exist)
    if (sessionIds.length > 0) {
        html += '<div class="integrity-sessions">';
        html += '<table class="data-table"><thead><tr>';
        html += '<th>Session</th><th>Turn</th><th>Actions</th><th>Blocked</th><th>Summary</th>';
        html += '</tr></thead><tbody>';
        sessionIds.forEach(function(sid) {
            var s = sessions[sid];
            var shortId = sid.length > 12 ? sid.slice(0, 12) + '…' : sid;
            html += '<tr>';
            html += '<td><code>' + escapeHtml(shortId) + '</code></td>';
            html += '<td>' + (s.current_turn || 0) + '</td>';
            html += '<td>' + (s.total_actions || 0) + '</td>';
            html += '<td>' + (s.blocked_actions > 0 ? '<span style="color:var(--red)">' + s.blocked_actions + '</span>' : '0') + '</td>';
            html += '<td class="text-muted">' + escapeHtml(s.summary || '-') + '</td>';
            html += '</tr>';
        });
        html += '</tbody></table></div>';
    }

    return html;
}

function buildBlockedCard(blocked) {
    var indicatorCls = blocked === 0 ? 'good' : 'bad';
    return '<div class="stat-card">' +
        '<div class="stat-value"><span class="stat-indicator ' + indicatorCls + '"></span>' + blocked + '</div>' +
        '<div class="stat-label">Blocked Actions</div>' +
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

function createEdgeTypesChart(edgeRelations) {
    var canvas = document.getElementById('chart-edges');
    if (!canvas) return;

    var labels = edgeRelations ? Object.keys(edgeRelations) : [];
    var values = edgeRelations ? Object.values(edgeRelations) : [];

    if (labels.length === 0) {
        canvas.parentElement.innerHTML = '<div class="empty-state" style="padding:40px 0"><p class="text-muted">No graph edges yet</p></div>';
        return;
    }

    // Sort by count descending
    var pairs = labels.map(function(l, i) { return { label: l, value: values[i] }; });
    pairs.sort(function(a, b) { return b.value - a.value; });

    var chart = new Chart(canvas, {
        type: 'bar',
        data: {
            labels: pairs.map(function(p) { return p.label.replace(/_/g, ' '); }),
            datasets: [{
                data: pairs.map(function(p) { return p.value; }),
                backgroundColor: 'rgba(124, 106, 247, 0.5)',
                borderColor: '#7c6af7',
                borderWidth: 1
            }]
        },
        options: {
            indexAxis: 'y',
            scales: {
                x: { beginAtZero: true, ticks: { precision: 0 } },
                y: { grid: { display: false } }
            },
            plugins: { legend: { display: false } }
        }
    });
    Dashboard.trackChart(chart);
}
