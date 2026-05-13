<template>
  <div class="app-shell">
    <!-- 侧边栏 -->
    <aside :class="['sidebar', { collapsed: appStore.collapsed }]">
      <!-- Logo -->
      <div class="sidebar-logo">
        <div class="logo-icon">
          <svg width="22" height="22" viewBox="0 0 22 22" fill="none">
            <rect width="22" height="22" rx="5" fill="#409eff"/>
            <path d="M6 11h10M11 6v10" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/>
          </svg>
        </div>
        <span v-if="!appStore.collapsed" class="logo-name">踩坑知识库</span>
      </div>

      <!-- 导航菜单 -->
      <nav class="sidebar-nav">
        <el-tooltip
          v-for="item in menus"
          :key="item.path"
          :content="item.label"
          placement="right"
          :disabled="!appStore.collapsed"
        >
          <router-link
            :to="item.path"
            :class="['nav-item', { active: route.path.startsWith(item.path) }]"
          >
            <el-icon class="nav-icon"><component :is="item.icon" /></el-icon>
            <span v-if="!appStore.collapsed" class="nav-label">{{ item.label }}</span>
          </router-link>
        </el-tooltip>
      </nav>

      <!-- 底部折叠按钮 -->
      <div class="sidebar-bottom">
        <button class="collapse-btn" @click="appStore.toggleSidebar()">
          <el-icon><DArrowLeft v-if="!appStore.collapsed" /><DArrowRight v-else /></el-icon>
          <span v-if="!appStore.collapsed" class="nav-label">收起侧边栏</span>
        </button>
      </div>
    </aside>

    <!-- 右侧主体 -->
    <div class="main-wrapper">
      <!-- 顶栏 -->
      <header class="topbar">
        <div class="topbar-left">
          <el-breadcrumb separator="/">
            <el-breadcrumb-item :to="{ path: '/' }">首页</el-breadcrumb-item>
            <el-breadcrumb-item>{{ (route.meta.title as string) || currentMenu?.label || "页面" }}</el-breadcrumb-item>
          </el-breadcrumb>
        </div>

        <div class="topbar-center">
          <div class="search-wrap">
            <el-icon class="search-icon"><Search /></el-icon>
            <input
              v-model="keyword"
              class="search-input"
              placeholder="搜索知识库 / Bug 报告 / 关键词..."
              @keyup.enter="handleSearch"
            />
          </div>
        </div>

        <div class="topbar-right">
          <el-tooltip content="通知中心">
            <button class="icon-btn" :class="{ 'has-badge': appStore.notifications.length > 0 }">
              <el-icon :size="17"><BellFilled /></el-icon>
              <span v-if="appStore.notifications.length" class="badge">{{ appStore.notifications.length }}</span>
            </button>
          </el-tooltip>

          <div class="user-chip">
            <div class="user-avatar">A</div>
            <span class="user-name">管理员</span>
          </div>
        </div>
      </header>

      <!-- 内容区 -->
      <main class="page-content">
        <slot />
      </main>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted } from "vue";
import { useRoute } from "vue-router";
import {
  BellFilled, Collection, Compass, Connection,
  DataAnalysis, Management, Search, Cpu,
  DArrowLeft, DArrowRight,
} from "@element-plus/icons-vue";
import { useAppStore } from "@/stores/app";

const appStore = useAppStore();
const route = useRoute();
const keyword = ref("");

const menus = [
  { path: "/dashboard",          label: "工作台",     icon: Compass },
  { path: "/ci-autofix",         label: "Bug 复盘",   icon: Cpu },
  { path: "/skill-library",      label: "踩坑知识库", icon: Collection },
  { path: "/task-scheduling",    label: "抽取管道",   icon: Connection },
  { path: "/repo-management",    label: "数据源管理", icon: Management },
  { path: "/context-management", label: "上下文",     icon: DataAnalysis },
  { path: "/observability",      label: "管道监控",   icon: DataAnalysis },
];

const currentMenu = computed(() => menus.find(m => route.path.startsWith(m.path)));

function handleSearch() {
  if (keyword.value.trim()) appStore.search(keyword.value);
}

onMounted(() => appStore.refreshGlobalState());
</script>

