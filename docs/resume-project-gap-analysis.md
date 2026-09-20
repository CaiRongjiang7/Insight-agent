# “研发问题复盘 Agent”简历描述与项目实现差异评估

## 1. 评估说明

- 评估对象：当前仓库 `Insight-agent-yuan`
- 代码基线：`44c3e2c`（`main`，`Merge pull request #1 from Goodeve0/feat/bug-review-pipeline`）
- 评估日期：2026-09-14
- 评估方法：依赖与配置检查、关键调用链静态追踪、后端测试、前端构建验证
- 结论口径：只有已经接入主流程、能够运行且有代码或测试证据的能力，才视为“已落地”；只有类、函数、依赖或注释但没有调用链的能力，视为“部分实现”或“预留能力”。

本评估不验证简历之外的口头经历，也不把 Demo 预置数据等同于真实系统运行结果。

## 2. 总体结论

当前仓库具备一套较完整的研发问题分析与 Bug 复盘原型组件，包括 GitHub/GitLab 事件解析、仓库上下文分析、历史记忆与知识条目检索、向量后端、结构化复盘 Schema、人工审核状态机、本地 Markdown 发布、前端管理界面等。

但原简历描述把多项“可选实现、未接入模块或后续规划”写成了“已落地的端到端生产能力”。主要差异如下：

1. 没有飞书开放平台接入，也没有飞书上下文检索；飞书 Wiki 仅出现在未来规划注释中。
2. 没有 Redis 依赖或实现。
3. PostgreSQL/pgvector 是可选向量后端，不是当前知识库的主存储；知识条目实际保存为 JSON，执行记忆和向量兜底主要使用 SQLite。
4. 项目调用 OpenAI 兼容 HTTP API，但没有使用官方 `openai` Python SDK。
5. Bug 复盘所需的事件解析、历史案例增强、结构化生成、质量评分、人工审核和发布模块没有真正串成一条端到端调用链。
6. “结果校验—人工审核—知识入库”没有闭环：发布动作只生成本地 Markdown 并更新独立审核状态，没有把审核后的复盘内容写回知识库。
7. 仓库中没有能支撑“整理耗时降低约 50%”的基线、样本量、统计口径或实验数据。
8. 当前主分支存在后端流程回归和前端类型错误，不能据此宣称稳定可用。

因此，原描述按当前仓库证据直接用于简历，面试追问风险较高。更准确的定位是：**研发问题复盘与代码维护 Agent 原型 / Bug 复盘知识库原型**。

## 3. 技术栈逐项核验

| 简历技术栈 | 评估 | 仓库证据 | 主要差异与建议 |
| --- | --- | --- | --- |
| Python | 已实现 | 后端代码位于 `backend/src/repo_maintainer/`，项目要求 Python 3.12+ | 可以保留。 |
| OpenAI SDK | 描述不准确 | `llm_executor.py` 和 `embeddings.py` 使用 `urllib.request` 直接调用 OpenAI 兼容 HTTP 接口；`pyproject.toml` 没有 `openai` 依赖 | 改为“OpenAI 兼容 API”或真正接入官方 OpenAI Python SDK 后再写“OpenAI SDK”。 |
| PostgreSQL | 部分实现 | `vector_store.py:596-710` 实现 `PgVectorStore`，依赖 `psycopg` | 仅用于可选向量后端；当前知识条目不是基于 PostgreSQL 存储。建议写“可选 pgvector 后端”，或补齐真实部署与主链路使用证据。 |
| pgvector | 部分实现 | `vector_store.py:596-710` 有建表、upsert 和余弦距离查询；`config.py:342-368` 默认没有 DSN | 代码适配存在，但缺少集成测试、部署配置和当前运行证据。不能直接写“基于 PostgreSQL/pgvector 构建知识库”。 |
| Redis | 未实现 | 后端依赖、源码和配置中均未发现 Redis 客户端或 Redis 数据结构使用 | 从简历删除，或补齐缓存、任务队列、幂等/状态管理等真实用途。 |
| RAG | 部分实现 | `retrieval.py` 支持向量、词法分数、reranker、MMR；`platform.py:262-312` 会召回技能和历史记忆 | RAG 能力存在于代码维护主链路，但没有接入 `generate_bug_review()`；BM25 组件也未在平台初始化时注入。建议写“实现向量与词法融合检索原型”。 |
| 飞书开放平台 | 未实现 | `wiki_publisher.py:5,25-28,59` 明确将飞书 Wiki 标为后续能力，remote 模式直接返回未实现 | 从当前简历删除，或完成飞书鉴权、消息/文档检索、权限处理、限流重试和 Wiki 发布后再写。 |

