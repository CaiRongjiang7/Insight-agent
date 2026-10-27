# 智能复盘证据来源与上下文管理台账

> 当前实现基线：2026-10-27（标准只读 Tool、源码会话与轨迹升级；其余行为沿用既有记录）；最近文档核对：2026-10-27（局部代码/隔离数据库验证，原有研究核对保留；真实平台演练沿用 2026-10-21）。本文只描述 `RetrospectivePipeline` 智能复盘主链路；整个项目的组件关系与部署见[项目架构文档](architecture.md)。旧代码修复 Agent 的记忆和 Token 预算不是本链路的运行机制。**每次修改复盘方案或实现时，同步修订相关章节，并在文末追加变更记录。**

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
| `CTX-01` | PR、CI、飞书及手工材料按来源生成 Evidence | [`GitHubConnector.collect`、`FeishuConnector.collect`](../backend/src/repo_maintainer/retrospective_connectors.py) | 单元/模拟验证；2026-10-21 另验证真实私有 PR/diff/源码及六条重建消息。该次未自动采集旧失败 CI 归档，具体权限与缺口见[执行记录](real-walkthrough-2026-10-21.md)。 |
| `CTX-02` | 未出现在 diff 的相关源码可通过静态关系进入候选；多 diff 文件可扩展候选上限，同文件多个远离的改动区域可分别取证；唯一目标文件的调用/导入边可提供两跳候选 | [`RepositoryCodeContext.collect`](../backend/src/repo_maintainer/retrospective_code_context.py)、[`test_multiple_diff_files_expand_beyond_base_limit_and_keep_caller`](../backend/tests/test_retrospective_pipeline.py)、[`test_separate_diff_hunks_in_one_file_produce_separate_code_evidence`](../backend/tests/test_retrospective_pipeline.py)、[`test_two_hop_graph_finds_unchanged_entry_point`](../backend/tests/test_retrospective_pipeline.py) | 验证特定 Python 调用方、多文件/多 hunk 与两跳入口情形；不覆盖动态调用或所有语言。 |
| `CTX-03` | 原文入库，筛选出的片段受字符预算约束；不同改动文件优先各得一个源码片段 | [`ContextSelector`](../backend/src/repo_maintainer/retrospective_pipeline.py)、[`add_evidence`](../backend/src/repo_maintainer/retrospective_store.py)、[`test_context_selection_covers_distinct_changed_code_files`](../backend/tests/test_retrospective_pipeline.py) | 验证选择器与存储语义；预算不足时仍可能遗漏文件；新请求已加快照，旧请求与自动重放仍有缺口。 |
| `CTX-04` | 核心字段引文按 Evidence ID 和原文字符串校验 | [`QualityValidator`](../backend/src/repo_maintainer/retrospective_pipeline.py)、[`test_quality_validator_rejects_unknown_or_fabricated_citations`](../backend/tests/test_retrospective_pipeline.py) | 验证存在性与字符串匹配；不验证因果支持度。 |
| `CTX-05` | 历史案例经发布后入库并用于后续召回 | [`RetrospectivePipeline`](../backend/src/repo_maintainer/retrospective_pipeline.py)、[`test_complete_review_publish_and_retrieval_loop`](../backend/tests/test_retrospective_pipeline.py) | 模拟闭环及 2026-10-21 单条重建案例的真实 Wiki、pgvector 入库、召回和仓库排除验证；包含人工运维恢复，不代表全自动成功率或线上召回质量。 |

### 单个问题的来源记录模板

运行时明细以 `evidence_documents` 和复盘页面为准。需要人工审查或导出某个案例时，可在该案例的材料中使用下表；不要在本台账写入真实 Token、密钥或无关个人信息。

| Incident ID | Evidence ID / 历史 Knowledge ID | 所支持的结论 | 来源 URL 或消息 ID | SHA、文件与行号 / 时间 | 原文核对结果 | 限制或争议 |
| --- | --- | --- | --- | --- | --- | --- |
| 待填 | 待填 | 待填 | 待填 | 待填 | 待填 | 待填 |

## 2. 上下文如何构建与压缩

当前流程：**GitHub/飞书原始采集 → 局部代码扩展 → 全部 Evidence 原文入库 → 去重和相关性筛选 → 裁剪进入模型的副本 → 历史案例召回 → 可选标准 Tool 补证及最终重选 → 结构化生成 → 双层引用校验**。代码扩展发生在入库前，扩展结果本身也作为原始 Evidence 入库。入口见 [`RetrospectivePipeline.run()`](../backend/src/repo_maintainer/retrospective_pipeline.py)。

