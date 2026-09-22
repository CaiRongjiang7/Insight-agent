# 研发问题复盘 Agent（小组/部门核心版）

面向合并 PR 和手工问题的证据化复盘工作流：自动汇聚 GitHub、飞书和历史知识上下文，生成带原文引用的结构化复盘，经人工审核后发布到飞书 Wiki，并回流 PostgreSQL/pgvector 供后续问题召回。

## 核心闭环

```text
GitHub merged PR Webhook / 手工创建
  -> GitHub PR、commit、diff、评论和 CI 日志
  -> 飞书指定群消息、文档上下文
  -> PostgreSQL 全文检索 + pgvector 历史案例召回
  -> 上下文去重、日志/差异裁剪和预算控制
  -> OpenAI Responses API 结构化复盘
  -> Evidence ID 与原文引用硬校验
  -> 人工编辑、通过或驳回
  -> 飞书 Wiki 发布
  -> 审核版本分块、向量化并回流知识库
```

核心页面位于 `/retrospective`。原有仓库自治和演示页面仍保留，但不作为本次复盘闭环的事实数据源。

## 技术实现

| 能力 | 实现 |
| --- | --- |
| API / 状态机 | FastAPI、Pydantic，固定状态迁移 |
| 异步任务 | Celery + Redis |
| 事实存储 | PostgreSQL |
| 语义检索 | pgvector + PostgreSQL 全文检索加权融合 |
| 模型生成 | 官方 OpenAI Python SDK、Responses API、Function Calling + Pydantic 校验 |
| 外部上下文 | GitHub REST API、飞书开放平台 |
| 管理台 | Vue 3 + Element Plus |
| 部署 | Docker Compose |

生产核心链路不会静默回退到 JSON、SQLite、FAISS 或内存存储；内存适配器仅供单元测试显式注入。

## Docker 一键启动

前置条件只有 Docker Engine、Docker Compose v2，以及可用的 OpenAI、GitHub、飞书测试凭据。

```powershell
Copy-Item .env.example .env
# 编辑 .env，填写所有必填凭据和检索范围
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

启动后：

- 管理台：`http://localhost:5173/retrospective`
- API：`http://localhost:8000`
- 健康检查：`http://localhost:8000/api/health`
- 连接器状态：`http://localhost:8000/api/connectors/status`
- GitHub Webhook：`http://<可访问地址>:8000/webhooks/github`

`migrate` 容器会创建 vector 扩展和业务表；`api` 与 `worker` 只有在 PostgreSQL、Redis 及外部连接配置完整时才报告核心链路就绪。

## 必填配置

| 变量 | 用途 |
| --- | --- |
| `POSTGRES_PASSWORD` | 本地 PostgreSQL 密码 |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | 结构化复盘生成 |
| `EMBEDDING_MODEL` | Evidence 和知识条目向量化 |
| `GITHUB_TOKEN` | 拉取 PR、diff、commit、评论和 Actions 日志 |
| `GITHUB_WEBHOOK_SECRET` | GitHub Webhook HMAC-SHA256 验签 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书 tenant token |
| `FEISHU_WIKI_SPACE_ID` | 发布目标 Wiki 空间 |
| `FEISHU_WIKI_PARENT_NODE_TOKEN` | 发布父节点 |
| `FEISHU_CHAT_IDS` | 默认检索群，逗号分隔 |
| `FEISHU_DOC_TOKENS` | 默认检索文档，逗号分隔 |

Webhook 创建的问题会自动继承默认飞书范围；手工创建时可以覆盖群和文档列表。

### 千问配置

项目已针对百炼 OpenAI 兼容接口做过真实调用验证。推荐配置如下：

```dotenv
OPENAI_API_KEY=你的百炼_API_Key
OPENAI_MODEL=qwen3.7-flash
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL=qwen3.7-text-embedding-flash
```

也可以把 `OPENAI_BASE_URL` 换成百炼控制台提供的业务空间专属 `.../compatible-mode/v1` 地址。生成链路使用 Responses Function Calling，避免千问把 JSON 包在 Markdown 代码块中造成 SDK 严格解析失败；最终结果仍会经过完整 Pydantic 与 Evidence 引用校验。当前向量模型默认返回 1024 维，与 pgvector 表结构兼容。

