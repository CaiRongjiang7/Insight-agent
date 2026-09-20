# 研发问题复盘 Agent 核心版验收标准

## 文档信息

| 项目 | 内容 |
| --- | --- |
| 文档状态 | 待评审 |
| 版本 | v1.2（旧方案复核后的核心闭环版） |
| 日期 | 2026-09-14 |
| 需求基线 | `docs/product-requirements.md` v1.2 |
| 实施计划 | `docs/implementation-plan.md` v1.2 |

## 1. 验收原则

1. 核心目标是完整链路跑通，不以企业级配套能力作为阻断项。
2. 代码存在不等于功能完成，必须有接口结果、数据库记录或真实外部页面证明。
3. Mock/Fake 用于自动测试；GitHub、飞书和 OpenAI 各至少完成一次真实联调。
4. PostgreSQL/pgvector 和 Redis 必须被主链路真实使用，不允许静默回退后仍报告成功。
5. 只有 `CORE-*` 需求和本文“核心阻断项”影响核心版签收。

## 2. 核心阻断项

出现任一情况即不通过：

- Docker Compose 无法在干净环境启动核心服务。
- 后端核心测试或前端生产构建失败。
- 同一 GitHub Webhook 重放产生重复问题任务。
- 无法获取真实飞书消息或文档上下文。
- 复盘核心字段没有真实 Evidence 引用仍能进入待审核。
- 未经审核可以发布。
- 发布重试产生重复飞书 Wiki 页面或知识条目。
- 发布后的知识不能被后续问题召回。
- 配置 pgvector 后实际使用本地 SQLite/FAISS 且系统仍报告正常。
- OpenAI、飞书未配置或调用失败时返回伪成功。

## 3. 验收环境

- 干净机器或新的 Docker 环境。
- Docker Engine 与 Docker Compose v2。
- OpenAI 测试 API Key。
- 飞书测试自建应用、测试群和测试 Wiki/文档。
- GitHub 测试仓库及 Webhook 配置权限。

正式验收需记录：Git commit、脱敏 Compose 配置、服务状态、测试结果、测试 incident ID、trace ID、飞书 Wiki URL 和知识条目 ID。

## 4. Docker 与基础设施

### AC-CORE-001：一键启动

关联：CORE-020、CORE-021、CORE-030。

