<template>
  <section class="review-page">
    <div class="page-head">
      <div>
        <h1>核心复盘闭环</h1>
        <p>GitHub / 飞书证据 → 历史检索 → 结构化复盘 → 审核 → 飞书 Wiki → 知识回流</p>
      </div>
      <div class="head-actions">
        <el-tag :type="connectorStatus.ready ? 'success' : 'danger'">
          {{ connectorStatus.ready ? "连接器已就绪" : "连接器未就绪" }}
        </el-tag>
        <el-button type="primary" @click="createVisible = true">手工创建问题</el-button>
      </div>
    </div>

    <el-alert v-if="connectorStatus.error" :title="connectorStatus.error" type="warning" show-icon :closable="false" />

    <div class="workspace">
      <el-card class="incident-list" shadow="never">
        <template #header><div class="card-title">问题任务 <el-button text @click="load">刷新</el-button></div></template>
        <el-empty v-if="!incidents.length" description="暂无核心链路任务" />
        <button
          v-for="item in incidents"
          :key="item.id"
          :class="['incident-item', { active: selectedId === item.id }]"
          @click="select(item.id)"
        >
          <strong>{{ item.title }}</strong>
          <span>{{ item.repo_name || item.repo_id || "未指定仓库" }}</span>
          <el-tag size="small" :type="statusType(item.status)">{{ item.status }}</el-tag>
        </button>
      </el-card>

      <el-card class="detail" shadow="never">
        <el-empty v-if="!detail" description="请选择问题任务" />
        <template v-else>
          <div class="detail-head">
            <div>
              <h2>{{ detail.incident.title }}</h2>
              <p>{{ detail.incident.description }}</p>
            </div>
            <div class="actions">
              <el-button v-if="detail.incident.status === 'received'" type="primary" @click="run">运行链路</el-button>
              <el-button v-if="detail.incident.status === 'failed'" type="warning" @click="retry">重试</el-button>
              <el-button v-if="detail.incident.status === 'rejected'" type="warning" @click="regenerate">重新生成</el-button>
              <el-button v-if="detail.incident.status === 'pending_review'" type="success" @click="approve">通过</el-button>
              <el-button v-if="detail.incident.status === 'pending_review'" type="danger" @click="reject">驳回</el-button>
              <el-button v-if="detail.incident.status === 'approved'" type="primary" @click="publish">发布飞书 Wiki</el-button>
            </div>
          </div>

          <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="状态">{{ detail.incident.status }}</el-descriptions-item>
            <el-descriptions-item label="Trace">{{ detail.incident.trace_id }}</el-descriptions-item>
            <el-descriptions-item label="版本">{{ detail.incident.current_version }}</el-descriptions-item>
          </el-descriptions>
          <el-alert v-if="detail.incident.error" :title="detail.incident.error" type="error" show-icon :closable="false" />

          <el-tabs v-model="activeTab">
            <el-tab-pane label="复盘材料" name="document">
              <template v-if="detail.retrospective">
                <div class="quality-row">
                  <el-progress :percentage="Math.round(detail.retrospective.quality.score * 100)" :stroke-width="10" />
                  <el-tag v-for="warning in detail.retrospective.quality.soft_warnings" :key="warning" type="warning">{{ warning }}</el-tag>
                </div>
                <div class="review-split">
                  <el-input v-model="documentJson" type="textarea" :rows="24" spellcheck="false" />
                  <div class="citation-panel">
                    <h3>核心结论引用核对</h3>
                    <el-empty v-if="!citationRows.length" description="暂无引用" />
                    <article v-for="row in citationRows" :key="`${row.section}-${row.citation.evidence_id}`" class="citation-card">
                      <div><el-tag size="small">{{ row.section }}</el-tag> <strong>{{ row.evidence?.location || row.citation.evidence_id }}</strong></div>
                      <blockquote>{{ row.citation.quote }}</blockquote>
                      <small>{{ row.evidence?.source_type || "证据不存在" }} · {{ row.citation.evidence_id }}</small>
                    </article>
                  </div>
                </div>
                <el-button
                  v-if="['pending_review', 'rejected'].includes(detail.incident.status)"
                  class="save-button"
                  @click="saveDocument"
                >保存人工版本</el-button>
              </template>
              <el-empty v-else description="尚未生成复盘" />
            </el-tab-pane>
            <el-tab-pane :label="`证据 (${detail.evidence.length})`" name="evidence">
              <div v-for="item in detail.evidence" :key="item.id" class="evidence-card">
                <div><el-tag size="small">{{ item.source_type }}</el-tag> <strong>{{ item.location }}</strong></div>
                <small>{{ item.id }} · {{ item.selected ? item.selection_reason : "未进入模型上下文" }}</small>
                <pre>{{ item.content }}</pre>
              </div>
            </el-tab-pane>
            <el-tab-pane :label="`版本 (${detail.versions.length})`" name="versions">
              <el-timeline>
                <el-timeline-item v-for="item in detail.versions" :key="item.id" :timestamp="item.created_at">
                  v{{ item.version }} · {{ item.kind }} · {{ item.actor }} · 质量 {{ Math.round(item.quality.score * 100) }}%
                </el-timeline-item>
              </el-timeline>
            </el-tab-pane>
          </el-tabs>
        </template>
      </el-card>
    </div>

    <el-dialog v-model="createVisible" title="手工创建研发问题" width="620px">
      <el-form label-position="top">
        <el-form-item label="标题"><el-input v-model="form.title" /></el-form-item>
        <el-form-item label="仓库 owner/repo"><el-input v-model="form.repo_name" /></el-form-item>
        <el-form-item label="问题类型"><el-input v-model="form.incident_type" /></el-form-item>
        <el-form-item label="描述"><el-input v-model="form.description" type="textarea" :rows="4" /></el-form-item>
        <el-form-item label="日志"><el-input v-model="form.logs" type="textarea" :rows="4" /></el-form-item>
        <el-form-item label="飞书群 ID（逗号分隔）"><el-input v-model="form.chat_ids" /></el-form-item>
        <el-form-item label="飞书文档 Token（逗号分隔）"><el-input v-model="form.doc_tokens" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="createVisible = false">取消</el-button><el-button type="primary" @click="create">创建</el-button></template>
    </el-dialog>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { retrospectiveApi, type CoreDetail, type CoreDocument, type CoreIncident, type CoreStatus } from "@/api/retrospective";

