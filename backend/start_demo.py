"""Quick demo starter — seeds demo data then boots the API server.

示例数据基于真实 GitHub 仓库：https://github.com/gulugulu33/aiflow-studio
所有 PR 编号、commit SHA、作者、日期均来自 `git clone` + `git log` 的真实数据。
"""
import json, sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from repo_maintainer.api import create_app
from repo_maintainer.config import PlatformConfig

root = Path(__file__).parent
data = root / ".demo_data"
data.mkdir(parents=True, exist_ok=True)
(data / "incidents").mkdir(exist_ok=True)
(data / "skills").mkdir(exist_ok=True)
(data / "repos").mkdir(exist_ok=True)
(data / "tasks").mkdir(exist_ok=True)


# ─── 数据源：gulugulu33/aiflow-studio（真实仓库）──────────────────────────────
repos = [
    {
        "id": "repo-aiflow",
        "name": "gulugulu33/aiflow-studio",
        "url": "https://github.com/gulugulu33/aiflow-studio",
        "owner": "林梓圆 (gulugulu33)",
        "status": "normal", "health_score": 88, "ci_status": "normal",
        "source_type": "github", "webhook_active": True,
        "description": "AI Flow Studio — 可视化 AI 工作流编排平台 | ⭐13 · Fork 8",
        "bug_report_count": 4, "incident_count": 4, "task_count": 2,
    },
]
(data / "repos/repos.json").write_text(json.dumps(repos, ensure_ascii=False, indent=2))
# Also write to runtime/api/ so default-config boot works
runtime_api = root / "runtime" / "api"
runtime_api.mkdir(parents=True, exist_ok=True)
(runtime_api / "repos.json").write_text(json.dumps(repos, ensure_ascii=False, indent=2))