```powershell
Copy-Item .env.example .env
# 填写数据库密码和外部平台测试凭证
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

通过标准：

- `postgres`、`redis`、`api`、`worker`、`frontend` 正常运行。
- `migrate` 成功退出。
- 宿主机不需要另行安装 Python、Node.js、PostgreSQL 或 Redis。
- API `/api/health` 显示 PostgreSQL、vector 扩展、Redis 和 Worker 正常。

### AC-CORE-002：pgvector 与 Redis 真实使用

通过标准：

- PostgreSQL 中存在 `vector` 扩展和核心业务表。
- 测试 Evidence 写入 `evidence_chunks` 并包含非空 embedding。
- 执行向量检索可以返回该 Evidence。
- 提交异步任务后可在 Redis/Celery 中观察任务，最终状态写入 PostgreSQL。
- 清空 Redis 缓存后，已完成问题、复盘和知识仍可查询。

### AC-CORE-003：无静默回退

通过标准：停止 PostgreSQL 或移除 vector 扩展后，readiness 失败并显示明确原因；系统不得切换到 SQLite/JSON/FAISS 后继续报告 pgvector 正常。

## 5. GitHub 事件与代码上下文

### AC-CORE-010：Webhook 接收与验签

```gherkin
Given 已配置 GitHub Webhook Secret
When 发送签名正确且 merged=true 的 pull_request closed 测试事件
Then API 接受事件并创建一条 source_event、incident 和异步 job
```

负向标准：错误签名返回 401，不创建业务记录；`merged=false` 的关闭事件不创建 incident。

### AC-CORE-011：Webhook 幂等

通过标准：同一 delivery ID 连续和并发重放 5 次，只存在一个 incident 和一个有效主流程任务。

### AC-CORE-012：代码证据

通过标准：测试 PR 至少产生以下证据：

- PR 标题和描述；
- commit SHA 和 message；
- changed files；
- 至少一个 diff hunk；
- 可用时包含 CI 失败日志。

每条证据能够定位到仓库、commit、文件或外部 URL。

### AC-CORE-013：代码与日志上下文去噪

准备一个同时包含 Bug 修复、无关重构和长 CI 日志的测试 PR。

通过标准：

- 原始 PR、diff 和日志作为 Evidence 完整保存。
- 进入生成上下文的 diff hunk 与问题文件、错误栈或模块有关，并记录选择原因。
- CI 日志优先保留错误栈、error/warning 及其上下窗口，不把完整长日志直接送入模型。
- `push`、`workflow_run` 数据可以被关联补拉，但首期不要求其单独触发 incident。

## 6. 飞书开放平台

### AC-CORE-020：真实鉴权与 Token 缓存

通过标准：

- 使用测试 App ID/Secret 获得真实 tenant access token。
- Token 写入 Redis 缓存；有效期内重复请求不重复获取。
- 模拟过期后能够刷新并重试。
- 日志不包含 App Secret 和完整 token。

### AC-CORE-021：真实群消息上下文

在测试群准备目标关键词消息、无关消息和超过时间范围的消息。

通过标准：

- 系统按 chat ID 和时间范围分页获取消息。
- 目标消息转为 Evidence，保留 message ID、时间和内容。
- 范围外消息不进入当前任务上下文。
- 权限不足时任务显示明确错误并可重试。

### AC-CORE-022：真实 Wiki/文档上下文

通过标准：

- 系统读取一个配置的测试 Wiki 节点或文档。
- 文档正文被分块并写入 PostgreSQL/pgvector。
- Evidence 保存 node/document token、URL 或其他稳定定位信息。

### AC-CORE-023：真实 Wiki 发布

通过标准：

- 审核通过后在指定测试 Wiki 位置生成复盘页面。
- 页面至少包含问题摘要、影响、根因、修复方案、预防措施和来源。
- 本地保存飞书 node/token 和 URL。

发布后群通知属于可选增强，不影响本项结论。

### AC-CORE-024：飞书发布幂等

通过标准：同一复盘版本重复执行发布 5 次，只产生一个 Wiki 页面和一个知识版本。

## 7. RAG 与历史问题查询

### AC-CORE-030：元数据过滤

准备两个仓库、代码/飞书/历史三类来源、不同时间和 draft/published 知识。

通过标准：

- `repo_id`、`source_type`、`incident_type`、时间范围和知识状态过滤分别有效。
- 至少三个条件组合过滤有效。
- draft/rejected 知识不参与默认召回。

### AC-CORE-031：混合召回

通过标准：

- 精确错误码或函数名可以通过 PostgreSQL 关键词/全文检索命中。
- 同义描述可以通过向量检索命中。
- 返回结果包含 lexical/vector/fusion 分数、Evidence ID、原文和来源。
- API 或日志明确显示使用 pgvector。

### AC-CORE-032：历史案例召回

通过标准：查询一个与已有 published 案例相似的新问题，历史案例位于 Top 5；查询结果能回溯到原复盘和飞书 Wiki。

核心版不设置正式 Recall@K、MRR 或大规模性能阈值，只要求准备 5–10 个代表性查询进行人工核验。

验收记录不得把 PostgreSQL 原生全文检索误写为严格 BM25；首期也不要求独立 reranker。

## 8. OpenAI 生成与质量校验

### AC-CORE-040：官方 SDK 和结构化输出

通过标准：

- 后端依赖包含官方 `openai` 包。
- 主生成链路使用 Responses API。
- 复盘通过 Pydantic/JSON Schema 结构化解析，不使用正则截取 JSON 作为主路径。
- 真实 OpenAI smoke test 成功生成一份复盘。

### AC-CORE-041：三类上下文进入生成

通过标准：生成记录保存本次使用的代码 Evidence、飞书 Evidence 和历史知识 ID；测试案例中三类至少各一条。

### AC-CORE-042：复盘字段

通过标准：复盘包含标题、问题摘要、影响、根因、修复方案、预防措施、相关模块、关键词和证据引用；缺少必填字段不能进入 `pending_review`。

### AC-CORE-043：证据真实性

通过标准：

- 根因、影响和修复方案至少分别绑定一条 Evidence。
- Evidence ID 必须存在于当前生成候选集合。
- 引用片段能在对应原文中定位。
- 人为构造不存在的 Evidence ID 或虚假片段时校验失败。

### AC-CORE-044：基础质量分

通过标准：系统返回字段完整率、证据覆盖率、引用准确率和综合分，并明确区分：

- 硬校验：Schema、Evidence ID 属于候选集合、引用片段可在原文定位；失败时不得进入 `pending_review`。
- 软评分：完整率、覆盖率、历史案例匹配度等；低于阈值时可进入 `pending_review`，但页面必须显示风险提示。

硬校验失败自动生成一次，仍失败后进入 `failed` 并允许重试。

### AC-CORE-045：基础错误处理

分别模拟 OpenAI timeout、429、500 和无效 Schema。

通过标准：可重试错误在上限内重试；超过上限后任务失败并显示原因，不无限重试、不返回伪成功。

### AC-CORE-046：上下文预算与分层摘要

使用超出配置预算的代码、日志、群消息和历史案例输入。

通过标准：

- 最终模型输入不超过配置预算，并优先保留当前问题直接证据。
- 系统先执行确定性裁剪、去重和分块，仍超限才进行分层摘要。
- 摘要片段保留原 Evidence ID，可从复盘引用回溯原文。
- 首期使用同一模型完成摘要和复盘不影响验收，不要求多模型路由。

## 9. 人工审核与知识回流

### AC-CORE-050：状态机

通过标准：

- 正常流程只能 `pending_review -> approved -> publishing -> published`。
- 驳回必须填写原因，驳回后可重新生成并再次提交。
- `draft -> published`、`pending_review -> published` 等非法跳转返回冲突错误。

### AC-CORE-051：人工编辑与版本

通过标准：AI 初稿、人工修改版和驳回后新版分别保存在 `retrospective_versions`；可以查看版本内容、操作者和时间。

核心版不要求通用字段级可视化 Diff，但根因、影响和修复方案必须可以与其引用证据并排检查。

### AC-CORE-052：发布后知识入库

```gherkin
Given 一份已通过审核的复盘
When 审核人执行发布
Then 飞书 Wiki 页面创建或更新
And knowledge_entry/knowledge_chunk 写入 PostgreSQL
And embedding 写入 pgvector
And 知识状态变为 searchable
```

### AC-CORE-053：知识回流闭环

通过标准：

1. 发布一个带唯一测试术语的复盘。
2. 创建另一个语义相似的新问题。
3. 新问题的历史案例 Top 5 中出现刚发布知识。
4. 结果显示原复盘、审核信息和飞书 Wiki URL。

### AC-CORE-054：发布失败恢复

通过标准：模拟飞书发布失败后，复盘保留 `approved` 和错误信息；恢复服务后手工重试成功，不重复创建页面或知识。

## 10. 前端与工程质量

### AC-CORE-060：前端核心流程

通过标准：用户可以在界面完成：

1. 查看问题状态和代码/飞书/历史证据；
2. 查看并编辑复盘草稿，对根因、影响、修复方案并排检查引用证据和风险提示；
3. 提交、驳回、重新生成和通过；
4. 发布到飞书 Wiki；
5. 在知识库搜索刚发布案例；
6. 查看失败原因并重试。

### AC-CORE-061：测试与构建

通过标准：

- 后端核心单元、数据库集成和 API 集成测试全部通过。
- 前端 TypeScript 检查和 `npm run build` 通过。
- 一条使用 Fake Connector 的自动端到端测试通过。
- 无测试依赖缺失或依赖本地 `.env` 才能通过的情况。

### AC-CORE-062：最小运行日志

通过标准：一个 trace ID 能查询到 collect、retrieve、generate、validate、review、publish 和 index 各阶段状态、耗时、重试和错误；日志不出现 API Key、App Secret 或完整 Token。

## 11. 完整链路最终验收

### AC-E2E-001：真实正向链路

必须使用真实 GitHub 测试事件、真实飞书测试群/Wiki 和真实 OpenAI API：

```text
GitHub Webhook
  -> 代码证据入库与上下文去噪
  -> 飞书消息/文档证据入库
  -> 历史知识 RAG
  -> OpenAI 结构化复盘
  -> 证据校验
  -> 人工通过
  -> 飞书 Wiki 发布
  -> PostgreSQL/pgvector 知识入库
  -> 新问题召回该知识
