# DeepSeek Harness 作为复盘助手执行底座的代码改造规模评估

评估日期：2026-10-31（Asia/Shanghai）。本地基线：`codex/standard-tools-upgrade` / `3af8ea8`。延续[Runtime 选型调研](agent-runtime-survey-2026-10-30.md)。

本文按用户明确的口径，只评估代码改造规模，不评估人力或工期。没有安装 SDK、运行模型、实现桥接或改变当前架构。估算基于仓库源码、测试契约及已核对的 DeepSeek 官方接口；行数是设计估算，不是已完成的 Git diff。外部主分支会变化，实施前须固定实际可安装版本，不能假定当前文档中的所有能力都在同一个发布包中。

## 1. 代码改造规模结论

按人工维护的源文件物理行数粗估；业务代码不含测试、YAML/环境模板、文档、锁文件、生成文件及上游 SDK 源码。新增行指新编写的逻辑，修改行指现有需要调整的行，不将原函数搬迁重复统计为新增能力。各档为累计范围，不能相加。

| 交付档位 | 新增业务代码 | 修改现有业务代码 | 新增/扩展测试 | 业务文件影响 |
| --- | --- | --- | --- | --- |
| 最小接通 | 300–600 行 | 50–100 行 | 200–400 行 | 新增 2–3 个，修改 2–3 个 |
| 可灰度调查底座（推荐首期） | 800–1,400 行 | 150–300 行 | 600–1,000 行 | 新增 5–6 个，修改 5–6 个 |
| 较完整的 Harness 底座 | 2,500–4,500 行 | 400–800 行 | 1,200–2,200 行 | 新增约 8–12 个，修改约 8–12 个，含前端/API/存储 |

最小接通只覆盖模型、三工具和证据包回交。推荐首期增加范围/预算/取消、轨迹、部署、失败对账/受控重跑和回退；Writer、审核发布继续复用。完整档再接管结构化生成、证据感知压缩、跨重启继续同一次调查及人工补材料交互，新增业务代码包含相关 Python/TypeScript/Vue/SQL。

首期另有配置约 80–150 行，涉及依赖声明、镜像、Compose、环境模板及 profile，文档不计入上述行数。业务与测试的新编写源代码合计约 1,400–2,400 行，另调整现有业务代码 150–300 行。文件数随模块拆分方式变化，不能视为精确提交清单。

不包含新接监控/日志平台、服务仓库和部署 SHA 自动绑定、跨仓库取证、多 Agent 调度、模型训练及生产修复执行，也不包含修改或 fork 上游 Harness 核心；这些均应另行评估。

实测的当前代码锚点：`_explore_tools()` 为 72 行，生成器的 `build_tool_request()` 为 33 行、`request_tool_calls()` 为 2 行；`retrospective_tools.py` 为 326 行，`retrospective_code_context.py` 为 602 行，`retrospective_pipeline.py` 为 999 行。后面三项是整个文件的物理行数，不能当作全部需要修改的行数。主要成本在新增接入层，不是重写现有取证、检索与审核。

## 2. 推荐第一期的结构

```text
现有入口 → Celery → RetrospectivePipeline
                       ├─ 基础取证 / 源码归档 / 历史召回
                       ├─ DeepSeek 调查适配器
                       │    ├─ Python SDK → 独立 dsh 运行时
                       │    └─ 只读工具桥接 → 现有 ToolExecutor
                       └─ 最终证据选择 → 现有 Writer → 引文校验 → 审核发布
```

DeepSeek 接管调查中的模型/工具循环和会话。PostgreSQL 仍保存事故、原始 Evidence、版本和审核发布事实；Celery 仍负责投递。Harness JSONL 会话不代替业务数据库，不在第一期重写状态机。

最小替换点是 [RetrospectivePipeline._explore_tools](../backend/src/repo_maintainer/retrospective_pipeline.py)。当前 `_supports_tools()` 依赖生成器的 `build_tool_request/request_tool_calls`，应改为明确选择调查运行器，而不是把 DSH 假装成旧 Responses 客户端。返回契约继续包含新增证据、历史命中、实际请求窗口和停止摘要，供现有最终筛选与 Writer 使用。

## 3. 推荐首期的代码拆分