const incidents = ref<CoreIncident[]>([]);
const selectedId = ref("");
const detail = ref<CoreDetail | null>(null);
const connectorStatus = ref<Record<string, any>>({ ready: false });
const activeTab = ref("document");
const documentJson = ref("");
const createVisible = ref(false);
const form = reactive({ title: "", repo_name: "", incident_type: "unknown", description: "", logs: "", chat_ids: "", doc_tokens: "" });
const citationRows = computed(() => {
  const document = detail.value?.retrospective?.document;
  if (!document || !detail.value) return [];
  const evidence = new Map(detail.value.evidence.map((item) => [item.id, item]));
  return [
    ...document.impact.citations.map((citation) => ({ section: "影响", citation })),
    ...document.root_cause.citations.map((citation) => ({ section: "根因", citation })),
    ...document.fix_solution.citations.map((citation) => ({ section: "修复", citation })),
  ].map((row) => ({ ...row, evidence: evidence.get(row.citation.evidence_id) }));
});

function split(value: string) { return value.split(",").map((item) => item.trim()).filter(Boolean); }
function statusType(status: CoreStatus) {
  if (status === "published" || status === "approved") return "success";
  if (status === "failed" || status === "rejected") return "danger";
  if (status === "pending_review") return "warning";
  return "info";
}

