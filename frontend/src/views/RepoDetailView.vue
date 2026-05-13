<template>
  <ConsoleLayout>
    <div class="page-shell">
      <div class="page-header">
        <div>
          <div class="page-title">{{ detail?.name || "仓库详情" }}</div>
          <div class="page-subtitle">{{ detail?.description }}</div>
        </div>
        <el-button @click="router.back()">返回列表</el-button>
      </div>

      <div class="stats-grid">
        <StatCard label="健康度" :value="detail?.health_score || 0" status="info" helper="综合仓库状态" />
        <StatCard label="关联任务数" :value="detail?.tasks?.length || 0" status="warning" helper="最近调度任务" />
        <StatCard label="CI 历史条目" :value="detail?.ci_history?.length || 0" status="danger" helper="最近异常轨迹" />
      </div>

      <div class="two-col">
        <PanelCard title="语言与图元信息">
          <el-descriptions :column="1" border>
            <el-descriptions-item label="本地路径">{{ detail?.local_path }}</el-descriptions-item>
            <el-descriptions-item label="语言分布">{{ detail?.language_breakdown }}</el-descriptions-item>
            <el-descriptions-item label="代码图元">{{ detail?.graph_metadata }}</el-descriptions-item>
          </el-descriptions>
        </PanelCard>
        <PanelCard title="近期任务">
          <el-table :data="detail?.tasks || []" size="small" stripe>
            <el-table-column prop="id" label="任务ID" min-width="180" />
            <el-table-column prop="task_type_label" label="类型" width="120" />
            <el-table-column prop="status_label" label="状态" width="100" />
          </el-table>
        </PanelCard>
      </div>

      <PanelCard title="CI 历史记录">
        <el-table :data="detail?.ci_history || []" stripe>
          <el-table-column prop="incident_id" label="异常ID" min-width="180" />
          <el-table-column prop="error_type" label="错误类型" width="140" />
          <el-table-column prop="status" label="状态" width="120" />
          <el-table-column prop="created_at" label="创建时间" min-width="180" />
        </el-table>
      </PanelCard>
    </div>
  </ConsoleLayout>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import ConsoleLayout from "@/components/layout/ConsoleLayout.vue";
import PanelCard from "@/components/common/PanelCard.vue";
import StatCard from "@/components/common/StatCard.vue";
import { platformApi } from "@/api/platform";

const route = useRoute();
const router = useRouter();
const detail = ref<Record<string, any> | null>(null);

async function fetchDetail() {
  detail.value = await platformApi.getRepoDetail(String(route.params.repoId));
}

onMounted(fetchDetail);
</script>
