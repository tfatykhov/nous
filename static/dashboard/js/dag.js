/**
 * Nous Dashboard -- DAG Orchestrator View (F038)
 *
 * Active/recent DAGs, wave-based graph visualization,
 * node detail panel, budget chart. Auto-refreshes every 15 seconds.
 * Fetches from GET /dashboard/dag
 */

/* global Dashboard, Chart, escapeHtml, d3 */

var _dagRefreshInterval = null;
var _dagRefreshInFlight = false;
var _dagAbortController = null;
var _dagExpandedIds = new Set();   // Recent DAG detail rows that are open
var _dagActiveGraphId = null;      // Active DAG whose graph is displayed

Dashboard.registerView('dag', async function (container) {
    Dashboard.showLoading(container);

    try {
        var data = await dagFetch();
        renderDag(container, data);
        startDagAutoRefresh(container);
    } catch (err) {
        Dashboard.showError(container, 'Failed to load DAG data.', function () {
            Dashboard.reloadView('dag');
        });
    }
});

function dagFetch() {
    if (_dagAbortController) {
        _dagAbortController.abort();
    }
    _dagAbortController = new AbortController();
    return fetch('/dashboard/dag', { signal: _dagAbortController.signal })
        .then(function (res) {
            if (!res.ok) throw new Error(res.status + ' ' + res.statusText);
            return res.json();
        })
        .finally(function () {
            _dagAbortController = null;
        });
}

function startDagAutoRefresh(container) {
    stopDagAutoRefresh();
    _dagRefreshInterval = setInterval(async function () {
        if (Dashboard.currentView !== 'dag') {
            stopDagAutoRefresh();
            return;
        }
        if (_dagRefreshInFlight) return;
        _dagRefreshInFlight = true;
        try {
            var data = await dagFetch();
            renderDag(container, data);
        } catch (err) {
            // Silently skip (including aborted requests)
        } finally {
            _dagRefreshInFlight = false;
        }
    }, 15000);
}

function stopDagAutoRefresh() {
    if (_dagRefreshInterval) {
        clearInterval(_dagRefreshInterval);
        _dagRefreshInterval = null;
    }
    if (_dagAbortController) {
        _dagAbortController.abort();
        _dagAbortController = null;
    }
}

// -- Helpers ---------------------------------------------------------------

function dagStatusBadge(status) {
    var colors = {
        pending: '#6b6b8a',
        ready: '#22d3ee',
        running: '#fbbf24',
        completed: '#4ade80',
        failed: '#f87171',
        blocked: '#991b1b',
        cancelled: '#4b4b5a',
        partial: '#fb923c'
    };
    var color = colors[status] || '#6b6b8a';
    return '<span style="display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;background:' +
        color + '20;color:' + color + ';border:1px solid ' + color + '40">' + escapeHtml(status) + '</span>';
}

function dagHumanizeDuration(seconds) {
    if (seconds == null || seconds === 0) return '--';
    if (seconds < 60) return Math.round(seconds) + 's';
    if (seconds < 3600) return (seconds / 60).toFixed(1) + 'm';
    return (seconds / 3600).toFixed(1) + 'h';
}

function dagBuildStatCard(label, value, sub, color) {
    return '<div class="stat-card">' +
        '<div class="stat-value" style="color:' + color + '">' + escapeHtml(String(value)) + '</div>' +
        '<div class="stat-label">' + escapeHtml(label) + '</div>' +
        (sub ? '<div style="font-size:11px;color:var(--muted);margin-top:2px">' + escapeHtml(sub) + '</div>' : '') +
        '</div>';
}

