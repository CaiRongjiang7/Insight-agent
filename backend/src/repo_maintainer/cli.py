from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

from .config import PlatformConfig
from .llm_executor import PatchExecutorError
from .models import ApprovalScope
from .platform import RepoAutonomyPlatform
from .security import SecurityPolicyError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Repository autonomy maintenance platform")
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser("demo", help="Run the platform on the bundled CI incident example.")
    demo.add_argument("--incident", default="examples/incidents/ci_failure.json")
    demo.add_argument("--repo", default="examples/sample_repo")

    analyze = subparsers.add_parser("analyze", help="Analyze a custom incident file.")
    analyze.add_argument("--incident", required=True)
    analyze.add_argument("--repo", default=".")

    repair = subparsers.add_parser("repair", help="Run the real repair pipeline in a disposable git sandbox.")
    repair.add_argument("--incident", required=True)
    repair.add_argument("--repo", default=".")
    repair.add_argument("--attempts", type=int, default=None)
    repair.add_argument("--push-remote", action="store_true", help="Allow pushing a verified repair branch to the configured remote.")

    github_sync = subparsers.add_parser("github-sync", help="Sync failed GitHub Actions runs into local incidents.")
    github_sync.add_argument("--owner", default=None)
    github_sync.add_argument("--repo", default=None)
    github_sync.add_argument("--branch", default=None)
    github_sync.add_argument("--limit", type=int, default=3)

    github_analyze = subparsers.add_parser("github-analyze", help="Analyze the latest failed GitHub Actions run.")
    github_analyze.add_argument("--owner", default=None)
    github_analyze.add_argument("--repo", default=None)
    github_analyze.add_argument("--branch", default=None)
    github_analyze.add_argument("--local-repo", default=".")

    github_rerun = subparsers.add_parser("github-rerun", help="Trigger a rerun for a GitHub Actions workflow run.")
    github_rerun.add_argument("--run-id", required=True, type=int)
    github_rerun.add_argument("--owner", default=None)
    github_rerun.add_argument("--repo", default=None)
    github_rerun.add_argument("--failed-only", action="store_true")

    ci_sync = subparsers.add_parser("ci-sync", help="Sync failed runs from a CI provider.")
    ci_sync.add_argument("--provider", required=True, choices=["github_actions", "gitlab_ci", "jenkins"])
    ci_sync.add_argument("--owner", default=None)
    ci_sync.add_argument("--repo", default=None)
    ci_sync.add_argument("--branch", default=None)
    ci_sync.add_argument("--project-id", default=None)
    ci_sync.add_argument("--job-name", default=None)
    ci_sync.add_argument("--limit", type=int, default=3)

    ci_analyze = subparsers.add_parser("ci-analyze", help="Analyze the latest failed run from a CI provider.")
    ci_analyze.add_argument("--provider", required=True, choices=["github_actions", "gitlab_ci", "jenkins"])
    ci_analyze.add_argument("--owner", default=None)
    ci_analyze.add_argument("--repo", default=None)
    ci_analyze.add_argument("--branch", default=None)
    ci_analyze.add_argument("--project-id", default=None)
    ci_analyze.add_argument("--job-name", default=None)
    ci_analyze.add_argument("--local-repo", default=".")

    ci_rerun = subparsers.add_parser("ci-rerun", help="Trigger a rerun for a CI workflow run.")
    ci_rerun.add_argument("--provider", required=True, choices=["github_actions", "gitlab_ci", "jenkins"])
    ci_rerun.add_argument("--run-id", required=True)
    ci_rerun.add_argument("--owner", default=None)
    ci_rerun.add_argument("--repo", default=None)
    ci_rerun.add_argument("--project-id", default=None)
    ci_rerun.add_argument("--job-name", default=None)
    ci_rerun.add_argument("--failed-only", action="store_true")

    subparsers.add_parser("serve", help="Run the FastAPI backend service.")

    approve = subparsers.add_parser("approve", help="Grant a local approval for a protected action.")
    approve.add_argument("--scope", required=True, choices=[scope.value for scope in ApprovalScope])
    approve.add_argument("--subject", required=True)
    approve.add_argument("--actor", default="local-user")
    approve.add_argument("--reason", default="")
    approve.add_argument("--ttl-hours", type=int, default=None)

    subparsers.add_parser("approvals", help="List active approvals.")
    audit = subparsers.add_parser("audit", help="Show recent audit events.")
    audit.add_argument("--limit", type=int, default=20)

    subparsers.add_parser("dream", help="Run Dream + Learn Loop on stored successful traces.")
    learning_daemon = subparsers.add_parser("learning-daemon", help="Run the persistent Dream + Learn daemon worker.")
    learning_daemon.add_argument("--once", action="store_true")
    learning_daemon.add_argument("--poll-seconds", type=float, default=None)
    learning_daemon.add_argument("--max-iterations", type=int, default=None)

    learning_status = subparsers.add_parser("learning-status", help="Show Dream + Learn queue and lease status.")
    learning_status.add_argument("--limit", type=int, default=20)

    learning_jobs = subparsers.add_parser("learning-jobs", help="List queued, dead-letter, or completed learning jobs.")
    learning_jobs.add_argument("--status", default=None)
    learning_jobs.add_argument("--limit", type=int, default=50)

    learning_retry = subparsers.add_parser("learning-retry", help="Retry a dead-letter learning job after manual review.")
    learning_retry.add_argument("--trace-id", required=True)
    learning_retry.add_argument("--actor", default="local-user")
    learning_retry.add_argument("--reason", required=True)
    learning_retry.add_argument("--priority", type=int, default=None)

    learning_dismiss = subparsers.add_parser("learning-dismiss", help="Dismiss a dead-letter learning job after manual review.")
    learning_dismiss.add_argument("--trace-id", required=True)
    learning_dismiss.add_argument("--actor", default="local-user")
    learning_dismiss.add_argument("--reason", required=True)

    subparsers.add_parser("metrics", help="Show observability summary.")
    subparsers.add_parser("skills", help="List skill inventory.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    base_root = Path(__file__).resolve().parents[2]
    base_config = PlatformConfig.default(base_root)
    if args.command == "learning-daemon":
        base_config = replace(
            base_config,
            learning=replace(base_config.learning, embedded_workers_enabled=False),
        )
    platform = RepoAutonomyPlatform(base_config)

    if args.command in {"demo", "analyze"}:
        trace = platform.analyze_incident_file(incident_path=Path(args.incident), repo_root=Path(args.repo))
        payload = {
            "trace_id": trace.id,
            "incident": trace.incident.title,
            "error_type": trace.compressed_error.error_type.value,
            "decision_mode": trace.decision.mode,
            "complexity_score": trace.decision.complexity_score,
            "success": trace.success,
            "repair_summary": trace.repair_summary,
            "matched_skill_ids": trace.matched_skill_ids,
            "report_path": str(platform.config.paths.reports_dir / f"{trace.id}.md"),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "repair":
        try:
            outcome = platform.repair_incident_file(
                incident_path=Path(args.incident),
                repo_root=Path(args.repo),
                max_attempts=args.attempts,
                remote_delivery_confirmed=args.push_remote,
            )
        except PatchExecutorError as exc:
            print(
                json.dumps(
                    {
                        "success": False,
                        "error": str(exc),
                        "hint": f"Set {platform.config.llm.api_key_env} before running `repair`.",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 2
        except SecurityPolicyError as exc:
            print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False, indent=2))
            return 3
        payload = {
            "trace_id": outcome.trace.id,
            "incident": outcome.trace.incident.title,
            "success": outcome.success,
            "attempts": outcome.attempts,
            "validation_passed": outcome.final_validation.success,
            "commit_hash": outcome.commit_hash,
            "sandbox_path": outcome.sandbox_path,
            "diff_summary": outcome.diff_summary,
            "report_path": outcome.report_path,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if outcome.success else 1

    if args.command == "github-sync":
        sync = platform.sync_github_failures(owner=args.owner, repo=args.repo, branch=args.branch, limit=args.limit)
        payload = {
            "owner": sync.owner,
            "repo": sync.repo,
            "run_count": len(sync.runs),
            "incident_ids": [incident.id for incident in sync.incidents],
            "run_ids": [run.run_id for run in sync.runs],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "ci-sync":
        sync = platform.sync_ci_failures(
            args.provider,
            owner=args.owner,
            repo=args.repo,
            branch=args.branch,
            project_id=args.project_id,
            job_name=args.job_name,
            limit=args.limit,
        )
        payload = {
            "provider": sync.provider,
            "run_count": len(sync.runs),
            "incident_ids": [incident.id for incident in sync.incidents],
            "run_ids": [run.run_id for run in sync.runs],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "github-analyze":
        trace, incident = platform.analyze_latest_github_failure(
            owner=args.owner,
            repo=args.repo,
            branch=args.branch,
            local_repo_root=Path(args.local_repo),
        )
        payload = {
            "incident_id": incident.id,
            "trace_id": trace.id,
            "github_run_id": trace.github_run_id,
            "success": trace.success,
            "report_path": str(platform.config.paths.reports_dir / f"{trace.id}.md"),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "ci-analyze":
        trace, incident = platform.analyze_latest_ci_failure(
            args.provider,
            owner=args.owner,
            repo=args.repo,
            branch=args.branch,
            project_id=args.project_id,
            job_name=args.job_name,
            local_repo_root=Path(args.local_repo),
        )
        payload = {
            "provider": args.provider,
            "incident_id": incident.id,
            "trace_id": trace.id,
            "success": trace.success,
            "report_path": str(platform.config.paths.reports_dir / f"{trace.id}.md"),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "github-rerun":
        try:
            payload = platform.rerun_github_workflow(
                run_id=args.run_id,
                owner=args.owner,
                repo=args.repo,
                failed_only=args.failed_only,
            )
        except SecurityPolicyError as exc:
            print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False, indent=2))
            return 3
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "ci-rerun":
        try:
            payload = platform.rerun_ci_workflow(
                args.provider,
                run_id=args.run_id,
                owner=args.owner,
                repo=args.repo,
                project_id=args.project_id,
                job_name=args.job_name,
                failed_only=args.failed_only,
            )
        except SecurityPolicyError as exc:
            print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False, indent=2))
            return 3
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "serve":
        from .api import run_dev_server

        run_dev_server()
        return 0

    if args.command == "approve":
        approval = platform.approve(
            scope=ApprovalScope(args.scope),
            subject=args.subject,
            actor=args.actor,
            reason=args.reason,
            ttl_hours=args.ttl_hours,
        )
        print(json.dumps(approval, ensure_ascii=False, indent=2))
        return 0

    if args.command == "approvals":
        print(json.dumps(platform.list_approvals(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "audit":
        print(json.dumps(platform.audit_events(limit=args.limit), ensure_ascii=False, indent=2))
        return 0

    if args.command == "dream":
        print(json.dumps(platform.run_dream_loop(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "learning-daemon":
        try:
            if args.once:
                print(json.dumps(platform.run_learning_daemon_once(), ensure_ascii=False, indent=2))
                return 0
            platform.run_learning_daemon_forever(
                poll_seconds=args.poll_seconds,
                max_iterations=args.max_iterations,
            )
            return 0
        except KeyboardInterrupt:
            platform.shutdown_learning_daemon()
            return 0

    if args.command == "learning-status":
        payload = platform.learning_status()
        payload["jobs"] = platform.learning_jobs(limit=args.limit)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "learning-jobs":
        print(json.dumps(platform.learning_jobs(status=args.status, limit=args.limit), ensure_ascii=False, indent=2))
        return 0

    if args.command == "learning-retry":
        print(
            json.dumps(
                platform.retry_learning_job(
                    args.trace_id,
                    actor=args.actor,
                    reason=args.reason,
                    priority=args.priority,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "learning-dismiss":
        print(
            json.dumps(
                platform.dismiss_learning_job(
                    args.trace_id,
                    actor=args.actor,
                    reason=args.reason,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "metrics":
        print(json.dumps(platform.metrics_summary(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "skills":
        print(json.dumps(platform.list_skills(), ensure_ascii=False, indent=2))
        return 0

    parser.print_help()
    return 1
