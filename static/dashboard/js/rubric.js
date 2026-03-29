/**
 * Nous Dashboard — Rubric View (F024 Phase 3b)
 *
 * Visualizes self-modifying rubric: current dimensions & weights,
 * outcome signal distribution, version history, correlation heatmap,
 * and weight evolution over time.
 */

/* global Dashboard, Chart, escapeHtml */

Dashboard.registerView('rubric', async function (container) {
    Dashboard.showLoading(container);

    try {
        var data = await Dashboard.apiGet('/dashboard/rubric');
        renderRubric(container, data);
    } catch (err) {
        Dashboard.showError(container, 'Failed to load rubric data.', function () {
            Dashboard.reloadView('rubric');
        });
    }
});

function renderRubric(container, data) {
    container.innerHTML = '<div class="view-header">' +
        '<h1>Self-Modifying Rubric</h1>' +
        '<p class="view-subtitle">F024 Phase 3b — Evaluation dimensions, outcome signals, and evolution tracking</p>' +
        '</div>' +
        '<div id="rubric-content"></div>';

    var content = document.getElementById('rubric-content');

    if (!data.active_rubric) {
        Dashboard.showEmpty(container, 'No active rubric — ensure NOUS_RUBRIC_ENABLED=true and the rubric has been seeded.');
        return;
    }

    renderRubricConfig(content, data);
    renderDimensionWeights(content, data);
    renderOutcomeSignals(content, data);
    renderSignalTrend(content, data);
    renderCorrelationHeatmap(content, data);
    renderWeightHistory(content, data);
    renderVersionHistory(content, data);
    renderRecentSignals(content, data);
}

// -- Config banner --------------------------------------------------------

function renderRubricConfig(el, data) {
    var cfg = data.config;
    var rubric = data.active_rubric;

    var banner = document.createElement('div');
    banner.className = 'admission-banner ' + (cfg.evolution_enabled ? 'enforced' : 'shadow');
    banner.innerHTML =
        '<div class="banner-indicator ' + (cfg.evolution_enabled ? 'enforced-indicator' : 'shadow-indicator') + '"></div>' +
        '<div class="banner-text">' +
        '<strong>Rubric v' + escapeHtml(rubric.version) + '</strong> — ' +
        rubric.dimension_count + ' dimensions' +
        (cfg.evolution_enabled ? ' | Evolution ACTIVE' : ' | Evolution OFF (observation mode)') +
        '<div class="banner-stats">' +
        'Outcome detection: ' + (cfg.outcome_detection_enabled ? 'ON' : 'OFF') + ' | ' +
        'Signals collected: ' + Dashboard.formatNumber(data.outcome_signals.total) + ' | ' +
        'Min episodes for correlation: ' + cfg.min_episodes_for_correlation + ' | ' +
        'Weight cap: \u00b1' + (cfg.weight_change_cap * 100).toFixed(0) + '%' +
        '</div></div>';
    el.appendChild(banner);
}

// -- Dimension weights (radar chart) --------------------------------------

