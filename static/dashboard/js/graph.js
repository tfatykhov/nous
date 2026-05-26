/**
 * Nous Dashboard — Knowledge Graph View
 *
 * D3.js force-directed graph visualization of the knowledge graph.
 * Fetches from GET /dashboard/graph?limit=500
 */

/* global Dashboard, d3, escapeHtml, el */

Dashboard.registerView('graph', async function(container) {
    Dashboard.showLoading(container);

    var data;
    try {
        data = await Dashboard.apiGet('/dashboard/graph?limit=500');
    } catch (err) {
        Dashboard.showError(container, 'Failed to load graph data.', function() {
            Dashboard.reloadView('graph');
        });
        return;
    }

    if (!data.nodes || data.nodes.length === 0) {
        Dashboard.showEmpty(container, 'No graph edges yet. As Nous learns facts and makes decisions, connections will appear here.');
        return;
    }

    // Build the view
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
    detailEl.addEventListener('pointerdown', function(e) {
        e.stopPropagation();
    });
    detailEl.addEventListener('touchstart', function(e) {
        e.stopPropagation();
    }, { passive: true });
    var controlsEl = document.getElementById('graph-controls');

    // Compute edge_count for each node from edge data
    var edgeCounts = {};
    data.edges.forEach(function(e) {
        edgeCounts[e.source] = (edgeCounts[e.source] || 0) + 1;
        edgeCounts[e.target] = (edgeCounts[e.target] || 0) + 1;
    });
    data.nodes.forEach(function(n) {
        n.edge_count = edgeCounts[n.id] || 0;
    });

    // State
    var allNodes = data.nodes;
    var allEdges = data.edges;
    var stats = data.stats || {};
    var filters = {
        types: { fact: true, episode: true, decision: true, procedure: true, chunk: true },
        minEdges: 0,
        search: ''
    };

    // Build controls
    buildControls(controlsEl, filters);
    renderStats(statsEl, stats);

    // Create graph
    renderGraph(graphEl, allNodes, allEdges, filters, detailEl);

    function buildControls(el, filters) {
        // Search
        var searchInput = document.createElement('input');
        searchInput.type = 'text';
        searchInput.className = 'search-input';
        searchInput.placeholder = 'Search nodes...';
        searchInput.style.maxWidth = '200px';
        searchInput.addEventListener('input', function() {
            filters.search = this.value.toLowerCase();
            updateHighlights();
        });
        el.appendChild(searchInput);

        // Type filters
        var checksDiv = document.createElement('div');
        checksDiv.className = 'filter-checks';
        checksDiv.style.background = 'rgba(17,17,24,0.9)';
        checksDiv.style.padding = '8px 12px';
        checksDiv.style.borderRadius = '8px';
        checksDiv.style.border = '1px solid var(--border)';

        var types = ['fact', 'episode', 'decision', 'procedure', 'chunk'];
        types.forEach(function(type) {
            var label = document.createElement('label');
            label.className = 'filter-check';
            var cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.checked = true;
            cb.addEventListener('change', function() {
                filters.types[type] = this.checked;
                rebuildGraph();
            });
            label.appendChild(cb);
            var dot = document.createElement('span');
            dot.style.cssText = 'width:8px;height:8px;border-radius:50%;background:' + Dashboard.typeColor(type);
            label.appendChild(dot);
            label.appendChild(document.createTextNode(' ' + type.charAt(0).toUpperCase() + type.slice(1)));
            checksDiv.appendChild(label);
        });
        el.appendChild(checksDiv);

        // Min edges slider
        var sliderDiv = document.createElement('div');
        sliderDiv.style.cssText = 'background:rgba(17,17,24,0.9);padding:8px 12px;border-radius:8px;border:1px solid var(--border);font-size:12px;color:var(--muted);';
        sliderDiv.innerHTML = 'Min edges: <span id="min-edge-val">0</span>';
        var slider = document.createElement('input');
        slider.type = 'range';
        slider.className = 'range-slider';
        slider.min = '0';
        slider.max = '10';
        slider.value = '0';
        slider.addEventListener('input', function() {
            filters.minEdges = parseInt(this.value);
            document.getElementById('min-edge-val').textContent = this.value;
            rebuildGraph();
        });
        sliderDiv.appendChild(slider);
        el.appendChild(sliderDiv);
    }

    function renderStats(el, stats) {
        var orphanTotal = 0;
        if (stats.orphan_counts) {
            Object.keys(stats.orphan_counts).forEach(function(k) { orphanTotal += stats.orphan_counts[k]; });
        }
        el.innerHTML =
            '<div class="stat-item"><div class="stat-num">' + (stats.node_count || 0) + '</div><div>Nodes</div></div>' +
            '<div class="stat-item"><div class="stat-num">' + (stats.total_edges || 0) + '</div><div>Edges</div></div>' +
            '<div class="stat-item"><div class="stat-num">' + (stats.displayed_edges || 0) + '</div><div>Shown</div></div>' +
            '<div class="stat-item"><div class="stat-num">' + orphanTotal + '</div><div>Orphans</div></div>';
    }

    var simulation = null;
    var svg = null;
    var nodeGroup, linkGroup;

    function rebuildGraph() {
        if (simulation) simulation.stop();
        d3.select(graphEl).select('svg').remove();
        renderGraph(graphEl, allNodes, allEdges, filters, detailEl);
    }

    function renderGraph(containerEl, nodes, edges, filters, detailPanel) {
        // Filter nodes
        var filteredNodes = nodes.filter(function(n) {
            if (!filters.types[n.type]) return false;
            if (n.edge_count < filters.minEdges) return false;
            return true;
        });

        var nodeIds = new Set(filteredNodes.map(function(n) { return n.id; }));

        // Filter edges to only include visible nodes
        var filteredEdges = edges.filter(function(e) {
            return nodeIds.has(e.source) && nodeIds.has(e.target);
        });

        // Node map for lookups
        var nodeMap = {};
        filteredNodes.forEach(function(n) { nodeMap[n.id] = n; });

        var width = containerEl.clientWidth;
        var height = containerEl.clientHeight - 4;  // account for border

        svg = d3.select(containerEl)
            .append('svg')
            .attr('width', width)
            .attr('height', height);

        // Zoom
        var g = svg.append('g');
        var zoom = d3.zoom()
            .scaleExtent([0.1, 5])
            .on('zoom', function(event) {
                g.attr('transform', event.transform);
            });
        svg.call(zoom);

        // Force simulation
        simulation = d3.forceSimulation(filteredNodes)
            .force('link', d3.forceLink(filteredEdges).id(function(d) { return d.id; }).distance(80))
            .force('charge', d3.forceManyBody().strength(-120))
            .force('center', d3.forceCenter(width / 2, height / 2))
            .force('collision', d3.forceCollide().radius(function(d) { return nodeRadius(d) + 4; }))
            .alphaDecay(0.02);

        Dashboard._graphSimulation = simulation;

        // Links
        linkGroup = g.append('g').attr('class', 'links');
        var links = linkGroup.selectAll('line')
            .data(filteredEdges)
            .enter().append('line')
            .attr('stroke', function(d) { return relationColor(d.relation); })
            .attr('stroke-width', function(d) { return Math.max(1, (d.weight || 0.5) * 2); })
            .attr('stroke-opacity', function(d) {
                // F065: inferred edges drawn lighter than heuristic/deterministic.
                return d.extraction_method === 'inferred' ? 0.3 : 0.5;
            })
            .attr('stroke-dasharray', function(d) {
                // Dash by either low weight OR inferred provenance.
                if (d.extraction_method === 'inferred') return '2,3';
                return (d.weight || 0.5) < 0.3 ? '4,4' : 'none';
            });

        // Link hover titles
        links.append('title')
            .text(function(d) {
                var prov = d.extraction_method ? ' · ' + d.extraction_method : '';
                return d.relation + ' (w=' + (d.weight || 0).toFixed(2) + ')' + prov;
            });

        // Nodes
        nodeGroup = g.append('g').attr('class', 'nodes');
        var nodeEls = nodeGroup.selectAll('circle')
            .data(filteredNodes)
            .enter().append('circle')
            .attr('r', function(d) { return nodeRadius(d); })
            .attr('fill', function(d) { return Dashboard.typeColor(d.type); })
            .attr('stroke', 'rgba(0,0,0,0.3)')
            .attr('stroke-width', 1)
            .style('cursor', 'pointer')
            .call(d3.drag()
                .on('start', dragStart)
                .on('drag', dragging)
                .on('end', dragEnd))
            .on('click', function(event, d) {
                showNodeDetail(d, detailPanel);
            });

        nodeEls.append('title')
            .text(function(d) { return d.label || d.id; });

        // Labels for high-connectivity nodes
        var labelNodes = filteredNodes.filter(function(n) { return n.edge_count >= 3; });
        g.append('g').attr('class', 'labels')
            .selectAll('text')
            .data(labelNodes)
            .enter().append('text')
            .text(function(d) { return Dashboard.truncate(d.label || '', 30); })
            .attr('font-size', '9px')
            .attr('fill', '#6b6b8a')
            .attr('text-anchor', 'middle')
            .attr('dy', function(d) { return nodeRadius(d) + 12; })
            .style('pointer-events', 'none');

        // Tick
        simulation.on('tick', function() {
            links
                .attr('x1', function(d) { return d.source.x; })
                .attr('y1', function(d) { return d.source.y; })
                .attr('x2', function(d) { return d.target.x; })
                .attr('y2', function(d) { return d.target.y; });

            nodeEls
                .attr('cx', function(d) { return d.x; })
                .attr('cy', function(d) { return d.y; });

            g.selectAll('.labels text')
                .attr('x', function(d) { return d.x; })
                .attr('y', function(d) { return d.y; });
        });

        function dragStart(event, d) {
            if (!event.active) simulation.alphaTarget(0.3).restart();
            d.fx = d.x;
            d.fy = d.y;
        }

        function dragging(event, d) {
            d.fx = event.x;
            d.fy = event.y;
        }

        function dragEnd(event, d) {
            if (!event.active) simulation.alphaTarget(0);
            d.fx = null;
            d.fy = null;
        }
    }

    function nodeRadius(d) {
        return Math.max(4, Math.min(16, 3 + (d.edge_count || 0) * 1.2));
    }

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
            // F070: chunk-graph relations
            part_of: '#22d3ee',
            summarized_by: '#06b6d4'
        };
        return colors[relation] || '#6b6b8a';
    }

    function showNodeDetail(node, panel) {
        panel.classList.add('open');
        panel.innerHTML =
            '<button class="detail-close" id="close-detail">&times;</button>' +
            '<span class="badge badge-' + node.type + '">' + node.type + '</span>' +
            (node.category ? ' <span class="badge" style="background:rgba(107,107,138,0.15);color:var(--muted)">' + escapeHtml(node.category) + '</span>' : '') +
            '<h3 style="margin-top:8px">' + escapeHtml(node.label || node.id) + '</h3>' +
            '<div class="detail-grid" style="margin-top:12px">' +
            '<div class="detail-label">ID</div><div class="detail-value mono">' + escapeHtml(node.id.slice(0, 8)) + '...</div>' +
            '<div class="detail-label">Edges</div><div class="detail-value">' + (node.edge_count || 0) + '</div>' +
            '<div class="detail-label">Created</div><div class="detail-value">' + Dashboard.formatDate(node.created_at) + '</div>' +
            '</div>' +
            '<div id="detail-extra" class="mt-16 text-muted" style="font-size:12px">Loading details...</div>';

        document.getElementById('close-detail').addEventListener('click', function() {
            panel.classList.remove('open');
        });

        // Fetch full details from appropriate endpoint
        fetchNodeDetail(node).then(function(detail) {
            var extraEl = document.getElementById('detail-extra');
            if (extraEl && detail) {
                extraEl.innerHTML = formatNodeDetail(node.type, detail);
            }
        }).catch(function() {
            var extraEl = document.getElementById('detail-extra');
            if (extraEl) extraEl.textContent = 'Could not load full details.';
        });
    }

    async function fetchNodeDetail(node) {
        try {
            if (node.type === 'decision') {
                return await Dashboard.apiGet('/decisions/' + node.id);
            } else if (node.type === 'fact') {
                var res = await Dashboard.apiGet('/facts?q=' + encodeURIComponent((node.label || '').slice(0, 60)) + '&limit=1');
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

    function updateHighlights() {
        if (!svg) return;
        var search = filters.search;
        svg.selectAll('.nodes circle')
            .attr('opacity', function(d) {
                if (!search) return 1;
                return (d.label || '').toLowerCase().includes(search) ? 1 : 0.15;
            })
            .attr('stroke', function(d) {
                if (!search) return 'rgba(0,0,0,0.3)';
                return (d.label || '').toLowerCase().includes(search) ? '#fbbf24' : 'rgba(0,0,0,0.3)';
            })
            .attr('stroke-width', function(d) {
                if (!search) return 1;
                return (d.label || '').toLowerCase().includes(search) ? 2 : 1;
            });
    }
});
