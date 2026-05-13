import { createI18n } from "vue-i18n";

const messages = {
  zh: {
    appName: "踩坑知识库沉淀平台",
    dashboard: "工作台",
    repos: "数据源管理",
    ci: "Bug复盘工作台",
    scheduling: "抽取任务管道",
    skills: "踩坑知识库",
    contexts: "上下文管理",
    observability: "管道监控",
  },
  en: {
    appName: "Bug Review Knowledge Platform",
    dashboard: "Dashboard",
    repos: "Data Sources",
    ci: "Bug Review",
    scheduling: "Extraction Pipeline",
    skills: "Knowledge Base",
    contexts: "Context Management",
    observability: "Pipeline Monitor",
  },
};

export const i18n = createI18n({
  legacy: false,
  locale: "zh",
  fallbackLocale: "en",
  messages,
});