function dagHumanizeAgo(isoString) {
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

// -- Main render -----------------------------------------------------------

function renderDag(container, data) {
    // Destroy existing charts before re-render
    if (Dashboard.charts['dag']) {
        Dashboard.charts['dag'].forEach(function (c) {
            try { c.destroy(); } catch (e) { /* ignore */ }
        });
        Dashboard.charts['dag'] = [];
    }

    var stats = data.stats || {};
    var activeDags = data.active_dags || [];
    var recentDags = data.recent_dags || [];

    // Prune expanded IDs that no longer exist in data
    var recentIds = new Set(recentDags.map(function (d) { return d.id; }));
    _dagExpandedIds.forEach(function (id) {
        if (!recentIds.has(id)) _dagExpandedIds.delete(id);
    });

    var html = '<div class="view-header">' +
        '<h1>DAG Orchestrator</h1>' +
        '<p class="view-subtitle">Unified execution DAGs, node progress, and graph visualization</p>' +
        '</div>';

    // -- Stat Cards --
    var successPct = stats.success_rate != null ? Math.round(stats.success_rate * 100) + '%' : '--';
    html += '<div class="stat-grid">';
    html += dagBuildStatCard('Active DAGs', String(stats.active_count || 0), '', 'var(--accent)');
    html += dagBuildStatCard('Nodes (24h)', String(stats.nodes_completed_24h || 0), 'completed', 'var(--green)');
    html += dagBuildStatCard('Success Rate', successPct, '', stats.success_rate >= 0.8 ? 'var(--green)' : 'var(--yellow)');
    html += dagBuildStatCard('Avg Duration', dagHumanizeDuration(stats.avg_completion_seconds), '', 'var(--muted)');
    html += '</div>';

    // -- Active DAGs Table --
    html += '<div class="chart-card mb-24"><h3>Active DAGs</h3>';
    if (activeDags.length > 0) {
        html += '<table class="dag-list-table">';
        html += '<thead><tr><th>Name</th><th>Status</th><th>Source</th><th>Progress</th><th>Created</th><th></th></tr></thead>';
        html += '<tbody>';
        activeDags.forEach(function (dag, idx) {
            var nodes = dag.nodes || [];
            var completedNodes = nodes.filter(function (n) { return n.status === 'completed'; }).length;
            var totalNodes = nodes.length;
            var pct = totalNodes > 0 ? Math.round((completedNodes / totalNodes) * 100) : 0;

            html += '<tr>';
            html += '<td><strong>' + escapeHtml(dag.name) + '</strong></td>';
            html += '<td>' + dagStatusBadge(dag.status) + '</td>';
            html += '<td style="color:var(--muted);font-size:12px">' + escapeHtml(dag.source) + '</td>';
            html += '<td style="min-width:120px">' +
                '<div class="dag-progress"><div class="dag-progress-fill" style="width:' + pct + '%"></div></div>' +
                '<div style="font-size:11px;color:var(--muted);margin-top:2px">' + completedNodes + '/' + totalNodes + ' nodes</div>' +
                '</td>';
            html += '<td style="color:var(--muted);font-size:12px">' + dagHumanizeAgo(dag.created_at) + '</td>';
            html += '<td><button class="btn btn-sm dag-graph-btn" data-dag-idx="' + idx + '">View Graph</button></td>';
            html += '</tr>';
        });
        html += '</tbody></table>';
    } else {
        html += '<div class="empty-state" style="padding:24px"><p>No active DAGs</p></div>';
    }
    html += '</div>';

    // -- Graph Container (hidden by default) --
    html += '<div id="dag-graph-section" class="chart-card mb-24" style="display:none">';
    html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">';
    html += '<h3 id="dag-graph-title">DAG Graph</h3>';
    html += '<button class="btn btn-sm" id="dag-graph-close">Close</button>';
    html += '</div>';
    html += '<div class="dag-graph-container" id="dag-graph-container">';
    html += '<div class="dag-node-detail" id="dag-node-detail"></div>';
    html += '</div>';
    html += '</div>';

    // -- Recent DAGs Table --
    html += '<div class="chart-card mb-24"><h3>Recent DAGs</h3>';
    if (recentDags.length > 0) {
        html += '<table class="dag-list-table">';
        html += '<thead><tr><th></th><th>Name</th><th>Status</th><th>Nodes</th><th>Tokens</th><th>Completed</th></tr></thead>';
        html += '<tbody>';
        recentDags.forEach(function (dag) {
            var isOpen = _dagExpandedIds.has(dag.id);
            var arrowChar = isOpen ? '\u25BC' : '\u25B6';

            html += '<tr class="dag-recent-row" data-dag-id="' + escapeHtml(dag.id) + '" style="cursor:pointer">';
            html += '<td class="dag-expand-arrow" style="width:20px;font-size:10px;color:var(--muted)">' + arrowChar + '</td>';
            html += '<td><strong>' + escapeHtml(dag.name) + '</strong></td>';
            html += '<td>' + dagStatusBadge(dag.status) + '</td>';
            html += '<td style="font-size:12px">' + (dag.completed_count || 0) + '/' + (dag.node_count || 0) + '</td>';
            html += '<td style="font-size:12px;color:var(--muted)">' + Dashboard.formatNumber(dag.tokens_consumed || 0) + '</td>';
            html += '<td style="font-size:12px;color:var(--muted)">' + dagHumanizeAgo(dag.completed_at) + '</td>';
            html += '</tr>';

            // Detail row
            html += '<tr class="dag-detail-row" data-dag-id="' + escapeHtml(dag.id) + '" style="display:' + (isOpen ? 'table-row' : 'none') + '">';
            html += '<td colspan="6" style="padding:0">';
            html += '<div class="dag-detail-content">';

            // Source + duration
            var duration = '';
            if (dag.created_at && dag.completed_at) {
                var secs = (new Date(dag.completed_at) - new Date(dag.created_at)) / 1000;
                duration = dagHumanizeDuration(secs);
            }
            html += '<div class="dag-detail-grid">';
            html += '<div><span class="dag-detail-label">Source</span><span>' + escapeHtml(dag.source || '--') + '</span></div>';
            html += '<div><span class="dag-detail-label">Duration</span><span>' + (duration || '--') + '</span></div>';
            html += '<div><span class="dag-detail-label">Token Budget</span><span>' + Dashboard.formatNumber(dag.token_budget || 0) + '</span></div>';
            html += '<div><span class="dag-detail-label">Created</span><span>' + dagHumanizeAgo(dag.created_at) + '</span></div>';
            html += '</div>';

            // Result summary
            if (dag.result_summary) {
                html += '<div class="dag-detail-section">';
                html += '<div class="dag-detail-label">Result Summary</div>';
                html += '<div class="dag-detail-text">' + escapeHtml(dag.result_summary) + '</div>';
                html += '</div>';
            }

            // Postmortem
            if (dag.postmortem) {
                html += '<div class="dag-detail-section">';
                html += '<div class="dag-detail-label">Postmortem</div>';
                html += '<div class="dag-detail-text dag-detail-postmortem">' + escapeHtml(dag.postmortem) + '</div>';
                html += '</div>';
            }

            html += '</div></td></tr>';
        });
        html += '</tbody></table>';
    } else {
        html += '<div class="empty-state" style="padding:24px"><p>No completed DAGs yet</p></div>';
    }
    html += '</div>';

    // -- Budget Chart --
    var dagsWithBudget = activeDags.filter(function (d) { return d.token_budget && d.token_budget > 0; });
    if (dagsWithBudget.length > 0) {
        html += '<div class="chart-card mb-24"><h3>Token Budgets</h3>';
        html += '<div class="chart-container" style="position:relative;max-height:280px"><canvas id="chart-dag-budget"></canvas></div>';
        html += '</div>';
    }

    container.innerHTML = html;

    // -- Wire up graph buttons --
    var graphBtns = container.querySelectorAll('.dag-graph-btn');
    graphBtns.forEach(function (btn) {
        btn.addEventListener('click', function () {
            var idx = parseInt(btn.getAttribute('data-dag-idx'), 10);
            var dag = activeDags[idx];
            if (dag) {
                _dagActiveGraphId = dag.id;
                showDagGraph(dag);
            }
        });
    });

    var closeBtn = document.getElementById('dag-graph-close');
    if (closeBtn) {
        closeBtn.addEventListener('click', function () {
            _dagActiveGraphId = null;
            var section = document.getElementById('dag-graph-section');
            if (section) section.style.display = 'none';
        });
    }

    // Re-open active graph after refresh
    if (_dagActiveGraphId) {
        var graphDag = activeDags.find(function (d) { return d.id === _dagActiveGraphId; });
        if (graphDag) {
            showDagGraph(graphDag);
        } else {
            _dagActiveGraphId = null;
        }
    }

    // -- Wire up recent DAG detail toggles --
    var recentRows = container.querySelectorAll('.dag-recent-row');
    recentRows.forEach(function (row) {
        row.addEventListener('click', function () {
            var dagId = row.getAttribute('data-dag-id');
            var detailRow = container.querySelector('.dag-detail-row[data-dag-id="' + dagId + '"]');
            var arrow = row.querySelector('.dag-expand-arrow');
            if (!detailRow) return;

            if (_dagExpandedIds.has(dagId)) {
                _dagExpandedIds.delete(dagId);
                detailRow.style.display = 'none';
                if (arrow) arrow.textContent = '\u25B6';
            } else {
                _dagExpandedIds.add(dagId);
                detailRow.style.display = 'table-row';
                if (arrow) arrow.textContent = '\u25BC';
            }
        });
    });

    // -- Create budget chart --
    if (dagsWithBudget.length > 0) {
        createDagBudgetChart(dagsWithBudget);
    }
}

// -- DAG Graph Visualization (D3, wave-based layout) ----------------------

function showDagGraph(dag) {
    var section = document.getElementById('dag-graph-section');
    var graphContainer = document.getElementById('dag-graph-container');
    var titleEl = document.getElementById('dag-graph-title');
    var detailPanel = document.getElementById('dag-node-detail');

    if (!section || !graphContainer) return;

    section.style.display = 'block';
    if (titleEl) titleEl.textContent = dag.name;
    if (detailPanel) {
        detailPanel.className = 'dag-node-detail';
        detailPanel.innerHTML = '';
    }

    // Clear previous SVG
    var existingSvg = graphContainer.querySelector('svg');
    if (existingSvg) existingSvg.remove();

    var nodes = dag.nodes || [];
    var edges = dag.edges || [];

    if (nodes.length === 0) {
        graphContainer.innerHTML = '<div class="dag-node-detail" id="dag-node-detail"></div>' +
            '<div class="empty-state" style="padding:40px"><p>No nodes in this DAG</p></div>';
        return;
    }

    // Group nodes by wave
    var waveGroups = {};
    var maxWave = 0;
    nodes.forEach(function (n) {
        var w = n.wave || 0;
        if (!waveGroups[w]) waveGroups[w] = [];
        waveGroups[w].push(n);
        if (w > maxWave) maxWave = w;
    });

    // Layout constants
    var marginLeft = 80;
    var marginTop = 50;
    var waveSpacing = 160;
    var nodeSpacing = 100;
    var nodeRadius = 22;
    var width = marginLeft + (maxWave + 1) * waveSpacing + 60;
    var maxNodesInWave = 0;
    for (var w = 0; w <= maxWave; w++) {
        var count = (waveGroups[w] || []).length;
        if (count > maxNodesInWave) maxNodesInWave = count;
    }
    var height = marginTop + maxNodesInWave * nodeSpacing + 40;

    // Assign positions
    var nodePositions = {};
    for (var wv = 0; wv <= maxWave; wv++) {
        var group = waveGroups[wv] || [];
        var groupHeight = group.length * nodeSpacing;
        var startY = marginTop + (height - marginTop - 40 - groupHeight) / 2 + nodeSpacing / 2;
        group.forEach(function (n, i) {
            nodePositions[n.id] = {
                x: marginLeft + wv * waveSpacing,
                y: startY + i * nodeSpacing
            };
        });
    }

    // Status colors
    var statusColors = {
        pending: '#6b6b8a',
        ready: '#22d3ee',
        running: '#fbbf24',
        completed: '#4ade80',
        failed: '#f87171',
        blocked: '#991b1b',
        cancelled: '#4b4b5a'
    };

    var svg = d3.select(graphContainer)
        .append('svg')
        .attr('width', width)
        .attr('height', height)
        .style('display', 'block')
        .style('margin', '0 auto');

    // Arrow marker
    svg.append('defs').append('marker')
        .attr('id', 'dag-arrow')
        .attr('viewBox', '0 0 10 10')
        .attr('refX', 10)
        .attr('refY', 5)
        .attr('markerWidth', 8)
        .attr('markerHeight', 8)
        .attr('orient', 'auto')
        .append('path')
        .attr('d', 'M0,0 L10,5 L0,10 Z')
        .attr('fill', 'var(--muted)');

    // Wave lane labels
    for (var wl = 0; wl <= maxWave; wl++) {
        svg.append('text')
            .attr('class', 'dag-wave-label')
            .attr('x', marginLeft + wl * waveSpacing)
            .attr('y', 20)
            .text('Wave ' + wl);
    }

    // Edges
    edges.forEach(function (edge) {
        var from = nodePositions[edge.from_node_id];
        var to = nodePositions[edge.to_node_id];
        if (!from || !to) return;

        var edgeClass = 'dag-edge ' + (edge.edge_type || 'dependency');
        svg.append('line')
            .attr('class', edgeClass)
            .attr('x1', from.x + nodeRadius)
            .attr('y1', from.y)
            .attr('x2', to.x - nodeRadius)
            .attr('y2', to.y)
            .attr('marker-end', 'url(#dag-arrow)');
    });

    // Nodes
    var nodeGroups = svg.selectAll('.dag-node')
        .data(nodes)
        .enter()
        .append('g')
        .attr('class', 'dag-node')
        .attr('transform', function (d) {
            var pos = nodePositions[d.id];
            return 'translate(' + pos.x + ',' + pos.y + ')';
        })
        .on('click', function (event, d) {
            showNodeDetail(d, detailPanel);
        });

    // Node shapes by type
    nodeGroups.each(function (d) {
        var g = d3.select(this);
        var color = statusColors[d.status] || '#6b6b8a';
        var isRunning = d.status === 'running';

        if (d.node_type === 'check') {
            // Diamond
            g.append('polygon')
                .attr('points', '0,-' + nodeRadius + ' ' + nodeRadius + ',0 0,' + nodeRadius + ' -' + nodeRadius + ',0')
                .attr('fill', color + '30')
                .attr('stroke', color)
                .attr('stroke-width', 2)
                .style('animation', isRunning ? 'pulse-running 1.5s infinite' : 'none');
        } else if (d.node_type === 'gate') {
            // Hexagon
            var r = nodeRadius;
            var hex = [];
            for (var i = 0; i < 6; i++) {
                var angle = (Math.PI / 3) * i - Math.PI / 6;
                hex.push(Math.cos(angle) * r + ',' + Math.sin(angle) * r);
            }
            g.append('polygon')
                .attr('points', hex.join(' '))
                .attr('fill', color + '30')
                .attr('stroke', color)
                .attr('stroke-width', 2)
                .style('animation', isRunning ? 'pulse-running 1.5s infinite' : 'none');
        } else if (d.node_type === 'callback') {
            // Triangle
            g.append('polygon')
                .attr('points', '0,-' + nodeRadius + ' ' + nodeRadius + ',' + (nodeRadius * 0.8) + ' -' + nodeRadius + ',' + (nodeRadius * 0.8))
                .attr('fill', color + '30')
                .attr('stroke', color)
                .attr('stroke-width', 2)
                .style('animation', isRunning ? 'pulse-running 1.5s infinite' : 'none');
        } else {
            // Circle (subtask - default)
            g.append('circle')
                .attr('r', nodeRadius)
                .attr('fill', color + '30')
                .attr('stroke', color)
                .attr('stroke-width', 2)
                .style('animation', isRunning ? 'pulse-running 1.5s infinite' : 'none');
        }

        // Label
        g.append('text')
            .attr('class', 'dag-node-label')
            .attr('dy', nodeRadius + 16)
            .text(d.name.length > 12 ? d.name.slice(0, 11) + '\u2026' : d.name);
    });
}

function showNodeDetail(node, panel) {
    if (!panel) return;
    panel.className = 'dag-node-detail visible';

    var html = '<div style="margin-bottom:12px">';
    html += '<strong style="font-size:14px">' + escapeHtml(node.name) + '</strong>';
    html += '<div style="margin-top:4px">' + dagStatusBadge(node.status) + '</div>';
    html += '</div>';

    html += '<div style="margin-bottom:8px"><span style="color:var(--muted)">Type:</span> ' + escapeHtml(node.node_type) + '</div>';
    html += '<div style="margin-bottom:8px"><span style="color:var(--muted)">Wave:</span> ' + node.wave + '</div>';
    html += '<div style="margin-bottom:8px"><span style="color:var(--muted)">Tokens:</span> ' + Dashboard.formatNumber(node.tokens_used || 0) + '</div>';

    if (node.started_at) {
        html += '<div style="margin-bottom:8px"><span style="color:var(--muted)">Started:</span> ' + dagHumanizeAgo(node.started_at) + '</div>';
    }
    if (node.completed_at) {
        html += '<div style="margin-bottom:8px"><span style="color:var(--muted)">Completed:</span> ' + dagHumanizeAgo(node.completed_at) + '</div>';
    }
    if (node.description) {
        html += '<div style="margin-top:12px;padding-top:12px;border-top:1px solid var(--border)">';
        html += '<div style="color:var(--muted);font-size:11px;margin-bottom:4px">Description</div>';
        html += '<div style="font-size:12px">' + escapeHtml(node.description) + '</div>';
        html += '</div>';
    }
    if (node.result) {
        html += '<div style="margin-top:12px;padding-top:12px;border-top:1px solid var(--border)">';
        html += '<div style="color:var(--muted);font-size:11px;margin-bottom:4px">Result</div>';
        html += '<pre style="font-size:11px;white-space:pre-wrap;word-break:break-word;background:rgba(255,255,255,0.03);padding:8px;border-radius:4px">' + escapeHtml(node.result) + '</pre>';
        html += '</div>';
    }
    if (node.error) {
        html += '<div style="margin-top:12px;padding-top:12px;border-top:1px solid var(--border)">';
        html += '<div style="color:var(--red);font-size:11px;margin-bottom:4px">Error</div>';
        html += '<pre style="font-size:11px;white-space:pre-wrap;word-break:break-word;background:rgba(248,113,113,0.05);padding:8px;border-radius:4px;color:var(--red)">' + escapeHtml(node.error) + '</pre>';
        html += '</div>';
    }

    panel.innerHTML = html;
}

// -- Budget Chart (Chart.js stacked bar) -----------------------------------

function createDagBudgetChart(dags) {
    var canvas = document.getElementById('chart-dag-budget');
    if (!canvas) return;

    var labels = dags.map(function (d) {
        return d.name.length > 20 ? d.name.slice(0, 19) + '\u2026' : d.name;
    });
    var used = dags.map(function (d) { return d.tokens_consumed || 0; });
    var remaining = dags.map(function (d) {
        var budget = d.token_budget || 0;
        var consumed = d.tokens_consumed || 0;
        return Math.max(0, budget - consumed);
    });

    var chart = new Chart(canvas, {
        type: 'bar',
        data: {
            labels: labels,
            datasets: [
                {
                    label: 'Used',
                    data: used,
                    backgroundColor: '#22d3ee',
                    borderWidth: 0
                },
                {
                    label: 'Remaining',
                    data: remaining,
                    backgroundColor: 'rgba(255,255,255,0.06)',
                    borderWidth: 0
                }
            ]
        },
        options: {
            indexAxis: 'y',
            scales: {
                x: {
                    stacked: true,
                    beginAtZero: true,
                    ticks: { precision: 0 }
                },
                y: {
                    stacked: true,
                    grid: { display: false }
                }
            },
            plugins: {
                legend: { position: 'bottom' }
            }
        }
    });
    Dashboard.trackChart(chart);
}
