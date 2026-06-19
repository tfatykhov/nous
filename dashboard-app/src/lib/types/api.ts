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
