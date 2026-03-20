/**
 * Nous Dashboard — Decision Intelligence View
 *
 * Calibration curves, confidence distributions, outcome analytics.
 * Fetches from GET /dashboard/calibration
 */

/* global Dashboard, Chart, escapeHtml */

Dashboard.registerView('decisions', async function(container) {
    Dashboard.showLoading(container);

    var data;
    try {
        data = await Dashboard.apiGet('/dashboard/calibration');
    } catch (err) {
        Dashboard.showError(container, 'Failed to load decision intelligence data.', function() {
            Dashboard.reloadView('decisions');
        });
        return;
    }

    // Check empty
    var hasData = (data.calibration_curve && data.calibration_curve.length > 0) ||
                  (data.daily_decisions && data.daily_decisions.length > 0) ||
                  (data.confidence_histogram && data.confidence_histogram.length > 0);

    if (!hasData) {
        Dashboard.showEmpty(container, 'No decisions recorded yet. Decisions are created when Nous makes significant choices.');
        return;
    }

    var html = '<div class="view-header"><h1>Decision Intelligence</h1><p>Calibration, confidence, and reasoning analytics</p></div>';

    html += '<div class="chart-grid">';
    html += '<div class="chart-card"><h3>Calibration Curve</h3><div class="chart-container tall"><canvas id="chart-calibration"></canvas></div></div>';
    html += '<div class="chart-card"><h3>Confidence Distribution</h3><div class="chart-container tall"><canvas id="chart-confidence"></canvas></div></div>';
    html += '<div class="chart-card"><h3>Outcome by Category</h3><div class="chart-container tall"><canvas id="chart-outcome-cat"></canvas></div></div>';
    html += '<div class="chart-card"><h3>Outcome by Stakes</h3><div class="chart-container tall"><canvas id="chart-outcome-stakes"></canvas></div></div>';
    html += '<div class="chart-card"><h3>Reason Type Usage</h3><div class="chart-container tall"><canvas id="chart-reasons"></canvas></div></div>';
    html += '<div class="chart-card"><h3>Brier Score Over Time</h3><div class="chart-container tall"><canvas id="chart-brier"></canvas></div></div>';
    html += '<div class="chart-card"><h3>Decisions Per Day</h3><div class="chart-container tall"><canvas id="chart-daily"></canvas></div></div>';
    html += '</div>';

    container.innerHTML = html;

    // Create charts
    createCalibrationChart(data.calibration_curve);
    createConfidenceChart(data.confidence_histogram);
    createOutcomeByCategoryChart(data.outcome_by_category);
    createOutcomeByStakesChart(data.outcome_by_stakes);
    createReasonTypeChart(data.reason_type_stats);
    createBrierChart(data.brier_history);
    createDailyDecisionsChart(data.daily_decisions);
});

function createCalibrationChart(curveData) {
    var canvas = document.getElementById('chart-calibration');
    if (!canvas) return;
    if (!curveData || curveData.length === 0) {
        canvas.parentElement.innerHTML = '<div class="empty-state" style="padding:40px 0"><p class="text-muted">Not enough data for calibration curve</p></div>';
        return;
    }

    // Custom plugin for diagonal reference line
    var diagonalPlugin = {
        id: 'diagonalLine',
        afterDraw: function(chart) {
            var ctx = chart.ctx;
            var xAxis = chart.scales.x;
            var yAxis = chart.scales.y;
            ctx.save();
            ctx.strokeStyle = 'rgba(107, 107, 138, 0.4)';
            ctx.lineWidth = 1;
            ctx.setLineDash([5, 5]);
            ctx.beginPath();
            ctx.moveTo(xAxis.left, yAxis.bottom);
            ctx.lineTo(xAxis.right, yAxis.top);
            ctx.stroke();
            ctx.restore();
        }
    };

    var chart = new Chart(canvas, {
        type: 'line',
        data: {
            labels: curveData.map(function(d) { return d.bucket; }),
            datasets: [{
                label: 'Actual Success Rate',
                data: curveData.map(function(d) { return d.actual_success_rate; }),
                borderColor: '#7c6af7',
                backgroundColor: 'rgba(124, 106, 247, 0.1)',
                fill: true,
                pointBackgroundColor: '#7c6af7',
                pointRadius: 5
            }]
        },
        options: {
            scales: {
                x: { title: { display: true, text: 'Predicted Confidence' } },
                y: { title: { display: true, text: 'Actual Success Rate' }, min: 0, max: 1 }
            },
            plugins: { legend: { display: false } }
        },
        plugins: [diagonalPlugin]
    });
    Dashboard.trackChart(chart);
}

