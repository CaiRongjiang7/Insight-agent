# 人工审核闭环验证（2026-10-31）

后续部署状态：2026-11-01 已完成本机真实部署和飞书/模型联调，见[独立记录](review-loop-live-2026-11-01.md)。下文保留 2026-10-31 离线阶段的验证范围。

核对基线：`codex/deepseek-harness-upgrade`，`3af8ea8` + 当前工作区；保留此前未提交的搜索/Harness 改动。本次局部核对通知、审核反馈、重新取证、版本并发保护与知识入库门槛。未更新运行服务或连接真实群/云端模型。

## 实现与测试范围

- 所有入口 AI 草稿及修订版进入 pending_review 后按事故群发提醒，附审核页面链接。原群隔离、版本/群去重、稳定 UUID、部分发送失败仅重试未送群、缺少 URL 的配置提示、旧版本/非待审核通知跳过；发送失败不重生成草稿。
- 页面保留人工修改，新增意见交给 Agent 和审核记录；API `/revise` 记录理由及目标版本并返回 202，Celery 重采集授权来源、重开只读调查并生成新待审核版本。旧原文/版本留存，重采集同 ID 不同内容重新分配 ID。
- 意见/旧稿进入 Writer、builtin 与 DSH 初始输入，调用前快照持久化，不转为 Evidence。引用硬校验错误继续在生成重试反馈，不能绕过原文校验。
- 编辑/审核/发布的 version_id 冲突、同事故执行锁、异步任务重复/过期投递、失败保留旧版本、通知重试不重生成；通过前拒绝发布/知识索引，包括存在 publication 的恢复路径。隔离 PostgreSQL 重新建 Store 后验证通知/意见快照/版本持久化与审核后知识召回。

## 验证结果

使用已有 `insight-review-harness-test:0.2.1rc2` 与 `insight-review-postgres:16-pgvector` 镜像；一次性 internal 网络、临时 PostgreSQL 数据目录、src/tests 只读挂载，旧平台运行目录写入临时容器。未挂载本地 `.env`，未开放外网。结束后删除临时容器和网络。

以下 15 个测试模块合计 **199 passed，0 skipped**（44.44 秒）；SDK 的两个弃用提示保留。RUN_HARNESS_INTEGRATION=1 使用本机模型 fixture，不是云端模型质量测试。TEST_CONTEXT_DATABASE_URL 仅指向一次性测试库。

```text
test_retrospective_review_loop.py
test_retrospective_pipeline.py
test_retrospective_execution_api.py
test_feishu_bot.py
test_feishu_connector.py
test_feishu_task_runtime.py
test_retrospective_tool_loop.py
test_retrospective_tools.py
test_standard_tool_adapter.py
test_review_model_options.py
test_incident_execution_lock.py
test_repository_search_strategy.py
test_deepseek_runner.py
test_deepseek_harness_integration.py
test_context_discovery.py
```

前端 `npm run build`（vue-tsc + Vite）通过；已有大 chunk 提示未作为失败。`docker compose config --quiet` 通过；README/AGENTS/backend README/docs 的 305 个本地 Markdown 链接检查无失效目标。Windows 原生扩展回归中的符号链接权限问题已由上述 Linux 用例验证，不把 Windows 未执行部分当作通过。

## 部署与剩余限制

无新依赖或表迁移。须同时更新 API、Worker、前端并为双方设置审核人可访问的 REVIEW_PUBLIC_BASE_URL 与事故通知群；旧客户端省略 version_id 仍兼容。新版提示标签 v6-human-review-feedback，历史版本保留。

本次没有部署、发送真实飞书通知或调用云端模型修订。没有事务发件箱、周期补偿、平台永久去重、统一审核身份/RBAC、跨重启业务续跑或整体 Token 硬预算。字符串匹配与只读取证工具不能证明结论语义/因果充分，修订版仍须人工审核，只有通过且发布的版本进入历史知识库；业务草稿、原始证据和请求快照仍先留存供追溯。
