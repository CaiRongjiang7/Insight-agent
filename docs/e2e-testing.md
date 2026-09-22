# Docker 端到端闭环验收

本测试使用独立的 Compose 项目和数据卷，真实运行 Nginx、API、Celery、Redis、PostgreSQL/pgvector，只模拟 GitHub、飞书开放平台和 OpenAI 兼容接口。它不使用正式 `.env` 中的凭据，也不会写入正式数据库或真实飞书空间。

## 一键执行

在项目根目录运行：

```bash
sh e2e/run.sh
```

脚本将执行以下验收链路：

1. 检查 Nginx、API 和依赖就绪状态。
2. 验证错误 GitHub Webhook 签名返回 401。
3. 验证未合并 PR 被忽略。
4. 发送带 HMAC-SHA256 签名的“已合并 PR”事件。
5. 验证重复 delivery 不会创建第二个问题。
6. 由 Celery Worker 拉取模拟的 PR、Commit、Diff、CI 日志、飞书聊天及飞书文档。
7. 调用 OpenAI 兼容接口生成带 Evidence ID 和原文引用的结构化复盘。
8. 验证引用准确性并进入 `pending_review`。
9. 模拟人工审核通过，发布到模拟飞书 Wiki。
10. 将审核版本切分并写入 PostgreSQL/pgvector 知识库。
11. 使用元数据过滤和向量/关键词混合检索召回新案例。
12. 验证 Webhook 和发布操作的幂等性。

成功时会输出 `"result": "PASS"`、incident ID、trace ID、job ID、证据类型、质量分、飞书页面 URL、知识条目 ID 和召回分数，并打印数据库各业务表的记录数。

## 查看状态和日志

```bash
docker compose -f compose.e2e.yaml ps -a
docker compose -f compose.e2e.yaml logs --tail=200 api worker mock-external
```

测试入口为 `http://127.0.0.1:18080`，与正式环境的 80 端口隔离。

## 清理测试环境

以下命令只删除名为 `insight-review-agent-e2e` 的测试容器、网络和测试数据卷：

```bash
docker compose -f compose.e2e.yaml down -v
```

真实 GitHub/飞书联调应作为第二层验收：使用测试仓库合并一条 PR，并在测试群/测试知识空间中配置真实 Token。确定性 E2E 通过后再运行真实平台联调，出现问题时可以快速区分是核心流程问题还是外部权限/网络问题。
