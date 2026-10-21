# 智能复盘证据来源与上下文管理台账

> 当前实现基线：2026-09-28；最近文档核对：2026-10-01。本文只描述 `RetrospectivePipeline` 智能复盘主链路；整个项目的组件关系与部署见[项目架构文档](architecture.md)。旧代码修复 Agent 的记忆和 Token 预算不是本链路的运行机制。**每次修改复盘方案或实现时，同步修订相关章节，并在文末追加变更记录。**

## 1. 证据从哪里来

一次复盘先收集原始材料，写入 `evidence_documents`，再从中选择进入模型的片段。来源类型由 [`EvidenceSource`](../backend/src/repo_maintainer/retrospective_models.py) 定义；单条 `Evidence` 包含 `id`、`incident_id`、`source_type`、`content`、`location`、`source_url`、`repo_id`、`commit_sha`、`occurred_at`、`metadata`、`selected` 和 `selection_reason`。有字段不等于该来源一定提供了有效值，取决于连接器和外部系统。

| 来源类型 | 当前采集方式 | 可追溯定位 | 事实属性与边界 |
| --- | --- | --- | --- |
| `github_pr` | PR 标题/描述及 issue 评论；手工记录的非 `manual` 问题也可能形成此类条目 | PR URL、评论 URL、仓库/PR 编号 | **参与者陈述**，可以说明意图或排查过程，不能单独证明故障机理。 |
| `github_commit` | PR commits 的提交消息 | Commit SHA、commit URL | **提交者陈述**，不是代码执行结果。 |
| `github_diff` | PR files 接口返回的文本 `patch` | 文件路径、PR/文件 URL、问题关联的 SHA | **代码变更原文**；接口未返回 `patch` 时不会生成该条证据，diff 不覆盖未改动代码。 |
| `repository_code` | 固定 PR head SHA（缺失时用问题记录的 commit）通过 GitHub ZIP 归档接口下载同仓库源码到临时目录；用 diff 行、错误栈、符号、调用、导入和测试引用定位源码片段，不读取 Agent 工作区的本地仓库 | SHA、文件路径、起止行及固定版本的 blob URL | **该版本源码原文**。`relation`/`retrieval_score` 只是静态候选线索，不能单独证明运行时调用或因果。基础上限 10 个文件，多 diff 时最多扩至 40 个文件，每文件最多 2 个不同区域、每段最多约 2,400 字符；私有仓库的令牌需要 `Contents: Read` 权限。 |
| `ci_log` | GitHub Actions 中与记录的 SHA 对应的失败 run 日志；手工问题也可提供日志 | Run URL 或问题 ID、SHA | **运行记录**。若日志归档不可取，连接器可能只保存 run URL；手工日志的真实性仍需核对。 |
| `feishu_message` | 配置群在问题时间窗口内的消息 | `chat_id`、`message_id`、时间、`feishu://message/{id}` | **沟通记录**，其中的判断仍是人的陈述。 |
| `feishu_doc` | 配置的文档 token 对应的原文 | 文档 token、文档 URL | **文档陈述**，可能比事故发生时间晚，需核对时序。 |
| `manual` | 手工问题的标题和描述 | 问题 ID、可选 `source_url` | **提交人陈述**；不应默认为已验证事实。 |
| 历史案例 `KnowledgeHit` | 审核、发布并入库的复盘，通过 PostgreSQL 全文检索与 pgvector 召回 | `knowledge_id`、历史问题 ID、发布 URL | **类比参考**。目前作为 `historical_cases` 输入，**不是当前问题的 `Evidence`，没有可用于当前引用校验的 Evidence ID**。枚举中的 `historical` 不代表主链路会将召回案例转成 Evidence。 |

采集实现见 [`retrospective_connectors.py`](../backend/src/repo_maintainer/retrospective_connectors.py) 和 [`retrospective_code_context.py`](../backend/src/repo_maintainer/retrospective_code_context.py)；历史案例的入库与检索见 [`retrospective_pipeline.py`](../backend/src/repo_maintainer/retrospective_pipeline.py) 和 [`retrospective_store.py`](../backend/src/repo_maintainer/retrospective_store.py)。

