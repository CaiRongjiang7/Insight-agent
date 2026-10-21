# 研发问题复盘 Agent 核心版需求文档

## 文档信息

| 项目 | 内容 |
| --- | --- |
| 文档状态 | 待评审 |
| 版本 | v1.2（旧方案复核后的核心闭环版） |
| 日期 | 2026-09-14 |
| 适用范围 | 小组/部门内部使用 |
| 需求来源 | `docs/resume-project-gap-analysis.md`、旧版技术方案 PDF |
| 关联文档 | `docs/legacy-solution-review.md`、`docs/implementation-plan.md`、`docs/acceptance-criteria.md` |

## 1. 本次调整

本版本以“完整链路可运行、核心功能可演示、简历描述有真实代码支撑”为唯一优先目标，不按企业级平台建设。

复核旧方案后，保留其低成本且直接服务主链路的设计：Webhook 事件驱动、结构化输出、混合检索、证据约束、人工审核、幂等重试和发布回流；不引入 Kafka/RabbitMQ、独立 reranker、日志平台连接器或多模型路由。代码上下文仅对固定 commit 做按需的局部静态扩展，不建设独立的全仓 Code Graph 服务。具体取舍见 `docs/legacy-solution-review.md`。

保留的核心能力：

1. GitHub 合并 PR 事件接入与代码变更上下文获取，支持手工补录；对可定位到 commit 的问题补充局部源码关系证据。
2. 飞书群消息、Wiki/文档上下文读取。
3. PostgreSQL/pgvector 知识库和带元数据过滤的 RAG。
4. Redis 异步任务、幂等和进度缓存。
5. 官方 OpenAI Python SDK 结构化生成复盘材料。
6. 证据引用与基础质量校验。
7. 人工编辑、审核、发布飞书 Wiki、知识入库和后续召回。
8. Docker Compose 一键启动全部本地基础设施。

不作为核心开发范围：

- 企业级 RBAC、SSO、多租户和复杂权限中心。
- Kubernetes、多节点高可用、跨地域容灾和完整备份平台。
- Prometheus/Grafana、分布式追踪和复杂告警平台。
- 大规模容量压测、向量索引在线双版本迁移。
- 飞书交互卡片审批、全企业搜索和事件订阅平台化。
- Sentry/ELK/Loki 等外部日志平台连接器；首期只使用 GitHub 可获取的 CI 日志或手工补充日志。
- Kafka、RabbitMQ、直接维护 Redis Stream 消费框架、独立检索 reranker 和多模型路由。
- 完整 PII/DLP 系统、密钥托管平台和复杂审计合规。
- 自动代码修复、自动合并 PR 或自动执行生产写操作。

这些能力可后续扩展，但不得阻塞核心链路交付。

## 2. 产品目标

系统需要真实跑通以下链路：

```text
GitHub 合并 PR 事件/手工创建问题
  -> 获取 PR、commit、diff、变更文件和 CI 日志
  -> 检索配置范围内的飞书群消息与 Wiki/文档
  -> 检索历史问题和处置经验
  -> 对变更、日志和消息做去噪、裁剪与上下文预算控制
  -> 生成带证据引用的结构化复盘材料
  -> 自动基础校验
  -> 人工修改、通过或驳回
  -> 发布到飞书 Wiki
  -> 审核版本写入 PostgreSQL/pgvector 知识库
  -> 后续相似问题能够召回该案例
```

核心完成标准不是模块数量，而是上述链路能够在一个真实测试案例上从头运行到尾，并在失败后允许人工重试。

## 3. 用户与使用方式

系统面向一个可信的小组或部门，不建设复杂角色体系。

| 用户 | 能力 |
| --- | --- |
| 普通成员 | 查看任务、证据和已发布知识；编辑复盘草稿 |
| 审核人 | 普通成员能力，加上通过、驳回和发布 |
| 管理员 | 配置 GitHub、飞书、OpenAI 和默认检索范围 |

核心版可以通过配置文件维护审核人名单，不要求接入 SSO。后端仍需阻止明显的未登录访问和非法状态跳转，但不实现细粒度组织权限。

## 4. 核心架构

```mermaid
flowchart LR
    GH[GitHub Webhook] --> API[FastAPI]
    UI[Vue 管理台] --> API
    API --> PG[(PostgreSQL + pgvector)]
    API --> RD[(Redis)]
    RD --> WK[Worker]
    WK --> GT[GitHub Tool]
    WK --> FT[飞书 Tool]
    WK --> RAG[历史知识 RAG]
    RAG --> PG
    WK --> OAI[OpenAI SDK]
    OAI --> QA[证据与质量校验]
    QA --> PG
    API --> HR[人工审核]
    HR --> WK
    WK --> FW[飞书 Wiki 发布]
    FW --> PG
    PG --> RAG
```

