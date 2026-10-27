# 标准取证 Tool 升级方案

> 日期：2026-10-27（Asia/Shanghai）。初始基线：`main` / `63aecdd`。本文先登记实施契约，完成后追加实际验证和限制；当前已在下述分支实现并完成隔离验证，尚未部署；交付边界以下方记录为准。现状总览见[架构文档](architecture.md)，证据边界见[专项台账](evidence-context-ledger.md)。

## 1. 目标与范围

将现有代码、飞书上下文和历史知识能力封装为 JSON Schema Function Tools，由模型按需请求、程序校验执行、以 call_id 回送结构化结果，形成有界的只读补证循环。保留固定的基础采集、最终复盘生成、引用校验、人工审核与发布。

第一版复用现有 OpenAI 兼容 SDK、Pydantic、源码索引、PostgreSQL/pgvector 和 generation_contexts。不引入 MCP 服务、额外图数据库、多 Agent、跨仓库自动取证或生产命令执行。飞书工具查询本次已采集的授权窗口材料，不宣称实现飞书端全文搜索。

## 2. 工具契约

| 工具 | 模型参数 | 执行范围与结果 |
| --- | --- | --- |
| `get_related_code` | symbol、direction（callers/callees/both）、limit（1–6） | 当前事故固定 SHA、同一只读源码会话内的唯一方法及直接关联方法；歧义返回候选，不强连同名方法 |
| `search_feishu_context` | query、limit（1–5） | 已采集并缓存的当前事故群聊与配置文档；输出原始证据 ID、来源与受预算限制的正文 |
| `search_historical_cases` | query、limit（1–5） | 继承事故仓库/类型过滤的现有混合检索；返回 Knowledge ID 与类比内容，不转成当前 Evidence |

参数均以 Pydantic 校验，拒绝额外字段。事故 ID、仓库、固定 SHA、群/文档范围和预算由服务端注入，不能由模型扩大。工具返回统一状态、当前 evidence、historical_cases、歧义候选、truncated 以及结构化错误。空结果、歧义、参数错误、预算用尽和上游错误分别记录。

Tool 注册表显式映射名称、描述、输入模型和 handler。执行器不通过 eval、任意导入或模型提供的函数名访问程序。JSON Schema 只约束结构，业务范围和数量仍由执行器校验。

## 3. 源码会话与证据生命周期

一次任务仅打开一份固定 SHA 只读归档，在同一会话内完成静态取证和全部代码工具调用，共用索引；退出时清理。原 collect 接口保留兼容。

工具读取的新代码先保存原始 Evidence，再根据预算返回副本，保留 SHA、路径、行号和 region_complete。模型收到的截断内容必须显式标记。已有素材去重、最终上下文筛选和引文校验继续执行；工具结果不能绕过人工审核。

静态代码关系、文档与人的陈述、运行观察和历史类比分开。历史案例只能以 Knowledge ID 返回。服务依赖只作候选，不代表业务已经受影响。

## 4. 补证循环与预算

基础采集/静态扩展 → 初始证据选择与历史召回 → 最多两轮模型工具决策 → 执行和回送全部请求的结果 → 最终重新筛选 → 独立复盘生成与校验。

- 配置 `REVIEW_TOOLS_ENABLED=0` 为默认；显式开启后运行标准工具循环，并避免叠加旧的一次符号规划。
- 每次最多两轮工具决策，累计最多四次实际工具执行。超额请求仍回送对应 call_id 的 budget_exhausted，不能静默丢弃协议结果。
- 全部工具输出中的正文共享 8,000 字符预算；每次输出还受单条/条目上限控制。最终证据继续采用既有 24,000/6,000 字符预算；这些不是完整请求 Token 硬限额。
- 工具决策模型请求每次最多 700 输出 Token、15 秒且不自动重试。执行器在调用间检查总时间；历史检索继续受现有连接器超时约束，不宣称能强制中断任意同步操作。
- 工具名与规范化参数构成调用缓存键，重复请求复用结果，避免重复网络查询和重复正文占用。
- 规划请求失败/格式错误时保留静态证据并记录失败；模型不请求工具时提前结束。工具失败不等于证据不存在。