### 当前方案的依据索引

这里登记的是**方案判断所依据的代码与验证**，不同于运行时某个问题的 Evidence ID。后续变更应更新受影响的行，并在文末追加变更记录。测试只证明覆盖的行为，不代表真实事故的复盘成功率。

| 依据 ID | 当前可核对的判断 | 一手依据 | 验证范围 |
| --- | --- | --- | --- |
| `CTX-01` | PR、CI、飞书及手工材料按来源生成 Evidence | [`GitHubConnector.collect`、`FeishuConnector.collect`](../backend/src/repo_maintainer/retrospective_connectors.py) | 连接器单元测试和模拟响应；真实权限与缺失数据仍需联调。 |
| `CTX-02` | 未出现在 diff 的相关源码可通过静态关系进入候选；多 diff 文件可扩展候选上限，同文件多个远离的改动区域可分别取证；唯一目标文件的调用/导入边可提供两跳候选 | [`RepositoryCodeContext.collect`](../backend/src/repo_maintainer/retrospective_code_context.py)、[`test_multiple_diff_files_expand_beyond_base_limit_and_keep_caller`](../backend/tests/test_retrospective_pipeline.py)、[`test_separate_diff_hunks_in_one_file_produce_separate_code_evidence`](../backend/tests/test_retrospective_pipeline.py)、[`test_two_hop_graph_finds_unchanged_entry_point`](../backend/tests/test_retrospective_pipeline.py) | 验证特定 Python 调用方、多文件/多 hunk 与两跳入口情形；不覆盖动态调用或所有语言。 |
| `CTX-03` | 原文入库，筛选出的片段受字符预算约束；不同改动文件优先各得一个源码片段 | [`ContextSelector`](../backend/src/repo_maintainer/retrospective_pipeline.py)、[`add_evidence`](../backend/src/repo_maintainer/retrospective_store.py)、[`test_context_selection_covers_distinct_changed_code_files`](../backend/tests/test_retrospective_pipeline.py) | 验证选择器与存储语义；预算不足时仍可能遗漏部分文件，且未保存完整模型请求。 |
| `CTX-04` | 核心字段引文按 Evidence ID 和原文字符串校验 | [`QualityValidator`](../backend/src/repo_maintainer/retrospective_pipeline.py)、[`test_quality_validator_rejects_unknown_or_fabricated_citations`](../backend/tests/test_retrospective_pipeline.py) | 验证存在性与字符串匹配；不验证因果支持度。 |
| `CTX-05` | 历史案例经发布后入库并用于后续召回 | [`RetrospectivePipeline`](../backend/src/repo_maintainer/retrospective_pipeline.py)、[`test_complete_review_publish_and_retrieval_loop`](../backend/tests/test_retrospective_pipeline.py) | 验证模拟闭环；不等于线上召回质量。 |

### 单个问题的来源记录模板

运行时明细以 `evidence_documents` 和复盘页面为准。需要人工审查或导出某个案例时，可在该案例的材料中使用下表；不要在本台账写入真实 Token、密钥或无关个人信息。

| Incident ID | Evidence ID / 历史 Knowledge ID | 所支持的结论 | 来源 URL 或消息 ID | SHA、文件与行号 / 时间 | 原文核对结果 | 限制或争议 |
| --- | --- | --- | --- | --- | --- | --- |
| 待填 | 待填 | 待填 | 待填 | 待填 | 待填 | 待填 |

## 2. 上下文如何构建与压缩

当前流程：**GitHub/飞书原始采集 → 局部代码扩展 → 全部 Evidence 原文入库 → 去重和相关性筛选 → 裁剪进入模型的副本 → 历史案例召回 → 结构化生成 → 双层引用校验**。代码扩展发生在入库前，扩展结果本身也作为原始 Evidence 入库。入口见 [`RetrospectivePipeline.run()`](../backend/src/repo_maintainer/retrospective_pipeline.py)。