### 4.1 Docker Compose 服务

| 服务 | 职责 |
| --- | --- |
| `postgres` | 业务数据、全文检索和 pgvector 向量 |
| `redis` | Celery broker、Webhook 幂等、简单锁和短期进度；首期不直接实现 Redis Stream 消费框架 |
| `migrate` | 执行 Alembic 数据库迁移 |
| `api` | FastAPI、Webhook、任务、审核和检索 API |
| `worker` | 上下文采集、RAG、LLM、校验、发布和索引 |
| `frontend` | Vue 管理界面 |

执行以下命令后，应自动完成本地基础设施初始化和应用启动：

```powershell
docker compose up -d --build
```

OpenAI、GitHub 和飞书是外部平台，Docker 负责加载凭证和检查连接，但不能代替用户创建外部应用与授权。

## 5. 核心状态机

```text
received
  -> collecting
  -> retrieving
  -> generating
  -> validating
  -> pending_review
  -> approved
  -> publishing
  -> published

pending_review -> rejected -> generating
任一自动阶段失败 -> failed -> 手工重试原阶段
```

要求：

- 状态保存在 PostgreSQL。
- Redis 丢失后不影响已完成任务和审核结果。
- 状态跳转必须合法，例如不允许 `draft` 直接发布。
- 重试发布时不得重复创建飞书页面或知识条目。

## 6. 功能需求

### 6.1 问题与代码上下文

| ID | 需求 |
| --- | --- |
| CORE-001 | 接收 GitHub `pull_request` 关闭事件，只有 `merged=true` 时自动创建问题，并验证 Webhook Secret。`push`、`workflow_run` 首期只作为关联信息补拉，不作为独立触发门禁。 |
| CORE-002 | 使用 GitHub delivery ID 做幂等，同一事件重复发送只生成一个问题任务。 |
| CORE-003 | Webhook 落库后异步处理，获取 PR/commit/diff、变更文件、PR 描述和 CI 日志。 |
| CORE-004 | 支持管理台手工创建问题，便于补录和演示。 |
| CORE-005 | 每份代码证据保存来源类型、仓库、commit SHA、文件路径、URL、时间和原文。 |
| CORE-006 | 在保留原始 Evidence 的前提下，依据 changed files、diff hunk、错误栈、模块和问题关键词筛选进入模型的片段，并记录选择原因，避免把整个 PR 和完整日志直接送入模型。对固定 commit 的仓库，使用 AST、导入、调用和测试引用扩展未出现在 diff 中的候选源码；关系只用于召回，结论引用必须落到源码原文。 |

GitLab 保留现有兼容代码，但不作为核心版完成门禁；核心链路先以 GitHub 跑通。

### 6.2 飞书上下文与发布

