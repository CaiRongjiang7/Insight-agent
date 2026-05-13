<template>
  <ConsoleLayout>
    <div class="page-shell">
      <!-- 页头 -->
      <div class="page-header">
        <div>
          <h1 class="page-title">工作台</h1>
          <p class="page-subtitle">Bug 复盘沉淀管道总览 — 追踪抽取时效、审核进度、知识库增长与 Token 消耗</p>
        </div>
        <div class="toolbar">
          <div class="seg-group">
            <button
              v-for="opt in dayOptions"
              :key="opt.value"
              :class="['seg-btn', { active: days === opt.value }]"
              @click="days = opt.value; fetchOverview()"
            >{{ opt.label }}</button>
          </div>
          <el-button @click="fetchOverview" :loading="loading" size="small">刷新</el-button>
          <el-button v-if="overview" size="small" @click="resetDemo" :loading="resetting">重置示例</el-button>
        </div>
      </div>

      <!-- 骨架屏 -->
      <template v-if="loading && !overview">
        <div class="stats-grid">
          <div v-for="i in 6" :key="i" class="skeleton-card" />
        </div>
      </template>

      <template v-else>
        <!-- 新手引导卡 -->
        <div v-if="showGuide" class="guide-banner">
          <div class="guide-left">
            <div class="guide-icon">🎯</div>
            <div class="guide-body">
              <div class="guide-title">欢迎使用踩坑知识库沉淀平台</div>
              <div class="guide-desc">
                以下示例数据基于 <a :href="demoInfo?.repo_url" target="_blank">{{ demoInfo?.repo }}</a>，涵盖完整的 Bug 复盘 → LLM 抽取 → 人工审核 → 发布知识库流程。可随时点击右上角「重置示例」重新开始。
              </div>
              <div class="guide-flow">
                <span v-for="(step, idx) in (demoInfo?.flow || defaultFlow)" :key="step.step" class="flow-item">
                  <span class="flow-num">{{ step.step }}</span>
                  <span class="flow-label">{{ step.label }}</span>
                  <span v-if="idx < (demoInfo?.flow || defaultFlow).length - 1" class="flow-arrow">→</span>
                </span>
              </div>
            </div>
          </div>
          <div class="guide-actions">
            <el-button type="primary" @click="router.push('/ci-autofix')">Bug 复盘工作台</el-button>
            <el-button @click="router.push('/skill-library')">踩坑知识库</el-button>
            <el-button text @click="dismissGuide">不再显示</el-button>
          </div>
        </div>

        <!-- 核心指标卡 -->
        <div class="stats-grid">
          <StatCard
            v-for="item in overview?.hero_metrics || []"
            :key="item.key"
            :label="item.label"
            :value="item.value"
            :status="item.status as any"
            helper="点击查看详情"
            @click="router.push(item.route)"
          >
            <button class="link-btn" @click.stop="router.push(item.route)">查看 →</button>
          </StatCard>
        </div>

        <!-- 图表行 -->
        <div class="two-col">
          <PanelCard title="沉淀质量综合评分" description="抽取置信度、审核通过率、知识命中率和来源覆盖率的综合得分">
            <div class="chart-surface">
              <BaseChart :option="healthOption" />
            </div>
            <p class="score-summary">{{ overview?.health_score?.summary }}</p>
          </PanelCard>

          <PanelCard title="六层管道流转" description="事件采集 → 任务编排 → LLM 抽取 → RAG 增强 → 审核发布 → 知识检索">
            <div class="chart-surface">
              <BaseChart :option="pipelineOption" />
            </div>
          </PanelCard>
        </div>

        <div class="two-col">
          <PanelCard title="核心趋势" description="近期抽取成功率、知识命中率、Token 消耗和平均审核时效走势">
            <div class="chart-surface">
              <BaseChart :option="trendOption" />
            </div>
          </PanelCard>

          <PanelCard title="高频踩坑条目 TOP 5" description="按召回次数和审核通过率排序">
            <el-table
              :data="overview?.top_knowledge || overview?.top_skills || []"
              size="small"
              :show-header="true"
            >
              <el-table-column prop="name" label="知识条目" min-width="140" />
              <el-table-column label="命中率" width="80" align="right">
                <template #default="{ row }">
                  <span class="rate-text">{{ Math.round((row.hit_rate || 0) * 100) }}%</span>
                </template>
              </el-table-column>
              <el-table-column label="成功率" width="80" align="right">
                <template #default="{ row }">
                  <span class="rate-text">{{ Math.round((row.success_rate || 0) * 100) }}%</span>
                </template>
              </el-table-column>
              <el-table-column label="状态" width="90">
                <template #default="{ row }">
                  <StatusTag :status="row.status" :label="row.status_label" />
                </template>
              </el-table-column>
            </el-table>
          </PanelCard>
        </div>

        <!-- 近期 Bug 复盘任务 -->
        <PanelCard title="近期 Bug 复盘任务" description="点击行跳转到 Bug 复盘工作台查看详情和审核操作">
          <el-table
            :data="overview?.recent_bug_reports || overview?.recent_incidents || []"
            @row-click="openBugReport"
            size="small"
            class="clickable-table"
          >
            <el-table-column prop="id" label="任务 ID" width="160">
              <template #default="{ row }">
                <span class="mono-text">{{ row.id }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="repo_name" label="仓库" min-width="160" />
            <el-table-column label="Bug 类型" width="130">
              <template #default="{ row }">
                {{ row.bug_category_label || row.error_type_label || row.bug_category || "-" }}
              </template>
            </el-table-column>
            <el-table-column label="来源" width="80">
              <template #default="{ row }">
                <span :class="['source-tag', `source-${row.source_type}`]">
                  {{ sourceLabel(row.source_type) }}
                </span>
              </template>
            </el-table-column>
            <el-table-column label="审核状态" width="100">
              <template #default="{ row }">
                <StatusTag
                  :status="row.review_status || row.status"
                  :label="reviewStatusLabel(row.review_status) || row.status_label"
                />
              </template>
            </el-table-column>
            <el-table-column prop="created_at" label="创建时间" min-width="160" />
          </el-table>
        </PanelCard>
      </template>
    </div>
  </ConsoleLayout>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { platformApi } from "@/api/platform";
import type { BugReport, DashboardOverview } from "@/types/platform";
import ConsoleLayout from "@/components/layout/ConsoleLayout.vue";
import PanelCard from "@/components/common/PanelCard.vue";
import StatCard from "@/components/common/StatCard.vue";
import StatusTag from "@/components/common/StatusTag.vue";
import BaseChart from "@/components/common/BaseChart.vue";

const router = useRouter();
const loading = ref(false);
const resetting = ref(false);
const days = ref(7);
const overview = ref<DashboardOverview | null>(null);
const demoInfo = ref<{ repo: string; repo_url: string; description: string; bug_reports: number; knowledge_entries: number; flow: Array<{ step: number; label: string; desc: string }> } | null>(null);
const showGuide = ref(!localStorage.getItem("dashboard-guide-dismissed"));

const dayOptions = [
  { label: "近 7 天", value: 7 },
  { label: "近 30 天", value: 30 },
];

const defaultFlow = [
  { step: 1, label: "PR 接收", desc: "" },
  { step: 2, label: "LLM 抽取", desc: "" },
  { step: 3, label: "人工审核", desc: "" },
  { step: 4, label: "发布知识库", desc: "" },
];

function dismissGuide() {
  showGuide.value = false;
  localStorage.setItem("dashboard-guide-dismissed", "1");
}

async function resetDemo() {
  resetting.value = true;
  try {
    await platformApi.resetDemo();
    ElMessage.success("示例数据已重置");
    await fetchOverview();
  } catch (e: any) {
    ElMessage.warning(e?.response?.data?.detail || "重置功能仅在 demo 模式下可用");
  } finally {
    resetting.value = false;
  }
}

async function fetchOverview() {
  loading.value = true;
  try {
    overview.value = await platformApi.getDashboard(days.value);
  } finally {
    loading.value = false;
  }
}

function openBugReport(row: BugReport) {
  router.push(`/ci-autofix?bugReportId=${row.id}`);
}

function sourceLabel(type?: string) {
  const map: Record<string, string> = { pr: "PR", log: "日志", chat: "群聊", manual: "手动" };
  return map[type || ""] || "未知";
}

function reviewStatusLabel(status?: string) {
  const map: Record<string, string> = {
    draft: "草稿", pending: "待审核", approved: "已通过",
    rejected: "已驳回", published: "已发布",
  };
  return status ? (map[status] || status) : "";
}

/* ---- 图表配置 ---- */
const CHART_COLORS = ["#409eff", "#67c23a", "#e6a23c", "#f56c6c", "#7c3aed", "#0284c7"];

const healthOption = computed(() => ({
  series: [{
    type: "gauge",
    radius: "85%",
    progress: { show: true, width: 10, itemStyle: { color: "#409eff" } },
    axisLine: { lineStyle: { width: 10, color: [[1, "#e2e8f0"]] } },
    axisTick: { show: false },
    splitLine: { show: false },
    axisLabel: { show: false },
    pointer: { itemStyle: { color: "#409eff" } },
    detail: {
      valueAnimation: true,
      formatter: "{value}",
      fontSize: 28,
      fontWeight: 700,
      color: "#303133",
      offsetCenter: [0, "20%"],
    },
    title: { offsetCenter: [0, "50%"], color: "#909399", fontSize: 12 },
    data: [{ value: overview.value?.health_score?.value || 0, name: "综合评分" }],
  }],
}));

const trendOption = computed(() => {
  const bundle = overview.value?.trend_bundle;
  return {
    tooltip: { trigger: "axis", backgroundColor: "#fff", borderColor: "#dcdfe6", textStyle: { color: "#303133" } },
    legend: { textStyle: { color: "#606266" }, bottom: 0 },
    grid: { left: 8, right: 8, top: 32, bottom: 40, containLabel: true },
    xAxis: {
      type: "category",
      data: bundle?.labels || [],
      axisLabel: { color: "#909399", fontSize: 11 },
      axisLine: { lineStyle: { color: "#dcdfe6" } },
      axisTick: { show: false },
    },
    yAxis: {
      type: "value",
      axisLabel: { color: "#909399", fontSize: 11 },
      splitLine: { lineStyle: { color: "#f5f7fa" } },
    },
    series: [
      { name: "抽取成功率", type: "line", smooth: true, symbol: "none", lineStyle: { width: 2 }, color: "#409eff", data: bundle?.extraction_success_rate || bundle?.repair_success_rate || [] },
      { name: "知识命中率", type: "line", smooth: true, symbol: "none", lineStyle: { width: 2 }, color: "#67c23a", data: bundle?.knowledge_hit_rate || bundle?.skill_hit_rate || [] },
      { name: "Token 消耗", type: "bar", barWidth: 6, color: "#e2e8f0", data: bundle?.token_usage || [] },
      { name: "审核时效(h)", type: "line", smooth: true, symbol: "none", lineStyle: { width: 2, type: "dashed" }, color: "#e6a23c", data: bundle?.avg_review_time_hours || bundle?.task_chain_length || [] },
    ],
  };
});

const pipelineOption = computed(() => {
  const nodes = overview.value?.pipeline_loop?.nodes || overview.value?.dream_loop?.nodes || [];
  return {
    tooltip: { backgroundColor: "#fff", borderColor: "#dcdfe6", textStyle: { color: "#303133" } },
    grid: { left: 8, right: 8, top: 16, bottom: 8, containLabel: true },
    xAxis: {
      type: "category",
      data: nodes.map((n: any) => n.name),
      axisLabel: { color: "#909399", fontSize: 11, interval: 0, rotate: 10 },
      axisLine: { lineStyle: { color: "#dcdfe6" } },
      axisTick: { show: false },
    },
    yAxis: {
      type: "value",
      axisLabel: { color: "#909399", fontSize: 11 },
      splitLine: { lineStyle: { color: "#f5f7fa" } },
    },
    series: [{
      type: "bar",
      barWidth: 24,
      itemStyle: {
        color: (p: { dataIndex: number }) => CHART_COLORS[p.dataIndex % CHART_COLORS.length],
        borderRadius: [4, 4, 0, 0],
      },
      data: nodes.map((n: any) => n.value),
    }],
  };
});

onMounted(async () => {
  await fetchOverview();
  try { demoInfo.value = await platformApi.getDemoInfo(); } catch { /* 降级 */ }
});
</script>

<style scoped>
/* 新手引导卡 */
.guide-banner {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  background: var(--brand-light);
  border: 1px solid var(--brand-border);
  border-radius: var(--radius-lg);
  padding: 18px 20px;
  gap: 16px;
}

.guide-left { display: flex; gap: 14px; flex: 1; min-width: 0; }
.guide-icon { font-size: 24px; flex-shrink: 0; line-height: 1.3; }
.guide-body { flex: 1; min-width: 0; }

.guide-title {
  font-size: 14px; font-weight: 600;
  color: var(--text-primary); margin-bottom: 6px;
}

.guide-desc {
  font-size: 13px; color: var(--text-secondary);
  line-height: 1.5; margin-bottom: 10px;
}

.guide-desc a { color: var(--brand); font-weight: 500; }

.guide-flow {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
}

.flow-item { display: flex; align-items: center; gap: 4px; font-size: 12px; }

.flow-num {
  width: 18px; height: 18px; border-radius: 50%;
  background: var(--brand); color: #fff;
  font-size: 10px; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
}

.flow-label { color: var(--text-secondary); font-weight: 500; }
.flow-arrow { color: var(--text-muted); font-size: 14px; margin: 0 2px; }

.guide-actions {
  display: flex; align-items: center; gap: 8px; flex-shrink: 0;
}

/* 分段控件 */
.seg-group {
  display: flex; border: 1px solid var(--border);
  border-radius: var(--radius); overflow: hidden;
}

.seg-btn {
  padding: 5px 14px; font-size: 13px; font-weight: 500;
  border: none; background: transparent;
  color: var(--text-secondary); cursor: pointer;
  transition: background 0.15s, color 0.15s;
}

.seg-btn + .seg-btn { border-left: 1px solid var(--border); }

.seg-btn.active { background: var(--brand); color: #fff; }

.seg-btn:not(.active):hover {
  background: var(--bg-subtle); color: var(--text-primary);
}

/* 骨架屏 */
.skeleton-card {
  height: 130px; border-radius: var(--radius-md);
  background: var(--bg-subtle);
  animation: pulse 1.5s ease-in-out infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}

/* 图表 */
.chart-surface { height: 240px; }

/* 评分摘要 */
.score-summary {
  margin: 8px 0 0; font-size: 12px;
  color: var(--text-muted); line-height: 1.5;
}

/* 表格 */
.clickable-table :deep(tr) { cursor: pointer; }

.mono-text {
  font-family: var(--font-mono); font-size: 12px;
  color: var(--text-secondary);
}

.rate-text {
  font-size: 13px; font-weight: 600; color: var(--text-primary);
}

/* 来源标签 */
.source-tag {
  font-size: 11px; font-weight: 600; padding: 2px 7px;
  border-radius: 100px; border: 1px solid;
}

.source-pr      { color: var(--brand);   background: var(--brand-light);   border-color: var(--brand-border); }
.source-log     { color: var(--warning); background: var(--warning-bg);    border-color: var(--warning-border); }
.source-chat    { color: var(--success); background: var(--success-bg);    border-color: var(--success-border); }
.source-manual  { color: var(--text-secondary); background: var(--bg-subtle); border-color: var(--border); }

/* 跳转按钮 */
.link-btn {
  font-size: 12px; color: var(--brand); background: none;
  border: none; cursor: pointer; padding: 0; font-weight: 500;
}

.link-btn:hover { color: var(--brand-hover); }
</style>