/**
 * Nous Dashboard — Subtasks View (F061 PR-3)
 *
 * Subtask outcome metrics: 5-state outcome counts, empty-rate / retry-rate,
 * tokens by outcome, top failing tasks, DAG correlation, recent outcomes,
 * daily trend. Auto-refreshes every 30 seconds.
 *
 * Backed by GET /dashboard/subtasks?hours=24
 */

/* global Dashboard, Chart, escapeHtml */

var _stRefreshInterval = null;
var _stAbortController = null;

Dashboard.registerView('subtasks', async function (container) {
    Dashboard.showLoading(container);
    try {
        var data = await stFetch();
        renderSubtasks(container, data);
        startStAutoRefresh(container);
    } catch (err) {
        Dashboard.showError(container, 'Failed to load subtask data.', function () {
            Dashboard.reloadView('subtasks');
        });
    }
});

function stFetch() {
    if (_stAbortController) {
        _stAbortController.abort();
    }
    _stAbortController = new AbortController();
    return fetch('/dashboard/subtasks?hours=24', { signal: _stAbortController.signal })
        .then(function (res) {
            if (!res.ok) throw new Error(res.status + ' ' + res.statusText);
            return res.json();
        })
        .finally(function () {
            _stAbortController = null;
        });
}

function startStAutoRefresh(container) {
    stopStAutoRefresh();
    _stRefreshInterval = setInterval(async function () {
        if (Dashboard.currentView !== 'subtasks') {
            stopStAutoRefresh();
            return;
        }
        try {
            var data = await stFetch();
            renderSubtasks(container, data);
        } catch (err) {
            // Silent retry — banner only on initial load
        }
    }, 30000);
}

function stopStAutoRefresh() {
    if (_stRefreshInterval) {
        clearInterval(_stRefreshInterval);
        _stRefreshInterval = null;
    }
}

// Outcome → CSS color class. Mirrors the 5-state enum.
var OUTCOME_COLORS = {
    completed: '#10b981',                // green
    incomplete_blocked: '#f59e0b',       // amber (soft-fail)
    incomplete_no_terminal: '#ef4444',   // red
    validation_failed: '#ef4444',        // red
    timed_out: '#dc2626',                // dark red
    errored: '#7c2d12',                  // dark brown
    cancelled: '#6b7280',                // gray
    unknown: '#9ca3af',                  // gray (legacy pre-flag rows)
};

function outcomeColor(name) {
    return OUTCOME_COLORS[name] || '#6b7280';
}

function renderSubtasks(container, data) {
    var totals = data.totals || { by_outcome: {}, total_terminal: 0, empty_rate: 0, retry_rate: 0 };
    var byOutcome = totals.by_outcome || {};
    var totalsHtml = renderTotalsCards(totals);
    var byOutcomeHtml = renderOutcomeBars(byOutcome, totals.total_terminal);
    var tokensHtml = renderTokensTable(data.tokens_by_outcome || {});
    var failingHtml = renderTopFailing(data.top_failing_tasks || []);
    var dagHtml = renderDagCorrelation(data.dag_correlation || {});
    var recentHtml = renderRecent(data.recent_outcomes || []);
    var trendHtml = renderDailyTrend(data.daily_trend || []);

    container.innerHTML = (
        '<div class="view-header">' +
        '  <h1>Subtasks <span class="view-subtitle">F061 outcome metrics — last ' +
              (data.window_hours || 24) + 'h</span></h1>' +
        '</div>' +
        totalsHtml +
        '<div class="card"><h2>Outcome distribution</h2>' + byOutcomeHtml + '</div>' +
        '<div class="card"><h2>Tokens by outcome</h2>' + tokensHtml + '</div>' +
        '<div class="card"><h2>Top failing tasks</h2>' + failingHtml + '</div>' +
        '<div class="card"><h2>DAG correlation</h2>' + dagHtml + '</div>' +
        '<div class="card"><h2>Daily trend</h2>' + trendHtml + '</div>' +
        '<div class="card"><h2>Recent terminal subtasks</h2>' + recentHtml + '</div>'
    );
}

function renderTotalsCards(totals) {
    var pct = function (v) { return (v * 100).toFixed(1) + '%'; };
    return (
        '<div class="stat-cards">' +
          '<div class="stat-card"><div class="stat-label">Total terminal</div>' +
            '<div class="stat-value">' + totals.total_terminal + '</div></div>' +
          '<div class="stat-card"><div class="stat-label">Empty rate</div>' +
            '<div class="stat-value">' + pct(totals.empty_rate || 0) + '</div>' +
            '<div class="stat-sublabel">incomplete_no_terminal + validation_failed</div></div>' +
          '<div class="stat-card"><div class="stat-label">Retry rate</div>' +
            '<div class="stat-value">' + pct(totals.retry_rate || 0) + '</div>' +
            '<div class="stat-sublabel">attempts &gt; 1</div></div>' +
        '</div>'
    );
}