| ID | 需求 |
| --- | --- |
| CORE-010 | 使用 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET` 获取 tenant access token，并在 Redis 缓存。 |
| CORE-011 | 通过配置为仓库指定允许读取的 `chat_id`、Wiki space/node 或文档 token。 |
| CORE-012 | 按时间范围分页读取指定群聊消息，转成统一证据并保留 message ID 和时间。 |
| CORE-013 | 读取指定 Wiki/文档内容，转成统一证据并保留 node/document token 和 URL。 |
| CORE-014 | 处理 Token 过期、分页、429、5xx 和权限不足；失败时任务显示原因并允许重试。 |
| CORE-015 | 审核通过后在指定 Wiki 位置创建或更新复盘页面，保存远端 URL/token。 |

核心版不实现飞书全局搜索和交互卡片。上下文检索范围通过 `.env` 或仓库配置表维护。发布后群通知为可选增强项 `OPT-001`，不阻塞核心链路验收。

### 6.3 PostgreSQL/pgvector 知识库与 RAG

| ID | 需求 |
| --- | --- |
| CORE-020 | PostgreSQL 存储问题、来源证据、复盘版本、审核记录和知识条目，不再依赖 JSON 作为运行时事实源。 |
| CORE-021 | pgvector 存储飞书、代码和历史复盘文本块的 embedding。 |
| CORE-022 | 支持按 `repo_id`、`source_type`、`incident_type`、时间范围和知识状态进行元数据过滤。 |
| CORE-023 | 使用 PostgreSQL 关键词/全文检索与 pgvector 向量检索进行混合召回；实现简单加权或 RRF 融合。首期不额外部署搜索引擎，也不把 PostgreSQL 原生全文检索表述为严格 BM25。 |
| CORE-024 | 返回 Top K 证据、历史案例、分数、原文片段和来源定位信息。 |
| CORE-025 | 只有审核并发布成功的知识版本参与默认历史案例召回。 |
| CORE-026 | 发布后的知识条目自动分块、生成 embedding 并标记为可检索。 |

核心版不要求复杂 reranker、MMR、大规模 ANN 调优和向量模型在线双版本切换。现有 reranker 能复用则保留，但不作为验收条件。

### 6.4 OpenAI 结构化复盘生成

| ID | 需求 |
| --- | --- |
| CORE-030 | 使用官方 `openai` Python SDK 和 Responses API，模型、base URL、超时与重试可配置。 |
| CORE-031 | 使用 Structured Outputs/JSON Schema 生成结构化复盘，不再依赖正则截取 JSON。 |
| CORE-032 | 复盘至少包含标题、问题摘要、影响、根因、修复方案、预防措施、相关模块、关键词和证据引用。 |
| CORE-033 | 将代码证据、飞书证据和历史案例统一组装为 LLM 上下文，按“当前问题直接证据 > 相关协作上下文 > 历史案例”分配可配置预算。 |
| CORE-034 | 保存模型、提示词版本、输入证据 ID、Token 用量、耗时和生成状态。 |
| CORE-035 | 超时、429、5xx 和 Schema 错误按简单规则重试，超过上限进入 `failed`。 |
| CORE-036 | 对超预算内容先做确定性裁剪、去重和分块；仍超限时才做分层摘要。摘要必须保留 Evidence ID 映射，首期允许使用同一模型，不要求多模型路由。 |

### 6.5 标准 Tool 与编排

核心版只要求以下 Tool：

| Tool | 职责 |
| --- | --- |
| `git.get_change_context` | 获取 PR、commit、diff 和 CI 上下文 |
| `feishu.get_context` | 获取配置群聊和文档上下文 |
| `knowledge.search` | 查询历史问题和处置经验 |
| `retrospective.generate` | 调用 OpenAI 生成结构化复盘 |
| `retrospective.validate` | 校验必填字段和证据引用 |
| `wiki.publish` | 发布飞书 Wiki 并回写远端信息 |

每个 Tool 提供明确名称、输入/输出 Schema、超时和错误信息，并记录 trace ID、耗时与结果状态。核心流程由固定状态机编排，LLM 不直接决定是否发布。

### 6.6 基础质量校验

| ID | 需求 |
| --- | --- |
| CORE-040 | 检查复盘必填字段和字段类型。 |
| CORE-041 | 根因、影响和修复结论至少分别引用一条候选 Evidence。 |
| CORE-042 | 检查 Evidence ID 存在且属于当前问题的候选上下文。 |
| CORE-043 | 检查引用片段可以在规范化原文中找到；无法定位时标记失败。 |
| CORE-044 | 输出字段完整率、证据覆盖率、引用准确率和总分，并区分硬校验与软评分：Schema、Evidence ID 和引用定位是硬校验，完整度或相似度等评分是软提示。 |
| CORE-045 | 硬校验失败时允许自动重新生成一次，仍失败则进入 `failed` 并允许人工重试；软评分偏低仍可进入 `pending_review`，但必须显示风险提示，不得伪装成高质量结果。 |

核心版只做基本日志脱敏，例如 Authorization、App Secret、API Key 和 Token；不建设完整 PII/DLP 系统。

### 6.7 人工审核与知识回流

| ID | 需求 |
| --- | --- |
| CORE-050 | 管理台展示复盘草稿、代码/飞书/历史证据和质量结果；根因、影响、修复方案可直接定位并并排查看引用证据。首期不实现通用文本 Diff 引擎。 |
| CORE-051 | 用户可以编辑复盘、补充或移除证据并提交审核。 |
| CORE-052 | 审核人可以通过或驳回；驳回必须填写原因。 |
| CORE-053 | 保存 AI 初稿和每次人工修改版本，保留审核人、动作、理由和时间。 |
| CORE-054 | 通过与发布是两个动作；只有 `approved` 状态才能发布。 |
| CORE-055 | 发布飞书 Wiki 成功后创建知识版本并安排 pgvector 索引。 |
| CORE-056 | 新知识索引成功后，另一个相似问题能够召回该案例。 |
| CORE-057 | 发布失败保留 `approved` 状态和错误，可手工重试且不重复创建内容。 |

### 6.8 页面与运行状态

核心版只要求三个主要页面：

1. **问题列表/详情**：状态、进度、失败原因、重试、代码和飞书证据。
2. **复盘审核**：草稿编辑、结论与证据并排查看、质量分/风险提示、通过、驳回和发布。
3. **知识库**：关键词/语义搜索、基础元数据过滤、来源和飞书 Wiki 链接。

运行状态只需提供：

- API、PostgreSQL、Redis、Worker、OpenAI 和飞书连接状态。
- 结构化日志和 trace ID。
- 页面可见的任务状态、阶段耗时、错误和重试次数。

不要求 Prometheus、Grafana 和完整告警系统。

## 7. 最小数据模型

| 表 | 用途 |
| --- | --- |
| `repositories` | GitHub 仓库和飞书检索范围配置 |
| `source_events` | Webhook delivery ID、类型、payload 摘要和幂等 |
| `incidents` | 问题信息、状态、当前复盘和失败原因 |
| `evidence_documents` | GitHub/飞书/历史来源正文与来源信息 |
| `evidence_chunks` | 分块文本、元数据和 pgvector embedding |
| `retrospective_versions` | AI 与人工复盘版本、质量分和输入证据 |
| `review_actions` | 提交、通过、驳回和发布历史 |
| `knowledge_entries` | 已发布知识、飞书 Wiki 信息和可检索状态 |
| `knowledge_chunks` | 知识分块与 pgvector embedding |
| `jobs` | 异步任务状态、阶段、重试和 trace ID |

`tool_execution` 和 `retrieval_run` 可以首期存为 JSONB 字段或简单附表，不要求构建复杂审计模型。

## 8. 核心 API

```text
POST /webhooks/github

