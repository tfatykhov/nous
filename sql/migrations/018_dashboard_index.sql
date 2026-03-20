-- F021: Dashboard index for graph edge timeline queries
CREATE INDEX IF NOT EXISTS idx_graph_edges_created ON brain.graph_edges(created_at);