### GitHub Webhook 配置

1. 先生成高熵 Secret，并将结果填入 `.env` 的 `GITHUB_WEBHOOK_SECRET`：

   ```powershell
   $bytes = New-Object byte[] 32
   [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
   [Convert]::ToHexString($bytes).ToLower()
   ```

2. GitHub 仓库进入 `Settings -> Webhooks -> Add webhook`，填写：

   | 配置项 | 值 |
   | --- | --- |
   | Payload URL | `https://你的公网域名/webhooks/github` |
   | Content type | `application/json` |
   | Secret | 与 `.env` 中 `GITHUB_WEBHOOK_SECRET` 完全相同 |
   | SSL verification | Enable SSL verification |
   | Events | Let me select individual events，只勾选 Pull requests |
   | Active | 勾选 |

   GitHub 无法访问 `localhost`。本机联调时，需要先用 HTTPS 隧道把 `localhost:8000` 暴露出去，再把隧道地址拼成 `https://<隧道域名>/webhooks/github`。如果公网反向代理指向前端 `5173`，Nginx 也会把 `/webhooks/` 转发给 API。

3. `GITHUB_TOKEN` 推荐使用 Fine-grained personal access token，只授权目标仓库，并开启 `Pull requests: Read` 和 `Actions: Read`；前者覆盖 PR、diff、commit 和 PR 评论读取，后者用于读取失败的 Actions 运行及日志。
4. 保存 Webhook 后，GitHub 会先发送 `ping`。真正触发复盘的事件必须是 `pull_request` 且 payload 同时满足 `action=closed`、`pull_request.merged=true`。在 Webhook 的 `Recent Deliveries` 中应看到接口返回 `202`；签名错误会返回 `401`。

修改 `.env` 后执行：

```powershell
docker compose --env-file .env up -d --force-recreate api worker
```

### 飞书变量配置

当前项目只主动调用飞书 OpenAPI，不接收飞书事件，所以不需要配置飞书事件订阅地址。

1. 在飞书开放平台创建“企业自建应用”，开启机器人能力。在“凭证与基础信息”复制 App ID 和 App Secret：

   ```dotenv
   FEISHU_BASE_URL=https://open.feishu.cn
   FEISHU_APP_ID=cli_xxx
   FEISHU_APP_SECRET=xxx
   ```

2. 在“权限管理”中为应用申请下列接口需要的权限，然后创建版本并发布：

   - 获取会话历史消息：`GET /open-apis/im/v1/messages`
   - 获取新版文档纯文本：`GET /open-apis/docx/v1/documents/{document_id}/raw_content`
   - 在知识库创建节点：`POST /open-apis/wiki/v2/spaces/{space_id}/nodes`
   - 创建新版文档块：`POST /open-apis/docx/v1/documents/{document_id}/blocks/{block_id}/children`

   飞书控制台会按接口显示并申请当前准确的权限名称；至少需要消息只读、文档只读、文档编辑和知识库节点创建/编辑能力。

3. 把机器人加入需要检索的群。通过飞书 API 调试台调用“获取用户或机器人所在的群列表”取得以 `oc_` 开头的 `chat_id`，多个群用英文逗号分隔：

   ```dotenv
   FEISHU_CHAT_IDS=oc_xxx,oc_yyy
   ```

4. `FEISHU_DOC_TOKENS` 填新版文档 URL 中 `/docx/` 后面的 `document_id`，多个文档用英文逗号分隔。若来源是 `/wiki/<node_token>` 链接，要先通过 Wiki 节点接口取得节点的 `obj_token`，不能直接填 Wiki node token：

   ```dotenv
   FEISHU_DOC_TOKENS=doxcn_xxx,doxcn_yyy
   ```

5. 在飞书知识库中创建或选定发布父节点：

   - `FEISHU_WIKI_SPACE_ID`：通过“获取知识空间列表”API 取得目标空间的 `space_id`。
   - `FEISHU_WIKI_PARENT_NODE_TOKEN`：目标父页面 URL 中 `/wiki/` 后面的 node token。

   ```dotenv
   FEISHU_WIKI_SPACE_ID=7xxxxxxxxxxxxxxx
   FEISHU_WIKI_PARENT_NODE_TOKEN=wikcn_xxx
   ```

