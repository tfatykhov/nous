/**
 * Nous Dashboard — Graph Health View
 *
 * Graph density trends, edge creation rate, degree distribution, orphan tracking.
 * Fetches from GET /dashboard/health
 */

/* global Dashboard, Chart */

Dashboard.registerView('health', async function(container) {
    Dashboard.showLoading(container);

    var data;
    try {
        data = await Dashboard.apiGet('/dashboard/health?days=30');
    } catch (err) {
        Dashboard.showError(container, 'Failed to load graph health data.', function() {
            Dashboard.reloadView('health');
        });
        return;
    }

    // Check empty
    var hasData = (data.density_history && data.density_history.length > 0) ||
                  (data.daily_edges && data.daily_edges.length > 0) ||
                  (data.degree_distribution && data.degree_distribution.length > 0);

    if (!hasData) {
        Dashboard.showEmpty(container, 'No graph data yet. Graph edges are created as Nous links facts, episodes, and decisions.');
        return;
    }

    var html = '<div class="view-header"><h1>Graph Health</h1><p>Density trends, edge creation, and node connectivity</p></div>';

    // Summary cards
    html += '<div class="stat-grid">';
    if (data.density_history && data.density_history.length > 0) {
        var latestDensity = data.density_history[data.density_history.length - 1].density;
        var indicatorCls = latestDensity >= 3.0 ? 'good' : latestDensity >= 1.0 ? 'warn' : 'bad';
        html += '<div class="stat-card"><div class="stat-value"><span class="stat-indicator ' + indicatorCls + '"></span>' +
            latestDensity.toFixed(1) + '</div><div class="stat-label">Current Density</div></div>';
    }
    if (data.daily_edges && data.daily_edges.length > 0) {
        var totalEdges = data.daily_edges.reduce(function(sum, d) { return sum + d.count; }, 0);
        html += '<div class="stat-card"><div class="stat-value">' + totalEdges + '</div><div class="stat-label">Edges (30d)</div></div>';
        var autoEdges = data.daily_edges.reduce(function(sum, d) { return sum + (d.auto || 0); }, 0);
        var manualEdges = data.daily_edges.reduce(function(sum, d) { return sum + (d.manual || 0); }, 0);
        var autoPercent = totalEdges > 0 ? ((autoEdges / totalEdges) * 100).toFixed(0) : 0;
        html += '<div class="stat-card"><div class="stat-value">' + autoPercent + '%</div><div class="stat-label">Auto-linked</div></div>';
    }
    if (data.orphan_trend && data.orphan_trend.length > 0) {
        var latestOrphans = data.orphan_trend[data.orphan_trend.length - 1].count;
        html += '<div class="stat-card"><div class="stat-value">' + latestOrphans + '</div><div class="stat-label">Orphan Nodes</div></div>';
    }
    html += '</div>';

    // Charts
    html += '<div class="chart-grid">';
    html += '<div class="chart-card"><h3>Graph Density Over Time</h3><div class="chart-container tall"><canvas id="chart-density"></canvas></div></div>';
    html += '<div class="chart-card"><h3>Edge Creation Rate</h3><div class="chart-container tall"><canvas id="chart-edge-rate"></canvas></div></div>';
    html += '<div class="chart-card"><h3>Node Degree Distribution</h3><div class="chart-container tall"><canvas id="chart-degree"></canvas></div></div>';
    html += '<div class="chart-card"><h3>Orphan Nodes Over Time</h3><div class="chart-container tall"><canvas id="chart-orphans"></canvas></div></div>';
    html += '<div class="chart-card" style="grid-column:1/-1"><h3>Auto vs Manual Edges</h3><div class="chart-container tall"><canvas id="chart-auto-manual"></canvas></div></div>';
    html += '</div>';

    container.innerHTML = html;

    // Create charts
    createDensityChart(data.density_history);
    createEdgeRateChart(data.daily_edges);
    createDegreeChart(data.degree_distribution);
    createOrphanChart(data.orphan_trend);
    createAutoManualChart(data.daily_edges);
});

function createDensityChart(densityHistory) {
    var canvas = document.getElementById('chart-density');
    if (!canvas || !densityHistory || densityHistory.length === 0) return;

    // Target line plugin
    var targetLinePlugin = {
        id: 'targetLine',
        afterDraw: function(chart) {
            var yAxis = chart.scales.y;
            var xAxis = chart.scales.x;
            var yPixel = yAxis.getPixelForValue(3.0);
            var ctx = chart.ctx;
            ctx.save();
            ctx.strokeStyle = 'rgba(52, 211, 153, 0.5)';
            ctx.lineWidth = 1;
            ctx.setLineDash([5, 5]);
            ctx.beginPath();
            ctx.moveTo(xAxis.left, yPixel);
            ctx.lineTo(xAxis.right, yPixel);
            ctx.stroke();
            // Label
            ctx.fillStyle = 'rgba(52, 211, 153, 0.7)';
            ctx.font = '10px -apple-system, sans-serif';
            ctx.fillText('Target: 3.0', xAxis.right - 60, yPixel - 5);
            ctx.restore();
        }
    };

    var chart = new Chart(canvas, {
        type: 'line',
        data: {
            labels: densityHistory.map(function(d) { return d.date; }),
            datasets: [{
                label: 'Density',
                data: densityHistory.map(function(d) { return d.density; }),
                borderColor: '#7c6af7',
                backgroundColor: 'rgba(124, 106, 247, 0.1)',
                fill: true,
                pointBackgroundColor: densityHistory.map(function(d) {
                    return d.density >= 3.0 ? '#34d399' : d.density >= 1.0 ? '#fbbf24' : '#f87171';
                }),
                pointRadius: 4
            }]
        },
        options: {
            scales: {
                x: {
                    ticks: {
                        maxTicksLimit: 10,
                        callback: function(val) {
                            var label = this.getLabelForValue(val);
                            return label ? String(label).slice(5) : '';
                        }
                    },
                    grid: { display: false }
                },
                y: {
                    beginAtZero: true,
                    title: { display: true, text: 'Avg Edges per Node' }
                }
            },
            plugins: { legend: { display: false } }
        },
        plugins: [targetLinePlugin]
    });
    Dashboard.trackChart(chart);
}

