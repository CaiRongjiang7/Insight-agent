# DeepSeek 1017 与长上下文真实部署验收

2026-11-01（Asia/Shanghai）；`codex/deepseek-harness-upgrade`，`3af8ea8` + 工作区。参数/动机见[配置说明](../long-context-model-upgrade.md)，[脱敏数据](long-context-live-2026-11-01.json)。本记录是合成材料的兼容性、运行与保护测试，不是生产事故效果集。

## 实际部署与回滚

API、Worker、Feishu Listener 均为 `sha256:926f61c12dcaf70464bfd52c91e0388b8b8ba99aa5ea4a68500633628bd5b7d2`，对应候选 `insight-review-long-context-candidate:20261101`，已标记为 `insight-review-backend:latest` 并实际重建三个容器。前端镜像、数据库、Redis 与业务卷保留；API 重建后已 reload Nginx。SDK/runtime=0.2.1rc2，MCP=2.3.0，未升级依赖版本或业务表。

调查/原生摘要/独立 Writer 均使用 **百炼 deepseek-v4.1-flash-1017**，端点主机 `dashscope.aliyuncs.com`，thinking=false。Embedding 仍是 `qwen3.7-text-embedding-flash`，没有重建知识库。实际读取到的配置为：1,000,000 Token 容量、40 次探索 / 80 次工具、600,000 返回字符、4 次摘要 / 16,384 输出 Token、600 秒调查期限、120 秒 HTTP 等待、32,768 headroom；Writer 600,000/60,000 字符，初始160,000/24,000/64条，代理16,000,000字节。

运行装配源码的10个模块哈希与工作区一致（完整哈希见JSON）。API/PostgreSQL/Redis健康、Worker响应ping且升级前active为0，Frontend/Listener运行；本机当前WLAN `10.160.226.172`，Nginx页面与健康接口实际访问通过。仅核对本机访问，没有另行验证其他设备或最新用户@命令。

升级前保存了忽略目录 `backend/runtime/deployment-backups/long-context-20261101/` 的 `.env.before`、`compose.before.yaml`、数据库自定义格式转储，并通过 `pg_restore -l` 核验。旧镜像保留为 `insight-review-backend:rollback-long-context-20261101`，ID `f6d6e16cd93fa1ac598fd2b6ef910e3d92fb6d822b125afff6cf2b7aba3ea948`。这些文件可能含敏感配置，不进入版本库。回滚时恢复相匹配的旧配置与镜像，保留业务卷；本轮无表迁移，不需要为镜像回退覆盖当前数据库。已有显式旧预算不会自动升级，必须核对 `.env`。

## 真实模型测试

先用既有授权百炼凭据发送两个小型合成请求：Chat Completions回复与Responses函数调用均通过，分别报告14/332总Token；据此确认准确模型名和当前端点能用。没有切换到DeepSeek直连服务或申请新凭据。这里验证的是函数工具Schema调用，不宣称严格JSON Schema输出模式兼容。

长链路使用33份明确标记的合成群聊/文档，以及本地合成固定SHA仓库；不读取真实GitHub或群聊，不访问业务DB、通知和发布。按顺序完成8次不同搜索、找文件、读源码和符号关系查询。原文在内存证据库保存，运行轨迹与快照在独立验收目录留存。结果如下：

| 场景 | 候选镜像 | 已部署 API 容器 |
| --- | --- | --- |
| 时间（UTC） | 11:08:59 | 11:11:39 |
| 探索 / 摘要 / 实际工具 | 12 / 1 / 11 | 12 / 1 / 11 |
| 最大探索请求字节 | 373,949 | 374,243 |
| 最大已报告输入Token（含缓存命中） | 77,265 | 77,386 |
| 摘要输出Token | 3,574 | 3,374 |
| 原生摘要事件 | start / summary / end | start / summary / end |
| 代理 / 原生摘要错误 | 0 / 0 | 0 / 0 |
| 工具正文/导航字符 | 96,171 | 96,171 |
| 原始材料 | 33份不变，新增1源码窗口 | 33份不变，新增1源码窗口 |
| 耗时 | 37.658秒 | 38.014秒 |
| 已报告累计总Token（含缓存重复输入） | 602,995 | 603,322 |
| 停止原因 | no_tool_calls | no_tool_calls |

