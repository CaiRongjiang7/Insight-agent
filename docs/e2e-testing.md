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

2026-10-21 真实百炼联调发现单次 embedding 输入超过 20 条会返回 400；`OpenAIEmbedder` 已按最多 20 条分批。部署时重建后端镜像并重建 API、Worker 容器，随后重试失败任务；无需 SQL 迁移或新增环境变量。回归见 [test_embedding_batches.py](../backend/tests/test_embedding_batches.py)，包含跨批次乱序响应的文本/向量对齐检查。此修复的真实演练结果须与模拟 E2E 分开记录。

本机 Compose 重建 API 后，若前端 Nginx 因缓存旧容器地址返回 502，可重启该 Compose 项目的 frontend 后再检查健康状态。当前前端客户端超时为 15 秒，重试/重生/发布接口仍同步执行；页面请求超时不代表后端执行已停止，应先刷新任务状态，避免重复提交。环境模板已核对，本次批量及引用反馈修复均无新参数。模型引文不匹配应被拦截；第二轮生成接收具体硬校验错误，只有通过原校验后才进入人工审核。

兼容服务可另设 `OPENAI_ENABLE_THINKING=0/1`；默认留空不发此扩展，不能假定所有服务支持。该配置已同步模板与 Compose，真实演练选用 0。完整执行记录见 [Nacos 场景演练](real-walkthrough-2026-10-21.md)，不能将小型模型探针或模拟 E2E 当作真实闭环成功。

该次真实演练最终已发布 Wiki v2，回读正文并在浏览器确认展示，写入 1 个知识案例及 5 个 1024 维片段，验证同仓库相似查询命中及错误仓库过滤。公网直推改成本地转投真实 GitHub API 数据；生成和部分发布通过人工运维恢复，因此这是一条带人工介入的功能验证记录，不能记为全自动或生产验收。27 项相关单元测试、Compose 校验和文档本地链接检查通过。

## 2026-10-23 轻量上下文升级验收

本次工作区增加 Python 方法优先补证、可选一次规划（默认关闭）、同仓库 `.insight/context.json` 服务文档映射和 generation_contexts 快照表。升级前使用新后端镜像执行 `docker compose run --rm migrate`，再更新 API/Worker。默认 Compose 现已转发 RAG_CHANNEL_POOL、RAG_RRF_K、RAG_RERANK_POOL 与 CODE_CONTEXT_MAX_EXPANDED_FILES。

针对性回归入口：`python -m unittest test_retrospective_pipeline test_review_model_options test_embedding_batches test_context_discovery -q`（PYTHONPATH 包含 src 与 tests）。`TEST_CONTEXT_DATABASE_URL` 只指向独立临时 PostgreSQL，用于重复初始化和快照读写；未配置时跳过该一项。不要使用生产库执行测试。

最终 40 项针对性回归全部通过，包含真实临时 PostgreSQL 用例；Compose 配置及 API/Worker 参数转发实查通过。未重跑完整 UI E2E。

本次在现有依赖镜像中只读挂载 src/tests，临时运行目录写容器内部，模型与外部采集均用 Fake/Mock；另在无主机端口、无持久卷的临时 PostgreSQL 上验证新增表。此范围不同于完整 UI E2E、真实平台和生产部署验收。完整上下文快照仅存数据库，没有新增 HTTP 读取接口。