function createConfidenceChart(histogram) {
    var canvas = document.getElementById('chart-confidence');
    if (!canvas) return;
    if (!histogram || histogram.length === 0) {
        canvas.parentElement.innerHTML = '<div class="empty-state" style="padding:40px 0"><p class="text-muted">No confidence data</p></div>';
        return;
    }

    var chart = new Chart(canvas, {
        type: 'bar',
        data: {
            labels: histogram.map(function(d) { return d.range; }),
            datasets: [{
                label: 'Count',
                data: histogram.map(function(d) { return d.count; }),
                backgroundColor: 'rgba(124, 106, 247, 0.5)',
                borderColor: '#7c6af7',
                borderWidth: 1
            }]
        },
        options: {
            scales: {
                x: { title: { display: true, text: 'Confidence Range' } },
                y: { beginAtZero: true, ticks: { precision: 0 }, title: { display: true, text: 'Count' } }
            },
            plugins: { legend: { display: false } }
        }
    });
    Dashboard.trackChart(chart);
}

function createOutcomeByCategoryChart(outcomeByCategory) {
    var canvas = document.getElementById('chart-outcome-cat');
    if (!canvas) return;
    if (!outcomeByCategory || Object.keys(outcomeByCategory).length === 0) {
        canvas.parentElement.innerHTML = '<div class="empty-state" style="padding:40px 0"><p class="text-muted">No category data</p></div>';
        return;
    }

    var categories = Object.keys(outcomeByCategory);
    var outcomes = ['success', 'partial', 'failure', 'pending'];
    var outcomeColors = { success: '#34d399', partial: '#fbbf24', failure: '#f87171', pending: '#6b6b8a' };

    var datasets = outcomes.map(function(outcome) {
        return {
            label: outcome.charAt(0).toUpperCase() + outcome.slice(1),
            data: categories.map(function(cat) { return (outcomeByCategory[cat] || {})[outcome] || 0; }),
            backgroundColor: outcomeColors[outcome]
        };
    });

    var chart = new Chart(canvas, {
        type: 'bar',
        data: {
            labels: categories.map(function(c) { return c.charAt(0).toUpperCase() + c.slice(1); }),
            datasets: datasets
        },
        options: {
            scales: {
                x: { stacked: true, grid: { display: false } },
                y: { stacked: true, beginAtZero: true, ticks: { precision: 0 } }
            },
            plugins: { legend: { position: 'bottom' } }
        }
    });
    Dashboard.trackChart(chart);
}

function createOutcomeByStakesChart(outcomeByStakes) {
    var canvas = document.getElementById('chart-outcome-stakes');
    if (!canvas) return;
    if (!outcomeByStakes || Object.keys(outcomeByStakes).length === 0) {
        canvas.parentElement.innerHTML = '<div class="empty-state" style="padding:40px 0"><p class="text-muted">No stakes data</p></div>';
        return;
    }

    var stakesLevels = Object.keys(outcomeByStakes);
    var outcomes = ['success', 'partial', 'failure', 'pending'];
    var outcomeColors = { success: '#34d399', partial: '#fbbf24', failure: '#f87171', pending: '#6b6b8a' };

    var datasets = outcomes.map(function(outcome) {
        return {
            label: outcome.charAt(0).toUpperCase() + outcome.slice(1),
            data: stakesLevels.map(function(s) { return (outcomeByStakes[s] || {})[outcome] || 0; }),
            backgroundColor: outcomeColors[outcome]
        };
    });

    var chart = new Chart(canvas, {
        type: 'bar',
        data: {
            labels: stakesLevels.map(function(s) { return s.charAt(0).toUpperCase() + s.slice(1); }),
            datasets: datasets
        },
        options: {
            scales: {
                x: { stacked: true, grid: { display: false } },
                y: { stacked: true, beginAtZero: true, ticks: { precision: 0 } }
            },
            plugins: { legend: { position: 'bottom' } }
        }
    });
    Dashboard.trackChart(chart);
}