| 步骤 | 已实现的方法 | 被保留的内容与限制 |
| --- | --- | --- |
| 固定代码版本 | 保存 webhook 的 head/base SHA；源码扩展使用 head SHA，缺失时使用记录的 commit SHA | 代码 Evidence 带实际使用的 SHA 和行号。当前主要分析**修复后的版本**；修复前状态主要来自 diff，尚未自动建立前后两版源码对照。归档不可用时保留其他证据，并在问题元数据记录 `code_context_warning`。 |
| 局部源码扩展 | 每个 diff 文件的新增行映射 Python AST 函数/类；结合错误栈行号、调用方/被调用方、导入关系及测试引用选候选；唯一目标文件的调用/导入边可扩至两跳；同文件最多取 2 个不同区域，候选文件基础 10 个，按改动文件数最多扩至 40 个，并给未改动文件留名额 | 先临时索引同仓库快照，再只发送候选源码片段及来源定位，不把全仓代码放入模型。已有索引包含调用、依赖和部分数据流/控制流图信号；两跳关系要求目标文件唯一。跨语言和动态调用可能漏召回或误召回，图关系只作为检索线索。 |
| 去重与排序 | 内容规范化后按指纹去重；源码额外包含位置，避免不同文件的同文片段合并；按来源优先级、关键词重合和源码检索分排序 | `selection_reason` 记录选择原因；保留不同来源的首条候选，源码优先留一条非 diff 文件。排序不是事实可信度评分。 |
| 日志裁剪 | 优先取包含错误、异常、失败等词或问题关键词的行及前后各 2 行；无匹配时取前 120 行 | 原日志仍在库；模型只看裁剪后的副本。若后续预算再次截断，日志取首尾片段。 |
| Diff 裁剪 | 选中与问题关键词重合的 hunk；无匹配时取前 3 个 hunk | 原始 patch 仍在库；模型可能只看到部分 hunk。后续预算截断时保留首尾。 |
| 证据预算 | 默认选中证据总内容不超过 24,000 字符；单条配置上限 6,000 字符；CI、diff、源码、飞书消息/文档、PR、commit 来源优先各留一个名额。之后以总预算约一半作为改动源码的优先覆盖额度，尽量让不同改动文件各有一个片段 | 这是**字符预算**，不是精确 Token 预算；单条首尾拼接会增加裁剪标记，总量仍由选择器限制。文件过多或其他证据占用较高时仍可能遗漏。手工来源参与后续统一排序。配置见 `CONTEXT_BUDGET_CHARS`、`CONTEXT_ITEM_MAX_CHARS`。 |
| 历史检索 | 用标题、前 500 字描述、问题类型、最多 3 条故障行及相关代码路径/符号组成查询，最多约 1,800 字符；按仓库、问题类型过滤，默认 Top 5 | 已发布知识参与检索。历史案例用于启发，不替代当前证据。 |
| 标准工具补证 | REVIEW_TOOLS_ENABLED 默认 0；get_related_code / search_feishu_context / search_historical_cases，最多两轮决策/四次实际执行；共享 8,000 正文字符，单次 15 秒/700 输出 Token；同参数缓存只回送已返回 ID | 服务端固定事故/SHA/授权素材范围；代码与静态采集共用一次归档/索引；飞书只搜索当前已采集材料，历史仍非当前 Evidence；软期限不强制中断同步上游调用。 |
| 生成输入 | 传入筛选后的 Evidence ID、来源、位置、正文、SHA 和选择原因；问题描述最多 2,000 字符；历史案例默认 5 条，`RAG_TOP_K` 最大 20，每条正文最多 1,500 字符 | 不把完整问题日志再次作为 `incident` 字段传入；日志经 Evidence 选择进入模型。模型实际输入还包含 JSON 字段和工具 Schema，**未实现整体精确 Token 预估或硬限额**。 |
| 驳回后重新生成 | 从库中读取原始内容，对先前选中的证据再次执行选择器 | 保持本次生成的字符预算；若选择器代码或配置变化，重新生成时看到的片段可能改变。 |

