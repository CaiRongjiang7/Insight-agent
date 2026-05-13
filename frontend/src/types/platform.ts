// ─── 通用搜索 ────────────────────────────────────────────────────────────────
export interface SearchHit {
  id: string;
  type: string;
  title: string;
  subtitle: string;
  route: string;
}

// ─── 数据源（仓库/Webhook）────────────────────────────────────────────────────
export interface RepoRecord {
  id: string;
  name: string;
  url: string;
  owner: string;
  status: string;
  status_label: string;
  ci_status: string;
  ci_status_label: string;
  health_score: number;
  agent_name: string;
  description: string;
  local_path: string;
  created_at: string;
  incident_count?: number;
  task_count?: number;
  // 数据源管理新增字段
  source_type?: "github" | "gitlab" | "manual";
  webhook_url?: string;
  webhook_secret?: string;
  webhook_active?: boolean;
  bug_report_count?: number;
}

// ─── Bug 报告（替代 IncidentRecord）──────────────────────────────────────────
export type BugCategory =
  | "react_closure"
  | "sse_stream"
  | "race_condition"
  | "ws_reconnect"
  | "ci_pollution"
  | "memory_leak"
  | "type_error"
  | "api_timeout"
  | "other";

export type ReviewStatus = "draft" | "pending" | "approved" | "rejected" | "published";

export interface SourceRef {
  type: "pr_comment" | "commit_message" | "log" | "chat";
  location: string;
  snippet: string;
}

export interface RootCauseInfo {
  content: string;
  source: SourceRef;
}

export interface ImpactInfo {
  scope: string;
  affected_users: string | null;
  severity: "P0" | "P1" | "P2" | "P3";
}

/** LLM 抽取生成的结构化复盘文档 */
export interface BugReviewDocument {
  title: string;
  root_cause: RootCauseInfo;
  impact: ImpactInfo;
  fix_solution: string;
  prevention: string;
  related_modules: string[];
  keywords: string[];
  confidence: number;
  source_refs: SourceRef[];
  review_status: ReviewStatus;
  reviewer_notes: string;
}

/** Bug 报告主体（来自 PR / 日志 / 手动输入） */
export interface BugReport {
  id: string;
  title: string;
  repo_id: string;
  repo_name: string;
  bug_category: BugCategory;
  bug_category_label: string;
  status: string;
  status_label: string;
  review_status: ReviewStatus;
  created_at: string;
  agent_name: string;
  semantic_summary: string;
  // 来源信息
  source_type: "pr" | "log" | "chat" | "manual";
  pr_id?: string;
  commit_sha?: string;
  repo_url?: string;
  // LLM 抽取结果
  review_document?: BugReviewDocument;
  extraction_confidence?: number;
  // 内容字段
  description?: string;
  logs_excerpt?: string;
  full_logs?: string;
  key_stack_frames?: string[];
  progress_logs?: Array<{ timestamp: string; step: string; summary: string }>;
  // 追溯
  trace_id?: string;
  wiki_publish_result?: Record<string, unknown>;
}

// 向后兼容别名
export type IncidentRecord = BugReport & {
  // 兼容旧字段名
  error_type?: string;
  error_type_label?: string;
  provider?: string;
  provider_label?: string;
  ci_run_id?: string;
  ci_ref?: string;
  ci_revision?: string;
  ci_web_url?: string;
  ci_rerun_target?: string;
  ci_subject?: string;
  provider_metadata?: Record<string, unknown>;
  report_available?: boolean;
  commit_hash?: string | null;
  delivery?: Record<string, unknown>;
  remote_delivery_pending?: boolean;
  repair_steps?: Array<{ title: string; description: string; target_files: string[] }>;
  root_cause_graph?: { nodes: Array<Record<string, unknown>>; links: Array<Record<string, unknown>> };
};

