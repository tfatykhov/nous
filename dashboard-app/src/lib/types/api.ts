// Response shapes for /dashboard/* and /status. One interface per endpoint.
// Added incrementally by each view migration task.

// ── /dashboard/cache (F036.1) ──────────────────────────────────────────────
export interface CacheSummary {
  total_calls: number;
  total_input_tokens: number;
  total_cache_read: number;
  total_cache_created: number;
  overall_hit_rate: number;
  total_breaks: number;
  break_rate: number;
  tokens_lost_to_breaks: number;
}

export interface CacheSession {
  session_id: string;
  calls: number;
  input_tokens: number;
  cache_read: number;
  cache_created: number;
  hit_rate: number;
  breaks: number;
}

export interface CacheTimelineEntry {
  timestamp: string;
  session_id: string;
  turn: number | null;
  model: string;
  input_tokens: number;
  cache_read: number;
  cache_created: number;
  hit_rate: number;
  cache_break: boolean;
  break_components: string[];
}

export interface CacheData {
  summary: CacheSummary;
  /** Component name → break count. Convert to array for charting. */
  break_components: Record<string, number>;
  sessions: CacheSession[];
  timeline: CacheTimelineEntry[];
}

// ── /dashboard/heartbeat (F034) ────────────────────────────────────────────

/** One entry from registry.get_status(), merged with its name key by rest.py. */
export interface HeartbeatCheck {
  name: string;
  active: boolean;
  /** Interval in seconds (registry field name is `interval`, not `interval_seconds`). */
  interval: number;
  last_run: string | null;
  consecutive_failures: number;
  max_failures: number;
  /** True when consecutive_failures >= max_failures. */
  circuit_breaker_open: boolean;
  permanent: boolean;
  urgent_override: boolean;
}

export interface HeartbeatStatus {
  enabled: boolean;
  is_running: boolean;
  last_tick: string | null;
  tick_interval: number;
}

export interface HeartbeatBudget {
  used: number;
  limit: number;
  percentage: number;
}

export interface HeartbeatQuietHours {
  start: number;
  end: number;
  active: boolean;
}

/** Flat finding from findings_timeline (sourced from heartbeat_tick event JSONB). */
export interface HeartbeatFinding {
  source: string | null;
  summary: string | null;
  urgency: string | null;
  check_name: string | null;
  timestamp: string;
}

/** Urgency breakdown per day — from findings_by_day. */
export interface HeartbeatFindingsByDayEntry {
  date: string;
  findings_count: number;
  by_urgency: { high: number; normal: number; low: number };
}

/** Totals aggregated from tick events. Returned as `totals` key. */
export interface HeartbeatTotals {
  total: number;
  by_source: Record<string, number>;
  by_urgency: Record<string, number>;
}

/** Cognitive session entry from heartbeat_triage events. */
export interface HeartbeatCognitiveSession {
  timestamp: string;
  session_id: string | null;
  findings_count: number;
  tokens_used: number;
  response_summary: string;
}

/** Tracked finding from FindingStore.to_list(). */
export interface HeartbeatTrackedFinding {
  fingerprint: string;
  check_name: string;
  source: string;
  summary: string;
  urgency: string;
  state: string;
  first_seen: string | null;
  last_seen: string | null;
  seen_count: number;
  escalated: boolean;
  outcome: string | null;
  reopen_count: number;
}

export interface HeartbeatFindingLifecycle {
  stats: {
    total: number;
    by_state: Record<string, number>;
    by_check: Record<string, number>;
  };
  findings: HeartbeatTrackedFinding[];
  escalation_policy: {
    low_to_normal_hours: number;
    normal_to_high_hours: number;
    high_realert_hours: number;
    accumulation_threshold: number;
  };
}

export interface HeartbeatTuningReport {
  adjustments: number;
  skipped_checks: string[];
  timestamp: string | null;
  summary: string;
}

