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
