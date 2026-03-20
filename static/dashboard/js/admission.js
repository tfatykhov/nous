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
