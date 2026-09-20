# 研发问题复盘 Agent 核心版实施计划

## 文档信息

| 项目 | 内容 |
| --- | --- |
| 文档状态 | 待评审 |
| 版本 | v1.2（旧方案复核后的核心闭环版） |
| 日期 | 2026-09-14 |
| 需求基线 | `docs/product-requirements.md` v1.2 |
| 验收依据 | `docs/acceptance-criteria.md` v1.2 |
| 选型复核 | `docs/legacy-solution-review.md` |

## 1. 实施目标

本计划只围绕一条可演示、可验证的端到端链路推进：

```text
GitHub -> 代码上下文 -> 飞书上下文 -> 历史 RAG -> OpenAI 复盘
       -> 基础校验 -> 人工审核 -> 飞书 Wiki -> 知识入库 -> 后续召回
```

所有外围能力服从这一目标。暂不投入企业级权限、高可用、复杂可观测和大规模性能优化。

## 2. 目标技术方案

| 模块 | 方案 |
| --- | --- |
| API | FastAPI + Pydantic |
| 数据库 | PostgreSQL + pgvector |
| ORM/迁移 | SQLAlchemy + Alembic |
| 异步任务 | Celery + Redis；不直接开发 Redis Stream 消费框架 |
| LLM | 官方 OpenAI Python SDK + Responses API Structured Outputs |
| RAG | PostgreSQL 关键词/全文检索 + pgvector + 简单 RRF/加权融合，不上独立 reranker |
| 飞书 | tenant token、指定群消息读取、Wiki/文档读取和 Wiki 发布；群通知可选 |
| 前端 | 复用 Vue 3 管理台，聚焦问题、审核、知识库三个页面 |
| 部署 | Docker Compose |

## 3. 里程碑

| 阶段 | 目标 | 参考工期 |
| --- | --- | --- |
| M0 | 修复现有工程基线 | 2–3 天 |
| M1 | Docker、PostgreSQL/pgvector、Redis 与数据迁移 | 4–6 天 |
| M2 | GitHub 采集、异步任务和标准 Tool | 4–6 天 |
| M3 | 飞书上下文与发布能力 | 5–7 天 |
| M4 | RAG、OpenAI 生成和基础校验 | 5–8 天 |
| M5 | 人工审核、知识回流和前端闭环 | 5–7 天 |
| M6 | 端到端联调与核心验收 | 3–5 天 |

单人串行约 4–6 周。M3 飞书接入和 M4 的 RAG 基础部分可在 M1 后交叉推进。

## 4. 分阶段任务

### M0：修复现有工程基线

#### 必做

1. 修复 `SkillRepository.match_skills()` 缺失导致的后端主流程失败。
2. 修复 `CiAutofixView.vue` 中 `root_cause.source` 类型不一致。
3. 补齐 TestClient/pytest 等测试依赖。
4. 隔离测试配置，消除本地 `.env` 对审批测试的影响。
5. 为已有 `PRListener`、`generate_bug_review()`、`QualityScorer`、审核状态机和 Wiki 发布器补最小单元测试。

#### 完成门禁

- 当前后端测试全部通过。
- `npm run build` 通过。
- 现有 Demo 可启动。

### M1：Docker、PostgreSQL/pgvector、Redis 与数据层

#### 必做

1. 新增根目录 `compose.yaml`、后端 Dockerfile、前端 Dockerfile 和 `.env.example`。
2. 配置 `postgres`、`redis`、`migrate`、`api`、`worker`、`frontend` 六个服务和健康检查。
3. PostgreSQL 启用 `vector` 扩展；引入 SQLAlchemy、Alembic、psycopg 和 pgvector。
4. 建立核心表：repositories、source_events、incidents、evidence_documents/chunks、retrospective_versions、review_actions、knowledge_entries/chunks、jobs。
5. Redis 用于 Celery broker、Webhook 幂等缓存、简单锁和任务进度。
6. 编写旧 incidents/skills/demo 数据到 PostgreSQL 的一次性导入脚本。
7. 修改新主流程只读写 PostgreSQL；旧 JSON/SQLite 保留为导入源，不再作为事实源。

#### 可以简化

- 不做迁移 downgrade、双写和复杂 expand/contract。
- 不做自动备份平台，只在 README 中给出 `pg_dump`/恢复命令。
- 不做 Redis 集群、Sentinel 或复杂持久化调优。

#### 完成门禁

- `docker compose up -d --build` 后全部核心服务可用。
- API 能检查 PostgreSQL、vector 扩展、Redis 和 Worker。
- 插入并检索一条 pgvector 测试向量成功。

### M2：GitHub 采集、异步任务和 Tool

#### 必做

