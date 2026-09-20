# 研发问题复盘 Agent 实现复核记录

## 结论

截至 2026-09-20，核心代码链路已经落地，Mock/Fake 环境可完成：问题创建 → 多源证据 → 历史检索 → 结构化复盘 → 硬校验 → 人工审核 → Wiki 发布 → 知识向量化 → 后续召回。

当前状态属于“代码完成、待真实外部环境验收”。没有 OpenAI、GitHub 和飞书测试凭据时，不能把外部平台联调项标记为验收通过；本机 Docker Engine 未启动，因此只完成 Compose 解析校验，尚未完成镜像构建和容器启动实测。

## 已实现范围

| 需求域 | 实现位置 | 复核结论 |
| --- | --- | --- |
| GitHub Webhook | `retrospective_api.py`、`retrospective_pipeline.py` | merged PR 过滤、HMAC-SHA256 验签、delivery ID 幂等、异步入队已实现 |
| GitHub 上下文 | `retrospective_connectors.py` | PR、commit、diff、评论和可关联失败 Actions 日志已实现 |
| 飞书上下文 | `retrospective_connectors.py` | tenant token Redis 缓存、群消息分页/时间窗、文档正文读取已实现 |
| 飞书发布 | `retrospective_connectors.py` | Wiki 节点创建、文档写入和远端 ID/URL 回写已实现 |
| 数据与向量 | `retrospective_schema.sql`、`retrospective_store.py` | PostgreSQL 事实表、Evidence/Knowledge chunks、pgvector 向量和全文索引已实现 |
| RAG | `retrospective_store.py` | published 知识限定、repo/type 过滤、全文与向量加权融合及分数明细已实现 |
| OpenAI | `retrospective_connectors.py` | 官方 SDK、Responses API、Pydantic Structured Outputs 和 Token/耗时记录已实现 |
| 上下文治理 | `retrospective_pipeline.py` | 原文留存、去重、来源优先级、diff/log 裁剪、总预算和选择原因已实现 |
| 质量校验 | `retrospective_pipeline.py` | Schema、Evidence ID、逐字引用和三类核心结论覆盖硬校验；质量分与软提示已实现 |
| 审核闭环 | `retrospective_pipeline.py` | AI/人工版本、编辑、通过、驳回原因、状态门禁、发布与回流已实现 |
| 异步任务 | `retrospective_tasks.py`、`jobs` 表 | Celery/Redis 与 PostgreSQL 任务状态回写已实现 |
| 管理台 | `RetrospectiveView.vue` | 问题、阶段、错误、证据、质量、版本、审核发布及核心引用并排核对已实现 |
| 部署 | `compose.yaml`、Dockerfiles、`.env.example` | 六服务编排、健康检查、迁移容器和默认飞书范围配置已实现 |

## 有意简化

- 数据迁移使用可重复执行的 SQL 初始化脚本和独立 `migrate` 容器，没有为核心版引入 SQLAlchemy/Alembic；当前没有多版本迁移和 downgrade 需求。
- 固定状态机负责流程决策，LLM 只生成结构化内容。
- 使用 PostgreSQL 原生全文检索与 pgvector 加权融合，不宣称严格 BM25，也不部署独立 reranker/搜索引擎。
- 不实现 RBAC/SSO、多租户、复杂审计、Kubernetes、高可用和企业级可观测平台。
- 生产核心链路不回退到旧 JSON/SQLite/FAISS；内存存储只在单元测试中显式使用。

## 自动验证记录

| 检查 | 结果 |
| --- | --- |
| 核心闭环测试 `test_retrospective_pipeline.py` | 5 passed |
| 旧修复链路 `test_repair_pipeline.py` | 6 passed |
| 后端完整测试集 | 39 passed；2 条第三方 TestClient 弃用警告 |
| 前端 `npm run build` | 通过；仅有 chunk size 警告 |
| Python `compileall` | 通过 |
| `docker compose --env-file .env.example config --quiet` | 通过 |
| Docker 镜像构建/启动 | 未执行完成：本机 Docker Desktop Linux Engine 未运行 |

## 正式验收前必须完成

1. 启动 Docker Engine，执行 `docker compose up -d --build`，确认 migrate、PostgreSQL vector、Redis、API、Worker 和前端状态。
2. 使用真实 GitHub 测试仓库重放 signed merged-PR Webhook，并核对 PR/diff/CI Evidence。
3. 使用真实飞书测试应用读取群消息和文档，发布 Wiki 页面并重复发布验证幂等。
4. 使用真实 OpenAI Key 完成 Structured Outputs 和 embedding smoke test。
5. 在 PostgreSQL 中核对 `evidence_chunks.embedding`、`knowledge_chunks.embedding`、版本、审核和任务记录。
6. 发布带唯一术语的案例，再创建相似问题，确认该案例进入 Top 5。

只有以上真实 smoke 和端到端记录完成后，才可按 `acceptance-criteria.md` 将项目标记为最终验收通过。
