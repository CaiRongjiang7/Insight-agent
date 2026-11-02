# 源码搜索与影响范围探索策略

2026-10-31（Asia/Shanghai）；`codex/deepseek-harness-upgrade`，基线 `3af8ea8` + 工作区。本文描述本次实现，不表示已有运行服务完成升级。总览见[架构](architecture.md)，来源与上下文约束见[证据台账](evidence-context-ledger.md)。

## 1. 可靠性与覆盖度分开

群聊和日志可以可靠地记录一个现象，但不构成全部受影响服务的清单。群里没有提到支付服务，不能据此排除支付服务；代码显示它调用同一 SDK，也不能据此认定它在本次事故中异常。

| 材料/结论 | 当前语义 | 不能推出 |
| --- | --- | --- |
| 日志原文 | 在记录所处环境、实例和窗口内观测到的现象 | 所有生产实例、用户或服务都受影响；CI 日志等于生产日志 |
| 群聊/PR/文档 | 保留陈述者及限定条件的报告，可作为定位依据 | 被系统自动确认、范围完整，或群里没说的现象不存在 |
| 固定 SHA 源码、配置、调用关系 | 该版本中的静态路径与关联候选 | 事故时部署该版本、执行该路径、必要条件成立或实际传播 |
| 历史案例 | 类比 | 当前事故的原始证据 |
| 人工审核结论 | 由现有审核动作及版本记录负责 | 仅凭模型字段就完成了人工确认 |

不设置自动可信度分数来掩盖这些区别。当前采集的手工日志仍使用 `ci_log` 来源枚举，必须核对 location/原文的实际环境；本次未新增生产观测连接器。

## 2. 搜索工具与范围

DSH 调查提供原三工具及下列三个新工具，共六个。默认 builtin 继续使用原三工具，不改变旧循环的执行预算。事故、仓库、SHA、归档和索引由服务端绑定，工具参数不能指定新仓库、URL、工作目录或命令。

| 工具 | 参数/输出 | 限制 |
| --- | --- | --- |
| `find_repository_files` | `pattern`、`limit`；返回相对路径 | 最多 40 项；相对路径通配符，`*` 可匹配 `/`，`**/` 也匹配根目录；无分页 |
| `search_repository_code` | `query`、`file_pattern`、`case_sensitive`、`limit`；返回文件、行号、短预览与搜索范围 | 单行字面量，等价于受限的 `grep -F`，不执行 shell/正则；最多 20 项，每行预览 240 字符；每次最多 1,000 文件、约 8 MB、3 秒及调查剩余期限 |
| `read_repository_file` | path/start_line/end_line；保存Evidence原文 | 1-based闭区间，最多120行；装配默认12,000字符/每文件4区域/最多120扩展文件；1MB文件上限；直接构造类仍旧小预算 |

文件范围仍是既有索引支持的 Python、JS/TS、JSON/TOML/YAML、Markdown/TXT，不是仓库全部文件。搜索跳过非 UTF-8、二进制、超限或不可读文件，返回 scanned/skipped/truncated；即使扫描完成，也只表示限定快照/文件模式内的字面量搜索完成，不表示影响范围完整。每文件限额/扫描字节数用额外 1 字节探测超限。

文件名、导航预览不是可引用 Evidence，不消耗源码区域额度，但其 JSON 条目计入工具字符预算；必须按行读取并先保存原文后才能引用。搜索结果带固定 repo/SHA、`static_candidate`、`navigation_only` 和 `impact_coverage=unknown`。重复查询不重复正文，已有定位信息保留在前轮轨迹中。索引和读取拒绝符号链接/解析后越出快照的文件。

源码读取的 `region_complete` 仅在返回完整文件时为 true；原方法工具仍按完整 AST 区域判定。任一完整标记均不表示覆盖完整运行恢复链。方法关系工具目前使用 Python 方法索引；其他已支持后缀可文本搜索和按行读取，不能据此宣称动态分派或跨服务调用解析已解决。

实现：[导航](../backend/src/repo_maintainer/retrospective_repository_search.py)、[源码会话](../backend/src/repo_maintainer/retrospective_code_context.py)、[工具执行器](../backend/src/repo_maintainer/retrospective_tools.py)。

## 3. 探索顺序

1. 从已报告症状与日志定位错误文本、配置、接口或方法，保留环境、时间和版本缺口。
2. 找文件/字面量搜索后读取具体区域；对唯一方法查直接调用者与被调用者，检查异常处理和恢复分支。
3. 按能改变结论的具体假设向外扩展。需要服务边界时读取 `.insight/context.json`，搜索依赖者和被依赖者；其声明仍是同仓库静态导航，无映射不等于没有其他服务。
4. 把额外路径表述为有条件的候选：例如“若支付服务在该窗口部署同一故障 SDK，恢复路径可能有同类问题”。明确要核对部署 SHA/SDK 版本、时间窗、实例日志、指标或 Trace。
5. 材料足够、预算用尽或无法获取时停止，保留未知。搜索为空、未记录报错、连接器失败及额度耗尽均不用于排除影响。