补充说明：根 README 把数据存储写成“JSON 文件 + SQLite”，把 LLM 集成写成“OpenAI 兼容 API”，这比原简历的技术栈更接近当前实现。

## 4. 两条项目描述逐项对比

### 4.1 “Agent 工作流与工具建设”

原描述：

> 面向研发问题复盘场景，设计并落地覆盖“代码提交解析—飞书上下文检索—历史问题查询—复盘材料生成”的端到端 Agent 工作流，将代码、协作平台及知识库能力封装为标准 Tool，实现多源研发信息的自动汇聚与编排。

| 描述要点 | 评估 | 实际实现 | 差异 |
| --- | --- | --- | --- |
| 代码提交解析 | 部分实现 | `PRListener` 可解析 GitHub PR/push/workflow 和 GitLab MR/push | `/webhooks/git` 只返回解析结果，没有持久化 Bug、触发抽取任务；GitHub 签名值被读取但没有调用验证函数；普通 PR Webhook 通常也不会直接携带完整文件列表。 |
| 飞书上下文检索 | 未实现 | 无飞书鉴权、搜索、消息、文档或 Wiki API 客户端 | 属于原描述中最大的事实缺口。 |
| 历史问题查询 | 部分实现 | `KnowledgeBase`、`MemoryStore`、`SemanticRetriever` 能查询知识条目与执行记忆 | `KnowledgeMatchSkill.run()` 没有调用方；Bug 复盘生成没有收到历史案例上下文。 |
| 复盘材料生成 | 模块存在但未接入 | `OpenAIPatchExecutor.generate_bug_review()` 能构造结构化复盘请求，`BugReviewDocument` 定义了标准输出 | 全仓仅有方法定义，没有业务调用方；`/api/extractions` 实际走 `repair_incident()` 代码修复流程。Demo 中的复盘文档是预置数据，不能证明自动生成链路已运行。 |
| 标准 Tool | 部分实现 | `AgentToolExecutor` 封装了 `context.inspect`、`memory.lookup`、`graph.trace`、`sandbox.exec` 等内部工具 | 没有飞书/协作平台 Tool；这些 Tool 主要服务代码维护 Agent，而非简历所述的多源复盘链路。 |
| 多源信息自动汇聚与编排 | 部分实现 | 代码仓库、CI incident、历史记忆可进入 `prepare_incident()` | 缺少飞书来源，Webhook 到复盘生成也未闭合，不宜写成“端到端已落地”。 |

结论：这一条约有一半的基础模块，但缺少协作平台来源和关键集成调用。建议将“设计并落地端到端工作流”改为“设计并实现原型模块”，除非先完成第 8 节的 P0 补齐项。

### 4.2 “证据检索与质量闭环”

原描述：

> 基于 PostgreSQL/pgvector 构建研发问题知识库，通过元数据过滤与 RAG 召回相关上下文及历史处置经验，为复盘生成提供可追溯证据；建立“结果校验—人工审核—知识入库”闭环，将审核案例回流用于后续检索，使复盘材料整理耗时降低约 50%。

