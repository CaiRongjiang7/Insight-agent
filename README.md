# Bug 复盘知识库 — Bug Review Knowledge Pipeline

![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-Backend-009688?logo=fastapi&logoColor=white)
![Vue](https://img.shields.io/badge/Vue-3-42b883?logo=vue.js&logoColor=white)
![Vite](https://img.shields.io/badge/Vite-Frontend-646CFF?logo=vite&logoColor=white)
![Element Plus](https://img.shields.io/badge/Element_Plus-UI-409EFF?logo=element&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

接收 GitHub PR → LLM 自动提取根因、修复方案和预防措施 → 人工审核 → 沉淀为团队可检索的知识条目。

---

## 核心流程

```
GitHub PR/Webhook          LLM 结构化抽取          人工审核             知识库沉淀
     │                         │                    │                    │
┌────▼────┐  触发      ┌───────▼──────┐   draft  ┌──▼──┐  approved  ┌──▼──────┐
│ PR #42  │──────────▶│ Bug Review    │─────────▶│ 审核  │──────────▶│ 知识条目  │
│ fix(rag)│ commit SHA│ Document      │  pending │      │ published │· 可检索  │
└─────────┘           │· 根因         │         └──────┘           │· 关联PR  │
                      │· 修复方案     │◄──────── rejected ────────│· 关键词  │
                      │· 预防措施     │                            └─────────┘
                      └──────────────┘
```

四条审核状态覆盖完整生命周期：

| 状态 | 对应 PR | 示例 |
|------|---------|------|
| `draft` | 刚接收 | PR #1 fix(style): CSS 样式回归 |
| `approved` | 已通过尚未发布 | PR #1 fix(editor): 运行按钮空参数 |
| `rejected` | 已驳回 | PR #1 fix(applist): 重复创建示例应用 |
| `published` | 已发布到知识库 | PR #3 fix(rag): 上传文件报500 |

## 架构

```
┌──────────────┐     ┌─────────────────────────────────────────────┐
│  GitHub PR   │────▶│          六层管道架构                        │
│  Webhook     │     │                                             │
│  Manual      │     │  ① 事件采集 ──▶ ② 任务编排 ──▶ ③ LLM 抽取    │
└──────────────┘     │       │              │              │        │
                     │  ④ RAG 增强 ◀── ⑤ 审核发布 ──▶ ⑥ 知识检索    │
                     │       │              │              │        │
                     │  BM25+向量    审核状态机    语义搜索+关键词    │
                     │  混合召回    (draft→approved→published)       │
                     └─────────────────────────────────────────────┘
```

**技术栈**：

| 层 | 技术 |
|----|------|
| 后端框架 | FastAPI + Uvicorn |
| 前端框架 | Vue 3 + Vite + Pinia |
| UI 组件库 | Element Plus |
| 可视化 | ECharts |
| 数据存储 | JSON 文件 + SQLite（幂等键） |
| LLM 集成 | OpenAI 兼容 API |
| 检索 | BM25 关键词索引 + 向量语义搜索 |
| 发布（阶段一） | 本地 Markdown 输出 |

## 快速开始

### 1. 启动后端（Demo 模式）

无需配置 LLM Key，直接启动即可体验完整流程：

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
python start_demo.py
```

后端地址：`http://localhost:8000`

Demo 模式预设了基于真实仓库 [gulugulu33/aiflow-studio](https://github.com/gulugulu33/aiflow-studio) 的 4 条 Bug 报告、5 条知识条目和审核历史。

> 如需真实 LLM 抽取，配置 `backend/.env` 中的 `LLM_API_KEY` 即可。

### 2. 启动前端

```bash
cd frontend
npm install
npm run dev
```

前端地址：`http://localhost:5173`

### 3. 试用流程

1. 打开工作台 → 看到新手引导卡和 4 条 Demo 数据
2. 点击 **Bug 复盘** → 左侧列表选择一条 Bug 报告
3. 右侧查看 **LLM 抽取结果**（根因/修复方案/预防措施/来源引用）
4. 编辑字段 → 点击 **通过审核** 或 **驳回**
5. 审核通过后点击 **发布到知识库**
6. 在知识库中搜索关键词（如 "upload"、"500"）验证召回效果
7. 点击 **重置示例** 可一键恢复初始数据

## Web 控制台

| 页面 | 路径 | 功能 |
|------|------|------|
| 工作台 | `/dashboard` | 管道总览：沉淀时效、审核进度、知识库增长、Token 消耗 |
| Bug 复盘 | `/ci-autofix` | 核心页面：LLM 抽取结果查看/编辑 + 审核操作（通过/驳回/发布） |
| 知识库 | `/skill-library` | 语义搜索 + BM25 关键词混合召回 |
| 抽取管道 | `/task-scheduling` | 六层管道任务流转可视化 |
| 数据源管理 | `/repo-management` | GitHub 仓库接入 + Webhook 配置 |
| 上下文 | `/context-management` | 代码上下文索引与依赖图 |
| 管道监控 | `/observability` | 各层延迟、成功率、降级触发率 |

## Demo 数据

基于真实仓库 [gulugulu33/aiflow-studio](https://github.com/gulugulu33/aiflow-studio)，所有 PR 编号和 commit SHA 来自真实 `git clone`：

| Bug 报告 | 真实 PR | Commit SHA | 审核状态 |
|----------|---------|------------|----------|
| `bug-aiflow-001` | PR #3 fix(rag): 上传文件报500 | `6ae78ab` | published |
| `bug-aiflow-002` | PR #1 fix(editor): 运行按钮空参数 | `b30d696` | approved |
| `bug-aiflow-003` | PR #1 fix(style): CSS 样式回归 | `70fafa3` | draft |
| `bug-aiflow-004` | PR #1 fix(applist): 重复创建示例应用 | `08c2141` | rejected |

## 目录结构

```
.
├── backend/
│   ├── src/repo_maintainer/
│   │   ├── api.py               # FastAPI 路由
│   │   ├── models.py            # BugReport / KnowledgeEntry
│   │   ├── bug_schema.py        # LLM 抽取结果 Schema + 防幻觉 SourceRef
│   │   ├── pr_listener.py       # GitHub/GitLab Webhook 解析
│   │   ├── idempotency.py       # 幂等键 SQLite 存储
│   │   ├── map_reduce_summary.py # 长上下文 Map-Reduce 压缩
│   │   ├── log_compressor.py    # 日志智能裁剪
│   │   ├── bm25_index.py        # BM25 关键词倒排索引
│   │   ├── retrieval.py         # BM25 + 向量混合召回
│   │   ├── llm_executor.py      # LLM 抽取 + 指数退避重试
│   │   ├── review_workflow.py   # 审核状态机
│   │   ├── wiki_publisher.py    # Markdown 知识发布
│   │   └── frontend_service.py  # 前端数据服务
│   ├── tests/
│   ├── start_demo.py            # Demo 数据注入 + 启动
│   └── .env.example
├── frontend/
│   ├── src/
│   │   ├── views/                # 7 个页面组件
│   │   ├── components/           # 通用组件 + 布局
│   │   ├── api/platform.ts       # API 调用层
│   │   ├── types/platform.ts     # 类型定义
│   │   └── styles/global.css     # 设计系统
│   └── index.html
├── docs/
│   └── refactor-plan.md
└── README.md
```

## 配置说明

| 变量 | 说明 | Demo 模式 |
|------|------|-----------|
| `LLM_API_KEY` | LLM API Key | 不需要 |
| `LLM_MODEL` | 模型名称 | 不需要 |
| `LLM_BASE_URL` | API 地址 | 不需要 |
| `EMBEDDING_API_KEY` | Embedding API Key | 不需要 |
| `API_HOST` | 后端监听地址 | `0.0.0.0` |
| `API_PORT` | 后端监听端口 | `8000` |

> Demo 模式无需任何配置，所有数据预注入，可直接体验完整流程。

## 常用命令

```bash
# 后端
cd backend && .venv/bin/python start_demo.py     # 启动 Demo 服务
cd backend && pytest                               # 运行测试

# 前端
cd frontend && npm run dev                         # 开发模式
cd frontend && npm run build                       # 生产构建
```

## 许可证

MIT License