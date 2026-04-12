/**
 * Nous Dashboard — Density View (F040)
 *
 * Graph density metrics: orphan rates, degree distribution,
 * edge distribution, and backfill progress.
 * Fetches from GET /dashboard/density
 */

/* global Dashboard, escapeHtml */

Dashboard.registerView('density', async function (container) {
    Dashboard.showLoading(container);

    try {
        var data = await Dashboard.apiGet('/dashboard/density');
        renderDensity(container, data);
    } catch (err) {
        Dashboard.showError(container, 'Failed to load density data.', function () {
            Dashboard.reloadView('density');
        });
    }
});

function orphanRateClass(rate) {
    if (rate > 0.40) return 'badge-failure';
    if (rate > 0.15) return 'badge-partial';
    return 'badge-success';
}

function orphanRateLabel(rate) {
    var pct = (rate * 100).toFixed(1) + '%';
    if (rate > 0.40) return pct + ' (high)';
    if (rate > 0.15) return pct + ' (moderate)';
    return pct + ' (healthy)';
}

function renderDensity(container, data) {
    var html = '';

    // Header
    html += '<div class="view-header">';
    html += '<h2>Graph Density</h2>';
    html += '<p class="view-subtitle">Knowledge graph connectivity health and backfill progress</p>';
    html += '</div>';

    // Overview stat cards
    html += '<div class="stat-grid">';

    html += '<div class="stat-card">';
    html += '<div class="stat-label">Total Nodes</div>';
    html += '<div class="stat-value">' + Dashboard.formatNumber(data.total_nodes) + '</div>';
    html += '</div>';

    html += '<div class="stat-card">';
    html += '<div class="stat-label">Total Edges</div>';
    html += '<div class="stat-value">' + Dashboard.formatNumber(data.total_edges) + '</div>';
    html += '</div>';

    html += '<div class="stat-card">';
    html += '<div class="stat-label">Orphan Rate</div>';
    html += '<div class="stat-value"><span class="badge ' + orphanRateClass(data.orphan_rate) + '">' +
        orphanRateLabel(data.orphan_rate) + '</span></div>';
    html += '</div>';

    html += '<div class="stat-card">';
    html += '<div class="stat-label">Avg Degree</div>';
    html += '<div class="stat-value">' + (data.avg_degree || 0).toFixed(2) + '</div>';
    html += '</div>';

    html += '</div>';  // stat-grid

    // Two-column layout for tables
    html += '<div class="chart-grid">';

    // Orphan rate by type
    html += '<div class="chart-card">';
    html += '<h3 class="chart-title">Orphan Rate by Type</h3>';
    html += '<table class="data-table"><thead><tr>';
    html += '<th>Type</th><th>Total</th><th>Orphans</th><th>Rate</th>';
    html += '</tr></thead><tbody>';

    var types = Object.keys(data.density_by_type || {});
    if (types.length === 0) {
        html += '<tr><td colspan="4" style="text-align:center;color:#6b6b8a">No data</td></tr>';
    } else {
        types.forEach(function (t) {
            var d = data.density_by_type[t];
            html += '<tr>';
            html += '<td><span class="type-dot" style="background:' + Dashboard.typeColor(t) + '"></span>' + escapeHtml(t) + '</td>';
            html += '<td>' + Dashboard.formatNumber(d.total) + '</td>';
            html += '<td>' + Dashboard.formatNumber(d.orphan) + '</td>';
            html += '<td><span class="badge ' + orphanRateClass(d.orphan_rate) + '">' +
                (d.orphan_rate * 100).toFixed(1) + '%</span></td>';
            html += '</tr>';
        });
    }
    html += '</tbody></table></div>';

    // Edge distribution
    html += '<div class="chart-card">';
    html += '<h3 class="chart-title">Edge Distribution</h3>';
    html += '<table class="data-table"><thead><tr>';
    html += '<th>Relation</th><th>Count</th>';
    html += '</tr></thead><tbody>';

    var relations = Object.keys(data.edge_distribution || {});
    if (relations.length === 0) {
        html += '<tr><td colspan="2" style="text-align:center;color:#6b6b8a">No edges</td></tr>';
    } else {
        relations.forEach(function (rel) {
            html += '<tr>';
            html += '<td>' + escapeHtml(rel) + '</td>';
            html += '<td>' + Dashboard.formatNumber(data.edge_distribution[rel]) + '</td>';
            html += '</tr>';
        });
    }
    html += '</tbody></table></div>';

    html += '</div>';  // chart-grid

    // Backfill progress
    html += '<div class="chart-card" style="margin-top:1rem">';
    html += '<h3 class="chart-title">Backfill Progress (Last 7 Days)</h3>';
    html += '<table class="data-table"><thead><tr>';
    html += '<th>Date</th><th>Auto-linked Edges</th>';
    html += '</tr></thead><tbody>';

    var backfill = data.backfill_progress || [];
    if (backfill.length === 0) {
        html += '<tr><td colspan="2" style="text-align:center;color:#6b6b8a">No backfill activity</td></tr>';
    } else {
        backfill.forEach(function (row) {
            html += '<tr>';
            html += '<td>' + escapeHtml(row.date) + '</td>';
            html += '<td>' + Dashboard.formatNumber(row.edges) + '</td>';
            html += '</tr>';
        });
    }
    html += '</tbody></table></div>';

    // Summary footer
    html += '<div class="chart-card" style="margin-top:1rem">';
    html += '<div style="display:flex;gap:2rem;flex-wrap:wrap">';
    html += '<div><span style="color:#6b6b8a">Connected nodes:</span> ' + Dashboard.formatNumber(data.connected_nodes) + '</div>';
    html += '<div><span style="color:#6b6b8a">Total orphans:</span> ' + Dashboard.formatNumber(data.total_orphans) + '</div>';
    html += '</div></div>';

    container.innerHTML = html;
}
