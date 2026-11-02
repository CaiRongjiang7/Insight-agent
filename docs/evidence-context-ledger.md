# 智能复盘证据来源与上下文管理台账

> 当前实现基线：2026-11-01，codex/deepseek-harness-upgrade，3af8ea8 + 工作区；实际 926f61c12dca，deepseek + 百炼 deepseek-v4.1-flash-1017（调查/摘要/Writer）、1M、thinking=false。275 项已安装包回归无 skip；真实部署容器 12 探索/1 压缩/11 工具、33 份原文不变，实际 Celery/Writer 长材料合成稿 v1 待审核、知识/发布 0；[最新实测](validation/long-context-live-2026-11-01.md)。既往记录保留，合成测试不是生产事实或人工批准。全项目见[架构](architecture.md)。

## 1. 证据从哪里来

一次复盘先收集原始材料，写入 `evidence_documents`，再从中选择进入模型的片段。来源类型由 [`EvidenceSource`](../backend/src/repo_maintainer/retrospective_models.py) 定义；[业务场景回顾](incident-business-scenarios.md)登记无 PR 配置、容量、依赖、数据和模型服务事故所需材料；只有已接入或人工提供的原文才进入当前证据。单条 `Evidence` 包含 `id`、`incident_id`、`source_type`、`content`、`location`、`source_url`、`repo_id`、`commit_sha`、`occurred_at`、`metadata`、`selected` 和 `selection_reason`。有字段不等于该来源一定提供了有效值，取决于连接器和外部系统。

| 来源类型 | 当前采集方式 | 可追溯定位 | 事实属性与边界 |
| --- | --- | --- | --- |
| `github_pr` | PR 标题/描述及 issue 评论；入口标题/描述仅在 `source_type=github_pr` 时归入此类 | PR URL、评论 URL、仓库/PR 编号 | **参与者陈述**，可以说明意图或排查过程，不能单独证明故障机理。 |
| `github_commit` | PR commits 的提交消息 | Commit SHA、commit URL | **提交者陈述**，不是代码执行结果。 |
| `github_diff` | PR files 接口返回的文本 `patch` | 文件路径、PR/文件 URL、问题关联的 SHA | **代码变更原文**；接口未返回 `patch` 时不会生成该条证据，diff 不覆盖未改动代码。 |
| `repository_code` | 固定 PR head SHA（缺失用问题记录的 commit）经 GitHub ZIP 获取同仓源码，用 diff/错误栈/符号/调用/导入/测试引用定位；不读取 Agent 本地工作区 | SHA、文件、起止行、固定 blob URL | **版本源码原文**，relation/retrieval_score 仅为静态候选。运行装配基础10文件，最多扩至120，每文件4区域/每段12,000字符；类直接构造仍旧40/2/2,400。私库需 Contents: Read。 |
| `ci_log` | GitHub Actions 中与记录的 SHA 对应的失败 run 日志；手工问题也可提供日志 | Run URL 或问题 ID、SHA | **运行记录**。若日志归档不可取，连接器可能只保存 run URL；手工日志的真实性仍需核对。 |
| `feishu_message` | 指定群窗口内的消息；飞书命令入口还直接保存 metadata.feishu_trigger_text 原始触发正文，不拿解析后的标题替代；采集排除同触发消息和自身机器人回复 | `chat_id`、`message_id`、`occurred_at`、`feishu://message/{id}`；事故 metadata 另存事件、发送者和窗口 | **沟通原文与参与者陈述**，不等于配置审计或确认根因；机器人生成回复不应成为自己的事故依据。 |
| `feishu_doc` | 配置的文档 token 对应的原文 | 文档 token、文档 URL | **文档陈述**，可能比事故发生时间晚，需核对时序。 |
| `manual` | 手工及其他非 GitHub PR、非飞书消息入口的标题和描述 | 问题 ID、可选 `source_url` | **提交人陈述**；不应默认为已验证事实，也不再因来源字符串不是 manual 而误记为 PR。 |
| 历史案例 `KnowledgeHit` | 审核、发布并入库的复盘，通过 PostgreSQL 全文检索与 pgvector 召回 | `knowledge_id`、历史问题 ID、发布 URL | **类比参考**。目前作为 `historical_cases` 输入，**不是当前问题的 `Evidence`，没有可用于当前引用校验的 Evidence ID**。枚举中的 `historical` 不代表主链路会将召回案例转成 Evidence。 |

采集实现见 [`retrospective_connectors.py`](../backend/src/repo_maintainer/retrospective_connectors.py) 和 [`retrospective_code_context.py`](../backend/src/repo_maintainer/retrospective_code_context.py)；历史案例的入库与检索见 [`retrospective_pipeline.py`](../backend/src/repo_maintainer/retrospective_pipeline.py) 和 [`retrospective_store.py`](../backend/src/repo_maintainer/retrospective_store.py)。

### 飞书命令的来源与授权范围（已部署并验证帮助收发，复盘闭环待验收）

独立 [Listener](../backend/src/repo_maintainer/retrospective_feishu_listener.py) 通过 `lark-oapi==1.7.5` 接收长连接事件；[解析器](../backend/src/repo_maintainer/retrospective_feishu_commands.py)要求事件应用匹配、群在 `FEISHU_CHAT_IDS` 白名单、发送者为用户、消息为群文本且实际 @了自身机器人，再识别“发起复盘”“复盘”或“复盘帮助”。这是明确命令触发，不由模型扫描普通讨论自动判断是否发生事故。传输认证由官方连接承担，Listener 与 Worker 都校验范围。

[命令处理](../backend/src/repo_maintainer/retrospective_feishu_bot.py)只设置当前群，文档列表为空，不继承其他默认群/文档；不接受任意群、文档、仓库或 SHA 参数。时间按北京时间，以消息发送时刻为锚点，省略时取之前 24 小时；显式范围须先后有效、至多 72 小时且结束不晚于触发消息。跨日须写出结束日期。该窗口保存为整数 Unix 秒的 `metadata.feishu_window`，连接器再次检查类型、顺序和跨度；旧 PR/手工入口未提供此字段时取发生/合并时间为中心前 3 天、后 1 天，缺少有效时间时使用问题创建时间，跨日审核重取证不漂移到审核当天。

原始触发文本和群聊原文存为 `feishu_message`；服务/环境来自参与者填写，保存在 metadata 和描述，只作线索，不作为消息或历史案例的严格过滤条件。当前无仓库的飞书事故使用 `repo_id=feishu:{app_id}:{chat_id}`，历史召回和发布知识保持同一群范围，不因空 repo_id 读取所有仓库。不能从这个分区推断知识已具备跨服务拓扑或服务/环境级权限隔离。

