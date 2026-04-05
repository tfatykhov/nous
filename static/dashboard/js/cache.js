/**
 * Nous Dashboard — Cache View (F036.1)
 *
 * API cache hit rates, token savings, break analysis,
 * efficiency timeline, and per-session/call tables.
 * Auto-refreshes every 30 seconds.
 * Fetches from GET /dashboard/cache
 */

/* global Dashboard, Chart, escapeHtml */

var _cacheRefreshInterval = null;
var _cacheRefreshInFlight = false;
var _cacheAbortController = null;

Dashboard.registerView('cache', async function (container) {
    Dashboard.showLoading(container);

    try {
        var data = await cacheFetch();
        renderCache(container, data);
        startCacheAutoRefresh(container);
    } catch (err) {
        Dashboard.showError(container, 'Failed to load cache data.', function () {
            Dashboard.reloadView('cache');
        });
    }
});

function cacheFetch() {
    if (_cacheAbortController) {
        _cacheAbortController.abort();
    }
    _cacheAbortController = new AbortController();
    return fetch('/dashboard/cache', { signal: _cacheAbortController.signal })
        .then(function (res) {
            if (!res.ok) throw new Error(res.status + ' ' + res.statusText);
            return res.json();
        })
        .finally(function () {
            _cacheAbortController = null;
        });
}

function startCacheAutoRefresh(container) {
    stopCacheAutoRefresh();
    _cacheRefreshInterval = setInterval(async function () {
        if (Dashboard.currentView !== 'cache') {
            stopCacheAutoRefresh();
            return;
        }
        if (_cacheRefreshInFlight) return;
        _cacheRefreshInFlight = true;
        try {
            var data = await cacheFetch();
            renderCache(container, data);
        } catch (err) {
            // Silently skip (including aborted requests)
        } finally {
            _cacheRefreshInFlight = false;
        }
    }, 30000);
}

function stopCacheAutoRefresh() {
    if (_cacheRefreshInterval) {
        clearInterval(_cacheRefreshInterval);
        _cacheRefreshInterval = null;
    }
    if (_cacheAbortController) {
        _cacheAbortController.abort();
        _cacheAbortController = null;
    }
}

// ── Helpers ─────────────────────────────────────────────────────────

function formatTokens(n) {
    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
    return String(n);
}

function hitRateColor(rate) {
    if (rate >= 50) return '#22c55e';
    if (rate >= 20) return '#eab308';
    return '#ef4444';
}