| 描述要点 | 评估 | 实际实现 | 差异 |
| --- | --- | --- | --- |
| PostgreSQL/pgvector 知识库 | 部分实现、表述过强 | 有可选 `PgVectorStore`；知识条目实际由 `KnowledgeBase` 写入 JSON，记忆在 SQLite | pgvector 存的是检索向量和 payload，不是当前唯一或默认知识库；默认 `VECTOR_BACKEND=auto`，候选顺序优先 FAISS/HNSW，再回退 SQLite。 |
| 元数据过滤 | 很弱 | `KnowledgeMatchSkill` 可按 `published_knowledge_ids` 排除循环引用；内存查询按 incident type、cluster、keyword 初筛 | 没有简历语义下常见的仓库、团队、时间、问题类型、权限等组合过滤；pgvector 查询只按 collection 和候选 item IDs 限定。 |
| RAG 召回上下文和历史经验 | 部分实现 | `SemanticRetriever` 可对知识条目和历史记忆做向量/词法/成功率融合排序 | 平台没有注入 `BM25Index`；Bug 复盘生成方法没有调用知识匹配模块；中文词法切分能力也较弱。 |
| 可追溯证据 | 部分实现 | `BugReviewDocument` 强制 `root_cause.source`；解析器要求 snippet 非空；`QualityScorer` 能检查 snippet 是否出现在原文 | `validate_bug_review_document()` 只检查 snippet 非空，不检查其确实来自原文；真正的原文包含校验在 `score_extraction()` 中，但该方法没有业务调用方。 |
| 结果校验 | 模块存在但未接入 | `QualityScorer.score_extraction()` 提供四维启发式评分 | 全仓没有调用方，不能算已建立运行闭环。 |
| 人工审核 | 部分实现 | 有 draft/pending/approved/rejected/published 状态机、审核 API 和前端编辑界面 | API 的 `approve` 会从默认 draft 直接请求 approved，而状态机不允许 draft→approved；Demo 审核记录写成单独文件，状态机实际读取的是 `review_records.json`。 |
| 知识入库与回流 | 未闭环 | 发布动作可以输出本地 Markdown | `/api/reviews/{id}/publish` 没有创建或更新 `KnowledgeEntry`，因此审核案例不会自动参与后续检索。 |
| 耗时降低约 50% | 无证据 | 未发现基线时长、自动化后时长、样本量、统计脚本或实验报告 | 建议暂时删除该数字。只有完成可复现测量后再使用，并保留样本与计算口径。 |

结论：有“证据 Schema、检索器、评分器、审核状态机、发布器”等组件，但质量闭环没有实际闭合，量化收益没有证据。

## 5. 当前真实调用链与目标调用链

### 5.1 当前调用链

```text
GitHub/GitLab Webhook
  -> PRListener.parse_*()
  -> API 直接返回 BugReport
  -> 未持久化、未触发复盘生成

POST /api/extractions
  -> FrontendDataService.retry_incident()
  -> RepoAutonomyPlatform.repair_incident()
  -> 代码修复/验证流程，而不是 generate_bug_review()

历史案例增强
  -> KnowledgeMatchSkill.run() 已定义
  -> 没有调用方

结构化复盘生成
  -> OpenAIPatchExecutor.generate_bug_review() 已定义
  -> 没有调用方

审核发布
  -> ReviewWorkflowEngine + WikiPublisherService(local)
  -> 本地 Markdown
  -> 未 upsert KnowledgeEntry，无法形成审核案例回流
```

### 5.2 原简历所描述的目标调用链

```text
Git 提交/PR/CI 事件
  -> 验签、幂等、持久化、任务触发
  -> 拉取 diff/commit/日志
  -> 飞书消息与文档上下文检索
  -> PostgreSQL 元数据过滤 + pgvector 混合召回
  -> LLM 生成带证据引用的复盘材料
  -> 质量校验
  -> 人工审核
  -> 飞书 Wiki/知识库发布
  -> 审核版本入库并可被后续 RAG 召回
```