事件经 Redis/Celery 到达 Worker；稳定事故 ID、数据库唯一键和非阻塞执行锁保护同一启动消息，成功回复阶段存入事故 metadata。原始材料、选中材料及模型调用快照仍按既有流程持久化，不增加表或 RAG 切片类型。API 的 run/retry/regenerate/edit/approve/reject/publish、GitHub 入队路径（含 inline）和两类 Worker 共用事故锁；API 竞争返回 409，常规 Worker 只运行 received/failed，其他状态跳过。飞书执行中断会转 failed 要求人工检查，避免重复投递自动生成第二份证据/版本。直接调用 Pipeline 不自动加锁，也没有跨系统 exactly-once 保证。

飞书任务对通知失败、锁冲突和 psycopg `OperationalError/InterfaceError` 最多重试 5 次、退避上限 60 秒，覆盖首次建单前的暂时数据库连接故障；已有复盘的通知重试不重新生成。有限重试不等于事务发件箱或永久补偿，真实故障重投、事故生成和模型链路仍须业务验收。

群消息、查询、所选材料及生成输入会经既有适配器发送到配置的模型/embedding 服务；本机既有配置是百炼，启用前须核对群材料与该服务的使用范围。没有“只在飞书内计算”的保证，也未新建事故自动处置权限。草稿必须在工作台人工核对、审核并发布后，才进入历史知识；群中触发不替代审核。配置、权限和真实验收步骤见[飞书机器人部署说明](feishu-bot-setup.md)。

2026-10-30 已重建镜像并在本机部署 API、Worker、前端、Redis、PostgreSQL 和 Listener，API healthy/ready；飞书后台确认长连接成功，`im.message.receive_v1` 已添加且所需权限均为既有已开通权限，无新增权限，应用版本 1.0.3 显示已发布/通过。用户真实 @复盘帮助已收到回复，Worker 记录任务成功、`status=help`、耗时 0.623 秒，事故数仍为 0。这验证了真实收发链路，没有验证事故建单、群聊取证或本次模型生成。当前 Docker 引擎启动时无旧镜像/容器/卷，当前数据库新建为空；历史演练结论不能当作新库现有条目或本次生成结果。

### 当前方案的依据索引

这里登记的是**方案判断所依据的代码与验证**，不同于运行时某个问题的 Evidence ID。后续变更应更新受影响的行，并在文末追加变更记录。测试只证明覆盖的行为，不代表真实事故的复盘成功率。

| 依据 ID | 当前可核对的判断 | 一手依据 | 验证范围 |
| --- | --- | --- | --- |
| `CTX-01` | PR、CI、飞书及手工材料按来源生成 Evidence | [`GitHubConnector.collect`、`FeishuConnector.collect`](../backend/src/repo_maintainer/retrospective_connectors.py) | 单元/模拟验证；2026-10-21 另验证真实私有 PR/diff/源码及六条重建消息。该次未自动采集旧失败 CI 归档，具体权限与缺口见[执行记录](real-walkthrough-2026-10-21.md)。 |
| `CTX-02` | 未出现在 diff 的相关源码可通过静态关系进入候选；多 diff 文件可扩展候选上限，同文件多个远离的改动区域可分别取证；唯一目标文件的调用/导入边可提供两跳候选 | [`RepositoryCodeContext.collect`](../backend/src/repo_maintainer/retrospective_code_context.py)、[`test_multiple_diff_files_expand_beyond_base_limit_and_keep_caller`](../backend/tests/test_retrospective_pipeline.py)、[`test_separate_diff_hunks_in_one_file_produce_separate_code_evidence`](../backend/tests/test_retrospective_pipeline.py)、[`test_two_hop_graph_finds_unchanged_entry_point`](../backend/tests/test_retrospective_pipeline.py) | 验证特定 Python 调用方、多文件/多 hunk 与两跳入口情形；不覆盖动态调用或所有语言。 |
| `CTX-03` | 原文入库，筛选出的片段受字符预算约束；不同改动文件优先各得一个源码片段 | [`ContextSelector`](../backend/src/repo_maintainer/retrospective_pipeline.py)、[`add_evidence`](../backend/src/repo_maintainer/retrospective_store.py)、[`test_context_selection_covers_distinct_changed_code_files`](../backend/tests/test_retrospective_pipeline.py) | 验证选择器与存储语义；预算不足时仍可能遗漏文件；新请求已加快照，旧请求与自动重放仍有缺口。 |
| `CTX-04` | 核心字段引文按 Evidence ID 和原文字符串校验 | [`QualityValidator`](../backend/src/repo_maintainer/retrospective_pipeline.py)、[`test_quality_validator_rejects_unknown_or_fabricated_citations`](../backend/tests/test_retrospective_pipeline.py) | 验证存在性与字符串匹配；不验证因果支持度。 |
| `CTX-05` | 历史案例经发布后入库并用于后续召回 | [`RetrospectivePipeline`](../backend/src/repo_maintainer/retrospective_pipeline.py)、[`test_complete_review_publish_and_retrieval_loop`](../backend/tests/test_retrospective_pipeline.py) | 模拟闭环及 2026-10-21 单条重建案例的真实 Wiki、pgvector 入库、召回和仓库排除验证；包含人工运维恢复，不代表全自动成功率或线上召回质量。 |
| `CTX-06` | 飞书显式命令限制当前群与有界时间窗，保留触发原文并排除自生成回复；无仓库历史按应用/群隔离；写操作共享事故锁 | [解析器](../backend/src/repo_maintainer/retrospective_feishu_commands.py)、[命令处理](../backend/src/repo_maintainer/retrospective_feishu_bot.py)、[连接器测试](../backend/tests/test_feishu_connector.py)、[命令测试](../backend/tests/test_feishu_commands.py)、[执行锁测试](../backend/tests/test_incident_execution_lock.py)、[真实 SDK/Celery 库 Mock](../backend/tests/test_feishu_task_runtime.py) | 新后端镜像/internal 隔离 PG 中 151 项相关回归通过、无 skip（含 4 项 PG）；SDK/Celery 库 Mock 5 项通过，前端本机/镜像 build、Compose 和 228 个本地链接通过；官方后台连接及版本发布已确认，真实 @复盘帮助收到回复；真实建单/取证/故障重投/本次模型生成未验收。 |

### 单个问题的来源记录模板

运行时明细以 `evidence_documents` 和复盘页面为准。需要人工审查或导出某个案例时，可在该案例的材料中使用下表；不要在本台账写入真实 Token、密钥或无关个人信息。