<style scoped>
/* ===== 整体布局 ===== */
.app-shell {
  display: flex;
  height: 100vh;
  overflow: hidden;
  background: var(--bg-page);
}

/* ===== 侧边栏 ===== */
.sidebar {
  width: var(--sidebar-w);
  min-width: var(--sidebar-w);
  height: 100vh;
  background: var(--sidebar-bg);
  border-right: 1px solid var(--sidebar-border, var(--border));
  display: flex;
  flex-direction: column;
  transition: width 0.2s ease, min-width 0.2s ease;
  overflow: hidden;
  flex-shrink: 0;
}

.sidebar.collapsed {
  width: var(--sidebar-w-collapsed);
  min-width: var(--sidebar-w-collapsed);
}

/* Logo 区 */
.sidebar-logo {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 14px;
  height: var(--topbar-h);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  overflow: hidden;
}

.logo-icon {
  width: 28px;
  height: 28px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}

.logo-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  white-space: nowrap;
  letter-spacing: 0;
}

/* 导航 */
.sidebar-nav {
  flex: 1;
  padding: 8px 8px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  overflow-y: auto;
  overflow-x: hidden;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 10px;
  border-radius: var(--radius-md);
  color: var(--sidebar-text);
  font-size: 13.5px;
  font-weight: 500;
  text-decoration: none;
  transition: background 0.15s, color 0.15s;
  white-space: nowrap;
  overflow: hidden;
}

.nav-item:hover {
  background: var(--sidebar-hover-bg);
  color: var(--text-primary);
}

.nav-item.active {
  background: var(--sidebar-active-bg);   /* #409eff */
  color: var(--sidebar-text-active);
}

.nav-icon {
  font-size: 16px;
  flex-shrink: 0;
}

.nav-label {
  font-size: 13.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 底部 */
.sidebar-bottom {
  padding: 8px;
  border-top: 1px solid var(--border);
  flex-shrink: 0;
}

.collapse-btn {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 9px 10px;
  border: none;
  background: transparent;
  color: var(--text-muted);
  font-size: 13px;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
  white-space: nowrap;
  overflow: hidden;
}

.collapse-btn:hover {
  background: var(--sidebar-hover-bg);
  color: var(--text-primary);
}

/* ===== 主体区域 ===== */
.main-wrapper {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-width: 0;
}

/* ===== 顶栏 ===== */
.topbar {
  height: var(--topbar-h);
  background: var(--topbar-bg);
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 0 20px;
  flex-shrink: 0;
}

.topbar-left {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}

.topbar-center {
  flex: 1;
  max-width: 380px;
}

.search-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--bg-subtle);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 0 12px;
  height: 32px;
  transition: border-color 0.15s, background 0.15s;
}

.search-wrap:focus-within {
  border-color: var(--brand);
  background: #fff;
}

.search-icon {
  color: var(--text-muted);
  font-size: 14px;
  flex-shrink: 0;
}

.search-input {
  flex: 1;
  border: none;
  outline: none;
  background: transparent;
  font-size: 13px;
  color: var(--text-primary);
  font-family: var(--font-sans);
}

.search-input::placeholder { color: var(--text-muted); }

.topbar-right {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-left: auto;
}

.icon-btn {
  position: relative;
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  border-radius: var(--radius-md);
  color: var(--text-secondary);
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}

.icon-btn:hover {
  background: var(--bg-subtle);
  color: var(--text-primary);
}

.badge {
  position: absolute;
  top: 3px;
  right: 3px;
  min-width: 15px;
  height: 15px;
  background: var(--danger);
  color: #fff;
  font-size: 10px;
  font-weight: 700;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0 3px;
}

.user-chip {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 4px 10px 4px 4px;
  border-radius: 100px;
  border: 1px solid var(--border);
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}

.user-chip:hover {
  border-color: var(--brand-border);
  background: var(--brand-light);
}

.user-avatar {
  width: 26px;
  height: 26px;
  border-radius: 50%;
  background: var(--brand);
  color: #fff;
  font-size: 12px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
}

.user-name {
  font-size: 13px;
  color: var(--text-secondary);
  font-weight: 500;
}

/* ===== 内容区 ===== */
.page-content {
  flex: 1;
  overflow-y: auto;
  padding: 20px 24px;
}
</style>