// ─── 知识条目（替代 SkillRecord）──────────────────────────────────────────────
export interface KnowledgeEntry {
  id: string;
  name: string;
  description: string;
  // 知识库新增字段
  keywords: string[];
  applicable_context: string;
  related_bug_ids: string[];
  // 兼容旧字段
  skill_type?: string;
  skill_type_label?: string;
  usage_count: number;
  success_rate: number;
  hit_rate: number;
  decay_factor: number;
  status: string;
  status_label: string;
  status_order?: number;
  origin?: string;
  last_used_at: string;
  triggers: string[];
  error_types: string[];
  action_template: string;
}

// 向后兼容别名
export type SkillRecord = KnowledgeEntry;

// ─── CI / 数据源相关（保留兼容性）────────────────────────────────────────────
export interface CiProviderRecord {
  key: string;
  label: string;
  enabled: boolean;
  defaults: Record<string, string | null | undefined>;
}

export interface JenkinsJobRecord {
  name: string;
  full_name: string;
  url?: string | null;
  status?: string | null;
}

export interface CiRunRecord {
  provider: string;
  provider_label: string;
  run_id: string;
  name: string;
  status: string;
  conclusion?: string;
  web_url?: string;
  logs_url?: string;
  rerun_target?: string;
  ref?: string;
  revision?: string;
  created_at?: string;
  updated_at?: string;
  metadata?: Record<string, unknown>;
}

export interface CiSyncResponse {
  provider: string;
  runs: CiRunRecord[];
  incidents: IncidentRecord[];
  latest_logs_excerpt: string;
}

// ─── 审核相关 ─────────────────────────────────────────────────────────────────
export interface ReviewAction {
  id: string;
  bug_report_id: string;
  action: "approve" | "reject" | "publish";
  reviewer: string;
  notes: string;
  created_at: string;
}

export interface ReviewActionResponse {
  success: boolean;
  bug_report_id: string;
  new_status: ReviewStatus;
  wiki_url?: string;
  message?: string;
}

// ─── 抽取任务（替代 RepairJobRecord）──────────────────────────────────────────
export interface ExtractionJob {
  id: string;
  bug_report_id: string;
  action: string;
  action_label: string;
  status: string;
  created_at: string;
  updated_at: string;
  started_at?: string;
  finished_at?: string;
  result?: BugReviewDocument;
  quality_score?: number;
  error?: string;
}

// 向后兼容别名
export interface RepairActionResponse {
  success: boolean;
  accepted?: boolean;
  job_id?: string;
  status?: string;
  action_label?: string;
  trace_id: string;
  report_path: string;
  commit_hash?: string | null;
  sandbox_path: string;
  diff_summary: string;
  delivery?: Record<string, unknown>;
  remote_delivery_pending?: boolean;
}

export type RepairJobRecord = ExtractionJob;

// ─── 任务调度 ─────────────────────────────────────────────────────────────────
export interface TaskRecord {
  id: string;
  incident_id: string;
  repo_id: string;
  repo_name: string;
  task_type: string;
  task_type_label: string;
  priority: string;
  priority_label: string;
  master_agent: string;
  sub_agents: string[];
  status: string;
  status_label: string;
  lifecycle_status: string;
  lifecycle_status_label: string;
  token_usage: number;
  cost_estimate: number;
  created_at: string;
  runtime_backend: string;
  task_chain_length: number;
  topology?: { nodes: Array<Record<string, unknown>>; links: Array<Record<string, unknown>> };
  trace_id?: string;
  report_available?: boolean;
  delivery?: Record<string, unknown>;
  // 抽取任务新增
  pipeline_stage?: "event_ingestion" | "orchestration" | "extraction" | "rag_enhancement" | "review" | "knowledge_retrieval";
  pipeline_stage_label?: string;
  extraction_score?: number;
}