export interface HeartbeatData {
  /** DB: last 100 heartbeat_tick events in window. */
  recent_ticks: Array<{ created_at: string; data: Record<string, unknown> }>;
  /** DB: last 20 heartbeat_triage events (cognitive sessions). */
  cognitive_sessions: HeartbeatCognitiveSession[];
  /** DB: aggregated findings totals across tick events in window. */
  totals: HeartbeatTotals;
  /** DB: per-day urgency breakdown for last 7 days. */
  findings_by_day: HeartbeatFindingsByDayEntry[];
  /** DB: flat finding list from tick events, capped at 50. */
  findings_timeline: HeartbeatFinding[];
  /** In-memory: heartbeat runner + settings state. */
  status: HeartbeatStatus;
  /** In-memory: one entry per registered check. */
  checks: HeartbeatCheck[];
  /** In-memory: token budget for the current day. */
  budget: HeartbeatBudget;
  /** In-memory: quiet-hours config + current active state. */
  quiet_hours: HeartbeatQuietHours;
  /** In-memory: FindingStore lifecycle stats + list. null when store not initialised. */
  finding_lifecycle: HeartbeatFindingLifecycle | null;
  /** In-memory: self-tuning status. */
  tuning: {
    enabled: boolean;
    last_report: HeartbeatTuningReport | null;
  };
}

// ── /dashboard/observability (F035) ───────────────────────────────────────

/** Per-handler stats from EventBusStats.to_dict(). */
export interface ObsHandlerStat {
  invocations: number;
  successes: number;
  errors: number;
  error_rate: number;
  avg_duration_ms: number;
  /** Seconds since last invocation — absent when never invoked. */
  last_invoked_ago_s?: number;
}

export interface ObsEventBus {
  total_processed: number;
  total_dropped: number;
  queue_depth: number;
  uptime_seconds: number;
  /** event_type → count */
  event_counts: Record<string, number>;
  /** handler fully-qualified name → stats */
  handlers: Record<string, ObsHandlerStat>;
}

export interface ObsTrace {
  trace_id: string;
  root_type: string;
  timestamp: string | null;
  event_count: number;
  has_modifications: boolean;
}

export interface ObsModification {
  event_id: string;
  type: string;
  trace_id: string | null;
  modifies: string | null;
  timestamp: string | null;
}

export interface ObsAnomaly {
  metric: string;
  severity: string;
  current: number;
  direction: string;
  mean: number;
  stddev: number;
}

export interface ObsDriftMetrics {
  fact_count?: number;
  fact_count_delta?: number;
  episode_count?: number;
  active_censor_count?: number;
  procedure_count?: number;
  handler_error_rate?: number;
}

export interface ObsDrift {
  timestamp: string;
  metrics: ObsDriftMetrics;
  anomalies: ObsAnomaly[];
}

/** {t: ISO string, v: number} data point for trend sparklines. */
export interface ObsTrendPoint {
  t: string;
  v: number;
}

export interface ObsContextLogEntry {
  id: string;
  session_id: string;
  turn_number: number | null;
  timestamp: string;
  call_type: string;
  model: string;
  frame_id: string;
  trace_id: string | null;
  /** section name → estimated token count */
  token_breakdown: Record<string, number>;
  total_tokens_est: number;
  context_window_size: number;
  utilization_pct: number;
  sections_present: string[];
  tools_count: number;
  tool_names: string[];
  messages_count: number;
  loaded_facts: number;
  loaded_decisions: number;
  loaded_procedures?: number;
  loaded_episodes?: number;
  input_tokens_actual: number | null;
  output_tokens: number | null;
  /** cache_read_tokens from ContextLogEntry.to_dict() key is "cache_read" */
  cache_read: number | null;
  duration_ms: number | null;
  stop_reason: string | null;
}

export interface ObservabilityData {
  event_bus: ObsEventBus;
  recent_traces: ObsTrace[];
  recent_modifications: ObsModification[];
  drift: ObsDrift | null;
  /** metric key → array of {t, v} points */
  drift_trends: Record<string, ObsTrendPoint[]>;
  context_log: ObsContextLogEntry[];
}