两条链路的主要缺口位于：Webhook 后续编排、飞书数据源、Bug 复盘生成接线、质量评分接线、审核状态一致性、发布后知识回流，以及可验证的外部存储部署。

## 6. 可运行性验证结果

### 6.1 后端

执行命令：

```powershell
$env:PYTHONPATH='backend/src'
backend/.venv/Scripts/python.exe -m unittest discover -s backend/tests -p 'test_*.py' -v
```

结果：共运行 32 个测试，17 个通过、2 个失败、13 个错误。

主要问题：

1. `RepoAutonomyPlatform.prepare_incident()` 调用 `SkillRepository.match_skills()`，但当前 `SkillRepository` 没有该方法，导致分析、学习和修复主流程的多项测试直接报错。
2. 两个安全审批相关测试未抛出预期异常；需要进一步隔离本地环境配置后确认是配置污染还是逻辑回归。
3. `test_api` 无法导入，因为当前虚拟环境缺少 Starlette TestClient 所需的 `httpx2`。
4. 新增的 `PRListener`、`generate_bug_review()`、`ReviewWorkflowEngine`、`WikiPublisherService` 和审核后知识回流没有对应端到端测试。

### 6.2 前端

执行命令：

```powershell
cd frontend
npm.cmd run build
```

结果：构建失败。`CiAutofixView.vue:442` 传入的 `root_cause.source` 是可选字段，但 `BugReviewDocument` 类型要求该字段必填，TypeScript 报 `TS2322`。

### 6.3 验证结论

仓库可以用于展示架构和部分模块能力，但当前主分支不能作为“完整、稳定、端到端可运行”的证据。建议先修复主流程回归、补齐测试依赖和前端类型问题，再做简历量化描述。

## 7. 当前仓库能够支撑的简历版本

以下版本不包含无法从仓库验证的飞书、Redis、生产级 PostgreSQL/pgvector 主存储和 50% 效率数据。

> **项目二：研发问题复盘与代码维护 Agent 原型**  
> **技术栈：** Python、FastAPI、OpenAI 兼容 API、SQLite/JSON、向量检索、RAG、Vue 3  
> - **Agent 工作流与上下文编排：** 面向 PR/CI 研发问题场景，设计 GitHub/GitLab 事件解析、仓库代码索引、日志压缩、历史记忆召回与多 Agent 分析流程，并将上下文检查、依赖追踪、历史查询和沙箱验证封装为受控 Tool。  
> - **知识检索与审核原型：** 实现知识条目和历史执行记忆的向量/词法融合检索，设计带来源引用的结构化 Bug 复盘 Schema、质量评分器、人工审核状态机及本地 Markdown 发布能力；为 pgvector 向量后端预留可配置适配。

如果简历篇幅有限，还可以压缩为：

> 设计研发问题复盘与代码维护 Agent 原型，打通 GitHub/GitLab 事件解析、仓库上下文构建、历史记忆检索和多 Agent 分析；实现向量/词法融合召回、来源引用 Schema、人工审核状态机与本地知识文档发布，并支持可选 pgvector 向量后端。

注意：“打通”在这里仅用于已经进入 `prepare_incident()` 的代码维护分析链；在修复第 6 节的主流程回归之前，面试中应明确说明当前是原型，而不是生产系统。

## 8. 若要保留原简历描述，需要补齐的工程项

### P0：闭合复盘主链路

1. Webhook 调用签名/Token 验证，保存解析后的 BugReport，并通过幂等键创建复盘任务。
2. 拉取 PR/MR 的 commit、diff、changed files、review comments 和 CI 日志，而不是只依赖 Webhook 请求体。
3. 在编排服务中依次调用：
   - `KnowledgeMatchSkill.run()`；
   - `OpenAIPatchExecutor.generate_bug_review()`；
   - `QualityScorer.score_extraction()`；
   - 持久化 `review_document` 与质量分数；
   - 提交人工审核。
