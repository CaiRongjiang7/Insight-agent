from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import PlatformConfig
from .context import LayeredContextManager
from .embeddings import EmbeddingClient, VectorCache
from .error_semantics import ErrorSemanticCompressor
from .flow_debug import configure_flow_events, flow_log
from .gitlab_ci import GitLabCiClient
from .github_actions import GitHubActionsClient
from .jenkins_ci import JenkinsCiClient
from .learning import AutoLearningOrchestrator, DreamLearnLoop
from .llm_executor import OpenAIPatchExecutor, PatchExecutorError
from .memory import MemoryStore, to_jsonable
from .models import (
    ApprovalScope,
    CiSyncResult,
    CiWorkflowRun,
    CompressedError,
    ContextBundle,
    ExecutionTrace,
    GitHubSyncResult,
    IncidentReport,
    IncidentType,
    PreparedIncident,
    RepairOutcome,
    RepairStep,
    RepoSnapshot,
    SchedulerDecision,
    ValidationResult,
    iso_now,
)
from .observability import MetricsCollector
from .repo_checkout import RepoCheckoutManager
from .repo_indexer import RepositoryIndexer
from .reranker import build_reranker
from .remote_delivery import RemoteDeliveryManager
from .retrieval import SemanticRetriever
from .runtime import ConcurrentAgentRuntime
from .sandbox import GitSandbox
from .scheduler import CostAwareScheduler
from .security import ApprovalStore, AuditLogger, SecurityManager
from .extraction_skills import KnowledgeMatchSkill
from .knowledge_base import SkillRepository
from .validator import ValidationRunner
from .vector_store import build_vector_store


