import { createApp } from "vue";
import { createPinia } from "pinia";
import ElementPlus from "element-plus";
import "element-plus/dist/index.css";
// 强制亮色模式：移除暗黑 CSS 导入，清除 localStorage 中可能残留的 dark 设置
// import "element-plus/theme-chalk/dark/css-vars.css";

import App from "./App.vue";
import { router } from "./router";
import { i18n } from "./i18n";
import "./styles/global.css";

// 强制清除暗黑模式残留
localStorage.removeItem("vueuse-color-scheme");
document.documentElement.classList.remove("dark");
document.documentElement.classList.add("light");

const app = createApp(App);

app.use(createPinia());
app.use(router);
app.use(i18n);
app.use(ElementPlus);

app.mount("#app");
