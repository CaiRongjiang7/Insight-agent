from __future__ import annotations

from .config import PlatformConfig
from .models import AgentRole, AgentTask, CompressedError, IncidentReport, SchedulerDecision, Skill


class CostAwareScheduler:
    def __init__(self, config: PlatformConfig) -> None:
        self.config = config

    def decide(
        self,
        incident: IncidentReport,
        compressed_error: CompressedError,
        relevant_files: list[str],
        matched_skills: list[Skill],
    ) -> SchedulerDecision:
        complexity_score, reasons = self._complexity(incident, compressed_error, relevant_files, matched_skills)
        multi_agent = (
            complexity_score > self.config.scheduler.simple_task_threshold
            and (
                len(relevant_files) >= self.config.scheduler.multi_agent_min_files
                or compressed_error.error_type.value == "cross_module_defect"
                or not matched_skills
            )
        )
        budget_breakdown = self._budget_breakdown(multi_agent)
        if multi_agent:
            tasks = [
                AgentTask(
                    id="diagnose-1",
                    role=AgentRole.DIAGNOSE,
                    objective="Compress the root cause and identify blast radius.",
                    focus_files=relevant_files[:4],
                    budget=budget_breakdown["diagnose"],
                    allowed_tools=["context.inspect", "graph.trace", "memory.lookup", "log.inspect"],
                    sandbox_policy={"access": "workspace_copy", "network": False},
                ),
                AgentTask(
                    id="fix-1",
                    role=AgentRole.FIX,
                    objective="Generate the repair plan using matched skills and repository context.",
                    focus_files=relevant_files[:6],
                    budget=budget_breakdown["fix"],
                    allowed_tools=["context.inspect", "patch.scope", "memory.lookup", "sandbox.exec"],
                    sandbox_policy={"access": "workspace_copy", "network": False},
                ),
                AgentTask(
                    id="validate-1",
                    role=AgentRole.VALIDATE,
                    objective="Design the narrowest safe validation path and estimate rollback risk.",
                    focus_files=relevant_files[:6],
                    budget=budget_breakdown["validate"],
                    allowed_tools=["validation.plan", "log.inspect", "sandbox.exec", "risk.rank"],
                    sandbox_policy={"access": "read_only", "network": False},
                ),
                AgentTask(
                    id="critic-1",
                    role=AgentRole.CRITIC,
                    objective="Challenge the proposed fix scope, identify hidden assumptions, and force a repair-vs-risk debate.",
                    focus_files=relevant_files[:6],
                    budget=budget_breakdown["critic"],
                    allowed_tools=["debate.challenge", "graph.trace", "risk.rank", "memory.lookup"],
                    sandbox_policy={"access": "read_only", "network": False},
                ),
                AgentTask(
                    id="rebuttal-1",
                    role=AgentRole.REBUTTAL,
                    objective="Respond to the critic with a narrower or better-defended fix revision before the final consensus.",
                    focus_files=relevant_files[:6],
                    budget=budget_breakdown["rebuttal"],
                    allowed_tools=["debate.rebuttal", "patch.scope", "validation.plan", "sandbox.exec"],
                    sandbox_policy={"access": "workspace_copy", "network": False},
                ),
                AgentTask(
                    id="reflect-1",
                    role=AgentRole.REFLECT,
                    objective="Merge diagnose, repair, critic, and rebuttal notes into one consensus plan and surface the main rollback risks.",
                    focus_files=relevant_files[:6],
                    budget=budget_breakdown["reflect"],
                    allowed_tools=["consensus.merge", "memory.write", "risk.rank", "memory.lookup"],
                    sandbox_policy={"access": "read_only", "network": False},
                ),
            ]
            mode = "multi_agent"
        else:
            tasks = [
                AgentTask(
                    id="generalist-1",
                    role=AgentRole.GENERALIST,
                    objective="Diagnose, repair, and validate in one pass.",
                    focus_files=relevant_files[:5],
                    budget=budget_breakdown["generalist"],
                    allowed_tools=["context.inspect", "graph.trace", "patch.scope", "validation.plan", "memory.lookup", "log.inspect", "sandbox.exec"],
                    sandbox_policy={"access": "workspace_copy", "network": False},
                )
            ]
            mode = "single_agent"
        return SchedulerDecision(
            mode=mode,
            complexity_score=complexity_score,
            reasons=reasons,
            tasks=tasks,
            budget_breakdown=budget_breakdown,
        )

    def _complexity(
        self,
        incident: IncidentReport,
        compressed_error: CompressedError,
        relevant_files: list[str],
        matched_skills: list[Skill],
    ) -> tuple[int, list[str]]:
        score = 1
        reasons: list[str] = []

        error_type = compressed_error.error_type.value
        if error_type == "dependency_conflict":
            score += 2
            reasons.append("Dependency conflicts require environment-wide reconciliation.")
        elif error_type == "test_regression":
            score += 2
            reasons.append("Regression failures usually need code plus test alignment.")
        elif error_type == "cross_module_defect":
            score += 3
            reasons.append("Cross-module signals increase coordination cost.")
        elif error_type == "ci_failure":
            score += 1
            reasons.append("CI failures often start from a localized failing frame.")

        if len(relevant_files) >= 3:
            score += 2
            reasons.append("Multiple relevant files indicate a wider blast radius.")
        elif len(relevant_files) == 2:
            score += 1
            reasons.append("Two relevant files suggest at least one contract boundary.")

        if len(compressed_error.key_stack_frames) >= 2:
            score += 1
            reasons.append("Multiple stack frames increase tracing depth.")

        if len(incident.logs) > 1_500:
            score += 1
            reasons.append("Long logs benefit from parallel diagnosis and validation.")

        if matched_skills:
            best_skill = max(skill.success_rate for skill in matched_skills)
            if best_skill >= 0.66:
                score -= self.config.scheduler.high_confidence_skill_bonus
                reasons.append("High-confidence historical skill reduces exploration cost.")
            else:
                score -= 1
                reasons.append("Matched skills partially reduce search overhead.")
        else:
            score += 1
            reasons.append("No skill match means higher reasoning uncertainty.")

        return max(score, 1), reasons

    def _budget_breakdown(self, multi_agent: bool) -> dict[str, int]:
        total = self.config.token_budget.total_budget
        if multi_agent:
            diagnose = int(total * 0.2)
            fix = int(total * 0.24)
            validate = int(total * 0.17)
            critic = int(total * 0.14)
            rebuttal = int(total * 0.11)
            return {
                "diagnose": diagnose,
                "fix": fix,
                "validate": validate,
                "critic": critic,
                "rebuttal": rebuttal,
                "reflect": total - diagnose - fix - validate - critic - rebuttal,
            }
        return {"generalist": total}
