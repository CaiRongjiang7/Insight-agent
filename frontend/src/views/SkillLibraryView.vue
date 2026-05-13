<template>
  <ConsoleLayout>
    <div class="page-shell">
      <div class="page-header">
        <div>
          <div class="page-title">踩坑知识库</div>
          <div class="page-subtitle">团队历史 Bug 复盘沉淀的知识条目，支持语义搜索 + BM25 关键词混合召回。</div>
        </div>
        <div class="toolbar">
          <el-button @click="openDialog()">新增条目</el-button>
          <el-button type="primary" @click="fetchKnowledge">刷新</el-button>
        </div>
      </div>

      <!-- 搜索区 -->
      <PanelCard title="语义搜索 + 关键词检索">
        <div class="search-bar">
          <el-input
            v-model="searchQuery"
            clearable
            placeholder="输入技术词汇、错误类型、场景描述…（支持语义搜索）"
            style="flex: 1"
            @keyup.enter="doSearch"
          >
            <template #prefix><el-icon><Search /></el-icon></template>
          </el-input>
          <el-button type="primary" @click="doSearch" :loading="searching">搜索</el-button>
          <el-button @click="clearSearch">清除</el-button>
        </div>
        <!-- 搜索结果 -->
        <template v-if="searchResults.length">
          <div class="search-result-header">
            找到 {{ searchResults.length }} 条相关知识，按相关度排序
          </div>
          <div class="search-results">
            <div
              v-for="result in searchResults"
              :key="result.entry.id"
              class="search-result-item"
              @click="viewDetail(result.entry.id)"
            >
              <div class="result-top">
                <span class="result-name">{{ result.entry.name }}</span>
                <div class="result-badges">
                  <el-tag size="small" :type="matchTagType(result.match_type)">
                    {{ result.match_type === "hybrid" ? "混合" : result.match_type === "bm25" ? "关键词" : "语义" }}
                  </el-tag>
                  <el-tag size="small" type="info">相关度 {{ Math.round(result.score * 100) }}%</el-tag>
                </div>
              </div>
              <div class="result-desc">{{ result.entry.description }}</div>
              <div v-if="result.matched_keywords.length" class="result-keywords">
                <el-tag
                  v-for="kw in result.matched_keywords"
                  :key="kw"
                  size="small"
                  type="warning"
                  class="kw-tag"
                >{{ kw }}</el-tag>
              </div>
            </div>
          </div>
        </template>
      </PanelCard>

      <!-- 筛选 -->
      <PanelCard>
        <div class="toolbar">
          <el-input v-model="keyword" clearable placeholder="知识条目名称搜索" style="max-width: 240px" />
          <el-select v-model="status" clearable placeholder="状态筛选" style="width: 160px">
            <el-option label="生效" value="active" />
            <el-option label="待优化" value="needs_optimization" />
            <el-option label="已下架" value="retired" />
          </el-select>
          <el-input v-model="categoryFilter" clearable placeholder="Bug 类型筛选" style="max-width: 180px" />
        </div>
      </PanelCard>

      <!-- 知识条目卡片网格 -->
      <div v-if="filteredEntries.length" class="card-grid-3">
        <PanelCard
          v-for="entry in filteredEntries"
          :key="entry.id"
          :title="entry.name"
          :description="entry.description"
        >
          <div class="toolbar" style="justify-content: space-between; margin-bottom: 12px">
            <StatusTag :status="entry.status" :label="entry.status_label" />
            <div class="entry-meta">
              <el-tag v-if="entry.keywords?.length" size="small" type="primary">
                {{ entry.keywords.length }} 个关键词
              </el-tag>
            </div>
          </div>

          <!-- 质量指标 -->
          <el-progress :percentage="Math.round((entry.hit_rate || 0) * 100)" :stroke-width="8" status="success">
            <span>命中率 {{ Math.round((entry.hit_rate || 0) * 100) }}%</span>
          </el-progress>
          <el-progress :percentage="Math.round((entry.success_rate || 0) * 100)" :stroke-width="8" style="margin-top: 12px">
            <span>成功率 {{ Math.round((entry.success_rate || 0) * 100) }}%</span>
          </el-progress>

          <el-descriptions :column="1" size="small" style="margin-top: 12px">
            <el-descriptions-item label="调用次数">{{ entry.usage_count || 0 }}</el-descriptions-item>
            <el-descriptions-item label="最后引用">{{ entry.last_used_at || "-" }}</el-descriptions-item>
            <el-descriptions-item v-if="entry.applicable_context" label="适用场景">
              {{ entry.applicable_context }}
            </el-descriptions-item>
          </el-descriptions>

          <!-- 关键词标签 -->
          <div v-if="entry.keywords?.length" class="entry-keywords">
            <el-tag
              v-for="kw in entry.keywords.slice(0, 4)"
              :key="kw"
              size="small"
              type="info"
              class="kw-tag"
            >{{ kw }}</el-tag>
            <span v-if="entry.keywords.length > 4" class="more-kw">+{{ entry.keywords.length - 4 }}</span>
          </div>

          <!-- 关联 Bug -->
          <div v-if="entry.related_bug_ids?.length" class="related-bugs">
            <span class="related-label">关联 {{ entry.related_bug_ids.length }} 个 Bug 报告</span>
          </div>

          <div class="toolbar" style="justify-content: flex-end; margin-top: 10px">
            <el-button link @click="viewDetail(entry.id)">详情</el-button>
            <el-button link @click="openDialog(entry)">编辑</el-button>
            <el-button v-if="entry.status !== 'retired'" link type="danger" @click="retire(entry.id)">下架</el-button>
            <el-button v-else link type="success" @click="restore(entry.id)">恢复</el-button>
          </div>
        </PanelCard>
      </div>
      <el-empty v-else description="暂无知识条目" />

      <!-- 新增/编辑对话框 -->
      <el-dialog v-model="dialogVisible" :title="editingId ? '编辑知识条目' : '新增踩坑知识'" width="600px">
        <el-form :model="form" label-width="100px">
          <el-form-item label="条目名称">
            <el-input v-model="form.name" placeholder="如：React useCallback 闭包陷阱" />
          </el-form-item>
          <el-form-item label="描述">
            <el-input v-model="form.description" type="textarea" :rows="3" placeholder="详细描述踩坑场景和问题本质" />
          </el-form-item>
          <el-form-item label="关键词">
            <el-input v-model="form.keywords" placeholder="以逗号分隔，如：react-closure, useCallback, 闭包" />
          </el-form-item>
          <el-form-item label="适用场景">
            <el-input v-model="form.applicable_context" placeholder="在什么场景下该知识条目会被召回" />
          </el-form-item>
          <el-form-item label="触发词">
            <el-input v-model="triggersText" placeholder="以逗号分隔" />
          </el-form-item>
          <el-form-item label="错误类型">
            <el-input v-model="errorTypesText" placeholder="以逗号分隔" />
          </el-form-item>
          <el-form-item label="处理模板">
            <el-input v-model="form.action_template" type="textarea" :rows="4" placeholder="修复步骤模板" />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="dialogVisible = false">取消</el-button>
          <el-button type="primary" @click="submitEntry">保存</el-button>
        </template>
      </el-dialog>

      <!-- 详情对话框 -->
      <el-dialog v-model="detailVisible" title="知识条目详情" width="680px">
        <pre class="detail-pre">{{ JSON.stringify(selectedEntry, null, 2) }}</pre>
      </el-dialog>
    </div>
  </ConsoleLayout>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessage } from "element-plus";