| Incident ID | Evidence ID / 历史 Knowledge ID | 所支持的结论 | 来源 URL 或消息 ID | SHA、文件与行号 / 时间 | 原文核对结果 | 限制或争议 |
| --- | --- | --- | --- | --- | --- | --- |
| 待填 | 待填 | 待填 | 待填 | 待填 | 待填 | 待填 |

## 2. 上下文如何构建与压缩

当前流程：**可得的 GitHub/飞书/手工原始采集 → 有仓库与固定版本时局部代码扩展 → 全部 Evidence 原文入库 → 去重和相关性筛选 → 裁剪进入模型的副本 → 历史案例召回 → 可选标准 Tool 补证及最终重选 → 结构化生成 → 双层引用校验**。无 PR 的飞书命令可仅依靠触发原文和当前群窗口，源码与 CI 缺失不能自动补成事实。代码扩展发生在入库前，扩展结果本身也作为原始 Evidence 入库。入口见 [`RetrospectivePipeline.run()`](../backend/src/repo_maintainer/retrospective_pipeline.py)。

| 步骤 | 已实现的方法 | 被保留的内容与限制 |
| --- | --- | --- |
| 飞书命令范围 | 当前白名单群；默认触发前 24 小时、显式窗口最多 72 小时；触发正文单独保存、历史列表跳过自身机器人及同触发消息 | 群聊仍是参与者陈述；服务/环境不作严格过滤；无仓库时按应用/群 repo_id 隔离历史。旧入口默认窗口未改变。 |
| 固定代码版本 | 保存 webhook 的 head/base SHA；源码扩展使用 head SHA，缺失时使用记录的 commit SHA | 代码 Evidence 带实际使用的 SHA 和行号。当前主要分析**修复后的版本**；修复前状态主要来自 diff，尚未自动建立前后两版源码对照。归档不可用时保留其他证据，并在问题元数据记录 `code_context_warning`。 |
| 局部源码扩展 | diff新增行映射Python AST/错误栈/调用/导入/测试引用，唯一目标边两跳；运行装配每文件4区域，基础10文件、按改动数最多120 | 仅发送候选片段和来源，不把全仓当已确认影响；跨语言/动态调用可能漏召回或误连，见[当前预算](long-context-model-upgrade.md)。 |
| 去重与排序 | 内容规范化后按指纹去重；源码额外包含位置，避免不同文件的同文片段合并；按来源优先级、关键词重合和源码检索分排序 | `selection_reason` 记录选择原因；保留不同来源的首条候选，源码优先留一条非 diff 文件。排序不是事实可信度评分。 |
| 日志裁剪 | 优先取包含错误、异常、失败等词或问题关键词的行及前后各 2 行；无匹配时取前 120 行 | 原日志仍在库；模型只看裁剪后的副本。若后续预算再次截断，日志取首尾片段。 |
| Diff 裁剪 | 选中与问题关键词重合的 hunk；无匹配时取前 3 个 hunk | 原始 patch 仍在库；模型可能只看到部分 hunk。后续预算截断时保留首尾。 |
| 证据预算 | Writer默认总量600,000字符，单条配置60,000；CI/diff/源码/群聊/文档/PR/commit优先各留一个名额；约一半预算优先覆盖不同改动文件 | 字符不等于Token；元数据/历史/审核反馈另占空间。原文独立保存；最终片段与原文双层引文校验不变；大窗口也可能遗漏材料。 |
| 历史检索 | 用标题、前 500 字描述、问题类型、最多 3 条故障行及相关代码路径/符号组成查询，最多约 1,800 字符；按 repo_id、问题类型过滤，默认 Top 5；飞书无仓库事故用应用/群复合 repo_id | 已发布知识参与检索。历史案例用于启发，不替代当前证据；没有服务/环境级严格过滤或自动跨分区合并。 |
| 标准工具补证 | REVIEW_TOOLS_ENABLED 默认开启（未配置/留空启用，0/false 关闭）；get_related_code / search_feishu_context / search_historical_cases，最多两轮决策/四次实际执行；共享 8,000 正文字符，单次 15 秒/700 输出 Token；同参数缓存只回送已返回 ID | 服务端固定事故/SHA/授权素材范围；代码与静态采集共用一次归档/索引；飞书只搜索当前已采集材料，历史仍非当前 Evidence；软期限不强制中断同步上游调用。 |
| 生成输入 | 传入筛选后的 Evidence ID、来源、位置、正文、SHA 和选择原因；问题描述最多 2,000 字符；历史案例默认 5 条，`RAG_TOP_K` 最大 20，每条正文最多 1,500 字符 | 不把完整问题日志再次作为 `incident` 字段传入；日志经 Evidence 选择进入模型。模型实际输入还包含 JSON 字段和工具 Schema，**未实现整体精确 Token 预估或硬限额**。 |
| 驳回后重新生成 | 携带审核意见/旧稿，重采集原授权来源并重开有界只读调查，再独立选择上下文 | 不覆盖原文；重采集同 ID 内容变化分配新 ID。保留固定 SHA、群窗口、预算及双层引用校验，未提供的来源不自动开通。 |

选择、裁剪与检索查询见 [`retrospective_pipeline.py`](../backend/src/repo_maintainer/retrospective_pipeline.py)；模型输入见 [`OpenAIReviewGenerator.generate()`](../backend/src/repo_maintainer/retrospective_connectors.py)。轻量补证已接入：Python 方法索引优先解析本地方法、导入别名和直接父类，按调用和后台任务引用补充两跳（至多 24 个种子/邻居），方法起点进入片段候选；同名歧义不在新符号索引中强连，原启发式候选仍保留。`.insight/context.json` 仅提供同仓库服务路径、显式依赖与文档导航，最多 20 服务、4 个文档候选；依赖的影响状态固定为 unverified。`CODE_CONTEXT_PLANNER_ENABLED` 默认 0；关闭标准工具后，显式启用且有未解析方法线索时，最多一次小型规划（最多 8 段各 1,000 字符，15 秒/700 输出 Token，禁用传输重试），返回最多 6 个唯一已有符号/4 个问题，继续受文件/区域预算约束。规划失败保留静态原文。尚未抓取其他仓库或生产运行拓扑。

源码 `region_complete` 表示选区是否完整对应 AST 区域，选择器再次裁剪会将其置 false，原文不变；不保证该片段已覆盖完整恢复链。发现摘要记录方法线索、文件/区域数、规划状态和未解析调用，属于静态候选元数据。

代码归档大小、基础和扩展文件数可通过 `CODE_CONTEXT_MAX_ARCHIVE_BYTES`、`CODE_CONTEXT_MAX_FILES`、`CODE_CONTEXT_MAX_EXPANDED_FILES` 调整，`CODE_CONTEXT_ENABLED=0` 可关闭扩展。