最终生成仅暴露 submit_retrospective，取证工具不包含编辑、审批、发布或修改配置操作。最后一轮工具结果通过重新选择的证据包进入独立写作，不把全部探索历史重复拼进最终请求；工具前的初始 selected 不登记为最终采用。工具请求的候选加 15 排序分，仍可能因最终预算被排除，相关 ID 单独记录。

## 5. 轨迹与兼容

模型请求、有效响应项目、结构化工具结果、停止原因和最终上下文选择使用已有 generation_contexts 持久化，按 run 标识关联；不新增 HTTP 快照读取接口。调用前保存逻辑请求，结果在工具执行后记录。凭据不进入工具参数或结果。

内置模型适配器负责保留协议要求的 response.output 项（含返回的 reasoning/加密内容）并将 function_call_output 与 call_id 对齐。[当前官方 reasoning 说明](https://developers.openai.com/api/docs/guides/reasoning)在 store=false 下默认提供加密内容；本次用 SDK 对象验证回放，不把该行为无条件推广到百炼等兼容提供商。自定义生成器未实现工具决策时保留原固定工作流，不冒充工具模式已运行。

源码不可读时代码工具明确返回不可用，其他已采集素材仍可用。驳回重生不重新访问源码归档和飞书窗口，沿用已经持久化的证据；不得宣称重生自动重放了原工具调用。

新增开关同步环境模板和 Compose API/Worker；其他默认值先固定在代码，避免过多调参入口。部署仍复用现有表；已有环境需确保前次 generation_contexts 迁移已执行。不开工具时保持原主链路。

## 6. 验收条件

1. 每个 Tool 的 Schema 与输入校验一致，未知工具、非法参数和歧义符号不会执行任意能力。
2. 飞书仅检索当前事故已采集窗口；历史结果不生成当前 Evidence ID，也不能通过引用校验冒充事实。
3. 同一仓库会话只下载/索引一次，并在失败或结束后关闭；新片段保留固定 SHA 与原文。
4. 验证两轮/四次调用、共享正文预算、截断、缓存和超额 call_id 的对应返回。
5. 验证模型先取代码再检索聊天或历史、工具结果回送后的再次决策，以及最终复盘引用新增 Evidence 的闭环。
6. 验证请求/结果轨迹、规划失败回退、工具关闭、无源码和自定义生成器兼容；最终生成依然校验引用与人工审核。
7. 复跑既有针对性测试，验证 Compose 配置及本地文档链接。Fake/Mock、真实数据库、真实外部平台与线上部署分别登记。

## 7. 实施与验证记录

- 先在 main/63aecdd 的干净基线上新增本文，再创建并切换到 `codex/standard-tools-upgrade` 实施；主分支没有提交本次升级。
- 已实现：[Tool 注册表/执行器](../backend/src/repo_maintainer/retrospective_tools.py)、[只读源码会话](../backend/src/repo_maintainer/retrospective_code_context.py)、[受控补证循环](../backend/src/repo_maintainer/retrospective_pipeline.py)、[模型协议适配](../backend/src/repo_maintainer/retrospective_connectors.py)和配置接线。使用现有表，无新增网络读取入口。
- 77 项针对性回归全部通过，包括隔离 PostgreSQL 幂等初始化、Tool 请求/结果/新证据/最终引用持久化。模型及 GitHub/飞书使用 Fake/Mock，无真实外部调用。新增测试分别见[源码会话](../backend/tests/test_repository_tool_session.py)、[工具执行器](../backend/tests/test_retrospective_tools.py)、[流程集成](../backend/tests/test_retrospective_tool_loop.py)、[SDK/选择回归](../backend/tests/test_standard_tool_adapter.py)。
- 已验证模型失败回退、非法协议、全部 call_id 配对、缓存和共享正文预算、长文档窗口、截断完整性、最终 selected、历史引用隔离、校验重试和驳回重生。现有真值/类型/仓库过滤与人工审核继续执行。
- Compose 配置及 API/Worker 的 REVIEW_TOOLS_ENABLED 转发检查通过；同步架构、证据台账、README 和验收文档，并检查本地链接及 git diff --check。
- 剩余限制：真实百炼多轮 Function Calling 尚未验证，未重跑完整 UI E2E 或更新原演练/生产环境；默认开关为 0。仍是字符预算、同步上游软期限，旧任务重试 selected 累积不在本次修复范围，未实现跨仓库/生产拓扑或请求级 Token 硬限额。