GET  /api/health
GET  /api/connectors/status

POST /api/incidents
GET  /api/incidents
GET  /api/incidents/{id}
POST /api/incidents/{id}/retry
GET  /api/incidents/{id}/events
GET  /api/incidents/{id}/evidence

GET   /api/retrospectives/{id}
PATCH /api/retrospectives/{id}
POST  /api/retrospectives/{id}/submit
POST  /api/retrospectives/{id}/approve
POST  /api/retrospectives/{id}/reject
POST  /api/retrospectives/{id}/publish
GET   /api/retrospectives/{id}/versions

GET /api/knowledge/search
GET /api/knowledge/{id}
```

异步操作返回任务 ID。核心版不要求通用 `Idempotency-Key` 中间件，但 Webhook 和 Wiki 发布必须幂等。

## 9. 最小配置

```dotenv
POSTGRES_DB=review_agent
POSTGRES_USER=review_agent
POSTGRES_PASSWORD=
DATABASE_URL=postgresql+psycopg://review_agent:...@postgres:5432/review_agent
REDIS_URL=redis://redis:6379/0

OPENAI_API_KEY=
OPENAI_MODEL=
OPENAI_BASE_URL=https://api.openai.com/v1
EMBEDDING_MODEL=

GITHUB_TOKEN=
GITHUB_WEBHOOK_SECRET=
GITHUB_REPOSITORY=

FEISHU_BASE_URL=https://open.feishu.cn
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_CHAT_IDS=
FEISHU_WIKI_SPACE_ID=
FEISHU_WIKI_PARENT_NODE_TOKEN=

REVIEWER_NAMES=
RAG_TOP_K=8
LLM_MAX_RETRIES=2
```

`.env.example` 不得放真实密钥。核心版不要求 Docker secrets 或外部密钥管理系统。

## 10. 核心完成定义

同时满足以下条件，核心版才算完成：

1. Docker Compose 可以在干净环境一键启动。
2. PostgreSQL/pgvector 和 Redis 被主链路真实使用，不发生静默本地回退。
3. 至少一个真实 GitHub 测试事件能完成代码上下文采集。
4. 至少能从一个真实飞书测试群和一个真实 Wiki/文档获取上下文。
5. OpenAI SDK 能生成带真实 Evidence 引用的结构化复盘。
6. 人工可以修改、驳回、通过和发布复盘。
7. 复盘发布到飞书 Wiki，并写入知识库和 pgvector。
8. 新问题能够召回刚发布的历史案例。
9. 后端核心测试、前端类型检查和构建全部通过。

“整理耗时降低约 50%”不作为核心版本发布阻断项。核心链路稳定后，再用一组内部案例做轻量统计；没有真实数据前不对外使用该数字。

## 11. 外部参考

- [OpenAI Responses API（Python）](https://developers.openai.com/api/reference/python/resources/responses/methods/create)
- [飞书 tenant_access_token](https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal)
- [飞书会话历史消息](https://open.feishu.cn/document/server-docs/im-v1/message/list)
- [飞书发送消息](https://open.feishu.cn/document/server-docs/im-v1/message/create)
- [飞书 Wiki 子节点](https://open.feishu.cn/document/server-docs/docs/wiki-v2/space-node/list)
- [飞书创建 Wiki 节点](https://open.feishu.cn/document/server-docs/docs/wiki-v2/space-node/create)