| 步骤 | 已实现的方法 | 被保留的内容与限制 |
| --- | --- | --- |
| 固定代码版本 | 保存 webhook 的 head/base SHA；源码扩展使用 head SHA，缺失时使用记录的 commit SHA | 代码 Evidence 带实际使用的 SHA 和行号。当前主要分析**修复后的版本**；修复前状态主要来自 diff，尚未自动建立前后两版源码对照。归档不可用时保留其他证据，并在问题元数据记录 `code_context_warning`。 |
| 局部源码扩展 | 每个 diff 文件的新增行映射 Python AST 函数/类；结合错误栈行号、调用方/被调用方、导入关系及测试引用选候选；唯一目标文件的调用/导入边可扩至两跳；同文件最多取 2 个不同区域，候选文件基础 10 个，按改动文件数最多扩至 40 个，并给未改动文件留名额 | 先临时索引同仓库快照，再只发送候选源码片段及来源定位，不把全仓代码放入模型。已有索引包含调用、依赖和部分数据流/控制流图信号；两跳关系要求目标文件唯一。跨语言和动态调用可能漏召回或误召回，图关系只作为检索线索。 |
| 去重与排序 | 内容规范化后按指纹去重；源码额外包含位置，避免不同文件的同文片段合并；按来源优先级、关键词重合和源码检索分排序 | `selection_reason` 记录选择原因；保留不同来源的首条候选，源码优先留一条非 diff 文件。排序不是事实可信度评分。 |
| 日志裁剪 | 优先取包含错误、异常、失败等词或问题关键词的行及前后各 2 行；无匹配时取前 120 行 | 原日志仍在库；模型只看裁剪后的副本。若后续预算再次截断，日志取首尾片段。 |
| Diff 裁剪 | 选中与问题关键词重合的 hunk；无匹配时取前 3 个 hunk | 原始 patch 仍在库；模型可能只看到部分 hunk。后续预算截断时保留首尾。 |
| 证据预算 | 默认选中证据总内容不超过 24,000 字符；单条配置上限 6,000 字符；CI、diff、源码、飞书消息/文档、PR、commit 来源优先各留一个名额。之后以总预算约一半作为改动源码的优先覆盖额度，尽量让不同改动文件各有一个片段 | 这是**字符预算**，不是精确 Token 预算；单条首尾拼接会增加裁剪标记，总量仍由选择器限制。文件过多或其他证据占用较高时仍可能遗漏。手工来源参与后续统一排序。配置见 `CONTEXT_BUDGET_CHARS`、`CONTEXT_ITEM_MAX_CHARS`。 |
| 历史检索 | 用标题、前 500 字描述、问题类型、最多 3 条故障行及相关代码路径/符号组成查询，最多约 1,800 字符；按仓库、问题类型过滤，默认 Top 5 | 已发布知识参与检索。历史案例用于启发，不替代当前证据。 |
| 生成输入 | 传入筛选后的 Evidence ID、来源、位置、正文、SHA 和选择原因；问题描述最多 2,000 字符；历史案例默认 5 条，`RAG_TOP_K` 最大 20，每条正文最多 1,500 字符 | 不把完整问题日志再次作为 `incident` 字段传入；日志经 Evidence 选择进入模型。模型实际输入还包含 JSON 字段和工具 Schema，**未实现整体精确 Token 预估或硬限额**。 |
| 驳回后重新生成 | 从库中读取原始内容，对先前选中的证据再次执行选择器 | 保持本次生成的字符预算；若选择器代码或配置变化，重新生成时看到的片段可能改变。 |

