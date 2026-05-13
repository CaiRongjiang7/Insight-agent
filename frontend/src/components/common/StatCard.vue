<template>
  <div class="stat-card" @click="$emit('click')">
    <div class="stat-top">
      <span class="stat-label">{{ label }}</span>
      <span :class="['stat-badge', `badge-${status || 'info'}`]">{{ statusLabel }}</span>
    </div>
    <div class="stat-value">{{ value }}</div>
    <div class="stat-bottom">
      <span class="stat-helper">{{ helper }}</span>
      <slot />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{
  label: string;
  value: string | number;
  helper?: string;
  status?: "success" | "warning" | "danger" | "info";
}>();

defineEmits<{ click: [] }>();

const statusLabel = computed(() =>
  ({
    success: "正常",
    warning: "关注",
    danger: "告警",
    info:    "运行中",
  } as const)[props.status || "info"] ?? "运行中",
);
</script>

<style scoped>
.stat-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-xs);
  padding: 20px;
  cursor: pointer;
  transition: border-color 0.15s, box-shadow 0.15s;
  display: flex;
  flex-direction: column;
  gap: 0;
}

.stat-card:hover {
  border-color: var(--brand-border);
  box-shadow: var(--shadow-sm);
}

.stat-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.stat-label {
  font-size: 13px;
  color: var(--text-secondary);
  font-weight: 500;
}

/* 状态徽标 — 纯色小标签，无渐变 */
.stat-badge {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 100px;
  border: 1px solid;
  white-space: nowrap;
}

.badge-success {
  color: var(--success);
  background: var(--success-bg);
  border-color: var(--success-border);
}

.badge-warning {
  color: var(--warning);
  background: var(--warning-bg);
  border-color: var(--warning-border);
}

.badge-danger {
  color: var(--danger);
  background: var(--danger-bg);
  border-color: var(--danger-border);
}

.badge-info {
  color: var(--brand);
  background: var(--brand-light);
  border-color: var(--brand-border);
}

.stat-value {
  margin: 16px 0 12px;
  font-size: 30px;
  font-weight: 700;
  color: var(--text-primary);
  letter-spacing: -0.02em;
  line-height: 1;
}

.stat-bottom {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.stat-helper {
  font-size: 12px;
  color: var(--text-muted);
}
</style>
