# DSH 原生压缩真实模型与实际部署验收

2026-11-01（Asia/Shanghai）；`codex/deepseek-harness-upgrade`，`3af8ea8` + 工作区。用户明确要求本轮真实部署，并将真实模型与部署测试作为以后升级的完成条件；已写入 [AGENTS.md](../../AGENTS.md)。本记录保留失败与修复，不把未触发能力的请求当成功。机器可核对的计数、配置及九个已部署源码哈希见 [JSON 记录](harness-compaction-live-2026-11-01.json)。

## 1. 当前实际运行状态

- API、Worker、Feishu Listener 使用最终后端镜像 **f6d6e16cd93f**；前端仍为此前已部署的 **94fb78ab4fa1**，只重载 Nginx 解析更新后的 API 地址。PostgreSQL/Redis 原容器和持久化卷保持。
- 本机 `.env` 显式 `INSTALL_HARNESS=1`、`REVIEW_INVESTIGATION_RUNNER=deepseek`、`REVIEW_HARNESS_COMPACTION_ENABLED=1`。API/Worker 均为 qwen3.7-flash、Chat Completions、thinking=false；上下文声明 32,768，默认 6 次探索 + 2 次压缩，摘要最多 2,048 Token，阈值 0.8/headroom 2,048，调查 60 秒。代码和环境模板的运行器默认仍是 builtin，不代表本机当前选择。
- API/PostgreSQL/Redis healthy，其余服务运行；Worker ping 返回 pong，验收后 active 为空。实际 API 容器内核对没有遗留 Harness 进程；未声称本次收到新用户 @命令或验证了 Listener 的完整消息闭环。
- 宿主 WLAN 已从旧 `10.162.7.238` 变为 **10.160.226.172**。旧地址验收返回异常后，已同步 API/Worker 的 `REVIEW_PUBLIC_BASE_URL=http://10.160.226.172`。本机以当前 WLAN 地址请求审核页和 `/api/health` 都为 200；没有第二台设备的访问确认。

部署前保存了忽略目录 `backend/runtime/deployment-backups/harness-compaction-20261101/` 下的旧配置、Compose 和 70,343 字节 PostgreSQL 自定义格式备份，`pg_restore -l` 校验通过；旧后端 09cfad0a2e56 保留为 `insight-review-backend:rollback-compaction-20261101`。Worker 优雅停止后更新，没有删除业务卷。回滚镜像/运行器时应保留当前有效审核地址，避免再次使用过期 WLAN IP；无新表或数据迁移要求，不需要因回滚运行器而还原整库。

## 2. 真实百炼压力验收

模型均为既有百炼 `dashscope.aliyuncs.com` 的 **qwen3.7-flash**，使用既有凭据。向独立容器只经 stdin 传必要模型配置；部署容器内取其实际运行配置。凭据未出现在生成上下文中，原始材料均为明确标记的合成陈述、合成背景和本地合成源码；假 SHA `aaaa…` 只表示此合成快照，不冒充业务仓库的真实提交。

| 尝试 | 运行位置 | 探索 + 摘要请求 | 实际成功替换 | 结果与处理 |
| --- | --- | --- | --- | --- |
| 02 | 初始候选 | 4 + 2 | 0 | 摘要比 387 Token 的选区更长，被 DSH 拒绝；随后达到模型长度限制。发现原生失败及已报告摘要用量未计入业务摘要，已修复；此旧失败用量不完整。 |
| 03 | 追踪修复候选 | 6 + 2 | 0 | 四类工具均执行，但小选区两次未缩短，后续再次触发被额度拦截，停止原因 compaction_limit；如实计入原生失败，不算压缩成功。 |
| 04 | 同版候选 | 6 + 1 | 1 | 提供明确可压缩的长历史后通过，13 份原文不变。 |
| 05 | 最终 f6d6e16cd93f 候选 | 6 + 1 | 1 | 新素材范围与 Writer 修复后的同版重跑通过。 |
| 06 | 实际 API 容器 | 1 + 0 | 0 | 模型只口头承诺第一步，没有函数调用，不算工具/压缩验收通过。 |
| 07 | **实际 API 容器** | **6 + 1** | **1** | 明确要求实际执行后通过，native start/summary/end 完整、错误 0；5 次工具执行覆盖搜索、找文件、按行读取与调用关系。 |

尝试 07 于北京时间 **18:31:30** 完成，耗时 **23.412 秒**，提供商报告总计 **74,505 Token（含缓存输入）**；这不是费用估算。它在最终已部署 API 容器中执行，用局部压力参数：16,384 上下文声明、8 次探索上限、24,000 工具字符、180 秒期限；阈值 0.8/headroom 2,048/摘要 2,048 与部署默认相同。没有修改服务的正常 32,768/6 次/12,000 字符/60 秒配置。