6. 把应用/机器人加入目标知识空间并授予编辑权限，同时确保应用有权访问 `FEISHU_DOC_TOKENS` 指定的文档。完成后重建 `api`、`worker`，再检查 `http://localhost:8000/api/health` 和 `http://localhost:8000/api/connectors/status`。

## 使用流程

1. 在 GitHub 仓库配置 `pull_request` Webhook 和相同的 Secret；只有 `closed + merged=true` 会创建任务。
2. 或在“核心复盘”页面手工创建问题，然后点击“运行链路”。
3. Worker 收集并保存原始 Evidence，只把经过选择和裁剪的片段送入模型。
4. 在复盘页面查看质量分、风险提示和引用证据，必要时编辑 JSON 并保存人工版本。
5. 执行“通过”；驳回时必须填写原因。
6. 只有 `approved` 版本可以发布。发布成功后保存飞书 URL，并创建可检索知识条目。
7. 使用 `GET /api/retrospective/knowledge/search?q=...` 验证新案例召回。

## 核心 API

| 方法与路径 | 说明 |
| --- | --- |
| `POST /webhooks/github` | GitHub merged PR 入口、验签与 delivery 幂等 |
| `POST /api/retrospective/incidents` | 手工创建问题 |
| `POST /api/retrospective/incidents/{id}/run` | 投递 Celery 主流程 |
| `POST /api/retrospective/incidents/{id}/regenerate` | 驳回后基于已保存 Evidence 重新生成 |
| `GET /api/retrospective/incidents/{id}` | 问题、Evidence、版本和审核历史 |
| `PATCH /api/retrospective/incidents/{id}/document` | 保存人工修改版本 |
| `POST .../{id}/approve` / `reject` | 审核操作 |
| `POST .../{id}/publish` | 发布飞书并回流知识库 |
| `GET /api/retrospective/knowledge/search` | 混合检索已发布知识 |

## 本地验证

完整 Docker 闭环可使用独立的模拟外部平台环境验证，不使用正式 `.env` 中的凭据，也不会污染正式 PostgreSQL 或飞书空间：

```bash
sh e2e/run.sh
```

测试覆盖 GitHub Webhook 验签与幂等、PR/Commit/Diff/CI 取证、飞书聊天与文档取证、Celery 异步生成、人工审核、飞书 Wiki 发布、PostgreSQL/pgvector 知识入库及 RAG 二次召回。详细说明见 [`docs/e2e-testing.md`](docs/e2e-testing.md)。

```powershell
$env:PYTHONPATH = "$PWD\backend\src"
backend\.venv\Scripts\python.exe -m pytest backend\tests\test_retrospective_pipeline.py -q
backend\.venv\Scripts\python.exe -m pytest backend\tests -q

Set-Location frontend
npm run build
```

核心单元测试使用显式 Fake/内存适配器，覆盖 Webhook 验签与幂等、完整审核发布回流、虚假引用拦截和上下文预算。正式验收仍需用真实 OpenAI、GitHub、飞书凭据完成一次端到端联调。

## 关键目录

```text
backend/src/repo_maintainer/
  retrospective_models.py       # 核心数据契约与状态机
  retrospective_connectors.py   # GitHub、飞书、OpenAI、Redis
  retrospective_pipeline.py     # 固定工作流、质量校验和闭环
  retrospective_store.py        # PostgreSQL/pgvector 与测试适配器
  retrospective_schema.sql      # vector 扩展和业务表
  retrospective_api.py          # 核心 API
  retrospective_tasks.py        # Celery Worker
frontend/src/views/RetrospectiveView.vue
compose.yaml
docs/
```

需求范围、实施顺序和验收方法分别见 [product-requirements.md](docs/product-requirements.md)、[implementation-plan.md](docs/implementation-plan.md) 和 [acceptance-criteria.md](docs/acceptance-criteria.md)。当前完成度与待真实联调项见 [implementation-status.md](docs/implementation-status.md)，旧方案取舍见 [legacy-solution-review.md](docs/legacy-solution-review.md)。
