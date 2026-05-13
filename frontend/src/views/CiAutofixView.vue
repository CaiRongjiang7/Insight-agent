<template>
  <ConsoleLayout>
    <div class="page-shell">
      <div class="page-header">
        <div>
          <div class="page-title">Bug 复盘工作台</div>
          <div class="page-subtitle">接收 Bug 报告 → LLM 结构化抽取 → 人工审核 → 发布知识库。</div>
        </div>
        <div class="toolbar">
          <el-button @click="openManualInput">手动提交</el-button>
          <el-button type="primary" @click="fetchBugReports">刷新列表</el-button>
        </div>
      </div>

      <!-- 筛选栏 -->
      <PanelCard>
        <div class="toolbar">
          <el-input v-model="filters.keyword" clearable placeholder="任务ID / 仓库名 / 关键词" style="max-width: 260px" />
          <el-select v-model="filters.review_status" clearable placeholder="审核状态" style="width: 150px">
            <el-option label="草稿" value="draft" />
            <el-option label="待审核" value="pending" />
            <el-option label="已通过" value="approved" />
            <el-option label="已驳回" value="rejected" />
            <el-option label="已发布" value="published" />
          </el-select>
          <el-select v-model="filters.source_type" clearable placeholder="来源类型" style="width: 130px">
            <el-option label="PR" value="pr" />
            <el-option label="日志" value="log" />
            <el-option label="手动" value="manual" />
          </el-select>
          <el-button type="primary" @click="fetchBugReports">筛选</el-button>
          <el-button type="danger" :disabled="!selectedIds.length" @click="deleteSelected">删除选中</el-button>
        </div>
      </PanelCard>

      <!-- 主体：列表 + 详情 -->
      <div class="split-layout">
        <!-- 左侧：Bug 报告列表 -->
        <PanelCard title="Bug 报告列表">
          <el-table
            :data="filteredReports"
            height="680"
            highlight-current-row
            @current-change="selectReport"
            @selection-change="(rows: BugReport[]) => (selectedIds = rows.map((r) => r.id))"
          >
            <el-table-column type="selection" width="48" />
            <el-table-column prop="id" label="ID" width="130" show-overflow-tooltip />
            <el-table-column prop="repo_name" label="仓库" min-width="130" show-overflow-tooltip />
            <el-table-column label="类型" width="110" show-overflow-tooltip>
              <template #default="{ row }">
                {{ row.bug_category_label || row.bug_category || "-" }}
              </template>
            </el-table-column>
            <el-table-column label="来源" width="70">
              <template #default="{ row }">
                <el-tag size="small" :type="sourceTagType(row.source_type)">
                  {{ sourceLabel(row.source_type) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="审核" width="90">
              <template #default="{ row }">
                <StatusTag :status="row.review_status || 'draft'" :label="reviewStatusLabel(row.review_status)" />
              </template>
            </el-table-column>
            <el-table-column prop="created_at" label="时间" width="100" show-overflow-tooltip />
          </el-table>
        </PanelCard>

        <!-- 右侧：详情 + 审核区 -->
        <div class="detail-panel">
          <template v-if="!selectedReport">
            <PanelCard>
              <el-empty description="请从左侧列表选择一个 Bug 报告" />
            </PanelCard>
          </template>
          <template v-else>
            <!-- 基础信息 -->
            <PanelCard :title="selectedReport.title || '（未命名）'" description="Bug 报告基础信息">
              <el-descriptions :column="2" border size="small">
                <el-descriptions-item label="任务ID">{{ selectedReport.id }}</el-descriptions-item>
                <el-descriptions-item label="仓库">{{ selectedReport.repo_name }}</el-descriptions-item>
                <el-descriptions-item label="来源类型">
                  <el-tag size="small" :type="sourceTagType(selectedReport.source_type)">
                    {{ sourceLabel(selectedReport.source_type) }}
                  </el-tag>
                </el-descriptions-item>
                <el-descriptions-item label="Bug 类型">{{ selectedReport.bug_category_label || selectedReport.bug_category || "-" }}</el-descriptions-item>
                <el-descriptions-item v-if="selectedReport.pr_id" label="PR ID">{{ selectedReport.pr_id }}</el-descriptions-item>
                <el-descriptions-item v-if="selectedReport.commit_sha" label="Commit SHA">{{ selectedReport.commit_sha?.substring(0, 8) }}</el-descriptions-item>
                <el-descriptions-item label="创建时间">{{ selectedReport.created_at }}</el-descriptions-item>
                <el-descriptions-item label="负责 Agent">{{ selectedReport.agent_name || "-" }}</el-descriptions-item>
              </el-descriptions>

              <!-- 原始描述 -->
              <div v-if="selectedReport.description" class="raw-section">
                <div class="section-label">原始描述</div>
                <div class="raw-content">{{ selectedReport.description }}</div>
              </div>
            </PanelCard>

            <!-- LLM 抽取结果 vs 人工编辑（核心 Diff 区） -->
            <PanelCard title="LLM 抽取结果" description="以下为 AI 抽取的结构化复盘文档，可点击编辑修正">
              <template v-if="selectedReport.review_document">
                <el-alert
                  :title="`AI 置信度：${Math.round((selectedReport.extraction_confidence || selectedReport.review_document.confidence || 0) * 100)}%`"
                  :type="confidenceType(selectedReport.extraction_confidence || selectedReport.review_document.confidence || 0)"
                  :closable="false"
                  show-icon
                  style="margin-bottom: 16px"
                />

                <!-- 可编辑字段 -->
                <el-form :model="editForm" label-position="top" size="small">
                  <el-row :gutter="16">
                    <el-col :span="24">
                      <el-form-item label="复盘标题">
                        <el-input v-model="editForm.title" placeholder="Bug 复盘标题" />
                      </el-form-item>
                    </el-col>
                    <el-col :span="12">
                      <el-form-item label="严重程度">
                        <el-select v-model="editForm.severity" style="width: 100%">
                          <el-option label="P0 - 紧急" value="P0" />
                          <el-option label="P1 - 严重" value="P1" />
                          <el-option label="P2 - 一般" value="P2" />
                          <el-option label="P3 - 轻微" value="P3" />
                        </el-select>
                      </el-form-item>
                    </el-col>
                    <el-col :span="12">
                      <el-form-item label="影响范围">
                        <el-input v-model="editForm.impact_scope" placeholder="如：全量用户 / 特定版本 / 特定功能" />
                      </el-form-item>
                    </el-col>
                    <el-col :span="24">
                      <el-form-item label="根本原因">
                        <el-input v-model="editForm.root_cause" type="textarea" :rows="3" placeholder="根本原因描述" />
                      </el-form-item>
                    </el-col>
                    <el-col :span="24">
                      <el-form-item label="修复方案">
                        <el-input v-model="editForm.fix_solution" type="textarea" :rows="3" placeholder="具体修复方案" />
                      </el-form-item>
                    </el-col>
                    <el-col :span="24">
                      <el-form-item label="预防措施">
                        <el-input v-model="editForm.prevention" type="textarea" :rows="2" placeholder="如何避免同类问题再次发生" />
                      </el-form-item>
                    </el-col>
                    <el-col :span="24">
                      <el-form-item label="关键词（用逗号分隔）">
                        <el-input v-model="editForm.keywords" placeholder="react-closure, useCallback, 闭包陷阱" />
                      </el-form-item>
                    </el-col>
                  </el-row>
                </el-form>

                <!-- 来源引用 -->
                <div v-if="selectedReport.review_document.source_refs?.length" class="source-refs">
                  <div class="section-label">来源引用（AI 结论来自以下原文，防止幻觉）</div>
                  <div v-for="(ref, idx) in selectedReport.review_document.source_refs" :key="idx" class="source-ref-item">
                    <div class="source-ref-header">
                      <el-tag size="small" :type="refTagType(ref.type)">{{ refTagText(ref.type) }}</el-tag>
                      <span class="ref-location">{{ ref.location }}</span>
                    </div>
                    <div class="source-ref-body">
                      <code class="ref-snippet">{{ ref.snippet }}</code>
                    </div>
                  </div>
                </div>

                <!-- 操作按钮 -->
                <div class="action-row">
                  <el-button @click="saveEdits" :loading="savingEdits">保存修改</el-button>
                  <el-button @click="triggerExtraction" :loading="extracting" type="warning">重新抽取</el-button>
                </div>
              </template>
              <template v-else>
                <el-empty description="暂无 LLM 抽取结果">
                  <el-button type="primary" @click="triggerExtraction" :loading="extracting">触发抽取</el-button>
                </el-empty>
              </template>
            </PanelCard>

            <!-- 审核操作区 -->
            <PanelCard title="审核操作" description="当前审核状态及可执行动作">
              <div class="review-status-bar">
                <span class="status-label-text">当前状态：</span>
                <StatusTag
                  :status="selectedReport.review_status || 'draft'"
                  :label="reviewStatusLabel(selectedReport.review_status)"
                />
                <el-tag v-if="selectedReport.extraction_confidence" size="small" type="info" style="margin-left: 8px">
                  AI置信度 {{ Math.round(selectedReport.extraction_confidence * 100) }}%
                </el-tag>
              </div>

              <el-form :model="reviewForm" label-position="top" size="small" style="margin-top: 12px">
                <el-form-item label="审核备注">
                  <el-input v-model="reviewForm.notes" type="textarea" :rows="2" placeholder="审核说明（驳回时必填）" />
                </el-form-item>
              </el-form>

              <div class="action-row">
                <el-button
                  type="success"
                  :disabled="!canApprove"
                  :loading="reviewing"
                  @click="approve"
                >
                  通过审核
                </el-button>
                <el-button
                  type="danger"
                  :disabled="!canReject"
                  :loading="reviewing"
                  @click="reject"
                >
                  驳回
                </el-button>
                <el-button
                  type="primary"
                  :disabled="selectedReport.review_status !== 'approved'"
                  :loading="publishing"
                  @click="publishWiki"
                >
                  发布到知识库
                </el-button>
              </div>

              <!-- 审核历史 -->
              <div v-if="reviewHistory.length" class="review-history">
                <div class="section-label">审核记录</div>
                <el-timeline>
                  <el-timeline-item
                    v-for="record in reviewHistory"
                    :key="record.id"
                    :timestamp="record.created_at"
                    placement="top"
                  >
                    <div>
                      <el-tag size="small" :type="record.action === 'approve' ? 'success' : record.action === 'reject' ? 'danger' : 'primary'">
                        {{ record.action === 'approve' ? '通过' : record.action === 'reject' ? '驳回' : '发布' }}
                      </el-tag>
                      <span class="reviewer-name">{{ record.reviewer }}</span>
                      <p v-if="record.notes" class="review-notes">{{ record.notes }}</p>
                    </div>
                  </el-timeline-item>
                </el-timeline>
              </div>
            </PanelCard>
          </template>
        </div>
      </div>

      <!-- 手动提交对话框 -->
      <el-dialog v-model="manualInputVisible" title="手动提交 Bug 信息" width="580px">
        <el-form :model="manualForm" label-width="100px">
          <el-form-item label="仓库">
            <el-select v-model="manualForm.repo_id" style="width: 100%" placeholder="选择关联仓库">
              <el-option v-for="repo in repos" :key="repo.id" :label="repo.name" :value="repo.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="PR 链接">
            <el-input v-model="manualForm.pr_url" placeholder="https://github.com/xxx/yyy/pull/123" />
          </el-form-item>
          <el-form-item label="Bug 标题">
            <el-input v-model="manualForm.pr_title" placeholder="简要描述 Bug 问题" />
          </el-form-item>
          <el-form-item label="详细描述">
            <el-input v-model="manualForm.description" type="textarea" :rows="4" placeholder="粘贴 PR 描述、错误信息等" />
          </el-form-item>
          <el-form-item label="日志片段">
            <el-input v-model="manualForm.logs" type="textarea" :rows="4" placeholder="粘贴关键错误日志（可选）" />
          </el-form-item>
        </el-form>
        <template #footer>
          <div class="dialog-footer">
            <el-button text type="primary" @click="fillExample">填入示例</el-button>
            <div>
              <el-button @click="manualInputVisible = false">取消</el-button>
              <el-button type="primary" @click="submitManualInput" :loading="submitting">提交并触发抽取</el-button>
            </div>
          </div>
        </template>
      </el-dialog>
    </div>
  </ConsoleLayout>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import { platformApi } from "@/api/platform";
import type { BugReport, ReviewAction, RepoRecord } from "@/types/platform";
import ConsoleLayout from "@/components/layout/ConsoleLayout.vue";
import PanelCard from "@/components/common/PanelCard.vue";
import StatusTag from "@/components/common/StatusTag.vue";

const route = useRoute();

// ─── 状态 ────────────────────────────────────────────────────────────────────
const bugReports = ref<BugReport[]>([]);
const selectedReport = ref<BugReport | null>(null);
const selectedIds = ref<string[]>([]);
const reviewHistory = ref<ReviewAction[]>([]);
const repos = ref<RepoRecord[]>([]);

const filters = reactive({ keyword: "", review_status: "", source_type: "" });
const editForm = reactive({
  title: "",
  root_cause: "",
  fix_solution: "",
  prevention: "",
  severity: "P2",
  impact_scope: "",
  keywords: "",
});
const reviewForm = reactive({ notes: "" });
const manualForm = reactive({ repo_id: "", pr_url: "", pr_title: "", description: "", logs: "" });

const manualInputVisible = ref(false);
const extracting = ref(false);
const savingEdits = ref(false);
const reviewing = ref(false);
const publishing = ref(false);
const submitting = ref(false);

// ─── 计算属性 ─────────────────────────────────────────────────────────────────
const filteredReports = computed(() => {
  return bugReports.value.filter((r) => {
    const kw = filters.keyword.toLowerCase();
    const byKeyword =
      !kw || r.id.toLowerCase().includes(kw) || r.repo_name?.toLowerCase().includes(kw) || r.title?.toLowerCase().includes(kw);
    const byStatus = !filters.review_status || r.review_status === filters.review_status;
    const bySource = !filters.source_type || r.source_type === filters.source_type;
    return byKeyword && byStatus && bySource;
  });
});

const canApprove = computed(() => {
  const s = selectedReport.value?.review_status;
  return s === "pending" || s === "draft";
});
const canReject = computed(() => {
  const s = selectedReport.value?.review_status;
  return s === "pending" || s === "approved";
});

// ─── 数据加载 ─────────────────────────────────────────────────────────────────
async function fetchBugReports() {
  try {
    // 优先尝试新端点，降级到旧端点
    try {
      bugReports.value = await platformApi.getBugReports(filters);
    } catch {
      const legacy = await platformApi.getIncidents(filters);
      bugReports.value = legacy as unknown as BugReport[];
    }
  } catch (err) {
    ElMessage.error("加载 Bug 报告失败");
  }
}

async function fetchRepos() {
  try {
    repos.value = await platformApi.getRepos();
  } catch {
    repos.value = [];
  }
}

async function fetchReviewHistory(id: string) {
  try {
    reviewHistory.value = await platformApi.getReviewHistory(id);
  } catch {
    reviewHistory.value = [];
  }
}

// ─── 用户交互 ─────────────────────────────────────────────────────────────────
function selectReport(report: BugReport | null) {
  selectedReport.value = report;
  if (report) {
    syncEditForm(report);
    fetchReviewHistory(report.id);
  }
}

function syncEditForm(report: BugReport) {
  const doc = report.review_document;
  editForm.title = doc?.title || report.title || "";
  editForm.root_cause = doc?.root_cause?.content || "";
  editForm.fix_solution = doc?.fix_solution || "";
  editForm.prevention = doc?.prevention || "";
  editForm.severity = doc?.impact?.severity || "P2";
  editForm.impact_scope = doc?.impact?.scope || "";
  editForm.keywords = (doc?.keywords || []).join(", ");
}

async function triggerExtraction() {
  if (!selectedReport.value) return;
  extracting.value = true;
  try {
    await platformApi.triggerExtraction(selectedReport.value.id, { async_mode: true });
    ElMessage.success("已触发 LLM 抽取");
  } catch (e: any) {
    const detail = e?.response?.data?.detail || e?.message || "";
    if (detail.includes("API key") || detail.includes("Missing")) {
      ElMessage.warning("Demo 模式下无 LLM 服务，数据已预置。配置 backend/.env 中 LLM_API_KEY 后可启用真实抽取");
    } else {
      ElMessage.error("触发抽取失败：" + (detail || "未知错误"));
    }
  } finally {
    extracting.value = false;
  }
}

async function saveEdits() {
  if (!selectedReport.value) return;
  savingEdits.value = true;
  try {
    const doc = {
      ...selectedReport.value.review_document,
      title: editForm.title,
      root_cause: {
        ...(selectedReport.value.review_document?.root_cause || {}),
        content: editForm.root_cause,
      },
      fix_solution: editForm.fix_solution,
      prevention: editForm.prevention,
      keywords: editForm.keywords.split(",").map((k) => k.trim()).filter(Boolean),
      impact: {
        ...(selectedReport.value.review_document?.impact || {}),
        severity: editForm.severity,
        scope: editForm.impact_scope,
      },
    };
    const updated = await platformApi.updateBugReportFields(selectedReport.value.id, { review_document: doc });
    selectedReport.value = updated;
    ElMessage.success("已保存修改");
  } catch {
    ElMessage.error("保存失败");
  } finally {
    savingEdits.value = false;
  }
}

async function approve() {
  if (!selectedReport.value) return;
  reviewing.value = true;
  try {
    const res = await platformApi.approveBugReport(selectedReport.value.id, reviewForm.notes);
    ElMessage.success("审核通过");
    selectedReport.value = { ...selectedReport.value, review_status: res.new_status };
    await fetchReviewHistory(selectedReport.value.id);
    await fetchBugReports();
  } catch {
    ElMessage.error("审核操作失败");
  } finally {
    reviewing.value = false;
  }
}

async function reject() {
  if (!selectedReport.value) return;
  if (!reviewForm.notes) {
    ElMessage.warning("驳回时请填写审核备注");
    return;
  }
  reviewing.value = true;
  try {
    const res = await platformApi.rejectBugReport(selectedReport.value.id, reviewForm.notes);
    ElMessage.success("已驳回");
    selectedReport.value = { ...selectedReport.value, review_status: res.new_status };
    await fetchReviewHistory(selectedReport.value.id);
    await fetchBugReports();
  } catch {
    ElMessage.error("驳回操作失败");
  } finally {
    reviewing.value = false;
  }
}

async function publishWiki() {
  if (!selectedReport.value) return;
  await ElMessageBox.confirm("确认发布到踩坑知识库？此操作将生成公开的知识条目。", "确认发布", {
    confirmButtonText: "发布",
    cancelButtonText: "取消",
    type: "warning",
  });
  publishing.value = true;
  try {
    const res = await platformApi.publishToWiki(selectedReport.value.id);
    ElMessage.success(res.wiki_url ? `已发布：${res.wiki_url}` : "已发布到知识库");
    selectedReport.value = { ...selectedReport.value, review_status: res.new_status };
    await fetchBugReports();
  } catch {
    ElMessage.error("发布失败");
  } finally {
    publishing.value = false;
  }
}

async function deleteSelected() {
  if (!selectedIds.value.length) return;
  await ElMessageBox.confirm(`确认删除选中的 ${selectedIds.value.length} 条 Bug 报告？`, "确认删除", {
    type: "warning",
  });
  try {
    await platformApi.deleteBugReports(selectedIds.value);
    ElMessage.success("已删除");
    selectedIds.value = [];
    selectedReport.value = null;
    await fetchBugReports();
  } catch {
    ElMessage.error("删除失败");
  }
}

function fillExample() {
  manualForm.repo_id = repos.value[0]?.id || "";
  manualForm.pr_url = "https://github.com/gulugulu33/aiflow-studio/pull/3";
  manualForm.pr_title = "fix(rag): 修复知识库上传文件报500的问题";
  manualForm.description = (
    "上传文件到知识库时，如遇同名文件覆盖或 PDF 解析失败，"
    + "后端直接返回 HTTP 500 而非有意义的错误提示。\n\n"
    + "根因：\n"
    + "1. 上传时未检查同名文件，Prisma create 触发唯一约束冲突\n"
    + "2. embedding 模型名配置错误（text-embedding-3-small → text-embedding-v3）\n"
    + "3. embedding 异常被 try/catch 静默吞掉"
  );
  manualForm.logs = (
    "[RAG] upload document '产品手册.pdf' → 500 Internal Server Error\n"
    + "[RAG] Prisma: duplicate key error on document.name\n"
    + "[RAG] Embedding API returned error: model not found"
  );
  ElMessage.success("已填入示例数据（PR #3 真实数据）");
}

function openManualInput() {
  manualForm.repo_id = "";
  manualForm.pr_url = "";
  manualForm.pr_title = "";
  manualForm.description = "";
  manualForm.logs = "";
  manualInputVisible.value = true;
}

async function submitManualInput() {
  submitting.value = true;
  try {
    await platformApi.submitManualInput(manualForm.repo_id, {
      pr_url: manualForm.pr_url || undefined,
      pr_title: manualForm.pr_title || undefined,
      description: manualForm.description || undefined,
      logs: manualForm.logs || undefined,
    });
    ElMessage.success("提交成功，已触发 LLM 抽取");
    manualInputVisible.value = false;
    await fetchBugReports();
  } catch {
    ElMessage.error("提交失败");
  } finally {
    submitting.value = false;
  }
}

// ─── 工具函数 ─────────────────────────────────────────────────────────────────
function reviewStatusLabel(status?: string) {
  const map: Record<string, string> = {
    draft: "草稿",
    pending: "待审核",
    approved: "已通过",
    rejected: "已驳回",
    published: "已发布",
  };
  return status ? (map[status] || status) : "草稿";
}

function sourceLabel(type?: string) {
  const map: Record<string, string> = { pr: "PR", log: "日志", chat: "群聊", manual: "手动" };
  return map[type || ""] || "未知";
}

function sourceTagType(type?: string): "primary" | "success" | "warning" | "info" | "danger" {
  const map: Record<string, "primary" | "success" | "warning" | "info" | "danger"> = {
    pr: "primary",
    log: "warning",
    chat: "success",
    manual: "info",
  };
  return map[type || ""] || "info";
}

function refTagType(type?: string): "primary" | "success" | "warning" | "info" | "danger" {
  const map: Record<string, "primary" | "success" | "warning" | "info" | "danger"> = {
    pr_comment: "primary",
    commit_message: "success",
    log: "warning",
    chat: "info",
    commit: "primary",
  };
  return map[type || ""] || "info";
}

function refTagText(type?: string) {
  const map: Record<string, string> = {
    pr_comment: "PR 评论",
    commit_message: "Commit",
    commit: "Commit",
    log: "错误日志",
    chat: "群聊记录",
  };
  return map[type || ""] || type || "来源";
}

function confidenceType(confidence: number): "success" | "warning" | "error" | "info" {
  if (confidence >= 0.8) return "success";
  if (confidence >= 0.5) return "warning";
  return "error";
}

// ─── 初始化 ────────────────────────────────────────────────────────────────────
onMounted(async () => {
  await Promise.all([fetchBugReports(), fetchRepos()]);
  // 如果有 URL 参数则自动选中
  const id = route.query.bugReportId as string;
  if (id) {
    const found = bugReports.value.find((r) => r.id === id);
    if (found) selectReport(found);
  }
});

watch(
  () => route.query.bugReportId,
  (id) => {
    if (id) {
      const found = bugReports.value.find((r) => r.id === String(id));
      if (found) selectReport(found);
    }
  },
);
</script>

<style scoped>
.split-layout {
  display: grid;
  grid-template-columns: 420px 1fr;
  gap: 16px;
  align-items: flex-start;
}

.dialog-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  width: 100%;
}

.detail-panel {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.raw-section {
  margin-top: 12px;
}

.section-label {
  font-size: 12px;
  color: var(--text-secondary);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-bottom: 8px;
}

.raw-content {
  background: var(--bg-subtle);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-md);
  padding: 10px 14px;
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 160px;
  overflow-y: auto;
}

.source-refs {
  margin-top: 16px;
  border-top: 1px solid var(--border-light);
  padding-top: 12px;
}

.source-ref-item {
  margin-bottom: 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 10px 12px;
  background: var(--bg-card);
}

.source-ref-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.ref-location {
  font-size: 12px;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.source-ref-body {
  margin-top: 4px;
}

.ref-snippet {
  display: block;
  padding: 8px 12px;
  background: var(--bg-subtle);
  border: 1px solid var(--border-light);
  border-radius: var(--radius);
  font-size: 12px;
  font-family: var(--font-mono);
  color: var(--text-primary);
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.6;
}

.action-row {
  display: flex;
  gap: 10px;
  margin-top: 16px;
  flex-wrap: wrap;
}

.review-status-bar {
  display: flex;
  align-items: center;
  gap: 8px;
}

.status-label-text {
  font-size: 13px;
  color: var(--text-secondary);
}

.review-history {
  margin-top: 16px;
  border-top: 1px solid var(--border-light);
  padding-top: 12px;
}

.reviewer-name {
  margin-left: 8px;
  font-size: 12px;
  color: var(--text-secondary);
}

.review-notes {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--text-secondary);
}
</style>
