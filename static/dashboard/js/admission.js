/**
 * Nous Dashboard — Admission Control View (F021.1)
 *
 * Visualizes F023 A-MAC scoring: score distributions, per-dimension
 * breakdowns, rejected-fact review, and threshold simulation.
 */

/* global Dashboard, Chart, escapeHtml */

// Module-scoped chart reference for threshold simulator recoloring
var _admissionHistogramChart = null;

Dashboard.registerView('admission', async function (container) {
    Dashboard.showLoading(container);

    try {
        var data = await Dashboard.apiGet('/dashboard/admission');
        renderAdmission(container, data);
    } catch (err) {
        Dashboard.showError(container, 'Failed to load admission data.', function () {
            Dashboard.reloadView('admission');
        });
    }
});

function renderAdmission(container, data) {
    container.innerHTML = '<div class="view-header">' +
        '<h1>Admission Control</h1>' +
        '<p class="view-subtitle">F023 Memory Admission — score analysis and threshold tuning</p>' +
        '</div>' +
        '<div id="admission-content"></div>';

    var content = document.getElementById('admission-content');

    if (data.summary.total_scored === 0 && data.summary.bypassed === 0) {
        Dashboard.showEmpty(container, 'No admission data yet — facts will appear here as they are scored by F023. Ensure NOUS_ADMISSION_ENABLED=true.');
        return;
    }

    renderBanner(content, data);
    renderScoreDistribution(content, data);
    renderRejectedList(content, data);
    renderThresholdSimulator(content, data);
    renderDimensionBreakdown(content, data);
    renderBySource(content, data);
    renderByCategory(content, data);
    renderTrends(content, data);
    renderBypassBreakdown(content, data);
}

// ── Phase 1 MVP ──────────────────────────────────────────────────────

function renderBanner(el, data) {
    var cfg = data.config;
    var s = data.summary;
    var banner = document.createElement('div');
    banner.className = 'admission-banner ' + (cfg.shadow_mode ? 'shadow' : 'enforced');

    if (cfg.shadow_mode) {
        banner.innerHTML =
            '<div class="banner-indicator shadow-indicator"></div>' +
            '<div class="banner-text">' +
            '<strong>SHADOW MODE ACTIVE</strong> — All facts are being admitted. Scores are logged but not enforced.' +
            '<div class="banner-stats">' +
            'Threshold: ' + cfg.threshold + ' | ' +
            'Facts scored: ' + Dashboard.formatNumber(s.total_scored) + ' | ' +
            'Would reject: ' + Dashboard.formatNumber(s.would_reject) +
            ' (' + (s.rejection_rate * 100).toFixed(1) + '%)' +
            '</div></div>';
    } else {
        banner.innerHTML =
            '<div class="banner-indicator enforced-indicator"></div>' +
            '<div class="banner-text">' +
            '<strong>ENFORCEMENT ACTIVE</strong> — Facts below ' + cfg.threshold + ' are rejected.' +
            '<div class="banner-stats">' +
            'Admitted: ' + Dashboard.formatNumber(s.admitted) +
            ' (' + ((1 - s.rejection_rate) * 100).toFixed(0) + '%) | ' +
            'Rejected: ' + Dashboard.formatNumber(s.would_reject) +
            ' (' + (s.rejection_rate * 100).toFixed(1) + '%) | ' +
            'Bypassed: ' + Dashboard.formatNumber(s.bypassed) +
            '</div></div>';
    }
    el.appendChild(banner);
}