function renderDimensionWeights(el, data) {
    var dims = data.active_rubric.dimensions;

    var section = document.createElement('div');
    section.className = 'chart-section';
    section.innerHTML =
        '<h3>Current Dimensions & Weights</h3>' +
        '<div class="chart-grid">' +
        '<div class="chart-container" style="height:320px"><canvas id="rubric-radar-chart"></canvas></div>' +
        '<div id="rubric-dimension-cards"></div>' +
        '</div>';
    el.appendChild(section);

    // Radar chart
    var labels = dims.map(function (d) { return d.name; });
    var weights = dims.map(function (d) { return d.weight; });
    var minWeights = dims.map(function (d) { return d.min_weight || 0.10; });
    var maxWeights = dims.map(function (d) { return d.max_weight || 0.40; });

    var ctx = document.getElementById('rubric-radar-chart').getContext('2d');
    Dashboard.trackChart(new Chart(ctx, {
        type: 'radar',
        data: {
            labels: labels,
            datasets: [
                {
                    label: 'Current Weight',
                    data: weights,
                    borderColor: '#7c6af7',
                    backgroundColor: 'rgba(124, 106, 247, 0.2)',
                    pointBackgroundColor: '#7c6af7',
                    borderWidth: 2,
                },
                {
                    label: 'Min Bound',
                    data: minWeights,
                    borderColor: 'rgba(248, 113, 113, 0.4)',
                    backgroundColor: 'transparent',
                    borderDash: [4, 4],
                    borderWidth: 1,
                    pointRadius: 0,
                },
                {
                    label: 'Max Bound',
                    data: maxWeights,
                    borderColor: 'rgba(52, 211, 153, 0.4)',
                    backgroundColor: 'transparent',
                    borderDash: [4, 4],
                    borderWidth: 1,
                    pointRadius: 0,
                }
            ]
        },
        options: {
            scales: {
                r: {
                    min: 0,
                    max: 0.5,
                    ticks: { stepSize: 0.1, display: true },
                    grid: { color: '#1e1e2e' },
                    angleLines: { color: '#1e1e2e' },
                    pointLabels: { font: { size: 12 } }
                }
            },
            plugins: { legend: { display: true, position: 'bottom' } }
        }
    }));

    // Dimension detail cards
    var cards = document.getElementById('rubric-dimension-cards');
    var grid = document.createElement('div');
    grid.className = 'stat-cards';
    dims.forEach(function (d) {
        var pct = (d.weight * 100).toFixed(0);
        var card = document.createElement('div');
        card.className = 'stat-card';
        card.innerHTML =
            '<div class="stat-value">' + pct + '%</div>' +
            '<div class="stat-label">' + escapeHtml(d.name) + '</div>' +
            '<div class="stat-detail">' + escapeHtml(d.description) + '</div>';
        grid.appendChild(card);
    });
    cards.appendChild(grid);
}

// -- Outcome signal distribution (doughnut) -------------------------------

function renderOutcomeSignals(el, data) {
    var byType = data.outcome_signals.by_type;
    if (!byType || Object.keys(byType).length === 0) return;

    var section = document.createElement('div');
    section.className = 'chart-section';
    section.innerHTML =
        '<h3>Outcome Signal Distribution</h3>' +
        '<p class="section-note">Total: ' + Dashboard.formatNumber(data.outcome_signals.total) + ' signals detected from episodes</p>' +
        '<div class="chart-container" style="height:280px;max-width:500px"><canvas id="rubric-signal-doughnut"></canvas></div>';
    el.appendChild(section);

    var signalColors = {
        completed: '#34d399',
        praised: '#60a5fa',
        corrected: '#fb923c',
        reworked: '#f87171',
        self_corrected: '#a78bfa'
    };

    var types = Object.keys(byType);
    var counts = types.map(function (t) { return byType[t]; });
    var colors = types.map(function (t) { return signalColors[t] || '#6b6b8a'; });

    var ctx = document.getElementById('rubric-signal-doughnut').getContext('2d');
    Dashboard.trackChart(new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels: types.map(function (t) { return t.replace('_', ' '); }),
            datasets: [{
                data: counts,
                backgroundColor: colors,
                borderColor: '#111118',
                borderWidth: 2,
            }]
        },
        options: {
            plugins: { legend: { position: 'right' } }
        }
    }));
}

// -- Signal trend over time (stacked area) --------------------------------

