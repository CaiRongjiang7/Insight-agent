<template>
  <ConsoleLayout>
    <div class="page-shell">
      <div class="page-header">
        <div>
          <div class="page-title">上下文管理</div>
          <div class="page-subtitle">Working Context、短期交互、长期记忆和动态上下文构建一体化查看。</div>
        </div>
        <div class="toolbar">
          <el-select v-model="repoId" placeholder="选择仓库" style="width: 220px" @change="fetchContexts">
            <el-option v-for="repo in contextData?.repos || []" :key="repo.id" :label="repo.name" :value="repo.id" />
          </el-select>
          <el-button @click="clearExpired">清理过期上下文</el-button>
          <el-button type="primary" @click="fetchContexts">刷新列表</el-button>
        </div>
      </div>

      <el-tabs v-model="activeTab">
        <el-tab-pane label="Working Context" name="working">
          <PanelCard title="当前上下文">
            <el-table :data="contextData?.working_contexts || []" stripe>
              <el-table-column prop="context_id" label="上下文ID" min-width="200" />
              <el-table-column prop="task_id" label="关联任务" min-width="160" />
              <el-table-column prop="repo_name" label="关联仓库" min-width="160" />
              <el-table-column prop="status" label="状态" width="120" />
              <el-table-column prop="size" label="上下文大小" width="120" />
            </el-table>
          </PanelCard>
        </el-tab-pane>
        <el-tab-pane label="短期交互" name="short">
          <PanelCard title="近7天短期上下文">
            <el-table :data="contextData?.short_term_contexts || []" stripe>
              <el-table-column prop="context_id" label="上下文ID" min-width="200" />
              <el-table-column prop="summary" label="摘要" min-width="280" />
              <el-table-column prop="created_at" label="创建时间" min-width="160" />
              <el-table-column prop="expires_at" label="过期时间" min-width="160" />
            </el-table>
          </PanelCard>
        </el-tab-pane>
        <el-tab-pane label="长期记忆" name="long">
          <PanelCard title="长期保存的经验">
            <el-table :data="contextData?.long_term_contexts || []" stripe>
              <el-table-column prop="context_id" label="记忆ID" min-width="200" />
              <el-table-column prop="repo_name" label="仓库" min-width="180" />
              <el-table-column prop="summary" label="内容摘要" min-width="320" />
              <el-table-column prop="created_at" label="创建时间" min-width="160" />
            </el-table>
          </PanelCard>
        </el-tab-pane>
        <el-tab-pane label="动态构建" name="graph">
          <PanelCard title="上下文构建可视化">
            <BaseChart :option="graphOption" />
          </PanelCard>
        </el-tab-pane>
      </el-tabs>
    </div>
  </ConsoleLayout>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
import ConsoleLayout from "@/components/layout/ConsoleLayout.vue";
import PanelCard from "@/components/common/PanelCard.vue";
import BaseChart from "@/components/common/BaseChart.vue";
import { platformApi } from "@/api/platform";
import type { ContextOverview } from "@/types/platform";

const activeTab = ref("working");
const repoId = ref("");
const contextData = ref<ContextOverview | null>(null);

async function fetchContexts() {
  contextData.value = await platformApi.getContexts(repoId.value || undefined);
}

async function clearExpired() {
  const result = await platformApi.clearExpiredContexts();
  ElMessage.success(`已清理 ${result.cleared} 条过期上下文（预估）`);
}

const graphOption = computed(() => ({
  tooltip: {},
  series: [
    {
      type: "graph",
      layout: "force",
      roam: true,
      label: { show: true, color: "#303133" },
      data: (contextData.value?.dynamic_graph.nodes || []).map((item) => ({ ...item, symbolSize: 42 })),
      links: contextData.value?.dynamic_graph.links || [],
      lineStyle: { color: "#409eff" },
    },
  ],
}));

onMounted(fetchContexts);
</script>
