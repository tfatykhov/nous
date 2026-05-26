/**
 * Nous Dashboard — Core SPA Framework
 *
 * Provides hash routing, API client, loading/error/empty states,
 * and Chart.js lifecycle management.
 */

/* global Chart */

// Chart.js dark theme defaults
Chart.defaults.color = '#6b6b8a';
Chart.defaults.borderColor = '#1e1e2e';
Chart.defaults.plugins.tooltip.backgroundColor = '#111118';
Chart.defaults.plugins.tooltip.borderColor = '#1e1e2e';
Chart.defaults.plugins.tooltip.borderWidth = 1;
Chart.defaults.plugins.tooltip.cornerRadius = 8;
Chart.defaults.plugins.tooltip.titleFont = { size: 12, weight: '600' };
Chart.defaults.plugins.tooltip.bodyFont = { size: 12 };
Chart.defaults.plugins.tooltip.padding = 10;
Chart.defaults.plugins.legend.labels.boxWidth = 12;
Chart.defaults.plugins.legend.labels.padding = 16;
Chart.defaults.plugins.legend.labels.usePointStyle = true;
Chart.defaults.elements.point.radius = 3;
Chart.defaults.elements.point.hoverRadius = 5;
Chart.defaults.elements.line.tension = 0.3;
Chart.defaults.elements.line.borderWidth = 2;
Chart.defaults.elements.bar.borderRadius = 4;
Chart.defaults.responsive = true;
Chart.defaults.maintainAspectRatio = false;

const Dashboard = {
    views: {},
    currentView: null,
    charts: {},  // viewName -> [Chart instances]

    /**
     * Register a view with its load function.
     */
    registerView(name, loadFn) {
        this.views[name] = { load: loadFn, loaded: false };
    },

    /**
     * Load and display a view by name.
     */
    async loadView(name) {
        if (!this.views[name]) {
            name = 'overview';
        }

        // Destroy charts and reset loaded state for previous view
        // so it re-fetches data when revisited
        if (this.currentView && this.currentView !== name) {
            if (this.charts[this.currentView]) {
                this.charts[this.currentView].forEach(c => {
                    try { c.destroy(); } catch (e) { /* ignore */ }
                });
                this.charts[this.currentView] = [];
            }
            if (this.views[this.currentView]) {
                this.views[this.currentView].loaded = false;
            }
        }

        // Stop D3 simulation if leaving graph view
        if (this.currentView === 'graph' && this._graphSimulation) {
            this._graphSimulation.stop();
            this._graphSimulation = null;
        }

        // Hide all views
        document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));

        // Show target view
        const container = document.getElementById('view-' + name);
        if (container) {
            container.classList.add('active');
        }

        // Update nav
        document.querySelectorAll('.nav-link').forEach(link => {
            link.classList.toggle('active', link.dataset.view === name);
        });

        this.currentView = name;
        this.charts[name] = this.charts[name] || [];

        // Load view content
        const view = this.views[name];
        if (view && !view.loaded) {
            try {
                await view.load(container);
                view.loaded = true;
            } catch (err) {
                console.error('Failed to load view:', name, err);
                this.showError(container, 'Failed to load ' + name + ' view.', () => {
                    view.loaded = false;
                    this.loadView(name);
                });
            }
        }
    },

    /**
     * Track a Chart.js instance for the current view.
     */
    trackChart(chart) {
        if (this.currentView) {
            this.charts[this.currentView] = this.charts[this.currentView] || [];
            this.charts[this.currentView].push(chart);
        }
        return chart;
    },

    /**
     * Force reload a view (clears loaded state).
     */
    reloadView(name) {
        if (this.views[name]) {
            this.views[name].loaded = false;
            // Clear charts
            if (this.charts[name]) {
                this.charts[name].forEach(c => {
                    try { c.destroy(); } catch (e) { /* ignore */ }
                });
                this.charts[name] = [];
            }
            if (this.currentView === name) {
                this.loadView(name);
            }
        }
    },

    /**
     * Fetch JSON from API with retry logic.
     */
    async apiGet(path, retries) {
        if (retries === undefined) retries = 3;
        for (var i = 0; i < retries; i++) {
            try {
                var res = await fetch(path);
                if (!res.ok) throw new Error(res.status + ' ' + res.statusText);
                return await res.json();
            } catch (err) {
                if (i === retries - 1) throw err;
                await new Promise(function(r) { setTimeout(r, 1000 * Math.pow(2, i)); });
            }
        }
    },

    /**
     * Show loading skeleton in a container.
     */
    showLoading(container) {
        container.innerHTML =
            '<div class="view-header"><div class="skeleton" style="width:200px;height:28px;margin-bottom:8px"></div>' +
            '<div class="skeleton" style="width:300px;height:16px"></div></div>' +
            '<div class="loading-grid">' +
            '<div class="skeleton skeleton-card"></div>'.repeat(4) +
            '</div>' +
            '<div class="chart-grid">' +
            '<div class="skeleton skeleton-chart"></div>'.repeat(2) +
            '</div>';
    },

    /**
     * Show error state in a container.
     */
    showError(container, msg, retryFn) {
        container.innerHTML =
            '<div class="error-banner">' +
            '<div class="error-icon">&#x26A0;</div>' +
            '<div><div class="error-msg">' + escapeHtml(msg) + '</div>' +
            '<div class="error-detail">Check that the Nous server is running and accessible.</div></div>' +
            (retryFn ? '<button class="btn" id="retry-btn">Retry</button>' : '') +
            '</div>';
        if (retryFn) {
            var btn = document.getElementById('retry-btn');
            if (btn) btn.addEventListener('click', retryFn);
        }
    },

    /**
     * Show empty state in a container.
     */
    showEmpty(container, msg) {
        container.innerHTML =
            '<div class="empty-state">' +
            '<div class="empty-icon">&#x1D6B9;</div>' +
            '<h3>No Data Yet</h3>' +
            '<p>' + escapeHtml(msg) + '</p>' +
            '</div>';
    },

    /**
     * Format a number with commas.
     */
    formatNumber(n) {
        if (n == null) return '0';
        return n.toLocaleString();
    },

    /**
     * Format a date string to a short readable form.
     */
    formatDate(dateStr) {
        if (!dateStr) return '-';
        var d = new Date(dateStr);
        return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
    },

    /**
     * Format a datetime string.
     */
    formatDateTime(dateStr) {
        if (!dateStr) return '-';
        var d = new Date(dateStr);
        return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) + ' ' +
               d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' });
    },

    /**
     * Truncate text with ellipsis.
     */
    truncate(text, maxLen) {
        if (!text) return '';
        if (text.length <= maxLen) return text;
        return text.slice(0, maxLen) + '...';
    },

    /**
     * Get node type color from CSS vars.
     */
    typeColor(type) {
        var colors = {
            fact: '#60a5fa',
            episode: '#34d399',
            decision: '#a78bfa',
            procedure: '#fb923c',
            censor: '#f87171',
            chunk: '#06b6d4'
        };
        return colors[type] || '#6b6b8a';
    },

    /**
     * Get outcome badge class.
     */
    outcomeBadge(outcome) {
        if (!outcome) return 'badge-pending';
        var map = { success: 'badge-success', failure: 'badge-failure', partial: 'badge-partial', pending: 'badge-pending' };
        return map[outcome] || 'badge-pending';
    },

    /**
     * Get responsive legend position based on viewport width.
     * Returns 'bottom' on mobile, 'right' on desktop.
     */
    legendPosition: function() {
        return window.innerWidth <= 768 ? 'bottom' : 'right';
    }
};