1. 重构 `/webhooks/github`，以合并 PR 为首期自动触发事件，真正校验签名并使用 delivery ID 幂等。
2. Webhook 只负责落库和入队，Worker 执行后续采集。
3. 使用 GitHub API 补拉 PR/commit/diff、changed files、PR 描述，以及该 PR 可关联到的 CI 日志；`push`、`workflow_run` 首期不单独创建问题。
4. 将采集内容统一保存为 EvidenceDocument/EvidenceChunk。
5. 建立简化状态机和手工重试 API。
6. 封装并接入 `git.get_change_context`、`knowledge.search` 和统一 Tool 输入/输出 Schema。
7. 支持管理台手工创建问题，复用同一后续链路。
8. 根据 changed files、diff hunk、错误栈、模块和问题关键词选出相关片段，原文完整入库但只把候选片段交给后续生成。

#### 可以简化

- GitLab 不纳入本阶段门禁，保持现有代码兼容即可。
- 不接 Sentry、ELK、Loki 等外部日志平台；需要时允许在手工创建问题时粘贴日志。
- 不做 dead-letter 管理台、复杂取消和 Worker 崩溃自动接管测试。
- 重试采用固定上限和简单指数退避。

#### 完成门禁

- 同一 Webhook 重放不会重复建任务。
- 一个测试 PR 能生成完整代码证据并进入 `retrieving`。

### M3：飞书上下文与发布能力

#### 外部准备

1. 创建部门测试用飞书自建应用。
2. 申请读取测试群消息、读取测试 Wiki/文档、创建 Wiki 内容和发送消息所需的最小权限。
3. 准备测试群、测试文档和测试 Wiki 父节点。

#### 必做

1. 实现 `FeishuAuthClient`，获取 tenant token 并使用 Redis 缓存。
2. 实现按 `chat_id + 时间范围` 分页读取消息。
3. 实现按配置 token 读取 Wiki/文档内容。
4. 将飞书内容转换为 EvidenceDocument/EvidenceChunk。
5. 实现飞书 Wiki 创建/更新；发布后简单群通知仅作为有余力时的增强项。
6. 实现 Token 过期、429、5xx、权限不足和分页处理。
7. 封装并接入 `feishu.get_context` 与 `wiki.publish`。
8. 编写 Fake Connector 测试，并完成一次真实测试群/Wiki 联调。

#### 可以简化

- 仓库与飞书范围先通过 `.env` 或数据库种子配置，不做复杂配置页面。
- 不做全企业消息搜索、附件内容解析和交互卡片审批。
- 不做飞书事件回调；主动读取与发布足够支撑核心链路。

#### 完成门禁

- 能从真实测试群和测试 Wiki/文档读取内容并生成可定位证据。
- 能在真实测试 Wiki 创建复盘页；群通知不作为完成门禁。

### M4：混合 RAG、OpenAI 生成和基础校验

#### 必做

1. 为代码、飞书和历史知识设计简单分块规则，保留来源边界。
2. 使用 OpenAI SDK 生成 embedding，保存模型、维度和内容哈希。
3. 实现 metadata filter：repo、source type、incident type、时间范围和 published 状态。
4. 实现 PostgreSQL 关键词/全文检索与 pgvector 向量召回。
5. 使用简单权重或 RRF 融合，返回 Top K 和分数明细。
6. 用官方 OpenAI SDK 替换当前直接 HTTP 调用。
7. 使用 Responses API Structured Outputs 生成 Pydantic 复盘对象。
8. 真正串联 `git.get_change_context`、`feishu.get_context`、`knowledge.search` 和 `retrospective.generate`。
9. 为 diff、CI 日志和群消息实现确定性去重/裁剪和上下文预算；只有仍超限时才执行保留 Evidence ID 的分层摘要。
10. 实现必填字段、Evidence ID、引用片段定位和核心字段证据覆盖校验。
11. 将质量控制分为硬校验和软评分：硬校验失败自动重新生成一次，仍失败进入 `failed`；软评分低则带风险标记进入人工审核。

#### 可以简化

- 不做 cross-encoder reranker、MMR 和复杂中文分词服务。
- 不做提示词管理平台，只使用带版本号的代码模板。
- 不做全面敏感信息识别，只脱敏密钥、Authorization 和 Token。
- 不做模型自动切换和复杂成本路由；分层摘要和复盘生成首期可用同一模型。

#### 完成门禁

- 一个真实测试问题能获得代码、飞书、历史三类上下文。
- 生成的复盘 Schema 有效，且核心字段引用真实 Evidence。
- 混合了无关改动和长日志的测试输入经过裁剪后不超过配置预算，摘要仍能追溯原 Evidence。
- 系统实际使用 PostgreSQL/pgvector，不静默回退本地向量库。

### M5：人工审核、知识回流和前端闭环

#### 必做