4. 修复审核状态：先 draft→pending，再 pending→approved；统一 incident 字段与审核记录的数据源。
5. 发布成功后把审核版本转换为 `KnowledgeEntry` 并 upsert，再写入向量索引，验证下一次检索能够召回。
6. 修复 `SkillRepository.match_skills()` 接口回归和前端 `BugReviewDocument` 类型错误。

### P1：落实简历中的基础设施

1. 将问题、复盘版本、来源证据、审核记录和知识条目迁移到 PostgreSQL，定义迁移脚本与唯一约束。
2. 明确启用 pgvector，建立适合规模的向量索引，并增加仓库、团队、问题类型、时间范围、审核状态和权限等元数据过滤。
3. 为 PostgreSQL/pgvector 增加真实集成测试，禁止配置失败时静默回退却仍对外宣称使用 pgvector。
4. 若保留 Redis：明确它承担缓存、任务队列、分布式锁、Webhook 幂等或流式任务状态中的哪一项，并提供故障降级与测试。
5. 若保留 OpenAI SDK：改用官方 Python SDK，并为超时、重试、结构化输出和错误类型编写测试。

### P1：落实飞书开放平台

1. 实现 tenant/user access token 管理和刷新。
2. 封装飞书消息搜索、群聊上下文、文档/Wiki 读取与发布 Tool。
3. 处理分页、权限、限流、重试、敏感信息脱敏和来源 URL/消息 ID 引用。
4. 将飞书检索结果写入统一证据模型，并在复盘中可追溯到原消息或文档。
5. 增加契约测试或可重复的沙箱联调记录。

### P1：建立量化收益证据

至少选取 20 个具有代表性的研发问题，记录：

- 人工方式：从收集信息到形成可审核初稿的耗时；
- Agent 方式：自动运行时间、人工补充时间、审核修改时间；
- 质量：必填字段完整率、证据引用准确率、一次审核通过率；
- 检索：Recall@K、MRR、无关案例率；
- 成本：平均 Token、模型费用、失败重试次数。

推荐使用中位数而不是只使用平均值，并按以下公式计算：

```text
整理耗时降低比例 = (人工基线中位耗时 - Agent 后人工中位耗时) / 人工基线中位耗时
```

只有在实验能够稳定复现约 50% 的结果时，才在简历中保留该数字，同时准备好样本量、基线、统计口径和异常值处理方式。

## 9. 原描述可恢复使用的验收标准

满足以下条件后，原简历描述才具有较强证据支撑：

- 至少 10 条真实或脱敏案例完整通过“Git 事件→飞书检索→历史检索→复盘生成→质量校验→人工审核→知识入库”。
- PostgreSQL 中能查询到问题、证据、复盘版本和审核记录；pgvector 查询实际执行并带有业务元数据过滤。
- Redis 有明确职责和可观察的读写证据，而不是仅出现在依赖列表。
- 飞书 API 完成真实联调，复盘中的飞书证据可回跳或通过稳定 ID 定位。
- 审核后的知识条目能被后续问题召回，且有端到端自动化测试。
- 后端与前端构建全部通过，核心链路测试覆盖成功、拒绝、重试、无权限和外部服务失败场景。
- “耗时降低约 50%”有可复现的对照实验记录。

## 10. 最终建议

短期用于投递时，采用第 7 节的保守版本，并删除 Redis、飞书开放平台和“降低约 50%”。如果岗位特别重视 RAG，可以保留 pgvector，但应表述为“实现可选 pgvector 向量后端适配”，不要写成当前知识库已经完全建立在 PostgreSQL/pgvector 上。

中期若希望恢复原描述，应优先闭合主调用链和审核后知识回流，再接飞书与生产存储，最后通过实验补上量化结果。这样不仅能降低面试追问风险，也会让项目从“模块较多的原型”变成真正可演示、可验证的端到端系统。
