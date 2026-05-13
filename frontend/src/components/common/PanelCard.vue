<template>
  <section class="panel-card">
    <header v-if="title || $slots.header" class="panel-header">
      <div class="panel-header-left">
        <div class="panel-title">{{ title }}</div>
        <div v-if="description" class="panel-desc">{{ description }}</div>
      </div>
      <div v-if="$slots.header" class="panel-header-right">
        <slot name="header" />
      </div>
    </header>
    <div class="panel-body">
      <slot />
    </div>
  </section>
</template>

<script setup lang="ts">
defineProps<{ title?: string; description?: string }>();
</script>

<style scoped>
.panel-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-xs);
  overflow: hidden;
}

.panel-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 16px 20px;
  border-bottom: 1px solid var(--border-light);
}

.panel-header-left { flex: 1; min-width: 0; }
.panel-header-right { flex-shrink: 0; }

.panel-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  letter-spacing: -0.01em;
}

.panel-desc {
  margin-top: 2px;
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.4;
}

.panel-body {
  padding: 20px;
}
</style>