## 3. 客观事实如何保留与校验

**预算方案的研究依据。** [研究说明](long-context-budget-evidence.md)支持有界组织、去重与保留行为链，不证明旧24,000/6,000或32K参数最优。当前按已核验1M模型扩大选材与探索，仍未通过真实事故消融证明新默认最优；见[参数、迁移和限制](long-context-model-upgrade.md)。

1. **原文与模型片段分开。** 流水线先写原始 `Evidence.content`；选择器在深拷贝上裁剪。存储层重复写同一 Evidence ID 时只更新 `selected` 和 `selection_reason`，不会用裁剪文本覆盖原文。数据库表见 [`retrospective_schema.sql`](../backend/src/repo_maintainer/retrospective_schema.sql) 和 [`retrospective_store.py`](../backend/src/repo_maintainer/retrospective_store.py)。这描述的是应用写入行为，不等于数据库具有防篡改保证。
2. **来源可回查。** 当前证据保留 Evidence ID 与可得的 URL、消息 ID、SHA、文件/行号或时间；生成版本保存使用过的 Evidence ID、模型、Token 用量与耗时。某些来源可能没有可直接打开的 URL，应以其他定位字段核对。
3. **推断与观察分离。** AST/调用/导入关系的 `relation` 标记为 `static_candidate`，只负责召回。PR 描述、聊天、手工输入和历史案例都可能含未经核实的说法。源码原文证明“该版本写了什么”，日志证明“记录了什么”；二者都不能单独证明完整的根因、影响范围或运行时调用路径。
4. **引用硬校验。** 影响、根因、修复方案各需引用 Evidence ID。AI 生成和驳回后重新生成时，引文须同时出现在**本次模型看到的片段**与**入库原文**中；人工编辑时校验入库原文。缺 ID、引文不匹配或核心字段无引用时不能进入下一步；模型提示在证据不足时写未知。见 [`QualityValidator`](../backend/src/repo_maintainer/retrospective_pipeline.py) 和 [`OpenAIReviewGenerator`](../backend/src/repo_maintainer/retrospective_connectors.py)。
5. **人工审核。** AI 结果进入待审核状态；人工可编辑、驳回、通过，或在页面提交意见让 Agent 异步重新调查修订；每次修订仍需再次审核。审核通过并发布后才把复盘分块加入历史知识库；原文、请求快照与草稿只在业务表留存用于追溯。引用字符串匹配只校验可定位性，**不能自动校验结论是否由引文充分支持**，因果判断仍需审核人核对。

生成与驳回后重新生成的第二轮请求会附带前轮 `QualityValidator.hard_errors` 或 Writer 结构校验的字段错误，作为 `previous_validation_errors` 输入；通过临时问题副本传递，不写为 Evidence 或事故 metadata。生成器提示使用短单行原文并保留 diff 的 +/- 标记，原有双层匹配规则保持。每次逻辑请求现在在调用前持久化到 generation_contexts（含反馈、裁剪后的实际输入、工具 Schema、模型与选项和内容哈希），Writer 结构拒绝另存 generation_contract_failure（字段错误与已报告用量），但原始失败响应和被拦截草稿仍未保存，不能宣称自动重放全部运行结果。

生成模式可通过 `OPENAI_ENABLE_THINKING` 选择：默认空值不发送提供商扩展，0/1 仅向兼容服务传 `enable_thinking`。本机百炼演练在一次生成连接断开后设为 0；该设置不改变证据、预算或校验门槛，不构成生成质量提高的证明。

### 当前未解决的可追溯性问题

- 新请求已在调用前保存独立快照和哈希：内置生成器 scope 为 provider_request，自定义生成器为 generator_arguments；可选规划单独登记。仅数据库内查询，无新增 HTTP/API 返回。旧版本无快照，不能事后复原；快照不等于完整 HTTP 传输、服务端内部提示词或自动因果验证。
- 当前总预算按字符而非模型 Token 计量；历史案例、问题字段、JSON 包装及工具 Schema 没有统一的请求级硬限额。
- 代码扩展是静态启发式，Python AST 与 JS/TS 解析能力不同；同名函数、动态分派、反射及跨服务调用可能产生误连或遗漏。归档超限或下载失败时会跳过代码扩展。
- GitHub 的大文件或二进制变更可能没有 `patch`；CI 日志取证目前只针对关联 SHA 的一个失败 run，可能缺少最初复现时的运行记录。
- 飞书命令只提供当前群窗口，不自动接通配置发布历史、监控、部署审计或其他服务；短窗口可能遗漏早期排查，较长窗口也可能混入无关讨论。服务/环境仅是填写线索，不能据此断言来源属于相同环境。
- `QualityValidator` 检查引文存在、字段覆盖及 observed/reported 的来源类型门槛，仍不检查“引文是否真的支持该因果结论”；尚无基于真实历史问题的成功率或审核通过率测量。
- [`map_reduce_summary.py`](../backend/src/repo_maintainer/map_reduce_summary.py) 是独立的长文本摘要模块，**未接入本复盘主链路**。不得将其写为当前已运行的压缩步骤。

### 标准 Tool 的证据与输入边界（已实现）

[标准工具执行器](../backend/src/repo_maintainer/retrospective_tools.py)将名称、描述、Pydantic 参数和 handler 注册为 JSON Schema Function Tools，拒绝额外字段、未知工具和越界结果。代码先存原文再构造片段；历史只带 Knowledge ID。缓存按工具名和规范化参数复用，无重复正文；飞书与历史长正文按查询命中位置取连续原文窗口，并保留偏移和 truncated。