function renderSignalTrend(el, data) {
    var trend = data.outcome_signals.daily_trend;
    if (!trend || trend.length === 0) return;

    var hasData = trend.some(function (d) {
        return d.completed + d.corrected + d.praised + d.reworked + d.self_corrected > 0;
    });
    if (!hasData) return;

    var section = document.createElement('div');
    section.className = 'chart-section';
    section.innerHTML =
        '<h3>Signal Trend (30 Days)</h3>' +
        '<div class="chart-container" style="height:280px"><canvas id="rubric-signal-trend"></canvas></div>';
    el.appendChild(section);

    var labels = trend.map(function (d) { return d.date; });

    var ctx = document.getElementById('rubric-signal-trend').getContext('2d');
    Dashboard.trackChart(new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [
                { label: 'Completed', data: trend.map(function (d) { return d.completed; }), borderColor: '#34d399', backgroundColor: 'rgba(52,211,153,0.15)', fill: 'origin' },
                { label: 'Praised', data: trend.map(function (d) { return d.praised; }), borderColor: '#60a5fa', backgroundColor: 'rgba(96,165,250,0.15)', fill: '-1' },
                { label: 'Corrected', data: trend.map(function (d) { return d.corrected; }), borderColor: '#fb923c', backgroundColor: 'rgba(251,146,60,0.15)', fill: '-1' },
                { label: 'Reworked', data: trend.map(function (d) { return d.reworked; }), borderColor: '#f87171', backgroundColor: 'rgba(248,113,113,0.15)', fill: '-1' },
                { label: 'Self-corrected', data: trend.map(function (d) { return d.self_corrected; }), borderColor: '#a78bfa', backgroundColor: 'rgba(167,139,250,0.15)', fill: '-1' },
            ]
        },
        options: {
            plugins: { legend: { display: true, position: 'bottom' } },
            scales: {
                x: { title: { display: true, text: 'Date' } },
                y: { title: { display: true, text: 'Signals' }, beginAtZero: true, stacked: true }
            }
        }
    }));
}

// -- Correlation heatmap (HTML table) -------------------------------------

function renderCorrelationHeatmap(el, data) {
    var corr = data.correlations.data;
    if (!corr || corr.length === 0) {
        var section = document.createElement('div');
        section.className = 'chart-section';
        section.innerHTML =
            '<h3>Dimension \u2194 Signal Correlations</h3>' +
            '<p class="section-note">No correlation data yet. Need ' +
            (data.config.min_episodes_for_correlation || 50) +
            '+ episodes with outcome signals before correlations are computed.</p>';
        el.appendChild(section);
        return;
    }

    // Build matrix: dims x signal_types
    var dims = [];
    var sigTypes = [];
    var matrix = {};
    corr.forEach(function (c) {
        if (dims.indexOf(c.dimension) === -1) dims.push(c.dimension);
        if (sigTypes.indexOf(c.signal_type) === -1) sigTypes.push(c.signal_type);
        matrix[c.dimension + '|' + c.signal_type] = c;
    });

    var section = document.createElement('div');
    section.className = 'table-section';
    section.innerHTML =
        '<h3>Dimension \u2194 Signal Correlations</h3>' +
        '<p class="section-note">Pearson r (Spearman \u03c1 in tooltip). Sample size: ' + data.correlations.sample_size + ' episodes.</p>';

    var html = '<table class="data-table correlation-table"><thead><tr><th>Dimension</th>';
    sigTypes.forEach(function (s) { html += '<th>' + escapeHtml(s.replace('_', ' ')) + '</th>'; });
    html += '</tr></thead><tbody>';

    dims.forEach(function (dim) {
        html += '<tr><td>' + escapeHtml(dim) + '</td>';
        sigTypes.forEach(function (sig) {
            var c = matrix[dim + '|' + sig];
            if (c) {
                var r = c.pearson_r;
                var color = correlationColor(r);
                html += '<td class="corr-cell" style="background:' + color + '" title="Pearson r=' +
                    r.toFixed(3) + ', Spearman \u03c1=' + c.spearman_rho.toFixed(3) + '">' +
                    r.toFixed(2) + '</td>';
            } else {
                html += '<td class="corr-cell" style="background:transparent">-</td>';
            }
        });
        html += '</tr>';
    });
    html += '</tbody></table>';
    section.innerHTML += html;
    el.appendChild(section);
}

function correlationColor(r) {
    var abs = Math.min(Math.abs(r), 1.0);
    var alpha = (abs * 0.6 + 0.05).toFixed(2);
    if (r >= 0) return 'rgba(52, 211, 153, ' + alpha + ')';
    return 'rgba(248, 113, 113, ' + alpha + ')';
}

// -- Weight evolution over versions (line chart) --------------------------