import { Search } from "@element-plus/icons-vue";
import { platformApi } from "@/api/platform";
import type { KnowledgeEntry, KnowledgeSearchResult } from "@/types/platform";
import ConsoleLayout from "@/components/layout/ConsoleLayout.vue";
import PanelCard from "@/components/common/PanelCard.vue";
import StatusTag from "@/components/common/StatusTag.vue";

// ─── 状态 ────────────────────────────────────────────────────────────────────
const entries = ref<KnowledgeEntry[]>([]);
const searchResults = ref<KnowledgeSearchResult[]>([]);
const searchQuery = ref("");
const keyword = ref("");
const status = ref("");
const categoryFilter = ref("");
const searching = ref(false);
const dialogVisible = ref(false);
const detailVisible = ref(false);
const editingId = ref<string | null>(null);
const selectedEntry = ref<Record<string, unknown> | null>(null);

const form = reactive({
  name: "",
  description: "",
  action_template: "",
  applicable_context: "",
  keywords: "",
});
const triggersText = ref("");
const errorTypesText = ref("");

// ─── 计算属性 ─────────────────────────────────────────────────────────────────
const filteredEntries = computed(() =>
  entries.value.filter((entry) => {
    const byName = !keyword.value || entry.name.toLowerCase().includes(keyword.value.toLowerCase());
    const byStatus = !status.value || entry.status === status.value;
    const byCategory = !categoryFilter.value || entry.error_types?.some((t) => t.toLowerCase().includes(categoryFilter.value.toLowerCase()));
    return byName && byStatus && byCategory;
  }),
);