该顺序通过[共享策略与材料角色](../backend/src/repo_maintainer/retrospective_evidence_policy.py)、DSH persona、初始视图和 Writer 提示实现，模型仍自主选择实际调用，不保证遍历每条依赖。Harness 自由回答不成为 Evidence，最终 Writer 只使用独立选材得到的原文窗口。没有接通跨仓库取证、生产拓扑、部署审计、监控或 Trace。

DSH当前默认1M容量、40探索请求/80工具/600,000返回字符/600秒，探索4,096输出Token；另4次原生摘要/每次16,384输出，0.8阈值/32,768余量，16%原生尾部保留。初始160,000/单条24,000字符/64条，Writer600,000/单条60,000字符；代理默认16MB，HTTP等待120秒另受调查期限限制。所有范围、旧显式配置兼容及小窗口模型迁移见[长上下文配置](long-context-model-upgrade.md)。实际DeepSeek1017已部署，并通过12探索/11工具/一次3,374Token摘要的[真实验收](validation/long-context-live-2026-11-01.md)；无整体精确Token硬限额或生产质量消融。

## 4. 复盘、校验和兼容

`ImpactSection` 保留 `scope/severity/affected_users/citations`，新增：

- `scope_basis=observed/reported/unknown`：区分局部日志观测、报告与未知，不代表人工已确认。`scope` 只写材料支持的已观测/已报告范围。
- `coverage=partial/unknown`、`coverage_notes`：明确完整度未获证明；不提供自动“全量覆盖”选项。
- `potential_impacts[]`：每项包含条件假设 `content`、固定 `status=unverified`、`citations` 和 `verification_needed`；不混入已发生范围、摘要或用户数。

新生成和驳回重生使用 `v5-impact-hypotheses` 提示版本；旧版本标签不改写。旧文档缺新字段时读为 unknown、空候选和默认覆盖说明，JSONB 原结构兼容，无业务表迁移。候选列表上限 8 条，单项内容与验证方式各最多 1,500 字符。

所有候选引文也执行“模型窗口 + 入库原文”双层校验；人工编辑沿用原文校验。`observed` 至少要引日志来源，`reported` 至少要引参与者/文档来源，静态源码不能独立通过这两种来源门槛。unknown 基础及非全量覆盖生成软提示。**这些是来源类型和字符串定位约束，不是语义支持度证明**：CI/生产混淆、混合引文牵强支持、unknown 文本仍断言事实等仍需审核人核对；不能宣称自动阻止所有过度推断。

管理台单列候选和验证材料，引用面板包含候选引文；Wiki 正文保留同样的区别。候选正文和其专属引用不进入五类事实历史切片，案例卡/症状片段保留已知影响的依据和覆盖说明。其他自由文本仍可能错误混入假设，提示约束不能替代人工审核；旧已发布知识不自动重建或清理。

## 5. 验证与部署

[策略测试](../backend/tests/test_repository_search_strategy.py)覆盖未报告调用方发现、静态候选标签、导航与原文分开、快照/路径/符号链接限制、字面量/空搜索/截断与预算、来源门槛、候选引文、旧文档默认值及知识切片。

[真实 SDK/MCP 用例](../backend/tests/test_deepseek_harness_integration.py)新增四次本机模拟模型请求，依次找文件/搜索、按行读取、调用关系、停止，累计五次工具执行；分别验证内存与独立 PostgreSQL 存储、候选字段回读和待审核状态。模型行为由 fixture 指定，验证通信与契约，不能证明真实模型会选择相同策略或真实事故质量提高。此前百炼两次合成请求只验证原补证兼容性，不作为本次新工具的真实模型验收。

2026-10-31 初始搜索升级未调用新云端或更新服务；2026-11-01 后续已完成[真实百炼与部署验收](validation/harness-compaction-live-2026-11-01.md)，本机显式 deepseek，代码/模板默认仍 builtin。群聊工具新增当前素材数量和 external_fetch=false；零素材时声明不可通过改写查询补齐，避免空搜循环。部署前需按[Harness 接入说明](deepseek-harness-upgrade.md)重建/更新实际镜像并显式选择 deepseek；新默认额度可能增加探索成本，已有显式参数可继续限制。最终核对：198 项相关回归通过、无 skip；前端构建、默认/显式 deepseek 的 Compose 模板解析通过；23 个 Markdown 文档的 291 个本地链接有效，git diff --check 通过。结果同步到架构和台账的本次升级记录。
