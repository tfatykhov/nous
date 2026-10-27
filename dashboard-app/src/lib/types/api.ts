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
  /** Known-FP rule id when state is auto_closed_known_fp. */
  auto_closed_rule?: string | null;
}

export interface HeartbeatFindingLifecycle {
  stats: {
    total: number;
    by_state: Record<string, number>;
    by_check: Record<string, number>;
    /** Findings auto-closed per known-FP rule since process start. */
    auto_closed_by_rule?: Record<string, number>;
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
  /** Null when the baseline had zero variance (series was previously constant). */
  z_score?: number | null;
  /** When fact_count_delta is residualized: the raw observed value before
   *  the prune adjustment. Null for non-residualized metrics. */
  raw_current?: number | null;
  /** Name of the metric whose value was subtracted (e.g. "inactive_fact_delta").
   *  Null for non-residualized metrics. */
  residualized_by?: string | null;
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
  /** Harness Phase 3: present only on approval nodes. */
  approval?: DagApproval;
}

/** One option on an approval card. */
export interface DagApprovalOption {
  id: string;
  label: string;
  outcome: 'proceed' | 'stop';
}

/** A previous answer to the same approval (retried questions). */
export interface DagApprovalAttempt {
  answer: string;
  label: string;
  outcome: string;
  answer_source: 'companion' | 'deadline';
  /** A person's identity, or null (never the 'unattributed' placeholder). */
  answered_by: string | null;
  answered_at: string | null;
}

/** Everything an approval step shows (/dashboard/dag, spec §3.1). */
export interface DagApproval {
  question: string;
  options: DagApprovalOption[];
  default_option: string | null;
  default_label: string;
  asked_at: string | null;
  deadline: string | null;
  answer: string | null;
  answer_label: string | null;
  answer_source: 'companion' | 'deadline' | null;
  answered_by: string | null;
  answered_at: string | null;
  card_url: string | null;
  /** Why the card was not delivered (surface not linked). */
  card_error: string | null;
  /** Exactly the text the card shows: question + inputs under review. */
  card_summary: string;
  reviewing: string[];
  attempts: DagApprovalAttempt[];
}

/** An approval step waiting on a person (/dashboard/dag waiting_on_you). */
export interface DagWaiting {
  dag_id: string;
  dag_name: string;
  node_id: string;
  node_name: string;
  question: string;
  deadline: string | null;
  default_label: string;
  card_url: string | null;
  card_error: string | null;
  reviewing: string[];
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
  /** Approval steps in this DAG waiting on a person. */
  waiting: number;
  /** The orchestrator's per-process hint (last tick), or null. */
  held_reason: string | null;
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
  /** A failed DAG that stopped at an approval: who stopped it ("mixed" when
   *  a decline and a deadline default both stopped it). */
  stopped_by: 'companion' | 'deadline' | 'mixed' | null;
  stops: { node_name: string; answer_source: 'companion' | 'deadline'; answer_label: string }[];
}

export interface DagStats {
  active_count: number;
  nodes_completed_24h: number;
  success_rate: number;
  avg_completion_seconds: number;
  waiting_count: number;
}

export interface DagDashboardData {
  active_dags: DagActiveDag[];
  recent_dags: DagRecentDag[];
  waiting_on_you: DagWaiting[];
  stats: DagStats;
}

// ── /dashboard/execution (harness spec §3.2) ─────────────────────────────

export interface ExecutionModes {
  persist: boolean;
  retention_days: number;
  /** Migration 086: whether the redacted call input and output are stored. */
  detail: boolean;
  detail_retention_days: number;
  offered_set: string;
  context_policy: string;
  claim_verification: string;
  action_gating: string;
  events_persisted: boolean;
}

export interface ExecutionStats {
  calls: number;
  sends: number;
  external: number;
  repeat_sends_refused: number;
  blocked: number;
  unknown: number;
  unknown_keyed: number;
  errors: number;
  pending: number;
}

export interface ExecutionHolder {
  id: string;
  status: string;
  created_at: string | null;
  external_ref: string | null;
  session_id: string | null;
  turn: number | null;
}

export interface ExecutionRow {
  id: string;
  created_at: string | null;
  completed_at: string | null;
  dispatched_at: string | null;
  tool_name: string;
  context_kind: string;
  side_effect_type: string;
  status: string;
  refusal_code: string | null;
  result_summary: string | null;
  key_args: Record<string, unknown>;
  /** First 240 characters of the stored call input, or null when none is stored. */
  input_preview: string | null;
  has_input: boolean;
  has_output: boolean;
  idempotency_key: string | null;
  external_ref: string | null;
  session_id: string | null;
  parent_session_id: string | null;
  subtask_id: string | null;
  dag_id: string | null;
  dag_name: string | null;
  dag_node_id: string | null;
  node_name: string | null;
  turn: number | null;
  tombstone: boolean;
  held_by: ExecutionHolder | null;
}

/** GET /dashboard/execution/{id}: the stored (redacted, capped) input and output of one call. */
export interface ExecutionDetail {
  id: string;
  tool_name: string;
  status: string;
  input_text: string | null;
  output_text: string | null;
}

export interface ExecutionData {
  modes: ExecutionModes;
  stats: ExecutionStats;
  attention: ExecutionRow[];
  /** Every send in doubt — `attention` is only the newest page of them. */
  attention_total: number;
  rows: ExecutionRow[];
  next_before: string | null;
}

// ── /dashboard/harness (harness spec §3.3) ───────────────────────────────

export interface Ranked { key: string; count: number }

export interface HarnessRule {
  mode: string | null;
  first_event_at: string | null;
  by_mode: Record<string, number>;
  by_context: Ranked[];
}

export interface HarnessData {
  window: string;
  events_persisted: boolean;
  rules: {
    offered_set: HarnessRule & { by_tool: Ranked[] };
    context_policy: HarnessRule & { by_violation: Ranked[] };
    claims: {
      mode: string | null;
      first_event_at: string | null;
      evidence_since: string | null;
      by_evidence: { exact: number; plausible: number; none: number };
      by_mode: Record<string, number>;
      /** No-evidence claims per the mode each was recorded under. */
      none_by_mode: Record<string, number>;
      turns_with_claims: number;
      legacy: { events: number; violations: number };
    };
  };
  /** null = not measured that day (before the series began, or persistence off). */
  daily: { date: string; offered_set: number | null; context_policy: number | null; claims_none: number | null }[];
  patterns: {
    rule: 'offered_set' | 'context_policy' | 'claims';
    mode: string;
    context: string | null;
    tool: string | null;
    violation: string;
    count: number;
    last_seen: string | null;
    latest_session: string | null;
    snippet: string | null;
  }[];
}

// ── /dashboard/attention (harness spec §3.4) ─────────────────────────────

export interface AttentionData {
  questions_waiting: number;
  next: { dag_name: string; node_name: string; deadline: string | null; default_label: string } | null;
  sends_in_doubt: number;
  latest_in_doubt: {
    tool_name: string;
    recipients: string[];
    created_at: string | null;
    dag_name: string | null;
    tombstone: boolean;
  } | null;
  ledger_persisted: boolean;
  harness: {
    events_persisted: boolean;
    offered_set: { mode: string | null; warn_7d: number; refused_7d: number };
    context_policy: { mode: string | null; warn_7d: number; refused_7d: number };
  };
}

// ── Memory Browser endpoints (/facts /episodes /decisions /procedures /censors /chunks) ──

export interface BrowserFact {
  id: string;
  content: string;
  category: string | null;
  subject: string | null;
  confidence: number;
  active: boolean;
  tags: string[];
  superseded_by: string | null;
  actionable: boolean | null;
  event_date: string | null;
  source: string | null;
  source_episode_id: string | null;
  learned_at: string;
  created_at: string;
}

export interface FactsResponse {
  facts: BrowserFact[];
  total: number;
  limit?: number;
  offset?: number;
}

export interface BrowserEpisode {
  id: string;
  title: string | null;
  summary: string;
  outcome: string | null;
  started_at: string;
  tags: string[];
  structured_summary: {
    key_points?: string[];
    outcome_rationale?: string;
    lessons?: string[];
    [key: string]: unknown;
  } | null;
}

export interface EpisodesResponse {
  episodes: BrowserEpisode[];
  total: number;
  limit: number;
  offset: number;
}

export interface BrowserDecision {
  id: string;
  description: string;
  category: string;
  stakes: string;
  confidence: number;
  outcome: string;
  pattern: string | null;
  tags: string[];
  created_at: string;
}

/** Full decision returned by GET /decisions/{id}. */
export interface DecisionDetail extends BrowserDecision {
  context: string | null;
  reasons: Array<{ type?: string; text?: string; content?: string }>;
}

export interface DecisionsResponse {
  decisions: BrowserDecision[];
  total: number;
  limit: number;
  offset: number;
}

/** Matches ProcedureSummary returned by GET /procedures. */
export interface BrowserProcedure {
  id: string;
  name: string;
  domain: string | null;
  description: string | null;
  core_patterns: string[];
  implementation_notes: string[];
  activation_count: number;
  effectiveness: number | null;
  score: number | null;
}

export interface ProceduresResponse {
  procedures: BrowserProcedure[];
  total: number;
  limit: number;
  offset: number;
}

export interface BrowserCensor {
  id: string;
  trigger_pattern: string;
  action: string;
  reason: string;
  domain: string | null;
  provenance: string;
  activation_count: number;
  false_positive_count: number;
  active: boolean;
  trigger_action: Record<string, unknown> | null;
  action_instruction: string | null;
  unblock_pattern: string | null;
  created_at: string;
}

export interface CensorsResponse {
  censors: BrowserCensor[];
  total: number;
  limit: number;
  offset: number;
}

export interface BrowserChunk {
  id: string;
  episode_id: string;
  chunk_index: number;
  content: string;
  created_at: string;
}

export interface ChunksResponse {
  chunks: BrowserChunk[];
  total: number;
  limit: number;
  offset: number;
}

// ── /dashboard/admission (F023 Memory Admission Control) ─────────────────

export interface AdmissionConfig {
  enabled: boolean;
  shadow_mode: boolean;
  threshold: number;
  weights: {
    utility: number;
    confidence: number;
    novelty: number;
    recency: number;
    type_prior: number;
  };
}

export interface AdmissionSummary {
  total_scored: number;
  admitted: number;
  would_reject: number;
  bypassed: number;
  avg_composite_score: number;
  rejection_rate: number;
  threshold_note: string;
  _pre_migration_note: string;
}

/** One 0.05-wide bucket from the histogram. bucket e.g. "0.50-0.55". */
export interface AdmissionScoreBucket {
  bucket: string;
  count: number;
}

/** Box-plot stats for one dimension × admitted/rejected split. */
export interface AdmissionDimStats {
  min: number;
  q1: number;
  median: number;
  q3: number;
  max: number;
}

/** Per-dimension breakdown: admitted + rejected box-plot stats. */
export interface AdmissionDimEntry {
  admitted: AdmissionDimStats | Record<string, never>;
  rejected: AdmissionDimStats | Record<string, never>;
}

export interface AdmissionBySourceEntry {
  admitted: number;
  rejected: number;
  bypassed: number;
  avg_score: number | null;
}

export interface AdmissionByCategoryEntry {
  admitted: number;
  rejected: number;
  avg_score: number | null;
}

export interface AdmissionDailyTrendEntry {
  date: string;
  scored: number;
  admitted: number;
  rejected: number;
  bypassed: number;
  avg_score: number | null;
}

export interface AdmissionData {
  config: AdmissionConfig;
  summary: AdmissionSummary;
  score_distribution: AdmissionScoreBucket[];
  /** Keys: utility | confidence | novelty | recency | type_prior + "_note" string. */
  dimension_stats: Record<string, AdmissionDimEntry | string>;
  /** source name → stats */
  by_source: Record<string, AdmissionBySourceEntry>;
  /** category name → stats */
  by_category: Record<string, AdmissionByCategoryEntry>;
  daily_trend: AdmissionDailyTrendEntry[];
  /** source/reason → count */
  bypass_breakdown: Record<string, number>;
}

// ── /dashboard/admission/rejected (paginated) ─────────────────────────────

export interface AdmissionRejectedFact {
  id: string;
  /** Truncated to 200 chars. */
  content_preview: string;
  content_full: string;
  category: string | null;
  source: string | null;
  composite_score: number;
  scores: {
    utility?: number;
    confidence?: number;
    novelty?: number;
    recency?: number;
    type_prior?: number;
  };
  created_at: string | null;
}

export interface AdmissionRejectedPage {
  facts: AdmissionRejectedFact[];
  total: number;
  limit: number;
  offset: number;
}

// ── /dashboard/calibration (F021 Decision Intelligence) ──────────────────

export interface CalibrationCurvePoint {
  /** FLOOR(confidence * 10) / 10 — e.g. "0.7" */
  bucket: string;
  actual_success_rate: number;
  total: number;
  successes: number;
  avg_confidence: number;
}

export interface ConfidenceHistogramBucket {
  /** e.g. "0.7-0.8" */
  range: string;
  count: number;
}

export interface ReasonTypeStat {
  count: number;
  success_rate: number;
  successes: number;
  reviewed: number;
}

export interface BrierHistoryPoint {
  date: string;
  brier_score: number;
}

export interface DailyDecisionPoint {
  date: string;
  count: number;
  successes: number;
  reviewed: number;
}

export interface CalibrationData {
  calibration_curve: CalibrationCurvePoint[];
  confidence_histogram: ConfidenceHistogramBucket[];
  /** category → { outcome → count } */
  outcome_by_category: Record<string, Record<string, number>>;
  /** stakes level → { outcome → count } */
  outcome_by_stakes: Record<string, Record<string, number>>;
  /** reason type → ReasonTypeStat */
  reason_type_stats: Record<string, ReasonTypeStat>;
  brier_history: BrierHistoryPoint[];
  daily_decisions: DailyDecisionPoint[];
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

/** One connection from GET /dashboard/graph/node/{id} — includes lineage
 *  (supersedes) and conflict (contradicts) edges that recall paths hide. */
export interface GraphConnection {
  edge_id: string;
  neighbor_id: string;
  neighbor_type: string;
  /** Truncated (≤120 char) label of the connected node. */
  neighbor_label: string;
  /** false when the neighbor is soft-deleted or dangling (hard-deleted). */
  neighbor_active: boolean;
  relation: string;
  /** 'out' = this node → neighbor; 'in' = neighbor → this node. */
  direction: 'out' | 'in';
  weight: number | null;
  /** 'deterministic' | 'heuristic' | 'inferred' | null */
  extraction_method: string | null;
  auto_linked: boolean;
}

/** The hydrated node returned inside GraphNodeDetail (full, untruncated content). */
export interface GraphNodeDetailNode {
  id: string;
  type: string;
  content: string;
  category: string | null;
  created_at: string | null;
}

/** Full response from GET /dashboard/graph/node/{id}?type=. found=false when the
 *  node id/type is unknown (404). */
export interface GraphNodeDetail {
  found: boolean;
  node?: GraphNodeDetailNode;
  connections?: GraphConnection[];
  connection_count?: number;
  /** true when the node has more edges than the server cap (200); list is the
   *  strongest-weight subset. */
  connections_truncated?: boolean;
}

// ── /dashboard/health (get_health_data) ──────────────────────────────────

/** One day of edge creation data (30-day window, hardcoded in backend). */
export interface HealthDailyEdge {
  date: string;
  count: number;
  auto: number;
  manual: number;
}

/** Degree distribution bucket: how many nodes have this degree. */
export interface HealthDegreeEntry {
  degree: number;
  count: number;
}

/** Cumulative daily graph density data point. */
export interface HealthDensityPoint {
  date: string;
  density: number;
}

/** Daily orphan node count. */
export interface HealthOrphanTrendPoint {
  date: string;
  count: number;
}

/** Full response from GET /dashboard/health (backend ignores ?days param — always 30d). */
export interface HealthData {
  daily_edges: HealthDailyEdge[];
  degree_distribution: HealthDegreeEntry[];
  /** Current graph density (edges / nodes ratio). */
  density: number;
  density_history: HealthDensityPoint[];
  /** Orphan counts per type: decisions, facts, episodes, procedures, chunks. */
  orphan_counts: Record<string, number>;
  orphan_trend: HealthOrphanTrendPoint[];
  total_orphans: number;
  total_edges: number;
  connected_nodes: number;
}

// ── /dashboard/activity (Task 8 / get_activity_data) ──────────────────────

/** One event row from nous_system.events, ordered DESC by created_at. */
export interface ActivityEvent {
  /** event_type aliased to 'type' in the SQL query. */
  type: string;
  created_at: string;
  /** Arbitrary JSONB payload; shape varies by event type. */
  data: Record<string, unknown>;
}

/** Top-censor entry from heart.censors ordered by activation_count DESC. */
export interface ActivityTopCensor {
  id: string;
  trigger_pattern: string | null;
  activations: number;
}

export interface ActivityCensorStats {
  total: number;
  active: number;
  auto_created: number;
  manual_created: number;
  total_activations_7d: number;
  false_positives_7d: number;
  top_censors: ActivityTopCensor[];
}

/** Next upcoming schedule fire. */
export interface ActivityNextFire {
  id: string;
  task: string | null;
  next_fire_at: string;
}

export interface ActivityScheduleStats {
  total: number;
  active: number;
  fires_7d: number;
  next_fires: ActivityNextFire[];
}

export interface ActivitySleepStats {
  total_sleeps: number;
  /** ISO string or null when no sleep has run yet. */
  last_sleep: string | null;
  facts_created: number;
  procedures_created: number;
  censors_retired: number;
}

/** Full response from GET /dashboard/activity?hours=168. */
export interface ActivityData {
  /** Last 100 events in the window, ordered newest-first. */
  events: ActivityEvent[];
  censor_stats: ActivityCensorStats;
  schedule_stats: ActivityScheduleStats;
  sleep_stats: ActivitySleepStats;
}

// ── /dashboard/rubric (F024-3b) ────────────────────────────────────────────

/** One rubric dimension from heart.rubric_versions.dimensions (JSONB array). */
export interface RubricDimension {
  name: string;
  weight: number;
  description: string;
  min_weight: number;
  max_weight: number;
}

/** Summary of the currently active rubric version. */
export interface RubricActiveRubric {
  version: string;
  status: string;
  dimension_count: number;
  created_at: string | null;
  dimensions: RubricDimension[];
}

/** One version history entry (no dimensions detail, just counts). */
export interface RubricVersionEntry {
  version: string;
  status: string;
  change_reason: string | null;
  dimension_count: number;
  created_at: string | null;
}

/** One recent outcome signal row. */
export interface RubricSignal {
  signal_type: string;
  confidence: number;
  evidence: string | null;
  created_at: string | null;
}

/** One day in the 30-day outcome signal trend. */
export interface RubricTrendDay {
  date: string;
  completed: number;
  corrected: number;
  praised: number;
  reworked: number;
  self_corrected: number;
}

/** One correlation entry from outcome_correlations JSONB. */
export interface RubricCorrelation {
  dimension: string;
  signal_type: string;
  pearson_r: number;
  spearman_rho: number;
}

/** Weight snapshot for one rubric version. */
export interface RubricWeightSnapshot {
  version: string;
  created_at: string | null;
  weights: Record<string, number>;
}

/** Feature-flag config from settings. */
export interface RubricConfig {
  rubric_enabled: boolean;
  evolution_enabled: boolean;
  outcome_detection_enabled: boolean;
  min_episodes_for_correlation: number;
  weight_change_cap: number;
}

/** Full response from GET /dashboard/rubric. */
export interface RubricData {
  active_rubric: RubricActiveRubric | null;
  version_history: RubricVersionEntry[];
  outcome_signals: {
    total: number;
    by_type: Record<string, number>;
    recent: RubricSignal[];
    daily_trend: RubricTrendDay[];
  };
  correlations: {
    data: RubricCorrelation[];
    sample_size: number;
  };
  weight_history: RubricWeightSnapshot[];
  config: RubricConfig;
}

// ── /dashboard/density (F040) ──────────────────────────────────────────────

/** Per-type orphan stats returned inside density_by_type. */
export interface DensityTypeStats {
  total: number;
  orphan: number;
  orphan_rate: number;
}

/** One row in backfill_progress (auto-linked edges per day). */
export interface DensityBackfillRow {
  date: string;
  edges: number;
}

/** Full response from GET /dashboard/density. */
export interface DensityData {
  total_nodes: number;
  total_edges: number;
  total_orphans: number;
  orphan_rate: number;
  avg_degree: number;
  connected_nodes: number;
  /** Keyed by node type: fact, decision, episode, procedure, chunk. */
  density_by_type: Record<string, DensityTypeStats>;
  /** Keyed by relation name; value is edge count. */
  edge_distribution: Record<string, number>;
  backfill_progress: DensityBackfillRow[];
}

// ── F035.6: Consolidation Audit Diff ──────────────────────────────────────

/** One persisted sleep-cycle consolidation audit (GET /dashboard/consolidation). */
export interface ConsolidationCycle {
  cycle_id: string;
  trace_id: string | null;
  started_at: string | null;
  finished_at: string | null;
  status: string;
  phases_run: string[];
  /** Per-op tallies recorded for the cycle (e.g. { merged: 1, superseded: 2 }). */
  totals: Record<string, number>;
  action_count: number;
}

/** Full response from GET /dashboard/consolidation. */
export interface ConsolidationData {
  cycles: ConsolidationCycle[];
}

/** One diff-style action row within a cycle. */
export interface ConsolidationAction {
  action_id: string;
  phase: string;
  op: string;
  target_ids: string[];
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  rationale: string | null;
  created_at: string | null;
}

/** Full response from GET /dashboard/consolidation/{cycle_id}. */
export interface ConsolidationCycleDetail {
  cycle: ConsolidationCycle | null;
  actions: ConsolidationAction[];
}

// ── Identity view (GET /identity, PUT /identity/{section}, /profile/facts, /facts/{id}) ──

/** Full response from GET /identity. */
export interface IdentityResponse {
  agent_id: string;
  is_initiated: boolean | null;
  sections: Record<string, string>;
}

/** Response from PUT /facts/{fact_id} — supersede-edit of a Tier-1 fact. */
export interface FactUpdateResponse {
  status: 'superseded' | 'merged_into_existing';
  new_fact_id: string;
  stored_content?: string;
}

/** Response from GET /profile/facts. */
export interface ProfileFactsResponse {
  facts: BrowserFact[];  // superset cast — endpoint omits some BrowserFact fields; view reads none of them
  total: number;
}

// ── Retrieval telemetry (F091 — GET /dashboard/retrieval, /dashboard/retrieval/{id}) ──

/** One score change applied to a candidate by a named stage. */
export interface RetrievalMutation {
  stage: string;
  score_before: number | null;
  score_after: number | null;
  reason: string | null;
}

/** One item that entered retrieval, and what became of it. */
export interface RetrievalCandidate {
  id: string;
  type: string;
  entry_leg: string;
  entry_score: number | null;
  entry_rank: number | null;
  snippet: string;
  mutations: RetrievalMutation[];
  final_rank: number | null;
  disposition: string;
  disposition_stage: string | null;
  /** Set when a gate dropped this but a later stage (e.g. pinning) rescued it. */
  restored_from: string | null;
}

/**
 * One leg's summary. `attempted` distinguishes a leg that ran and found
 * nothing from one that never ran — no combination of config flags can
 * reconstruct that, since some legs are skipped on runtime state.
 */
export interface RetrievalLeg {
  name: string;
  attempted: boolean;
  n_returned: number;
  n_deduped: number;
  /**
   * Rows this leg fetched then discarded internally before returning.
   * EXACT and present on every retrieval — unlike `candidates`, which is
   * sampled and capped. So an item missing from `candidates` may be a
   * sampling artifact, but this count never overstates what survived.
   */
  n_dropped: number;
  score_min: number | null;
  score_max: number | null;
  error: string | null;
  skip_reason: string | null;
  /**
   * Set on a mechanism leg (spreading_activation, brain_graph) whose rows are
   * counted under another leg — always "graph_expanded". Absent on every other
   * leg and on rows written before it existed (see lib/retrievalLegs.ts).
   */
  counted_in?: string | null;
}

/** One seed → edge → neighbor traversal. */
export interface RetrievalExpansion {
  seed_id: string;
  seed_type: string;
  seed_score: number | null;
  neighbor_id: string;
  neighbor_type: string;
  stage: string;
  hop: number;
  edge_relation: string | null;
  edge_weight: number | null;
  extraction_method: string | null;
  /** seed_score × edge_weight — traversal path strength, NOT the pipeline ranking score. */
  path_strength: number | null;
  won_best_path: boolean;
}

/** A memory type dropped from the pool BEFORE search ran. */
export interface RetrievalExcludedType {
  type: string;
  stage: string;
}

/** List-view row — headers only; candidate/expansion payloads are not fetched. */
export interface RetrievalEntry {
  id: string;
  session_id: string | null;
  turn_number: number | null;
  trace_id: string | null;
  timestamp: string;
  path: 'pipeline' | 'context' | 'script';
  query: string | null;
  duration_ms: number | null;
  legs: RetrievalLeg[];
  excluded_types: RetrievalExcludedType[];
  n_candidates: number;
  n_rendered: number;
  n_expansions: number;
  disposition_counts: Record<string, number>;
  truncated: boolean;
  has_candidates: boolean;
}

export interface RetrievalData {
  entries: RetrievalEntry[];
  /** Aggregated over SAMPLED rows only — dispositions do not exist otherwise. */
  disposition_totals: Record<string, number>;
  /** Aggregated over ALL rows — legs are captured regardless of sampling. */
  /**
   * `dropped` is summed over EVERY retrieval in the window, not just the
   * sampled ones — leg counters are recorded unconditionally, unlike the
   * candidate array. So it has a different (larger) denominator than the
   * disposition totals, and the two must not be combined into one ratio.
   */
  leg_totals: Record<
    string,
    { attempted: number; returned: number; deduped: number; dropped: number; errors: number }
  >;
  count: number;
  /** Honest denominator for disposition_totals. */
  sampled_count: number;
}

export interface RetrievalDetail extends Omit<RetrievalEntry, 'has_candidates'> {
  /** null when this retrieval was not sampled — NOT the same as "found nothing". */
  candidates_by_disposition: Record<string, RetrievalCandidate[]> | null;
  expansions: RetrievalExpansion[];
}

// ── /dashboard/intentions (F099 Phase 3c) ────────────────────────────────
// Model-authored strings (intent, note, rationale, arguments, result, title) arrive RAW: render as text only.

export interface IntentionLimits {
  depth: number; spawns: number; turns: number; tokens: number; stalls: number;
  spawn_blocked: boolean; escalate: string | null;
}
export interface IntentionNode {
  id: string; parent_id: string | null; depth: number; source_kind: string; source_id: string; state: string;
  close_reason: string | null; intent: string; created_at: string | null; result_at: string | null;
  closed_at: string | null; attempts: number; failed_tokens: number; children: IntentionNode[];
}
export interface IntentionArrival {
  id: string; n: number; intention_ids: string[]; decision: string | null; note: string | null;
  progress_claimed: boolean | null; progress: boolean | null; confidence: number | null; gate_reason: string | null;
  outcome: string; tokens_in: number; tokens_out: number; decided_at: string | null;
  decision_record_id: string | null; decision_outcome: string | null; decision_reviewer: string | null;
  result_to_decision_s: number | null;
}
export interface IntentionProposalView {
  id: string; short_id: string; root_id: string; intention_id: string; arrival_id: string | null; tool: string;
  arguments: unknown; rationale: string; state: string; deadline: string | null; decided_at: string | null;
  decided_by: string | null; result: string | null;
}
export interface IntentionOwnerRow {
  id: string; msg_type: string; title: string; created_at: string | null; push_after: string | null;
  pushed_at: string | null; delivered_at: string | null;
}
export interface IntentionRoot {
  id: string; short_id: string; intent: string; state: string; wake_policy: string; authority: string;
  origin_kind: string; origin_channel: string | null; created_at: string | null; deadline: string | null;
  root_cancelled_at: string | null; root_expired_at: string | null; closed_at: string | null;
  limits: IntentionLimits | null; children: IntentionNode[]; arrivals: IntentionArrival[];
  proposals: IntentionProposalView[]; owner_rows: IntentionOwnerRow[];
}
export interface TokenColumns {
  arrivals_in: number; arrivals_out: number; failed_tokens: number; subtask_tokens: number; dag_tokens: number; total: number;
}
export interface ContextLogColumns {
  input_uncached: number; output: number; cache_write: number; cache_read: number; calls: number;
}
export interface IntentionMetrics {
  arrivals_by_decision_outcome: { decision: string | null; outcome: string; gated: boolean; count: number }[];
  result_to_decision_latency_s: { p50: number | null; p90: number | null; max: number | null; n: number };
  results_lost: number; results_waiting: number; results_lost_grace_s: number; owner_rows_unpushed: number;
  proposals_by_state: { counts: Record<string, number>; approved_total: number };
  escalations_by_cause: { by_reason: Record<string, number>; expired_roots: number };
  stalls: {
    verified_no_progress: number; claimed_not_credited: number; stall_escalations: number;
    progress_claimed: number; progress_credited: number;
  };
  arrivals_ungradable: number;
  token_spend: {
    basis: string; roots_counted: number; budget_counted: TokenColumns; context_log: ContextLogColumns;
    unattributed_output: number; failed_tokens: number;
    top_roots: { root_id: string; short_id: string; intent: string | null; budget_counted: TokenColumns;
      context_log: ContextLogColumns; unattributed_output: number }[];
  };
}
export interface IntentionsData {
  continuation: boolean; window?: string; roots: IntentionRoot[]; metrics: IntentionMetrics | null;
}

// ── /dashboard/autonomy (task scores, the owner's attention, autonomy inventory) ──

export interface ScoreStats {
  count: number;
  mean: number | null;
  p10: number | null;
  p50: number | null;
  p90: number | null;
  overrides: number;
}

export interface VerifiedShare { scored: number; verified: number; share: number | null }

export interface Penalties { harness_errors: number; refusals: number; unverified_claims: number }

export interface InventoryTool {
  tool_name: string;
  total: number;
  by_status: { success: number; error: number; blocked: number; other: number };
  /** Over the successes only. */
  by_context: { owner_present: number; approved: number; autonomous: number };
  side_effects: Record<string, number>;
  compensable: boolean;
  de_facto_level: 'L0' | 'acts_without_asking';
}

export interface AutonomyData {
  window_days: number;
  taps: { total: number; by_action: { action_name: string; count: number }[] };
  triage_taps: { heartbeat: number; decision: number };
  approval_cards_asked: number;
  decision_grades: { reviewer: string; outcome: string | null; count: number }[];
  task_scores: {
    scorer_version: number;
    first_scored_at: string | null;
    last_scored_at: string | null;
    /** The heartbeat the scoring command writes after every pass; null before its first pass. */
    scorer: {
      scorer_version: number;
      last_run_at: string;
      last_status: string;
      last_error: string | null;
      last_ok_at: string | null;
      rows_submitted: number | null;
    } | null;
    /** Answered check audits with no completed audit.verdict action for that audit; normally 0. */
    audits_without_action_row: number;
    /** The settled numbers' window: as long as the chosen one, ending 14 days ago (a score settles at 14 days).
     *  `held` counts its subtasks that are not settled, and so are in neither settled number. */
    settled_window: { from: string; to: string; held: number };
    headline: {
      verified_share: { provisional: VerifiedShare; settled: VerifiedShare };
      owner_overrides: { total: number; subtasks: number; rollups: number };
      harness_penalties: Penalties;
    };
    subtasks: {
      /** null before the scorer's first run. */
      finished: number | null;
      covered: number | null;
      coverage: number | null;
      by_outcome: Record<string, number>;
      provisional: ScoreStats;
      settled: ScoreStats;
    };
    rollups: Record<string, {
      count: number;
      by_outcome: Record<string, number>;
      own_penalties: Penalties & { owner_overrides: number };
    }>;
  };
  /** Always the last 30 days, whatever `window_days` is. */
  inventory: {
    days: number;
    tools: InventoryTool[];
    censors: { days: number; by_provenance: Record<string, { created: number; deactivated: number }> };
  };
  events_persisted: boolean;
  /** Empty when events_persisted is false: not measured, not "no warnings". At most 50. */
  warn_classes: { mode: string; context_kind: string; violation: string; tool_name: string; count: number }[];
  /** Every check-and-fix record, whatever `window_days` is (nous/api/job_fix_dashboard.py). */
  job_fixes: { enabled: boolean; jobs: JobFixRow[] };
  /** F107 scheduled-run guards in the window (nous/api/guard_dashboard.py), from persisted records only. */
  guards: GuardsBlock;
}

/** `invalid`: a heartbeat check's guard refused before it ran (its timeout does not fit the check's, or a DAG owns the check). */
export type GuardReason = 'output' | 'stderr' | 'exit' | 'timeout' | 'error' | 'not_allowlisted' | 'invalid';
export type GuardSignal = 'report' | 'finding' | 'inbox' | 'push' | 'ledger';

/** The counts are the totals over every schedule with a guard record or a guard now, and every guarded heartbeat check. */
export interface GuardsBlock extends GuardCounts {
  /** NOUS_SCHEDULE_GUARDS_ENABLED. */
  enabled: boolean;
  /** NOUS_EVENT_BUS_ENABLED. Every record but a skip is a bus event: false means nothing of them was recorded, not "none". */
  bus_enabled: boolean;
  window_days: number;
  /** Active schedules with a guard, each with its own counts. `mode` as written: shadow (the default), skip, or an unknown one. */
  schedules: GuardSchedule[];
  /** The remainder of every count (records of stopped or de-guarded schedules, and ones naming no schedule),
   *  so the rows add up to the totals; null when it is all zero. */
  other: GuardOther | null;
  /** F107 phase 3: one row per heartbeat check with a guard now or a record in the window. */
  checks: GuardCheck[];
  /** The latest 10 records, newest first. Texts are command output and job text: plain text only. */
  recent: GuardRecord[];
}

export interface GuardOther extends GuardCounts {
  /** The latest 10 of those records, newest first. */
  last_runs: GuardRecord[];
}

export interface GuardCounts {
  runs: number;
  guard_runs: number;
  skipped: number;
  would_skip: number;
  /** Would-skip runs with no record yet of whether the model acted: not counted in shadow_misses either way. */
  would_skip_unrecorded: number;
  /** Would-skip runs whose record could not see a finding (no structured report: the legacy path, or no report emitted) and saw no ledger write. */
  would_skip_unobserved: number;
  fell_through: Record<GuardReason, number>;
  /** Would-skip runs in which the model acted (a finding or a ledger write); never more than would_skip. */
  shadow_misses: number;
  shadow_misses_by_signal: Record<GuardSignal, number>;
}

export interface GuardSchedule extends GuardCounts {
  schedule_id: string;
  job: string;
  name: string | null;
  mode: string;
  allowlisted: boolean;
  /** This schedule's latest 10 records, newest first. */
  last_runs: GuardRecord[];
}

export interface GuardCheck extends GuardCounts {
  check_id: string;
  /** The check's name; null when its row is gone and no record named it. */
  check: string | null;
  /** null when the check no longer has a guard (or no row): its records are kept until they leave the window. */
  enabled: boolean | null;
  timeout_seconds: number | null;
  name: string | null;
  mode: string | null;
  allowlisted: boolean | null;
  /** This check's latest 10 records, newest first. */
  last_runs: GuardRecord[];
}

export interface GuardRecord {
  at: string;
  kind: 'skipped' | 'would_skip' | 'fell_through';
  schedule_id: string | null;
  subtask_id: string | null;
  /** Set on a heartbeat check's record (F107 phase 3), null on a schedule's. */
  check_id: string | null;
  name: string | null;
  mode: string | null;
  reason: GuardReason | null;
  exit_code: number | null;
  timed_out: boolean | null;
  duration_ms: number | null;
  stdout_bytes: number | null;
  stderr_bytes: number | null;
  stderr_head: string | null;
  command: string | null;
  /** Shadow runs only, once the turn is over: null when there is no record of it, or when it was not observed. */
  acted: boolean | null;
  acted_by: GuardSignal[] | null;
  /** False when the run left no structured report, so "the model did nothing" could not be seen; null with no record. */
  observed: boolean | null;
}

/** One recurring job check-and-fix has a record for. Texts are written by the check run: plain text only. */
export interface JobFixRow {
  schedule_id: string;
  /** The job's instructions as one clipped line (the old ones while a changed text stands). */
  job: string;
  active: boolean;
  /** running, judging, kept, closed, stopping, stopped; null for an unreadable record. */
  state: string | null;
  trigger: string | null;
  verdict: string | null;
  stop_reason: string | null;
  started_at: string | null;
  applied_at: string | null;
  settled_at: string | null;
  /** When the job was stopped; records stopped before this field existed have none. */
  stopped_at: string | null;
  fix_run: string | null;
  /** The owner report was written. A stop from outside the check writes none, nor does a job with no container. */
  reported: boolean;
  /** Runs before the check: how many of the scored ones went wrong. */
  baseline: { bad: number; scored: number } | null;
  /** Judging only: scored runs since the change, as the judge counts them. */
  since_change: { scored: number; bad: number; needed: number } | null;
  detail: {
    why?: string; what_changed?: string; diagnosis?: string; owner_action?: string; checked?: string;
    reason?: string; new_instructions?: string;
    check?: { status?: string; outcome?: string; head?: string };
  };
  /** Only while a changed text stands: the instructions before and now. */
  old_task: string | null;
  task: string | null;
}