// ─── 数据加载 ─────────────────────────────────────────────────────────────────
async function fetchKnowledge() {
  try {
    // 优先新端点，降级到旧端点
    try {
      entries.value = await platformApi.getKnowledge();
    } catch {
      const legacy = await platformApi.getSkills();
      entries.value = legacy;
    }
  } catch {
    ElMessage.error("加载知识库失败");
  }
}

async function doSearch() {
  if (!searchQuery.value.trim()) {
    searchResults.value = [];
    return;
  }
  searching.value = true;
  try {
    searchResults.value = await platformApi.searchKnowledge(searchQuery.value.trim());
  } catch {
    ElMessage.error("搜索失败，请检查后端连接");
    searchResults.value = [];
  } finally {
    searching.value = false;
  }
}

function clearSearch() {
  searchQuery.value = "";
  searchResults.value = [];
}

// ─── 用户交互 ─────────────────────────────────────────────────────────────────
function openDialog(entry?: KnowledgeEntry) {
  editingId.value = entry?.id || null;
  form.name = entry?.name || "";
  form.description = entry?.description || "";
  form.action_template = entry?.action_template || "";
  form.applicable_context = entry?.applicable_context || "";
  form.keywords = (entry?.keywords || []).join(", ");
  triggersText.value = (entry?.triggers || []).join(", ");
  errorTypesText.value = (entry?.error_types || []).join(", ");
  dialogVisible.value = true;
}

async function submitEntry() {
  const payload = {
    ...form,
    keywords: form.keywords.split(",").map((k) => k.trim()).filter(Boolean),
    triggers: triggersText.value.split(",").map((k) => k.trim()).filter(Boolean),
    error_types: errorTypesText.value.split(",").map((k) => k.trim()).filter(Boolean),
  };
  try {
    if (editingId.value) {
      try {
        await platformApi.updateKnowledge(editingId.value, payload);
      } catch {
        await platformApi.updateSkill(editingId.value, payload);
      }
      ElMessage.success("知识条目已更新");
    } else {
      try {
        await platformApi.createKnowledge(payload);
      } catch {
        await platformApi.createSkill(payload);
      }
      ElMessage.success("知识条目已新增");
    }
    dialogVisible.value = false;
    fetchKnowledge();
  } catch {
    ElMessage.error("保存失败");
  }
}

async function viewDetail(id: string) {
  try {
    try {
      selectedEntry.value = (await platformApi.getKnowledgeDetail(id)) as unknown as Record<string, unknown>;
    } catch {
      selectedEntry.value = (await platformApi.getSkillDetail(id)) as Record<string, unknown>;
    }
    detailVisible.value = true;
  } catch {
    ElMessage.error("加载详情失败");
  }
}

async function retire(id: string) {
  try {
    try {
      await platformApi.retireKnowledge(id, "手动下架");
    } catch {
      await platformApi.retireSkill(id, "手动下架");
    }
    ElMessage.success("已下架");
    fetchKnowledge();
  } catch {
    ElMessage.error("操作失败");
  }
}

async function restore(id: string) {
  try {
    try {
      await platformApi.restoreKnowledge(id);
    } catch {
      await platformApi.restoreSkill(id);
    }
    ElMessage.success("已恢复");
    fetchKnowledge();
  } catch {
    ElMessage.error("操作失败");
  }
}

function matchTagType(type: string): "primary" | "success" | "warning" | "info" {
  const map: Record<string, "primary" | "success" | "warning" | "info"> = {
    hybrid: "primary",
    bm25: "warning",
    semantic: "success",
  };
  return map[type] || "info";
}

onMounted(fetchKnowledge);
</script>

<style scoped>
.search-bar {
  display: flex;
  gap: 10px;
  align-items: center;
}

.search-result-header {
  margin: 12px 0 8px;
  font-size: 12px;
  color: var(--text-secondary);
}

.search-results {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-height: 360px;
  overflow-y: auto;
}

.search-result-item {
  padding: 12px 14px;
  border-radius: var(--radius-md);
  background: var(--bg-card);
  border: 1px solid var(--border);
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}

.search-result-item:hover {
  border-color: var(--brand);
  background: var(--brand-light);
}

.result-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}

.result-name {
  font-weight: 600;
  font-size: 14px;
}

.result-badges {
  display: flex;
  gap: 6px;
}

.result-desc {
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.5;
}

.result-keywords {
  margin-top: 8px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.entry-keywords {
  margin-top: 10px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: center;
}

.kw-tag {
  cursor: default;
}

.more-kw {
  font-size: 11px;
  color: var(--text-secondary);
}

.entry-meta {
  display: flex;
  gap: 6px;
}

.related-bugs {
  margin-top: 8px;
}

.related-label {
  font-size: 12px;
  color: var(--text-secondary);
}

.detail-pre {
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
  font-size: 12px;
}
</style>