// ── /dashboard/subtasks (F061) ─────────────────────────────────────────────

export interface SubtaskTotals {
  total_terminal: number;
  by_outcome: Record<string, number>;
  empty_rate: number;
  retry_rate: number;
}

export interface SubtaskTokenEntry {
  mean_total_tokens: number;
  mean_tool_calls: number;
  n: number;
}

export interface SubtaskTopFailing {
  task_prefix: string;
  total: number;
  failures: number;
  failure_rate: number;
}

export interface SubtaskRecentEntry {
  id: string;
  task: string;
  final_outcome: string | null;
  attempts: number;
  tokens_in: number;
  tokens_out: number;
  tool_calls_made: number;
  completed_at: string | null;
  dag_node_id: string | null;
}

export interface SubtaskDailyTrendEntry {
  date: string;
  by_outcome: Record<string, number>;
}

export interface SubtasksData {
  window_hours: number;
  totals: SubtaskTotals;
  /** outcome → { mean_total_tokens, mean_tool_calls, n } */
  tokens_by_outcome: Record<string, SubtaskTokenEntry>;
  top_failing_tasks: SubtaskTopFailing[];
  /** outcome → count (DAG-attached subtasks only) */
  dag_correlation: Record<string, number>;
  recent_outcomes: SubtaskRecentEntry[];
  daily_trend: SubtaskDailyTrendEntry[];
}

// ── /dashboard/ledger (F032) ──────────────────────────────────────────────

/** One tool invocation recorded in the execution ledger. */
export interface LedgerAction {
  turn: number;
  tool_name: string;
  /** Redacted key args — string values only. */
  key_args: Record<string, string>;
  /** 'success' | 'blocked' | 'error' | 'timeout' */
  status: string;
  timestamp: string;
  result_summary: string | null;
  /** 'none' | 'write' | 'external' | 'irreversible' */
  side_effect_type: string | null;
}

/** Per-session ledger entry from /dashboard/ledger. */
export interface LedgerSession {
  session_id: string;
  current_turn: number;
  total_actions: number;
  success_actions: number;
  blocked_actions: number;
  error_actions: number;
  timeout_actions: number;
  summary: string;
  actions: LedgerAction[];
  actions_truncated: boolean;
}

export interface LedgerData {
  enabled: {
    ledger: boolean;
    claim_verification: boolean;
    action_gating: boolean;
  };
  modes: {
    claim_verification: string;
    action_gating: string;
  };
  sessions: LedgerSession[];
}

// ── GET /status?dashboard=true (Overview view) ────────────────────────────

/** Memory counts from /status. */
export interface StatusMemory {
  active_conversations: number;
  active_censors: number;
  total_decisions: number;
  total_facts: number;
  total_episodes: number;
  total_procedures: number;
  total_chunks: number;
}

/** Calibration summary from /status. */
export interface StatusCalibration {
  brier_score: number | null;
  accuracy: number | null;
  total_decisions: number;
  reviewed_decisions: number;
}

/** 7-day delta for one entity type. */
export interface StatusDeltaEntry {
  total: number;
  last_7_days: number;
}

/** One timeseries point: {date: ISO string, count: number}. */
export interface StatusTimeseriesPoint {
  date: string;
  count: number;
}

/** dashboard sub-object from get_dashboard_stats(). */
export interface StatusDashboard {
  /** 7-day deltas for facts/episodes/decisions/procedures. */
  deltas: {
    facts: StatusDeltaEntry;
    episodes: StatusDeltaEntry;
    decisions: StatusDeltaEntry;
    procedures: StatusDeltaEntry;
  };
  distributions: {
    /** category → count */
    fact_categories: Record<string, number>;
    /** outcome → count (success/partial/failure/pending) */
    decision_outcomes: Record<string, number>;
    /** category → count */
    decision_categories: Record<string, number>;
    /** relation → count */
    edge_relations: Record<string, number>;
  };
  /** 30-day daily timeseries for facts/episodes/decisions/procedures. */
  timeseries: {
    facts: StatusTimeseriesPoint[];
    episodes: StatusTimeseriesPoint[];
    decisions: StatusTimeseriesPoint[];
    procedures: StatusTimeseriesPoint[];
  };
  /** edges / nodes ratio; null when no edges exist. */
  graph_density: number | null;
}