选择、裁剪与检索查询见 [`retrospective_pipeline.py`](../backend/src/repo_maintainer/retrospective_pipeline.py)；模型输入见 [`OpenAIReviewGenerator.generate()`](../backend/src/repo_maintainer/retrospective_connectors.py)。轻量补证已接入：Python 方法索引优先解析本地方法、导入别名和直接父类，按调用和后台任务引用补充两跳（至多 24 个种子/邻居），方法起点进入片段候选；同名歧义不在新符号索引中强连，原启发式候选仍保留。`.insight/context.json` 仅提供同仓库服务路径、显式依赖与文档导航，最多 20 服务、4 个文档候选；依赖的影响状态固定为 unverified。`CODE_CONTEXT_PLANNER_ENABLED` 默认 0；显式启用且有未解析方法线索时，最多一次小型规划（最多 8 段各 1,000 字符，15 秒/700 输出 Token，禁用传输重试），返回最多 6 个唯一已有符号/4 个问题，继续受文件/区域预算约束。规划失败保留静态原文。尚未抓取其他仓库或生产运行拓扑。

源码 `region_complete` 表示选区是否完整对应 AST 区域，选择器再次裁剪会将其置 false，原文不变；不保证该片段已覆盖完整恢复链。发现摘要记录方法线索、文件/区域数、规划状态和未解析调用，属于静态候选元数据。

代码归档大小、基础和扩展文件数可通过 `CODE_CONTEXT_MAX_ARCHIVE_BYTES`、`CODE_CONTEXT_MAX_FILES`、`CODE_CONTEXT_MAX_EXPANDED_FILES` 调整，`CODE_CONTEXT_ENABLED=0` 可关闭扩展。

## 3. 客观事实如何保留与校验

**预算方案的研究依据。** [长上下文研究说明](long-context-budget-evidence.md)记录的 2026 年研究支持：当前模型在过长上下文或大量干扰材料下仍可能遗漏关键证据，因此应筛选、去重并有界组织飞书会话与代码，而非无限制地全部传入。此工程动机不依赖证明所有模型都会退化或确定统一 Token 阈值；具体参数再按覆盖、质量和成本评估。保留模型差异及稳定结果，不将行为变化等同于已测得内部注意力机制，也不因压缩而丢掉关键行为链。论文不证明当前 24,000/6,000 字符预算或两跳上限最优，亦不替代本项目模型的消融；当前参数、本次来源范围、审核门槛与部署保持不变。

1. **原文与模型片段分开。** 流水线先写原始 `Evidence.content`；选择器在深拷贝上裁剪。存储层重复写同一 Evidence ID 时只更新 `selected` 和 `selection_reason`，不会用裁剪文本覆盖原文。数据库表见 [`retrospective_schema.sql`](../backend/src/repo_maintainer/retrospective_schema.sql) 和 [`retrospective_store.py`](../backend/src/repo_maintainer/retrospective_store.py)。这描述的是应用写入行为，不等于数据库具有防篡改保证。
2. **来源可回查。** 当前证据保留 Evidence ID 与可得的 URL、消息 ID、SHA、文件/行号或时间；生成版本保存使用过的 Evidence ID、模型、Token 用量与耗时。某些来源可能没有可直接打开的 URL，应以其他定位字段核对。
3. **推断与观察分离。** AST/调用/导入关系的 `relation` 标记为 `static_candidate`，只负责召回。PR 描述、聊天、手工输入和历史案例都可能含未经核实的说法。源码原文证明“该版本写了什么”，日志证明“记录了什么”；二者都不能单独证明完整的根因、影响范围或运行时调用路径。
4. **引用硬校验。** 影响、根因、修复方案各需引用 Evidence ID。AI 生成和驳回后重新生成时，引文须同时出现在**本次模型看到的片段**与**入库原文**中；人工编辑时校验入库原文。缺 ID、引文不匹配或核心字段无引用时不能进入下一步；模型提示在证据不足时写未知。见 [`QualityValidator`](../backend/src/repo_maintainer/retrospective_pipeline.py) 和 [`OpenAIReviewGenerator`](../backend/src/repo_maintainer/retrospective_connectors.py)。
5. **人工审核。** AI 结果进入待审核状态；人工可编辑、驳回、通过，审核通过并发布后才把复盘分块加入历史知识库。引用字符串匹配只校验可定位性，**不能自动校验结论是否由引文充分支持**，因果判断仍需审核人核对。

生成与驳回后重新生成的第二轮请求会附带前轮 `QualityValidator.hard_errors`，作为 `previous_validation_errors` 输入；通过临时问题副本传递，不写为 Evidence 或事故 metadata。生成器提示使用短单行原文并保留 diff 的 +/- 标记，原有双层匹配规则保持。每次逻辑请求现在在调用前持久化到 generation_contexts（含反馈、裁剪后的实际输入、工具 Schema、模型与选项和内容哈希），失败响应与被拦截草稿仍未保存，不能宣称自动重放全部运行结果。