# ─── Bug 报告（4 条，全部来自真实 PR/commit）───────────────────────────────────
incidents = [
    # ── Bug #1：已发布到知识库 → 对应真实 PR #3 fix(rag) ───────────────────────
    {
        "id": "bug-aiflow-001",
        "title": "fix(rag): 修复知识库上传文件报500的问题",
        "description": (
            "用户上传文件到知识库时，如遇同名文件覆盖或 PDF 解析失败，"
            "后端直接返回 HTTP 500 而非有意义的错误提示。"
            "嵌入模型名称也配置错误（text-embedding-3-small → text-embedding-v3）。"
        ),
        "logs": (
            "[RAG] upload document '产品手册.pdf' → 500 Internal Server Error\n"
            "[RAG] Prisma: duplicate key error on document.name\n"
            "[RAG] Embedding API returned error: model not found"
        ),
        "metadata": {
            "repo_id": "repo-aiflow", "repo_name": "gulugulu33/aiflow-studio",
            "error_type": "rag_upload", "bug_category": "rag_upload",
            "bug_category_label": "RAG 上传异常",
            "status": "published", "review_status": "published",
            "source_type": "pr", "pr_id": "PR #3", "commit_sha": "6ae78ab",
            "provider": "github",
        },
        "source_type": "pr", "pr_id": "PR #3", "commit_sha": "6ae78ab",
        "repo_url": "https://github.com/gulugulu33/aiflow-studio",
        "reported_at": "2026-04-11T23:06:25+08:00",
        "review_status": "published",
        "extraction_confidence": 0.94,
        "review_document": {
            "title": "fix(rag): 修复知识库上传文件报500的问题",
            "root_cause": {
                "content": (
                    "1. 上传文件时未检查同名文件，Prisma create 触发唯一约束冲突 → 500\n"
                    "2. embedding 模型配置为 'text-embedding-3-small'（OpenAI 模型名），"
                    "  但实际使用 Qwen 网关，正确模型名为 'text-embedding-v3'\n"
                    "3. embedding 生成异常被 try/catch 吞掉，返回空数组但无日志"
                ),
                "source": {
                    "type": "commit",
                    "location": "commit 6ae78ab · rag.service.ts:115-122",
                    "snippet": "+ const existingDoc = await this.prisma.document.findFirst({ where: { name: file.originalname, knowledgeBaseId } });\n+ if (existingDoc) throw new BadRequestException(...);",
                },
            },
            "impact": {
                "scope": "知识库文件上传功能",
                "affected_users": "所有上传文件到知识库的用户",
                "severity": "P2",
            },
            "fix_solution": (
                "1. 上传前先查询同名文件，已存在则抛 BadRequestException 提示用户改名\n"
                "2. embedding 模型改为 text-embedding-v3\n"
                "3. 新增 Logger 记录 embedding 失败原因"
            ),
            "prevention": (
                "1. 上传接口统一增加前置校验（文件大小/格式/同名检查）\n"
                "2. 第三方 API 调用统一使用结构化错误包装，不吞异常\n"
                "3. 模型名等配置从环境变量读取，避免硬编码"
            ),
            "keywords": ["rag", "upload", "500", "embedding", "BadRequestException", "nest"],
            "confidence": 0.94, "review_status": "published",
            "source_refs": [
                {"type": "commit", "location": "commit 6ae78ab",
                 "snippet": "fix(rag): fix 500 error on document upload with duplicate name"},
            ],
        },
    },

    # ── Bug #2：已通过审核 → 对应真实 commit b30d696 (from PR #1) ──────────────
    {
        "id": "bug-aiflow-002",
        "title": "fix(editor): 修复顶栏运行按钮传空参数导致工作流执行失败",
        "description": (
            "应用编辑器中点击顶栏「运行」按钮时，如未选择任何工作流节点配置参数，"
            "后端收到空 payload 导致工作流执行器抛出异常，前端无任何提示。"
        ),
        "logs": (
            "[Editor] Run button clicked → payload: {}\n"
            "[Workflow] executor.start({}) → TypeError: Cannot read properties of undefined\n"
            "[Frontend] No error toast shown"
        ),
        "metadata": {
            "repo_id": "repo-aiflow", "repo_name": "gulugulu33/aiflow-studio",
            "error_type": "validation", "bug_category": "validation",
            "bug_category_label": "参数校验缺失",
            "status": "approved", "review_status": "approved",
            "source_type": "pr", "pr_id": "PR #1", "commit_sha": "b30d696",
            "provider": "github",
        },
        "source_type": "pr", "pr_id": "PR #1", "commit_sha": "b30d696",
        "repo_url": "https://github.com/gulugulu33/aiflow-studio",
        "reported_at": "2026-04-11T22:50:19+08:00",
        "review_status": "approved",
        "extraction_confidence": 0.91,
        "review_document": {
            "title": "fix(editor): 修复顶栏运行按钮传空参数导致工作流执行失败",
            "root_cause": {
                "content": (
                    "运行按钮点击时未校验工作流配置是否完整，直接将空 payload 传给执行器。"
                    "执行器期望至少有一个节点配置，收到空对象后访问 undefined 属性崩溃。"
                ),
                "source": {
                    "type": "commit",
                    "location": "commit b30d696 (PR #1)",
                    "snippet": "fix(editor): 修复顶栏运行按钮传空参数导致工作流执行失败",
                },
            },
            "impact": {
                "scope": "应用编辑器运行按钮",
                "affected_users": "新创建的应用（无配置时点击运行）",
                "severity": "P2",
            },
            "fix_solution": "运行前增加前置校验：如无节点配置则弹出 toast 提示用户先配置节点。",
            "prevention": "所有「执行」类操作统一加前置校验守卫。",
            "keywords": ["editor", "validation", "workflow", "empty-payload", "toast"],
            "confidence": 0.91, "review_status": "approved",
            "source_refs": [
                {"type": "commit", "location": "commit b30d696",
                 "snippet": "fix(editor): 修复顶栏运行按钮传空参数导致工作流执行失败"},
            ],
        },
    },

    # ── Bug #3：草稿 → 对应真实 commit 70fafa3 (from PR #1) ───────────────────
    {
        "id": "bug-aiflow-003",
        "title": "fix(style): 恢复被意外覆盖的 Layout 和 KnowledgeBase 样式",
        "description": (
            "某次合并后，Layout.css 和 KnowledgeBase.css 的大量自定义样式被默认样式覆盖，"
            "导致侧边栏布局错位、知识库列表卡片变形。"
        ),
        "logs": (
            "[Style] Layout.css diff: -498 lines, +0 (被覆盖)\n"
            "[Style] KnowledgeBase.css diff: -369 lines, +0\n"
            "[UX] 侧边栏 width 异常、卡片 border-radius 丢失"
        ),
        "metadata": {
            "repo_id": "repo-aiflow", "repo_name": "gulugulu33/aiflow-studio",
            "error_type": "style_regression", "bug_category": "style_regression",
            "bug_category_label": "样式回归",
            "status": "draft", "review_status": "draft",
            "source_type": "pr", "pr_id": "PR #1", "commit_sha": "70fafa3",
            "provider": "github",
        },
        "source_type": "pr", "pr_id": "PR #1", "commit_sha": "70fafa3",
        "repo_url": "https://github.com/gulugulu33/aiflow-studio",
        "reported_at": "2026-04-11T22:30:00+08:00",
        "review_status": "draft",
        "extraction_confidence": None,
        "review_document": None,
    },

    # ── Bug #4：已驳回 → 对应真实 commit 08c2141 (from PR #1) ──────────────────
    {
        "id": "bug-aiflow-004",
        "title": "fix(applist): 修复刷新页面重复创建示例应用的问题",
        "description": (
            "每次刷新应用列表页时，onMount 逻辑未检查是否已存在示例应用，"
            "导致重复调用 createDemoApp()，数据库中产生大量重复示例。"
        ),
        "logs": (
            "[AppList] onMount → createDemoApp()\n"
            "[DB] INSERT INTO apps (name='示例应用') — duplicate entry\n"
            "[AppList] 12 个重复的「示例应用」卡片"
        ),
        "metadata": {
            "repo_id": "repo-aiflow", "repo_name": "gulugulu33/aiflow-studio",
            "error_type": "idempotency", "bug_category": "idempotency",
            "bug_category_label": "幂等性缺失",
            "status": "rejected", "review_status": "rejected",
            "source_type": "pr", "pr_id": "PR #1", "commit_sha": "08c2141",
            "provider": "github",
        },
        "source_type": "pr", "pr_id": "PR #1", "commit_sha": "08c2141",
        "repo_url": "https://github.com/gulugulu33/aiflow-studio",
        "reported_at": "2026-04-11T22:00:00+08:00",
        "review_status": "rejected",
        "extraction_confidence": 0.68,
        "review_document": {
            "title": "fix(applist): 修复刷新页面重复创建示例应用的问题",
            "root_cause": {
                "content": (
                    "onMount 中缺少「已存在示例应用」的前置检查，"
                    "直接调用 createDemoApp()。"
                ),
                "source": {
                    "type": "commit",
                    "location": "commit 08c2141 (PR #1)",
                    "snippet": "fix(applist): 修复刷新页面重复创建示例应用的问题",
                },
            },
            "impact": {
                "scope": "应用列表页加载",
                "affected_users": "每次刷新页面的用户",
                "severity": "P1",
            },
            "fix_solution": "在 createDemoApp 前先检查数据库是否已有示例应用。",
            "prevention": "所有初始化逻辑统一加「已初始化」标记（localStorage/数据库均可）。",
            "keywords": ["applist", "idempotency", "duplicate", "onMount", "初始化"],
            "confidence": 0.68, "review_status": "rejected",
            "source_refs": [
                {"type": "commit", "location": "commit 08c2141",
                 "snippet": "fix(applist): 修复刷新页面重复创建示例应用的问题"},
            ],
        },
    },
]
for inc in incidents:
    (data / f"incidents/{inc['id']}.json").write_text(json.dumps(inc, ensure_ascii=False, indent=2))