1. 将审核状态统一迁移到 PostgreSQL 状态机。
2. 保存 AI 初稿与人工编辑版本。
3. 实现提交、驳回、重新生成、通过和发布 API。
4. 发布采用 `retrospective_id + version` 幂等键，避免重复 Wiki 页面。
5. Wiki 发布成功后创建 KnowledgeEntry/KnowledgeChunk 并生成 pgvector embedding。
6. 索引成功后把知识标记为 searchable。
7. 新建相似问题，验证刚发布知识能进入历史案例 Top K。
8. 前端聚焦问题列表/详情、复盘审核和知识库检索三个页面。
9. 页面展示阶段、错误、重试、证据、质量分和飞书 Wiki 链接。
10. 对根因、影响和修复方案提供“结论 + 引用证据”并排检查，不建设通用 Diff 引擎。

#### 可以简化

- 审核人由配置名单或简单管理员账号确定，不做企业 RBAC。
- 不做复杂富文本编辑器，结构化表单或 Markdown 编辑足够。
- 不做通用字段级可视化 diff；版本历史只展示内容与修改时间，核心结论只做引用证据并排检查。
- 发布一致性使用数据库状态、远端 ID 和幂等重试，不引入完整 saga/outbox 框架。

#### 完成门禁

- 正向审核发布链路和驳回重生成链路均可操作。
- 发布失败可重试且不重复创建 Wiki/知识。
- 已发布案例能够被后续问题召回。

### M6：端到端联调与核心验收

#### 必做

1. 在干净环境执行 Docker Compose 一键启动。
2. 执行一条 Mock 端到端测试，覆盖全部自动阶段。
3. 使用真实 GitHub 测试事件、真实飞书群/Wiki 和真实 OpenAI API 跑通完整链路。
4. 执行驳回→重新生成→通过→发布流程。
5. 重放 Webhook 和发布任务，验证幂等。
6. 执行后端测试、前端类型检查和生产构建。
7. 记录一份脱敏的验收报告和演示步骤。

#### 非阻断项

- 使用 5–10 个内部问题记录人工整理与 Agent 辅助时间，作为早期观察。
- 若要在简历正式保留“降低约 50%”，后续仍应扩大到至少 20 个样本；核心版上线不以此为阻断。

## 5. 推荐实现顺序

1. 修复测试与前端构建。
2. Docker Compose + PostgreSQL/pgvector + Redis。
3. SQLAlchemy/Alembic 核心模型。
4. GitHub Webhook、幂等、Celery 任务。
5. 飞书认证与上下文读取。
6. 证据相关性筛选、上下文预算与混合 RAG/metadata filter。
7. OpenAI SDK Structured Outputs、硬校验与软评分。
8. 审核状态机与飞书 Wiki 发布。
9. 知识回流与后续召回。
10. 前端核心页面与端到端验收。

每个提交应保持测试可运行，不建议先做全部底层重构再统一联调。

## 6. 最小测试矩阵

| 类型 | 必须覆盖 |
| --- | --- |
| 单元测试 | 状态机、Webhook 幂等、上下文裁剪、metadata filter、融合排序、硬校验/软评分、飞书 Token 缓存 |
| Connector Fake | GitHub、飞书、OpenAI 正常/超时/429/权限失败 |
| 数据库集成 | Alembic、pgvector 插入与搜索、审核版本、知识入库 |
| API 集成 | 创建问题、重试、审核、驳回、通过、发布、知识搜索 |
| 前端 | TypeScript 构建和核心审核流程 |
| 真实 smoke | GitHub、飞书群/Wiki、OpenAI 各至少一次 |
| 端到端 | 正向链路、驳回重生成、重复 Webhook、重复发布 |

## 7. 核心风险与简单应对

| 风险 | 应对 |
| --- | --- |
| 飞书权限或历史消息不可读 | 只使用明确授权的测试群/Wiki；页面显示权限错误；允许手工补充问题 |
| OpenAI 输出证据不足 | 限定 Evidence ID，做原文定位；失败自动重生成一次 |
| PR/日志/群聊上下文过长或噪声大 | 原文入库，进入模型前按相关性裁剪；超预算才分层摘要，并保留 Evidence ID |
| pgvector 配置失败后误用本地存储 | 启动检查 `vector` 扩展；失败直接 readiness 不通过，不静默回退 |
| Redis 重启 | 最终任务和审核状态存 PostgreSQL；Redis 只保存队列和短期进度 |
| 飞书发布重复 | 保存远端 node/token，以复盘版本为幂等键，重试前查询本地发布记录 |
| 旧模块与新数据层冲突 | 新闭环统一走 PostgreSQL；旧 JSON 路径只读并逐步移除 |

## 8. 核心版完成定义

- 所有 CORE-* 需求有实现或明确映射。
- Docker Compose 一键启动通过。
- PostgreSQL/pgvector、Redis、OpenAI SDK 和飞书真实参与主链路。
- GitHub→复盘→人工审核→飞书 Wiki→知识回流→后续召回完整跑通。
- 后端测试和前端构建通过。
- 有脱敏演示数据和操作说明。
- 不要求在核心版完成企业级安全、可观测、高可用和大规模性能建设。
