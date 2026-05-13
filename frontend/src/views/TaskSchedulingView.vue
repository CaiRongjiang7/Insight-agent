<template>
  <ConsoleLayout>
    <div class="page-shell">
      <div class="page-header">
        <div>
          <div class="page-title">抽取任务管道</div>
          <div class="page-subtitle">可视化六层管道任务流转：事件采集 → 任务编排 → LLM抽取 → RAG增强 → 审核发布 → 知识检索。</div>
        </div>
        <div class="toolbar">
          <el-select v-model="priority" style="width: 130px">
            <el-option label="高优先级" value="high" />
            <el-option label="中优先级" value="medium" />
            <el-option label="低优先级" value="low" />
          </el-select>
          <el-button @click="batchUpdatePriority">调整优先级</el-button>
          <el-button type="primary" @click="fetchTasks()">刷新状态</el-button>
        </div>
      </div>

      <!-- 管道各阶段统计 -->
      <div class="pipeline-stages">
        <div
          v-for="(stage, idx) in pipelineStages"
          :key="stage.key"
          class="stage-card"
          :class="{ active: stage.count > 0 }"
        >
          <div class="stage-num">{{ stage.count }}</div>
          <div class="stage-label">{{ stage.label }}</div>
          <div v-if="idx < pipelineStages.length - 1" class="stage-arrow">→</div>
        </div>
      </div>

      <!-- 活跃抽取任务拓扑图 -->
      <PanelCard
        v-if="activeExtractionTasks.length"
        title="活跃抽取任务拓扑"
        description="当前正在执行的 Bug 抽取任务 Agent 协作图（最多展示 8 个）。"
      >
        <BaseChart :option="graphOption" style="height: 320px" />
      </PanelCard>

      <!-- 任务列表 -->
      <PanelCard title="抽取任务列表">
        <template #default>
          <div class="toolbar" style="margin-bottom: 12px">
            <el-select v-model="filterStage" clearable placeholder="管道阶段" style="width: 170px">
              <el-option label="事件采集" value="event_ingestion" />
              <el-option label="任务编排" value="orchestration" />
              <el-option label="LLM 抽取" value="extraction" />
              <el-option label="RAG 增强" value="rag_enhancement" />
              <el-option label="审核发布" value="review" />
              <el-option label="知识检索" value="knowledge_retrieval" />
            </el-select>
            <el-select v-model="filterStatus" clearable placeholder="任务状态" style="width: 150px">
              <el-option label="待处理" value="pending" />
              <el-option label="处理中" value="running" />
              <el-option label="已完成" value="done" />
              <el-option label="已失败" value="failed" />
            </el-select>
          </div>
          <el-table :data="filteredTasks" stripe @selection-change="onSelectionChange">
            <el-table-column type="selection" width="48" />
            <el-table-column prop="id" label="任务ID" min-width="160" show-overflow-tooltip />
            <el-table-column label="管道阶段" width="130">
              <template #default="{ row }">
                <el-tag size="small" :type="stageTagType(row.pipeline_stage)">
                  {{ row.pipeline_stage_label || stageName(row.pipeline_stage) || row.task_type_label }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="priority_label" label="优先级" width="90" />
            <el-table-column prop="master_agent" label="主 Agent" width="160" />
            <el-table-column label="子 Agent" min-width="160">
              <template #default="{ row }">{{ row.sub_agents?.join(", ") || "generalist" }}</template>
            </el-table-column>
            <el-table-column label="状态" width="120">
              <template #default="{ row }">
                <StatusTag :status="row.lifecycle_status" :label="row.lifecycle_status_label" />
              </template>
            </el-table-column>
            <el-table-column prop="token_usage" label="Token消耗" width="110" />
            <el-table-column label="质量分" width="90">
              <template #default="{ row }">
                <span v-if="row.extraction_score != null" :class="scoreClass(row.extraction_score)">
                  {{ Math.round(row.extraction_score * 100) }}
                </span>
                <span v-else class="text-muted">-</span>
              </template>
            </el-table-column>
            <el-table-column prop="created_at" label="创建时间" min-width="130" show-overflow-tooltip />
          </el-table>
        </template>
      </PanelCard>
    </div>
  </ConsoleLayout>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { ElMessage } from "element-plus";
import ConsoleLayout from "@/components/layout/ConsoleLayout.vue";
import PanelCard from "@/components/common/PanelCard.vue";
import BaseChart from "@/components/common/BaseChart.vue";
import StatusTag from "@/components/common/StatusTag.vue";
import { platformApi } from "@/api/platform";
import type { TaskRecord } from "@/types/platform";

// ─── 状态 ────────────────────────────────────────────────────────────────────
const tasks = ref<TaskRecord[]>([]);
const selectedIds = ref<string[]>([]);
const priority = ref("medium");
const filterStage = ref("");
const filterStatus = ref("");
const previousLifecycle = new Map<string, string>();
let pollTimer: number | undefined;

// 六层管道阶段定义
const PIPELINE_STAGES = [
  { key: "event_ingestion", label: "事件采集" },
  { key: "orchestration", label: "任务编排" },
  { key: "extraction", label: "LLM抽取" },
  { key: "rag_enhancement", label: "RAG增强" },
  { key: "review", label: "审核发布" },
  { key: "knowledge_retrieval", label: "知识检索" },
] as const;

// ─── 计算属性 ─────────────────────────────────────────────────────────────────
const pipelineStages = computed(() =>
  PIPELINE_STAGES.map((s) => ({
    ...s,
    count: tasks.value.filter(
      (t) => (t.pipeline_stage || t.task_type) === s.key && t.lifecycle_status === "running",
    ).length,
  })),
);

const activeExtractionTasks = computed(() =>
  tasks.value.filter((t) => ["running", "repairing"].includes(t.lifecycle_status)),
);

const filteredTasks = computed(() =>
  tasks.value.filter((t) => {
    const byStage = !filterStage.value || (t.pipeline_stage || t.task_type) === filterStage.value;
    const byStatus = !filterStatus.value || t.lifecycle_status.includes(filterStatus.value) || t.status.includes(filterStatus.value);
    return byStage && byStatus;
  }),
);

// ─── 拓扑图 ───────────────────────────────────────────────────────────────────
const graphOption = computed(() => {
  const nodes: Array<Record<string, unknown>> = [];
  const links: Array<Record<string, unknown>> = [];
  activeExtractionTasks.value.slice(0, 8).forEach((task, taskIndex) => {
    const originX = (taskIndex % 2) * 780;
    const originY = Math.floor(taskIndex / 2) * 380;
    const masterId = `master:${task.id}`;
    const taskId = `task:${task.id}`;
    nodes.push({ id: masterId, name: "管道编排器", category: 0, symbolSize: 72, x: originX, y: originY });
    nodes.push({
      id: taskId,
      name: task.incident_id ? `Bug#${task.incident_id.substring(0, 6)}` : task.id.substring(0, 8),
      category: 1,
      symbolSize: 58,
      x: originX + 240,
      y: originY,
    });
    links.push({ source: masterId, target: taskId });
    const agents = task.sub_agents?.length ? task.sub_agents : ["generalist"];
    agents.forEach((agent, agentIndex) => {
      const angle = (Math.PI * 2 * agentIndex) / Math.max(agents.length, 1);
      const nodeId = `${task.id}:${agent}:${agentIndex}`;
      nodes.push({
        id: nodeId,
        name: agentDisplayName(agent),
        category: 2,
        symbolSize: 44,
        x: originX + 460 + Math.cos(angle) * 150,
        y: originY + Math.sin(angle) * 120,
      });
      links.push({ source: taskId, target: nodeId });
    });
  });
  return {
    tooltip: {},
    legend: [
      {
        data: ["管道编排器", "抽取任务", "执行Agent"],
        textStyle: { color: "var(--text-secondary)" },
      },
    ],
    series: [
      {
        type: "graph",
        layout: "none",
        roam: true,
        categories: [
          { name: "管道编排器", itemStyle: { color: "#409eff" } },
          { name: "抽取任务", itemStyle: { color: "#67c23a" } },
          { name: "执行Agent", itemStyle: { color: "#e6a23c" } },
        ],
        data: nodes,
        links,
        label: { show: true, color: "#303133", fontSize: 11 },
        edgeSymbol: ["none", "arrow"],
        edgeSymbolSize: 8,
        lineStyle: { color: "#409eff", width: 2, curveness: 0.08 },
      },
    ],
  };
});

// ─── 数据加载 ─────────────────────────────────────────────────────────────────
async function fetchTasks(showCompletionMessage = false) {
  const nextTasks = await platformApi.getTasks();
  if (showCompletionMessage) {
    notifyCompletedTasks(nextTasks);
  }
  nextTasks.forEach((task) => previousLifecycle.set(task.id, task.lifecycle_status));
  tasks.value = nextTasks;
}

function notifyCompletedTasks(nextTasks: TaskRecord[]) {
  nextTasks.forEach((task) => {
    const previous = previousLifecycle.get(task.id);
    if (!previous || previous === task.lifecycle_status) return;
    if (task.lifecycle_status === "done" || task.lifecycle_status === "repair_success") {
      ElMessage.success(`任务 ${task.id.substring(0, 8)} 抽取完成`);
    }
    if (task.lifecycle_status === "failed" || task.lifecycle_status === "repair_failed") {
      ElMessage.error(`任务 ${task.id.substring(0, 8)} 抽取失败`);
    }
  });
}

function onSelectionChange(rows: TaskRecord[]) {
  selectedIds.value = rows.map((row) => row.id);
}

async function batchUpdatePriority() {
  if (!selectedIds.value.length) {
    ElMessage.warning("请先选中任务");
    return;
  }
  await platformApi.updateTaskPriority(selectedIds.value, priority.value);
  ElMessage.success("任务优先级已更新");
  fetchTasks();
}

// ─── 工具函数 ─────────────────────────────────────────────────────────────────
function stageName(key?: string) {
  const found = PIPELINE_STAGES.find((s) => s.key === key);
  return found?.label || key || "-";
}

function stageTagType(stage?: string): "primary" | "success" | "warning" | "info" | "danger" {
  const map: Record<string, "primary" | "success" | "warning" | "info" | "danger"> = {
    event_ingestion: "info",
    orchestration: "primary",
    extraction: "warning",
    rag_enhancement: "success",
    review: "primary",
    knowledge_retrieval: "success",
  };
  return map[stage || ""] || "info";
}

function agentDisplayName(agent: string) {
  const map: Record<string, string> = {
    EventClassifier: "事件分类",
    InfoExtractor: "信息抽取",
    ContextEnricher: "上下文增强",
    ReviewAgent: "审核Agent",
    PipelineOrchestrator: "管道编排",
    QualityScorer: "质量评分",
    generalist: "通用Agent",
  };
  return map[agent] || agent;
}

function scoreClass(score: number) {
  if (score >= 0.8) return "score-good";
  if (score >= 0.5) return "score-warn";
  return "score-bad";
}

// ─── 生命周期 ─────────────────────────────────────────────────────────────────
onMounted(async () => {
  await fetchTasks();
  pollTimer = window.setInterval(() => void fetchTasks(true), 8000);
});

onUnmounted(() => {
  if (pollTimer) window.clearInterval(pollTimer);
});
</script>

<style scoped>
.pipeline-stages {
  display: flex;
  align-items: center;
  gap: 0;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 16px 20px;
  overflow-x: auto;
}

.stage-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 8px 16px;
  min-width: 90px;
  position: relative;
  transition: all 0.2s;
}

.stage-card.active .stage-num {
  color: var(--brand);
}

.stage-num {
  font-size: 22px;
  font-weight: 700;
  color: var(--text-muted);
}

.stage-label {
  font-size: 12px;
  color: var(--text-muted);
  white-space: nowrap;
}

.stage-arrow {
  position: absolute;
  right: -8px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--text-muted);
  font-size: 16px;
  z-index: 1;
}

.score-good {
  color: var(--success);
  font-weight: 600;
}

.score-warn {
  color: var(--warning);
  font-weight: 600;
}

.score-bad {
  color: #f87171;
  font-weight: 600;
}

.text-muted {
  color: var(--text-secondary);
}
</style>