function formatTime(ts) {
    var d = new Date(ts);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function formatTimeSeconds(ts) {
    var d = new Date(ts);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

// ── Main render ─────────────────────────────────────────────────────

function renderCache(container, data) {
    // Destroy existing charts before re-render
    if (Dashboard.charts['cache']) {
        Dashboard.charts['cache'].forEach(function (c) {
            try { c.destroy(); } catch (e) { /* ignore */ }
        });
        Dashboard.charts['cache'] = [];
    }

    var summary = data.summary || {};
    var sessions = data.sessions || [];
    var timeline = data.timeline || [];
    // F036.1: API returns break_components as {name: count} dict — convert to array
    var rawBreakComponents = data.break_components || {};
    var breakComponents = Object.keys(rawBreakComponents).map(function (k) {
        return { name: k, count: rawBreakComponents[k] };
    });

    var html = '<div class="view-header">' +
        '<h1>Cache</h1>' +
        '<p class="view-subtitle">API cache performance, token savings, and break analysis</p>' +
        '</div>';

    // ── Row 1: Stat Cards ──
    var hitRateVal = summary.overall_hit_rate != null ? summary.overall_hit_rate : 0;
    var breakRateVal = summary.break_rate != null ? summary.break_rate : 0;

    html += '<div class="stat-grid">';
    html += buildCacheStatCard('Total API Calls', Dashboard.formatNumber(summary.total_calls || 0), '', 'var(--accent)');
    html += buildCacheStatCard('Cache Hit Rate', hitRateVal + '%', '', hitRateColor(hitRateVal));
    html += buildCacheStatCard('Tokens Saved', formatTokens(summary.total_cache_read || 0), 'cache_read tokens', '#22c55e');
    html += buildCacheStatCard('Cache Breaks', Dashboard.formatNumber(summary.total_breaks || 0), breakRateVal + '% break rate', '#ef4444');
    html += '</div>';

    // ── Row 2: Two charts side by side ──
    html += '<div class="chart-grid">';
    html += '<div class="chart-card"><h3>Token Breakdown</h3><div class="chart-container"><canvas id="chart-cache-tokens"></canvas></div></div>';
    html += '<div class="chart-card"><h3>Break Components</h3><div class="chart-container"><canvas id="chart-cache-breaks"></canvas></div></div>';
    html += '</div>';

    // ── Row 3: Efficiency Timeline ──
    html += '<div class="chart-card mb-24"><h3>Efficiency Timeline</h3><div class="chart-container"><canvas id="chart-cache-timeline"></canvas></div></div>';

    // ── Row 4: Session Table ──
    html += '<div class="chart-card mb-24"><h3>Sessions</h3>';
    if (sessions.length > 0) {
        html += '<table style="width:100%;font-size:13px;border-collapse:collapse">';
        html += '<thead><tr style="text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:var(--muted)">';
        html += '<th style="padding:8px 12px">Session</th>';
        html += '<th style="padding:8px 12px">Calls</th>';
        html += '<th style="padding:8px 12px">Input Tokens</th>';
        html += '<th style="padding:8px 12px">Cache Read</th>';
        html += '<th style="padding:8px 12px">Hit Rate</th>';
        html += '<th style="padding:8px 12px">Breaks</th>';
        html += '</tr></thead><tbody>';
        sessions.forEach(function (s) {
            var sHitRate = s.hit_rate != null ? s.hit_rate : 0;
            html += '<tr style="border-top:1px solid var(--border)">';
            html += '<td style="padding:8px 12px"><code style="font-size:11px">' + escapeHtml(String(s.session_id || '').slice(0, 8)) + '</code></td>';
            html += '<td style="padding:8px 12px">' + Dashboard.formatNumber(s.calls || 0) + '</td>';
            html += '<td style="padding:8px 12px">' + formatTokens(s.input_tokens || 0) + '</td>';
            html += '<td style="padding:8px 12px">' + formatTokens(s.cache_read || 0) + '</td>';
            html += '<td style="padding:8px 12px;color:' + hitRateColor(sHitRate) + '">' + sHitRate + '%</td>';
            html += '<td style="padding:8px 12px">' + Dashboard.formatNumber(s.breaks || 0) + '</td>';
            html += '</tr>';
        });
        html += '</tbody></table>';
    } else {
        html += '<div class="empty-state" style="padding:24px"><p>No session data available</p></div>';
    }
    html += '</div>';

    // ── Row 5: Recent Calls Table ──
    var recentCalls = timeline.slice(0, 20);
    html += '<div class="chart-card mb-24"><h3>Recent Calls</h3>';
    if (recentCalls.length > 0) {
        html += '<table style="width:100%;font-size:13px;border-collapse:collapse">';
        html += '<thead><tr style="text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:var(--muted)">';
        html += '<th style="padding:8px 12px">Time</th>';
        html += '<th style="padding:8px 12px">Session</th>';
        html += '<th style="padding:8px 12px">Turn</th>';
        html += '<th style="padding:8px 12px">Model</th>';
        html += '<th style="padding:8px 12px">Input</th>';
        html += '<th style="padding:8px 12px">Cache Read</th>';
        html += '<th style="padding:8px 12px">Hit Rate</th>';
        html += '<th style="padding:8px 12px">Break</th>';
        html += '</tr></thead><tbody>';
        recentCalls.forEach(function (c) {
            var cHitRate = c.hit_rate != null ? c.hit_rate : 0;
            html += '<tr style="border-top:1px solid var(--border)">';
            html += '<td style="padding:8px 12px;white-space:nowrap">' + formatTimeSeconds(c.timestamp) + '</td>';
            html += '<td style="padding:8px 12px"><code style="font-size:11px">' + escapeHtml(String(c.session_id || '').slice(0, 8)) + '</code></td>';
            html += '<td style="padding:8px 12px">' + (c.turn != null ? c.turn : '-') + '</td>';
            html += '<td style="padding:8px 12px;font-size:11px">' + escapeHtml(c.model || '') + '</td>';
            html += '<td style="padding:8px 12px">' + formatTokens(c.input_tokens || 0) + '</td>';
            html += '<td style="padding:8px 12px">' + formatTokens(c.cache_read || 0) + '</td>';
            html += '<td style="padding:8px 12px;color:' + hitRateColor(cHitRate) + '">' + cHitRate + '%</td>';
            html += '<td style="padding:8px 12px">' + (c.cache_break ? '<span style="color:#ef4444">&#x25CF;</span>' : '') + '</td>';
            html += '</tr>';
        });
        html += '</tbody></table>';
    } else {
        html += '<div class="empty-state" style="padding:24px"><p>No recent API calls</p></div>';
    }
    html += '</div>';

    container.innerHTML = html;

    // ── Create Charts ──
    createCacheTokenChart(summary);
    createCacheBreakChart(breakComponents);
    createCacheTimelineChart(timeline);
}

// ── Stat card helper ────────────────────────────────────────────────

function buildCacheStatCard(label, value, period, color) {
    return '<div class="stat-card">' +
        '<div class="stat-value" style="color:' + color + '">' + escapeHtml(String(value)) + '</div>' +
        '<div class="stat-label">' + escapeHtml(label) + '</div>' +
        (period ? '<div style="font-size:11px;color:var(--muted);margin-top:2px">' + escapeHtml(period) + '</div>' : '') +
        '</div>';
}

// ── Token Breakdown Doughnut ────────────────────────────────────────

function createCacheTokenChart(summary) {
    var canvas = document.getElementById('chart-cache-tokens');
    if (!canvas) return;

    var cacheRead = summary.total_cache_read || 0;
    var cacheCreated = summary.total_cache_created || 0;
    var totalInput = summary.total_input_tokens || 0;
    var uncached = Math.max(0, totalInput - cacheRead - cacheCreated);

    var chart = new Chart(canvas, {
        type: 'doughnut',
        data: {
            labels: ['Cache Read', 'Cache Created', 'Uncached'],
            datasets: [{
                data: [cacheRead, cacheCreated, uncached],
                backgroundColor: ['#22c55e', '#3b82f6', '#4b5563'],
                borderWidth: 0
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            cutout: '60%',
            plugins: {
                legend: { position: 'bottom' }
            }
        }
    });
    Dashboard.charts.cache = Dashboard.charts.cache || [];
    Dashboard.charts.cache.push(chart);
}

// ── Break Components Horizontal Bar ─────────────────────────────────

function createCacheBreakChart(breakComponents) {
    var canvas = document.getElementById('chart-cache-breaks');
    if (!canvas || !breakComponents || breakComponents.length === 0) return;

    var palette = ['#f87171', '#fbbf24', '#60a5fa', '#34d399', '#a78bfa', '#fb923c', '#22d3ee', '#e879f9'];

    var labels = breakComponents.map(function (c) { return c.component || c.name || 'unknown'; });
    var values = breakComponents.map(function (c) { return c.count || 0; });
    var colors = breakComponents.map(function (_, i) { return palette[i % palette.length]; });

    var chart = new Chart(canvas, {
        type: 'bar',
        data: {
            labels: labels,
            datasets: [{
                label: 'Breaks',
                data: values,
                backgroundColor: colors,
                borderWidth: 0
            }]
        },
        options: {
            indexAxis: 'y',
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                x: {
                    beginAtZero: true,
                    ticks: { precision: 0 },
                    grid: { display: false }
                },
                y: {
                    grid: { display: false }
                }
            },
            plugins: {
                legend: { display: false }
            }
        }
    });
    Dashboard.charts.cache = Dashboard.charts.cache || [];
    Dashboard.charts.cache.push(chart);
}

// ── Efficiency Timeline Line Chart ──────────────────────────────────

function createCacheTimelineChart(timeline) {
    var canvas = document.getElementById('chart-cache-timeline');
    if (!canvas || !timeline || timeline.length === 0) return;

    // Reverse so oldest is on the left
    var sorted = timeline.slice().reverse();

    var labels = sorted.map(function (t) { return formatTime(t.timestamp); });
    var hitRates = sorted.map(function (t) { return t.hit_rate != null ? t.hit_rate : 0; });

    var chart = new Chart(canvas, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [{
                label: 'Hit Rate %',
                data: hitRates,
                borderColor: '#22c55e',
                backgroundColor: 'rgba(34, 197, 94, 0.1)',
                fill: true,
                borderWidth: 2,
                pointRadius: 2,
                pointHoverRadius: 5
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                x: {
                    grid: { display: false }
                },
                y: {
                    beginAtZero: true,
                    max: 100,
                    ticks: {
                        callback: function (val) { return val + '%'; }
                    }
                }
            },
            plugins: {
                legend: { display: false }
            }
        }
    });
    Dashboard.charts.cache = Dashboard.charts.cache || [];
    Dashboard.charts.cache.push(chart);
}
