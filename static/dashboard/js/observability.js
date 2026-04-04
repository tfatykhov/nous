/**
 * Nous Dashboard — Observability View (F035)
 *
 * Event bus health, causal traces, behavioral drift trends,
 * and context visibility. Auto-refreshes every 30 seconds.
 * Fetches from GET /dashboard/observability
 */

/* global Dashboard, Chart, escapeHtml */

var _obsRefreshInterval = null;
var _obsRefreshInFlight = false;
var _obsAbortController = null;

Dashboard.registerView('observability', async function (container) {
    Dashboard.showLoading(container);

    try {
        var data = await obsFetch();
        renderObservability(container, data);
        startObsAutoRefresh(container);
    } catch (err) {
        Dashboard.showError(container, 'Failed to load observability data.', function () {
            Dashboard.reloadView('observability');
        });
    }
});

function obsFetch() {
    if (_obsAbortController) {
        _obsAbortController.abort();
    }
    _obsAbortController = new AbortController();
    return fetch('/dashboard/observability', { signal: _obsAbortController.signal })
        .then(function (res) {
            if (!res.ok) throw new Error(res.status + ' ' + res.statusText);
            return res.json();
        })
        .finally(function () {
            _obsAbortController = null;
        });
}

function startObsAutoRefresh(container) {
    stopObsAutoRefresh();
    _obsRefreshInterval = setInterval(async function () {
        if (Dashboard.currentView !== 'observability') {
            stopObsAutoRefresh();
            return;
        }
        if (_obsRefreshInFlight) return;
        _obsRefreshInFlight = true;
        try {
            var data = await obsFetch();
            renderObservability(container, data);
        } catch (err) {
            // Silently skip
        } finally {
            _obsRefreshInFlight = false;
        }
    }, 30000);
}

function stopObsAutoRefresh() {
    if (_obsRefreshInterval) {
        clearInterval(_obsRefreshInterval);
        _obsRefreshInterval = null;
    }
    if (_obsAbortController) {
        _obsAbortController.abort();
        _obsAbortController = null;
    }
}

// ── Helpers ─────────────────────────────────────────────────────────

function obsHumanizeAgo(isoString) {
    if (!isoString) return '--';
    var now = new Date();
    var then = new Date(isoString);
    var diffSecs = Math.floor((now - then) / 1000);
    if (diffSecs < 30) return 'just now';
    var diffMins = Math.floor(diffSecs / 60);
    if (diffMins < 1) return diffSecs + 's ago';
    if (diffMins < 60) return diffMins + 'm ago';
    var diffHours = Math.floor(diffMins / 60);
    if (diffHours < 24) return diffHours + 'h ago';
    return Math.floor(diffHours / 24) + 'd ago';
}

function obsFormatNumber(n) {
    if (n == null) return '--';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
    return String(n);
}

function obsShortHandlerName(fullName) {
    var parts = fullName.split('.');
    return parts.length >= 2 ? parts[parts.length - 2] : fullName;
}

function obsBuildStatCard(label, value, sub, color) {
    return '<div class="stat-card">' +
        '<div class="stat-value" style="color:' + (color || 'var(--text)') + '">' + escapeHtml(String(value)) + '</div>' +
        '<div class="stat-label">' + escapeHtml(label) + '</div>' +
        (sub ? '<div class="stat-sub" style="font-size:11px;color:var(--muted);margin-top:2px">' + escapeHtml(sub) + '</div>' : '') +
        '</div>';
}

// ── Main render ─────────────────────────────────────────────────────

