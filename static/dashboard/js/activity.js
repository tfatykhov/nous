/**
 * Nous Dashboard — Activity View
 *
 * System activity timeline, censor stats, schedule status, sleep stats.
 * Fetches from GET /dashboard/activity
 */

/* global Dashboard, escapeHtml */

Dashboard.registerView('activity', async function(container) {
    Dashboard.showLoading(container);

    var data;
    try {
        data = await Dashboard.apiGet('/dashboard/activity?hours=168');
    } catch (err) {
        Dashboard.showError(container, 'Failed to load activity data.', function() {
            Dashboard.reloadView('activity');
        });
        return;
    }

    var events = data.events || [];
    var censorStats = data.censor_stats || {};
    var scheduleStats = data.schedule_stats || {};
    var sleepStats = data.sleep_stats || {};

    // Check empty
    var hasData = events.length > 0 || censorStats.total_activations_7d > 0 ||
                  scheduleStats.active > 0 || sleepStats.last_sleep;

    if (!hasData) {
        Dashboard.showEmpty(container, 'No system activity yet. Activity appears as Nous processes conversations, fires schedules, and runs sleep cycles.');
        return;
    }

    var html = '<div class="view-header"><h1>System Activity</h1><p>Events, censors, schedules, and sleep cycles</p></div>';

    // Stat cards row
    html += '<div class="stat-grid">';
    html += buildActivityCard('Censor Activations', censorStats.total_activations_7d || 0, '7 days', 'var(--censor-color)');
    html += buildActivityCard('Auto-created Censors', censorStats.auto_created || 0, 'total', 'var(--yellow)');
    html += buildActivityCard('Manual Censors', censorStats.manual_created || 0, 'total', 'var(--muted)');
    html += buildActivityCard('False Positives', censorStats.false_positives_7d || 0, '7 days', 'var(--red)');
    html += buildActivityCard('Active Schedules', scheduleStats.active || 0, '', 'var(--green)');
    html += buildActivityCard('Fires (7d)', scheduleStats.fires_7d || 0, '7 days', 'var(--accent)');
    html += buildActivityCard('Last Sleep', sleepStats.last_sleep ? formatTimeAgo(sleepStats.last_sleep) : 'Never', '', 'var(--episode-color)');
    html += buildActivityCard('Sleep Facts Created', sleepStats.facts_created || 0, 'last sleep', 'var(--fact-color)');
    html += '</div>';

    // Two columns: timeline + side panels
    html += '<div style="display:grid;grid-template-columns:1fr 360px;gap:24px;align-items:start">';

    // Activity Timeline
    html += '<div>';
    html += '<h2 style="font-size:16px;font-weight:600;margin-bottom:16px">Event Timeline</h2>';
    if (events.length > 0) {
        html += '<div class="timeline">';
        events.forEach(function(event) {
            html += buildTimelineItem(event);
        });
        html += '</div>';
    } else {
        html += '<div class="empty-state" style="padding:24px"><p>No events in the last 7 days.</p></div>';
    }
    html += '</div>';

    // Side panels
    html += '<div>';

    // Top censors
    html += '<div class="chart-card mb-16"><h3>Most Active Censors</h3>';
    if (censorStats.top_censors && censorStats.top_censors.length > 0) {
        html += '<div style="font-size:13px">';
        censorStats.top_censors.forEach(function(c) {
            html += '<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border)">' +
                '<span class="cell-truncate" style="max-width:200px" title="' + escapeHtml(c.trigger_pattern || '') + '">' +
                escapeHtml(c.trigger_pattern || c.id || '') + '</span>' +
                '<span class="badge badge-censor">' + (c.activations || 0) + '</span></div>';
        });
        html += '</div>';
    } else {
        html += '<p class="text-muted" style="font-size:12px;padding:12px 0">No censor activations</p>';
    }
    html += '</div>';

    // Next fires
    html += '<div class="chart-card mb-16"><h3>Upcoming Schedules</h3>';
    if (scheduleStats.next_fires && scheduleStats.next_fires.length > 0) {
        html += '<div style="font-size:13px">';
        scheduleStats.next_fires.forEach(function(s) {
            html += '<div style="padding:8px 0;border-bottom:1px solid var(--border)">' +
                '<div>' + escapeHtml(s.task || s.id || '') + '</div>' +
                '<div class="text-muted" style="font-size:11px">Next: ' + Dashboard.formatDateTime(s.next_fire_at) + '</div>' +
                '</div>';
        });
        html += '</div>';
    } else {
        html += '<p class="text-muted" style="font-size:12px;padding:12px 0">No upcoming schedules</p>';
    }
    html += '</div>';

    // Sleep stats
    html += '<div class="chart-card"><h3>Sleep Activity</h3>';
    html += '<div class="detail-grid" style="font-size:13px">';
    html += '<div class="detail-label">Last Sleep</div><div class="detail-value">' + (sleepStats.last_sleep ? Dashboard.formatDateTime(sleepStats.last_sleep) : 'Never') + '</div>';
    html += '<div class="detail-label">Facts Created</div><div class="detail-value">' + (sleepStats.facts_created || 0) + '</div>';
    html += '<div class="detail-label">Procedures</div><div class="detail-value">' + (sleepStats.procedures_created || 0) + '</div>';
    html += '<div class="detail-label">Censors Retired</div><div class="detail-value">' + (sleepStats.censors_retired || 0) + '</div>';
    html += '</div></div>';

    html += '</div>';  // side panels
    html += '</div>';  // grid

    container.innerHTML = html;
});