模型输出项目原样序列化回放，包括提供商返回的 reasoning 加密内容等字段；按 call_id 配对全部工具请求结果，超额请求也记录 budget_exhausted。最后一轮结果以证据包进入独立最终生成，不重复塞入全部探索轨迹。已用 SDK 对象与 Mock 验证回放，[当前官方 reasoning 协议](https://developers.openai.com/api/docs/guides/reasoning)在 store=false 下默认提供加密内容；其他兼容服务的真实多轮行为尚未验证。

工具实际返回过的证据在最终筛选中加 15 候选分，采用返回的窗口正文，截短区域 region_complete=false；原文仍保留。工具模式不提前登记初始 selected，避免把初始与最终材料混记；最终遗漏的请求 ID 单独记账。历史工具返回的命中窗口优先于初始整卡，不把不同查询排名当作同一可信度。

generation_contexts 复用现有表，保存 tool_exploration、tool_model_response、tool_execution、tool_exploration_summary、tool_context_selection 以及最终生成请求，按 run_id/round/call_id 关联。工具结果是当前素材，不作为新的独立事实来源；模型最终仅可引用本次写作实际看到并可回查的原文。新 AI 生成与重生的提示版本为 v6-human-review-feedback（旧 v5 等历史标签保留）；模型失败或达到轮次上限附软警告，最终引文校验与人工审核不变。

当前 Writer 600,000/单条60,000字符；builtin工具仍8,000字符，DSH工具累计600,000/单条12,000字符（含导航条目）；DSH初始160,000/单条24,000/最多64条，仍两张短历史卡。元数据/Schema/审核反馈另计，无整体精确Token硬限额。驳回重生重取原授权来源，反馈和旧稿单列；旧 selected 累积和旧快照缺失仍保留。模型与实际部署已切 DeepSeek 1017，未新增外部权限或HTTP快照接口。

### 可选 Harness 的证据边界（2026-10-31）

[架构总图与流程对照](architecture.md#21-升级前后与部署状态)已明确：builtin 与 deepseek 择一执行，补证之后统一回到最终选材和独立 Writer。MCP/模型代理属于同一个按次启动的本机网关；原文、固定 SHA、群窗口、历史类比、引文及人工审核约束继续由 Python 领域组件执行，保存会话不等于恢复整个业务任务。

默认 builtin；显式 deepseek 通过固定版 dsh 与本机 MCP 请求原三工具和新增的找文件、字面量搜索、按行读取，共六工具。原始材料、参与者陈述、静态候选、历史类比及人工结论的类别保持；Harness 回答、事件和会话摘要不成为事故原文。源码仍固定本次 SHA，工具仍共享一次归档、区域/字符预算和缓存，由 Pipeline 调用线程实际执行。模型不能通过 MCP 参数修改事故、仓库或飞书窗口。

DSH初始160,000证据字符/64条/单项24,000；默认40探索请求（4,096输出Token）、另4摘要（16,384输出）、80工具执行、600,000返回字符和600秒。单次HTTP等待120秒受剩余期限限制；代理固定模型/工具/路径，准备前后默认16,000,000字节，按配置阻断额外请求并先存快照。1M为容量声明；旧显式值优先。builtin仍两轮/四次/8,000，通用自动模型重试关闭；原生摘要重试0、溢出重试至多1且受代理限额。详见[完整参数](long-context-model-upgrade.md)。

每次实际请求正文（含兼容服务思考选项）在转发前存为 provider_request，不含鉴权头，不冒充完整网络传输。harness_run/profile/event/call_mapping 与旧轨迹共用 generation_contexts/run_id；MCP bridge call ID 在工具结果中关联 Harness call ID。上游密钥仅由 Python 代理持有，DSH 使用短期 loopback 凭证；配置文件结束后脱敏。最终写作独立选择窗口并执行原双层校验，新 AI 版本统一标为 v6-human-review-feedback（旧 v5 标签保留），原 v4/v3 历史标签保留。

启动/模型错误、轮次或期限停止只表示补证可能不完整，不能当作排除证据；存储失败继续阻断。保存会话不代表恢复了归档、缓存、预算或中断业务任务；当前失败重跑仍创建新调查，历史 selected 累积限制保留。输出用量只包含已报告部分，失败费用可能未知。已有 Linux 真实 SDK + 本机 SSE 模型 + 隔离 PG 的验证；用户授权后，百炼 qwen3.7-flash 的 Chat Completions 合成补证也已完成两轮请求和一次工具执行。原 tool/result 映射误读 callId，已改读 toolCallId，并用原真实事件离线回放校验，不新增云端调用。Windows 固定包启动失败；原生压缩已按隔离契约验证，多 Agent、跨重启续跑和真实事故压缩质量仍未验收。详见[接入记录](deepseek-harness-upgrade.md)。

### DSH 原生压缩与来源索引（2026-11-01）

DSH compaction-basic负责压力估算、16%尾部保留的安全选区、摘要和checkpoint；业务不另写压缩算法。deepseek+Chat Completions默认开启，Responses须显式关闭压缩。默认4摘要准入、16,384输出Token、0.8阈值、32,768 headroom；探索40次/4,096输出，整次600秒，共至多44次模型准入，不含Writer。真实事故成本未调优，原生估算不是精确费用保证。

摘要输入实际出现的当前Evidence ID建立来源索引，保留类型/repo/SHA/位置/存储时间及static_candidate=unverified。代理校验完整、唯一且逐项相同的索引与响应结束后回交DSH；不改原文，不自行替换会话。索引48,000字符、准备后请求默认16,000,000字节、摘要响应16,000,000字节上限；超限拒绝而不丢来源。索引保真不证明正文语义或因果，recorded_at可能是采集时间。

harness_compaction_request/result 与原生 start/summary/end 都存现有 generation_contexts；摘要和索引不是 Evidence，不进入最终 Writer 的原始材料。原文、预算和文件额度不因压缩重置；成功原生 checkpoint 后按需从缓存重新交付已检索窗口一次，仍扣剩余正文/导航字符，不重复外部检索/持久化。代理拒绝与原生收敛失败分开记录，compaction_errors 取两者最大值用于质量提示；已完整返回的摘要用量在代理处保存，后续 DSH 拒绝也不丢，未报告/终止调用仍可能未知。无表迁移；后续已完成真实百炼合成压缩和实际部署，见[实测记录](validation/harness-compaction-live-2026-11-01.md)，不等于真实事故效果集或语义无损验证。详见[专项实现与验收](harness-native-compaction.md)。

### 快照搜索与不完整影响范围（2026-10-31）

群聊/日志可以可靠地支持局部现象，可靠性不等于覆盖完整；未提及、未命中和无报错不能用于排除影响。共享[探索策略](../backend/src/repo_maintainer/retrospective_evidence_policy.py)引导从已知症状向调用方、被调用方、配置和显式服务依赖扩展，优先核对可能改变结论的假设；它不是必须遍历全部依赖的确定性算法。

DSH提供find_repository_files/search_repository_code/read_repository_file，仅固定索引快照单行字面量，单次1,000文件/约8MB/3秒/20命中，找文件最多40项。导航是static_candidate/navigation_only/impact_coverage=unknown，不能引用为Evidence；返回计字符、不占源码区域。读取最多120行，装配默认12,000字符、每文件4区域、最多120扩展文件；1MB单文件/相对路径/索引与解析后路径检查继续拒绝越界。未命中/截断/失败保留缺口，无跨仓库或运行拓扑取证。

影响新增 scope_basis=observed/reported/unknown、coverage=partial/unknown、coverage_notes 及 potential_impacts。原群聊仍是参与者陈述，日志是局部记录，源码是静态候选；候选只能 status=unverified，必须带当前 Evidence 引文、成立条件和 verification_needed。observed 至少引日志来源、reported 至少引陈述来源；所有候选引文也执行模型窗口/原文双层校验。不能自动验证语义蕴含、环境匹配或 unknown 文本是否仍有过度断言；人工审核门槛保留，模型没有自动确认或排除影响的权限。

管理台和 Wiki 单列候选及验证方式；候选正文和专属引用不写入五类事实知识切片，案例卡/症状保留影响依据与覆盖缺口，新格式 v4-impact-coverage。其他自由文本仍需人工核对，旧切片不自动清理。旧文档缺新字段时读为 unknown/空候选，无业务表迁移，已有显式预算覆盖新默认。本次代码、测试及部署界限详见[搜索与影响策略](repository-search-impact-strategy.md)。

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

飞书命令入口沿用相同门槛与五类切片，不另建聊天知识库；无仓库事故保留feishu范围。原始触发/参与者陈述/人工确认分别保存；后续历史命中仍仅类比。当前DSH与Writer使用[较大预算](long-context-model-upgrade.md)；builtin两轮/四次/8,000、RAG片段约1,200/重叠100/Top5不变。

| 层次 | 当前持久化内容 | 检索用途与事实边界 |
| --- | --- | --- |
| 原始材料 | `evidence_documents` 保留当前案例的 Evidence 原文及来源定位 | 供审核与引文回查；不把历史 Evidence ID 当作新问题的证据。 |
| 完整复盘 | `retrospective_versions.document` 保存标题、摘要、影响、根因、修复、预防、模块、关键词和引用 | 是知识条目的版本化事实来源；人工修改产生新版本。 |
| 案例条目 | `knowledge_entries` 关联 `incident_id`、`retrospective_version_id`、仓库、问题类型、Wiki URL，并在 metadata 保存关键词、模块、严重度、commit SHA | 只检索 `status=published`；按仓库和问题类型过滤。 |
| 检索片段 | `knowledge_chunks` 现有五类：`case_card`（完整案例卡）、`symptoms`、`root_cause`、`fix_solution`、`prevention`。除主卡外，长字段约 1,200 字符一段、相邻段重叠约 100 字符；每段保存正文、向量、`section`、`segment_index`、`source_evidence_ids`、新片段格式版本 `v4-impact-coverage` 和 embedding 模型名；旧片段不自动重建 | 按复盘字段切片，允许只命中某一侧的根因或修复；Evidence ID 只用于追溯该**历史案例**的原始材料，不可当作当前问题的引用。 |

**召回与使用。** 知识片段完整正文入库；单段向量化输入最多取前 4,000 字符，长字段的后段由独立片段承担召回。新问题由标题、故障描述、故障日志中的关键行，以及所选 diff/源码片段中的路径和符号构造查询，最长 1,800 字符，并生成查询向量。可按仓库、问题类型过滤；[`PostgresRetrospectiveStore.search_knowledge()`](../backend/src/repo_maintainer/retrospective_store.py) 的全文路取查询前 40 个词，以 OR 连接，避免长查询要求所有词同时命中；全文匹配与 pgvector 余弦相似度分别选每个案例的最佳片段，再各取最多 40 个**不同案例**，以 RRF `1/(60+全文名次) + 1/(60+向量名次)` 融合。全文无匹配时仍可由向量路召回。默认取前 20 个案例进入规则精排，最后返回 5 个（`RAG_TOP_K` 可调，最大 20）。返回正文是该案例的 `case_card`，同时带命中片段类型、原始 Evidence ID、版本信息和 Wiki URL。模型接收精排后所返回的案例，每个卡片前 1,500 字符；提示词明确历史案例只能用于类比。完整卡片和完整复盘仍保存在库中。

**精排。** [`_rerank_knowledge_hits()`](../backend/src/repo_maintainer/retrospective_pipeline.py) 将候选的 RRF 分数按本次候选最大值归一化，按 `0.75 × 归一化召回分 + 0.12 × 模块路径匹配 + 0.08 × 关键词匹配 + 0.05 × 标题词重合` 排序。匹配特征和精排前分数记录到返回 metadata，便于检查排序原因。这是无需额外模型服务的确定性精排，不是 cross-encoder 或根因真实性判定；权重仅为初始值，尚未由真实案例集调优。`RAG_CHANNEL_POOL`、`RAG_RRF_K`、`RAG_RERANK_POOL`、`RAG_TOP_K` 分别控制两路候选数、RRF 平滑常数、精排候选案例数和最终案例数；默认值依次为 40、60、20、5。

人工检索入口为 `GET /api/retrospective/knowledge/search?q=...&repo_id=...&incident_type=...&limit=5`；接口见 [`retrospective_api.py`](../backend/src/repo_maintainer/retrospective_api.py)，`limit` 可设为 1–20。

**失败恢复。** Wiki 发布成功但向量生成或知识索引失败时，重试会复用已保存的发布记录，补做知识索引后才标记为已发布；同一版本重建索引会替换旧片段，避免重复案例。对应流程和模拟测试见 [`test_publish_retry_indexes_saved_publication_after_index_failure`](../backend/tests/test_retrospective_pipeline.py) 和 [`test_knowledge_search_returns_distinct_cases_with_structured_card`](../backend/tests/test_retrospective_pipeline.py)。

真实演练还观察到发布记录保存之前的失败：节点已创建、正文因 docx 权限被拒绝，本地没有 publication。该窗口不受上述恢复分支保护，直接重试可能重复建页；本次需人工续写并回读既有节点后登记 publication，不能宣称自动恢复已实现。另一次生成由诊断脚本复用 30 段已索引证据，调用真实模型并重新通过原双层校验后恢复待审核，UI 再编辑审核；其运维介入、模型用量及重复取证范围见[执行记录](real-walkthrough-2026-10-21.md)。

**当前限制与后续验证。** PostgreSQL 的 `simple` 全文配置对中文词语的切分能力有限，中文召回更依赖向量部分；RRF 和规则精排参数尚未用真实案例调优。当前 SQL 会对过滤后的片段计算排名，知识库增长后需检查执行计划和延迟，再考虑专用全文索引、ANN 向量索引及更强的精排模型。改变 embedding 模型或片段格式后，已有知识条目需要专门重建；本次未自动回填旧版条目。2026-10-21 已在真实 PostgreSQL/pgvector 上完成单条重建案例的发布入库、混合召回和仓库排除验证，尚非真实事故集的质量或性能评估。应以真实问题集测量按案例的 Recall@5/20、MRR 或 nDCG、根因可用率、引用可回查率、审核通过率和检索延迟，并对照只用全文、只用向量及混合检索消融，再决定是否扩大候选池、调整精排权重或引入 cross-encoder。pgvector 官方将全文检索与向量检索，以及 RRF 或 cross-encoder 合并，列为[混合检索方式](https://github.com/pgvector/pgvector#hybrid-search)。

### 人工反馈、通知与重新取证（2026-10-31 实现，2026-11-01 本机联调）

- [审核 API](../backend/src/repo_maintainer/retrospective_api.py) 的 `/revise` 要求意见及目标 version_id，记录参与者审核指令后交 Celery。Worker 持事故锁检查版本/状态，再复用采集、builtin 或 DSH 调查、独立选材、Writer 与双层校验；既有同步 `/regenerate` 同样重取证。旧原文/版本不覆盖，重采集同 ID 的不同内容分配新 ID，避免旧引文指向新材料。
- 输入 `review_feedback` 包含审核者、理由（最多 4,000 字）、目标版本、旧稿 JSON（最多 16,000 字并标注截断）、旧稿引用硬校验问题（最多 20 条，每条 300 字）和核对指引。builtin、DSH 及 Writer 均收到，调用前快照持久化；反馈和旧稿不是当前原始材料，不增加 Evidence，审核意见也不是结论已获人工确认。额外字段不计入证据字符预算，整体 Token 硬预算仍未实现。
- 审核人指出引文错时，Agent 可重搜原授权仓库的固定 SHA/按行读取，或查询原群窗材料；不执行任意 shell、扩大仓库/群权限或把 grep 结果导航直接当证据。工具关闭或来源不可用时只能依靠可得材料，须保留未知；自动字符串核验不证明语义支持或因果成立。
- [待审核提醒](../backend/src/repo_maintainer/retrospective_notifications.py) 用标题/ID/版本/页面链接通知事故指定群，飞书发起只发原群。群地址来源不改变证据权限；提醒、模型旧稿与审核意见不作为新的事故原始证据。成功按版本/群去重，失败单独有限重试，可在页面重试，不重生成已有草稿；缺群或 REVIEW_PUBLIC_BASE_URL 在页面提示。状态/通知标记复用 metadata，无新表。
- 页面保存/审核/发布传 version_id 阻止陈旧页面操作，保留旧 API 的省略兼容。approve 再校验当前引文；publish（含已有 publication 的恢复）和索引拒绝未通过状态/非当前稿。知识库只收审核通过且发布的版本；待审核、驳回或 Agent 修订中不建历史案例，无需先入库后删除。
- [闭环测试](../backend/tests/test_retrospective_review_loop.py)、[工具重开测试](../backend/tests/test_retrospective_tool_loop.py)验证模拟通知重试/隔离、意见进入两种调查输入与 Writer、重采集保留原文、异步任务幂等与审核门槛。2026-10-31 独立 Linux/临时 pgvector 中 199 项相关回归通过、无 skip，前端/Compose/当时 305 本地链接通过；该阶段未部署。2026-11-01 后续已部署并完成真实通知/模型修订，详见下方记录；没有事务发件箱、周期补偿、跨重启业务续跑或审核 RBAC。

2026-11-01 真实联调补充：人工描述/日志明确为合成 integration 样本；既有默认飞书文档也被采集，但关键结论引用合成描述/日志，不代表生产原始证据。两次真实 Writer 与新取证/一次 search_feishu_context（empty）完成；意见/旧稿单列输入且原文/版本保留。模型撤回故意注入的生产影响断言，却仍将合成日志标 observed，已通过页面校正为 unknown；这是需要人工审核的实际缺口。错误引文无法保存，陈旧版本批准和未审核发布被拦截；最终无批准/发布/历史知识。两条真实群通知已回读，用户确认链接可用。[详细证据](validation/review-loop-live-2026-11-01.md)。

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
| 2026-10-29 | 标准补证工具改为默认开启，并登记事故类型与当前来源边界 | 运行时/构造/环境模板/Compose 一致；75 项断网 Mock 测试通过、2 项数据库测试跳过；未配置/空值/显式关闭和旧规划互斥回归，配置与本地链接核对；[业务回顾](incident-business-scenarios.md) | 不扩展授权范围、工具预算或证据来源；无新连接器/表/模型；生产日志手工输入仍统一标为 ci_log，需核对实际来源；真实百炼多轮协议与运行部署未验证 |
| 2026-10-29 | 新增飞书显式 @机器人复盘入口，让仅有群聊的无 PR 配置事故复用既有证据与审核链路 | 基线 2fd6e5b + 工作区；核对 listener/parser/bot/tasks/connectors/store；针对性断网 Mock 已通过，当前群、2h/72h 窗口、触发原文、排除自身消息、稳定 ID/执行锁/通知去重及群范围历史；同步架构、业务场景与[部署验收](feishu-bot-setup.md)，检查本地链接 | 新增 lark-oapi 1.7.5 与可选长连接进程，无新表/切片或预算调整；服务/环境只作线索；仍需人工审核发布；该阶段未部署或验证真实飞书/百炼；本轮最终实现由应用入口共享执行锁，直接调 Pipeline 不自动持锁；事务发件箱/跨服务取证未实现 |
| 2026-10-30 | 完成共享事故锁、初次落库前连接错误重试及飞书入口本机部署核对，补记最新证据边界 | 基线 2fd6e5b + 工作区；151 项相关回归在新后端镜像/internal 隔离 PG 全过、无 skip（含 4 项 PG）；SDK/Celery 真实库 Mock 5 项通过；前端本机/镜像 build、Compose 与 228 个本地链接通过；API healthy/ready，飞书后台长连接成功、应用 1.0.3 已发布/通过；真实 @复盘帮助收到回复，任务 status=help/0.623 秒 | 当前引擎原无镜像/容器/卷，新库无旧演练数据，帮助后事故数仍 0；事件和既有权限已配置，无新增权限；本次真实建单/取证/模型生成未验收，不构成完整复盘闭环成功；无新表、切片或预算调整，人工审核及跨系统事务/跨服务取证限制保留 |
| 2026-10-30 | 默认飞书取证窗口从前 2 小时调整为前 24 小时，避免短窗口遗漏事故前期讨论 | 6a9f63b + 工作区；解析器常量/帮助、Worker metadata 和 collect 时间查询断言一致；55 项相关 Mock 测试通过；显式同日/跨日/72 小时上限用例保留，230 个本地链接通过；新容器读回默认值/帮助均为 24 小时，API ready | 仅新命令采用新默认，已建事故与重复投递保留原窗口；显式范围可短于 24 小时，最大 72 小时；原文与模型预算不变，扩大采集范围不保证全部进入模型；本次未真实生成、发布或重跑数据库测试 |

| 2026-10-31 | 引入可选 Harness 调查端口，保留原工具和最终证据/审核边界；模型代理在供应商请求前写快照并限制模型/工具/轮次 | 调查运行器、网关、profile、调用轨迹及真实 SDK/MCP/PG 契约测试；详见[接入与验收](deepseek-harness-upgrade.md) | 工作区实现，默认 builtin；真实模型调用未获自动审批，原服务未部署。无业务表迁移，自动压缩/跨重启续跑/人工补材料仍未实现 |

| 2026-10-31 | 用户授权后以初始视图外的合成标记验证百炼两轮补证，核对实际事件并修正 toolCallId 映射 | 两次真实 qwen3.7-flash 请求、一份原文窗口、2 份请求快照和20事件；原记录离线回放及34项 SDK/MCP/隔离PG 回归通过；[脱敏证据](validation/deepseek-harness-bailian-2026-10-31.json) | 材料仅为合成参与者陈述，不是实际事故原始证据；历史与人工结论规则未改。未调用 Writer/发布/真实来源或更新服务；单案例不证明质量、性能或其他协议兼容 |

| 2026-10-31 | 同步架构图的调查运行器择一、网关入口和独立 Writer 说明，明确流程与部署区别 | 对照现有 Pipeline/Runner/网关及 Compose；本地链接检查；本次为文档澄清，未新增测试或模型调用 | 未改变来源、阈值、引用或审核语义；仍默认 builtin，既有服务未切换，跨重启续跑和质量提升未获此说明验证 |

| 2026-10-31 | 升级固定快照搜索与条件式探索，处理群聊/日志可信但影响清单不完整的情况 | 六工具、范围/预算测试；观测/报告/未知及单列未验证候选；候选双层引文校验、来源门槛、管理台/Wiki 与事实知识切片；198 项相关回归含 10 项真实 SDK/MCP（内存/隔离 PG）通过，前端构建通过；详见[策略](repository-search-impact-strategy.md) | DSH 默认 6 次请求/12 次工具/12,000 字符/60 秒，builtin 仍 2/4/8,000；无业务表迁移，旧文档默认 unknown，旧知识不重建；无新云端调用或服务更新；语义支持度、运行环境/部署匹配、跨仓库及真实事故质量未解决 |

| 2026-10-31 | 补齐人工审核提醒与意见驱动重取证修订，明确草稿留存/历史知识入库区别 | 对照 ReviewNotifier、审核 API/Worker、Pipeline、Writer/builtin/DSH 输入与页面；新增模拟闭环用例覆盖按版本/群通知、失败重试不重生成、意见不作 Evidence、原文不覆盖、版本冲突与未审核拒绝索引；同步架构/README/环境/部署/验收与本地链接检查 | 独立 Linux/临时 pgvector 中 199 项相关回归全过、无 skip，前端构建/Compose/305 本地链接通过，见架构第 10 节及[验证记录](validation/review-loop-2026-10-31.md)；基线 3af8ea8 + 工作区；无新表/依赖，未部署或真实消息/云端模型联调；语义因果支持仍靠人工，无整体 Token 硬限额、事务发件箱、周期补偿或 RBAC |

| 2026-11-01 | 完成真实部署与飞书/模型/网页联调，实查意见驱动重新取证及知识入库保护 | 真实 PostgreSQL 4 个版本/6 条证据、生成快照、2 轮/1 次只读查询（empty）、飞书 v1/v3 消息回读及用户确认；不存在引文、陈旧版本批准、未审核发布均被 409 拦截；来源属性仍需页面校对；最终 v4 pending_review、publications=0、knowledge_entries=0；真实服务已更新，既有思考配置设 false 后补证不再超时，未改预算/表/权限；[联调记录](validation/review-loop-live-2026-11-01.md) | 合成材料与 kind=human 的自动化页面编辑不等于生产事实或真人批准；本次未执行批准/发布、不覆盖生产质量或新 @启动命令；语义/事务/RBAC 等限制保留 |

| 2026-11-01 | 用 DSH 原生压缩替代自行维护会话压缩，分开运行机制与证据保留契约 | 固定 0.2.1rc2 的压力/选区/摘要/checkpoint；代理单独准入/先存请求/检查来源索引与完整响应，拒绝不合格摘要；原文不改，成功 checkpoint 后缓存按需重交且继续扣预算；261 项已安装包回归含 20 项真实 SDK/MCP 全过、无 skip，另补强限额 2 项通过；[验收计数](validation/harness-compaction-2026-11-01.json) | 原生压缩不成为 Evidence 或已确认影响；索引保真不能证明摘要语义无损，recorded_at 可能为采集时间；默认 6 探索 + 2 摘要/摘要 2,048 Token/60 秒，关闭或其他协议限制见[专项契约](harness-native-compaction.md)；无表迁移、本次无云端请求/服务更新；真实提供商压缩质量、失败用量完整性及业务续跑未验收 |

| 2026-11-01 | 按真实模型与部署完成条件验收，修复实际发现的压缩追踪、无素材搜索和结构输出失败 | 真百炼实际 API 容器 6 探索/1 压缩/5 工具；13 合成原文不变，角色/SHA/时间索引校验通过；原生失败与代理拒绝分开、已返回摘要用量不因后续拒绝丢失；零素材不可改写查询补齐；Writer 在既有次数内反馈缺字段等结构错误并记 generation_contract_failure；267 项回归无 skip；[实际记录](validation/harness-compaction-live-2026-11-01.md) | 已部署 f6d6e16cd93f，deepseek/压缩开启，原始与新采集材料共 4 条/最终 v1 pending_review；坏引文/陈旧批准/未审核发布均 409，知识/发布 0；合成材料不是生产事实，observed 仅合成日志记录需人工校对；旧失败、未触发探针不算通过；不自动审批或通知他人，早期失败用量不完整，语义/实际效果集/业务续跑仍有限制 |
| 2026-11-01 | 解除初期32K/6探索/2K摘要及隐藏截断，按1M模型扩大证据路径 | DeepSeek-V4.1-Flash-1017官方规格和百炼两协议真实请求；40探索/80工具/16K摘要、初始160K/工具600K/Writer600K字符，代理16MB与120秒HTTP；275项回归；实际部署长链路12+1请求/11工具/3,374 Token摘要/33原文不变，Writer43,886字证据末尾可见；[实测](validation/long-context-live-2026-11-01.md) | 无业务表迁移，显式旧预算仍优先；模型/配置/镜像已真实部署，旧镜像/配置/DB可回滚，Embedding不变；群聊是陈述/源码是未验证候选/摘要仅导航/历史仅类比；压力测试局部96K，部署1M，未测满窗口或生产事故效果，语义审核与整体Token硬限额等缺口保留 |