以下为拟议模块，尚未创建。命名仅用于说明职责，可按实际实现合并。

| 拟议新增模块 | 新增业务代码 | 职责 |
| --- | --- | --- |
| `investigation_runner.py` | 100–150 行 | 调查输入/输出契约、运行器接口、现有循环适配；搬迁的旧逻辑不重复计入 |
| `deepseek_runner.py` | 250–400 行 | SDK 启动、输入构造、运行与停止、期限和重试协调、结果归一化 |
| `retrospective_mcp.py` | 200–350 行 | 三工具桥接、服务端运行身份、现有源码会话复用、Schema/结果转换、调用串行化与生命周期 |
| `retrospective_harness_trace.py` | 150–250 行 | run/session/call 关联、事件/请求快照、用量与停止记录；如需小型 TS 观测插件，计入本项 |
| `retrospective_harness_state.py` | 100–200 行 | 会话与事故映射、目录/资源引用、失败对账和退出清理辅助逻辑 |
| **合计** | **800–1,350 行，取整按 800–1,400 行** | 第六个业务文件可能是小型运行时插件；不预计修改 Harness 核心 |

| 现有模块 | 预计修改现有行 | 改动范围 |
| --- | --- | --- |
| `retrospective_pipeline.py` | 60–100 行 | 注入调查运行器、拆开 `_supports_tools()` 与生成器协议依赖、结果回交与回退 |
| `retrospective_runtime.py` | 30–50 行 | 运行器选择、模型和 Harness 配置装配 |
| `retrospective_tools.py` | 20–50 行 | 显式调用上下文、桥接所需的状态访问；已有检索与证据逻辑复用 |
| `retrospective_tasks.py` | 30–60 行 | Worker 退出、任务失败与运行时资源清理接线 |
| `retrospective_store.py` / `retrospective_models.py` | 合计 10–40 行 | 会话元数据、类型和现有轨迹存储复用 |
| **合计** | **150–300 行** | 首期不预设新增业务表；完整恢复档需另行设计持久状态 |

预计需测试的代码约 600–1,000 行，拆为 3–5 个测试文件，覆盖工具协议/范围/预算、模型事件与证据、取消/崩溃/清理及原链路回退。现有工具、引用、审核发布测试继续复用，不应为了换 SDK 全部重写。

估算假设可安装的 SDK 提供所需 profile/MCP/会话协议，现有模型可以走标准适配路径。若需自行实现供应商协议或 fork Harness 核心，上述范围不适用，应先单独确定新增模块和兼容测试面。

## 4. 为什么不是装一个 SDK 就能完成

### 4.1 跨进程工具接入是第一处主要工作

官方 Python SDK 通过 stdio JSON-RPC 驱动独立 Harness。当前公开协议列出初始化、会话、输入、等待、恢复和交互请求等方法，没有列出一个可直接注册现有 Python callable 的通用工具回调方法。因此，按当前文档，稳妥路线是 MCP 或自定义运行时插件，而不是只给 `harness.run()` 传三个函数。[Python SDK](https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk/README.md)、[协议方法](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sdk/protocol/README.md)

DSH 自带 MCP 客户端支持 stdio 和 Streamable HTTP，可以省去自行实现工具发现和调用协议。第一期优先复用该桥接；如果采用 Python Worker 内的 HTTP MCP 网关，应绑定本机/私网、独立运行身份和生命周期。stdio MCP 子进程也可用于 PoC，但不能直接共享父进程的 Python 对象。[MCP 客户端](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md)

本项目的难点是 [RepositoryCodeSession](../backend/src/repo_maintainer/retrospective_code_context.py) 和 [ToolExecutor](../backend/src/repo_maintainer/retrospective_tools.py) 持有当前进程的源码根目录、AST/符号索引、已返回区域、预算及缓存。不能让每次 MCP 调用重新下载归档，也不能在并发调用下重复消耗或绕过预算。网关执行、状态访问和落库的线程/连接归属需要明确；最初可以按调查串行调用工具，不必立即并行。

### 4.2 Trace 需要保留原有证据语义

现有 `_explore_tools()` 通过局部 `current_call_id` 把同步执行结果记入 [generation_contexts](../backend/src/repo_maintainer/retrospective_store.py)。跨进程后不能直接沿用该闭包。MCP 请求 ID、DSH tool call ID、业务 run ID 是不同身份，需要建立映射。