生成模式可通过 `OPENAI_ENABLE_THINKING` 选择：默认空值不发送提供商扩展，0/1 仅向兼容服务传 `enable_thinking`。本机百炼演练在一次生成连接断开后设为 0；该设置不改变证据、预算或校验门槛，不构成生成质量提高的证明。

### 当前未解决的可追溯性问题

- 新请求已在调用前保存独立快照和哈希：内置生成器 scope 为 provider_request，自定义生成器为 generator_arguments；可选规划单独登记。仅数据库内查询，无新增 HTTP/API 返回。旧版本无快照，不能事后复原；快照不等于完整 HTTP 传输、服务端内部提示词或自动因果验证。
- 当前总预算按字符而非模型 Token 计量；历史案例、问题字段、JSON 包装及工具 Schema 没有统一的请求级硬限额。
- 代码扩展是静态启发式，Python AST 与 JS/TS 解析能力不同；同名函数、动态分派、反射及跨服务调用可能产生误连或遗漏。归档超限或下载失败时会跳过代码扩展。
- GitHub 的大文件或二进制变更可能没有 `patch`；CI 日志取证目前只针对关联 SHA 的一个失败 run，可能缺少最初复现时的运行记录。
- `QualityValidator` 检查引文存在与字段覆盖，不检查“引文是否真的支持该因果结论”；尚无基于真实历史问题的成功率或审核通过率测量。
- [`map_reduce_summary.py`](../backend/src/repo_maintainer/map_reduce_summary.py) 是独立的长文本摘要模块，**未接入本复盘主链路**。不得将其写为当前已运行的压缩步骤。

### 标准 Tool 的证据与输入边界（已实现）

[标准工具执行器](../backend/src/repo_maintainer/retrospective_tools.py)将名称、描述、Pydantic 参数和 handler 注册为 JSON Schema Function Tools，拒绝额外字段、未知工具和越界结果。代码先存原文再构造片段；历史只带 Knowledge ID。缓存按工具名和规范化参数复用，无重复正文；飞书与历史长正文按查询命中位置取连续原文窗口，并保留偏移和 truncated。