function renderObservability(container, data) {
    // Destroy existing charts
    if (Dashboard.charts['observability']) {
        Dashboard.charts['observability'].forEach(function (c) {
            try { c.destroy(); } catch (e) { /* ignore */ }
        });
        Dashboard.charts['observability'] = [];
    }

    var eb = data.event_bus || {};
    var traces = data.recent_traces || [];
    var mods = data.recent_modifications || [];
    var drift = data.drift;
    var driftTrends = data.drift_trends || {};
    var ctxLog = data.context_log || [];

    var html = '<div class="view-header">' +
        '<h1>Observability</h1>' +
        '<p class="view-subtitle">Event bus health, causal traces, behavioral drift, and context visibility</p>' +
        '</div>';

    // ════════════════════════════════════════════════════════════════
    // Section 1: Event Bus Health
    // ════════════════════════════════════════════════════════════════
    var handlers = eb.handlers || {};
    var handlerNames = Object.keys(handlers);
    var totalErrors = 0;
    var totalInvocations = 0;
    handlerNames.forEach(function (n) {
        totalErrors += handlers[n].errors || 0;
        totalInvocations += handlers[n].invocations || 0;
    });
    var overallErrorRate = totalInvocations > 0 ? totalErrors / totalInvocations : 0;

    var uptimeSecs = eb.uptime_seconds || 0;
    var uptimeHours = Math.floor(uptimeSecs / 3600);
    var uptimeMins = Math.floor((uptimeSecs % 3600) / 60);
    var uptimeStr = uptimeHours > 0 ? uptimeHours + 'h ' + uptimeMins + 'm' : uptimeMins + 'm';

    html += '<div class="obs-section">';
    html += '<div class="stat-grid">';
    html += obsBuildStatCard('Events Processed', obsFormatNumber(eb.total_processed || 0), 'since start', '#38bdf8');
    html += obsBuildStatCard('Queue Depth', String(eb.queue_depth || 0), '', eb.queue_depth > 10 ? 'var(--yellow)' : 'var(--green)');
    html += obsBuildStatCard('Handlers', handlerNames.length + ' active', (overallErrorRate * 100).toFixed(1) + '% error rate', overallErrorRate > 0.1 ? 'var(--red)' : 'var(--green)');
    html += obsBuildStatCard('Dropped', String(eb.total_dropped || 0), '', eb.total_dropped > 0 ? 'var(--red)' : 'var(--muted)');
    html += obsBuildStatCard('Uptime', uptimeStr, '', 'var(--muted)');
    html += '</div>'; // stat-grid

    // Handler health table
    html += '<div class="chart-card mb-24"><h3>Handler Health</h3>';
    if (handlerNames.length > 0) {
        handlerNames.forEach(function (name) {
            var h = handlers[name];
            var errorRate = h.error_rate || 0;
            var dotCls = 'obs-handler-dot';
            if (errorRate > 0.1) dotCls += ' bad';
            else if (errorRate > 0) dotCls += ' warn';
            else dotCls += ' good';

            html += '<div class="obs-handler-row">';
            html += '<div class="' + dotCls + '"></div>';
            html += '<div style="flex:1;color:var(--text)">' + escapeHtml(obsShortHandlerName(name)) + '</div>';
            html += '<div style="min-width:60px;text-align:right;color:var(--muted)">' + (h.successes || 0) + '/' + (h.invocations || 0) + '</div>';
            html += '<div style="min-width:70px;text-align:right;color:var(--muted)">' + Number(h.avg_duration_ms || 0).toFixed(1) + 'ms</div>';
            if (h.last_invoked_ago_s != null) {
                html += '<div style="min-width:60px;text-align:right;color:var(--muted)">' + Math.round(h.last_invoked_ago_s) + 's ago</div>';
            }
            html += '</div>';
        });
    } else {
        html += '<p class="text-muted" style="font-size:12px;padding:12px 0">No handlers registered</p>';
    }
    html += '</div>'; // chart-card
    html += '</div>'; // obs-section

    // ════════════════════════════════════════════════════════════════
    // Section 2: Causal Traces + Modifications
    // ════════════════════════════════════════════════════════════════
    html += '<div class="obs-section">';
    html += '<div class="chart-grid">';

    // Recent traces
    html += '<div class="chart-card"><h3>Recent Causal Traces</h3>';
    if (traces.length > 0) {
        traces.forEach(function (t) {
            html += '<div class="obs-trace-row">';
            html += '<div class="obs-trace-type">' + escapeHtml(t.root_type || '?') + '</div>';
            html += '<span class="obs-trace-badge events">' + (t.event_count || 0) + ' events</span>';
            if (t.has_modifications) {
                html += ' <span class="obs-trace-badge mod">MOD</span>';
            }
            html += '<div class="obs-trace-meta" style="margin-left:auto">' + obsHumanizeAgo(t.timestamp) + '</div>';
            html += '</div>';
        });
    } else {
        html += '<div class="empty-state" style="padding:20px"><p>No traces recorded yet</p></div>';
    }
    html += '</div>'; // chart-card

    // Recent modifications
    html += '<div class="chart-card"><h3>Autonomous Modifications (24h)</h3>';
    if (mods.length > 0) {
        mods.forEach(function (m) {
            html += '<div class="obs-mod-row">';
            html += '<div class="obs-mod-type">' + escapeHtml(m.modifies || '?') + '</div>';
            html += '<div style="flex:1;color:var(--text)">' + escapeHtml(m.type || '') + '</div>';
            html += '<div>' + obsHumanizeAgo(m.timestamp) + '</div>';
            html += '</div>';
        });
    } else {
        html += '<div class="empty-state" style="padding:20px"><p>No autonomous modifications in the last 24h</p></div>';
    }
    html += '</div>'; // chart-card

    html += '</div>'; // chart-grid
    html += '</div>'; // obs-section

    // ════════════════════════════════════════════════════════════════
    // Section 3: Behavioral Drift
    // ════════════════════════════════════════════════════════════════
    html += '<div class="obs-section">';

    // Anomalies
    var anomalies = drift ? (drift.anomalies || []) : [];
    if (anomalies.length > 0) {
        html += '<div class="chart-card mb-24"><h3>Active Drift Anomalies</h3>';
        anomalies.forEach(function (a) {
            var sev = ['warning','alert'].includes(a.severity) ? a.severity : 'warning';
            var cls = 'obs-anomaly ' + sev;
            html += '<div class="' + cls + '">';
            html += '<strong>' + escapeHtml(a.metric || '?') + '</strong>: ';
            html += escapeHtml(String(a.current)) + ' (' + escapeHtml(a.direction || '') + ' from ' + escapeHtml(String(a.mean)) + ' &plusmn; ' + escapeHtml(String(a.stddev)) + ')';
            html += '</div>';
        });
        html += '</div>';
    }

    // Drift trend charts — only render if we have trend data
    var factTrendData = (driftTrends.fact_count_delta || []);
    var errorTrendData = (driftTrends.handler_error_rate || []);
    var hasTrendData = factTrendData.length > 0 || errorTrendData.length > 0;

    if (hasTrendData) {
        html += '<div class="chart-grid">';
        html += '<div class="chart-card"><h3>Fact Growth Rate (7d)</h3><div class="chart-container"><canvas id="chart-obs-facts"></canvas></div></div>';
        html += '<div class="chart-card"><h3>Handler Error Rate (7d)</h3><div class="chart-container"><canvas id="chart-obs-errors"></canvas></div></div>';
        html += '</div>';
    } else {
        html += '<div class="empty-state" style="padding:20px"><p>No drift trend data available yet</p></div>';
    }

    // Latest snapshot summary
    if (drift) {
        var m = drift.metrics || {};
        html += '<div class="chart-card mb-24"><h3>Latest Snapshot</h3>';
        html += '<div class="stat-grid">';
        html += obsBuildStatCard('Facts', String(m.fact_count || 0), (m.fact_count_delta > 0 ? '+' : '') + (m.fact_count_delta || 0) + ' delta', 'var(--fact-color)');
        html += obsBuildStatCard('Episodes', String(m.episode_count || 0), '', 'var(--episode-color)');
        html += obsBuildStatCard('Censors', String(m.active_censor_count || 0), '', 'var(--censor-color)');
        html += obsBuildStatCard('Procedures', String(m.procedure_count || 0), '', 'var(--procedure-color)');
        html += obsBuildStatCard('Error Rate', ((m.handler_error_rate || 0) * 100).toFixed(1) + '%', '', m.handler_error_rate > 0.1 ? 'var(--red)' : 'var(--green)');
        html += '</div>';
        html += '<p class="text-muted" style="font-size:11px;margin-top:8px">Snapshot: ' + obsHumanizeAgo(drift.timestamp) + '</p>';
        html += '</div>';
    }
    html += '</div>'; // obs-section

    // ════════════════════════════════════════════════════════════════
    // Section 4: Context Visibility
    // ════════════════════════════════════════════════════════════════
    html += '<div class="obs-section">';
    html += '<div class="chart-grid">';

    // Token breakdown chart (last call)
    html += '<div class="chart-card"><h3>Context Token Breakdown (Last Call)</h3><div class="chart-container"><canvas id="chart-obs-tokens"></canvas></div></div>';

    // Recent API calls table (clickable to expand)
    html += '<div class="chart-card"><h3>Recent API Calls</h3>';
    if (ctxLog.length > 0) {
        ctxLog.forEach(function (entry, idx) {
            var total = entry.total_tokens_est || 0;
            var actual = entry.input_tokens_actual;
            var tokenStr = '~' + obsFormatNumber(total);
            if (actual) tokenStr += ' / ' + obsFormatNumber(actual) + ' actual';

            html += '<div class="obs-ctx-row" style="cursor:pointer" data-obs-ctx-idx="' + idx + '">';
            html += '<div style="min-width:50px;font-weight:600;color:#38bdf8">T' + escapeHtml(String(entry.turn_number || '?')) + '</div>';
            html += '<div style="min-width:80px;color:var(--muted)">' + escapeHtml(entry.frame_id || '') + '</div>';
            html += '<div style="flex:1">' + tokenStr + ' (' + (entry.utilization_pct || 0).toFixed(1) + '%)</div>';
            html += '<div style="min-width:60px;text-align:right;color:var(--muted)">' + (entry.tools_count || 0) + ' tools</div>';
            html += '<div style="min-width:60px;text-align:right;color:var(--muted)">' + obsHumanizeAgo(entry.timestamp) + '</div>';
            html += '</div>';

            // Expandable detail panel (hidden by default)
            html += '<div class="obs-ctx-detail" id="obs-ctx-detail-' + idx + '" style="display:none">';

            // Token breakdown bar
            var breakdown = entry.token_breakdown || {};
            var sections = Object.keys(breakdown).sort(function (a, b) { return breakdown[b] - breakdown[a]; });
            if (sections.length > 0) {
                html += '<div style="margin-bottom:12px"><div style="font-size:11px;font-weight:600;color:var(--muted);margin-bottom:6px;text-transform:uppercase;letter-spacing:0.5px">Token Breakdown</div>';
                var sectionColors = { messages: '#60a5fa', tools_definition: '#a78bfa', identity: '#34d399', working_memory: '#fb923c', execution_ledger: '#f87171', relevant_facts: '#fbbf24', related_decisions: '#22d3ee', frame_instructions: '#c084fc', user_profile: '#38bdf8', censors: '#f472b6' };
                sections.forEach(function (s) {
                    var tokens = breakdown[s];
                    var pct = total > 0 ? (tokens / total * 100) : 0;
                    var color = sectionColors[s] || '#6b6b8a';
                    html += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:3px;font-size:12px">';
                    html += '<div style="min-width:120px;color:var(--muted)">' + escapeHtml(s) + '</div>';
                    html += '<div style="flex:1;height:6px;background:var(--border);border-radius:3px;overflow:hidden"><div style="width:' + pct.toFixed(1) + '%;height:100%;background:' + color + ';border-radius:3px"></div></div>';
                    html += '<div style="min-width:80px;text-align:right;color:var(--text)">' + obsFormatNumber(tokens) + ' <span style="color:var(--muted)">(' + pct.toFixed(0) + '%)</span></div>';
                    html += '</div>';
                });
                html += '</div>';
            }

            // Metadata grid
            html += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px;font-size:12px">';
            html += '<div><span style="color:var(--muted)">Model:</span> ' + escapeHtml(entry.model || '?') + '</div>';
            html += '<div><span style="color:var(--muted)">Call type:</span> ' + escapeHtml(entry.call_type || '?') + '</div>';
            html += '<div><span style="color:var(--muted)">Window:</span> ' + obsFormatNumber(entry.context_window_size || 0) + '</div>';
            html += '<div><span style="color:var(--muted)">Messages:</span> ' + (entry.messages_count || 0) + '</div>';
            html += '<div><span style="color:var(--muted)">Facts:</span> ' + (entry.loaded_facts || 0) + '</div>';
            html += '<div><span style="color:var(--muted)">Decisions:</span> ' + (entry.loaded_decisions || 0) + '</div>';
            if (entry.output_tokens) {
                html += '<div><span style="color:var(--muted)">Output:</span> ' + obsFormatNumber(entry.output_tokens) + ' tokens</div>';
            }
            if (entry.duration_ms) {
                html += '<div><span style="color:var(--muted)">Duration:</span> ' + (entry.duration_ms / 1000).toFixed(1) + 's</div>';
            }
            if (entry.cache_read) {
                html += '<div><span style="color:var(--muted)">Cache read:</span> ' + obsFormatNumber(entry.cache_read) + '</div>';
            }
            if (entry.stop_reason) {
                html += '<div><span style="color:var(--muted)">Stop:</span> ' + escapeHtml(entry.stop_reason) + '</div>';
            }
            html += '</div>';

            // Sections present
            var sectionsList = entry.sections_present || [];
            if (sectionsList.length > 0) {
                html += '<div style="margin-top:8px;font-size:11px;color:var(--muted)">Sections: ' + sectionsList.map(function (s) { return escapeHtml(s); }).join(', ') + '</div>';
            }

            // Tools list
            var toolNames = entry.tool_names || [];
            if (toolNames.length > 0) {
                html += '<div style="margin-top:4px;font-size:11px;color:var(--muted)">Tools: ' + toolNames.map(function (t) { return escapeHtml(t); }).join(', ') + '</div>';
            }

            html += '</div>'; // obs-ctx-detail
        });
    } else {
        html += '<div class="empty-state" style="padding:20px"><p>No API calls logged yet</p></div>';
    }
    html += '</div>'; // chart-card

    html += '</div>'; // chart-grid
    html += '</div>'; // obs-section

    container.innerHTML = html;

    // ── Click handlers for expandable context rows ──
    container.querySelectorAll('.obs-ctx-row[data-obs-ctx-idx]').forEach(function (row) {
        row.addEventListener('click', function () {
            var idx = this.dataset.obsCtxIdx;
            var detail = document.getElementById('obs-ctx-detail-' + idx);
            if (detail) {
                var isHidden = detail.style.display === 'none';
                detail.style.display = isHidden ? 'block' : 'none';
                this.style.background = isHidden ? 'var(--surface-hover)' : '';
            }
        });
    });

    // ── Render Charts ──
    renderObsCharts(data);
}

