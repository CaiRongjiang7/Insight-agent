import { computed, ref } from "vue";
import { defineStore } from "pinia";
import { useDebounceFn } from "@vueuse/core";
import { platformApi } from "@/api/platform";
import type { SearchHit } from "@/types/platform";

export const useAppStore = defineStore("app", () => {
  const collapsed = ref(false);
  const locale = ref<"zh" | "en">("zh");
  // 固定亮色模式，不使用 useColorMode（避免 localStorage 残留导致暗黑）
  const isDark = ref(false);
  const notifications = ref<Array<Record<string, unknown>>>([]);
  const systemStatus = ref<Record<string, unknown>>({});
  const searchResults = ref<SearchHit[]>([]);
  const loading = ref(false);

  async function refreshGlobalState() {
    loading.value = true;
    try {
      const [status, notice] = await Promise.all([
        platformApi.getSystemStatus(),
        platformApi.getNotifications(),
      ]);
      systemStatus.value = status;
      notifications.value = notice;
    } finally {
      loading.value = false;
    }
  }

  const search = useDebounceFn(async (query: string) => {
    if (!query.trim()) {
      searchResults.value = [];
      return;
    }
    searchResults.value = await platformApi.search(query);
  }, 220);

  function toggleSidebar() {
    collapsed.value = !collapsed.value;
  }

  function toggleTheme() {
    // 暂不支持暗黑模式切换
  }

  function setLocale(next: "zh" | "en") {
    locale.value = next;
  }

  return {
    collapsed,
    locale,
    isDark,
    notifications,
    systemStatus,
    searchResults,
    loading,
    refreshGlobalState,
    search,
    toggleSidebar,
    toggleTheme,
    setLocale,
  };
});