累计总Token是多次请求的合计，不能当成单次上下文长度，也不等于未缓存输入量或费用。为真实触发DSH原生压缩，压力场景仅在该次设置中覆盖容量为**96,000**、headroom=4,096、初始24,000/单条1,000字符；其他探索/摘要/工具/HTTP预算使用新默认值。**实际部署配置始终为1M，未执行接近1M的满窗口请求**。本轮确实越过了旧6次探索、32K容量、256KB代理和2,048Token摘要限制。

压缩摘要来源台账通过硬校验，原生引擎提交checkpoint；原文不被替换，静态来源仍未验证。硬校验不证明摘要正文语义无损。初次主机启动脚本因Windows执行策略被拒绝，发生在发送模型请求之前；改为仅本进程的执行策略后运行，未更改系统策略；没有将该失败算作模型通过。

## 实际业务链路

通过已部署的Nginx → API → Celery Worker → PostgreSQL → DSH / 百炼 Writer / Embedding 创建独立合成事故：

- 事故 `inc_95ab966641d74b759bcf08f6bb5dfb10`，任务 `5679e406-2c9e-4dbd-bc01-84cd4fbb23dc`。
- 版本 `retro_a7daad4c9aa040d5860adc4524d0b1c9`，v1、prompt `v6-human-review-feedback`、pending_review。
- 23.123秒；DSH真实2次请求/1次工具，零已采集飞书材料后正常停止；小场景未压缩，与上方真实压力测试分开。
- Writer为DeepSeek1017，报告输入13,484 / 输出1,894 Token；实际入选证据**43,886字符**，请求中包含长日志末尾 `END_OF_LONG_SOURCE_20261101`，生成稿也逐字引用此行；突破原24,000字符总选材限制。
- 2份原始合成Evidence，硬校验错误为空。坏引文PATCH、陈旧版本批准、未审核发布全部409，版本未被这些请求覆盖。
- 创建API会为来源空列表应用默认群/doc，因此在任务开始前用带合成标记及状态条件的SQL仅清空该合成记录的源列表，并核验；未抓真实群聊或发送消息。
- 只做拒绝路径测试，没有有效人工批准或发布操作。通知0，publications/knowledge_entries前后均0。

[查看合成待审核稿](http://10.160.226.172/retrospective?incident_id=inc_95ab966641d74b759bcf08f6bb5dfb10)。地址随本机网络变化，不等于稳定外网服务。

模型给出的共享依赖、上游传播等条件仍是待核对假设；本轮只验证引用存在和状态保护，没有证明引用语义足以支持每项假设、生产环境匹配或实际因果。合成日志不能当作生产观测，不能据此确认支付或其他服务受影响。

## 回归与完成条件

最终候选安装包派生测试镜像 `95dcc915089d`，仅挂载测试目录、不覆盖安装源码；隔离内部网络与临时PostgreSQL。**275 passed，0 skipped，60.44秒**，包含20个固定DSH/MCP原生集成用例（内存/隔离PG），两条已有Feishu SDK弃用警告。覆盖预算拒绝、超过256KB的代理转发、长材料选择与原文不变、来源类型、原生成功/无效/供应商失败/关闭/额度、持久化、Writer重试、引用与审核、任务和锁。早期源码挂载快测95通过/5跳过（缺PG）没有替代最终零skip验证。

Compose配置、本地Markdown链接、`git diff --check`和最终健康检查已核对。临时测试PG与网络已清理，生产卷、回滚点和合成验收记录保留。架构、证据台账、README、模板、部署说明和验收标准同步更新。

剩余限制：1M只作已核验模型的容量声明，没有满窗口实测或自动适配任意模型；字符/字节/原生估算不是整体精确Token或费用硬限额；没有生产事故质量/成本消融、语义保真保证、跨仓库运行拓扑或跨重启业务恢复；没有在本轮授权之外发送消息或代替人工批准。
