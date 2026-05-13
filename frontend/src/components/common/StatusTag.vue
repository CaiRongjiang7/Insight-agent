<template>
  <span :class="['status-tag', `tag-${resolvedStatus}`]">{{ resolvedLabel }}</span>
</template>

<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{
  status?: string;
  label?: string;
}>();

const STATUS_MAP: Record<string, { label: string; cls: string }> = {
  draft:     { label: "草稿",   cls: "draft" },
  pending:   { label: "待审核", cls: "pending" },
  approved:  { label: "已通过", cls: "approved" },
  rejected:  { label: "已驳回", cls: "rejected" },
  published: { label: "已发布", cls: "published" },
  running:   { label: "运行中", cls: "running" },
  success:   { label: "成功",   cls: "approved" },
  failed:    { label: "失败",   cls: "rejected" },
  warning:   { label: "告警",   cls: "pending" },
};

const resolvedStatus = computed(() => STATUS_MAP[props.status || ""]?.cls || "draft");
const resolvedLabel  = computed(() => props.label || STATUS_MAP[props.status || ""]?.label || props.status || "-");
</script>

<style scoped>
.status-tag {
  display: inline-flex;
  align-items: center;
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 100px;
  border: 1px solid;
  white-space: nowrap;
}

.tag-draft {
  color: var(--text-secondary);
  background: var(--bg-subtle);
  border-color: var(--border);
}

.tag-pending {
  color: var(--warning);
  background: var(--warning-bg);
  border-color: var(--warning-border);
}

.tag-approved {
  color: var(--success);
  background: var(--success-bg);
  border-color: var(--success-border);
}

.tag-rejected {
  color: var(--danger);
  background: var(--danger-bg);
  border-color: var(--danger-border);
}

.tag-published {
  color: var(--brand);
  background: var(--brand-light);
  border-color: var(--brand-border);
}

.tag-running {
  color: var(--info);
  background: var(--info-bg);
  border-color: var(--info-border);
}
</style>
