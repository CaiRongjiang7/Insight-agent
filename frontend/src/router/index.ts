import { createRouter, createWebHistory, type RouteRecordRaw } from "vue-router";

const routes: RouteRecordRaw[] = [
  { path: "/", redirect: "/dashboard" },
  {
    path: "/dashboard",
    name: "dashboard",
    component: () => import("@/views/DashboardView.vue"),
    meta: { title: "工作台" },
  },
  {
    path: "/repo-management",
    name: "repo-management",
    component: () => import("@/views/RepoManagementView.vue"),
    meta: { title: "数据源管理" },
  },
  {
    path: "/repo-management/detail/:repoId",
    name: "repo-detail",
    component: () => import("@/views/RepoDetailView.vue"),
    meta: { title: "数据源详情" },
  },
  {
    path: "/ci-autofix",
    name: "ci-autofix",
    component: () => import("@/views/CiAutofixView.vue"),
    meta: { title: "Bug复盘工作台" },
  },
  {
    path: "/task-scheduling",
    name: "task-scheduling",
    component: () => import("@/views/TaskSchedulingView.vue"),
    meta: { title: "抽取任务管道" },
  },
  {
    path: "/skill-library",
    name: "skill-library",
    component: () => import("@/views/SkillLibraryView.vue"),
    meta: { title: "踩坑知识库" },
  },
  {
    path: "/context-management",
    name: "context-management",
    component: () => import("@/views/ContextManagementView.vue"),
    meta: { title: "上下文管理" },
  },
  {
    path: "/observability",
    name: "observability",
    component: () => import("@/views/ObservabilityView.vue"),
    meta: { title: "管道监控" },
  },
];

export const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
});
