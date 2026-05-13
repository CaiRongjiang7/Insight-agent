<template>
  <ConsoleLayout>
    <div class="page-shell">
      <div class="page-header">
        <div>
          <div class="page-title">数据源管理</div>
          <div class="page-subtitle">接入 PR / 日志 / 群聊等 Bug 数据源，管理 Webhook 配置。</div>
        </div>
        <div class="toolbar">
          <el-button @click="openDialog()">新增数据源</el-button>
          <el-button type="primary" @click="fetchRepos">刷新列表</el-button>
        </div>
      </div>

      <!-- 筛选栏 -->
      <PanelCard>
        <div class="toolbar">
          <el-input v-model="filters.name" clearable placeholder="仓库名称搜索" style="max-width: 240px" />
          <el-select v-model="filters.source_type" clearable placeholder="数据源类型" style="width: 160px">
            <el-option label="GitHub" value="github" />
            <el-option label="GitLab" value="gitlab" />
            <el-option label="手动" value="manual" />
          </el-select>
          <el-select v-model="filters.status" clearable placeholder="状态" style="width: 130px">
            <el-option label="正常" value="normal" />
            <el-option label="异常" value="abnormal" />
            <el-option label="维护中" value="maintenance" />
          </el-select>
          <el-button type="primary" @click="fetchRepos">筛选</el-button>
          <el-button @click="resetFilters">重置</el-button>
        </div>
      </PanelCard>

      <!-- 数据源列表 -->
      <PanelCard title="数据源列表">
        <el-table :data="repos" stripe>
          <el-table-column prop="name" label="仓库名称" min-width="160" />
          <el-table-column label="类型" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="sourceTagType(row.source_type)">
                {{ sourceLabel(row.source_type) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="仓库地址" min-width="180">
            <template #default="{ row }">
              <a v-if="row.url" :href="row.url" target="_blank" rel="noreferrer" @click.stop class="repo-link">{{ row.url }}</a>
              <span v-else class="text-muted">-</span>
            </template>
          </el-table-column>
          <el-table-column prop="owner" label="负责人" width="100" />
          <el-table-column label="Webhook" width="100">
            <template #default="{ row }">
              <span :class="['webhook-dot', row.webhook_active ? 'dot-on' : 'dot-off']" />
              {{ row.webhook_active ? "已激活" : "未激活" }}
            </template>
          </el-table-column>
          <el-table-column label="Bug 数" width="80" align="right">
            <template #default="{ row }">
              <span class="num-text">{{ row.bug_report_count ?? row.incident_count ?? 0 }}</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <StatusTag :status="row.status" :label="row.status_label" />
            </template>
          </el-table-column>
          <el-table-column label="操作" width="200" fixed="right">
            <template #default="{ row }">
              <div class="row-actions">
                <button class="action-btn" @click.stop="viewDetail(row)">
                  <el-icon><View /></el-icon> 详情
                </button>
                <button class="action-btn" @click.stop="showWebhook(row)">
                  <el-icon><Link /></el-icon> Webhook
                </button>
                <button class="action-btn" @click.stop="openDialog(row)">
                  <el-icon><Edit /></el-icon> 编辑
                </button>
                <button class="action-btn danger" @click.stop="removeRepo(row.id)">
                  <el-icon><Delete /></el-icon>
                </button>
              </div>
            </template>
          </el-table-column>
        </el-table>
      </PanelCard>

      <!-- 新增/编辑数据源对话框 -->
      <el-dialog v-model="dialogVisible" :title="editingId ? '编辑数据源' : '新增数据源'" width="560px">
        <el-form :model="form" label-width="100px">
          <el-form-item label="仓库名称">
            <el-input v-model="form.name" placeholder="如：my-frontend-app" />
          </el-form-item>
          <el-form-item label="数据源类型">
            <el-select v-model="form.source_type" style="width: 100%">
              <el-option label="GitHub" value="github" />
              <el-option label="GitLab" value="gitlab" />
              <el-option label="手动输入" value="manual" />
            </el-select>
          </el-form-item>
          <el-form-item label="仓库 URL">
            <el-input v-model="form.url" placeholder="https://github.com/owner/repo" />
          </el-form-item>
          <el-form-item label="Owner">
            <el-input v-model="form.owner" placeholder="负责人" />
          </el-form-item>
          <el-form-item label="描述">
            <el-input v-model="form.description" type="textarea" :rows="2" placeholder="可选描述" />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="dialogVisible = false">取消</el-button>
          <el-button type="primary" @click="submitRepo">保存</el-button>
        </template>
      </el-dialog>

      <!-- Webhook 配置对话框 -->
      <el-dialog v-model="webhookVisible" title="Webhook 配置" width="560px">
        <el-alert
          title="将以下 Webhook URL 和 Secret 配置到 GitHub/GitLab 仓库设置中"
          type="info"
          :closable="false"
          show-icon
          style="margin-bottom: 16px"
        />
        <el-descriptions :column="1" border>
          <el-descriptions-item label="Webhook URL">
            <div class="copy-row">
              <code class="code-text">{{ webhookInfo.url }}</code>
              <el-button size="small" @click="copyText(webhookInfo.url)">复制</el-button>
            </div>
          </el-descriptions-item>
          <el-descriptions-item label="Secret Token">
            <div class="copy-row">
              <code class="code-text">{{ webhookInfo.secret || "（无 Secret）" }}</code>
              <el-button v-if="webhookInfo.secret" size="small" @click="copyText(webhookInfo.secret)">复制</el-button>
            </div>
          </el-descriptions-item>
          <el-descriptions-item label="触发事件">
            <el-tag size="small" style="margin-right: 6px">pull_request</el-tag>
            <el-tag size="small" style="margin-right: 6px">push</el-tag>
            <el-tag size="small">workflow_run</el-tag>
          </el-descriptions-item>
        </el-descriptions>
        <template #footer>
          <div style="display: flex; justify-content: space-between; align-items: center">
            <el-switch
              v-model="webhookActiveState"
              active-text="激活 Webhook"
              inactive-text="停用"
              @change="toggleWebhook"
            />
            <el-button type="primary" @click="webhookVisible = false">关闭</el-button>
          </div>
        </template>
      </el-dialog>
    </div>
  </ConsoleLayout>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import { View, Edit, Delete, Link } from "@element-plus/icons-vue";
import { platformApi } from "@/api/platform";
import type { RepoRecord } from "@/types/platform";
import ConsoleLayout from "@/components/layout/ConsoleLayout.vue";
import PanelCard from "@/components/common/PanelCard.vue";
import StatusTag from "@/components/common/StatusTag.vue";

const router = useRouter();

// ─── 状态 ────────────────────────────────────────────────────────────────────
const repos = ref<RepoRecord[]>([]);
const editingId = ref<string | null>(null);
const dialogVisible = ref(false);
const webhookVisible = ref(false);
const webhookInfo = ref({ url: "", secret: "" });
const webhookActiveState = ref(false);
const currentWebhookRepoId = ref("");

const filters = reactive({ name: "", status: "", source_type: "" });
const form = reactive({
  name: "",
  url: "",
  owner: "",
  description: "",
  source_type: "github" as string,
});

// ─── 数据加载 ─────────────────────────────────────────────────────────────────
async function fetchRepos() {
  try {
    repos.value = await platformApi.getRepos(filters);
  } catch {
    ElMessage.error("加载数据源列表失败");
  }
}

function resetFilters() {
  filters.name = "";
  filters.status = "";
  filters.source_type = "";
  fetchRepos();
}

// ─── 用户交互 ─────────────────────────────────────────────────────────────────
function openDialog(repo?: RepoRecord) {
  editingId.value = repo?.id || null;
  form.name = repo?.name || "";
  form.url = repo?.url || "";
  form.owner = repo?.owner || "";
  form.description = repo?.description || "";
  form.source_type = repo?.source_type || "github";
  dialogVisible.value = true;
}

async function submitRepo() {
  try {
    if (editingId.value) {
      await platformApi.updateRepo(editingId.value, { ...form });
      ElMessage.success("数据源已更新");
    } else {
      await platformApi.createRepo({ ...form });
      ElMessage.success("数据源已添加");
    }
    dialogVisible.value = false;
    fetchRepos();
  } catch {
    ElMessage.error("操作失败");
  }
}

async function removeRepo(repoId: string) {
  await ElMessageBox.confirm("确认删除该数据源？关联的 Bug 报告将不受影响。", "确认删除", {
    type: "warning",
  });
  try {
    await platformApi.deleteRepo(repoId);
    ElMessage.success("已删除");
    fetchRepos();
  } catch {
    ElMessage.error("删除失败");
  }
}

function viewDetail(repo: RepoRecord) {
  router.push(`/repo-management/detail/${repo.id}`);
}

async function showWebhook(repo: RepoRecord) {
  currentWebhookRepoId.value = repo.id;
  webhookActiveState.value = repo.webhook_active || false;
  try {
    const info = await platformApi.getWebhookUrl(repo.id);
    webhookInfo.value = info;
  } catch {
    // 降级：生成本地 URL 提示
    webhookInfo.value = {
      url: `${window.location.origin}/api/webhooks/git?repo=${repo.id}`,
      secret: "（请在后端配置 Webhook Secret）",
    };
  }
  webhookVisible.value = true;
}

async function toggleWebhook(active: boolean) {
  try {
    await platformApi.toggleWebhook(currentWebhookRepoId.value, active);
    ElMessage.success(active ? "Webhook 已激活" : "Webhook 已停用");
    fetchRepos();
  } catch {
    ElMessage.error("操作失败");
  }
}

function copyText(text: string) {
  navigator.clipboard.writeText(text).then(() => ElMessage.success("已复制到剪贴板")).catch(() => ElMessage.error("复制失败"));
}

// ─── 工具函数 ─────────────────────────────────────────────────────────────────
function sourceLabel(type?: string) {
  const map: Record<string, string> = { github: "GitHub", gitlab: "GitLab", manual: "手动" };
  return map[type || ""] || type || "未知";
}

function sourceTagType(type?: string): "primary" | "success" | "warning" | "info" | "danger" {
  const map: Record<string, "primary" | "success" | "warning" | "info" | "danger"> = {
    github: "primary",
    gitlab: "warning",
    manual: "info",
  };
  return map[type || ""] || "info";
}

onMounted(fetchRepos);
</script>

<style scoped>
.repo-link {
  color: var(--brand);
  text-decoration: none;
  font-size: 12px;
}
.repo-link:hover { text-decoration: underline; }

.text-muted { color: var(--text-muted); font-size: 12px; }

.num-text { font-weight: 600; color: var(--text-primary); }

/* Webhook 状态点 */
.webhook-dot {
  display: inline-block;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  margin-right: 5px;
  vertical-align: middle;
}
.dot-on  { background: var(--success); }
.dot-off { background: var(--text-muted); }

/* 行内操作按钮 — 始终可见 */
.row-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}

.action-btn {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 3px 8px;
  font-size: 12px;
  font-weight: 500;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: #fff;
  color: var(--text-secondary);
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s, background 0.15s;
  white-space: nowrap;
  font-family: var(--font-sans);
}

.action-btn:hover {
  border-color: var(--brand);
  color: var(--brand);
  background: var(--brand-light);
}

.action-btn.danger:hover {
  border-color: var(--danger);
  color: var(--danger);
  background: var(--danger-bg);
}

/* 对话框内 */
.copy-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.code-text {
  font-family: var(--font-mono);
  font-size: 12px;
  background: var(--bg-subtle);
  border: 1px solid var(--border);
  padding: 4px 10px;
  border-radius: var(--radius);
  word-break: break-all;
  flex: 1;
  color: var(--text-primary);
}
</style>