async function load() {
  try {
    [connectorStatus.value, incidents.value] = await Promise.all([retrospectiveApi.status(), retrospectiveApi.list()]);
    if (selectedId.value) await select(selectedId.value);
  } catch (error) { ElMessage.error(String(error)); }
}
async function select(id: string) {
  selectedId.value = id;
  detail.value = await retrospectiveApi.detail(id);
  documentJson.value = detail.value.retrospective ? JSON.stringify(detail.value.retrospective.document, null, 2) : "";
}
async function create() {
  if (!form.title.trim()) return ElMessage.warning("请填写标题");
  const incident = await retrospectiveApi.create({
    title: form.title, description: form.description, repo_id: form.repo_name, repo_name: form.repo_name,
    incident_type: form.incident_type, source_type: "manual", logs: form.logs,
    feishu_chat_ids: split(form.chat_ids), feishu_doc_tokens: split(form.doc_tokens), metadata: {},
  });
  createVisible.value = false;
  await load();
  await select(incident.id);
}
async function run() { await retrospectiveApi.run(selectedId.value); ElMessage.success("任务已提交"); await load(); }
async function retry() { await retrospectiveApi.retry(selectedId.value); ElMessage.success("重试完成"); await load(); }
async function regenerate() { await retrospectiveApi.regenerate(selectedId.value); ElMessage.success("已基于原证据重新生成"); await load(); }
async function approve() { await retrospectiveApi.approve(selectedId.value, "admin"); await load(); }
async function reject() {
  const { value } = await ElMessageBox.prompt("请输入驳回原因", "驳回复盘", { inputType: "textarea" });
  await retrospectiveApi.reject(selectedId.value, "admin", value); await load();
}
async function publish() { const result = await retrospectiveApi.publish(selectedId.value, "admin"); ElMessage.success(`已发布：${result.publication.url}`); await load(); }
async function saveDocument() {
  try {
    const document = JSON.parse(documentJson.value) as CoreDocument;
    await retrospectiveApi.edit(selectedId.value, "admin", document);
    ElMessage.success("人工版本已保存"); await load();
  } catch (error) { ElMessage.error(`保存失败：${String(error)}`); }
}

onMounted(load);
</script>

<style scoped>
.review-page { display: flex; flex-direction: column; gap: 16px; }
.page-head, .detail-head, .card-title { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.page-head h1, .detail-head h2 { margin: 0 0 6px; }
.page-head p, .detail-head p { margin: 0; color: var(--text-secondary); }
.head-actions, .actions, .quality-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.workspace { display: grid; grid-template-columns: minmax(260px, 340px) minmax(0, 1fr); gap: 16px; min-height: 650px; }
.incident-list :deep(.el-card__body) { padding: 8px; }
.incident-item { width: 100%; border: 0; border-radius: 8px; background: transparent; padding: 12px; display: grid; gap: 6px; text-align: left; cursor: pointer; }
.incident-item:hover, .incident-item.active { background: var(--el-color-primary-light-9); }
.incident-item span { color: var(--text-secondary); }
.detail { min-width: 0; }
.detail-head { margin-bottom: 16px; }
.quality-row { margin: 14px 0; }
.quality-row .el-progress { width: 220px; }
.save-button { margin-top: 12px; }
.review-split { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(300px, .65fr); gap: 14px; align-items: start; }
.citation-panel { border: 1px solid var(--border); border-radius: 8px; padding: 12px; max-height: 574px; overflow: auto; }
.citation-panel h3 { margin: 0 0 12px; }
.citation-card { border-bottom: 1px solid var(--border); padding: 0 0 12px; margin-bottom: 12px; }
.citation-card:last-child { border-bottom: 0; margin-bottom: 0; }
.citation-card blockquote { margin: 8px 0; padding: 8px 10px; background: var(--bg-page); border-left: 3px solid var(--el-color-primary); }
.citation-card small { color: var(--text-secondary); }
.evidence-card { border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 10px; }
.evidence-card small { display: block; color: var(--text-secondary); margin: 8px 0; }
.evidence-card pre { white-space: pre-wrap; max-height: 280px; overflow: auto; background: var(--bg-page); padding: 10px; border-radius: 6px; }
@media (max-width: 960px) { .workspace, .review-split { grid-template-columns: 1fr; } }
</style>