function renderScoreDistribution(el, data) {
    var section = document.createElement('div');
    section.className = 'chart-section';
    section.innerHTML =
        '<h3>Score Distribution</h3>' +
        '<p class="section-note">Counts based on current threshold (' + data.config.threshold + '). Scores were computed at admission time.</p>' +
        '<div class="chart-container" style="height:300px"><canvas id="admission-score-histogram"></canvas></div>';
    el.appendChild(section);

    if (data.score_distribution.length === 0) return;

    var threshold = data.config.threshold;
    var labels = data.score_distribution.map(function (d) { return d.bucket; });
    var counts = data.score_distribution.map(function (d) { return d.count; });
    var colors = data.score_distribution.map(function (d) {
        var start = parseFloat(d.bucket.split('-')[0]);
        return start >= threshold ? '#34d399' : '#f87171';
    });

    // Custom threshold line plugin (no annotation plugin dependency)
    var thresholdLinePlugin = {
        id: 'admissionThresholdLine',
        afterDraw: function (chart) {
            var xAxis = chart.scales.x;
            var yAxis = chart.scales.y;
            // Find the x position for the threshold bucket
            var bucketIndex = -1;
            for (var i = 0; i < data.score_distribution.length; i++) {
                var start = parseFloat(data.score_distribution[i].bucket.split('-')[0]);
                if (start >= threshold) { bucketIndex = i; break; }
            }
            if (bucketIndex < 0) return;
            var xPixel = xAxis.getPixelForValue(bucketIndex) - (xAxis.getPixelForValue(1) - xAxis.getPixelForValue(0)) / 2;
            var ctx = chart.ctx;
            ctx.save();
            ctx.strokeStyle = 'rgba(248, 113, 113, 0.8)';
            ctx.lineWidth = 2;
            ctx.setLineDash([6, 3]);
            ctx.beginPath();
            ctx.moveTo(xPixel, yAxis.top);
            ctx.lineTo(xPixel, yAxis.bottom);
            ctx.stroke();
            ctx.fillStyle = 'rgba(248, 113, 113, 0.8)';
            ctx.font = '10px -apple-system, sans-serif';
            ctx.fillText('Threshold: ' + threshold, xPixel + 4, yAxis.top + 12);
            ctx.restore();
        }
    };

    var ctx = document.getElementById('admission-score-histogram').getContext('2d');
    _admissionHistogramChart = Dashboard.trackChart(new Chart(ctx, {
        type: 'bar',
        data: {
            labels: labels,
            datasets: [{
                label: 'Facts',
                data: counts,
                backgroundColor: colors,
                borderRadius: 4,
            }]
        },
        plugins: [thresholdLinePlugin],
        options: {
            plugins: { legend: { display: false } },
            scales: {
                x: { title: { display: true, text: 'Composite Score' } },
                y: { title: { display: true, text: 'Fact Count' }, beginAtZero: true }
            }
        }
    }));

    // Store distribution data for threshold simulator
    el._scoreDistribution = data.score_distribution;
}

function renderRejectedList(el, data) {
    var section = document.createElement('div');
    section.className = 'table-section';
    section.innerHTML =
        '<h3>Would-Have-Been-Rejected Facts</h3>' +
        '<p class="section-note">Review this list to decide if the threshold is safe to enforce.</p>' +
        '<div id="rejected-table-container"></div>';
    el.appendChild(section);

    loadRejectedPage(0);
}

var REJECTED_PAGE_SIZE = 25;