选择、裁剪与检索查询见 [`retrospective_pipeline.py`](../backend/src/repo_maintainer/retrospective_pipeline.py)；模型输入见 [`OpenAIReviewGenerator.generate()`](../backend/src/repo_maintainer/retrospective_connectors.py)。代码归档大小、基础和扩展文件数可通过 `CODE_CONTEXT_MAX_ARCHIVE_BYTES`、`CODE_CONTEXT_MAX_FILES`、`CODE_CONTEXT_MAX_EXPANDED_FILES` 调整，`CODE_CONTEXT_ENABLED=0` 可关闭扩展。

## 3. 客观事实如何保留与校验

1. **原文与模型片段分开。** 流水线先写原始 `Evidence.content`；选择器在深拷贝上裁剪。存储层重复写同一 Evidence ID 时只更新 `selected` 和 `selection_reason`，不会用裁剪文本覆盖原文。数据库表见 [`retrospective_schema.sql`](../backend/src/repo_maintainer/retrospective_schema.sql) 和 [`retrospective_store.py`](../backend/src/repo_maintainer/retrospective_store.py)。这描述的是应用写入行为，不等于数据库具有防篡改保证。
2. **来源可回查。** 当前证据保留 Evidence ID 与可得的 URL、消息 ID、SHA、文件/行号或时间；生成版本保存使用过的 Evidence ID、模型、Token 用量与耗时。某些来源可能没有可直接打开的 URL，应以其他定位字段核对。
3. **推断与观察分离。** AST/调用/导入关系的 `relation` 标记为 `static_candidate`，只负责召回。PR 描述、聊天、手工输入和历史案例都可能含未经核实的说法。源码原文证明“该版本写了什么”，日志证明“记录了什么”；二者都不能单独证明完整的根因、影响范围或运行时调用路径。
4. **引用硬校验。** 影响、根因、修复方案各需引用 Evidence ID。AI 生成和驳回后重新生成时，引文须同时出现在**本次模型看到的片段**与**入库原文**中；人工编辑时校验入库原文。缺 ID、引文不匹配或核心字段无引用时不能进入下一步；模型提示在证据不足时写未知。见 [`QualityValidator`](../backend/src/repo_maintainer/retrospective_pipeline.py) 和 [`OpenAIReviewGenerator`](../backend/src/repo_maintainer/retrospective_connectors.py)。
5. **人工审核。** AI 结果进入待审核状态；人工可编辑、驳回、通过，审核通过并发布后才把复盘分块加入历史知识库。引用字符串匹配只校验可定位性，**不能自动校验结论是否由引文充分支持**，因果判断仍需审核人核对。

### 当前未解决的可追溯性问题

- 数据库保存原文与“是否选中”，但**没有单独持久化每次模型实际看到的裁剪片段、完整请求或其哈希**；只靠 Evidence ID 不能精确重放旧生成，尤其在选择器改版后。
- 当前总预算按字符而非模型 Token 计量；历史案例、问题字段、JSON 包装及工具 Schema 没有统一的请求级硬限额。
- 代码扩展是静态启发式，Python AST 与 JS/TS 解析能力不同；同名函数、动态分派、反射及跨服务调用可能产生误连或遗漏。归档超限或下载失败时会跳过代码扩展。
- GitHub 的大文件或二进制变更可能没有 `patch`；CI 日志取证目前只针对关联 SHA 的一个失败 run，可能缺少最初复现时的运行记录。
- `QualityValidator` 检查引文存在与字段覆盖，不检查“引文是否真的支持该因果结论”；尚无基于真实历史问题的成功率或审核通过率测量。
- [`map_reduce_summary.py`](../backend/src/repo_maintainer/map_reduce_summary.py) 是独立的长文本摘要模块，**未接入本复盘主链路**。不得将其写为当前已运行的压缩步骤。

### 可评估的下一步增强（均未实现）

