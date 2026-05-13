# 降低难度指南

本文档面向**学习/教学场景**，提供逐步简化 Code Agent Studio 的策略。所有方案均为可选，按需执行，不会影响项目的完整能力。

---

## 目录

1. [整体策略](#1-整体策略)
2. [依赖简化](#2-依赖简化)
3. [运行时模式降级](#3-运行时模式降级)
4. [Agent 复杂度分级](#4-agent-复杂度分级)
5. [向量检索降级](#5-向量检索降级)
6. [功能开关式渐进简化](#6-功能开关式渐进简化)
7. [配置最小化](#7-配置最小化)
8. [API 端点精简](#8-api-端点精简)
9. [前端页面精简](#9-前端页面精简)
10. [Demo 数据准备](#10-demo-数据准备)

---

## 1. 整体策略

降级遵循三个原则：

- **渐进式**：每一步独立可逆，不会破坏现有功能
- **配置驱动**：大部分难度控制通过 `.env` 开关实现，无需改代码
- **教学友好**：保留核心架构（Agent 协作、SSE 工作流、Dashboard），仅减少外围复杂度

推荐按以下顺序执行：

```
依赖简化 → 运行时降级 → Agent 单模式 → 禁用自学习 → 精简前端
```

---

## 2. 依赖简化

### 2.1 当前完整依赖

`pyproject.toml` 中列出的核心依赖：

```toml
dependencies = [
  "fastapi>=0.115.0,<1.0.0",
  "uvicorn>=0.32.0,<1.0.0",
  "faiss-cpu>=1.8.0",            # 可移除
  "hnswlib>=0.8.0",              # 可移除
  "sentence-transformers>=5.0.0",# 可移除（reranker 用）
  "pymilvus>=2.4.0",             # 可移除
  "psycopg[binary]>=3.2.0",      # 可移除
  "pgvector>=0.3.0",             # 可移除
]
```

### 2.2 最小依赖方案

仅保留核心 Web 服务所需依赖：

```toml
dependencies = [
  "fastapi>=0.115.0,<1.0.0",
  "uvicorn>=0.32.0,<1.0.0",
]
```

### 2.3 操作步骤

在 `pyproject.toml` 中注释掉不需要的可选依赖：

```toml
dependencies = [
  "fastapi>=0.115.0,<1.0.0",
  "uvicorn>=0.32.0,<1.0.0",
  # 以下为可选依赖，学习场景可注释掉
  # "faiss-cpu>=1.8.0",
  # "hnswlib>=0.8.0",
  # "sentence-transformers>=5.0.0",
  # "pymilvus>=2.4.0",
  # "psycopg[binary]>=3.2.0",
  # "pgvector>=0.3.0",
]
```

> 💡 **说明**：移除这些依赖后，向量检索会自动降级使用内置的 SQLite ANN 方案，reranker 功能会自动跳过。

---

## 3. 运行时模式降级

### 3.1 三种运行时模式

| 模式 | 隔离级别 | 复杂度 | 适用场景 |
|------|---------|--------|---------|
| `container` | Docker 容器 | 🔴 高 | 生产环境 |
| `process` | 独立进程 | 🟡 中 | 默认模式 |
| `thread` | 线程池 | 🟢 低 | 学习/调试 |

### 3.2 配置方式

```env
# 降低到线程模式（最简单）
AGENT_RUNTIME_BACKEND=thread
AGENT_RUNTIME_MAX_WORKERS=2

# 关闭沙箱网络隔离（本地调试更方便）
AGENT_SANDBOX_NETWORK_ENABLED=1
AGENT_SANDBOX_MODE=workspace
```

### 3.3 关掉沙箱容器

```env
# 使用本地 workspace 副本代替 Docker 容器
SANDBOX_BACKEND=workspace
```

当 `SANDBOX_BACKEND=workspace` 时，系统不会尝试启动 Docker 容器。

---

## 4. Agent 复杂度分级

### 4.1 模式对比

| 模式 | Agent 数量 | 角色 | Token 消耗 | 执行时间 |
|------|-----------|------|-----------|---------|
| 多 Agent 辩论 | 6 个 | Diagnose→Fix→Validate→Critic→Rebuttal→Reflect | 🔴 高 | 🟡 慢 |
| 三 Agent 协作 | 3 个 | Diagnose→Fix→Validate | 🟡 中 | 🟡 中 |
| 单 Agent | 1 个 | Generalist | 🟢 低 | 🟢 快 |

### 4.2 强制单 Agent 模式

调度器会根据复杂度自动选择单 Agent 或多 Agent。通过调高阈值可强制使用单 Agent：

在 `backend/src/repo_maintainer/config.py` 中修改 `SchedulerConfig`：

```python
@dataclass(frozen=True)
class SchedulerConfig:
    simple_task_threshold: int = 99  # 原值 5，调高后强制单 Agent
```

### 4.3 关闭 Agent 推理

Agent 推理（LLM Reasoning）是可选特性，关闭后 agent 使用启发式规则：

```env
LLM_AGENT_REASONING_ENABLED=0
```

---

## 5. 向量检索降级

### 5.1 后端层级

```
Milvus > FAISS > pgvector > HNSW > SQLite ANN（自动兜底）
```

### 5.2 使用 SQLite ANN（零外部依赖）

```env
VECTOR_BACKEND=sqlite_ann
```

这是最低依赖的方案，不需要安装任何额外的向量数据库。

### 5.3 关闭 Reranker

Reranker 需要 `sentence-transformers` 库，首次使用会下载模型。学习场景可关闭：

```env
RETRIEVAL_RERANKER_ENABLED=0
```

### 5.4 关闭 Embedding（使用哈希向量）

如果不想调用 Embedding API，可以关闭它，系统会自动使用本地哈希向量兜底：

```env
EMBEDDINGS_ENABLED=0
```

> ⚠️ 关闭 embedding 后检索精度会下降，但不影响系统可运行性。

---

## 6. 功能开关式渐进简化

以下 `.env` 开关可独立控制各个模块：

```env
# ===== 核心模块 =====
LLM_AGENT_REASONING_ENABLED=0      # 关闭 Agent LLM 推理（用启发式）

# ===== 检索模块 =====
EMBEDDINGS_ENABLED=0               # 关闭向量 embedding
RETRIEVAL_RERANKER_ENABLED=0       # 关闭 reranker
VECTOR_BACKEND=sqlite_ann          # 使用 SQLite ANN
RETRIEVAL_ANN_ENABLED=1            # 保留本地 ANN

# ===== 自学习 =====
AUTO_DREAM_ON_SUCCESSFUL_REPAIR=0  # 关闭成功后的自动学习
AUTO_DREAM_ASYNC_ENABLED=0         # 关闭异步学习队列
AUTO_DREAM_EMBEDDED_WORKERS_ENABLED=0  # 关闭嵌入式学习 worker

# ===== 代码分析 =====
TREE_SITTER_ENABLED=0              # 关闭 Tree-sitter（只用 AST/正则）
DATA_FLOW_ANALYSIS_ENABLED=0       # 关闭数据流分析
CONTROL_FLOW_ANALYSIS_ENABLED=0    # 关闭控制流分析

# ===== 安全模块 =====
REQUIRE_GITHUB_WRITE_APPROVAL=0    # 关闭写操作审批
REQUIRE_REMOTE_EXECUTION_APPROVAL=0 # 关闭远程执行审批
```

### 推荐的难度等级组合

#### 🟢 入门级（只体验核心流程）

```env
AGENT_RUNTIME_BACKEND=thread
LLM_AGENT_REASONING_ENABLED=0
EMBEDDINGS_ENABLED=0
RETRIEVAL_RERANKER_ENABLED=0
AUTO_DREAM_ON_SUCCESSFUL_REPAIR=0
TREE_SITTER_ENABLED=0
DATA_FLOW_ANALYSIS_ENABLED=0
CONTROL_FLOW_ANALYSIS_ENABLED=0
SANDBOX_BACKEND=workspace
VECTOR_BACKEND=sqlite_ann
```

#### 🟡 进阶级（保留检索和分析）

```env
AGENT_RUNTIME_BACKEND=process
LLM_AGENT_REASONING_ENABLED=1
EMBEDDINGS_ENABLED=1
RETRIEVAL_RERANKER_ENABLED=0
AUTO_DREAM_ON_SUCCESSFUL_REPAIR=1
TREE_SITTER_ENABLED=1
SANDBOX_BACKEND=workspace
VECTOR_BACKEND=sqlite_ann
```

#### 🔴 完整级（和生产一致，保留所有功能）

```env
AGENT_RUNTIME_BACKEND=process
LLM_AGENT_REASONING_ENABLED=1
EMBEDDINGS_ENABLED=1
RETRIEVAL_RERANKER_ENABLED=1
AUTO_DREAM_ON_SUCCESSFUL_REPAIR=1
TREE_SITTER_ENABLED=1
DATA_FLOW_ANALYSIS_ENABLED=1
CONTROL_FLOW_ANALYSIS_ENABLED=1
SANDBOX_BACKEND=workspace
VECTOR_BACKEND=sqlite_ann
```

---

## 7. 配置最小化

### 7.1 最简 `.env`（入门级可用）

```env
# 仅需配置 LLM 连接
LLM_PROVIDER=openai
LLM_API_KEY=your-api-key
LLM_MODEL=your-model
LLM_BASE_URL=https://api.openai.com/v1

# 其他全部使用默认值，无需配置
```

### 7.2 默认行为

未配置的变量会自动使用以下默认值：

| 变量 | 默认值 | 行为 |
|------|--------|------|
| `VECTOR_BACKEND` | `auto` → `sqlite_ann` | 自动选择最小依赖方案 |
| `AGENT_RUNTIME_BACKEND` | `process` | 使用进程池 |
| `SANDBOX_BACKEND` | `workspace` | 本地文件副本隔离 |
| `EMBEDDINGS_ENABLED` | `1` | 使用 embedding |
| `RETRIEVAL_RERANKER_ENABLED` | `1` | 但如果没装依赖会自动跳过 |

---

## 8. API 端点精简

### 8.1 核心端点（始终保留）

```
GET  /api/health                      # 健康检查
GET  /api/dashboard/overview          # Dashboard 数据
GET  /api/repos                       # 仓库列表
POST /api/repos                       # 创建仓库
GET  /api/repos/{id}                  # 仓库详情
PUT  /api/repos/{id}                  # 更新仓库
DELETE /api/repos/{id}                # 删除仓库
GET  /api/incidents/{id}/agent-workflow         # Agent 工作流数据
GET  /api/incidents/{id}/agent-workflow/stream  # Agent 工作流 SSE（核心亮点）
GET  /api/skills                      # 技能列表
POST /api/skills                      # 创建技能
GET  /api/skills/{id}                 # 技能详情
PUT  /api/skills/{id}                 # 更新技能
```

### 8.2 学习场景可跳过的端点

以下端点依赖外部服务或高级功能，学习场景可以不使用：

```
POST /api/ci/sync                      # CI 同步 — 依赖外部平台
POST /api/ci/rerun                    # CI 重跑 — 依赖外部平台
POST /api/repairs/{trace_id}/remote-delivery  # 远程交付 — 需要仓库权限
GET  /api/learning/status             # 自学习状态 — 高级功能
GET  /api/learning/jobs               # 学习任务 — 高级功能
```

> 💡 这些端点在后端代码中保留，只需要前端不调用即可。无需修改后端代码。

---

## 9. 前端页面精简

### 9.1 完整的 8 个页面

| 路由 | 页面 | 复杂度 |
|------|------|--------|
| `/dashboard` | 系统总览 | 🟢 低 |
| `/ci-autofix` | Agent 工作区 | 🔴 高 |
| `/repo-management` | 仓库管理 | 🟡 中 |
| `/repo-management/detail/:id` | 仓库详情 | 🟡 中 |
| `/task-scheduling` | 任务调度 | 🟡 中 |
| `/skill-library` | Skill 技能库 | 🟢 低 |
| `/context-management` | 上下文管理 | 🟡 中 |
| `/observability` | 可观测性 | 🟡 中 |

### 9.2 入门级保留 4 个页面

在 `frontend/src/router/index.ts` 中注释掉复杂页面：

```typescript
const routes: RouteRecordRaw[] = [
  { path: "/", redirect: "/dashboard" },
  { path: "/dashboard", component: () => import("@/views/DashboardView.vue") },
  { path: "/ci-autofix", component: () => import("@/views/CiAutofixView.vue") },
  // 以下页面入门阶段可注释掉：
  // { path: "/repo-management", ... },
  // { path: "/task-scheduling", ... },
  { path: "/skill-library", component: () => import("@/views/SkillLibraryView.vue") },
  // { path: "/context-management", ... },
  { path: "/observability", component: () => import("@/views/ObservabilityView.vue") },
];
```

### 9.3 侧边导航同步精简

在 `ConsoleLayout.vue` 中同步隐藏对应的导航菜单项。

---

## 10. Demo 数据准备

### 10.1 内置示例数据

项目 `backend/examples/` 目录下已包含：

```
examples/
├── incidents/
│   ├── ci_failure.json          # 一个示例异常
│   ├── dependency_conflict.json  # 一个依赖冲突示例
│   └── test_regression.json      # 一个测试回归示例
└── sample_repo/                  # 用于测试的小型示例仓库
    ├── app/
    │   ├── __init__.py
    │   ├── repository.py
    │   └── service.py
    └── tests/
        └── test_service.py
```

### 10.2 自动启动 Demo

在 `backend/.env` 中设置：

```env
API_BOOTSTRAP_DEMO=1
```

启动后端时会自动加载示例数据并执行一轮分析，无需手动提交。

### 10.3 手动触发 Demo

```bash
cd backend
python main.py demo
```

这条命令会：
1. 加载 `examples/incidents/` 下的所有异常
2. 对每个异常执行分析流程
3. 展示 Agent 协作链路
4. 生成执行报告到 `runtime/reports/`

---

## 附录 A：快速诊断命令

```bash
# 检查当前运行的模块状态
curl http://localhost:8000/api/health

# 查看已加载的技能
cd backend && python main.py skills

# 查看指标摘要
cd backend && python main.py metrics

# 执行一次 Dream+Learn 循环
cd backend && python main.py dream
```

## 附录 B：常见问题

**Q: 关闭 embedding 后为什么还能检索？**
A: 系统内置了哈希向量兜底方案，使用关键词的文本哈希作为伪向量，精度较低但保证可用。

**Q: 单 Agent 和多 Agent 结果一样吗？**
A: 单 Agent 模式使用 Generalist 角色一次性完成诊断+修复+验证，适合简单任务。多 Agent 辩论模式适合复杂跨模块问题，结果更可靠但更耗时。

**Q: 如何完全重置数据？**
A: 删除 `backend/runtime/` 目录即可清除所有运行数据、技能库和追踪记录。

---

> 📚 更多架构设计细节，请参阅 [架构文档](../backend/docs/architecture.md)。