function renderOutcomeBars(byOutcome, total) {
    var keys = Object.keys(byOutcome).sort(function (a, b) {
        return byOutcome[b] - byOutcome[a];
    });
    if (keys.length === 0) {
        return '<p class="muted">No terminal subtasks in window.</p>';
    }
    var rows = keys.map(function (k) {
        var cnt = byOutcome[k];
        var width = total > 0 ? (cnt / total * 100).toFixed(1) : 0;
        return (
            '<div class="bar-row">' +
              '<span class="bar-label">' + escapeHtml(k) + '</span>' +
              '<span class="bar-track">' +
                '<span class="bar-fill" style="width:' + width + '%;background:' +
                  outcomeColor(k) + '"></span>' +
              '</span>' +
              '<span class="bar-count">' + cnt + '</span>' +
            '</div>'
        );
    }).join('');
    return rows;
}

function renderTokensTable(tokensByOutcome) {
    var keys = Object.keys(tokensByOutcome);
    if (keys.length === 0) {
        return '<p class="muted">No data.</p>';
    }
    var rows = keys.map(function (k) {
        var v = tokensByOutcome[k] || {};
        return (
            '<tr>' +
              '<td>' + escapeHtml(k) + '</td>' +
              '<td class="num">' + (v.mean_total_tokens || 0) + '</td>' +
              '<td class="num">' + (v.mean_tool_calls || 0) + '</td>' +
              '<td class="num">' + (v.n || 0) + '</td>' +
            '</tr>'
        );
    }).join('');
    return (
        '<table class="data-table"><thead><tr>' +
          '<th>Outcome</th><th>Mean total tokens</th><th>Mean tool calls</th><th>n</th>' +
        '</tr></thead><tbody>' + rows + '</tbody></table>'
    );
}

function renderTopFailing(items) {
    if (!items.length) {
        return '<p class="muted">No failing tasks in window. 🎉</p>';
    }
    var rows = items.map(function (it) {
        var ratePct = (it.failure_rate * 100).toFixed(1) + '%';
        return (
            '<tr>' +
              '<td>' + escapeHtml(it.task_prefix) + '</td>' +
              '<td class="num">' + it.failures + '</td>' +
              '<td class="num">' + it.total + '</td>' +
              '<td class="num">' + ratePct + '</td>' +
            '</tr>'
        );
    }).join('');
    return (
        '<table class="data-table"><thead><tr>' +
          '<th>Task (first 80 chars)</th><th>Failures</th><th>Total</th><th>Rate</th>' +
        '</tr></thead><tbody>' + rows + '</tbody></table>'
    );
}

function renderDagCorrelation(dagCorr) {
    var keys = Object.keys(dagCorr);
    if (keys.length === 0) {
        return '<p class="muted">No DAG-attached subtasks in window.</p>';
    }
    var rows = keys.sort().map(function (k) {
        return '<tr><td>' + escapeHtml(k) + '</td><td class="num">' + dagCorr[k] + '</td></tr>';
    }).join('');
    return (
        '<table class="data-table"><thead><tr>' +
          '<th>Outcome</th><th>Count (DAG-attached)</th>' +
        '</tr></thead><tbody>' + rows + '</tbody></table>'
    );
}

function renderRecent(rows) {
    if (!rows.length) {
        return '<p class="muted">No recent terminal subtasks.</p>';
    }
    var html = rows.map(function (r) {
        var ts = r.completed_at ? new Date(r.completed_at).toLocaleString() : '-';
        var dag = r.dag_node_id ? ('<span class="badge">DAG:' + r.dag_node_id.slice(0, 8) + '</span>') : '';
        return (
            '<tr>' +
              '<td>' + ts + '</td>' +
              '<td><span class="pill" style="background:' + outcomeColor(r.final_outcome) +
                '">' + escapeHtml(r.final_outcome || '-') + '</span></td>' +
              '<td class="num">' + (r.attempts || 0) + '</td>' +
              '<td class="num">' + ((r.tokens_in || 0) + (r.tokens_out || 0)) + '</td>' +
              '<td class="num">' + (r.tool_calls_made || 0) + '</td>' +
              '<td>' + escapeHtml(r.task) + ' ' + dag + '</td>' +
            '</tr>'
        );
    }).join('');
    return (
        '<table class="data-table"><thead><tr>' +
          '<th>Completed</th><th>Outcome</th><th>Attempts</th><th>Tokens</th>' +
          '<th>Tool calls</th><th>Task</th>' +
        '</tr></thead><tbody>' + html + '</tbody></table>'
    );
}

function renderDailyTrend(daily) {
    if (!daily.length) {
        return '<p class="muted">No data.</p>';
    }
    // Lightweight stacked-bar table; full Chart.js is overkill for v1.
    var allOutcomes = {};
    daily.forEach(function (d) {
        Object.keys(d.by_outcome || {}).forEach(function (k) { allOutcomes[k] = true; });
    });
    var outcomes = Object.keys(allOutcomes).sort();
    if (!outcomes.length) {
        return '<p class="muted">No data.</p>';
    }
    var headers = '<th>Date</th>' + outcomes.map(function (o) {
        return '<th class="num">' + escapeHtml(o) + '</th>';
    }).join('');
    var rows = daily.map(function (d) {
        var cells = outcomes.map(function (o) {
            return '<td class="num">' + ((d.by_outcome || {})[o] || 0) + '</td>';
        }).join('');
        return '<tr><td>' + d.date + '</td>' + cells + '</tr>';
    }).join('');
    return (
        '<table class="data-table"><thead><tr>' + headers + '</tr></thead>' +
        '<tbody>' + rows + '</tbody></table>'
    );
}