/**
 * Escape HTML to prevent XSS.
 */
function escapeHtml(str) {
    if (!str) return '';
    var div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

/**
 * Create an element with attributes and children.
 */
function el(tag, attrs, children) {
    var elem = document.createElement(tag);
    if (attrs) {
        Object.keys(attrs).forEach(function(key) {
            if (key === 'className') elem.className = attrs[key];
            else if (key === 'innerHTML') elem.innerHTML = attrs[key];
            else if (key === 'textContent') elem.textContent = attrs[key];
            else if (key.startsWith('on')) elem.addEventListener(key.slice(2).toLowerCase(), attrs[key]);
            else elem.setAttribute(key, attrs[key]);
        });
    }
    if (children) {
        if (typeof children === 'string') {
            elem.textContent = children;
        } else if (Array.isArray(children)) {
            children.forEach(function(child) {
                if (child) elem.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
            });
        }
    }
    return elem;
}

// Hash Router
function handleRoute() {
    var hash = location.hash.slice(2) || 'overview';
    Dashboard.loadView(hash);
}

window.addEventListener('hashchange', handleRoute);

document.addEventListener('DOMContentLoaded', function() {
    // Sidebar toggle (desktop collapse)
    var toggleBtn = document.getElementById('sidebar-toggle');
    var sidebar = document.getElementById('sidebar');
    if (toggleBtn && sidebar) {
        toggleBtn.addEventListener('click', function() {
            sidebar.classList.toggle('collapsed');
        });
    }

    // Mobile drawer
    var mobileMenuBtn = document.getElementById('mobile-menu-btn');
    var overlay = document.getElementById('sidebar-overlay');

    function openMobileNav() {
        if (sidebar) {
            sidebar.classList.add('mobile-open');
            sidebar.removeAttribute('aria-hidden');
            sidebar.removeAttribute('inert');
        }
        if (overlay) overlay.classList.add('visible');
    }

    function closeMobileNav() {
        if (sidebar) {
            sidebar.classList.remove('mobile-open');
            if (window.innerWidth <= 768) {
                sidebar.setAttribute('aria-hidden', 'true');
                sidebar.setAttribute('inert', '');
            }
        }
        if (overlay) overlay.classList.remove('visible');
    }

    if (mobileMenuBtn) {
        mobileMenuBtn.addEventListener('click', openMobileNav);
    }

    if (overlay) {
        overlay.addEventListener('click', closeMobileNav);
    }

    // Close drawer on nav link click (mobile)
    document.querySelectorAll('.nav-link').forEach(function(link) {
        link.addEventListener('click', function() {
            if (window.innerWidth <= 768) {
                closeMobileNav();
            }
        });
    });

    // Escape key closes drawer
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape' && sidebar && sidebar.classList.contains('mobile-open')) {
            closeMobileNav();
        }
    });

    // Close drawer on orientation change / resize past breakpoint
    window.addEventListener('resize', function() {
        if (window.innerWidth > 768) {
            closeMobileNav();
            if (sidebar) {
                sidebar.removeAttribute('aria-hidden');
                sidebar.removeAttribute('inert');
            }
        }
    });

    // Set initial ARIA state for mobile
    if (window.innerWidth <= 768 && sidebar) {
        sidebar.setAttribute('aria-hidden', 'true');
        sidebar.setAttribute('inert', '');
    }

    // Initial route
    handleRoute();
});