```

通过标准：整条链路拥有同一个可追踪 incident/trace，所有关键数据库记录存在，飞书页面可以访问，后续召回成功。

### AC-E2E-002：驳回链路

通过标准：首版复盘被驳回后保存原因，重新生成或修改产生新版本，新版本可以通过并发布；旧驳回版本不进入知识检索。

### AC-E2E-003：重复与错误链路

通过标准：重复 Webhook、重复生成请求和重复发布不会产生重复核心数据；OpenAI/飞书临时失败后可以重试完成。

## 12. 非阻断的效率观察

核心版完成后，可以用 5–10 个内部案例记录：

- 纯人工整理时间；
- Agent 生成后人工修改和审核时间；
- 一次审核是否通过；
- 明显事实错误数量。

该观察不阻断核心版发布，也不能直接证明“降低约 50%”。如果简历要保留该数字，应在后续使用至少 20 个案例，并按以下方式计算：

```text
降低比例 = (人工基线时间中位数 - Agent 后人工时间中位数) / 人工基线时间中位数
```

没有真实统计结果前，应删除或弱化简历中的 50% 表述。

## 13. 最终签收清单

- [ ] AC-CORE-001～003：Docker、PostgreSQL/pgvector、Redis 通过。
- [ ] AC-CORE-010～013：GitHub 采集与上下文去噪通过。
- [ ] AC-CORE-020～024：真实飞书读写通过。
- [ ] AC-CORE-030～032：元数据过滤和混合 RAG 通过。
- [ ] AC-CORE-040～046：OpenAI 结构化生成、上下文预算和证据校验通过。
- [ ] AC-CORE-050～054：人工审核、发布和知识回流通过。
- [ ] AC-CORE-060～062：前端、测试、构建和最小日志通过。
- [ ] AC-E2E-001～003：真实正向、驳回、重复/错误链路通过。
- [ ] 有脱敏的 incident ID、trace ID、飞书 Wiki URL、知识 ID 和测试结果作为验收证据。
- [ ] 企业级安全、可观测、高可用和规模化能力明确列为后续项，不阻塞核心版签收。
