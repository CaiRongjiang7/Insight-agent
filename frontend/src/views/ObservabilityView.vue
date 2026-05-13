<template>
  <ConsoleLayout>
    <div class="page-shell">
      <div class="page-header">
        <div>
          <div class="page-title">管道监控</div>
          <div class="page-subtitle">六层管道各层延迟、成功率、Token 消耗和降级触发率实时追踪。</div>
        </div>
        <div class="toolbar">
          <div class="seg-group">
            <button
              v-for="opt in dayOptions"
              :key="opt.value"
              :class="['seg-btn', { active: days === opt.value }]"
              @click="days = opt.value; fetchData()"
            >{{ opt.label }}</button>
          </div>
          <el-button @click="thresholdVisible = true">设置监控阈值</el-button>
          <el-button type="primary" @click="fetchData">刷新数据</el-button>
        </div>
      </div>

      <!-- 核心指标卡 -->
      <div class="stats-grid">
        <StatCard
          label="抽取成功率"
          :value="pct(snapshot?.summary.extraction_success_rate || snapshot?.summary.repair_success_rate)"
          status="success"
          helper="LLM 抽取输出符合 Schema 的比率"
        />
        <StatCard
          label="知识命中率"
          :value="pct(snapshot?.summary.knowledge_hit_rate || snapshot?.summary.skill_hit_rate)"
          status="info"
          helper="RAG 召回后命中历史相似案例的比率"
        />
        <StatCard
          label="审核通过率"
          :value="pct(snapshot?.summary.review_approval_rate)"
          status="success"
          helper="人工审核通过的比率"
        />
        <StatCard
          label="Wiki 发布率"
          :value="pct(snapshot?.summary.wiki_publish_rate)"
          status="info"
          helper="审核通过后发布到知识库的比率"
        />
        <StatCard
          label="降级触发率"
          :value="pct(snapshot?.summary.fallback_trigger_rate)"
          status="warning"
          helper="LLM 失败后触发启发式降级的比率"
        />
        <StatCard
          label="检索平均得分"
          :value="(snapshot?.summary.avg_retrieval_score || 0).toFixed(3)"
          status="info"
          helper="向量+BM25混合检索命中质量"
        />
      </div>

      <!-- 管道各层延迟 -->
      <PanelCard title="管道各层 P50 延迟（ms）" description="六层架构各层平均处理耗时，超时可能导致降级。">
        <BaseChart :option="latencyOption" style="height: 220px" />
      </PanelCard>

      <!-- 趋势图 2x4 -->
      <div class="two-col">
        <PanelCard title="抽取成功率趋势">
          <BaseChart :option="extractionChart" />
        </PanelCard>
        <PanelCard title="知识命中率趋势">
          <BaseChart :option="knowledgeChart" />
        </PanelCard>
        <PanelCard title="审核通过率趋势">
          <BaseChart :option="reviewChart" />
        </PanelCard>
        <PanelCard title="检索得分趋势">
          <BaseChart :option="retrievalChart" />
        </PanelCard>
        <PanelCard title="降级触发率趋势">
          <BaseChart :option="fallbackChart" />
        </PanelCard>
        <PanelCard title="Wiki 发布率趋势">
          <BaseChart :option="wikiChart" />
        </PanelCard>
        <PanelCard title="Token 消耗趋势">
          <BaseChart :option="tokenChart" />
        </PanelCard>
        <PanelCard title="知识库增长趋势（兼容旧指标）">
          <BaseChart :option="legacyChart" />
        </PanelCard>
      </div>

      <!-- 管道状态 -->
      <PanelCard title="知识演化管道状态">
        <el-descriptions :column="3" border>
          <el-descriptions-item label="运行模式">{{ learningMode }}</el-descriptions-item>
          <el-descriptions-item label="Worker 存活">
            <el-tag :type="workerAlive ? 'success' : 'danger'" size="small">{{ workerAlive ? "在线" : "离线" }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="进行中批次">{{ inflightCount }}</el-descriptions-item>
          <el-descriptions-item label="已完成批次">{{ completedBatches }}</el-descriptions-item>
          <el-descriptions-item label="失败批次">{{ failedBatches }}</el-descriptions-item>
          <el-descriptions-item label="上次运行">{{ lastRunAt }}</el-descriptions-item>
        </el-descriptions>
      </PanelCard>

      <!-- 异常指标 -->
      <PanelCard title="异常指标列表" description="超过阈值的指标会在此处展示。">
        <el-table :data="snapshot?.anomalies || []" stripe>
          <el-table-column prop="metric_label" label="指标名称" min-width="160" />
          <el-table-column prop="current_value" label="当前值" width="120" />
          <el-table-column prop="warning_threshold" label="警告阈值" width="120" />
          <el-table-column prop="danger_threshold" label="危险阈值" width="120" />
          <el-table-column label="等级" width="90">
            <template #default="{ row }">
              <el-tag :type="row.level === 'danger' ? 'danger' : 'warning'" size="small">{{ row.level }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="created_at" label="异常时间" min-width="180" />
        </el-table>
      </PanelCard>

      <!-- 阈值设置对话框 -->
      <el-dialog v-model="thresholdVisible" title="设置监控阈值" width="640px">
        <el-form label-width="200px">
          <el-form-item v-for="(item, key) in thresholdDraft" :key="key" :label="String(key)">
            <div class="toolbar">
              <span style="min-width: 50px; font-size: 12px; color: var(--text-secondary)">警告</span>
              <el-input-number v-model="item.warning" :step="0.05" :min="0" :max="1" :precision="2" />
              <span style="min-width: 50px; font-size: 12px; color: var(--text-secondary)">危险</span>
              <el-input-number v-model="item.danger" :step="0.05" :min="0" :max="1" :precision="2" />
            </div>
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="thresholdVisible = false">取消</el-button>
          <el-button type="primary" @click="saveThresholds">保存</el-button>
        </template>
      </el-dialog>
    </div>
  </ConsoleLayout>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
import ConsoleLayout from "@/components/layout/ConsoleLayout.vue";
import PanelCard from "@/components/common/PanelCard.vue";
import StatCard from "@/components/common/StatCard.vue";
import BaseChart from "@/components/common/BaseChart.vue";
import { platformApi } from "@/api/platform";
import type { ObservabilitySnapshot } from "@/types/platform";

// ─── 状态 ────────────────────────────────────────────────────────────────────
const days = ref(30);
const snapshot = ref<ObservabilitySnapshot | null>(null);
const thresholdVisible = ref(false);
const thresholdDraft = ref<Record<string, { warning: number; danger: number }>>({});
let refreshTimer: number | null = null;

const dayOptions = [
  { label: "近7天", value: 7 },
  { label: "近30天", value: 30 },
  { label: "近90天", value: 90 },
];

// ─── 数据加载 ─────────────────────────────────────────────────────────────────
async function fetchData() {
  try {
    snapshot.value = await platformApi.getObservability(days.value);
    thresholdDraft.value = JSON.parse(JSON.stringify(snapshot.value.thresholds || {}));
  } catch {
    // 接口未实现时静默降级
  }
}

async function saveThresholds() {
  await platformApi.updateThresholds(thresholdDraft.value);
  ElMessage.success("监控阈值已更新");
  thresholdVisible.value = false;
  fetchData();
}

// ─── 工具函数 ─────────────────────────────────────────────────────────────────
function pct(val?: number) {
  return `${Math.round((val || 0) * 100)}%`;
}

function lineOption(seriesName: string, seriesData: number[], type: "line" | "bar" = "line") {
  return {
    tooltip: { trigger: "axis", backgroundColor: "#fff", borderColor: "#dcdfe6", textStyle: { color: "#303133" } },
    grid: { left: 8, right: 8, top: 16, bottom: 8, containLabel: true },
    xAxis: {
      type: "category",
      data: snapshot.value?.charts.labels || [],
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
      {
        name: seriesName,
        type,
        smooth: type === "line",
        symbol: "none",
        lineStyle: { width: 2, color: "#409eff" },
        itemStyle: { color: "#409eff", borderRadius: type === "bar" ? [3, 3, 0, 0] : 0 },
        data: seriesData,
      },
    ],
  };
}

// ─── 管道各层延迟柱状图 ───────────────────────────────────────────────────────
const latencyOption = computed(() => {
  const lat = snapshot.value?.pipeline_latency;
  if (!lat) return { series: [] };
  const data = [
    { name: "事件采集", value: lat.event_ingestion_ms },
    { name: "任务编排", value: lat.orchestration_ms },
    { name: "LLM抽取", value: lat.extraction_ms },
    { name: "RAG增强", value: lat.rag_enhancement_ms },
    { name: "审核发布", value: lat.review_ms },
  ];
  return {
    tooltip: { trigger: "axis" },
    xAxis: {
      type: "category",
      data: data.map((d) => d.name),
      axisLabel: { color: "var(--text-secondary)" },
    },
    yAxis: {
      type: "value",
      name: "ms",
      axisLabel: { color: "var(--text-secondary)" },
    },
    series: [
      {
        type: "bar",
        barWidth: 36,
        itemStyle: {
          color: (params: { dataIndex: number }) => {
            const colors = ["#409eff", "#67c23a", "#e6a23c", "#909399", "#f56c6c"];
            return colors[params.dataIndex % colors.length];
          },
          borderRadius: [4, 4, 0, 0],
        },
        data: data.map((d) => d.value || 0),
        label: {
          show: true,
          position: "top",
          color: "var(--text-secondary)",
          formatter: (params: { value: number }) => `${params.value}ms`,
        },
      },
    ],
  };
});

// ─── 各趋势图 ─────────────────────────────────────────────────────────────────
const charts = computed(() => snapshot.value?.charts);
const extractionChart = computed(() =>
  lineOption("抽取成功率", charts.value?.extraction_success_rate || charts.value?.repair_success_rate || []),
);
const knowledgeChart = computed(() =>
  lineOption("知识命中率", charts.value?.knowledge_hit_rate || charts.value?.skill_hit_rate || []),
);
const reviewChart = computed(() => lineOption("审核通过率", charts.value?.review_approval_rate || []));
const retrievalChart = computed(() => lineOption("检索得分", charts.value?.avg_retrieval_score || []));
const fallbackChart = computed(() => lineOption("降级触发率", charts.value?.fallback_trigger_rate || []));
const wikiChart = computed(() => lineOption("Wiki发布率", charts.value?.wiki_publish_rate || []));
const tokenChart = computed(() => lineOption("Token消耗", charts.value?.token_usage || [], "bar"));
const legacyChart = computed(() => lineOption("任务链路长度", charts.value?.task_chain_length || charts.value?.agent_llm_reasoning_rate || [], "bar"));

// ─── 管道状态 ─────────────────────────────────────────────────────────────────
const learningStatus = computed(() => (snapshot.value?.learning_status || {}) as Record<string, unknown>);
const learningMode = computed(() => String(learningStatus.value.mode || "knowledge_evolution"));
const workerAlive = computed(() => Boolean(learningStatus.value.worker_alive));
const inflightCount = computed(() =>
  Array.isArray(learningStatus.value.inflight_trace_ids) ? learningStatus.value.inflight_trace_ids.length : 0,
);
const completedBatches = computed(() => Number(learningStatus.value.completed_batches || 0));
const failedBatches = computed(() => Number(learningStatus.value.failed_batches || 0));
const lastRunAt = computed(() => String(learningStatus.value.last_run_at || "-"));

// ─── 生命周期 ─────────────────────────────────────────────────────────────────
onMounted(() => {
  fetchData();
  refreshTimer = window.setInterval(fetchData, 15000);
});

onBeforeUnmount(() => {
  if (refreshTimer !== null) window.clearInterval(refreshTimer);
});
</script>

<style scoped>
.seg-group {
  display: flex;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
}

.seg-btn {
  padding: 5px 14px;
  font-size: 13px;
  font-weight: 500;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
  font-family: var(--font-sans);
}

.seg-btn + .seg-btn { border-left: 1px solid var(--border); }

.seg-btn.active {
  background: var(--brand);
  color: #fff;
}

.seg-btn:not(.active):hover {
  background: var(--bg-subtle);
  color: var(--text-primary);
}
</style>