function loadRejectedPage(offset) {
    var container = document.getElementById('rejected-table-container');
    if (!container) return;
    container.innerHTML = '<div class="skeleton skeleton-card"></div>';

    Dashboard.apiGet('/dashboard/admission/rejected?limit=' + REJECTED_PAGE_SIZE + '&offset=' + offset)
        .then(function (data) {
            if (data.facts.length === 0 && offset === 0) {
                container.innerHTML = '<div class="empty-inline">No facts below threshold in this time window.</div>';
                return;
            }

            var html = '<table class="data-table">' +
                '<thead><tr>' +
                '<th>Content</th><th>Source</th><th>Category</th>' +
                '<th>Score</th><th>Utility</th><th>Conf</th><th>Novelty</th><th>Recency</th><th>Type</th><th>Date</th>' +
                '</tr></thead><tbody>';

            data.facts.forEach(function (f, idx) {
                var scores = f.scores || {};
                html += '<tr class="expandable-row" data-idx="' + idx + '">' +
                    '<td class="content-cell">' + escapeHtml(f.content_preview) + '</td>' +
                    '<td>' + escapeHtml(f.source || '-') + '</td>' +
                    '<td><span class="badge badge-' + (f.category || 'unknown') + '">' + escapeHtml(f.category || '-') + '</span></td>' +
                    '<td class="score-cell">' + f.composite_score.toFixed(3) + '</td>' +
                    '<td>' + (scores.utility != null ? scores.utility.toFixed(2) : '-') + '</td>' +
                    '<td>' + (scores.confidence != null ? scores.confidence.toFixed(2) : '-') + '</td>' +
                    '<td>' + (scores.novelty != null ? scores.novelty.toFixed(2) : '-') + '</td>' +
                    '<td>' + (scores.recency != null ? scores.recency.toFixed(2) : '-') + '</td>' +
                    '<td>' + (scores.type_prior != null ? scores.type_prior.toFixed(2) : '-') + '</td>' +
                    '<td>' + Dashboard.formatDate(f.created_at) + '</td>' +
                    '</tr>';
            });
            html += '</tbody></table>';

            // Pagination
            html += '<div class="pagination">';
            if (offset > 0) {
                html += '<button class="btn btn-sm" id="rejected-prev">Previous</button>';
            }
            html += '<span class="page-info">Showing ' + (offset + 1) + '-' +
                Math.min(offset + data.facts.length, data.total) + ' of ' + data.total + '</span>';
            if (offset + data.facts.length < data.total) {
                html += '<button class="btn btn-sm" id="rejected-next">Next</button>';
            }
            html += '</div>';

            container.innerHTML = html;

            // Store full content for expand
            container._factData = data.facts;

            // Expand rows on click
            container.querySelectorAll('.expandable-row').forEach(function (row) {
                row.addEventListener('click', function () {
                    var idx = parseInt(row.dataset.idx);
                    if (row.classList.contains('expanded')) {
                        row.classList.remove('expanded');
                        var detail = row.nextElementSibling;
                        if (detail && detail.classList.contains('row-detail')) {
                            detail.remove();
                        }
                    } else {
                        row.classList.add('expanded');
                        var fullContent = container._factData[idx] ? container._factData[idx].content_full : '';
                        var detailRow = document.createElement('tr');
                        detailRow.className = 'row-detail expanded';
                        var td = document.createElement('td');
                        td.colSpan = 10;
                        var div = document.createElement('div');
                        div.className = 'detail-content';
                        div.textContent = fullContent;
                        td.appendChild(div);
                        detailRow.appendChild(td);
                        row.after(detailRow);
                    }
                });
            });

            // Pagination handlers
            var prevBtn = document.getElementById('rejected-prev');
            if (prevBtn) prevBtn.addEventListener('click', function () { loadRejectedPage(offset - REJECTED_PAGE_SIZE); });
            var nextBtn = document.getElementById('rejected-next');
            if (nextBtn) nextBtn.addEventListener('click', function () { loadRejectedPage(offset + REJECTED_PAGE_SIZE); });
        })
        .catch(function () {
            container.innerHTML = '<div class="error-inline">Failed to load rejected facts.</div>';
        });
}

function renderThresholdSimulator(el, data) {
    var section = document.createElement('div');
    section.className = 'chart-section simulator-section';
    section.innerHTML =
        '<h3>Threshold Simulator</h3>' +
        '<p class="section-note">Slide to explore different thresholds. Read-only — does not change actual config.</p>' +
        '<div class="simulator-controls">' +
        '<input type="range" id="threshold-slider" min="0" max="1" step="0.05" value="' + data.config.threshold + '" aria-label="Threshold simulator">' +
        '<span id="threshold-display" class="threshold-value">' + data.config.threshold + '</span>' +
        '</div>' +
        '<div id="simulator-result" class="simulator-result"></div>';
    el.appendChild(section);

    var slider = document.getElementById('threshold-slider');
    var display = document.getElementById('threshold-display');
    var resultEl = document.getElementById('simulator-result');
    var dist = data.score_distribution;

    function updateSimulation(threshold) {
        display.textContent = threshold.toFixed(2);
        var admitted = 0;
        var rejected = 0;
        dist.forEach(function (d) {
            var start = parseFloat(d.bucket.split('-')[0]);
            if (start >= threshold) {
                admitted += d.count;
            } else {
                rejected += d.count;
            }
        });
        var total = admitted + rejected;
        var pct = total > 0 ? ((rejected / total) * 100).toFixed(1) : '0.0';
        resultEl.innerHTML =
            'At threshold <strong>' + threshold.toFixed(2) + '</strong>: ' +
            '<span class="sim-admitted">' + admitted + ' admitted</span>, ' +
            '<span class="sim-rejected">' + rejected + ' rejected</span> ' +
            '(' + pct + '% rejection rate)';

        // Recolor histogram bars via stored chart reference
        if (_admissionHistogramChart) {
            _admissionHistogramChart.data.datasets[0].backgroundColor = dist.map(function (d) {
                return parseFloat(d.bucket.split('-')[0]) >= threshold ? '#34d399' : '#f87171';
            });
            _admissionHistogramChart.update('none');
        }
    }

    slider.addEventListener('input', function () {
        updateSimulation(parseFloat(slider.value));
    });
    updateSimulation(data.config.threshold);
}