| 候选方法 | 客观事实保留方式 | 上线前验证 |
| --- | --- | --- |
| 证据感知压缩 | 从原文提取时间、错误、组件、版本、前后行为等事实项；每项保留 Evidence ID、原文短引、SHA/行号或消息时间，摘要只做导航，不作为独立证据。冲突陈述并列保留。 | 比较压缩前后关键事实和引文的保留率，并检查是否引入原文没有的事实。 |
| 完整输入快照 | 持久化每次生成实际发送的片段、历史案例 ID、选择器/提示词版本、预算和内容哈希；敏感内容沿用现有数据访问控制。 | 同一版本能重放输入；任何片段变化都能在哈希或版本记录中发现。 |
| 真实 Token 预算与分层召回 | 按实际模型计量输入，给故障日志、变更代码、未修改关联代码和协作记录分配可调整预算；覆盖不足时按错误栈和符号逐轮扩展。 | 在真实案例集上测量根因相关源码召回率、无关片段比例、超限率和成本。 |
| 结论支持度检查 | 对每条根因及影响结论建立“结论 → 直接观察 → 静态推断/人的陈述”的支持关系；无法证明的部分明确写“待验证”。 | 人工盲审无依据结论比例、误判率和审核通过率，比较改动前后。 |

只有在实现、测试和真实案例验证后，才能把这些方法移到“已实现”部分，并在下方追加变更记录。

## 4. 结构化复盘如何进入 RAG 知识库

**入库门槛。** `RetrospectiveDocument` 的完整结构化版本保存在 `retrospective_versions.document`（JSONB）。AI 草稿先经引用校验和人工审核；只有审核通过且发布到飞书 Wiki 的版本，才由 [`RetrospectivePipeline.publish()`](../backend/src/repo_maintainer/retrospective_pipeline.py) 建立知识条目。发布 URL、版本 ID、仓库和问题类型分别用于回查、版本定位和检索过滤。未审核的草稿不进入历史知识库。

| 层次 | 当前持久化内容 | 检索用途与事实边界 |
| --- | --- | --- |
| 原始材料 | `evidence_documents` 保留当前案例的 Evidence 原文及来源定位 | 供审核与引文回查；不把历史 Evidence ID 当作新问题的证据。 |
| 完整复盘 | `retrospective_versions.document` 保存标题、摘要、影响、根因、修复、预防、模块、关键词和引用 | 是知识条目的版本化事实来源；人工修改产生新版本。 |
| 案例条目 | `knowledge_entries` 关联 `incident_id`、`retrospective_version_id`、仓库、问题类型、Wiki URL，并在 metadata 保存关键词、模块、严重度、commit SHA | 只检索 `status=published`；按仓库和问题类型过滤。 |
| 检索片段 | `knowledge_chunks` 现有五类：`case_card`（完整案例卡）、`symptoms`、`root_cause`、`fix_solution`、`prevention`。除主卡外，长字段约 1,200 字符一段、相邻段重叠约 100 字符；每段保存正文、向量、`section`、`segment_index`、`source_evidence_ids`、片段格式版本 `v3` 和 embedding 模型名 | 按复盘字段切片，允许只命中某一侧的根因或修复；Evidence ID 只用于追溯该**历史案例**的原始材料，不可当作当前问题的引用。 |

**召回与使用。** 知识片段完整正文入库；单段向量化输入最多取前 4,000 字符，长字段的后段由独立片段承担召回。新问题由标题、故障描述、故障日志中的关键行，以及所选 diff/源码片段中的路径和符号构造查询，最长 1,800 字符，并生成查询向量。可按仓库、问题类型过滤；[`PostgresRetrospectiveStore.search_knowledge()`](../backend/src/repo_maintainer/retrospective_store.py) 的全文路取查询前 40 个词，以 OR 连接，避免长查询要求所有词同时命中；全文匹配与 pgvector 余弦相似度分别选每个案例的最佳片段，再各取最多 40 个**不同案例**，以 RRF `1/(60+全文名次) + 1/(60+向量名次)` 融合。全文无匹配时仍可由向量路召回。默认取前 20 个案例进入规则精排，最后返回 5 个（`RAG_TOP_K` 可调，最大 20）。返回正文是该案例的 `case_card`，同时带命中片段类型、原始 Evidence ID、版本信息和 Wiki URL。模型接收精排后所返回的案例，每个卡片前 1,500 字符；提示词明确历史案例只能用于类比。完整卡片和完整复盘仍保存在库中。

