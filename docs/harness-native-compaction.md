# DSH 原生会话压缩接入

最新核对：2026-11-01，实际926f61c12dca / DeepSeek1017 / 1M，40探索+4摘要/16K摘要预算。参数已按[长上下文升级](long-context-model-upgrade.md)更新，历史验收保留；[最新实测](validation/long-context-live-2026-11-01.md)。

2026-11-01（Asia/Shanghai）；`codex/deepseek-harness-upgrade`，基线 `3af8ea8` + 工作区。初始阶段仅代码与隔离验证；用户后续要求真实模型与部署，现已部署为 deepseek，并通过[真实压缩/部署验收](validation/harness-compaction-live-2026-11-01.md)。此前 builtin 人工修订的[记录](validation/review-loop-live-2026-11-01.md)保留。

## 1. 运行时与业务的分工

选择 deepseek 时，Agent Loop、模型/工具协议、会话历史与自动压缩由固定 `deepseek-harness-sdk/runtime==0.2.1rc2` 执行。业务侧不实现另一套历史选区、摘要算法或会话替换；仍负责固定源码版本、工具范围、原始证据、引用与人工审核。

原生 `compaction-basic` 判断上下文压力，按 16% 保留尾部，遵守工具请求/结果配对边界，调用自身 summarizer 并写入 checkpoint。开启并不意味着每次调查都会压缩；初始取证筛选、单次工具结果有界返回、最终独立 Writer 选材继续存在，它们与会话历史压缩职责不同。

依据：[官方循环](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/agent-loop/README.md)、[压缩能力](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/compaction.md)、[压缩配置](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/compaction-basic/src/config.ts)。同时检查了已安装 0.2.1rc2 二进制内的配置/摘要实现，并在真实 Linux 运行时验证，不能只凭 master 文档推断固定包可用。

## 2. 模型路由、预算与失败处理

同一个按次启动的 loopback 网关提供 MCP 和模型代理。探索模型走 `/model/*`；原生压缩配置 `summarizationProvider=insight-compaction`，走 `/compaction-model/*`。二者使用同一固定上游模型与 API Key，但 DSH 只持有本机短期凭证。没有新增常驻服务、公开 API 或业务表。

| 配置 | 默认 | 范围与含义 |
| --- | --- | --- |
| `REVIEW_HARNESS_COMPACTION_ENABLED` | `1` | 仅显式 deepseek 路径开启；`0` 回退关闭，builtin 不受影响 |
| `REVIEW_HARNESS_COMPACTION_MAX_REQUESTS` | `4` | 1–32 次摘要请求准入，与探索默认 40 次分开，均含失败的已准入尝试 |
| `REVIEW_HARNESS_COMPACTION_MAX_TOKENS` | `16384` | 256–131,072，每次摘要输出上限；探索默认 4,096 |
| `REVIEW_HARNESS_COMPACTION_THRESHOLD` | `0.8` | 大于 0.2、至多 0.95；原生压力阈值比例，实际阈值同时受输出预留及 headroom 约束 |
| `REVIEW_HARNESS_COMPACTION_HEADROOM` | `32768` | 至少256；摘要、探索输出与余量之和须小于容量，须给原生尾部留下空间 |

保留比例固定0.16，原生 compactionRetries=0，单次摘要不额外重试；后续步骤可再触发，直至摘要额度耗尽。maxOverflowRetries=1仍受两类代理预算和同一调查期限约束。当前探索40请求/80工具/600,000返回字符/600秒，另4摘要，共至多44次准入，独立Writer另计；1M容量及完整选材/字节/超时参数见[长上下文升级](long-context-model-upgrade.md)。新默认也未经过真实事故质量/费用消融。

目前只验收 Chat Completions。选择 `openai-responses` 且开启压缩会在配置校验阶段报错；原协议需显式 `REVIEW_HARNESS_COMPACTION_ENABLED=0` 使用。不能把未验证协议默认为压缩已兼容。

压缩请求也先在调用线程保存实际逻辑请求，落库失败阻断。准备前后默认最多16,000,000字节（REVIEW_HARNESS_REQUEST_MAX_BYTES），摘要响应缓冲最多16,000,000字节；HTTP等待默认120秒，另受600秒调查期限约束。失败、流未结束、截断或越限不能交回DSH提交；原生引擎决定继续/停止，业务不自行改写会话。达到摘要准入上限记compaction_limit，失败和停止原因进入质量软提示，保留原文及未知范围。

## 3. 证据保留契约

本机代理针对原生选择的摘要输入，从本次事故已持有的原始 Evidence 中建立来源索引。只保护该选区实际出现的已知 ID，包括前轮 checkpoint 里的索引，不把未知 ID 发明成证据。索引逐项保留：

- `evidence_id`、`material_role`、repo、SHA、位置、存储的 `recorded_at`。
- 静态来源固定 `impact_status=unverified`，其他来源为 source_only，不自动表示确认。
- 群聊仍是参与者陈述，日志仍是局部观测，历史仍是类比；摘要本身是导航，不是原始材料。

