/**
 * Nous Dashboard — Knowledge Graph View (Cytoscape edition)
 *
 * Replaced D3 force-directed with Cytoscape.js to handle the 5K-10K-node
 * scale the graph is growing into and to give us layout switching,
 * centrality-based sizing, and community detection essentially for free.
 *
 * Parity features carried over from the previous D3 implementation:
 *   - Type filter checkboxes (fact, episode, decision, procedure, chunk)
 *   - Min-edges slider
 *   - Search box with neighborhood highlight
 *   - Click node → detail panel with full record fetch
 *   - Stats overlay (nodes, edges, shown, orphans)
 *   - F065 provenance: 'inferred' edges drawn dashed at lower opacity
 *   - F070 relation colors: part_of / summarized_by
 *
 * New in Phase 3:
 *   - Degree-based node sizing — hubs visible at a glance
 *   - "Highlight clusters" toggle — connected components colored as a
 *     border ring, so the dec↔dec cluster, fact/episode bridges, and
 *     chunk constellations visually separate without losing type color
 *
 * Phase 2 (compound nodes, layout switcher) deferred per user direction.
 */

/* global Dashboard, cytoscape, escapeHtml */

Dashboard.registerView('graph', async function (container) {
    // Codex P2 (2026-05-26): destroy any prior Cytoscape instance before
    // creating a new one. Dashboard.loadView() handles teardown on
    // navigation transitions, but Dashboard.reloadView('graph') re-runs
    // this function without firing the leave-view branch. Without this
    // belt-and-suspenders cleanup, repeated reloads accumulate canvas
    // contexts + event listeners + RAF callbacks.
    if (Dashboard._cyInstance) {
        try { Dashboard._cyInstance.destroy(); } catch (e) { /* ignore */ }
        Dashboard._cyInstance = null;
    }

    Dashboard.showLoading(container);

    var data;
    try {
        data = await Dashboard.apiGet('/dashboard/graph?limit=500');
    } catch (err) {
        Dashboard.showError(container, 'Failed to load graph data.', function () {
            Dashboard.reloadView('graph');
        });
        return;
    }

    if (!data.nodes || data.nodes.length === 0) {
        Dashboard.showEmpty(
            container,
            'No graph edges yet. As Nous learns facts and makes decisions, connections will appear here.'
        );
        return;
    }

    // ── Build view scaffold ────────────────────────────────────────────
    container.innerHTML =
        '<div class="view-header"><h1>Knowledge Graph</h1>' +
        '<p>' + (data.stats ? data.stats.node_count + ' nodes, ' + data.stats.total_edges + ' edges' : '') + '</p></div>' +
        '<div class="graph-container" id="graph-container">' +
            '<div class="graph-controls" id="graph-controls"></div>' +
            '<div class="graph-stats-overlay" id="graph-stats"></div>' +
            '<div class="graph-detail-panel" id="graph-detail"></div>' +
        '</div>';

    var graphEl = document.getElementById('graph-container');
    var statsEl = document.getElementById('graph-stats');
    var detailEl = document.getElementById('graph-detail');
    detailEl.addEventListener('pointerdown', function (e) { e.stopPropagation(); });
    detailEl.addEventListener('touchstart', function (e) { e.stopPropagation(); }, { passive: true });
    var controlsEl = document.getElementById('graph-controls');

    // ── State ──────────────────────────────────────────────────────────
    // Compute edge_count for each node from the edge data (used by
    // min-edges filter and degree-sizing fallback).
    var edgeCounts = {};
    data.edges.forEach(function (e) {
        edgeCounts[e.source] = (edgeCounts[e.source] || 0) + 1;
        edgeCounts[e.target] = (edgeCounts[e.target] || 0) + 1;
    });
    data.nodes.forEach(function (n) {
        n.edge_count = edgeCounts[n.id] || 0;
    });

    var filters = {
        types: { fact: true, episode: true, decision: true, procedure: true, chunk: true },
        minEdges: 0,
        search: '',
    };
    var clusterMode = false;       // Phase 3: cluster-coloring toggle

    var cy = null; // Cytoscape instance

    // ── Build UI controls ──────────────────────────────────────────────
    buildControls(controlsEl);
    renderStats(statsEl, data.stats || {});

    // ── Initial render ─────────────────────────────────────────────────
    cy = buildCytoscape(graphEl, data.nodes, data.edges);
    // Publish on Dashboard so loadView() can destroy() on navigation away.
    Dashboard._cyInstance = cy;
    cy.ready(function () {
        runLayout();
        computeClusters();
    });

    // ───────────────────────────────────────────────────────────────────
    // Control builders
    // ───────────────────────────────────────────────────────────────────

    function buildControls(el) {
        // Search
        var searchInput = document.createElement('input');
        searchInput.type = 'text';
        searchInput.className = 'search-input';
        searchInput.placeholder = 'Search nodes...';
        searchInput.style.maxWidth = '200px';
        searchInput.addEventListener('input', function () {
            filters.search = this.value.toLowerCase();
            applySearchHighlight();
        });
        el.appendChild(searchInput);

        // Type filters
        var checksDiv = document.createElement('div');
        checksDiv.className = 'filter-checks';
        checksDiv.style.cssText =
            'background:rgba(17,17,24,0.9);padding:8px 12px;border-radius:8px;' +
            'border:1px solid var(--border);';

        var types = ['fact', 'episode', 'decision', 'procedure', 'chunk'];
        types.forEach(function (type) {
            var label = document.createElement('label');
            label.className = 'filter-check';
            var cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.checked = true;
            cb.addEventListener('change', function () {
                filters.types[type] = this.checked;
                applyFilters();
            });
            label.appendChild(cb);
            var dot = document.createElement('span');
            dot.style.cssText =
                'width:8px;height:8px;border-radius:50%;background:' +
                Dashboard.typeColor(type);
            label.appendChild(dot);
            label.appendChild(document.createTextNode(' ' + type.charAt(0).toUpperCase() + type.slice(1)));
            checksDiv.appendChild(label);
        });
        el.appendChild(checksDiv);

        // Min edges slider
        var sliderDiv = document.createElement('div');
        sliderDiv.style.cssText =
            'background:rgba(17,17,24,0.9);padding:8px 12px;border-radius:8px;' +
            'border:1px solid var(--border);font-size:12px;color:var(--muted);';
        sliderDiv.innerHTML = 'Min edges: <span id="min-edge-val">0</span>';
        var slider = document.createElement('input');
        slider.type = 'range';
        slider.className = 'range-slider';
        slider.min = '0';
        slider.max = '10';
        slider.value = '0';
        slider.addEventListener('input', function () {
            filters.minEdges = parseInt(this.value, 10);
            document.getElementById('min-edge-val').textContent = this.value;
            applyFilters();
        });
        sliderDiv.appendChild(slider);
        el.appendChild(sliderDiv);

        // Phase 3: cluster-coloring toggle
        var clusterDiv = document.createElement('label');
        clusterDiv.className = 'filter-check';
        clusterDiv.style.cssText =
            'background:rgba(17,17,24,0.9);padding:8px 12px;border-radius:8px;' +
            'border:1px solid var(--border);font-size:12px;color:var(--muted);';
        var clusterCb = document.createElement('input');
        clusterCb.type = 'checkbox';
        clusterCb.checked = false;
        clusterCb.addEventListener('change', function () {
            clusterMode = this.checked;
            applyClusterColors();
        });
        clusterDiv.appendChild(clusterCb);
        clusterDiv.appendChild(document.createTextNode(' Highlight clusters'));
        el.appendChild(clusterDiv);
    }

    function renderStats(el, stats) {
        var orphanTotal = 0;
        if (stats.orphan_counts) {
            Object.keys(stats.orphan_counts).forEach(function (k) {
                orphanTotal += stats.orphan_counts[k];
            });
        }
        el.innerHTML =
            '<div class="stat-item"><div class="stat-num">' + (stats.node_count || 0) + '</div><div>Nodes</div></div>' +
            '<div class="stat-item"><div class="stat-num">' + (stats.total_edges || 0) + '</div><div>Edges</div></div>' +
            '<div class="stat-item"><div class="stat-num">' + (stats.displayed_edges || 0) + '</div><div>Shown</div></div>' +
            '<div class="stat-item"><div class="stat-num">' + orphanTotal + '</div><div>Orphans</div></div>';
    }

    // ───────────────────────────────────────────────────────────────────
    // Cytoscape factory
    // ───────────────────────────────────────────────────────────────────

    function buildCytoscape(containerEl, nodes, edges) {
        var elements = {
            nodes: nodes.map(function (n) {
                return {
                    data: {
                        id: n.id,
                        label: n.label || '',
                        type: n.type,
                        category: n.category || '',
                        edge_count: n.edge_count || 0,
                        created_at: n.created_at || null,
                        // Color computed at element creation; Cytoscape lets us
                        // read this directly in style selectors via mapData.
                        color: Dashboard.typeColor(n.type),
                    },
                };
            }),
            edges: edges.map(function (e) {
                return {
                    data: {
                        id: e.id,
                        source: e.source,
                        target: e.target,
                        relation: e.relation,
                        weight: e.weight || 0.5,
                        extraction_method: e.extraction_method || 'heuristic',
                        color: relationColor(e.relation),
                    },
                };
            }),
        };

        return cytoscape({
            container: containerEl,
            elements: elements,
            wheelSensitivity: 0.2,
            // Initial layout chosen separately via runLayout() so we can
            // tune params and re-run on filter changes.
            layout: { name: 'preset' },
            style: [
                {
                    selector: 'node',
                    style: {
                        'background-color': 'data(color)',
                        'border-width': 1,
                        'border-color': 'rgba(0,0,0,0.3)',
                        // Phase 3: size by degree. mapData(prop, min_in, max_in, min_out, max_out).
                        // Clamp at 30 incoming edges so super-hubs don't dwarf the rest.
                        'width': 'mapData(edge_count, 0, 30, 14, 44)',
                        'height': 'mapData(edge_count, 0, 30, 14, 44)',
                        'label': '',  // labels only for high-connectivity nodes (see below)
                        'font-size': '9px',
                        'color': '#6b6b8a',
                        'text-valign': 'bottom',
                        'text-margin-y': 4,
                        'text-outline-color': '#0a0a0f',
                        'text-outline-width': 2,
                    },
                },
                {
                    // Label only nodes with >= 3 edges to reduce visual noise.
                    selector: 'node[edge_count >= 3]',
                    style: {
                        'label': function (ele) {
                            return Dashboard.truncate(ele.data('label') || '', 30);
                        },
                    },
                },
                {
                    selector: 'node:selected',
                    style: {
                        'border-width': 3,
                        'border-color': '#fbbf24',
                    },
                },
                {
                    selector: 'node.faded',
                    style: { 'opacity': 0.15 },
                },
                {
                    selector: 'node.searchHit',
                    style: {
                        'border-width': 2,
                        'border-color': '#fbbf24',
                    },
                },
                {
                    selector: 'edge',
                    style: {
                        'curve-style': 'bezier',
                        'line-color': 'data(color)',
                        'opacity': 0.5,
                        'width': 'mapData(weight, 0, 1, 1, 4)',
                        'target-arrow-color': 'data(color)',
                        'target-arrow-shape': 'triangle',
                        'arrow-scale': 0.7,
                    },
                },
                {
                    // F065: inferred edges drawn lighter + dashed.
                    selector: 'edge[extraction_method = "inferred"]',
                    style: {
                        'opacity': 0.3,
                        'line-style': 'dashed',
                    },
                },
                {
                    selector: 'edge.faded',
                    style: { 'opacity': 0.05 },
                },
                {
                    // Phase 3: when clusterMode is on, nodes get a thick border
                    // tinted by cluster hue (set via .data('clusterColor')).
                    // Type color stays as fill so people still know fact vs decision.
                    selector: 'node.clustered',
                    style: {
                        'border-width': 4,
                        'border-color': 'data(clusterColor)',
                    },
                },
            ],
        });
    }

    function runLayout() {
        // cose is bundled with Cytoscape, no plugin needed. Tuned for our
        // hub-and-spoke topology: stronger repulsion, looser edge length so
        // chunk constellations don't crush together against their episode.
        cy.layout({
            name: 'cose',
            animate: false,
            randomize: true,
            componentSpacing: 80,
            nodeRepulsion: function () { return 80000; },
            nodeOverlap: 12,
            idealEdgeLength: function () { return 80; },
            edgeElasticity: function () { return 100; },
            nestingFactor: 1.2,
            gravity: 80,
            numIter: 1500,
            initialTemp: 200,
            coolingFactor: 0.95,
            minTemp: 1.0,
        }).run();
    }

    // ───────────────────────────────────────────────────────────────────
    // Filters
    // ───────────────────────────────────────────────────────────────────

    function applyFilters() {
        // Hide nodes failing the type or min-edges filter; hide edges
        // referencing any hidden node. Cytoscape's .filter() returns a
        // collection — we use .style() to flip display rather than rebuild
        // the graph, which preserves layout positions.
        cy.batch(function () {
            cy.nodes().forEach(function (n) {
                var visible = filters.types[n.data('type')] !== false
                    && n.data('edge_count') >= filters.minEdges;
                n.style('display', visible ? 'element' : 'none');
            });
            // Edges follow their endpoints automatically when source/target
            // is display:none, so no per-edge work required by us.
        });
        // Codex P2 round 3: if cluster mode is on, filters may have split
        // the visible subgraph (e.g. by hiding a bridge node). Recompute
        // and re-apply so colors match what the user sees.
        if (clusterMode) applyClusterColors();
    }

    function applySearchHighlight() {
        cy.batch(function () {
            var q = filters.search;
            if (!q) {
                cy.elements().removeClass('faded searchHit');
                return;
            }
            // Codex P2 (2026-05-26): two-pass to avoid order-dependence.
            // The prior loop toggled edge fade per-node, so an edge could
            // be faded by its non-matching endpoint AFTER being un-faded
            // by its matching endpoint, depending on iteration order.
            // Now: pass 1 classifies nodes; pass 2 decides each edge
            // exactly once based on whether either endpoint matches.
            var matchSet = {};
            cy.nodes().forEach(function (n) {
                var label = (n.data('label') || '').toLowerCase();
                if (label.indexOf(q) >= 0) {
                    n.removeClass('faded');
                    n.addClass('searchHit');
                    matchSet[n.id()] = true;
                } else {
                    n.removeClass('searchHit');
                    n.addClass('faded');
                }
            });
            cy.edges().forEach(function (e) {
                if (matchSet[e.source().id()] || matchSet[e.target().id()]) {
                    e.removeClass('faded');
                } else {
                    e.addClass('faded');
                }
            });
        });
    }

    // ───────────────────────────────────────────────────────────────────
    // Phase 3: clusters
    // ───────────────────────────────────────────────────────────────────

    function computeClusters() {
        // Codex P2 round 3 (2026-05-26): derive components from the
        // VISIBLE subgraph, not the full graph. Filters can hide bridge
        // nodes — when they do, the visible graph splits into more
        // components, and cluster colors must reflect that. Computing
        // against cy.elements() once at render time meant filtered-out
        // bridges silently misreported connectivity.
        //
        // Clear stale colors first so a node that became hidden (and
        // thus dropped from the component graph) doesn't keep a color
        // that no longer matches its visible neighbors.
        cy.nodes().forEach(function (n) { n.removeData('clusterColor'); });

        var visibleEls = cy.elements().filter(function (e) { return e.visible(); });
        var components = visibleEls.components();
        var hues = [
            '#a78bfa', '#34d399', '#60a5fa', '#fb923c', '#fbbf24',
            '#f87171', '#06b6d4', '#22d3ee', '#a3e635', '#ec4899',
            '#8b5cf6', '#10b981',
        ];
        components.forEach(function (comp, idx) {
            var hue = hues[idx % hues.length];
            comp.nodes().forEach(function (n) { n.data('clusterColor', hue); });
        });
    }

    function applyClusterColors() {
        cy.batch(function () {
            if (clusterMode) {
                // Recompute against the current visible subgraph so colors
                // match what the user actually sees post-filter.
                computeClusters();
                cy.nodes().addClass('clustered');
            } else {
                cy.nodes().removeClass('clustered');
            }
        });
    }

    // ───────────────────────────────────────────────────────────────────
    // Detail panel (unchanged behavior from D3 version)
    // ───────────────────────────────────────────────────────────────────

    cy.on('tap', 'node', function (evt) {
        var node = evt.target;
        showNodeDetail({
            id: node.id(),
            type: node.data('type'),
            label: node.data('label'),
            category: node.data('category'),
            edge_count: node.data('edge_count'),
            created_at: node.data('created_at'),
        });
    });
    // Tapping background closes the detail panel.
    cy.on('tap', function (evt) {
        if (evt.target === cy) {
            detailEl.classList.remove('open');
        }
    });

    function showNodeDetail(node) {
        detailEl.classList.add('open');
        detailEl.innerHTML =
            '<button class="detail-close" id="close-detail">&times;</button>' +
            '<span class="badge badge-' + node.type + '">' + node.type + '</span>' +
            (node.category
                ? ' <span class="badge" style="background:rgba(107,107,138,0.15);color:var(--muted)">' +
                  escapeHtml(node.category) + '</span>'
                : '') +
            '<h3 style="margin-top:8px">' + escapeHtml(node.label || node.id) + '</h3>' +
            '<div class="detail-grid" style="margin-top:12px">' +
                '<div class="detail-label">ID</div><div class="detail-value mono">' + escapeHtml(node.id.slice(0, 8)) + '...</div>' +
                '<div class="detail-label">Edges</div><div class="detail-value">' + (node.edge_count || 0) + '</div>' +
                '<div class="detail-label">Created</div><div class="detail-value">' + Dashboard.formatDate(node.created_at) + '</div>' +
            '</div>' +
            '<div id="detail-extra" class="mt-16 text-muted" style="font-size:12px">Loading details...</div>';

        document.getElementById('close-detail').addEventListener('click', function () {
            detailEl.classList.remove('open');
        });

        fetchNodeDetail(node).then(function (detail) {
            var extraEl = document.getElementById('detail-extra');
            if (extraEl && detail) {
                extraEl.innerHTML = formatNodeDetail(node.type, detail);
            }
        }).catch(function () {
            var extraEl = document.getElementById('detail-extra');
            if (extraEl) extraEl.textContent = 'Could not load full details.';
        });
    }

    async function fetchNodeDetail(node) {
        try {
            if (node.type === 'decision') {
                return await Dashboard.apiGet('/decisions/' + node.id);
            } else if (node.type === 'fact') {
                var res = await Dashboard.apiGet(
                    '/facts?q=' + encodeURIComponent((node.label || '').slice(0, 60)) + '&limit=1'
                );
                return res.facts && res.facts.length > 0 ? res.facts[0] : null;
            }
            return null;
        } catch (e) {
            return null;
        }
    }

    function formatNodeDetail(type, detail) {
        if (!detail) return '';
        var html = '';
        if (type === 'decision' && detail.decision) {
            var d = detail.decision;
            html += '<div class="detail-grid">';
            if (d.category) html += '<div class="detail-label">Category</div><div class="detail-value">' + escapeHtml(d.category) + '</div>';
            if (d.stakes) html += '<div class="detail-label">Stakes</div><div class="detail-value">' + escapeHtml(d.stakes) + '</div>';
            if (d.confidence != null) html += '<div class="detail-label">Confidence</div><div class="detail-value">' + (d.confidence * 100).toFixed(0) + '%</div>';
            if (d.outcome) html += '<div class="detail-label">Outcome</div><div class="detail-value"><span class="badge ' + Dashboard.outcomeBadge(d.outcome) + '">' + d.outcome + '</span></div>';
            html += '</div>';
            if (d.context) html += '<div style="margin-top:12px;font-size:12px;color:var(--text)">' + escapeHtml(d.context) + '</div>';
        } else if (type === 'fact' && detail) {
            html += '<div class="detail-grid">';
            if (detail.category) html += '<div class="detail-label">Category</div><div class="detail-value">' + escapeHtml(detail.category) + '</div>';
            if (detail.confidence != null) html += '<div class="detail-label">Confidence</div><div class="detail-value">' + (detail.confidence * 100).toFixed(0) + '%</div>';
            if (detail.subject) html += '<div class="detail-label">Subject</div><div class="detail-value">' + escapeHtml(detail.subject) + '</div>';
            html += '</div>';
            if (detail.content) html += '<div style="margin-top:12px;font-size:12px;color:var(--text)">' + escapeHtml(detail.content) + '</div>';
        }
        return html;
    }

    // ───────────────────────────────────────────────────────────────────
    // Edge relation colors (mirror from prior D3 implementation)
    // ───────────────────────────────────────────────────────────────────

    function relationColor(relation) {
        var colors = {
            related_to: '#7c6af7',
            extracted_from: '#60a5fa',
            supports: '#34d399',
            informed_by: '#a78bfa',
            evidence_for: '#fbbf24',
            contradicts: '#f87171',
            supersedes: '#fb923c',
            caused_by: '#e2e2f0',
            discussed_in: '#6b6b8a',
            // F070 chunk-graph relations
            part_of: '#22d3ee',
            summarized_by: '#06b6d4',
        };
        return colors[relation] || '#6b6b8a';
    }
});
