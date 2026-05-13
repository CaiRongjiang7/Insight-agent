import { http } from "./http";
import type {
  AgentWorkflowResponse,
  BugReport,
  CiProviderRecord,
  CiSyncResponse,
  ContextOverview,
  DashboardOverview,
  ExtractionJob,
  IncidentRecord,
  JenkinsJobRecord,
  KnowledgeEntry,
  KnowledgeSearchResult,
  ObservabilitySnapshot,
  RepairActionResponse,
  ReportResponse,
  RepoRecord,
  ReviewAction,
  ReviewActionResponse,
  ReviewStatus,
  SearchHit,
  SkillRecord,
  TaskRecord,
} from "@/types/platform";

const apiBaseUrl = String(import.meta.env.VITE_API_BASE_URL || "/api").replace(/\/$/, "");

function apiUrl(path: string) {
  return `${apiBaseUrl}${path.startsWith("/") ? path : `/${path}`}`;
}

export const platformApi = {
  // ─── 仪表盘 ─────────────────────────────────────────────────────────────
  getDashboard(days = 7) {
    return http.get<DashboardOverview>("/dashboard/overview", { params: { days } }).then((res) => res.data);
  },
  getSystemStatus() {
    return http.get("/system/status").then((res) => res.data);
  },
  getNotifications() {
    return http.get("/notifications").then((res) => res.data);
  },
  search(query: string) {
    return http.get<SearchHit[]>("/search", { params: { q: query } }).then((res) => res.data);
  },

  // ─── CI 数据源（向后兼容保留）───────────────────────────────────────────
  getProviders() {
    return http.get("/providers").then((res) => res.data);
  },
  getCiProviders() {
    return http.get<{ providers: CiProviderRecord[]; default: string }>("/ci/providers").then((res) => res.data);
  },
  getJenkinsJobs() {
    return http.get<JenkinsJobRecord[]>("/ci/jenkins/jobs").then((res) => res.data);
  },
  syncCiFailures(payload: Record<string, unknown>) {
    return http.post<CiSyncResponse>("/ci/sync", payload).then((res) => res.data);
  },
  rerunCiRun(payload: Record<string, unknown>) {
    return http.post("/ci/rerun", payload).then((res) => res.data);
  },

  // ─── 数据源（仓库 + Webhook）────────────────────────────────────────────
  getRepos(params?: Record<string, unknown>) {
    return http.get<RepoRecord[]>("/repos", { params }).then((res) => res.data);
  },
  getRepoDetail(repoId: string) {
    return http.get<RepoRecord>(`/repos/${repoId}`).then((res) => res.data);
  },
  createRepo(payload: Record<string, unknown>) {
    return http.post<RepoRecord>("/repos", payload).then((res) => res.data);
  },
  updateRepo(repoId: string, payload: Record<string, unknown>) {
    return http.put<RepoRecord>(`/repos/${repoId}`, payload).then((res) => res.data);
  },
  deleteRepo(repoId: string) {
    return http.delete(`/repos/${repoId}`).then((res) => res.data);
  },
  /** 获取 Webhook 配置 URL（给用户复制到 GitHub/GitLab） */
  getWebhookUrl(repoId: string) {
    return http.get<{ url: string; secret: string }>(`/repos/${repoId}/webhook`).then((res) => res.data);
  },
  /** 激活/停用 Webhook */
  toggleWebhook(repoId: string, active: boolean) {
    return http.post(`/repos/${repoId}/webhook/toggle`, { active }).then((res) => res.data);
  },
  /** 手动提交 PR 信息触发抽取 */
  submitManualInput(repoId: string, payload: { pr_url?: string; pr_title?: string; description?: string; logs?: string }) {
    return http.post<BugReport>("/webhooks/manual", { repo_id: repoId, ...payload }).then((res) => res.data);
  },

  // ─── Demo / 试用 ──────────────────────────────────────────────────────────
  /** 一键重置 Demo 示例数据 */
  resetDemo() {
    return http.post("/demo/reset").then((res) => res.data);
  },
  /** 获取 Demo 示例仓库信息 */
  getDemoInfo() {
    return http.get<{ repo: string; repo_url: string; description: string; bug_reports: number; knowledge_entries: number; flow: Array<{ step: number; label: string; desc: string }> }>("/demo/info").then((res) => res.data);
  },

  // ─── Bug 报告（核心业务端点）────────────────────────────────────────────
  getBugReports(params?: Record<string, unknown>) {
    return http.get<BugReport[]>("/bug-reports", { params }).then((res) => res.data);
  },
  getBugReportDetail(id: string) {
    return http.get<BugReport>(`/bug-reports/${id}`).then((res) => res.data);
  },
  createBugReport(payload: Record<string, unknown>) {
    return http.post<BugReport>("/bug-reports", payload).then((res) => res.data);
  },
  deleteBugReports(ids: string[]) {
    return http.post("/bug-reports/delete", { ids }).then((res) => res.data);
  },
  /** 触发 LLM 抽取 */
  triggerExtraction(bugReportId: string, options?: { async_mode?: boolean }) {
    return http.post<ExtractionJob>(`/extractions`, { bug_report_id: bugReportId, ...options }).then((res) => res.data);
  },
  getExtractionDetail(jobId: string) {
    return http.get<ExtractionJob>(`/extractions/${jobId}`).then((res) => res.data);
  },
  /** 更新字段（人工审核编辑 LLM 结果） */
  updateBugReportFields(id: string, fields: Partial<BugReport>) {
    return http.patch<BugReport>(`/bug-reports/${id}`, fields).then((res) => res.data);
  },

  // ─── 审核操作 ────────────────────────────────────────────────────────────
  approveBugReport(id: string, notes?: string) {
    return http.post<ReviewActionResponse>(`/reviews/${id}/approve`, { notes: notes || "" }).then((res) => res.data);
  },
  rejectBugReport(id: string, notes: string) {
    return http.post<ReviewActionResponse>(`/reviews/${id}/reject`, { notes }).then((res) => res.data);
  },
  publishToWiki(id: string) {
    return http.post<ReviewActionResponse>(`/reviews/${id}/publish`).then((res) => res.data);
  },
  getReviewHistory(id: string) {
    return http.get<ReviewAction[]>(`/reviews/${id}/history`).then((res) => res.data);
  },

  // ─── Agent 工作流（保留向后兼容）────────────────────────────────────────
  getAgentWorkflow(incidentId: string) {
    return http.get<AgentWorkflowResponse>(`/incidents/${incidentId}/agent-workflow`).then((res) => res.data);
  },
  agentWorkflowStreamUrl(incidentId: string, after = 0) {
    const params = new URLSearchParams({ after: String(after) });
    return apiUrl(`/incidents/${encodeURIComponent(incidentId)}/agent-workflow/stream?${params.toString()}`);
  },
  /** Bug 复盘进度 SSE 流 URL */
  bugReviewStreamUrl(bugReportId: string, after = 0) {
    const params = new URLSearchParams({ after: String(after) });
    return apiUrl(`/bug-reports/${encodeURIComponent(bugReportId)}/stream?${params.toString()}`);
  },

  // ─── 旧版操作（向后兼容） ────────────────────────────────────────────────
  getIncidents(params?: Record<string, unknown>) {
    return http.get<IncidentRecord[]>("/incidents", { params }).then((res) => res.data);
  },
  getIncidentDetail(incidentId: string) {
    return http.get<IncidentRecord>(`/incidents/${incidentId}`).then((res) => res.data);
  },
  deleteIncidents(incidentIds: string[]) {
    return http.post("/incidents/delete", { incident_ids: incidentIds }).then((res) => res.data);
  },
  retryIncident(incidentId: string, maxAttempts = 1, remoteDeliveryConfirmed = false, asyncMode = false) {
    return http
      .post<RepairActionResponse>(`/incidents/${incidentId}/retry`, {
        max_attempts: maxAttempts,
        remote_delivery_confirmed: remoteDeliveryConfirmed,
        async_mode: asyncMode,
      })
      .then((res) => res.data);
  },
  manualFixIncident(incidentId: string, instructions: string, maxAttempts = 1, remoteDeliveryConfirmed = false, asyncMode = false) {
    return http
      .post<RepairActionResponse>(`/incidents/${incidentId}/manual-fix`, {
        instructions,
        max_attempts: maxAttempts,
        remote_delivery_confirmed: remoteDeliveryConfirmed,
        async_mode: asyncMode,
      })
      .then((res) => res.data);
  },
  getRepairJob(jobId: string) {
    return http.get(`/repair-jobs/${jobId}`).then((res) => res.data);
  },
  deliverRepair(traceId: string, asyncMode = false) {
    return http.post<RepairActionResponse>(`/repairs/${traceId}/remote-delivery`, { async_mode: asyncMode }).then((res) => res.data);
  },
  terminateIncident(incidentId: string) {
    return http.post(`/incidents/${incidentId}/terminate`).then((res) => res.data);
  },
  getReport(traceId: string) {
    return http.get<ReportResponse>(`/reports/${traceId}`).then((res) => res.data);
  },

  // ─── 任务调度 ────────────────────────────────────────────────────────────
  getTasks(limit = 100) {
    return http.get<TaskRecord[]>("/tasks", { params: { limit } }).then((res) => res.data);
  },
  updateTaskPriority(taskIds: string[], priority: string) {
    return http.post<TaskRecord[]>("/tasks/priority", { task_ids: taskIds, priority }).then((res) => res.data);
  },
  terminateTask(taskId: string) {
    return http.post<TaskRecord>(`/tasks/${taskId}/terminate`).then((res) => res.data);
  },

  // ─── 踩坑知识库（替代 Skills）────────────────────────────────────────────
  /** 获取知识条目列表 */
  getKnowledge(params?: Record<string, unknown>) {
    return http.get<KnowledgeEntry[]>("/knowledge", { params }).then((res) => res.data);
  },
  /** 语义 + BM25 混合搜索 */
  searchKnowledge(query: string, limit = 10) {
    return http.get<KnowledgeSearchResult[]>("/knowledge/search", { params: { q: query, limit } }).then((res) => res.data);
  },
  getKnowledgeDetail(id: string) {
    return http.get<KnowledgeEntry>(`/knowledge/${id}`).then((res) => res.data);
  },
  createKnowledge(payload: Record<string, unknown>) {
    return http.post<KnowledgeEntry>("/knowledge", payload).then((res) => res.data);
  },
  updateKnowledge(id: string, payload: Record<string, unknown>) {
    return http.put<KnowledgeEntry>(`/knowledge/${id}`, payload).then((res) => res.data);
  },
  retireKnowledge(id: string, reason: string) {
    return http.post<KnowledgeEntry>(`/knowledge/${id}/retire`, { reason }).then((res) => res.data);
  },
  restoreKnowledge(id: string) {
    return http.post<KnowledgeEntry>(`/knowledge/${id}/restore`).then((res) => res.data);
  },

  // 向后兼容别名（旧 skills 端点）
  getSkills(limit = 100) {
    return http.get<SkillRecord[]>("/skills", { params: { limit } }).then((res) => res.data);
  },
  getSkillDetail(skillId: string) {
    return http.get(`/skills/${skillId}`).then((res) => res.data);
  },
  createSkill(payload: Record<string, unknown>) {
    return http.post<SkillRecord>("/skills", payload).then((res) => res.data);
  },
  updateSkill(skillId: string, payload: Record<string, unknown>) {
    return http.put<SkillRecord>(`/skills/${skillId}`, payload).then((res) => res.data);
  },
  retireSkill(skillId: string, reason: string) {
    return http.post<SkillRecord>(`/skills/${skillId}/retire`, { reason }).then((res) => res.data);
  },
  restoreSkill(skillId: string) {
    return http.post<SkillRecord>(`/skills/${skillId}/restore`).then((res) => res.data);
  },

  // ─── 上下文管理 ──────────────────────────────────────────────────────────
  getContexts(repoId?: string) {
    return http.get<ContextOverview>("/contexts", { params: { repo_id: repoId } }).then((res) => res.data);
  },
  clearExpiredContexts() {
    return http.delete("/contexts/expired").then((res) => res.data);
  },

  // ─── 可观测性 ────────────────────────────────────────────────────────────
  getObservability(days = 30) {
    return http.get<ObservabilitySnapshot>("/observability", { params: { days } }).then((res) => res.data);
  },
  updateThresholds(thresholds: Record<string, { warning: number; danger: number }>) {
    return http.put("/observability/thresholds", { thresholds }).then((res) => res.data);
  },
};
