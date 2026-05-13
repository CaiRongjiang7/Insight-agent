"""
多 Agent 协作系统（重映射为 Bug 复盘场景）

Agent 角色对照：
- Architect / DIAGNOSE  → EventClassifier：判断事件是否为 Bug 修复相关
- Repairer / FIX        → InfoExtractor：从原始数据抽取结构化字段
- Tester / VALIDATE     → ContextEnricher：RAG 召回历史相似案例
- Reviewer / CRITIC     → ReviewAgent：生成审核摘要 + 风险标记
- Coordinator / REFLECT → PipelineOrchestrator：协调全流程
- Critic / REBUTTAL     → QualityScorer：评估 LLM 输出可信度
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .agent_reasoning import AgentReasoningError, LLMReasoningClient
from .models import (
    AgentExecution, AgentRole, AgentTask, BugReport, CompressedError,
    ContextBundle, ExtractionStep, IncidentReport, RepairStep,
)

if TYPE_CHECKING:
    from .agent_tools import AgentToolExecutor


class MaintainerAgent:
    """
    维护者 Agent（重映射后支持 Bug 复盘场景的所有角色）

    新角色映射：
    - EVENT_CLASSIFIER  → 判断是否为 Bug 相关事件
    - INFO_EXTRACTOR    → LLM 结构化抽取核心字段
    - CONTEXT_ENRICHER  → RAG 召回 + 历史案例关联
    - REVIEW_AGENT      → 审核摘要生成 + 风险标记
    - PIPELINE_ORCHESTRATOR → 全流程协调
    - QUALITY_SCORER    → 输出可信度评估
    """

    def __init__(self, role: AgentRole, reasoning_settings: dict | None = None) -> None:
        self.role = role
        self.reasoning_client = LLMReasoningClient(reasoning_settings)

    def execute(
        self,
        task: AgentTask,
        incident: BugReport | IncidentReport,
        compressed_error: CompressedError,
        context: ContextBundle,
        tool_executor: "AgentToolExecutor | None" = None,
    ) -> AgentExecution:
        tool_results = tool_executor.run_default_plan() if tool_executor is not None else []
        fallback_steps = self._fallback_steps(incident, compressed_error, context)
        if self.reasoning_client.enabled():
            try:
                llm_execution = self.reasoning_client.reason(task, incident, compressed_error, context, fallback_steps, tool_executor=tool_executor, initial_tool_results=tool_results)
                if task.role in {AgentRole.CRITIC, AgentRole.REBUTTAL, AgentRole.REFLECT}:
                    llm_execution.reasoning_summary = f"Round {task.negotiation_round}: {llm_execution.reasoning_summary}"
                llm_execution.metadata.setdefault("tool_results", tool_results)
                llm_execution.metadata["executed_tool_count"] = len(llm_execution.metadata.get("tool_results", []))
                return self._augment_protocol_metadata(llm_execution, incident, compressed_error, context)
            except AgentReasoningError:
                pass

        # 新角色处理（Bug 复盘场景）
        if self.role == AgentRole.EVENT_CLASSIFIER:
            summary = self._event_classifier_summary(incident, compressed_error, context)
            steps = fallback_steps
            likelihood = min(0.60 + (0.04 * len(context.relevant_files)), 0.85)
        elif self.role == AgentRole.INFO_EXTRACTOR:
            summary = self._info_extractor_summary(incident, compressed_error, context)
            steps = fallback_steps
            likelihood = min(0.55 + 0.08 * len(steps), 0.9)
        elif self.role == AgentRole.CONTEXT_ENRICHER:
            summary = self._context_enricher_summary(incident, compressed_error, context)
            steps = fallback_steps
            likelihood = 0.78
        elif self.role == AgentRole.REVIEW_AGENT:
            summary = self._review_agent_summary(incident, compressed_error, context)
            steps = fallback_steps
            likelihood = 0.74
        elif self.role == AgentRole.PIPELINE_ORCHESTRATOR:
            summary = self._reflection_summary(incident, compressed_error, context)
            steps = fallback_steps
            likelihood = 0.80
        elif self.role == AgentRole.QUALITY_SCORER:
            summary = self._quality_scorer_summary(incident, compressed_error, context)
            steps = fallback_steps
            likelihood = 0.77
        # 旧角色向后兼容
        elif self.role == AgentRole.DIAGNOSE:
            summary = self._diagnosis_summary(incident, compressed_error, context)
            steps = fallback_steps
            likelihood = min(0.55 + (0.05 * len(context.relevant_files)), 0.82)
        elif self.role == AgentRole.FIX:
            summary = self._fix_summary(incident, compressed_error, context)
            steps = fallback_steps
            likelihood = min(0.55 + 0.08 * len(steps), 0.9)
        elif self.role == AgentRole.VALIDATE:
            summary = self._validation_summary(incident, compressed_error, context)
            steps = fallback_steps
            likelihood = 0.78
        elif self.role == AgentRole.CRITIC:
            summary = self._critic_summary(incident, compressed_error, context)
            steps = fallback_steps
            likelihood = 0.74
        elif self.role == AgentRole.REBUTTAL:
            summary = self._rebuttal_summary(incident, compressed_error, context)
            steps = fallback_steps
            likelihood = 0.77
        elif self.role == AgentRole.REFLECT:
            summary = self._reflection_summary(incident, compressed_error, context)
            steps = fallback_steps
            likelihood = 0.8
        else:
            summary = "\n".join(
                [
                    self._diagnosis_summary(incident, compressed_error, context),
                    self._fix_summary(incident, compressed_error, context),
                    self._validation_summary(incident, compressed_error, context),
                ]
            )
            steps = self._repair_steps(incident, compressed_error, context) + self._validation_steps(context)
            likelihood = min(0.60 + 0.05 * len(steps), 0.9)

        tool_evidence = self._tool_evidence_summary(tool_results)
        if tool_evidence:
            summary = f"{summary} Tool evidence: {tool_evidence}."
        token_estimate = max(len(summary) // 4, 1) + sum(len(step.description) // 4 for step in steps)
        if task.role in {AgentRole.CRITIC, AgentRole.REBUTTAL, AgentRole.REFLECT}:
            summary = f"Round {task.negotiation_round}: {summary}"
        execution = AgentExecution(
            task_id=task.id,
            role=self.role,
            reasoning_summary=summary,
            repair_steps=steps,
            token_estimate=min(token_estimate, task.budget),
            success_likelihood=likelihood,
            metadata={
                "reasoning_source": "heuristic",
                "tool_results": tool_results,
                "executed_tool_count": len(tool_results),
            },
        )
        return self._augment_protocol_metadata(execution, incident, compressed_error, context)

    def _fallback_steps(
        self,
        incident: BugReport | IncidentReport,
        compressed_error: CompressedError,
        context: ContextBundle,
    ) -> list[ExtractionStep]:
        # 新角色 fallback steps
        if self.role == AgentRole.EVENT_CLASSIFIER:
            return [
                ExtractionStep(
                    title="判断事件是否为 Bug 修复相关",
                    description="分析事件标题、描述和日志，判断是否包含 Bug 修复意图（PR merge、hotfix、revert 等）。",
                    target_files=context.relevant_files[:2],
                    confidence=0.75,
                )
            ]
        if self.role == AgentRole.INFO_EXTRACTOR:
            return self._extraction_steps(incident, compressed_error, context)
        if self.role == AgentRole.CONTEXT_ENRICHER:
            return self._enrichment_steps(context)
        if self.role == AgentRole.REVIEW_AGENT:
            return self._review_steps(context)
        if self.role == AgentRole.PIPELINE_ORCHESTRATOR:
            return self._reflection_steps(context)
        if self.role == AgentRole.QUALITY_SCORER:
            return self._quality_steps(context)
        # 旧角色向后兼容
        if self.role == AgentRole.DIAGNOSE:
            return [
                ExtractionStep(
                    title="Compress root cause",
                    description="Extract the failure signature, narrow the blast radius, and anchor the investigation around the first relevant stack frame and the most related modules.",
                    target_files=context.relevant_files[:3],
                    confidence=0.68,
                )
            ]
        if self.role == AgentRole.FIX:
            return self._repair_steps(incident, compressed_error, context)
        if self.role == AgentRole.VALIDATE:
            return self._validation_steps(context)
        if self.role == AgentRole.CRITIC:
            return self._critic_steps(context)
        if self.role == AgentRole.REBUTTAL:
            return self._rebuttal_steps(context)
        if self.role == AgentRole.REFLECT:
            return self._reflection_steps(context)
        return self._repair_steps(incident, compressed_error, context) + self._validation_steps(context)

    # ===== 新角色摘要方法（Bug 复盘场景）=====

    def _event_classifier_summary(
        self,
        incident: BugReport | IncidentReport,
        compressed_error: CompressedError,
        context: ContextBundle,
    ) -> str:
        """EventClassifier：判断事件是否为 Bug 修复相关"""
        keywords = ", ".join(compressed_error.keywords[:5]) or "无关键词"
        error_type = compressed_error.error_type.value
        return (
            f"事件分类分析：`{incident.title}`。"
            f"错误类型：{error_type}，关键词：{keywords}。"
            f"判断是否包含 Bug 修复意图（hotfix/revert/bugfix），并分类到对应的 BugCategory。"
        )

    def _info_extractor_summary(
        self,
        incident: BugReport | IncidentReport,
        compressed_error: CompressedError,
        context: ContextBundle,
    ) -> str:
        """InfoExtractor：从原始数据抽取结构化字段"""
        files = ", ".join(context.relevant_files[:4]) or "无关联文件"
        skills = ", ".join(s.name for s in context.matched_skills[:2]) or "无匹配知识条目"
        return (
            f"结构化信息抽取：`{incident.title}`。"
            f"语义摘要：{compressed_error.semantic_summary}。"
            f"关联文件：{files}。参考知识条目：{skills}。"
            f"目标：输出符合 BugReviewDocument Schema 的 JSON，包含 root_cause/impact/fix_solution/prevention 字段。"
        )

    def _context_enricher_summary(
        self,
        incident: BugReport | IncidentReport,
        compressed_error: CompressedError,
        context: ContextBundle,
    ) -> str:
        """ContextEnricher：RAG 召回历史相似案例"""
        memories = ", ".join(m["cluster"] for m in context.long_term_memories[:2]) or "无历史案例"
        skills = ", ".join(s.name for s in context.matched_skills[:3]) or "无匹配知识"
        repair_anchor = self._shared_blackboard_summary(context, role="info_extractor")
        return (
            f"上下文增强：`{incident.title}`。"
            f"历史相似案例：{memories}。召回知识条目：{skills}。"
            f"抽取初稿：{repair_anchor}。"
            f"目标：用历史案例补充佐证，提升抽取结果的置信度。"
        )

    def _review_agent_summary(
        self,
        incident: BugReport | IncidentReport,
        compressed_error: CompressedError,
        context: ContextBundle,
    ) -> str:
        """ReviewAgent：生成审核摘要 + 风险标记"""
        extractor_anchor = self._shared_blackboard_summary(context, role="info_extractor")
        enricher_anchor = self._shared_blackboard_summary(context, role="context_enricher")
        return (
            f"审核摘要生成：`{incident.title}`。"
            f"抽取初稿：{extractor_anchor}。"
            f"上下文增强结果：{enricher_anchor}。"
            f"目标：评估抽取质量，标记高风险字段（来源不足/语义不一致），生成人工审核建议。"
        )

    def _quality_scorer_summary(
        self,
        incident: BugReport | IncidentReport,
        compressed_error: CompressedError,
        context: ContextBundle,
    ) -> str:
        """QualityScorer：评估 LLM 输出可信度"""
        review_anchor = self._shared_blackboard_summary(context, role="review_agent")
        return (
            f"质量评估：`{incident.title}`。"
            f"审核摘要：{review_anchor}。"
            f"目标：对抽取结果进行四维评分（字段完整率/语义一致性/历史匹配度/来源覆盖率），输出 overall 综合分数。"
        )

    def _extraction_steps(
        self,
        incident: BugReport | IncidentReport,
        compressed_error: CompressedError,
        context: ContextBundle,
    ) -> list[ExtractionStep]:
        """InfoExtractor 的抽取步骤"""
        target_files = context.relevant_files[:3] or incident.changed_files[:3]
        steps: list[ExtractionStep] = []
        for entry in context.matched_skills[:2]:
            steps.append(
                ExtractionStep(
                    title=f"参考知识条目：{entry.name}",
                    description=entry.action_template,
                    target_files=target_files,
                    confidence=max(0.55, min(0.9, 0.55 + entry.success_rate * 0.3)),
                    source_skill_id=entry.id,
                )
            )
        steps.append(
            ExtractionStep(
                title="抽取根本原因",
                description="从 PR 描述/日志/commit message 中定位直接触发 Bug 的代码变更，绑定原文片段作为来源证据。",
                target_files=target_files,
                confidence=0.72,
            )
        )
        steps.append(
            ExtractionStep(
                title="评估影响范围",
                description="分析受影响的模块、用户群体和严重程度，输出 Impact.scope/affected_users/severity。",
                target_files=target_files,
                confidence=0.70,
            )
        )
        return self._dedupe_steps(steps)

    def _enrichment_steps(self, context: ContextBundle) -> list[ExtractionStep]:
        """ContextEnricher 的上下文增强步骤"""
        target_files = context.relevant_files[:4]
        return [
            ExtractionStep(
                title="RAG 召回历史相似案例",
                description="基于 embedding 向量和 BM25 关键词混合召回最相似的历史 Bug 复盘条目。",
                target_files=target_files,
                confidence=0.78,
            ),
            ExtractionStep(
                title="补充历史案例佐证",
                description="将召回案例的 fix_solution 和 prevention 与当前抽取结果对比，补充缺失的上下文信息。",
                target_files=target_files,
                confidence=0.72,
            ),
        ]

    def _review_steps(self, context: ContextBundle) -> list[ExtractionStep]:
        """ReviewAgent 的审核步骤"""
        blackboard_files = list(self._shared_blackboard(context).get("focus_files", []))[:4]
        target_files = blackboard_files or context.relevant_files[:4]
        return [
            ExtractionStep(
                title="校验来源覆盖率",
                description="检查 root_cause.source.snippet 是否真实出现在原始文本中，标记幻觉风险。",
                target_files=target_files,
                confidence=0.80,
            ),
            ExtractionStep(
                title="生成人工审核建议",
                description="对低置信度字段添加审核备注，标记需要人工确认的关键信息。",
                target_files=target_files,
                confidence=0.75,
            ),
        ]

    def _quality_steps(self, context: ContextBundle) -> list[ExtractionStep]:
        """QualityScorer 的评分步骤"""
        target_files = context.relevant_files[:4]
        return [
            ExtractionStep(
                title="字段完整率评分",
                description="检查 BugReviewDocument 必填字段的完整性，输出 field_completeness 分数。",
                target_files=target_files,
                confidence=0.85,
            ),
            ExtractionStep(
                title="综合质量评分",
                description="对四维质量（字段完整/语义一致/历史匹配/来源覆盖）进行加权评分，输出 overall 分数。",
                target_files=target_files,
                confidence=0.80,
            ),
        ]

    # ===== 旧角色摘要方法（向后兼容）=====

    def _diagnosis_summary(
        self,
        incident: BugReport | IncidentReport,
        compressed_error: CompressedError,
        context: ContextBundle,
    ) -> str:
        frames = ", ".join(compressed_error.key_stack_frames[:2]) or "no stack frames"
        files = ", ".join(context.relevant_files[:4]) or "no relevant files"
        memories = ", ".join(memory["cluster"] for memory in context.long_term_memories[:2]) or "no similar memory"
        return (
            f"Root cause focus: {compressed_error.semantic_summary} "
            f"Relevant files: {files}. Key frames: {frames}. Historical anchors: {memories}."
        )

    def _fix_summary(
        self,
        incident: BugReport | IncidentReport,
        compressed_error: CompressedError,
        context: ContextBundle,
    ) -> str:
        skill_names = ", ".join(skill.name for skill in context.matched_skills[:3]) or "no matched skills"
        diagnose_anchor = self._shared_blackboard_summary(context, role="diagnose")
        return (
            f"Generate repair plan for `{incident.title}` using semantic focus "
            f"{', '.join(context.semantic_focus[:4])} and skills {skill_names}. "
            f"Shared diagnosis: {diagnose_anchor}."
        )

    def _validation_summary(
        self,
        incident: BugReport | IncidentReport,
        compressed_error: CompressedError,
        context: ContextBundle,
    ) -> str:
        repair_anchor = self._shared_blackboard_summary(context, role="fix")
        return (
            f"Validate the fix for `{compressed_error.error_type.value}` with "
            f"targeted tests over {', '.join(context.relevant_files[:4]) or 'affected modules'}. "
            f"Repair proposal anchor: {repair_anchor}."
        )

    def _critic_summary(
        self,
        incident: BugReport | IncidentReport,
        compressed_error: CompressedError,
        context: ContextBundle,
    ) -> str:
        repair_anchor = self._shared_blackboard_summary(context, role="fix")
        validation_anchor = self._shared_blackboard_summary(context, role="validate")
        root_paths = self._root_cause_path_summary(context)
        return (
            f"Debate the current repair for `{incident.title}`. "
            f"Challenge this fix thesis: {repair_anchor}. "
            f"Use validation concerns: {validation_anchor}. "
            f"Cross-file root cause paths: {root_paths}."
        )

    def _reflection_summary(
        self,
        incident: BugReport | IncidentReport,
        compressed_error: CompressedError,
        context: ContextBundle,
    ) -> str:
        blackboard = self._shared_blackboard(context)
        note_count = len(blackboard.get("notes", []))
        risk_count = len(blackboard.get("risks", []))
        debate_rounds = int(blackboard.get("debate_rounds", 0) or 0)
        focus_files = ", ".join(list(blackboard.get("focus_files", []))[:4]) or ", ".join(context.relevant_files[:4]) or "affected modules"
        rebuttal_anchor = self._shared_blackboard_summary(context, role="rebuttal")
        return (
            f"Merge {note_count} prior agent notes for `{incident.title}` into a consensus repair plan. "
            f"Focus files: {focus_files}. Rebuttal resolution: {rebuttal_anchor}. "
            f"Highlight {risk_count} validation or rollback risks across {debate_rounds} debate turns around {compressed_error.root_cause_cluster}."
        )

    def _rebuttal_summary(
        self,
        incident: BugReport | IncidentReport,
        compressed_error: CompressedError,
        context: ContextBundle,
    ) -> str:
        repair_anchor = self._shared_blackboard_summary(context, role="fix")
        critic_anchor = self._shared_blackboard_summary(context, role="critic")
        validation_anchor = self._shared_blackboard_summary(context, role="validate")
        return (
            f"Respond to the critic for `{incident.title}` by revising or defending the fix thesis. "
            f"Original repair proposal: {repair_anchor}. "
            f"Critic objection: {critic_anchor}. "
            f"Validation guardrails: {validation_anchor}. "
            f"Keep the final patch minimal around {compressed_error.root_cause_cluster}."
        )

    def _repair_steps(
        self,
        incident: BugReport | IncidentReport,
        compressed_error: CompressedError,
        context: ContextBundle,
    ) -> list[ExtractionStep]:
        target_files = context.relevant_files[:3] or incident.changed_files[:3]
        steps: list[RepairStep] = []

        for skill in context.matched_skills[:2]:
            steps.append(
                RepairStep(
                    title=f"Apply skill: {skill.name}",
                    description=skill.action_template,
                    target_files=target_files,
                    confidence=max(0.55, min(0.9, 0.55 + skill.success_rate * 0.3)),
                    source_skill_id=skill.id,
                )
            )

        error_type = compressed_error.error_type.value
        if error_type == "dependency_conflict":
            steps.append(
                RepairStep(
                    title="Reconcile dependency constraints",
                    description="Align direct dependency pins in requirements or pyproject with the resolver error, then regenerate the minimal lock/install artifact needed by CI.",
                    target_files=self._prefer_config_files(context.relevant_files),
                    confidence=0.82,
                )
            )
        elif error_type == "test_regression":
            steps.append(
                RepairStep(
                    title="Restore broken behavior contract",
                    description="Compare the changed service path against the failing test expectation, repair the service boundary, and add a regression assertion for the restored contract.",
                    target_files=target_files,
                    confidence=0.76,
                )
            )
        elif error_type == "cross_module_defect":
            steps.append(
                RepairStep(
                    title="Stabilize cross-module interface",
                    description="Trace producer and consumer modules around the failing stack, normalize the shared contract, and update adapters at the boundary instead of patching each caller independently.",
                    target_files=target_files,
                    confidence=0.72,
                )
            )
        else:
            steps.append(
                RepairStep(
                    title="Patch the top failing frame",
                    description="Start from the first failing frame, verify import paths and null/shape assumptions, then limit edits to the narrowest module that can stop the failure from propagating.",
                    target_files=target_files,
                    confidence=0.7,
                )
            )
        return self._dedupe_steps(steps)

    def _validation_steps(self, context: ContextBundle) -> list[ExtractionStep]:
        target_files = context.relevant_files[:4]
        return [
            RepairStep(
                title="Run targeted validation",
                description="Execute the narrowest tests or CI job that covers the affected modules before running the full pipeline.",
                target_files=target_files,
                confidence=0.8,
            ),
            RepairStep(
                title="Guard against regression spread",
                description="Add or update a regression test around the repaired contract and compare the new failure surface with the original root cause cluster.",
                target_files=target_files,
                confidence=0.75,
            ),
        ]

    def _critic_steps(self, context: ContextBundle) -> list[ExtractionStep]:
        target_files = list(self._shared_blackboard(context).get("focus_files", []))[:4] or context.relevant_files[:4]
        return [
            RepairStep(
                title="Challenge the dominant fix assumption",
                description="Compare the proposed patch scope against the root cause path and call out where the current plan may only treat symptoms rather than the shared contract break.",
                target_files=target_files,
                confidence=0.74,
            ),
            RepairStep(
                title="Demand rollback-safe evidence",
                description="Ask for a narrower patch scope or stronger validation evidence before accepting edits that span multiple boundaries.",
                target_files=target_files,
                confidence=0.72,
            ),
        ]

    def _reflection_steps(self, context: ContextBundle) -> list[ExtractionStep]:
        blackboard = self._shared_blackboard(context)
        target_files = list(blackboard.get("focus_files", []))[:4] or context.relevant_files[:4]
        steps = [
            RepairStep(
                title="Consolidate shared repair path",
                description="Merge diagnosis, repair, and validation notes into one ordered plan that keeps edits on the smallest shared file set.",
                target_files=target_files,
                confidence=0.79,
            )
        ]
        if blackboard.get("risks"):
            steps.append(
                RepairStep(
                    title="Protect the highest-risk boundary",
                    description="Address the strongest rollback or validation risk from the shared blackboard before widening the repair scope.",
                    target_files=target_files,
                    confidence=0.77,
                )
            )
        return self._dedupe_steps(steps)

    def _rebuttal_steps(self, context: ContextBundle) -> list[ExtractionStep]:
        blackboard = self._shared_blackboard(context)
        target_files = list(blackboard.get("focus_files", []))[:4] or context.relevant_files[:4]
        return [
            RepairStep(
                title="Answer the critic with a narrower patch",
                description="Refine the original fix proposal so each edit directly addresses the critic's strongest objection and removes any file that lacks root-cause evidence.",
                target_files=target_files,
                confidence=0.78,
            ),
            RepairStep(
                title="Bind the revised fix to validation evidence",
                description="Pair the rebuttal with the exact validation signal that would prove the critic's rollback concern is resolved before the patch is accepted.",
                target_files=target_files,
                confidence=0.76,
            ),
        ]

    def _prefer_config_files(self, candidates: list[str]) -> list[str]:
        config_files = [
            path for path in candidates if path.endswith(("requirements.txt", "pyproject.toml", "poetry.lock", "package.json"))
        ]
        return config_files or candidates[:2]

    def _dedupe_steps(self, steps: list[ExtractionStep]) -> list[ExtractionStep]:
        deduped: list[ExtractionStep] = []
        seen_titles: set[str] = set()
        for step in steps:
            if step.title in seen_titles:
                continue
            seen_titles.add(step.title)
            deduped.append(step)
        return deduped

    def _shared_blackboard(self, context: ContextBundle) -> dict:
        blackboard = context.working_context.get("shared_blackboard", {})
        if isinstance(blackboard, dict):
            return blackboard
        return {}

    def _shared_blackboard_summary(self, context: ContextBundle, role: str) -> str:
        blackboard = self._shared_blackboard(context)
        for note in blackboard.get("notes", []):
            if isinstance(note, dict) and note.get("role") == role:
                return str(note.get("summary", "no prior note"))[:180]
        return "no prior note"

    def _root_cause_path_summary(self, context: ContextBundle) -> str:
        paths = context.working_context.get("root_cause_paths", [])
        if not isinstance(paths, list) or not paths:
            return "no explicit path"
        first = paths[0]
        if isinstance(first, dict):
            return " -> ".join(str(item) for item in first.get("path", [])[:4]) or "no explicit path"
        return str(first)

    def _augment_protocol_metadata(
        self,
        execution: AgentExecution,
        incident: BugReport | IncidentReport,
        compressed_error: CompressedError,
        context: ContextBundle,
    ) -> AgentExecution:
        execution.metadata.setdefault(
            "collaboration_messages",
            self._collaboration_messages(execution, incident, compressed_error, context),
        )
        tool_calls = self._tool_protocol_calls(execution, context)
        execution.metadata["tool_protocol_calls"] = tool_calls
        execution.metadata.setdefault(
            "shared_memory_write",
            self._shared_memory_write(execution, compressed_error, context),
        )
        debate = self._debate_position(execution, context)
        if debate:
            execution.metadata.setdefault("debate_position", debate)
        return execution

    def _collaboration_messages(
        self,
        execution: AgentExecution,
        incident: BugReport | IncidentReport,
        compressed_error: CompressedError,
        context: ContextBundle,
    ) -> list[dict]:
        target_files = list(
            dict.fromkeys(
                file_path
                for step in execution.repair_steps
                for file_path in step.target_files
                if file_path
            )
        )[:5]
        message_targets = {
            # 新角色
            AgentRole.EVENT_CLASSIFIER: ["info_extractor"],
            AgentRole.INFO_EXTRACTOR: ["context_enricher", "review_agent", "quality_scorer"],
            AgentRole.CONTEXT_ENRICHER: ["review_agent", "quality_scorer"],
            AgentRole.REVIEW_AGENT: ["quality_scorer", "pipeline_orchestrator"],
            AgentRole.PIPELINE_ORCHESTRATOR: ["orchestrator"],
            AgentRole.QUALITY_SCORER: ["review_agent", "pipeline_orchestrator"],
            # 旧角色（向后兼容）
            AgentRole.DIAGNOSE: ["fix", "validate", "critic"],
            AgentRole.FIX: ["critic", "rebuttal", "reflect"],
            AgentRole.VALIDATE: ["fix", "critic", "reflect"],
            AgentRole.CRITIC: ["fix", "rebuttal", "reflect"],
            AgentRole.REBUTTAL: ["critic", "reflect"],
            AgentRole.REFLECT: ["orchestrator"],
            AgentRole.GENERALIST: ["orchestrator"],
        }
        message_type_map = {
            # 新角色
            AgentRole.EVENT_CLASSIFIER: "event_classification",
            AgentRole.INFO_EXTRACTOR: "extraction_result",
            AgentRole.CONTEXT_ENRICHER: "context_enrichment",
            AgentRole.REVIEW_AGENT: "review_summary",
            AgentRole.PIPELINE_ORCHESTRATOR: "pipeline_consensus",
            AgentRole.QUALITY_SCORER: "quality_score",
            # 旧角色
            AgentRole.DIAGNOSE: "root_cause_handoff",
            AgentRole.FIX: "repair_proposal",
            AgentRole.VALIDATE: "validation_warning",
            AgentRole.CRITIC: "counter_argument",
            AgentRole.REBUTTAL: "rebuttal_revision",
            AgentRole.REFLECT: "consensus",
            AgentRole.GENERALIST: "final_plan",
        }
        message_type = message_type_map.get(execution.role, "unknown")
        return [
            {
                "sender": execution.role.value,
                "recipient": recipient,
                "message_type": message_type,
                "content": execution.reasoning_summary[:220],
                "incident_id": incident.id,
                "error_type": compressed_error.error_type.value,
                "related_files": target_files,
            }
            for recipient in message_targets[execution.role]
        ]

    def _tool_protocol_calls(self, execution: AgentExecution, context: ContextBundle) -> list[dict]:
        actual_results = execution.metadata.get("tool_results", [])
        if isinstance(actual_results, list) and actual_results:
            return [
                {
                    "actor": execution.role.value,
                    "tool_name": str(item.get("tool_name", "unknown")),
                    "intent": execution.reasoning_summary[:160],
                    "payload": dict(item.get("arguments", {})) if isinstance(item.get("arguments", {}), dict) else {},
                    "status": str(item.get("status", "completed")),
                    "output": item.get("output", {}),
                    "duration_ms": int(item.get("duration_ms", 0)),
                }
                for item in actual_results
                if isinstance(item, dict)
            ]
        tool_name = {
            # 新角色
            AgentRole.EVENT_CLASSIFIER: "event.classify",
            AgentRole.INFO_EXTRACTOR: "extraction.run",
            AgentRole.CONTEXT_ENRICHER: "rag.enrich",
            AgentRole.REVIEW_AGENT: "review.summarize",
            AgentRole.PIPELINE_ORCHESTRATOR: "pipeline.orchestrate",
            AgentRole.QUALITY_SCORER: "quality.score",
            # 旧角色
            AgentRole.DIAGNOSE: "context.inspect",
            AgentRole.FIX: "patch.scope",
            AgentRole.VALIDATE: "validation.plan",
            AgentRole.CRITIC: "debate.challenge",
            AgentRole.REBUTTAL: "debate.rebuttal",
            AgentRole.REFLECT: "consensus.merge",
            AgentRole.GENERALIST: "orchestrator.plan",
        }.get(execution.role, "unknown.tool")
        payload = {
            "focus_files": list(
                dict.fromkeys(
                    file_path
                    for step in execution.repair_steps
                    for file_path in step.target_files
                    if file_path
                )
            )[:6],
            "step_titles": [step.title for step in execution.repair_steps[:4]],
            "root_cause_paths": context.working_context.get("root_cause_paths", [])[:3],
        }
        return [
            {
                "actor": execution.role.value,
                "tool_name": tool_name,
                "intent": execution.reasoning_summary[:160],
                "payload": payload,
                "status": "completed",
            }
        ]

    def _tool_evidence_summary(self, tool_results: list[dict]) -> str:
        if not tool_results:
            return ""
        fragments: list[str] = []
        for item in tool_results[:3]:
            if not isinstance(item, dict):
                continue
            tool_name = str(item.get("tool_name", "tool"))
            output = item.get("output", {})
            if tool_name == "sandbox.exec" and isinstance(output, dict):
                fragments.append(f"{tool_name} rc={output.get('returncode', '?')}")
                continue
            if isinstance(output, dict):
                keys = [key for key in output.keys() if key not in {"preview", "stdout", "stderr"}][:2]
                if keys:
                    fragments.append(f"{tool_name} -> {', '.join(keys)}")
                    continue
            fragments.append(tool_name)
        return "; ".join(fragments)

    def _shared_memory_write(
        self,
        execution: AgentExecution,
        compressed_error: CompressedError,
        context: ContextBundle,
    ) -> dict:
        return {
            "role": execution.role.value,
            "category": {
                # 新角色
                AgentRole.EVENT_CLASSIFIER: "event_classification",
                AgentRole.INFO_EXTRACTOR: "extraction_result",
                AgentRole.CONTEXT_ENRICHER: "context_enrichment",
                AgentRole.REVIEW_AGENT: "review_summary",
                AgentRole.PIPELINE_ORCHESTRATOR: "pipeline_consensus",
                AgentRole.QUALITY_SCORER: "quality_assessment",
                # 旧角色
                AgentRole.DIAGNOSE: "root_cause_pattern",
                AgentRole.FIX: "repair_strategy",
                AgentRole.VALIDATE: "validation_guardrail",
                AgentRole.CRITIC: "risk_objection",
                AgentRole.REBUTTAL: "rebuttal_resolution",
                AgentRole.REFLECT: "consensus_plan",
                AgentRole.GENERALIST: "combined_plan",
            }.get(execution.role, "unknown"),
            "summary": execution.reasoning_summary[:220],
            "cluster": compressed_error.root_cause_cluster,
            "focus_files": list(
                dict.fromkeys(
                    file_path
                    for step in execution.repair_steps
                    for file_path in step.target_files
                    if file_path
                )
            )[:6]
            or context.relevant_files[:4],
        }

    def _debate_position(self, execution: AgentExecution, context: ContextBundle) -> dict:
        if execution.role not in {AgentRole.FIX, AgentRole.CRITIC, AgentRole.REBUTTAL, AgentRole.REFLECT}:
            return {}
        stance = {
            AgentRole.FIX: "propose",
            AgentRole.CRITIC: "challenge",
            AgentRole.REBUTTAL: "rebut",
            AgentRole.REFLECT: "adjudicate",
        }[execution.role]
        return {
            "role": execution.role.value,
            "stance": stance,
            "claim": execution.reasoning_summary[:220],
            "supporting_files": list(
                dict.fromkeys(
                    file_path
                    for step in execution.repair_steps
                    for file_path in step.target_files
                    if file_path
                )
            )[:5]
            or context.relevant_files[:4],
        }