**精排。** [`_rerank_knowledge_hits()`](../backend/src/repo_maintainer/retrospective_pipeline.py) 将候选的 RRF 分数按本次候选最大值归一化，按 `0.75 × 归一化召回分 + 0.12 × 模块路径匹配 + 0.08 × 关键词匹配 + 0.05 × 标题词重合` 排序。匹配特征和精排前分数记录到返回 metadata，便于检查排序原因。这是无需额外模型服务的确定性精排，不是 cross-encoder 或根因真实性判定；权重仅为初始值，尚未由真实案例集调优。`RAG_CHANNEL_POOL`、`RAG_RRF_K`、`RAG_RERANK_POOL`、`RAG_TOP_K` 分别控制两路候选数、RRF 平滑常数、精排候选案例数和最终案例数；默认值依次为 40、60、20、5。

人工检索入口为 `GET /api/retrospective/knowledge/search?q=...&repo_id=...&incident_type=...&limit=5`；接口见 [`retrospective_api.py`](../backend/src/repo_maintainer/retrospective_api.py)，`limit` 可设为 1–20。

**失败恢复。** Wiki 发布成功但向量生成或知识索引失败时，重试会复用已保存的发布记录，补做知识索引后才标记为已发布；同一版本重建索引会替换旧片段，避免重复案例。对应流程和模拟测试见 [`test_publish_retry_indexes_saved_publication_after_index_failure`](../backend/tests/test_retrospective_pipeline.py) 和 [`test_knowledge_search_returns_distinct_cases_with_structured_card`](../backend/tests/test_retrospective_pipeline.py)。