function renderWeightHistory(el, data) {
    var wh = data.weight_history;
    if (!wh || wh.length < 2) return;

    // Reverse so oldest first
    wh = wh.slice().reverse();

    var section = document.createElement('div');
    section.className = 'chart-section';
    section.innerHTML =
        '<h3>Weight Evolution</h3>' +
        '<p class="section-note">How dimension weights have changed across rubric versions</p>' +
        '<div class="chart-container" style="height:280px"><canvas id="rubric-weight-history"></canvas></div>';
    el.appendChild(section);

    var labels = wh.map(function (v) { return 'v' + v.version; });

    // Collect all dimension names
    var allDims = {};
    wh.forEach(function (v) {
        Object.keys(v.weights).forEach(function (d) { allDims[d] = true; });
    });

    var dimColors = ['#7c6af7', '#34d399', '#60a5fa', '#fb923c', '#f87171', '#a78bfa', '#fbbf24'];
    var datasets = Object.keys(allDims).map(function (dim, i) {
        return {
            label: dim,
            data: wh.map(function (v) { return v.weights[dim] || null; }),
            borderColor: dimColors[i % dimColors.length],
            backgroundColor: 'transparent',
            spanGaps: true,
        };
    });

    var ctx = document.getElementById('rubric-weight-history').getContext('2d');
    Dashboard.trackChart(new Chart(ctx, {
        type: 'line',
        data: { labels: labels, datasets: datasets },
        options: {
            plugins: { legend: { display: true, position: 'bottom' } },
            scales: {
                x: { title: { display: true, text: 'Version' } },
                y: { title: { display: true, text: 'Weight' }, min: 0, max: 0.5 }
            }
        }
    }));
}

// -- Version history table ------------------------------------------------

function renderVersionHistory(el, data) {
    var history = data.version_history;
    if (!history || history.length === 0) return;

    var section = document.createElement('div');
    section.className = 'table-section';
    section.innerHTML =
        '<h3>Version History</h3>';

    var html = '<table class="data-table"><thead><tr>' +
        '<th>Version</th><th>Status</th><th>Dimensions</th><th>Change Reason</th><th>Created</th>' +
        '</tr></thead><tbody>';

    history.forEach(function (v) {
        var statusClass = v.status === 'active' ? 'badge-success' :
                          v.status === 'rollback' ? 'badge-failure' : 'badge-pending';
        html += '<tr>' +
            '<td><strong>' + escapeHtml(v.version) + '</strong></td>' +
            '<td><span class="badge ' + statusClass + '">' + escapeHtml(v.status) + '</span></td>' +
            '<td>' + v.dimension_count + '</td>' +
            '<td>' + escapeHtml(v.change_reason) + '</td>' +
            '<td>' + Dashboard.formatDate(v.created_at) + '</td>' +
            '</tr>';
    });

    html += '</tbody></table>';
    section.innerHTML += html;
    el.appendChild(section);
}

// -- Recent outcome signals table -----------------------------------------

function renderRecentSignals(el, data) {
    var signals = data.outcome_signals.recent;
    if (!signals || signals.length === 0) return;

    var section = document.createElement('div');
    section.className = 'table-section';
    section.innerHTML = '<h3>Recent Outcome Signals</h3>';

    var signalColors = {
        completed: 'badge-success',
        praised: 'badge-success',
        corrected: 'badge-partial',
        reworked: 'badge-failure',
        self_corrected: 'badge-pending'
    };

    var html = '<table class="data-table"><thead><tr>' +
        '<th>Type</th><th>Confidence</th><th>Evidence</th><th>Detected</th>' +
        '</tr></thead><tbody>';

    signals.forEach(function (s) {
        var badge = signalColors[s.signal_type] || 'badge-pending';
        html += '<tr>' +
            '<td><span class="badge ' + badge + '">' + escapeHtml(s.signal_type.replace('_', ' ')) + '</span></td>' +
            '<td>' + (s.confidence * 100).toFixed(0) + '%</td>' +
            '<td class="content-cell">' + escapeHtml(Dashboard.truncate(s.evidence || '-', 120)) + '</td>' +
            '<td>' + (s.created_at ? Dashboard.formatDateTime(s.created_at) : '-') + '</td>' +
            '</tr>';
    });

    html += '</tbody></table>';
    section.innerHTML += html;
    el.appendChild(section);
}