// ─── Agent 工作流 ─────────────────────────────────────────────────────────────
export interface AgentWorkflowResponse {
  incident_id: string;
  running: boolean;
  latest_job?: RepairJobRecord | Record<string, never>;
  events: Array<Record<string, unknown>>;
  nodes: Array<Record<string, unknown>>;
  links: Array<Record<string, unknown>>;
}

export interface AgentWorkflowDeltaResponse {
  incident_id: string;
  from_event_index: number;
  event_count: number;
  running: boolean;
  done: boolean;
  latest_job?: RepairJobRecord | Record<string, never>;
  events?: Array<Record<string, unknown>>;
  nodes: Array<Record<string, unknown>>;
  node_updates?: Array<Record<string, unknown>>;
  links: Array<Record<string, unknown>>;
}

export interface ReportResponse {
  trace_id: string;
  markdown: string;
}

// ─── 仪表盘（重构版）──────────────────────────────────────────────────────────
export interface DashboardOverview {
  hero_metrics: Array<{ key: string; label: string; value: number; status: string; route: string }>;
  health_score: { value: number; summary: string };
  trend_bundle: {
    labels: string[];
    // 重构版新指标
    extraction_success_rate: number[];
    knowledge_hit_rate: number[];
    token_usage: number[];
    avg_review_time_hours: number[];
    // 兼容旧字段
    repair_success_rate?: number[];
    skill_hit_rate?: number[];
    task_chain_length?: number[];
  };
  pipeline_loop: {
    nodes: Array<{ name: string; value: number }>;
  };
  // 兼容旧字段
  dream_loop?: { nodes: Array<{ name: string; value: number }> };
  recent_bug_reports: BugReport[];
  // 兼容旧字段
  recent_incidents?: IncidentRecord[];
  top_knowledge: KnowledgeEntry[];
  // 兼容旧字段
  top_skills?: SkillRecord[];
  system_status: {
    online_agents: number;
    running_tasks: number;
    provider_snapshot: Record<string, unknown>;
  };
  notifications: Array<Record<string, unknown>>;
  provider_snapshot: Record<string, unknown>;
}

// ─── 上下文管理 ───────────────────────────────────────────────────────────────
export interface ContextOverview {
  selected_repo_id: string;
  repos: RepoRecord[];
  working_contexts: Array<Record<string, unknown>>;
  short_term_contexts: Array<Record<string, unknown>>;
  long_term_contexts: Array<Record<string, unknown>>;
  dynamic_graph: {
    nodes: Array<Record<string, unknown>>;
    links: Array<Record<string, unknown>>;
    graph_metadata: Record<string, unknown>;
    language_breakdown: Record<string, number>;
  };
}

// ─── 可观测性（重构版）────────────────────────────────────────────────────────
export interface ObservabilitySnapshot {
  summary: Record<string, number>;
  charts: {
    labels: string[];
    // 重构版：六层管道各层指标
    extraction_success_rate: number[];
    knowledge_hit_rate: number[];
    avg_retrieval_score: number[];
    review_approval_rate: number[];
    wiki_publish_rate: number[];
    fallback_trigger_rate: number[];
    token_usage: number[];
    // 兼容旧字段
    repair_success_rate?: number[];
    skill_hit_rate?: number[];
    agent_llm_reasoning_rate?: number[];
    auto_dream_rate?: number[];
    reflection_rate?: number[];
    task_chain_length?: number[];
  };
  pipeline_latency: {
    event_ingestion_ms: number;
    orchestration_ms: number;
    extraction_ms: number;
    rag_enhancement_ms: number;
    review_ms: number;
    total_ms: number;
  };
  thresholds: Record<string, { warning: number; danger: number }>;
  anomalies: Array<Record<string, unknown>>;
  learning_status: Record<string, unknown>;
}

// ─── 语义搜索 ─────────────────────────────────────────────────────────────────
export interface KnowledgeSearchResult {
  entry: KnowledgeEntry;
  score: number;
  match_type: "semantic" | "bm25" | "hybrid";
  matched_keywords: string[];
}