function createEdgeRateChart(dailyEdges) {
    var canvas = document.getElementById('chart-edge-rate');
    if (!canvas || !dailyEdges || dailyEdges.length === 0) return;

    var chart = new Chart(canvas, {
        type: 'bar',
        data: {
            labels: dailyEdges.map(function(d) { return d.date; }),
            datasets: [{
                label: 'Edges Created',
                data: dailyEdges.map(function(d) { return d.count; }),
                backgroundColor: 'rgba(124, 106, 247, 0.5)',
                borderColor: '#7c6af7',
                borderWidth: 1
            }]
        },
        options: {
            scales: {
                x: {
                    ticks: {
                        maxTicksLimit: 15,
                        callback: function(val) {
                            var label = this.getLabelForValue(val);
                            return label ? String(label).slice(5) : '';
                        }
                    },
                    grid: { display: false }
                },
                y: { beginAtZero: true, ticks: { precision: 0 } }
            },
            plugins: { legend: { display: false } }
        }
    });
    Dashboard.trackChart(chart);
}

function createDegreeChart(degreeDistribution) {
    var canvas = document.getElementById('chart-degree');
    if (!canvas || !degreeDistribution || degreeDistribution.length === 0) return;

    var chart = new Chart(canvas, {
        type: 'bar',
        data: {
            labels: degreeDistribution.map(function(d) { return d.degree; }),
            datasets: [{
                label: 'Node Count',
                data: degreeDistribution.map(function(d) { return d.count; }),
                backgroundColor: 'rgba(96, 165, 250, 0.5)',
                borderColor: '#60a5fa',
                borderWidth: 1
            }]
        },
        options: {
            scales: {
                x: {
                    title: { display: true, text: 'Edge Count (degree)' },
                    grid: { display: false }
                },
                y: {
                    beginAtZero: true,
                    ticks: { precision: 0 },
                    title: { display: true, text: 'Number of Nodes' }
                }
            },
            plugins: { legend: { display: false } }
        }
    });
    Dashboard.trackChart(chart);
}

function createOrphanChart(orphanTrend) {
    var canvas = document.getElementById('chart-orphans');
    if (!canvas || !orphanTrend || orphanTrend.length === 0) return;

    var chart = new Chart(canvas, {
        type: 'line',
        data: {
            labels: orphanTrend.map(function(d) { return d.date; }),
            datasets: [{
                label: 'Orphan Nodes',
                data: orphanTrend.map(function(d) { return d.count; }),
                borderColor: '#f87171',
                backgroundColor: 'rgba(248, 113, 113, 0.1)',
                fill: true,
                pointBackgroundColor: '#f87171'
            }]
        },
        options: {
            scales: {
                x: {
                    ticks: {
                        maxTicksLimit: 10,
                        callback: function(val) {
                            var label = this.getLabelForValue(val);
                            return label ? String(label).slice(5) : '';
                        }
                    },
                    grid: { display: false }
                },
                y: {
                    beginAtZero: true,
                    ticks: { precision: 0 },
                    title: { display: true, text: 'Orphan Count' }
                }
            },
            plugins: { legend: { display: false } }
        }
    });
    Dashboard.trackChart(chart);
}

function createAutoManualChart(dailyEdges) {
    var canvas = document.getElementById('chart-auto-manual');
    if (!canvas || !dailyEdges || dailyEdges.length === 0) return;

    var chart = new Chart(canvas, {
        type: 'bar',
        data: {
            labels: dailyEdges.map(function(d) { return d.date; }),
            datasets: [
                {
                    label: 'Auto-linked',
                    data: dailyEdges.map(function(d) { return d.auto || 0; }),
                    backgroundColor: 'rgba(52, 211, 153, 0.5)',
                    borderColor: '#34d399',
                    borderWidth: 1
                },
                {
                    label: 'Manual',
                    data: dailyEdges.map(function(d) { return d.manual || 0; }),
                    backgroundColor: 'rgba(124, 106, 247, 0.5)',
                    borderColor: '#7c6af7',
                    borderWidth: 1
                }
            ]
        },
        options: {
            scales: {
                x: {
                    stacked: true,
                    ticks: {
                        maxTicksLimit: 15,
                        callback: function(val) {
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
            plugins: { legend: { position: 'bottom' } }
        }
    });
    Dashboard.trackChart(chart);
}