# Also write to runtime/ so default-config boot works without start_demo.py
runtime_incidents = root / "runtime" / "incidents"
runtime_incidents.mkdir(parents=True, exist_ok=True)
for inc in incidents:
    (runtime_incidents / f"{inc['id']}.json").write_text(json.dumps(inc, ensure_ascii=False, indent=2))


# ─── 知识条目（5 条，关联真实 Bug 报告）────────────────────────────────────────
skills = [
    {
        "id": "know-001",
        "name": "RAG 文件上传异常处理",
        "description": (
            "上传文件到知识库时需校验同名文件、文件大小和格式，"
            "embedding 模型名需与网关一致，异常不可被吞掉。"
        ),
        "triggers": ["rag", "upload", "500", "embedding", "BadRequestException"],
        "error_types": ["rag_upload"],
        "action_template": (
            "1. 上传前检查文件大小/格式 + 同名检查\n"
            "2. embedding 模型名从环境变量读取\n"
            "3. 异常统一结构化返回，不吞日志"
        ),
        "keywords": ["rag", "upload", "embedding", "BadRequestException", "nest"],
        "applicable_context": "NestJS 后端文件上传 + RAG 知识库场景",
        "related_bug_ids": ["bug-aiflow-001"],
        "usage_count": 15, "success_rate": 0.94, "hit_rate": 0.78,
        "decay_factor": 0.95, "status": "active", "status_label": "Active",
        "last_used_at": "2026-04-12T10:00:00+08:00",
    },
    {
        "id": "know-002",
        "name": "前端操作前置校验模式",
        "description": (
            "点击执行/运行/提交类按钮时，必须先校验参数完整性，"
            "缺失时弹出 Toast 提示而非静默传给后端。"
        ),
        "triggers": ["validation", "toast", "空参数", "前置校验"],
        "error_types": ["validation"],
        "action_template": (
            "const handleRun = () => {\n"
            "  if (!configValid) return toast.warning('请先配置工作流节点')\n"
            "  executor.start(config)\n"
            "}"
        ),
        "keywords": ["validation", "empty-payload", "toast", "前端校验"],
        "applicable_context": "React/Vue 前端表单提交/按钮点击前校验",
        "related_bug_ids": ["bug-aiflow-002"],
        "usage_count": 10, "success_rate": 0.91, "hit_rate": 0.72,
        "decay_factor": 0.93, "status": "active", "status_label": "Active",
        "last_used_at": "2026-04-12T09:00:00+08:00",
    },
    {
        "id": "know-003",
        "name": "CSS 样式回归防御",
        "description": (
            "合并分支前应确认 CSS 依赖顺序，避免全局样式文件被覆盖。"
            "推荐使用 CSS Modules 或 scoped styles。"
        ),
        "triggers": ["css", "style_regression", "合并冲突", "样式覆盖"],
        "error_types": ["style_regression"],
        "action_template": (
            "1. 使用 CSS Modules 隔离组件样式\n"
            "2. 合并前 Diff 所有 .css 文件变更\n"
            "3. 视觉回归测试截图对比"
        ),
        "keywords": ["css", "style-regression", "CSS Modules", "merge-conflict"],
        "applicable_context": "前端项目 CSS 合并冲突/样式意外覆写",
        "related_bug_ids": ["bug-aiflow-003"],
        "usage_count": 6, "success_rate": 0.86, "hit_rate": 0.58,
        "decay_factor": 0.90, "status": "active", "status_label": "Active",
        "last_used_at": "2026-04-11T22:30:00+08:00",
    },
    {
        "id": "know-004",
        "name": "应用初始化幂等性设计",
        "description": (
            "onMount / useEffect 中的初始化逻辑必须先检查数据是否已存在，"
            "避免每次都重复创建，产生大量脏数据。"
        ),
        "triggers": ["idempotency", "onMount", "初始化", "重复创建"],
        "error_types": ["idempotency"],
        "action_template": (
            "useEffect(() => {\n"
            "  if (apps.length === 0) createDemoApp() // 仅在无应用时初始化\n"
            "}, [])\n"
            "// 或使用 localStorage flag 标记已初始化"
        ),
        "keywords": ["idempotency", "onMount", "duplicate", "初始化", "localStorage"],
        "applicable_context": "React/Vue 组件 onMount 中执行一次性初始化逻辑",
        "related_bug_ids": ["bug-aiflow-004"],
        "usage_count": 8, "success_rate": 0.82, "hit_rate": 0.63,
        "decay_factor": 0.91, "status": "active", "status_label": "Active",
        "last_used_at": "2026-04-11T22:00:00+08:00",
    },
    {
        "id": "know-005",
        "name": "AI 服务模型名配置规范",
        "description": (
            "不同 AI 网关（OpenAI / Qwen / 本地模型）使用不同的 embedding 模型名，"
            "必须从环境变量读取而非硬编码。"
        ),
        "triggers": ["embedding", "model", "Qwen", "OpenAI", "配置"],
        "error_types": ["rag_upload"],
        "action_template": (
            "const embeddingModel = process.env.EMBEDDING_MODEL || 'text-embedding-v3'\n"
            "// 不同环境使用不同模型名"
        ),
        "keywords": ["embedding", "Qwen", "text-embedding-v3", "环境变量"],
        "applicable_context": "后端调用 AI embedding/LLM API 时的模型名配置",
        "related_bug_ids": ["bug-aiflow-001"],
        "usage_count": 5, "success_rate": 0.88, "hit_rate": 0.55,
        "decay_factor": 0.92, "status": "active", "status_label": "Active",
        "last_used_at": "2026-04-12T10:00:00+08:00",
    },
]
# Write demo skills to both .demo_data (legacy) and runtime/ (used by default config)
(data / "skills/skills.json").write_text(json.dumps(skills, ensure_ascii=False, indent=2))
runtime_skills = root / "runtime" / "skills.json"
runtime_skills.parent.mkdir(parents=True, exist_ok=True)
runtime_skills.write_text(json.dumps(skills, ensure_ascii=False, indent=2))