// ── Phase 2 Analytics ────────────────────────────────────────────────

function renderDimensionBreakdown(el, data) {
    if (!data.dimension_stats || Object.keys(data.dimension_stats).length <= 1) return;

    var section = document.createElement('div');
    section.className = 'chart-section';
    section.innerHTML =
        '<h3>Per-Dimension Breakdown</h3>' +
        '<p class="section-note">Excludes bypassed facts. Shows score spread for admitted vs rejected.</p>';

    var dims = ['utility', 'confidence', 'novelty', 'recency', 'type_prior'];
    var grid = document.createElement('div');
    grid.className = 'dimension-grid';

    dims.forEach(function (dim) {
        var d = data.dimension_stats[dim];
        if (!d) return;
        var card = document.createElement('div');
        card.className = 'dimension-card';
        card.innerHTML = '<h4>' + dim.replace('_', ' ') + '</h4>' +
            renderBoxPlotHtml('Admitted', d.admitted, '#34d399') +
            renderBoxPlotHtml('Rejected', d.rejected, '#f87171');
        grid.appendChild(card);
    });

    section.appendChild(grid);
    el.appendChild(section);
}

function renderBoxPlotHtml(label, stats, color) {
    if (!stats || (!stats.min && stats.min !== 0)) {
        return '<div class="box-plot-row"><span class="bp-label">' + label + '</span><span class="bp-empty">No data</span></div>';
    }
    var maxVal = Math.max(1.0, stats.max);
    var pct = function (v) { return (Math.min(v / maxVal, 1.0) * 100).toFixed(1) + '%'; };
    return '<div class="box-plot-row">' +
        '<span class="bp-label">' + label + '</span>' +
        '<div class="bp-track">' +
        '<div class="bp-whisker" style="left:' + pct(stats.min) + ';width:' + pct(stats.max - stats.min) + ';background:' + color + '30"></div>' +
        '<div class="bp-box" style="left:' + pct(stats.q1) + ';width:' + pct(stats.q3 - stats.q1) + ';background:' + color + '80"></div>' +
        '<div class="bp-median" style="left:' + pct(stats.median) + ';background:' + color + '"></div>' +
        '</div>' +
        '<span class="bp-values">' + stats.min.toFixed(2) + ' / ' + stats.median.toFixed(2) + ' / ' + stats.max.toFixed(2) + '</span>' +
        '</div>';
}

function renderBySource(el, data) {
    if (!data.by_source || Object.keys(data.by_source).length === 0) return;

    var section = document.createElement('div');
    section.className = 'chart-section';
    section.innerHTML =
        '<h3>Admission by Source</h3>' +
        '<div class="chart-container" style="height:300px"><canvas id="admission-by-source-chart"></canvas></div>';
    el.appendChild(section);

    var sources = Object.keys(data.by_source);
    var admitted = sources.map(function (s) { return data.by_source[s].admitted; });
    var rejected = sources.map(function (s) { return data.by_source[s].rejected; });
    var bypassed = sources.map(function (s) { return data.by_source[s].bypassed || 0; });

    var ctx = document.getElementById('admission-by-source-chart').getContext('2d');
    Dashboard.trackChart(new Chart(ctx, {
        type: 'bar',
        data: {
            labels: sources,
            datasets: [
                { label: 'Admitted', data: admitted, backgroundColor: '#34d399' },
                { label: 'Rejected', data: rejected, backgroundColor: '#f87171' },
                { label: 'Bypassed', data: bypassed, backgroundColor: '#6b6b8a' },
            ]
        },
        options: {
            plugins: { legend: { display: true } },
            scales: {
                x: { stacked: true },
                y: { stacked: true, beginAtZero: true, title: { display: true, text: 'Count' } }
            }
        }
    }));
}