class RepoAutonomyPlatform:
    def __init__(self, config: PlatformConfig | None = None) -> None:
        self.config = config or PlatformConfig.default(Path.cwd())
        configure_flow_events(self.config.paths.api_data_dir / "agent_flow_events.jsonl")
        self.approval_store = ApprovalStore(self.config.paths.approvals_path, self.config.security)
        self.audit_logger = AuditLogger(self.config.paths.audit_log_path)
        self.security_manager = SecurityManager(self.config.security, self.approval_store, self.audit_logger)
        self.indexer = RepositoryIndexer(self.config.graph)
        self.compressor = ErrorSemanticCompressor()
        self.context_manager = LayeredContextManager(self.config)
        self.memory_store = MemoryStore(self.config.paths.memory_db_path)
        self.skill_repository = SkillRepository(self.config.paths.skills_path)
        self.scheduler = CostAwareScheduler(self.config)
        self.metrics_collector = MetricsCollector(self.config.paths.metrics_path)
        self.dream_loop = DreamLearnLoop(self.memory_store, self.skill_repository)
        self.auto_learning = AutoLearningOrchestrator(
            self.dream_loop,
            self.audit_logger,
            enabled=self.config.learning.auto_run_on_successful_repair,
            async_enabled=self.config.learning.async_queue_enabled,
            review_limit=self.config.learning.review_limit,
            batch_window_seconds=self.config.learning.batch_window_seconds,
            max_batch_size=self.config.learning.max_batch_size,
            lease_timeout_seconds=self.config.learning.lease_timeout_seconds,
            scheduler_lease_ttl_seconds=self.config.learning.scheduler_lease_ttl_seconds,
            idle_poll_seconds=self.config.learning.idle_poll_seconds,
            idle_after_seconds=self.config.learning.idle_after_seconds,
            idle_backfill_limit=self.config.learning.idle_backfill_limit,
            repair_priority=self.config.learning.repair_priority,
            idle_backfill_priority=self.config.learning.idle_backfill_priority,
            retry_delay_seconds=self.config.learning.retry_delay_seconds,
            max_retry_delay_seconds=self.config.learning.max_retry_delay_seconds,
            max_job_failures=self.config.learning.max_job_failures,
            embedded_workers_enabled=self.config.learning.embedded_workers_enabled,
        )
        self.patch_executor = OpenAIPatchExecutor(self.config.openai, self.config.sandbox)
        self.knowledge_match_skill = KnowledgeMatchSkill(self.skill_repository, top_k=3)
        self.validation_runner = ValidationRunner(self.config.validation, self.security_manager)
        self.vector_cache = VectorCache(self.config.paths.vector_index_path)
        self.vector_store = build_vector_store(self.config.retrieval, self.config.paths.vector_store_path)
        self.embedding_client = EmbeddingClient(self.config.embeddings, self.vector_cache)
        self.retriever = SemanticRetriever(self.config.retrieval, self.embedding_client, self.vector_store, build_reranker(self.config.retrieval))
        self.github_client = GitHubActionsClient(self.config.github, self.security_manager)
        self.gitlab_client = GitLabCiClient(self.config.gitlab, self.security_manager)
        self.jenkins_client = JenkinsCiClient(self.config.jenkins, self.security_manager)
        self.remote_delivery = RemoteDeliveryManager(
            github_client=self.github_client,
            gitlab_client=self.gitlab_client,
            security_manager=self.security_manager,
            github_token=self.config.github.token,
            gitlab_token=self.config.gitlab.token,
        )
        self.repo_checkout = RepoCheckoutManager(
            self.config.paths.checkouts_dir,
            self.config.root_dir,
            self.security_manager,
            github_token=self.config.github.token,
            gitlab_token=self.config.gitlab.token,
        )
        self.agent_runtime = ConcurrentAgentRuntime(
            self.config.runtime,
            self.config.paths.runtime_tasks_dir,
            self._agent_reasoning_settings(),
        )

    def approve(
        self,
        scope: ApprovalScope,
        subject: str,
        actor: str,
        reason: str = "",
        ttl_hours: int | None = None,
    ) -> dict[str, Any]:
        approval = self.security_manager.approve(scope=scope, subject=subject, actor=actor, reason=reason, ttl_hours=ttl_hours)
        return to_jsonable(approval)

    def list_approvals(self) -> list[dict[str, Any]]:
        return [to_jsonable(item) for item in self.security_manager.active_approvals()]

    def audit_events(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.audit_logger.recent(limit=limit)

    def load_incident(self, incident_path: str | Path) -> IncidentReport:
        data = json.loads(Path(incident_path).read_text(encoding="utf-8"))
        return IncidentReport.from_dict(data)

    def sync_github_failures(
        self,
        owner: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        limit: int = 5,
    ) -> GitHubSyncResult:
        sync = self.github_client.sync_failed_runs(owner=owner, repo=repo, branch=branch, limit=limit)
        for incident in sync.incidents:
            self._persist_incident(incident)
        return sync

    def sync_ci_failures(
        self,
        provider: str,
        *,
        owner: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        project_id: str | None = None,
        job_name: str | None = None,
        limit: int = 5,
    ) -> CiSyncResult:
        provider_key = provider.strip().lower()
        flow_log(
            "platform.sync_ci.start",
            provider=provider_key,
            owner=owner,
            repo=repo,
            branch=branch,
            project_id=project_id,
            job_name=job_name,
            limit=limit,
        )
        if provider_key == "github_actions":
            sync = self.github_client.sync_failed_runs(owner=owner, repo=repo, branch=branch, limit=limit)
            result = CiSyncResult(
                provider="github_actions",
                runs=[
                    CiWorkflowRun(
                        provider="github_actions",
                        run_id=str(run.run_id),
                        name=run.name,
                        status=run.status,
                        conclusion=run.conclusion,
                        web_url=run.html_url,
                        logs_url=run.logs_url,
                        rerun_target=run.rerun_url,
                        ref=run.head_branch,
                        revision=run.head_sha,
                        created_at=run.created_at,
                        updated_at=run.updated_at,
                        metadata={
                            "workflow_id": run.workflow_id,
                            "event": run.event,
                            "owner": resolved_owner,
                            "repo": resolved_repo,
                            "repo_url": f"https://github.com/{resolved_owner}/{resolved_repo}.git",
                            "head_branch": run.head_branch,
                            "head_sha": run.head_sha,
                        },
                    )
                    for run in sync.runs
                ],
                incidents=sync.incidents,
                latest_logs_excerpt=sync.latest_logs_excerpt,
            )
        elif provider_key == "gitlab_ci":
            result = self.gitlab_client.sync_failed_runs(project_id=project_id, ref=branch, limit=limit)
        elif provider_key == "jenkins":
            result = self.jenkins_client.sync_failed_runs(job_name=job_name, limit=limit)
        else:
            raise ValueError(f"Unsupported CI provider: {provider}")
        for incident in result.incidents:
            flow_log("platform.sync_ci.persist_incident", provider=provider_key, incident_id=incident.id, metadata=incident.metadata)
            self._persist_incident(incident)
        flow_log("platform.sync_ci.done", provider=provider_key, runs=len(result.runs), incidents=len(result.incidents))
        return result

    def list_jenkins_jobs(self) -> list[dict[str, str | None]]:
        return self.jenkins_client.list_jobs()

    def rerun_github_workflow(
        self,
        run_id: int,
        owner: str | None = None,
        repo: str | None = None,
        failed_only: bool = False,
    ) -> dict[str, Any]:
        resolved_owner = owner or self.config.github.default_owner
        resolved_repo = repo or self.config.github.default_repo
        if not resolved_owner or not resolved_repo:
            raise ValueError("GitHub owner and repo are required for rerun.")
        self.github_client.rerun_workflow_run(resolved_owner, resolved_repo, run_id, failed_only=failed_only)
        return {"owner": resolved_owner, "repo": resolved_repo, "run_id": run_id, "failed_only": failed_only}

    def rerun_ci_workflow(
        self,
        provider: str,
        *,
        run_id: str,
        owner: str | None = None,
        repo: str | None = None,
        project_id: str | None = None,
        job_name: str | None = None,
        failed_only: bool = False,
    ) -> dict[str, Any]:
        provider_key = provider.strip().lower()
        if provider_key == "github_actions":
            return self.rerun_github_workflow(int(run_id), owner=owner, repo=repo, failed_only=failed_only)
        if provider_key == "gitlab_ci":
            resolved_project = project_id or self.config.gitlab.default_project_id
            if not resolved_project:
                raise ValueError("GitLab project id is required for rerun.")
            self.gitlab_client.rerun_pipeline(resolved_project, run_id)
            return {"provider": provider_key, "project_id": resolved_project, "run_id": run_id}
        if provider_key == "jenkins":
            resolved_job = job_name or self.config.jenkins.default_job
            if not resolved_job:
                raise ValueError("Jenkins job name is required for rerun.")
            self.jenkins_client.rerun_build(resolved_job, run_id)
            return {"provider": provider_key, "job_name": resolved_job, "run_id": run_id}
        raise ValueError(f"Unsupported CI provider: {provider}")

    def prepare_incident(
        self,
        incident: IncidentReport,
        repo_root: str | Path | None = None,
    ) -> PreparedIncident:
        workspace = self.repo_checkout.resolve(incident, repo_root=repo_root)
        snapshot = self.indexer.index(workspace.root)
        compressed_error = self.compressor.compress(incident)
        retrieval_query = self._retrieval_query(incident, compressed_error)

        lexical_skill_matches = self.skill_repository.match_skills(compressed_error, incident, limit=max(self.config.retrieval.top_k, 4))
        semantic_skill_matches = self.retriever.rank_skills(retrieval_query, self.skill_repository.all_skills(), limit=self.config.retrieval.top_k)
        matched_skills = self._merge_skill_matches(lexical_skill_matches, semantic_skill_matches)

        relevant_files = self.indexer.relevant_files(snapshot, incident, compressed_error)
        short_term_memories = self.memory_store.recent(limit=4)
        memory_candidates = self._merge_memory_candidates(
            self.memory_store.related(
                incident_type=compressed_error.error_type.value,
                cluster=compressed_error.root_cause_cluster,
                keywords=compressed_error.keywords,
                limit=5,
            ),
            self.memory_store.candidates(limit=self.config.retrieval.max_memory_candidates),
        )
        semantic_memory_matches = self.retriever.rank_memories(retrieval_query, memory_candidates, limit=self.config.retrieval.top_k)
        collaboration_candidates = self._merge_memory_candidates(
            self.memory_store.related_collaboration_memories(
                incident_type=compressed_error.error_type.value,
                cluster=compressed_error.root_cause_cluster,
                keywords=compressed_error.keywords,
                limit=max(self.config.retrieval.top_k, 4),
            ),
            self.memory_store.collaboration_candidates(limit=self.config.retrieval.max_memory_candidates),
        )
        semantic_collaboration_matches = self.retriever.rank_memories(
            retrieval_query,
            collaboration_candidates,
            limit=max(2, self.config.retrieval.top_k),
        )
        long_term_memories = [match.payload for match in (semantic_memory_matches + semantic_collaboration_matches)]

        context = self.context_manager.build_context(
            incident=incident,
            compressed_error=compressed_error,
            snapshot=snapshot,
            relevant_files=relevant_files,
            matched_skills=matched_skills,
            short_term_memories=short_term_memories,
            long_term_memories=long_term_memories,
        )
        decision = self.scheduler.decide(
            incident=incident,
            compressed_error=compressed_error,
            relevant_files=relevant_files,
            matched_skills=matched_skills,
        )
        runtime_trace_id = f"prep-{incident.id}-{iso_now().replace(':', '-').replace('+00:00', 'z')}"
        executions = self.agent_runtime.execute(runtime_trace_id, decision.tasks, incident, compressed_error, context)
        for execution in executions:
            execution.metadata.setdefault(
                "repo_workspace",
                {
                    "root": str(workspace.root),
                    "source": workspace.source,
                    "repo_url": workspace.repo_url,
                    "branch": workspace.branch,
                    "revision": workspace.revision,
                },
            )
        repair_steps = self._flatten_repair_steps(executions)
        github_run_id = self._github_run_id(incident)
        return PreparedIncident(
            incident=incident,
            snapshot=snapshot,
            compressed_error=compressed_error,
            matched_skills=matched_skills,
            relevant_files=relevant_files,
            context=context,
            decision=decision,
            executions=executions,
            repair_steps=repair_steps,
            retrieval_matches=semantic_skill_matches + semantic_memory_matches + semantic_collaboration_matches,
            github_run_id=github_run_id,
        )

    def analyze_incident(
        self,
        incident: IncidentReport,
        repo_root: str | Path | None = None,
    ) -> ExecutionTrace:
        prepared = self.prepare_incident(incident=incident, repo_root=repo_root)
        trace = self._build_trace(
            prepared=prepared,
            success=self._estimate_success(prepared.executions, matched_skills=prepared.matched_skills),
            token_usage=sum(execution.token_estimate for execution in prepared.executions),
            repair_summary=self._repair_summary(prepared.repair_steps, prepared.compressed_error, prepared.relevant_files),
            execution_mode="planning",
        )
        self._persist_trace(trace)
        return trace

    def analyze_incident_file(
        self,
        incident_path: str | Path,
        repo_root: str | Path | None = None,
    ) -> ExecutionTrace:
        incident = self.load_incident(incident_path)
        return self.analyze_incident(incident=incident, repo_root=repo_root)

    def analyze_latest_github_failure(
        self,
        owner: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        local_repo_root: str | Path | None = None,
    ) -> tuple[ExecutionTrace, IncidentReport]:
        sync = self.sync_github_failures(owner=owner, repo=repo, branch=branch, limit=1)
        if not sync.incidents:
            raise RuntimeError("No failed GitHub Actions runs were found.")
        incident = sync.incidents[0]
        trace = self.analyze_incident(incident=incident, repo_root=local_repo_root)
        return trace, incident

    def analyze_latest_ci_failure(
        self,
        provider: str,
        *,
        owner: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        project_id: str | None = None,
        job_name: str | None = None,
        local_repo_root: str | Path | None = None,
    ) -> tuple[ExecutionTrace, IncidentReport]:
        sync = self.sync_ci_failures(
            provider,
            owner=owner,
            repo=repo,
            branch=branch,
            project_id=project_id,
            job_name=job_name,
            limit=1,
        )
        if not sync.incidents:
            raise RuntimeError(f"No failed {provider} runs were found.")
        incident = sync.incidents[0]
        trace = self.analyze_incident(incident=incident, repo_root=local_repo_root)
        return trace, incident

    def repair_incident(
        self,
        incident: IncidentReport,
        repo_root: str | Path | None = None,
        max_attempts: int | None = None,
        remote_delivery_confirmed: bool = False,
    ) -> RepairOutcome:
        flow_log(
            "platform.repair.start",
            incident_id=incident.id,
            repo_root=repo_root,
            max_attempts=max_attempts,
            remote_delivery_confirmed=remote_delivery_confirmed,
            metadata=incident.metadata,
        )
        workspace = self.repo_checkout.resolve(incident, repo_root=repo_root)
        flow_log(
            "platform.repair.workspace_resolved",
            incident_id=incident.id,
            root=workspace.root,
            source=workspace.source,
            repo_url=workspace.repo_url,
            branch=workspace.branch,
            revision=workspace.revision,
        )
        self._hydrate_incident_repo_metadata(incident, workspace)
        flow_log("platform.repair.metadata_hydrated", incident_id=incident.id, metadata=incident.metadata)
        remote_approvals = []
        if self.config.runtime.agent_network_enabled:
            flow_log("platform.repair.remote_execution_authorize.start", incident_id=incident.id, workspace=workspace.root)
            remote_approvals = self.security_manager.authorize_remote_execution(f"repair:{workspace.root}")
            flow_log("platform.repair.remote_execution_authorize.ok", incident_id=incident.id, approvals=len(remote_approvals))
        flow_log("platform.repair.prepare.start", incident_id=incident.id)
        prepared = self.prepare_incident(incident=incident, repo_root=workspace.root)
        flow_log(
            "platform.repair.prepare.done",
            incident_id=incident.id,
            relevant_files=prepared.relevant_files,
            matched_skills=[{"id": skill.id, "name": skill.name} for skill in prepared.matched_skills],
            executions=len(prepared.executions),
        )
        for skill in prepared.matched_skills:
            flow_log("agent.skill.selected", incident_id=incident.id, skill_id=skill.id, skill_name=skill.name)
        for task in prepared.decision.tasks:
            flow_log(
                "agent.sub_agent.derived",
                incident_id=incident.id,
                task_id=task.id,
                role=task.role.value,
                objective=task.objective,
                focus_files=task.focus_files,
                allowed_tools=task.allowed_tools,
            )
            for tool_name in task.allowed_tools:
                flow_log("agent.tool.allowed", incident_id=incident.id, task_id=task.id, role=task.role.value, tool=tool_name)
        for execution in prepared.executions:
            flow_log(
                "agent.execution.planned",
                incident_id=incident.id,
                task_id=execution.task_id,
                role=execution.role.value,
                reasoning_summary=execution.reasoning_summary,
                repair_steps=[to_jsonable(step) for step in execution.repair_steps],
            )
        for execution in prepared.executions:
            execution.metadata["repo_workspace"] = {
                "root": str(workspace.root),
                "source": workspace.source,
                "repo_url": workspace.repo_url,
                "branch": workspace.branch,
                "revision": workspace.revision,
            }
        repo_path = workspace.root.resolve()
        job_id = f"repair-{incident.id}-{iso_now().replace(':', '-').replace('+00:00', 'z')}"
        flow_log("platform.repair.sandbox_create.start", incident_id=incident.id, repo_path=repo_path, job_id=job_id)
        sandbox = GitSandbox.create(repo_path, self.config.paths.sandboxes_dir, job_id, self.config.sandbox)
        flow_log(
            "platform.repair.sandbox_create.done",
            incident_id=incident.id,
            sandbox_path=sandbox.sandbox_path,
            baseline_commit=sandbox.baseline_commit,
            runtime_backend=sandbox.runtime_backend,
            degraded_reason=sandbox.degraded_reason,
        )

        baseline_commands = self.validation_runner.infer_commands(prepared.incident, sandbox.sandbox_path)
        flow_log("platform.repair.baseline_validation.start", incident_id=incident.id, commands=baseline_commands)
        baseline_validation = self.validation_runner.run(sandbox, baseline_commands)
        flow_log(
            "platform.repair.baseline_validation.done",
            incident_id=incident.id,
            success=baseline_validation.success,
            duration_seconds=baseline_validation.duration_seconds,
        )
        attempts_limit = max_attempts or self.config.sandbox.max_repair_attempts

        final_validation = baseline_validation
        patch_plan = None
        validation_commands = baseline_commands
        rollback_performed = False
        commit_hash = None
        diff_summary = "Validation never reached a committed change."
        llm_token_usage = 0
        success = False
        previous_failure = baseline_validation.combined_output
        attempts = 0
        repair_error = ""

        for attempt in range(1, attempts_limit + 1):
            attempts = attempt
            try:
                flow_log("platform.repair.attempt.generate_patch.start", incident_id=incident.id, attempt=attempt)
                patch_plan = self.patch_executor.generate_patch(
                    prepared=prepared,
                    repo_root=sandbox.sandbox_path,
                    validation_commands=baseline_commands,
                    previous_failure=previous_failure,
                    attempt=attempt,
                )
                llm_token_usage += patch_plan.usage.get("total_tokens", 0)
                flow_log(
                    "platform.repair.attempt.generate_patch.done",
                    incident_id=incident.id,
                    attempt=attempt,
                    edit_count=len(patch_plan.edits),
                    validation_commands=patch_plan.validation_commands,
                    token_usage=patch_plan.usage,
                )
                flow_log("platform.repair.attempt.apply_patch.start", incident_id=incident.id, attempt=attempt)
                sandbox.apply_patch_plan(patch_plan)
                flow_log("platform.repair.attempt.apply_patch.done", incident_id=incident.id, attempt=attempt)
            except (PatchExecutorError, ValueError, RuntimeError) as exc:
                repair_error = f"Patch generation or application failed on attempt {attempt}: {exc}"
                flow_log("platform.repair.attempt.patch_failed", incident_id=incident.id, attempt=attempt, error=repair_error)
                final_validation = ValidationResult(
                    commands=validation_commands,
                    success=False,
                    command_results=[
                        {
                            "command": "patch_generation",
                            "returncode": -1,
                            "success": False,
                            "stdout": "",
                            "stderr": repair_error,
                            "sandbox_backend": getattr(sandbox, "runtime_backend", "workspace"),
                        }
                    ],
                    combined_output=repair_error,
                    duration_seconds=0.0,
                )
                rollback_performed = True
                try:
                    flow_log("platform.repair.rollback.start", incident_id=incident.id, attempt=attempt)
                    sandbox.rollback()
                    flow_log("platform.repair.rollback.done", incident_id=incident.id, attempt=attempt)
                except Exception:
                    flow_log("platform.repair.rollback.failed", incident_id=incident.id, attempt=attempt)
                    pass
                previous_failure = repair_error
                continue
            validation_commands = self.validation_runner.infer_commands(
                prepared.incident,
                sandbox.sandbox_path,
                patch_plan.validation_commands,
            )
            flow_log("platform.repair.attempt.validation.start", incident_id=incident.id, attempt=attempt, commands=validation_commands)
            final_validation = self.validation_runner.run(sandbox, validation_commands)
            flow_log(
                "platform.repair.attempt.validation.done",
                incident_id=incident.id,
                attempt=attempt,
                success=final_validation.success,
                duration_seconds=final_validation.duration_seconds,
            )
            if final_validation.success:
                flow_log("platform.repair.attempt.commit.start", incident_id=incident.id, attempt=attempt)
                commit_hash = sandbox.commit(patch_plan.commit_message)
                flow_log("platform.repair.attempt.commit.done", incident_id=incident.id, attempt=attempt, commit_hash=commit_hash)
                diff_summary = sandbox.diff_summary()
                flow_log("platform.repair.attempt.diff_summary", incident_id=incident.id, attempt=attempt, diff_summary=diff_summary)
                success = True
                break
            rollback_performed = True
            flow_log("platform.repair.rollback.start", incident_id=incident.id, attempt=attempt)
            sandbox.rollback()
            flow_log("platform.repair.rollback.done", incident_id=incident.id, attempt=attempt)
            previous_failure = final_validation.combined_output

        if not success:
            diff_summary = repair_error or "Validation failed after applying generated patches; sandbox was rolled back to baseline."
            flow_log("platform.repair.failed", incident_id=incident.id, attempts=attempts, diff_summary=diff_summary)

        trace_id = f"trace-{prepared.incident.id}-{iso_now().replace(':', '-').replace('+00:00', 'z')}"
        report_path_hint = self.config.paths.reports_dir / f"{trace_id}.md"
        delivery: dict[str, Any] = {}
        if success and commit_hash:
            if remote_delivery_confirmed:
                flow_log("platform.repair.remote_delivery.confirmed", incident_id=incident.id, trace_id=trace_id)
                delivery = self.remote_delivery.deliver(
                    prepared=prepared,
                    sandbox_path=sandbox.sandbox_path,
                    commit_hash=commit_hash,
                    diff_summary=diff_summary,
                    report_path=str(report_path_hint),
                )
            else:
                flow_log("platform.repair.remote_delivery.pending_confirmation", incident_id=incident.id, trace_id=trace_id)
                delivery = {
                    "status": "skipped",
                    "reason": "remote_delivery_not_confirmed",
                    "requires_confirmation": True,
                }
        elif success:
            flow_log("platform.repair.remote_delivery.skipped_no_commit", incident_id=incident.id, trace_id=trace_id)

        repair_summary = (
            patch_plan.summary
            if patch_plan is not None and patch_plan.summary.strip()
            else self._repair_summary(prepared.repair_steps, prepared.compressed_error, prepared.relevant_files)
        )
        trace = self._build_trace(
            prepared=prepared,
            success=success,
            token_usage=sum(execution.token_estimate for execution in prepared.executions) + llm_token_usage,
            repair_summary=repair_summary,
            execution_mode="repair",
            validation_passed=final_validation.success,
            validation_commands=validation_commands,
            sandbox_path=str(sandbox.sandbox_path),
            commit_hash=commit_hash,
            rollback_performed=rollback_performed,
            diff_summary=diff_summary,
            attempt_count=attempts,
            llm_model=patch_plan.model if patch_plan is not None else self.config.openai.model,
            approvals_used=self._approval_payloads(remote_approvals, baseline_validation, final_validation),
            delivery=delivery,
            trace_id=trace_id,
        )
        flow_log("platform.repair.trace_built", incident_id=incident.id, trace_id=trace_id, success=success, delivery=delivery)
        report_path = self._persist_trace(trace)
        flow_log("platform.repair.trace_persisted", incident_id=incident.id, trace_id=trace_id, report_path=report_path)
        outcome = RepairOutcome(
            trace=trace,
            success=success,
            sandbox_path=str(sandbox.sandbox_path),
            baseline_validation=baseline_validation,
            final_validation=final_validation,
            attempts=attempts,
            rollback_performed=rollback_performed,
            commit_hash=commit_hash,
            diff_summary=diff_summary,
            patch_plan=patch_plan,
            report_path=str(report_path),
            delivery=delivery,
        )
        if not self.config.sandbox.keep_sandboxes:
            flow_log("platform.repair.sandbox_cleanup.start", incident_id=incident.id, sandbox_path=sandbox.sandbox_path)
            sandbox.cleanup()
            flow_log("platform.repair.sandbox_cleanup.done", incident_id=incident.id, sandbox_path=sandbox.sandbox_path)
        flow_log(
            "platform.repair.done",
            incident_id=incident.id,
            trace_id=trace_id,
            success=success,
            validation_success=final_validation.success,
            commit_hash=commit_hash,
            delivery=delivery,
        )
        return outcome

    def repair_incident_file(
        self,
        incident_path: str | Path,
        repo_root: str | Path | None = None,
        max_attempts: int | None = None,
        remote_delivery_confirmed: bool = False,
    ) -> RepairOutcome:
        incident = self.load_incident(incident_path)
        return self.repair_incident(
            incident=incident,
            repo_root=repo_root,
            max_attempts=max_attempts,
            remote_delivery_confirmed=remote_delivery_confirmed,
        )

    def deliver_repair_trace(self, trace_id: str) -> dict[str, Any]:
        flow_log("platform.deliver_trace.start", trace_id=trace_id)
        payload = self.memory_store.trace_payload(trace_id)
        if payload is None:
            flow_log("platform.deliver_trace.missing_trace", trace_id=trace_id)
            raise KeyError(f"Trace `{trace_id}` was not found.")
        if not payload.get("success") or payload.get("execution_mode") != "repair":
            flow_log(
                "platform.deliver_trace.invalid_trace",
                trace_id=trace_id,
                success=payload.get("success"),
                execution_mode=payload.get("execution_mode"),
            )
            raise ValueError(f"Trace `{trace_id}` is not a successful repair trace.")
        commit_hash = str(payload.get("commit_hash") or "").strip()
        sandbox_path = str(payload.get("sandbox_path") or "").strip()
        if not commit_hash or not sandbox_path:
            flow_log("platform.deliver_trace.missing_commit", trace_id=trace_id, commit_hash=commit_hash, sandbox_path=sandbox_path)
            raise ValueError(f"Trace `{trace_id}` is missing sandbox commit information.")
        sandbox_root = Path(sandbox_path)
        if not sandbox_root.exists():
            delivery = {
                "status": "failed",
                "reason": "sandbox_path_missing",
                "sandbox_path": sandbox_path,
            }
            self._persist_delivery_update(trace_id, delivery)
            flow_log("platform.deliver_trace.sandbox_missing", trace_id=trace_id, delivery=delivery)
            return delivery
        prepared = self._prepared_from_trace_payload(payload)
        report_path = str(self.config.paths.reports_dir / f"{trace_id}.md")
        flow_log(
            "platform.deliver_trace.remote_delivery.start",
            trace_id=trace_id,
            incident_id=prepared.incident.id,
            repo_url=prepared.incident.metadata.get("repo_url"),
            sandbox_path=sandbox_root,
        )
        delivery = self.remote_delivery.deliver(
            prepared=prepared,
            sandbox_path=sandbox_root,
            commit_hash=commit_hash,
            diff_summary=str(payload.get("diff_summary") or ""),
            report_path=report_path,
        )
        self._persist_delivery_update(trace_id, delivery)
        flow_log("platform.deliver_trace.done", trace_id=trace_id, incident_id=prepared.incident.id, delivery=delivery)
        return delivery

    def run_dream_loop(self) -> dict[str, object]:
        return self.dream_loop.run()

    def metrics_summary(self) -> dict[str, Any]:
        return self.metrics_collector.summary()

    def learning_status(self) -> dict[str, Any]:
        return self.auto_learning.status()

    def learning_jobs(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self.memory_store.learning_jobs(status=status, limit=limit)

    def retry_learning_job(
        self,
        trace_id: str,
        *,
        actor: str,
        reason: str,
        priority: int | None = None,
    ) -> dict[str, Any]:
        job = self.memory_store.retry_learning_job(trace_id, actor=actor, reason=reason, priority=priority)
        if job is None:
            raise KeyError(f"Learning job '{trace_id}' was not found.")
        self.memory_store.update_trace_payload(
            trace_id,
            {
                "auto_learning_status": "queued",
                "auto_learning_report": {"manual_recovery": {"action": "retry", "actor": actor, "reason": reason}},
            },
        )
        self.audit_logger.record(
            "dream_loop",
            "manual_retry",
            trace_id,
            payload={"actor": actor, "reason": reason, "priority": priority},
        )
        return job

    def dismiss_learning_job(self, trace_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        job = self.memory_store.dismiss_learning_job(trace_id, actor=actor, reason=reason)
        if job is None:
            raise KeyError(f"Learning job '{trace_id}' was not found.")
        self.memory_store.update_trace_payload(
            trace_id,
            {
                "auto_learning_status": "dismissed",
                "auto_learning_report": {"manual_recovery": {"action": "dismiss", "actor": actor, "reason": reason}},
            },
        )
        self.audit_logger.record(
            "dream_loop",
            "manual_dismiss",
            trace_id,
            payload={"actor": actor, "reason": reason},
        )
        return job

    def run_learning_daemon_once(self) -> dict[str, Any]:
        return self.auto_learning.run_once()

    def run_learning_daemon_forever(self, poll_seconds: float | None = None, max_iterations: int | None = None) -> None:
        self.auto_learning.run_forever(poll_seconds=poll_seconds, max_iterations=max_iterations)

    def shutdown_learning_daemon(self) -> None:
        self.auto_learning.shutdown()

    def wait_for_learning_idle(self, timeout_seconds: float = 10.0) -> bool:
        return self.auto_learning.wait_until_idle(timeout_seconds)

    def list_skills(self) -> list[dict[str, Any]]:
        return [to_jsonable(skill) for skill in self.skill_repository.all_skills()]

    def write_report(self, trace: ExecutionTrace) -> Path:
        report_path = self.config.paths.reports_dir / f"{trace.id}.md"
        report_path.write_text(self._render_report(trace), encoding="utf-8")
        return report_path

    def _persist_trace(self, trace: ExecutionTrace) -> Path:
        auto_learning_requested = (
            trace.success
            and trace.execution_mode == "repair"
            and self.config.learning.auto_run_on_successful_repair
        )
        if auto_learning_requested:
            trace.auto_learning_status = "queued" if self.auto_learning.async_enabled else "running"
        self.memory_store.append_trace(trace)
        self.memory_store.append_collaboration_memories(trace)
        self.skill_repository.record_outcomes(trace.matched_skill_ids, trace.success)
        if auto_learning_requested:
            try:
                result = self.auto_learning.submit(trace.id, priority=self.config.learning.repair_priority, source="repair_success")
                if result.get("status") == "completed":
                    trace.auto_learning_status = "completed"
                    trace.auto_learning_report = dict(result.get("report", {}))
                    self.memory_store.update_trace_payload(
                        trace.id,
                        {
                            "auto_learning_status": trace.auto_learning_status,
                            "auto_learning_report": trace.auto_learning_report,
                        },
                    )
            except Exception as exc:  # noqa: BLE001
                trace.auto_learning_status = "failed"
                trace.auto_learning_report = {"error": str(exc)}
                self.memory_store.update_trace_payload(
                    trace.id,
                    {
                        "auto_learning_status": trace.auto_learning_status,
                        "auto_learning_report": trace.auto_learning_report,
                    },
                )
        self.metrics_collector.record(trace)
        return self.write_report(trace)

    def _persist_incident(self, incident: IncidentReport) -> Path:
        incident_path = self.config.paths.incidents_dir / f"{incident.id}.json"
        incident_path.write_text(json.dumps(to_jsonable(incident), ensure_ascii=False, indent=2), encoding="utf-8")
        return incident_path

    def _persist_delivery_update(self, trace_id: str, delivery: dict[str, Any]) -> None:
        self.memory_store.update_trace_payload(trace_id, {"delivery": delivery})
        report_path = self.config.paths.reports_dir / f"{trace_id}.md"
        if report_path.exists():
            report = report_path.read_text(encoding="utf-8")
            marker = "\n## Remote Delivery Update\n"
            update = marker + json.dumps(to_jsonable(delivery), ensure_ascii=False, indent=2) + "\n"
            if marker in report:
                report = report.split(marker, 1)[0].rstrip() + update
            else:
                report = report.rstrip() + "\n" + update
            report_path.write_text(report, encoding="utf-8")

    def _prepared_from_trace_payload(self, payload: dict[str, Any]) -> PreparedIncident:
        compressed_payload = dict(payload.get("compressed_error") or {})
        error_type_value = str(compressed_payload.get("error_type") or IncidentType.UNKNOWN.value)
        try:
            error_type = IncidentType(error_type_value)
        except ValueError:
            error_type = IncidentType.UNKNOWN
        compressed_error = CompressedError(
            error_type=error_type,
            error_name=str(compressed_payload.get("error_name") or ""),
            keywords=list(compressed_payload.get("keywords") or []),
            key_stack_frames=list(compressed_payload.get("key_stack_frames") or []),
            root_cause_cluster=str(compressed_payload.get("root_cause_cluster") or "unknown"),
            semantic_summary=str(compressed_payload.get("semantic_summary") or ""),
            confidence=float(compressed_payload.get("confidence") or 0.0),
        )
        return PreparedIncident(
            incident=IncidentReport.from_dict(dict(payload.get("incident") or {})),
            snapshot=RepoSnapshot(
                root_dir="",
                files=[],
                file_summaries={},
                dependency_edges=[],
                call_edges=[],
                language_breakdown={},
            ),
            compressed_error=compressed_error,
            matched_skills=[],
            relevant_files=list(payload.get("context_digest", {}).get("relevant_files") or []),
            context=ContextBundle(
                working_context={},
                short_term_memories=[],
                long_term_memories=[],
                matched_skills=[],
                relevant_files=[],
                budget_allocations={},
                semantic_focus=[],
            ),
            decision=SchedulerDecision(mode=str(payload.get("decision", {}).get("mode") or "repair"), complexity_score=0, reasons=[], tasks=[], budget_breakdown={}),
            executions=[],
            repair_steps=[],
            retrieval_matches=[],
            github_run_id=payload.get("github_run_id"),
        )

    def _hydrate_incident_repo_metadata(self, incident: IncidentReport, workspace) -> None:
        if workspace.repo_url and not str(incident.metadata.get("repo_url") or "").strip():
            incident.metadata["repo_url"] = workspace.repo_url
        if workspace.branch:
            incident.metadata.setdefault("head_branch", workspace.branch)
            incident.metadata.setdefault("ref", workspace.branch)
        if workspace.revision:
            incident.metadata.setdefault("head_sha", workspace.revision)
            incident.metadata.setdefault("sha", workspace.revision)

    def _flatten_repair_steps(self, executions: list) -> list[RepairStep]:
        deduped: list[RepairStep] = []
        seen_titles: set[str] = set()
        for execution in executions:
            for step in execution.repair_steps:
                if step.title in seen_titles:
                    continue
                seen_titles.add(step.title)
                deduped.append(step)
        return deduped

    def _build_trace(
        self,
        prepared: PreparedIncident,
        success: bool,
        token_usage: int,
        repair_summary: str,
        execution_mode: str,
        validation_passed: bool | None = None,
        validation_commands: list[str] | None = None,
        sandbox_path: str | None = None,
        commit_hash: str | None = None,
        rollback_performed: bool = False,
        diff_summary: str | None = None,
        attempt_count: int = 0,
        llm_model: str | None = None,
        approvals_used: list[dict[str, Any]] | None = None,
        delivery: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> ExecutionTrace:
        runtime_backend = None
        repo_workspace = None
        if prepared.executions:
            runtime_backend = str(prepared.executions[0].metadata.get("runtime_backend", self.config.runtime.backend))
            repo_workspace = prepared.executions[0].metadata.get("repo_workspace")
        return ExecutionTrace(
            id=trace_id or f"trace-{prepared.incident.id}-{iso_now().replace(':', '-').replace('+00:00', 'z')}",
            incident=prepared.incident,
            compressed_error=prepared.compressed_error,
            decision=prepared.decision,
            executions=prepared.executions,
            matched_skill_ids=[skill.id for skill in prepared.matched_skills],
            success=success,
            repair_summary=repair_summary,
            token_usage=token_usage,
            context_digest={
                "relevant_files": prepared.relevant_files,
                "semantic_focus": prepared.context.semantic_focus,
                "short_term_memory_count": len(prepared.context.short_term_memories),
                "long_term_memory_count": len(prepared.context.long_term_memories),
                "graph_nodes": prepared.snapshot.graph_metadata.graph_nodes if prepared.snapshot.graph_metadata else 0,
                "graph_edges": prepared.snapshot.graph_metadata.graph_edges if prepared.snapshot.graph_metadata else 0,
                "root_cause_paths": prepared.context.working_context.get("root_cause_paths", []),
                "repo_workspace": repo_workspace or {},
            },
            execution_mode=execution_mode,
            validation_passed=validation_passed,
            validation_commands=validation_commands or [],
            sandbox_path=sandbox_path,
            commit_hash=commit_hash,
            rollback_performed=rollback_performed,
            diff_summary=diff_summary,
            attempt_count=attempt_count,
            llm_model=llm_model,
            github_run_id=prepared.github_run_id,
            retrieval_matches=[to_jsonable(match) for match in prepared.retrieval_matches],
            approvals_used=approvals_used or [],
            agent_runtime_backend=runtime_backend,
            collaboration_blackboard=to_jsonable(self.agent_runtime.collaboration_snapshot(prepared.executions)),
            delivery=to_jsonable(delivery or {}),
        )

    def _agent_reasoning_settings(self) -> dict[str, Any]:
        return {
            "enabled": self.config.agent_reasoning.enabled,
            "provider": self.config.openai.provider,
            "api_key": self.config.openai.api_key,
            "base_url": self.config.openai.base_url,
            "model": self.config.openai.model,
            "timeout_seconds": self.config.openai.timeout_seconds,
            "max_output_tokens": min(self.config.openai.max_output_tokens, 2_000),
            "compatibility_mode": self.config.openai.compatibility_mode,
            "extra_headers": self.config.openai.extra_headers,
            "max_steps": self.config.agent_reasoning.max_steps,
            "max_context_chars": self.config.agent_reasoning.max_context_chars,
            "max_tool_rounds": self.config.agent_reasoning.max_tool_rounds,
        }

    def _repair_summary(self, repair_steps: list[RepairStep], compressed_error, relevant_files: list[str]) -> str:
        if not repair_steps:
            return (
                f"No concrete repair step was generated; further investigation is required for "
                f"{compressed_error.root_cause_cluster} across {', '.join(relevant_files[:3]) or 'the repository'}."
            )
        titles = ", ".join(step.title for step in repair_steps[:3])
        return (
            f"Planned repair for {compressed_error.root_cause_cluster}: {titles}. "
            f"Focus files: {', '.join(relevant_files[:4]) or 'repository-wide validation'}."
        )

    def _estimate_success(self, executions: list, matched_skills) -> bool:
        likelihood = sum(execution.success_likelihood for execution in executions) / max(len(executions), 1)
        if matched_skills:
            likelihood += 0.05
        return likelihood >= 0.62

    def _retrieval_query(self, incident: IncidentReport, compressed_error) -> str:
        return "\n".join(
            [
                incident.title,
                incident.description,
                compressed_error.semantic_summary,
                " ".join(compressed_error.keywords),
                " ".join(incident.changed_files),
                " ".join(incident.suspected_modules),
            ]
        )

    def _merge_skill_matches(self, lexical_skills, semantic_skill_matches) -> list:
        ordered_ids: list[str] = []
        for match in semantic_skill_matches:
            if match.item_id not in ordered_ids:
                ordered_ids.append(match.item_id)
        for skill in lexical_skills:
            if skill.id not in ordered_ids:
                ordered_ids.append(skill.id)
        skill_map = {skill.id: skill for skill in self.skill_repository.all_skills()}
        return [skill_map[skill_id] for skill_id in ordered_ids if skill_id in skill_map][: max(self.config.retrieval.top_k, 4)]

    def _merge_memory_candidates(self, primary: list[dict[str, Any]], secondary: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for memory in primary + secondary:
            memory_id = str(memory.get("memory_id") or memory.get("trace_id", ""))
            if memory_id in seen_ids:
                continue
            seen_ids.add(memory_id)
            merged.append(memory)
        return merged[: self.config.retrieval.max_memory_candidates]

    def _github_run_id(self, incident: IncidentReport) -> int | None:
        value = incident.metadata.get("run_id")
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _approval_payloads(self, remote_approvals, baseline_validation, final_validation) -> list[dict[str, Any]]:
        payloads = self.security_manager.summarize_approvals(remote_approvals)
        for validation in (baseline_validation, final_validation):
            for command_result in validation.command_results:
                payloads.extend(list(command_result.get("approvals_used", [])))
        return payloads

    def _render_report(self, trace: ExecutionTrace) -> str:
        report = [
            f"# Execution Report: {trace.id}",
            "",
            f"- Incident: {trace.incident.title}",
            f"- Error type: {trace.compressed_error.error_type.value}",
            f"- Execution mode: {trace.execution_mode}",
            f"- Decision mode: {trace.decision.mode}",
            f"- Complexity score: {trace.decision.complexity_score}",
            f"- Success estimate: {'success' if trace.success else 'needs_review'}",
            f"- Token usage: {trace.token_usage}",
            f"- Agent runtime backend: {trace.agent_runtime_backend or self.config.runtime.backend}",
            f"- Auto learning status: {trace.auto_learning_status}",
        ]
        if trace.github_run_id is not None:
            report.append(f"- GitHub run id: {trace.github_run_id}")
        if trace.validation_passed is not None:
            report.append(f"- Validation passed: {trace.validation_passed}")
        if trace.commit_hash:
            report.append(f"- Commit hash: {trace.commit_hash}")
        if trace.sandbox_path:
            report.append(f"- Sandbox path: {trace.sandbox_path}")
        report.extend(
            [
                "",
                "## Semantic Summary",
                trace.compressed_error.semantic_summary,
                "",
                "## Scheduler Reasons",
                *[f"- {reason}" for reason in trace.decision.reasons],
                "",
                "## Repair Steps",
            ]
        )
        for execution in trace.executions:
            report.append(f"### {execution.role.value}")
            report.append(execution.reasoning_summary)
            if execution.metadata:
                report.append(f"- Runtime metadata: {json.dumps(execution.metadata, ensure_ascii=False)}")
            for step in execution.repair_steps:
                targets = ", ".join(step.target_files) or "n/a"
                report.append(f"- {step.title}: {step.description} [targets: {targets}]")
            report.append("")
        if trace.retrieval_matches:
            report.append("## Retrieval Matches")
            for match in trace.retrieval_matches[:10]:
                report.append(
                    f"- {match['item_type']}:{match['item_id']} score={match['score']} "
                    f"(semantic={match['semantic_score']}, lexical={match['lexical_score']})"
                )
            report.append("")
        if trace.collaboration_blackboard:
            report.append("## Collaboration Blackboard")
            report.append(json.dumps(trace.collaboration_blackboard, ensure_ascii=False, indent=2))
            report.append("")
        if trace.validation_commands:
            report.append("## Validation Commands")
            report.extend(f"- {command}" for command in trace.validation_commands)
            report.append("")
        if trace.approvals_used:
            report.append("## Approvals Used")
            for approval in trace.approvals_used:
                report.append(f"- {json.dumps(approval, ensure_ascii=False)}")
            report.append("")
        if trace.auto_learning_report:
            report.append("## Auto Learning")
            report.append(json.dumps(trace.auto_learning_report, ensure_ascii=False, indent=2))
            report.append("")
        if trace.diff_summary:
            report.append("## Diff Summary")
            report.append(trace.diff_summary)
            report.append("")
        if trace.delivery:
            report.append("## Remote Delivery")
            report.append(json.dumps(trace.delivery, ensure_ascii=False, indent=2))
            report.append("")
        report.append("## Context Digest")
        report.append(json.dumps(trace.context_digest, ensure_ascii=False, indent=2))
        return "\n".join(report)