/** Execution integrity block from /status. */
export interface StatusIntegrity {
  enabled: { ledger: boolean; claim_verification: boolean; action_gating: boolean };
  modes: { claim_verification: string; action_gating: string };
  active_ledgers: number;
  sessions: Record<string, {
    total_actions: number;
    blocked_actions: number;
    current_turn: number;
    summary: string;
  }>;
  pending_corrections: Record<string, number>;
}

/** Full response from GET /status?dashboard=true. */
export interface StatusData {
  agent_id: string;
  agent_name: string;
  model: string;
  memory: StatusMemory;
  calibration: StatusCalibration;
  execution_integrity: StatusIntegrity;
  /** Only present when ?dashboard=true. */
  dashboard: StatusDashboard;
}

// ── /dashboard/dag (F038) ─────────────────────────────────────────────────

/** A node inside an active DAG (nested under DagActiveDag.nodes). */
export interface DagActiveNode {
  id: string;
  name: string;
  description: string;
  node_type: string;
  wave: number;
  status: string;
  result: string;
  error: string;
  tokens_used: number;
  started_at: string | null;
  completed_at: string | null;
}

/** An edge inside an active DAG (nested under DagActiveDag.edges). */
export interface DagActiveEdge {
  id: string;
  from_node_id: string;
  to_node_id: string;
  edge_type: string | null;
}

/** One active (pending/running) DAG returned by /dashboard/dag. */
export interface DagActiveDag {
  id: string;
  name: string;
  description: string;
  status: string;
  source: string;
  created_at: string | null;
  started_at: string | null;
  token_budget: number;
  tokens_consumed: number;
  nodes: DagActiveNode[];
  edges: DagActiveEdge[];
}

/** One recently completed/failed/cancelled DAG returned by /dashboard/dag. */
export interface DagRecentDag {
  id: string;
  name: string;
  status: string;
  source: string;
  created_at: string | null;
  completed_at: string | null;
  token_budget: number;
  tokens_consumed: number;
  result_summary: string | null;
  postmortem: string | null;
  node_count: number;
  completed_count: number;
}

export interface DagStats {
  active_count: number;
  nodes_completed_24h: number;
  success_rate: number;
  avg_completion_seconds: number;
}

export interface DagDashboardData {
  active_dags: DagActiveDag[];
  recent_dags: DagRecentDag[];
  stats: DagStats;
}

// ── /dashboard/graph (F022/F040/F067/F070) ───────────────────────────────

/** One node from get_graph_data(). */
export interface GraphNodeData {
  id: string;
  /** 'decision' | 'fact' | 'episode' | 'procedure' | 'chunk' */
  type: string;
  label: string;
  category: string | null;
}

/** One edge from get_graph_data(). */
export interface GraphEdgeData {
  id: string;
  source: string;
  target: string;
  source_type: string;
  target_type: string;
  relation: string;
  weight: number;
  auto_linked: boolean;
  /** 'deterministic' | 'heuristic' | 'inferred' */
  extraction_method: string | null;
}

/** Stats block from get_graph_data(). */
export interface GraphStats {
  total_edges: number;
  displayed_edges: number;
  node_count: number;
  /** key = 'decisions' | 'facts' | 'episodes' | 'procedures' | 'chunks' */
  orphan_counts: Record<string, number>;
}

/** Full response from GET /dashboard/graph?limit=500. */
export interface GraphData {
  nodes: GraphNodeData[];
  edges: GraphEdgeData[];
  stats: GraphStats;
}