成功摘要的来源索引校验通过，静态代码仍为 unverified，支付影响仍未知，13 份原文全部保持，另存 1 份读取的源码区域。该演练验证实际运行器、协议、压缩和来源约束，不证明所有历史选区都能有效缩短或真实事故质量提高。模型仍可能把复用 ID 误读为空结果；最终 Writer 不使用其自由回答作为 Evidence，关系/因果仍须回查源码与运行材料。

## 3. 实际 Nginx/API/Celery/数据库生成链路

通过部署的 Nginx 创建合成事故，再由 API 入队、真实 Worker 消费，使用 DSH 与真实百炼 Writer/Embedding。创建接口会对空来源列表应用默认群/文档，因此在任务启动前仅对带本次合成标记且尚未运行的记录进行受保护的范围清空；没有采集真实群聊/文档，也没有发送群消息。

事故：`inc_4cb0dcb96ce44df38c0dd594069515b5`。

1. 首轮任务 `7f56b970-a2d9-4e8e-9450-ed6391cfb75e`：在没有群聊素材时连续改写查询至 6 次上限，随后真实 Writer 漏掉必填 `impact.citations`，未保存版本。失败原文与请求保留。
2. 修复：群聊工具明确返回 eligible_materials=0、external_fetch=false、retry_with_new_query=false；共享探索指令要求停止对此来源反复查询。Writer 缺函数/缺字段/类型错误在**既有生成次数**内将字段级错误反馈给模型，记录 generation_contract_failure 与已报告用量，不填造引用、不放宽 Schema；该失败重试分支另有故障注入测试，不声称本次成功输出必然由重试造成。
3. 最终任务 `f0cf8f34-d464-4743-b3c4-4bd2b7dbd26c`：**15.258 秒**完成。DSH 两轮模型请求、一次查询后识别素材为空并停止；Writer qwen3.7-flash 输入/输出 **2,620 / 596 Token**，生成 **v1 pending_review**，提示版本沿用 v6-human-review-feedback，硬错误为空。

当前 4 条原文包括失败前的 2 条和重新采集的 2 条；旧材料没有被覆盖。影响范围为“模拟图像服务请求超时”，覆盖 unknown；支付条目为带条件和验证需求的 unverified 候选。scope_basis=observed 对应的是**合成日志记录**，不能当真实生产观测；来源属性和语义支持仍需审核人校对。

实际审核保护再次验证：不存在的引文 PATCH、陈旧版本批准、当前未审核发布均返回 **409**；无效稿未新增版本，批准动作 0。`publications=0`、`knowledge_entries=0` 前后不变，通知 0；没有创建 Wiki 或代替人工审核。

当前审核入口：[查看合成验收稿](http://10.160.226.172/retrospective?incident_id=inc_4cb0dcb96ce44df38c0dd594069515b5)。该条仅是自动化合成验收，保留待审核，不是生产事故案例。

## 4. 回归、持久化与边界

最终正式 Dockerfile 镜像的已安装包回归 **267 项通过、无 skip**，包含 20 项真实 SDK/MCP（其中原生压缩 10 项）的本机模型/独立 pgvector 测试；两个警告为既有飞书 SDK 弃用调用。九个已部署模块哈希与工作区相符。临时数据库/网络已清理，业务库和卷未删除。

本次真实发现并修复：原生摘要收敛失败漏计、摘要成功返回后被 DSH 拒绝时的用量漏计、零素材反复搜索、Writer 结构错误无法使用既有反馈次数，以及实际审核地址过期。compaction_proxy_errors/native_errors 分开保留；compaction_errors 使用两者最大值作为质量提示指标，不冒充失败事务去重后的精确计数。响应未报告用量、早期失败和被终止请求的总费用仍可能未知。

原始上下文、JSONL、合成源码和本地脚本保留在忽略的 `backend/runtime/harness-inspection/` 与业务 harness_data 卷；公开记录不含密钥。索引完整不保证摘要正文无损；本次不覆盖真实事故效果集、跨仓库/监控取证、跨重启业务续跑或其他提供商协议。既有审核/RBAC/跨系统事务限制继续存在，不因这次通过而改称已解决。

最终核对：27 个 Markdown 文档的 350 个本地链接有效，git diff --check 与实际 Compose 配置解析通过；Worker pong，API/PostgreSQL/Redis healthy，临时测试库和网络已清理。配置/原始材料备份均被 Git 忽略，公开记录无密钥。
