<template>
  <div ref="container" class="chart-surface"></div>
</template>

<script setup lang="ts">
import * as echarts from "echarts";
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useWindowSize } from "@vueuse/core";

const props = defineProps<{
  option: Record<string, unknown>;
}>();

const container = ref<HTMLDivElement>();
let chart: echarts.ECharts | null = null;
const { width } = useWindowSize();

function render() {
  if (!container.value) return;
  chart = chart || echarts.init(container.value);
  chart.setOption(props.option, true);
  chart.resize();
}

onMounted(render);
watch(() => props.option, render, { deep: true });
watch(width, () => chart?.resize());
onBeforeUnmount(() => chart?.dispose());
</script>
