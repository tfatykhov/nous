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