模型输出项目原样序列化回放，包括提供商返回的 reasoning 加密内容等字段；按 call_id 配对全部工具请求结果，超额请求也记录 budget_exhausted。最后一轮结果以证据包进入独立最终生成，不重复塞入全部探索轨迹。已用 SDK 对象与 Mock 验证回放，[当前官方 reasoning 协议](https://developers.openai.com/api/docs/guides/reasoning)在 store=false 下默认提供加密内容；其他兼容服务的真实多轮行为尚未验证。

工具实际返回过的证据在最终筛选中加 15 候选分，采用返回的窗口正文，截短区域 region_complete=false；原文仍保留。工具模式不提前登记初始 selected，避免把初始与最终材料混记；最终遗漏的请求 ID 单独记账。历史工具返回的命中窗口优先于初始整卡，不把不同查询排名当作同一可信度。

generation_contexts 复用现有表，保存 tool_exploration、tool_model_response、tool_execution、tool_exploration_summary、tool_context_selection 以及最终生成请求，按 run_id/round/call_id 关联。工具结果是当前素材，不作为新的独立事实来源；模型最终仅可引用本次写作实际看到并可回查的原文。提示词版本更新为 v3-standard-tools；模型失败或达到轮次上限附软警告，最终引文校验与人工审核不变。

现有 24,000/6,000 字符是最终证据正文预算，8,000 是工具新返回正文预算，不含 Schema、元数据、历史卡和协议项；未实现整体 Token 硬限额。模型决策输入另限 8,000 证据字符、12 条、两张短历史卡。历史检索仍使用现有同步连接器超时。驳回重生复用持久化证据，不重新探索；旧任务跨重试 selected 累积与过去无快照记录不能追溯补全。本次未新增 HTTP 快照接口、外部权限或模型部署。

### 已实现边界与可评估的下一步增强

2026-10-23 的[实施记录](incident-context-discovery-design.md)区分已实现的轻量子集与后续完整方案；当前没有多 Agent、跨仓库获取或部署版本解析；标准 Tool 的两轮有界补证已经单独实现，人工审核和引用门槛保持。生成快照仅存数据库，不新增网络读取入口。

| 候选方法 | 客观事实保留方式 | 上线前验证 |
| --- | --- | --- |
| 证据感知压缩 | 从原文提取时间、错误、组件、版本、前后行为等事实项；每项保留 Evidence ID、原文短引、SHA/行号或消息时间，摘要只做导航，不作为独立证据。冲突陈述并列保留。 | 比较压缩前后关键事实和引文的保留率，并检查是否引入原文没有的事实。 |
| 快照的进一步完善 | 已存逻辑请求与哈希；待补失败响应、选择器/预算版本、真实 Token 计量与自动重放工具。 | 比较输入与结果的可回放性；旧输入不可回填。 |
| 真实 Token 预算与分层召回 | 按实际模型计量输入，给故障日志、变更代码、未修改关联代码和协作记录分配可调整预算；覆盖不足时按错误栈和符号逐轮扩展。 | 在真实案例集上测量根因相关源码召回率、无关片段比例、超限率和成本。 |
| 结论支持度检查 | 对每条根因及影响结论建立“结论 → 直接观察 → 静态推断/人的陈述”的支持关系；无法证明的部分明确写“待验证”。 | 人工盲审无依据结论比例、误判率和审核通过率，比较改动前后。 |

只有在实现、测试和真实案例验证后，才能把这些方法移到“已实现”部分，并在下方追加变更记录。

## 4. 结构化复盘如何进入 RAG 知识库

**向量请求批量边界。** 查询、选中 Evidence 和发布知识均通过 `OpenAIEmbedder` 发出请求，每批最多 20 条，顺序执行并按批内返回 index 恢复输入顺序。缺失、重复或越界 index 会报错，避免文本与向量静默错配；空输入不发请求。该上限来自 2026-10-21 百炼真实联调的 400 响应；它不改变原文、上下文预算或存储维度，也不代表所有服务商的单段长度限制均已适配。

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

真实演练还观察到发布记录保存之前的失败：节点已创建、正文因 docx 权限被拒绝，本地没有 publication。该窗口不受上述恢复分支保护，直接重试可能重复建页；本次需人工续写并回读既有节点后登记 publication，不能宣称自动恢复已实现。另一次生成由诊断脚本复用 30 段已索引证据，调用真实模型并重新通过原双层校验后恢复待审核，UI 再编辑审核；其运维介入、模型用量及重复取证范围见[执行记录](real-walkthrough-2026-10-21.md)。

**当前限制与后续验证。** PostgreSQL 的 `simple` 全文配置对中文词语的切分能力有限，中文召回更依赖向量部分；RRF 和规则精排参数尚未用真实案例调优。当前 SQL 会对过滤后的片段计算排名，知识库增长后需检查执行计划和延迟，再考虑专用全文索引、ANN 向量索引及更强的精排模型。改变 embedding 模型或片段格式后，已有知识条目需要专门重建；本次未自动回填旧版条目。2026-10-21 已在真实 PostgreSQL/pgvector 上完成单条重建案例的发布入库、混合召回和仓库排除验证，尚非真实事故集的质量或性能评估。应以真实问题集测量按案例的 Recall@5/20、MRR 或 nDCG、根因可用率、引用可回查率、审核通过率和检索延迟，并对照只用全文、只用向量及混合检索消融，再决定是否扩大候选池、调整精排权重或引入 cross-encoder。pgvector 官方将全文检索与向量检索，以及 RRF 或 cross-encoder 合并，列为[混合检索方式](https://github.com/pgvector/pgvector#hybrid-search)。

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
| 2026-10-21 | 向量适配器每批最多 20 条，校验返回 index，修复真实百炼拒绝 31 条输入的问题 | `test_embedding_batches.py` 的 3 项回归及原复盘 21 项测试通过；真实失败保留在本机隔离演练任务中，随后重试 | 不改变 Evidence 来源和引用规则；尚无所有兼容模型的长度/限流验证；无需数据迁移 |
| 2026-10-21 | 保留引文硬校验，给生成与驳回重生的第二轮传递具体校验错误，提示逐行保留 diff 标记 | 真实任务因 `Quote not found` 失败；新增测试覆盖两个入口的反馈、有效结果保存及不污染事故 metadata，复盘 22 项和向量 3 项测试通过 | 中间失败草稿和完整模型请求仍未保存；反馈只改善可定位性，不验证因果真实性；[演练记录](real-walkthrough-2026-10-21.md) |
| 2026-10-21 | 增加可选 `OPENAI_ENABLE_THINKING`，便于为兼容提供商明确生成模式；演练设为 0 后重试 | 百炼小型非思考 Function Calling 请求 1.34 秒返回；27 项相关测试覆盖原流程、分批、配置与校验反馈 | 单个探针不等于完整复盘成功；没有生成质量/延迟消融；默认空值保持提供商原行为 |
| 2026-10-21 | 登记重建材料的来源边界、真实模型人工恢复和 Wiki 部分失败 | 30 段证据的 1024 维向量实存；真实模型 13,016 输入/1,185 输出 Token 的输出通过双层引文校验；UI 保存人工 v2 并审核；Wiki 节点已建、正文权限 99991672 | 当前历史知识为 0，正文发布及召回待完成；不是全自动 UI 闭环；引用匹配满分仍有过度影响推断，须人工审核 |
| 2026-10-21 | 用户发布文档权限后完成既有节点续写、知识入库与单案例检索，更新真实验证范围 | Wiki 正文 3,334 字符回读核对，浏览器实见首页下文档；最终 published v2，1 案例/5 知识片段均 1024 维；同仓库查询命中、错误仓库过滤 0；累计 59 证据向量，最终生成用 30 段，详见[执行记录](real-walkthrough-2026-10-21.md) | 生成与部分发布包含人工恢复；旧重复来源/向量未清理；单条重建案例不等于真实问题集效果；普通文本排版与原事故未证实内容仍有边界 |
| 2026-10-23 | 新增[事故相关代码发现设计](incident-context-discovery-design.md)，将一次性源码扩展的后续方向细化为按问题补证，明确跨服务定位、证据分类与预算 | 对照当前源码采集、索引、选择器、模型与连接器；依据 Codex/Claude Code/Pi 官方文档和 Pi 公开源码；本次仅文档静态核对及本地链接检查，未运行新测试或线上验证 | 所有新工具、服务/部署映射、探索循环、ContextPack 与参数档位均未实现；无表、配置或部署迁移；原有字符预算和输入快照缺口保留 |
| 2026-10-23 | 实施轻量 Python 方法发现、显式同仓库服务文档、默认关闭的一次规划和生成输入快照；转发四项既有配置 | 针对性回归与隔离 PostgreSQL 幂等建表/快照读写验证；快照在调用前写入，包含失败请求与反馈，原始 Evidence 不覆盖；详见[实施设计](incident-context-discovery-design.md) | 无新增 HTTP 快照入口，现有部署需先 migrate；字符预算不等于 Token 硬限额，跨仓库、双版本、动态分派与生产运行证据仍未接入；未改线上或原演练环境 |
| 2026-10-27 | 补充代码探索预算的论文依据，区分行为退化、位置注意力偏差和项目工程推论 | 核对[原始论文与技术报告](long-context-budget-evidence.md)的图、表、版本和控制条件；本地链接检查；未改预算或代码、未运行新模型实验 | 文献不证明当前项目模型已发生同样退化，也不证明字符/文件/跳数阈值最优；业务质量和预算消融仍待验证 |
| 2026-10-27 | 补核 2026 年模型与思考模式差异，纳入稳定/压缩无显著收益的反证，旧研究降为背景 | 核对研究说明所列 2026 年原文、版本、受测模型和实验条件；同步架构与本地链接检查；仅文档修订 | 不把行为变化当作内部注意力机制证明；无本项目消融，不改预算、模型、审核或部署；过度压缩仍可能遗漏证据 |
| 2026-10-27 | 将论证目标明确为“长上下文仍可能受干扰，因此应控制输入”，避免把通用阈值当作前置条件 | 复核 Classifier 与既有近期研究，更新研究说明、架构及本地链接；仅文档修订 | 控制的必要性不证明某个固定阈值最优；保留原始证据与关键行为链，未改代码、参数或部署 |
| 2026-10-27 | 沉淀并在新分支实现三个标准只读 Tools、同快照会话、受控调用循环与证据轨迹 | 77 项针对性测试含隔离 PostgreSQL 通过；Mock/SDK 对象验证两轮回送、输入验证、预算/缓存、空/错/歧义、截断窗口、最终 selected、历史隔离及失败/重生兼容；[方案](standard-tools-upgrade.md) | 默认关闭，无新 HTTP/表/依赖；未真实验证百炼多轮工具协议、生产权限或业务效果；保留字符预算、同步上游超时、旧重试标记与跨服务取证限制 |