function buildActivityCard(label, value, period, color) {
    return '<div class="stat-card">' +
        '<div class="stat-value" style="color:' + color + '">' + escapeHtml(String(value)) + '</div>' +
        '<div class="stat-label">' + escapeHtml(label) + '</div>' +
        (period ? '<div style="font-size:11px;color:var(--muted);margin-top:2px">' + escapeHtml(period) + '</div>' : '') +
        '</div>';
}

function buildTimelineItem(event) {
    var type = event.type || 'unknown';
    var typeLabel = type.replace(/_/g, ' ');
    var time = event.created_at ? Dashboard.formatDateTime(event.created_at) : '';
    var summary = buildEventSummary(event);

    return '<div class="timeline-item event-' + escapeHtml(type) + '">' +
        '<div class="event-type-badge">' + escapeHtml(typeLabel) + '</div>' +
        '<div class="event-summary">' + summary + '</div>' +
        '<div class="event-time">' + escapeHtml(time) + '</div>' +
        '</div>';
}

function buildEventSummary(event) {
    var data = event.data || {};
    var type = event.type || '';

    switch (type) {
        case 'censor_activated':
            return 'Censor <strong>' + escapeHtml(data.trigger || '') + '</strong> fired (' + escapeHtml(data.action || 'warn') + ')';
        case 'sleep_completed':
            return 'Sleep cycle completed. ' + (data.facts_created || 0) + ' facts, ' + (data.procedures_created || 0) + ' procedures.';
        case 'schedule_fired':
            return 'Schedule <strong>' + escapeHtml(data.task || data.schedule_id || '') + '</strong> fired.';
        case 'subtask_completed':
            return 'Subtask completed: ' + escapeHtml(data.title || data.subtask_id || '');
        case 'subtask_failed':
            return 'Subtask failed: ' + escapeHtml(data.title || data.subtask_id || '') +
                (data.error ? ' — ' + escapeHtml(data.error) : '');
        case 'censor_created':
            return 'New censor created: ' + escapeHtml(data.trigger_pattern || '') +
                ' (' + (data.auto ? 'auto' : 'manual') + ')';
        case 'censor_escalated':
            return 'Censor escalated: ' + escapeHtml(data.trigger_pattern || '') +
                ' — ' + escapeHtml(data.reason || '');
        default:
            return escapeHtml(JSON.stringify(data).slice(0, 120));
    }
}

function formatTimeAgo(dateStr) {
    if (!dateStr) return 'Never';
    var now = new Date();
    var then = new Date(dateStr);
    var diffMs = now - then;
    var diffMins = Math.floor(diffMs / 60000);
    var diffHours = Math.floor(diffMins / 60);
    var diffDays = Math.floor(diffHours / 24);

    if (diffMins < 1) return 'Just now';
    if (diffMins < 60) return diffMins + 'm ago';
    if (diffHours < 24) return diffHours + 'h ago';
    if (diffDays < 7) return diffDays + 'd ago';
    return Dashboard.formatDate(dateStr);
}