核对的 MCP 桥源码向上游传 `name/arguments` 和超时/取消选项，并没有在该调用点自动透传业务 run ID 或 DSH call ID。因此，应通过运行上下文、关联结果或小型插件建立显式映射；不能仅凭相同工具名和参数猜测属于哪次调用。[工具桥源码](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/src/tools.ts)

DSH 的通用模型服务同时承载调查、压缩等调用，可以作为观测接入位置。其规范化输入与供应商适配器翻译后的 HTTP 请求应区分标注；只收到 Session 通知时，不应把它写为已保存完整 `provider_request`。[模型服务](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/llm/llm/README.md)

### 4.3 会话恢复不等于恢复一次业务调查

官方 `resume_session()` 恢复已持久化历史并修补中断轮次，不恢复 shell/browser 等外部进程；`request_timeout_seconds` 限制协议响应等待，不限制后续整次调查，超时也不是回滚。[生命周期与超时](https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk/README.md)

官方 checkpoint 在模型/工具/步骤边界刷新日志；中断的工具调用恢复为结果未知，不自动重试。这比自己实现会话日志更省事，但仍需重建本项目的源码会话、缓存与累计预算，并核对原文是否已入库、最终版本是否已经提交。[Checkpoint](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/session/session-checkpoint-policy/README.md)

第二档要求失败可识别、旧运行可查询、无遗留进程、原路径可受控重跑。第三档才承诺从旧会话继续同一次调查，并持久化继续执行所需的工具状态、证据窗口、预算和配置版本。旧 selected 标记等历史限制也不能靠保存 DSH 日志消失。

### 4.4 现有模型和默认 profile 都要实际验证

当前后端使用兼容 Responses 的生成器；DSH 的多供应商层可以声明自定义网关，但这不证明本机百炼的 reasoning、工具回放、错误处理、结构化输出和 Token 计量已兼容。建议 PoC 优先验证现有模型，避免将换模型的效果与换 Harness 混在一起。[多供应商适配器](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/llm/llm-pi-ai/README.md)

最小 profile 默认带全权限 shell；官方 Session-log 插件另有默认启用的官方 API 日志贡献字段。复盘专用 profile 应明确只读工具与实际日志发送范围；这属于可复现的配置改造，不是靠提示词声明“只读”。[Profile 配置](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/sdk-minimal/cordis.patch.yml)、[日志字段](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/session/session-log-deepseek/README.md)

## 5. 最小接通的代码边界

最小版本可以将模型输入、SDK 生命周期和结果交回合并为一个适配器，将三工具 MCP 包装和当前运行上下文合并为一个桥接模块，再调整 Pipeline 与配置装配。估计新增业务代码 300–600 行、修改现有 50–100 行、测试 200–400 行。

这版仅验证模型协议、三工具和证据回交，不把完整请求观测、可靠跨重启恢复、多轮材料追加或完整生产故障处理包含在几百行的估算里。测试通过也不代表统计上的质量提升；真实事故集的质量对照属于验证，不计为新增业务代码。

优先判断现有模型协议、工具状态/证据衔接和取消/超时语义能否在接入层解决。若必须修改 Harness 核心，应先重新界定代码范围。

## 6. 全量接管时的追加范围

第三档除第二档外，需要重新设计结构化 Writer 协议及校验反馈、证据感知压缩、跨运行预算和上下文版本、持久化工具状态、人工补材料 API/页面与可恢复状态迁移。可先利用现有 JSON payload/metadata 验证映射，稳定的执行会话关系和迁移规则再专门设计；不预先承诺无需表变更。

这里可以复用 DSH 的会话/压缩/交互能力，但仍必须保留原始 Evidence、历史类比隔离、原文引文校验及人工审核。官方当前仍标注 developer preview 并预告破坏兼容性变更，应固定版本，保留回退路径；日常升级成本需在首次 PoC 和升级回归后才能可信估算。[项目状态](https://github.com/deepseek-ai/deepseek-harness)

本次只修订代码规模评估文档。实际实施仍须同步架构和证据台账，核对配置/部署/验收文档并检查本地链接；文档行数未计入业务代码规模。