`recorded_at` 来自 Evidence.occurred_at，飞书消息通常是消息时间，CI/手工材料可能只是采集/入库时间，不能据此填事故发生时间。

业务指令追加到原生summarizer末尾，保留环境/时间限定、传播条件、矛盾陈述、审核更正、未解析分支及失败/空/截断搜索。摘要附带 `<retrospective-evidence-ledger>` JSON，代理校验完全相同后才交给DSH。缺失/重复/格式错误、改ID/来源/SHA/位置/时间或提升静态状态均拒绝；编码索引超过48,000字符也拒绝而不丢来源。实现见[保留契约](../backend/src/repo_maintainer/retrospective_compaction.py)、[网关](../backend/src/repo_maintainer/retrospective_mcp.py)。

这只硬校验来源索引和响应完整性，**不能证明摘要正文的语义忠实**。正文仍可能遗漏环境、错写因果或过度断言；即使索引正确也不能因此称摘要“无损”。新增业务指令与索引占用摘要输入/输出空间，原生 Token 估算不是精确费用保证，过大或不能有效缩短的历史可能压缩失败。真实百炼合成压缩与运行部署已验收；真实事故效果集、其他提供商及上下文溢出恢复仍未验收。

原始 Evidence 和本次工具返回窗口不被压缩结果覆盖；最终 Writer 仍独立读取原始证据窗口、校验引文，不把 checkpoint、压缩事件或来源索引当作事故原文。成功原生 compaction/end 之后，工具缓存允许按需重新交付已有窗口一次；不重新访问外部来源、不重复持久化或占新源码区域，正文/导航字符再次计入剩余预算。缓存、预算、文件额度均不因压缩重置；预算不足仍保留缺口。

## 4. 记录、兼容与验证

沿用 `generation_contexts`：新增用途 harness_compaction_request（实际准备后的模型请求）、harness_compaction_result（接受/拒绝及已接受摘要），原生 compaction/start/summary/end 继续存在 harness_event 和 JSONL 中。运行摘要增加 compaction_enabled/requests/errors/events、total_model_requests；rounds 保持探索次数。完整响应中已报告的摘要 usage 在代理处保存，即使后续 DSH 因不能缩短而拒绝也计入；原生事件不重复累计该用量。compaction_proxy_errors/native_errors 分开保留，compaction_errors 取最大值用于质量提示；网络失败、截断/终止及未报告用量的费用仍可能未知。

无需业务表迁移，不改 v6-human-review-feedback 的 Writer 协议、人工审核或既有知识切片。关闭压缩仍可使用已接入的调查循环与六工具；保存 checkpoint 不等于恢复整个事故任务，跨重启恢复源码归档/工具缓存/预算仍未实现。

[契约测试](../backend/tests/test_harness_compaction.py)覆盖来源索引、分类/版本篡改、截断/异常响应、参数/协议/预算/关闭、成功与失败 checkpoint 的缓存重读。[真实 SDK 测试](../backend/tests/test_harness_compaction_integration.py)在内存与独立 PG 两种存储分别验证正常压缩、无效摘要拒绝、供应商失败、关闭、额度限制，共 10 例；模型为本机 SSE fixture，既验证压缩进入后续模型上下文，也验证失败摘要不被提交、快照先落库和原文不变。

此前真实验收阶段曾部署f6d6e16cd93f，实际API容器触发百炼压缩、Celery生成及审核拦截通过，前端仅reload Nginx；当时267项回归与结果见[该阶段验收](validation/harness-compaction-live-2026-11-01.md)。现在已升级到926f61c12dca，见下文；旧记录保留。

首轮隔离验收：正式候选 e9f1f7dfe489，已安装模块而非源码挂载的 261 项相关回归通过，无 skip；20 项真实 SDK/MCP 中新增原生压缩 10 项。限额场景补强后内存/PG 两项通过。正常 PG 场景为 3 次探索 + 1 次压缩，checkpoint 进入后续上下文；不合格/供应商失败各 2 次摘要均未提交；关闭为 0；首次失败后第二次摘要准入被阻断时为 compaction_limit。五种场景各保留 6 份原始 Evidence，6 个安装模块哈希与工作区一致；见[脱敏验收记录](validation/harness-compaction-2026-11-01.json)。默认与显式 deepseek 的 Compose 模板解析通过，git diff --check 通过，26 个 Markdown 文档的 335 个本地链接检查通过；两个警告来自既有飞书 SDK 弃用调用。

此前f6部署阶段完成6探索/1压缩/5工具与实际Celery/Writer合成验收，267项回归通过，见[原记录](validation/harness-compaction-live-2026-11-01.md)。运行摘要分开记录proxy/native错误，完整响应的摘要usage在原生拒绝后仍保留；零素材查询及Writer反馈属于业务边界。

当前已部署926f61c12dca，DeepSeek1017、1M、40探索/80工具/16K摘要，预算贯通初始/工具/Writer/代理。实际容器完成12探索/11工具/一次3,374Token原生摘要、33原文不变，275项回归通过；压力局部96K，未测满百万，详见[最新实测](validation/long-context-live-2026-11-01.md)。原生Loop与业务证据/审核的职责边界不变。