**当前限制与后续验证。** PostgreSQL 的 `simple` 全文配置对中文词语的切分能力有限，中文召回更依赖向量部分；RRF 和规则精排参数尚未用真实案例调优。当前 SQL 会对过滤后的片段计算排名，知识库增长后需检查执行计划和延迟，再考虑专用全文索引、ANN 向量索引及更强的精排模型。改变 embedding 模型或片段格式后，已有知识条目需要专门重建；本次未自动回填旧版条目。PostgreSQL 查询尚未在真实数据库联调。应以真实问题集测量按案例的 Recall@5/20、MRR 或 nDCG、根因可用率、引用可回查率、审核通过率和检索延迟，并对照只用全文、只用向量及混合检索消融，再决定是否扩大候选池、调整精排权重或引入 cross-encoder。pgvector 官方将全文检索与向量检索，以及 RRF 或 cross-encoder 合并，列为[混合检索方式](https://github.com/pgvector/pgvector#hybrid-search)。

## 5. 方案变更登记规则

任何涉及**证据来源、取证范围、源码/历史召回、去重和压缩、预算、模型输入、引用校验、存储或审核**的变更，都应在同一改动中：

1. 更新上面的来源表、流程、阈值和限制；明确“已实现”“仅计划”或“尚未验证”。
2. 在下表**追加**一行，不覆盖旧记录。记录日期、改动与动机、事实依据（代码/测试/数据）、验证结果及剩余风险。若有真实案例，可附上脱敏的 Evidence ID 和定位信息。
3. 如果改变了 Evidence 字段、来源类型或模型输入，检查 README、环境配置、界面和测试是否需要同步更新。

| 日期 | 方案/实现变更及动机 | 依据与验证 | 剩余限制 |
| --- | --- | --- | --- |
| 2026-09-27 | 建立本台账和仓库根目录 `AGENTS.md` 维护约定；README 加入入口。登记当前复盘链路的证据来源、局部源码扩展、选择/压缩、历史召回与引用校验。 | 对照 `retrospective_models.py`、`retrospective_connectors.py`、`retrospective_code_context.py`、`retrospective_pipeline.py`、`retrospective_store.py`、`retrospective_schema.sql` 和 `backend/tests/test_retrospective_pipeline.py`；本次检查台账、README、AGENTS 的本地链接均可解析。 | 未有真实案例的逐条证据登记；尚无生产链路成功率与完整请求重放数据。本机 pytest 因未安装 `fastapi`，未能收集测试。 |
| 2026-09-27 | 补齐 GitHub 源码扩展的访问条件：README 的 Fine-grained token 权限增加 `Contents: Read`，台账明确 ZIP 归档只读取同仓库固定提交的源码。 | 实现使用 `GitHubArchiveSource.open()` 请求 `/repos/{owner}/{repo}/zipball/{sha}`；[GitHub 官方接口文档](https://docs.github.com/en/rest/repos/contents?apiVersion=2022-11-28#download-a-repository-archive-zip)列出 `Contents: Read`。 | 未用真实私有仓库令牌做端到端权限验证。 |
| 2026-09-27 | 针对多个 diff 文件扩大候选覆盖：基础 10 个文件，多 diff 时最多 40 个；同文件最多 2 个改动区域，并在上下文选择时优先覆盖不同改动文件。 | 修改 `RepositoryCodeContext.collect`、`ContextSelector.select` 与运行配置；新增多文件、多 hunk、预算覆盖测试，本机相关测试 14 项通过。 | 24,000 字符总预算仍可能使大量文件无法全部进入模型；静态图边可能误连，尚未测真实案例的根因召回率。 |
| 2026-09-27 | 为未改动的间接入口或依赖增加有界两跳图扩展，只追踪能唯一定位目标文件的调用/导入边。 | `RepositoryCodeContext.collect` 构建文件邻接图；`test_two_hop_graph_finds_unchanged_entry_point` 验证 `entry → middle → changed`，本机相关测试合计 15 项通过。 | 仍是静态近似；同名目标、动态分派、跨仓库调用及超过两跳的路径可能遗漏，两跳候选也可能受文件和上下文预算淘汰。 |
| 2026-09-27 | 将结构化复盘的历史索引改为四类带来源追溯的片段；按案例去重返回主卡片，并补齐 Wiki 已发布但知识索引失败后的重试。 | 对照 `RetrospectivePipeline.publish`、`_knowledge_chunks`、`PostgresRetrospectiveStore` 和模拟端到端测试；本机相关测试 17 项通过。 | PostgreSQL 查询尚未在真实数据库联调；旧三片段条目需重建，中文混合检索权重和真实案例召回率未验证。 |
| 2026-09-28 | 将历史案例切片细分为五类，对长字段分段；全文和向量两路召回各自先按案例选最佳片段，再以 RRF 融合，之后加入模块、关键词、标题的可解释精排，避免长案例占满候选池，也避免主卡前 4,000 字之外的内容失去独立召回机会。长查询的全文词项改用 OR 连接，并让模型接收配置的最终案例数。 | 修改 `_knowledge_chunks`、`_split_knowledge_section`、两种存储实现的 `search_knowledge`、`_rerank_knowledge_hits`、模型输入和环境配置；新增长字段、双路召回、候选池公平性与精排测试，本机相关测试 21 项通过。[PostgreSQL 文档](https://www.postgresql.org/docs/16/textsearch-controls.html)说明 `plainto_tsquery` 默认以 AND 连接词项；混合检索组合方式参见 [pgvector 官方文档](https://github.com/pgvector/pgvector#hybrid-search)。 | 参数是初始值，缺少真实复盘案例的排序评估；PostgreSQL SQL 未联调，旧版片段需重建；中文全文切词及知识库增长后的延迟仍待验证。 |
| 2026-10-01 | 新增全项目架构总览与持续升级约定；台账加入架构入口，并将生成输入表中的历史案例数量与 2026-09-28 实现对齐 | 对照 API、Pipeline、运行配置、生成器、SQL、前端路由和 Compose；核对文档本地链接；业务实现未变化 | 本次为静态文档核对，未重跑数据库/真实平台联调；Compose 尚未转发的四项检索/源码配置已登记在架构文档 |

