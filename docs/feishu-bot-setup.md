# 飞书 @机器人启动智能复盘

2026-10-29 开始实现，2026-10-30 完成核对，基于 `2fd6e5b` + 工作区。组件关系见[架构](architecture.md)，证据边界见[台账](evidence-context-ledger.md)。

## 使用方式

在 `FEISHU_CHAT_IDS` 配置的群里，从飞书的 @ 列表选择应用机器人，发送文本消息：

```text
@机器人 复盘 推理服务配置回滚
服务：image-inference
环境：prod
时间：今天 14:00-15:00
```

仅首行标题必填，也支持“发起复盘 <标题>”；“@机器人 复盘帮助”查看格式。时间必须已经结束；“今天”按消息发送日期、北京时间计算。默认前 2 小时，最长 72 小时；跨日须写完整日期，例如 `2026-10-28 23:00~2026-10-29 01:00`。标题最多 200 字、服务/环境各 100 字、整条命令 4,000 字；重复、未知或空字段返回格式说明。

机器人回复受理与取证窗口，Worker 收集当前群消息、按需查询历史案例，生成草稿后回复事故 ID 和工作台链接。缺少 PR/SHA 时不下载源码。人在工作台审核、发布后才进入 Wiki 与历史知识库；不支持群内批准/发布，不从普通闲聊自动判断事故。

命令仅为发起人的陈述。应在窗口内准备故障现象、配置前后值、处置时间、恢复验证等材料；后续消息不在原窗口内，应在材料齐全后发起新命令。缺少证据的结论保留待确认，引用存在不能证明因果成立。

## 应用设置

企业自建应用启用机器人能力并加入允许群；“事件与回调”选择**使用长连接接收事件**，订阅 `im.message.receive_v1`。可能需要先启动 listener 才能保存长连接配置。

| 能力 | 应用身份权限/配置 |
| --- | --- |
| 接收群内 @ | `im:message.group_at_msg:readonly`（旧 `im:message.group_at_msg` 已授权可能仍有效）及事件订阅 |
| 回复 | `im:message:send_as_bot` |
| 窗口内群历史 | `im:message.group_msg` |
| 发布 Wiki | 沿用既有 Wiki/文档写入权限 |

修改后创建并发布版本，完成组织审批。历史消息读取权限不等于事件订阅。其他群、私聊、机器人发言、其他机器人的 @、非文本消息和普通聊天均忽略。

所选群消息会保存为证据，传给项目配置的模型与 Embedding 服务（当前示例为百炼）。群配置应与团队授权范围一致；命令不能扩大到其他群、文档或仓库。

## 部署

在 `.env` 配置现有 Feishu 凭据和 `FEISHU_CHAT_IDS`。可选 `REVIEW_PUBLIC_BASE_URL=https://review.example.com`，不含 `/retrospective`；留空只回复事故 ID。管理台沿用现有访问控制，链接配置不增加鉴权。

```powershell
docker compose build api frontend
docker compose run --rm migrate
docker compose up -d api worker frontend
docker compose --profile feishu-bot up -d feishu-bot
docker compose logs --tail 50 feishu-bot
```

新增 `lark-oapi==1.7.5`。可选 `feishu-bot` 服务复用后端镜像，无端口映射；仅需出站 HTTPS/WSS，不需公网 IP、域名或隧道接收事件。Worker 必须同步更新。无新表；旧部署仍须有此前的 generation_contexts 等表。停止监听：`docker compose --profile feishu-bot stop feishu-bot`。

非 Docker 开发：安装 backend 声明依赖，配置环境变量，运行 `python -m repo_maintainer.retrospective_feishu_listener`；需已有 Redis、Worker、数据库。SDK 阻塞 start 在独立进程运行。日志设 WARNING，避免 INFO 打印带鉴权参数的连接 URL；不要开启 DEBUG 收集原始群消息。

SDK WebSocket 不保证继承系统代理，7897 HTTP 代理不代表长连接可达。无事件时依次检查连接、订阅与版本、群成员身份、白名单、真正的 @ 及文本格式。

## 去重与失败

回调仅解析、入队，目标在平台 3 秒 ACK 窗口内完成。Broker 连接/读写超时各 1 秒，关闭发布重试；失败交给 SDK 返回失败 ACK，飞书重投。超时参数不是完整 callback 延迟的性能保证。Redis 沿用 AOF，没有跨系统事务或 outbox。

应用、群、message_id 决定事故 ID；event_id 改变也不重复创建。PostgreSQL 独立 autocommit 会话 advisory lock 排除并发；内存测试用线程 Lock。工作台生成、编辑、审核、发布与 Worker 共享锁。飞书任务进程被杀允许重投；拿到锁后发现中间状态则标记中断失败，转工作台核对后重试。

回复使用稳定 UUID（平台仅 1 小时去重）和 metadata 已发送标记。完成回复失败不重生成；受理回复失败仍继续。发送成功后写标记失败、且重试超过平台去重期，仍可能重复回复。通知、锁冲突和暂时数据库连接错误最多 5 次重试，退避最多 60 秒；业务失败不因重复消息自动重跑。无周期补偿扫描或群内状态命令。

不继承全局文档/其他群。历史按 `feishu:<app_id>:<chat_id>` 的 repo_id 隔离；服务/环境仅为元数据，不是独立检索过滤，因此同群不同服务的历史可能作为候选。保持参与者陈述、静态推断、人工确认的区别。

## 验收

PYTHONPATH 包含 backend/src 和 backend/tests：

```text
python -m unittest test_feishu_commands test_feishu_connector test_feishu_bot test_incident_execution_lock test_retrospective_execution_api -q
```

覆盖真正 @、窗口、来源、自回复排除、入队失败、重复消息、并发、无 PR 生成及通知重试。TEST_CONTEXT_DATABASE_URL 仅用独立临时库；跳过不代表数据库已验证。前端运行 npm run build。

真实验收由群内用户先发“复盘帮助”，再发有材料的复盘命令，核对事故唯一、取证范围、引用、pending_review 和链接。完成回复不代表已发布 Wiki。部署和真实平台状态以交付记录为准，Mock 测试不能替代联调。

2026-10-30 交付核对：151 项相关测试在新后端镜像和仅连接隔离 PostgreSQL 的内部网络中全部通过，无跳过；包含 4 项数据库用例、5 项官方 SDK/Celery 兼容与重试用例。前端在本机和镜像构建通过，Compose 配置与本地文档链接检查通过。

当前本机已部署 API、Worker、前端、Redis、PostgreSQL 和 feishu-bot，API healthy 且连接器 ready。飞书后台显示长连接成功，`im.message.receive_v1` 已订阅，应用 1.0.3 已发布，无权限变更。用户已在原配置群发送真正 @机器人的“复盘帮助”，确认收到回复；Worker 返回 help，耗时 0.623 秒，数据库仍为 0 条事故。该路径不调用模型或创建事故。完整飞书命令生成由断网集成测试覆盖，本次未真实发起百炼生成或发布 Wiki。当前引擎最初无容器/镜像/卷，本次新建本地数据库，旧演练数据未自动恢复。

依据：[SDK 版本](https://pypi.org/project/lark-oapi/1.7.5/)、[SDK 长连接](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/ws/client.py)、[事件机制](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/event-subscription-guide/event-subscriptions)、[回复消息](https://open.feishu.cn/document/server-docs/im-v1/message/reply)。