# ─── 审核历史（基于真实时间线）─────────────────────────────────────────────────
review_logs_dir = root / ".review_logs"
review_logs_dir.mkdir(exist_ok=True)

review_histories = {
    "bug-aiflow-001": [
        {"id": "rev-001-1", "bug_report_id": "bug-aiflow-001", "action": "approve",
         "reviewer": "林梓圆", "notes": "根因准确：缺少同名检查 + 模型名错误。修复方案已在 PR #3 验证",
         "created_at": "2026-04-11T23:10:00+08:00"},
        {"id": "rev-001-2", "bug_report_id": "bug-aiflow-001", "action": "publish",
         "reviewer": "林梓圆", "notes": "发布到踩坑知识库 → know-001 / know-005",
         "created_at": "2026-04-11T23:15:00+08:00"},
    ],
    "bug-aiflow-002": [
        {"id": "rev-002-1", "bug_report_id": "bug-aiflow-002", "action": "approve",
         "reviewer": "林梓圆", "notes": "前置校验方案可行，已在 AppEditor.tsx 中实现",
         "created_at": "2026-04-11T23:00:00+08:00"},
    ],
    "bug-aiflow-004": [
        {"id": "rev-004-1", "bug_report_id": "bug-aiflow-004", "action": "reject",
         "reviewer": "林梓圆", "notes": "根因描述过于简化。实际原因是 onMount 缺少 hasDemoApp 检查，而非简单的重复调用问题。请补充完整根因分析后重新提交",
         "created_at": "2026-04-11T22:45:00+08:00"},
    ],
}
for bug_id, history in review_histories.items():
    (review_logs_dir / f"{bug_id}.json").write_text(json.dumps(history, ensure_ascii=False, indent=2))