function renderByCategory(el, data) {
    if (!data.by_category || Object.keys(data.by_category).length === 0) return;

    var section = document.createElement('div');
    section.className = 'chart-section';
    section.innerHTML =
        '<h3>Admission by Category</h3>' +
        '<div class="chart-container" style="height:300px"><canvas id="admission-by-category-chart"></canvas></div>';
    el.appendChild(section);

    var categories = Object.keys(data.by_category);
    var admitted = categories.map(function (c) { return data.by_category[c].admitted; });
    var rejected = categories.map(function (c) { return data.by_category[c].rejected; });

    var ctx = document.getElementById('admission-by-category-chart').getContext('2d');
    Dashboard.trackChart(new Chart(ctx, {
        type: 'bar',
        data: {
            labels: categories,
            datasets: [
                { label: 'Admitted', data: admitted, backgroundColor: '#34d399' },
                { label: 'Rejected', data: rejected, backgroundColor: '#f87171' },
            ]
        },
        options: {
            plugins: { legend: { display: true } },
            scales: {
                x: { stacked: true },
                y: { stacked: true, beginAtZero: true, title: { display: true, text: 'Count' } }
            }
        }
    }));
}

function renderTrends(el, data) {
    if (!data.daily_trend || data.daily_trend.length === 0) return;

    var section = document.createElement('div');
    section.className = 'chart-section';
    section.innerHTML =
        '<h3>Trends Over Time</h3>' +
        '<div class="chart-grid">' +
        '<div class="chart-container" style="height:250px"><canvas id="admission-trend-rate-chart"></canvas></div>' +
        '<div class="chart-container" style="height:250px"><canvas id="admission-trend-score-chart"></canvas></div>' +
        '</div>';
    el.appendChild(section);

    var labels = data.daily_trend.map(function (d) { return d.date; });
    var admitRate = data.daily_trend.map(function (d) {
        return d.scored > 0 ? ((d.admitted / d.scored) * 100) : null;
    });
    var avgScore = data.daily_trend.map(function (d) { return d.avg_score; });

    var ctx1 = document.getElementById('admission-trend-rate-chart').getContext('2d');
    Dashboard.trackChart(new Chart(ctx1, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [{
                label: 'Admission Rate (%)',
                data: admitRate,
                borderColor: '#34d399',
                backgroundColor: 'rgba(52,211,153,0.1)',
                fill: true,
                spanGaps: true,
            }]
        },
        options: {
            plugins: { legend: { display: false } },
            scales: {
                x: { title: { display: true, text: 'Date' } },
                y: { title: { display: true, text: 'Admission Rate (%)' }, min: 0, max: 100 }
            }
        }
    }));

    var ctx2 = document.getElementById('admission-trend-score-chart').getContext('2d');
    Dashboard.trackChart(new Chart(ctx2, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [{
                label: 'Avg Composite Score',
                data: avgScore,
                borderColor: '#7c6af7',
                backgroundColor: 'rgba(124,106,247,0.1)',
                fill: true,
                spanGaps: true,
            }]
        },
        options: {
            plugins: { legend: { display: false } },
            scales: {
                x: { title: { display: true, text: 'Date' } },
                y: { title: { display: true, text: 'Average Score' }, min: 0, max: 1 }
            }
        }
    }));
}

function renderBypassBreakdown(el, data) {
    if (!data.bypass_breakdown || Object.keys(data.bypass_breakdown).length === 0) return;

    var section = document.createElement('div');
    section.className = 'chart-section';
    section.innerHTML =
        '<h3>Bypass Breakdown</h3>' +
        '<div class="chart-container" style="height:250px;max-width:400px"><canvas id="admission-bypass-chart"></canvas></div>';
    el.appendChild(section);

    var reasons = Object.keys(data.bypass_breakdown);
    var counts = reasons.map(function (r) { return data.bypass_breakdown[r]; });
    var colors = ['#7c6af7', '#60a5fa', '#34d399', '#fb923c', '#f87171', '#6b6b8a'];

    var ctx = document.getElementById('admission-bypass-chart').getContext('2d');
    Dashboard.trackChart(new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels: reasons,
            datasets: [{
                data: counts,
                backgroundColor: colors.slice(0, reasons.length),
                borderColor: '#111118',
                borderWidth: 2,
            }]
        },
        options: {
            plugins: {
                legend: { position: Dashboard.legendPosition() }
            }
        }
    }));
}
