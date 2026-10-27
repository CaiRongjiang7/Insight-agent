# Nacos gRPC 掉线场景：真实平台演练记录

日期：2026-10-21，Asia/Shanghai。本记录描述本机隔离演练，不是原实习事故的生产复盘，也不是生产环境验收。

最终结果：真实 PR 已合并，六条重建群消息已取证，真实模型生成后经人工修订审核，v2 已发布到 [飞书 Wiki](https://mcnf3hrucokf.feishu.cn/wiki/TxSJwNi9lifLORkjH1kcmCiYnpg)，并写入 PostgreSQL/pgvector，实际检索已召回。公网直推、生成阶段及部分发布阶段的人工介入见下文，不能把本次记为无需干预的自动闭环。

## 已创建的真实材料

- 私有仓库：[nacos-grpc-registration-recovery-demo](https://github.com/CaiRongjiang7/nacos-grpc-registration-recovery-demo)。基于官方 SDK `2.0.7` 的 `b3c26c7ba9157fab411dc4dce452464e08cbead5`，保留上游代码及许可证。
- [PR #1](https://github.com/CaiRongjiang7/nacos-grpc-registration-recovery-demo/pull/1) 已合并；修复 head 为 `77425fc2b98f6994af8e0ebfa0057b7a8d1f1789`，merge 为 `908077aff4f462250278e410a7b6b1ae4370a09f`。
- [修复前失败 CI](https://github.com/CaiRongjiang7/nacos-grpc-registration-recovery-demo/actions/runs/39610672318)：SHA `21c07677385364d849b8e7d01b1cf3ba9c2713cc`；真实 HTTP/2 RST_STREAM(CANCEL=8) 导致 SDK 抛出 `AioRpcError`，`frames_sent=1 client_after=RUNNING reconnect_queue=0`，六项测试中两项报错。
- [修复后通过 CI](https://github.com/CaiRongjiang7/nacos-grpc-registration-recovery-demo/actions/runs/39610976592)：SHA `77425fc2b98f6994af8e0ebfa0057b7a8d1f1789`；`frames_sent=1 client_after=UNHEALTHY reconnect_queue=1`，六项测试通过。首次注入器收尾卡住的两条运行已按用户授权取消，不作为 SDK 缺陷失败依据。
- 配置群内真实发送六条应用机器人消息，均标有“实习场景重建演练 / NACOS-GRPC-20261021”。内容依次为历史症状、待证实云网络猜测、源码线索、当前失败实验、修复实验、验证范围。它们是今日重建材料，不是当年聊天原文。
- 独立 Compose `nacos-image-registration-demo` 启动 Nacos 2.4.3 和图像亮度分类服务。`public` namespace 查得 `image-inference-demo` 的 `healthy=true`、`ephemeral=true`；白色 PNG 的真实 HTTP 推理结果为 `bright / 1.0`。这个简易分类器不是原业务模型。

## 实际入口与部署

真实复盘 UI：[本机 18081](http://127.0.0.1:18081/retrospective)。Compose 项目为 `insight-review-agent-live-demo`，使用独立 PostgreSQL/pgvector、Redis、API、Celery Worker 和前端。旧模拟演练在 18080，两者数据隔离。没有额外采集配置中的无关飞书文档。

Cloudflare 临时入口没有连接成功；设置用户提供的 7897 代理后，TCP/TLS 边缘连接仍返回 EOF。用户明确选择从认证 GitHub API 读取真实合并结果并在本机转投，入口 `/webhooks/github` 验证本地生成的 HMAC 后入队。这是**真实 API 数据的本地中继**，不是 GitHub 公网直推成功，也不是 GitHub 原始签名的转发。未创建仓库 Webhook；隧道和专用本地代理进程已停止。

本次任务 ID 为 `gh_pr_cairongjiang7-nacos-grpc-registration-recovery-demo_1`。取证自动读取 GitHub PR、commit、diff、固定 head 源码和六条飞书消息。当前 CI 连接器只查问题记录的 merge SHA 上的失败运行，因此没有自动拉入上述修复前、后两份完整 CI 归档；其执行结果和链接由 PR 描述及重建群消息提供。两份真实归档已另外下载核验，不能将它们宣称为模型收到的完整日志证据。

## 向量与生成记录

用户明确授权将本次演练的相关源码/diff、PR 描述、CI 材料和六条消息发送到配置的百炼端点。模型为 `qwen3.7-flash`，向量模型为 `qwen3.7-text-embedding-flash`，实际返回 1024 维。

首次取证得到 36 条 Evidence，选中 30 条。向量适配器一次发出“查询 + 30 段证据”，被百炼以批量上限 20 拒绝，任务在 `retrieving` 失败。修复 [OpenAIEmbedder](../backend/src/repo_maintainer/retrospective_connectors.py) 为最多 20 条顺序分批、校验批内返回索引；3 项新增回归与原复盘 21 项测试通过。重建真实演练 API/Worker 后，从 UI 重试同一任务。数据库实查已有 30 条 `evidence_chunks`，每条向量均为 1024 维；此时 `knowledge_entries` 仍为 0，说明证据索引与审核后历史知识入库是两个阶段。

最终结果须由草稿、审核记录、远程正文与数据库实查确认，不能由连接器“已就绪”推定。

第二次执行在引用校验阶段失败：`Quote not found in evidence ev_f29d1130290149559808c3a606112f6d`（SDK diff）。校验拦截后没有保存可发布版本。为纠正重试仍看不到前次错误的问题，初次生成和驳回重生均将上一轮硬错误传入第二轮模型请求，并提示复制单行原文、保留 diff 标记；原校验规则未放宽。新增反馈回归后复盘测试共 22 项，向量测试 3 项。重试取证会保留不同 Evidence ID 的重复来源；第二次执行后共有 54 行原文，不能把这些行数理解为 54 份独立外部材料。单次进入向量索引的上下文仍为 30 段。

第三次执行在 `generating` 以 `Connection error` 失败，无可发布版本。默认模式的小型模型探针返回 reasoning + message，用时 12.99 秒；显式 `enable_thinking=false` 的小型 Function Calling 探针用时 1.34 秒成功。新增可选配置 `OPENAI_ENABLE_THINKING`（默认空，不传扩展），本次隔离演练设置 0 后重试。此对比不是大样本延迟或质量评估；27 项相关回归通过。

## 人工恢复、审核与发布

非思考模式下的页面重试仍有引用不匹配；一次独立诊断生成还遇到无效 JSON。最终在诊断脚本中，复用数据库中已索引的 30 段证据，调用**同一个真实生成器**并保留其 Function Calling 输出，得到通过双层引用校验的草稿：模型 `qwen3.7-flash`，输入 13,016 Token、输出 1,185 Token、耗时 11,753 ms。没有改写模型引文或放宽 `QualityValidator`。

由于页面没有分阶段续跑入口，本次由操作脚本重新核对这份输出与所选片段、入库原文，再沿允许的 `failed → generating → validating → pending_review` 状态迁移保存 AI v1，并登记 `walkthrough_operator` 的 submit 审计及恢复 metadata。这是人工运维恢复，不是普通页面重试自动成功。历次取证累计 90 行原文包含不同 ID 的重复来源；实际用于该次生成的是 30 段，不能把 90 行当作独立材料数。

AI v1 虽然引用得分为 1.0，仍将影响扩大为“所有实例”，并将调度重连推断为“恢复服务可用性”。在 UI 保存人工 v2，修正影响范围、标注 P2 只是演练登记、补充回忆/假设/当前实验的来源区别与未验证范围，然后在 UI 审核通过。该过程展示了字符串引用校验不能取代事实审核。

首次 UI 发布成功创建 [Wiki 节点](https://feishu.cn/wiki/TxSJwNi9lifLORkjH1kcmCiYnpg)，但文档正文写入被飞书 `99991672` 拒绝，明确要求 `docx:document` 或 `docx:document:write_only`；回读还要求 `docx:document` 或只读权限。已请用户开通应用身份 `docx:document` 并发布版本。节点对象为 `CVbEdVfftoa9LVxTwMvcMXctnCf`。此时任务仍为 approved，`publications` 和知识表均为空，不能宣称发布成功。恢复时必须复用已创建的节点，不能直接再次建页。

剩余产品限制包括：失败生成未保存中间草稿；普通重试会重复采集来源；Wiki 建节点与写正文不是原子操作，正文写入失败后发布记录未保存，直接重试可能重复建页；本次只能由操作脚本续写并验证既有节点。当前 Wiki 发布器把 Markdown 行写成普通文本块，未实现原生标题、代码块或可点击的 Evidence 引用样式。

## 最终发布、向量入库与召回实查

用户发布 `docx:document` 应用权限后，恢复脚本核对既有节点的对象与父节点，续写正文并回读校验，登记 publication；没有重新创建节点。随后在 UI 使用原发布接口完成知识索引，任务最终为 `published`、版本为 2、错误字段为空。飞书回读正文为 3,334 字符，包含审核版摘要、根因、修复和全部预防措施；父节点列表中同标题仅 1 个节点且无后续分页。用户登录后已在真实 Wiki 页面观察到“首页”下的该文档与正文。

| 项目 | 最终实际值 | 解释 |
| --- | --- | --- |
| `evidence_documents` | 90 行 | 包含重试重复采集；按类型、位置、正文去重为 36 行，不等于 90 份独立材料 |
| `evidence_chunks` | 59 行，全部 1024 维 | 按 Evidence ID 更新/保留，不会在重试时清空历史不同 ID 的向量；最终有效模型调用选择 30 段，不等于累计存储行数 |
| `retrospective_versions` | 2 行 | 真实模型 AI v1、经 UI 修订审核的 human v2 |
| `publications` | 1 行 | `TxSJwNi9lifLORkjH1kcmCiYnpg`，复用首次创建节点 |
| `knowledge_entries` | 1 行 | `knowledge_retro_75e04489e9e749c9b7b7fc4df777769e`，关联审核后的 v2 |
| `knowledge_chunks` | 5 行，全部 1024 维 | `case_card`、`symptoms`、`root_cause`、`fix_solution`、`prevention` 各 1 行；模型 metadata 为 `qwen3.7-text-embedding-flash`、格式 `v3` |

实际过程是：原始材料存入 `evidence_documents` → 选中的文本调用百炼 embedding（每批最多 20 条）→ 浮点数组写入 `evidence_chunks.embedding` 的 pgvector 列；审核并发布 v2 后，再把完整案例与四类语义字段调用同一 embedding 服务，写入 `knowledge_chunks.embedding`，并保留版本、来源 Evidence ID、仓库与 Wiki URL。飞书 Wiki 呈现审核正文；用于后续问题召回的向量保存在本机 PostgreSQL 中。

通过原知识搜索接口提交“图像推理服务运行几天后注册消失，gRPC 双向流 RST_STREAM 后 watcher 退出，应该如何恢复？”，同时按本仓库和 `bug_fix` 过滤，实际召回上述案例，命中 `symptoms`，返回同一 Wiki URL、RRF metadata 和规则精排分 `0.806667`。该分数是排序值，不是正确率或置信度。改用不相关仓库过滤后返回 0 条。这只验证单条案例上的功能与过滤，不代表真实问题集召回质量或性能评估。

本次工作区修复的 27 项相关测试通过，Compose 配置检查通过，本地文档链接检查通过。验证范围是这次本机演练；没有重建旧知识、跑长期压测或证明原事故完整根因。

## 结论边界

源码与当前注入实验支持：旧 watcher 的循环体内异常边界捕获不到流迭代异常；补上外层捕获后会标记 UNHEALTHY 并调度重连。未证明原事故中 RST_STREAM 的发起方，也未证明 Google Cloud 清理静默 TCP 是根因。原场景“数天后反复掉线”“升级后未再发生”来自用户回忆，没有原生产日志或明确观察窗口。

本次测试没有覆盖正常 EOF、连续抖动、切换后的完整重注册或数天稳定性。质量分与引用字符串校验也不等于因果结论自动成立，发布前仍需人工核对。