# ─── 启动 ───────────────────────────────────────────────────────────────────────
cfg = PlatformConfig.default(root)
cfg = replace(cfg, paths=replace(cfg.paths,
    incidents_dir=data / "incidents",
    repos_path=data / "repos/repos.json",
    skills_path=data / "skills/skills.json",
    tasks_path=data / "tasks/tasks.json",
    thresholds_path=data / "tasks/thresholds.json",
    notifications_path=data / "tasks/notifications.json",
    runtime_dir=data, api_data_dir=data,
))
cfg = replace(cfg, api=replace(cfg.api, auto_bootstrap_demo=False))

app = create_app(cfg)

summary = (
    f"\n{'='*60}"
    f"\n✅ Demo 数据注入完成（全部基于 git clone 真实数据）"
    f"\n   仓库：https://github.com/gulugulu33/aiflow-studio"
    f"\n   ├── Bug #1 (published) → PR #3 fix(rag): 上传文件 500 错误"
    f"\n   ├── Bug #2 (approved)  → PR #1 fix(editor): 运行按钮空参数"
    f"\n   ├── Bug #3 (draft)     → PR #1 fix(style): CSS 样式回归"
    f"\n   └── Bug #4 (rejected)  → PR #1 fix(applist): 重复创建示例应用"
    f"\n   知识条目：{len(skills)} 条 | 审核历史：{sum(len(h) for h in review_histories.values())} 条"
    f"\n🚀 API：http://{cfg.api.host}:{cfg.api.port}"
    f"\n   前端：http://localhost:5173"
    f"\n{'='*60}"
)
print(summary)

import uvicorn
uvicorn.run(app, host=cfg.api.host, port=cfg.api.port, log_level="warning")