function createReasonTypeChart(reasonStats) {
    var canvas = document.getElementById('chart-reasons');
    if (!canvas) return;
    if (!reasonStats || Object.keys(reasonStats).length === 0) {
        canvas.parentElement.innerHTML = '<div class="empty-state" style="padding:40px 0"><p class="text-muted">No reason type data</p></div>';
        return;
    }

    var types = Object.keys(reasonStats);
    var counts = types.map(function(t) { return reasonStats[t].count || 0; });
    var successRates = types.map(function(t) { return reasonStats[t].success_rate || 0; });

    // Sort by count descending
    var pairs = types.map(function(t, i) { return { type: t, count: counts[i], rate: successRates[i] }; });
    pairs.sort(function(a, b) { return b.count - a.count; });

    var chart = new Chart(canvas, {
        type: 'bar',
        data: {
            labels: pairs.map(function(p) { return p.type.replace(/_/g, ' '); }),
            datasets: [{
                label: 'Usage Count',
                data: pairs.map(function(p) { return p.count; }),
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
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        afterLabel: function(ctx) {
                            var rate = pairs[ctx.dataIndex].rate;
                            return 'Success rate: ' + (rate * 100).toFixed(0) + '%';
                        }
                    }
                }
            }
        }
    });
    Dashboard.trackChart(chart);
}

function createBrierChart(brierHistory) {
    var canvas = document.getElementById('chart-brier');
    if (!canvas) return;
    if (!brierHistory || brierHistory.length === 0) {
        canvas.parentElement.innerHTML = '<div class="empty-state" style="padding:40px 0"><p class="text-muted">No Brier score history</p></div>';
        return;
    }

    var chart = new Chart(canvas, {
        type: 'line',
        data: {
            labels: brierHistory.map(function(d) { return d.date || d.created_at; }),
            datasets: [{
                label: 'Brier Score',
                data: brierHistory.map(function(d) { return d.brier_score || d.score; }),
                borderColor: '#7c6af7',
                backgroundColor: 'rgba(124, 106, 247, 0.1)',
                fill: true,
                pointBackgroundColor: '#7c6af7'
            }]
        },
        options: {
            scales: {
                x: {
                    ticks: {
                        maxTicksLimit: 10,
                        callback: function(val) {
                            var label = this.getLabelForValue(val);
                            if (!label) return '';
                            return String(label).slice(5, 10);  // MM-DD
                        }
                    },
                    grid: { display: false }
                },
                y: {
                    title: { display: true, text: 'Brier Score (lower is better)' },
                    min: 0,
                    max: 0.5
                }
            },
            plugins: { legend: { display: false } }
        }
    });
    Dashboard.trackChart(chart);
}

function createDailyDecisionsChart(dailyDecisions) {
    var canvas = document.getElementById('chart-daily');
    if (!canvas) return;
    if (!dailyDecisions || dailyDecisions.length === 0) {
        canvas.parentElement.innerHTML = '<div class="empty-state" style="padding:40px 0"><p class="text-muted">No daily decision data</p></div>';
        return;
    }

    var chart = new Chart(canvas, {
        type: 'bar',
        data: {
            labels: dailyDecisions.map(function(d) { return d.date; }),
            datasets: [{
                label: 'Decisions',
                data: dailyDecisions.map(function(d) { return d.count; }),
                backgroundColor: 'rgba(167, 139, 250, 0.5)',
                borderColor: '#a78bfa',
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
                            if (!label) return '';
                            return String(label).slice(5);  // MM-DD
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