// ── Charts ─────────────────────────────────────────────────────────

function renderObsCharts(data) {
    var driftTrends = data.drift_trends || {};
    var ctxLog = data.context_log || [];

    // Fact growth trend
    var factData = driftTrends.fact_count_delta || [];
    if (factData.length > 0) {
        var factCtx = document.getElementById('chart-obs-facts');
        if (factCtx) {
            var labels = factData.map(function (p) {
                var d = new Date(p.t);
                return d.getMonth() + 1 + '/' + d.getDate() + ' ' + d.getHours() + ':00';
            });
            Dashboard.trackChart(new Chart(factCtx, {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: [{
                        label: 'Fact delta',
                        data: factData.map(function (p) { return p.v; }),
                        borderColor: '#60a5fa',
                        backgroundColor: 'rgba(96, 165, 250, 0.1)',
                        fill: true,
                    }]
                },
                options: {
                    scales: {
                        x: { display: true, ticks: { maxTicksLimit: 8, font: { size: 10 } } },
                        y: { beginAtZero: true }
                    },
                    plugins: { legend: { display: false } }
                }
            }));
        }
    }

    // Error rate trend
    var errorData = driftTrends.handler_error_rate || [];
    if (errorData.length > 0) {
        var errorCtx = document.getElementById('chart-obs-errors');
        if (errorCtx) {
            var labels2 = errorData.map(function (p) {
                var d = new Date(p.t);
                return d.getMonth() + 1 + '/' + d.getDate() + ' ' + d.getHours() + ':00';
            });
            Dashboard.trackChart(new Chart(errorCtx, {
                type: 'line',
                data: {
                    labels: labels2,
                    datasets: [{
                        label: 'Error rate',
                        data: errorData.map(function (p) { return p.v; }),
                        borderColor: '#f87171',
                        backgroundColor: 'rgba(248, 113, 113, 0.1)',
                        fill: true,
                    }]
                },
                options: {
                    scales: {
                        x: { display: true, ticks: { maxTicksLimit: 8, font: { size: 10 } } },
                        y: { beginAtZero: true }
                    },
                    plugins: { legend: { display: false } }
                }
            }));
        }
    }

    // Context token breakdown (last call) — doughnut
    if (ctxLog.length > 0) {
        var last = ctxLog[0];
        var breakdown = last.token_breakdown || {};
        var sections = Object.keys(breakdown).sort(function (a, b) { return breakdown[b] - breakdown[a]; });
        // Top 6 + "other"
        var topSections = sections.slice(0, 6);
        var otherTotal = 0;
        sections.slice(6).forEach(function (s) { otherTotal += breakdown[s]; });

        var doughnutLabels = topSections.map(function (s) { return s; });
        var doughnutValues = topSections.map(function (s) { return breakdown[s]; });
        if (otherTotal > 0) {
            doughnutLabels.push('other');
            doughnutValues.push(otherTotal);
        }

        var sectionColors = [
            '#60a5fa', '#a78bfa', '#34d399', '#fb923c', '#f87171', '#fbbf24', '#6b6b8a'
        ];

        var tokenCtx = document.getElementById('chart-obs-tokens');
        if (tokenCtx) {
            Dashboard.trackChart(new Chart(tokenCtx, {
                type: 'doughnut',
                data: {
                    labels: doughnutLabels,
                    datasets: [{
                        data: doughnutValues,
                        backgroundColor: sectionColors.slice(0, doughnutLabels.length),
                        borderWidth: 0,
                    }]
                },
                options: {
                    cutout: '60%',
                    plugins: {
                        legend: { position: 'right', labels: { font: { size: 11 }, padding: 8 } }
                    }
                }
            }));
        }
    }
}
