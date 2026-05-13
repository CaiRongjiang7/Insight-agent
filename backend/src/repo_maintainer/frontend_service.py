from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from statistics import mean
from typing import Any
from uuid import uuid4

from .config import current_provider_snapshot, provider_catalog
from .flow_debug import flow_log
from .memory import to_jsonable
from .models import IncidentReport, Skill, iso_now
from .platform import RepoAutonomyPlatform


UTC = timezone.utc


def parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


def slugify(value: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in value).strip("-")
    return "-".join(part for part in cleaned.split("-") if part) or f"item-{uuid4().hex[:8]}"


def default_thresholds() -> dict[str, dict[str, float]]:
    return {
        "repair_success_rate": {"warning": 0.78, "danger": 0.62},
        "skill_hit_rate": {"warning": 0.55, "danger": 0.35},
        "avg_retrieval_score": {"warning": 0.18, "danger": 0.08},
        "auto_dream_rate": {"warning": 0.45, "danger": 0.2},
        "avg_token_usage": {"warning": 900.0, "danger": 1800.0},
        "avg_task_chain_length": {"warning": 2.8, "danger": 4.0},
    }


class FrontendDataService:
    def __init__(self, platform: RepoAutonomyPlatform) -> None:
        self.platform = platform
        self.config = platform.config
        self.bootstrap()

    def bootstrap(self) -> None:
        if not self.config.paths.repos_path.exists():
            self._save_json(self.config.paths.repos_path, [])
        if not self.repair_jobs_path.exists():
            self._save_json(self.repair_jobs_path, [])
        if not self.config.paths.thresholds_path.exists():
            self._save_json(self.config.paths.thresholds_path, default_thresholds())
        if self.config.api.auto_bootstrap_demo:
            self._ensure_demo_incidents()
        if self.config.api.auto_bootstrap_demo and not self.platform.memory_store.trace_payloads(limit=1):
            sample_repo = self.config.root_dir / "examples" / "sample_repo"
            for incident_path in sorted((self.config.root_dir / "examples" / "incidents").glob("*.json")):
                try:
                    self.platform.analyze_incident_file(incident_path=incident_path, repo_root=sample_repo)
                except Exception:
                    continue
        self.sync_tasks_from_traces()

    @property
    def repair_jobs_path(self) -> Path:
        return self.config.paths.api_data_dir / "repair_jobs.json"

    @property
    def agent_flow_events_path(self) -> Path:
        return self.config.paths.api_data_dir / "agent_flow_events.jsonl"

    def system_status(self) -> dict[str, Any]:
        tasks = self.list_tasks(limit=100, sync=False)
        return {
            "online_agents": max(1, min(self.config.runtime.max_workers, max(len(tasks), 1))),
            "running_tasks": sum(1 for task in tasks if task["status"] in {"queued", "running"}),
            "provider_snapshot": current_provider_snapshot(self.config),
        }

    def notifications(self) -> list[dict[str, Any]]:
        items = []
        for incident in self.list_incidents(limit=4):
            if incident["status"] in {"pending", "processing", "repair_failed"}:
                items.append(
                    {
                        "id": f"incident-{incident['id']}",
                        "level": "danger" if incident["status"] == "repair_failed" else "warning",
                        "title": incident["title"],
                        "message": f"{incident['repo_name']} 当前存在待处理异常任务。",
                        "route": f"/ci-autofix?incidentId={incident['id']}",
                        "created_at": incident["created_at"],
                    }
                )
        return items

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        needle = query.strip().lower()
        if not needle:
            return []
        hits: list[dict[str, Any]] = []
        for repo in self.list_repositories():
            if needle in f"{repo['name']} {repo['owner']}".lower():
                hits.append({"id": repo["id"], "type": "repo", "title": repo["name"], "subtitle": repo["url"], "route": f"/repo-management/detail/{repo['id']}"})
        for incident in self.list_incidents(limit=50):
            if needle in f"{incident['title']} {incident['repo_name']} {incident['error_type_label']}".lower():
                hits.append({"id": incident["id"], "type": "incident", "title": incident["title"], "subtitle": incident["repo_name"], "route": f"/ci-autofix?incidentId={incident['id']}"})
        for task in self.list_tasks(limit=50, sync=False):
            if needle in f"{task['id']} {task['repo_name']} {task['task_type_label']}".lower():
                hits.append({"id": task["id"], "type": "task", "title": task["id"], "subtitle": task["repo_name"], "route": f"/task-scheduling?taskId={task['id']}"})
        for skill in self.list_skills(limit=50):
            if needle in f"{skill['name']} {skill['skill_type_label']}".lower():
                hits.append({"id": skill["id"], "type": "skill", "title": skill["name"], "subtitle": skill["skill_type_label"], "route": f"/skill-library?skillId={skill['id']}"})
        return hits[:limit]

    def list_repositories(self, name: str | None = None, status: str | None = None, agent: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        repos = deepcopy(self._load_json(self.config.paths.repos_path, []))
        incidents = self.list_incidents(limit=200, include_details=False)
        tasks = self.list_tasks(limit=200, sync=False)
        for repo in repos:
            repo["status_label"] = self.repo_status_label(repo.get("status", "normal"))
            repo["ci_status_label"] = self.ci_status_label(repo.get("ci_status", "normal"))
            repo["incident_count"] = sum(1 for item in incidents if item["repo_id"] == repo["id"])
            repo["task_count"] = sum(1 for item in tasks if item["repo_id"] == repo["id"])
        if name:
            repos = [repo for repo in repos if name.lower() in repo["name"].lower()]
        if status:
            repos = [repo for repo in repos if repo.get("status") == status]
        if agent:
            repos = [repo for repo in repos if agent.lower() in repo.get("agent_name", "").lower()]
        repos.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return repos[:limit]

    def get_repository_detail(self, repo_id: str) -> dict[str, Any]:
        repo = self.find_repo(repo_id)
        if repo is None:
            raise KeyError(f"Repository `{repo_id}` was not found.")
        incidents = [item for item in self.list_incidents(limit=200) if item["repo_id"] == repo_id]
        tasks = [item for item in self.list_tasks(limit=200, sync=False) if item["repo_id"] == repo_id]
        snapshot = self.safe_snapshot(repo.get("local_path"))
        return {
            **repo,
            "status_label": self.repo_status_label(repo.get("status", "normal")),
            "ci_status_label": self.ci_status_label(repo.get("ci_status", "normal")),
            "incidents": incidents[:10],
            "tasks": tasks[:10],
            "ci_history": [{"incident_id": item["id"], "status": item["status"], "error_type": item["error_type"], "created_at": item["created_at"]} for item in incidents[:12]],
            "graph_metadata": snapshot.get("graph_metadata", {}),
            "language_breakdown": snapshot.get("language_breakdown", {}),
        }

    def create_repository(self, payload: dict[str, Any]) -> dict[str, Any]:
        repos = self._load_json(self.config.paths.repos_path, [])
        repo = {
            "id": payload.get("id") or slugify(payload.get("name", "repository")),
            "name": payload.get("name", "Unnamed Repository"),
            "url": payload.get("url", ""),
            "owner": payload.get("owner", "Unknown"),
            "status": payload.get("status", "normal"),
            "ci_status": payload.get("ci_status", "normal"),
            "health_score": int(payload.get("health_score", 88)),
            "agent_name": payload.get("agent_name", "maintainer-orchestrator"),
            "description": payload.get("description", ""),
            "local_path": payload.get("local_path", ""),
            "created_at": iso_now(),
        }
        repos = [item for item in repos if item.get("id") != repo["id"]]
        repos.insert(0, repo)
        self._save_json(self.config.paths.repos_path, repos)
        return repo

    def update_repository(self, repo_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        repos = self._load_json(self.config.paths.repos_path, [])
        updated = None
        for repo in repos:
            if repo.get("id") == repo_id:
                repo.update({key: value for key, value in payload.items() if value is not None})
                updated = repo
                break
        if updated is None:
            raise KeyError(f"Repository `{repo_id}` was not found.")
        self._save_json(self.config.paths.repos_path, repos)
        return updated

    def delete_repository(self, repo_id: str) -> None:
        repos = [item for item in self._load_json(self.config.paths.repos_path, []) if item.get("id") != repo_id]
        self._save_json(self.config.paths.repos_path, repos)

    def list_incidents(
        self,
        limit: int = 50,
        incident_type: str | None = None,
        status: str | None = None,
        repo_id: str | None = None,
        provider: str | None = None,
        keyword: str | None = None,
        include_details: bool = True,
    ) -> list[dict[str, Any]]:
        traces = self.latest_trace_map()
        items: list[dict[str, Any]] = []
        for incident_path in sorted(self.config.paths.incidents_dir.glob("*.json"), reverse=True):
            raw = json.loads(incident_path.read_text(encoding="utf-8"))
            incident = IncidentReport.from_dict(raw)
            # 将顶层字段暂存到 metadata 中（from_dict 不识别这些新字段）
            if "review_status" in raw:
                incident.metadata["review_status"] = raw["review_status"]
            if "review_document" in raw:
                incident.metadata["_review_document"] = raw["review_document"]
            if "extraction_confidence" in raw:
                incident.metadata["_extraction_confidence"] = raw["extraction_confidence"]
            trace = traces.get(incident.id)
            repo = self.resolve_repo_for_incident(incident)
            items.append(self.serialize_incident(incident, trace, repo, include_details))
        if incident_type:
            items = [item for item in items if item["error_type"] == incident_type]
        if status:
            items = [item for item in items if item["status"] == status]
        if repo_id:
            items = [item for item in items if item["repo_id"] == repo_id]
        if provider:
            items = [item for item in items if item.get("provider") == provider]
        if keyword:
            needle = keyword.strip().lower()
            if needle:
                items = [
                    item
                    for item in items
                    if needle in " ".join(
                        [
                            str(item.get("id", "")),
                            str(item.get("title", "")),
                            str(item.get("repo_name", "")),
                            str(item.get("provider_label", "")),
                            str(item.get("error_type_label", "")),
                        ]
                    ).lower()
                ]
        items.sort(key=lambda item: item["created_at"], reverse=True)
        return items[:limit]

    def get_incident_detail(self, incident_id: str) -> dict[str, Any]:
        for item in self.list_incidents(limit=500, include_details=True):
            if item["id"] == incident_id:
                return item
        raise KeyError(f"Incident `{incident_id}` was not found.")

    def create_incident(self, repo_id: str | None, data: dict[str, Any]) -> dict[str, Any]:
        """Create a new BugReport / IncidentReport from frontend payload.
        
        Resolves the repo from repo_id, builds an IncidentReport (which extends
        BugReport), and persists to the incidents directory.
        
        All legacy fields (repo_id, error_type, etc.) are stored in metadata
        since BugReport only has: id, title, description, logs, metadata,
        source_type, pr_id, commit_sha, repo_url.
        """
        repo = self.resolve_repo(repo_id) if repo_id else {}
        now = iso_now()
        incident = IncidentReport(
            id=f"bug-{uuid4().hex[:12]}",
            title=data.get("title", ""),
            description=data.get("description", ""),
            logs=data.get("logs_excerpt") or data.get("full_logs", ""),
            source_type=data.get("source_type", "manual"),
            pr_id=data.get("pr_id"),
            commit_sha=data.get("commit_sha"),
            repo_url=data.get("repo_url") or repo.get("url", ""),
            reported_at=data.get("created_at", now),
            metadata={
                "repo_id": repo_id or data.get("repo_id", ""),
                "repo_name": repo.get("name", ""),
                "error_type": data.get("bug_category") or data.get("error_type", "unknown"),
                "bug_category": data.get("bug_category", "unknown"),
                "status": data.get("status", "pending"),
                "semantic_summary": data.get("semantic_summary", ""),
                "logs_excerpt": data.get("logs_excerpt", ""),
                "full_logs": data.get("full_logs", ""),
                "key_stack_frames": data.get("key_stack_frames", []),
                "review_status": data.get("review_status", "draft"),
                "source_type": data.get("source_type", "manual"),
            },
        )
        self._save_json(
            self.config.paths.incidents_dir / f"{incident.id}.json",
            incident.to_dict(),
        )
        return self.serialize_incident(incident, None, repo, include_details=True)

    def update_incident_fields(self, incident_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        """Patch-selectively update fields on an existing incident JSON file.
        
        Reads the existing file, merges update fields (including nested
        review_document), and writes back.
        """
        filepath = self.config.paths.incidents_dir / f"{incident_id}.json"
        if not filepath.exists():
            raise KeyError(f"Incident `{incident_id}` was not found.")
        current = json.loads(filepath.read_text(encoding="utf-8"))
        for key, value in updates.items():
            if value is not None:
                current[key] = value
        self._save_json(filepath, current)
        return self.get_incident_detail(incident_id)

    def resolve_repo(self, repo_id: str) -> dict[str, Any]:
        """Find a repo dict by id, returning empty dict if not found."""
        repos = self._load_json(self.config.paths.repos_path, [])
        for repo in repos:
            if repo.get("id") == repo_id:
                return repo
        return {}

    def list_tasks(self, limit: int = 100, sync: bool = True) -> list[dict[str, Any]]:
        if sync:
            self.sync_tasks_from_traces()
        tasks = self._load_json(self.config.paths.tasks_path, [])
        tasks.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return tasks[:limit]

    def sync_tasks_from_traces(self) -> None:
        existing = {item.get("id"): item for item in self._load_json(self.config.paths.tasks_path, [])}
        tasks: list[dict[str, Any]] = []
        for payload in self.platform.memory_store.trace_payloads(limit=200):
            incident = IncidentReport.from_dict(payload.get("incident", {}))
            repo = self.resolve_repo_for_incident(incident)
            current = existing.get(payload.get("id"), {})
            status = current.get("status") if current.get("status") == "terminated" else self.task_status_from_trace(payload)
            lifecycle_status = self.task_lifecycle_status(payload, status)
            priority = current.get("priority") or self.default_priority(payload)
            tasks.append(
                {
                    "id": payload.get("id"),
                    "incident_id": incident.id,
                    "repo_id": repo.get("id") if repo else "",
                    "repo_name": repo.get("name") if repo else self.repo_label_from_incident(incident),
                    "task_type": payload.get("compressed_error", {}).get("error_type", "unknown"),
                    "task_type_label": self.error_type_label(payload.get("compressed_error", {}).get("error_type", "unknown")),
                    "priority": priority,
                    "priority_label": self.priority_label(priority),
                    "master_agent": "maintainer-orchestrator",
                    "sub_agents": [item.get("role", "") for item in payload.get("executions", []) if item.get("role") != "generalist"],
                    "status": status,
                    "status_label": self.task_status_label(status),
                    "lifecycle_status": lifecycle_status,
                    "lifecycle_status_label": self.task_lifecycle_status_label(lifecycle_status),
                    "token_usage": int(payload.get("token_usage", 0)),
                    "cost_estimate": round(int(payload.get("token_usage", 0)) / 1000.0, 2),
                    "created_at": payload.get("created_at", iso_now()),
                    "runtime_backend": payload.get("agent_runtime_backend", self.config.runtime.backend),
                    "task_chain_length": len(payload.get("executions", [])),
                    "topology": self.task_topology(payload),
                    "trace_id": payload.get("id"),
                    "report_available": self.report_exists(str(payload.get("id", ""))),
                    "delivery": to_jsonable(payload.get("delivery", {})),
                }
            )
        self._save_json(self.config.paths.tasks_path, tasks)

    def update_task_priority(self, task_ids: list[str], priority: str) -> list[dict[str, Any]]:
        tasks = self._load_json(self.config.paths.tasks_path, [])
        updated: list[dict[str, Any]] = []
        for task in tasks:
            if task.get("id") in task_ids:
                task["priority"] = priority
                task["priority_label"] = self.priority_label(priority)
                updated.append(task)
        self._save_json(self.config.paths.tasks_path, tasks)
        return updated

    def terminate_task(self, task_id: str) -> dict[str, Any]:
        tasks = self._load_json(self.config.paths.tasks_path, [])
        for task in tasks:
            if task.get("id") == task_id:
                task["status"] = "terminated"
                task["status_label"] = self.task_status_label("terminated")
                self._save_json(self.config.paths.tasks_path, tasks)
                return task
        raise KeyError(f"Task `{task_id}` was not found.")

    def enqueue_retry_incident(self, incident_id: str, max_attempts: int = 1, remote_delivery_confirmed: bool = False) -> dict[str, Any]:
        job = self._create_repair_job(
            incident_id=incident_id,
            action="retry",
            action_label="重试修复",
            max_attempts=max_attempts,
            remote_delivery_confirmed=remote_delivery_confirmed,
        )
        flow_log("service.repair_job.queued", incident_id=incident_id, job_id=job["id"], action="retry")
        return self._repair_job_submission_payload(job)

    def enqueue_manual_fix_incident(
        self,
        incident_id: str,
        instructions: str,
        max_attempts: int = 1,
        remote_delivery_confirmed: bool = False,
    ) -> dict[str, Any]:
        job = self._create_repair_job(
            incident_id=incident_id,
            action="manual_fix",
            action_label="手动修复",
            max_attempts=max_attempts,
            remote_delivery_confirmed=remote_delivery_confirmed,
            instructions=instructions,
        )
        flow_log("service.repair_job.queued", incident_id=incident_id, job_id=job["id"], action="manual_fix")
        return self._repair_job_submission_payload(job)

    def run_retry_incident_job(self, job_id: str) -> None:
        job = self._update_repair_job(job_id, status="running", started_at=iso_now())
        incident_id = str(job.get("incident_id", ""))
        flow_log("service.repair_job.running", incident_id=incident_id, job_id=job_id, action=job.get("action"))
        try:
            result = self.retry_incident(
                incident_id,
                max_attempts=int(job.get("max_attempts") or 1),
                remote_delivery_confirmed=bool(job.get("remote_delivery_confirmed")),
            )
            status = "succeeded" if result.get("success") else "failed"
            self._update_repair_job(job_id, status=status, finished_at=iso_now(), result=result)
            flow_log("service.repair_job.finished", incident_id=incident_id, job_id=job_id, status=status, result=result)
        except Exception as exc:  # noqa: BLE001 - background job must persist the failure for the UI.
            self._update_repair_job(job_id, status="failed", finished_at=iso_now(), error=str(exc))
            flow_log("service.repair_job.failed", incident_id=incident_id, job_id=job_id, error=str(exc))

    def run_manual_fix_incident_job(self, job_id: str) -> None:
        job = self._update_repair_job(job_id, status="running", started_at=iso_now())
        incident_id = str(job.get("incident_id", ""))
        flow_log("service.repair_job.running", incident_id=incident_id, job_id=job_id, action=job.get("action"))
        try:
            result = self.manual_fix_incident(
                incident_id,
                instructions=str(job.get("instructions") or ""),
                max_attempts=int(job.get("max_attempts") or 1),
                remote_delivery_confirmed=bool(job.get("remote_delivery_confirmed")),
            )
            status = "succeeded" if result.get("success") else "failed"
            self._update_repair_job(job_id, status=status, finished_at=iso_now(), result=result)
            flow_log("service.repair_job.finished", incident_id=incident_id, job_id=job_id, status=status, result=result)
        except Exception as exc:  # noqa: BLE001 - background job must persist the failure for the UI.
            self._update_repair_job(job_id, status="failed", finished_at=iso_now(), error=str(exc))
            flow_log("service.repair_job.failed", incident_id=incident_id, job_id=job_id, error=str(exc))

    def enqueue_deliver_repair_trace(self, trace_id: str) -> dict[str, Any]:
        incident_id = self._incident_id_from_trace(trace_id)
        job = self._create_repair_job(
            incident_id=incident_id,
            action="remote_delivery",
            action_label="远程交付",
            max_attempts=1,
            remote_delivery_confirmed=True,
            trace_id=trace_id,
        )
        flow_log("service.repair_job.queued", incident_id=incident_id, job_id=job["id"], action="remote_delivery", trace_id=trace_id)
        return self._repair_job_submission_payload(job)

    def run_deliver_repair_trace_job(self, job_id: str) -> None:
        job = self._update_repair_job(job_id, status="running", started_at=iso_now())
        incident_id = str(job.get("incident_id", ""))
        trace_id = str(job.get("trace_id") or "")
        flow_log("service.repair_job.running", incident_id=incident_id, job_id=job_id, action=job.get("action"), trace_id=trace_id)
        try:
            result = self.deliver_repair_trace(trace_id)
            status = "succeeded" if result.get("success") else "failed"
            self._update_repair_job(job_id, status=status, finished_at=iso_now(), result=result)
            flow_log("service.repair_job.finished", incident_id=incident_id, job_id=job_id, status=status, result=result)
        except Exception as exc:  # noqa: BLE001 - background job must persist the failure for the UI.
            self._update_repair_job(job_id, status="failed", finished_at=iso_now(), error=str(exc))
            flow_log("service.repair_job.failed", incident_id=incident_id, job_id=job_id, error=str(exc))

    def repair_job(self, job_id: str) -> dict[str, Any]:
        for job in self._load_json(self.repair_jobs_path, []):
            if job.get("id") == job_id:
                return job
        raise KeyError(f"Repair job `{job_id}` was not found.")

    def latest_repair_job_for_incident(self, incident_id: str) -> dict[str, Any] | None:
        jobs = [job for job in self._load_json(self.repair_jobs_path, []) if job.get("incident_id") == incident_id]
        if not jobs:
            return None
        jobs.sort(key=lambda item: parse_dt(str(item.get("updated_at") or item.get("created_at") or "")), reverse=True)
        return jobs[0]

    def agent_workflow(self, incident_id: str) -> dict[str, Any]:
        events = self._agent_flow_events_for_incident(incident_id)
        latest_job = self.latest_repair_job_for_incident(incident_id)
        nodes, links = self._agent_workflow_graph(events)
        running = bool(latest_job and latest_job.get("status") in {"queued", "running"})
        return {
            "incident_id": incident_id,
            "running": running,
            "latest_job": latest_job or {},
            "events": events[-120:],
            "nodes": nodes,
            "links": links,
        }

    def agent_workflow_delta(self, incident_id: str, after_event_index: int = 0) -> dict[str, Any]:
        events = self._agent_flow_events_for_incident(incident_id)
        latest_job = self.latest_repair_job_for_incident(incident_id)
        cursor = max(0, min(after_event_index, len(events)))
        previous_nodes, previous_links = self._agent_workflow_graph(events[:cursor])
        current_nodes, current_links = self._agent_workflow_graph(events)

        previous_node_map = {str(node.get("id")): node for node in previous_nodes}
        previous_link_ids = {str(link.get("id") or self._workflow_link_id(link)) for link in previous_links}

        new_nodes = [node for node in current_nodes if str(node.get("id")) not in previous_node_map]
        node_updates = [
            node
            for node in current_nodes
            if str(node.get("id")) in previous_node_map and self._workflow_node_changed(previous_node_map[str(node.get("id"))], node)
        ]
        new_links = [link for link in current_links if str(link.get("id") or self._workflow_link_id(link)) not in previous_link_ids]
        running = bool(latest_job and latest_job.get("status") in {"queued", "running"})
        return {
            "incident_id": incident_id,
            "from_event_index": cursor,
            "event_count": len(events),
            "running": running,
            "done": bool(latest_job and latest_job.get("status") in {"succeeded", "failed"}),
            "latest_job": latest_job or {},
            "events": events[cursor:],
            "nodes": new_nodes,
            "node_updates": node_updates,
            "links": new_links,
        }

    def retry_incident(self, incident_id: str, max_attempts: int = 1, remote_delivery_confirmed: bool = False) -> dict[str, Any]:
        flow_log(
            "service.retry_incident.start",
            incident_id=incident_id,
            max_attempts=max_attempts,
            remote_delivery_confirmed=remote_delivery_confirmed,
        )
        incident = self.refresh_ci_incident(self.load_runtime_incident(incident_id))
        flow_log("service.retry_incident.incident_loaded", incident_id=incident.id, metadata=incident.metadata)
        outcome = self.platform.repair_incident(
            incident=incident,
            repo_root=None,
            max_attempts=max_attempts,
            remote_delivery_confirmed=remote_delivery_confirmed,
        )
        flow_log(
            "service.retry_incident.platform_done",
            incident_id=incident.id,
            success=outcome.success,
            trace_id=outcome.trace.id,
            commit_hash=outcome.commit_hash,
            delivery=outcome.delivery,
        )
        self.sync_tasks_from_traces()
        remote_delivery_pending = self._remote_delivery_pending(outcome.success, outcome.commit_hash, outcome.delivery)
        flow_log(
            "service.retry_incident.response",
            incident_id=incident.id,
            trace_id=outcome.trace.id,
            remote_delivery_pending=remote_delivery_pending,
        )
        return {
            "success": outcome.success,
            "trace_id": outcome.trace.id,
            "report_path": outcome.report_path,
            "commit_hash": outcome.commit_hash,
            "sandbox_path": outcome.sandbox_path,
            "diff_summary": outcome.diff_summary,
            "delivery": to_jsonable(outcome.delivery),
            "remote_delivery_pending": remote_delivery_pending,
        }

    def manual_fix_incident(
        self,
        incident_id: str,
        instructions: str,
        max_attempts: int = 1,
        remote_delivery_confirmed: bool = False,
    ) -> dict[str, Any]:
        flow_log(
            "service.manual_fix.start",
            incident_id=incident_id,
            max_attempts=max_attempts,
            remote_delivery_confirmed=remote_delivery_confirmed,
        )
        incident = self.refresh_ci_incident(self.load_runtime_incident(incident_id))
        incident.description = f"{incident.description}\n\nManual fix instructions:\n{instructions}".strip()
        incident.metadata["manual_fix_instructions"] = instructions
        self.save_runtime_incident(incident)
        flow_log("service.manual_fix.incident_prepared", incident_id=incident.id, metadata=incident.metadata)
        outcome = self.platform.repair_incident(
            incident=incident,
            repo_root=None,
            max_attempts=max_attempts,
            remote_delivery_confirmed=remote_delivery_confirmed,
        )
        flow_log(
            "service.manual_fix.platform_done",
            incident_id=incident.id,
            success=outcome.success,
            trace_id=outcome.trace.id,
            commit_hash=outcome.commit_hash,
            delivery=outcome.delivery,
        )
        self.sync_tasks_from_traces()
        remote_delivery_pending = self._remote_delivery_pending(outcome.success, outcome.commit_hash, outcome.delivery)
        flow_log(
            "service.manual_fix.response",
            incident_id=incident.id,
            trace_id=outcome.trace.id,
            remote_delivery_pending=remote_delivery_pending,
        )
        return {
            "success": outcome.success,
            "trace_id": outcome.trace.id,
            "report_path": outcome.report_path,
            "commit_hash": outcome.commit_hash,
            "sandbox_path": outcome.sandbox_path,
            "diff_summary": outcome.diff_summary,
            "delivery": to_jsonable(outcome.delivery),
            "remote_delivery_pending": remote_delivery_pending,
        }

    def deliver_repair_trace(self, trace_id: str) -> dict[str, Any]:
        flow_log("service.deliver_repair_trace.start", trace_id=trace_id)
        delivery = self.platform.deliver_repair_trace(trace_id)
        self.sync_tasks_from_traces()
        flow_log("service.deliver_repair_trace.response", trace_id=trace_id, delivery=delivery)
        return {
            "success": delivery.get("status") == "created",
            "trace_id": trace_id,
            "delivery": to_jsonable(delivery),
            "remote_delivery_pending": False,
        }

    def _remote_delivery_pending(self, success: bool, commit_hash: str | None, delivery: dict[str, Any]) -> bool:
        return (
            success
            and bool(commit_hash)
            and str(delivery.get("status") or "") == "skipped"
            and str(delivery.get("reason") or "") == "remote_delivery_not_confirmed"
            and bool(delivery.get("requires_confirmation"))
        )

    def _incident_id_from_trace(self, trace_id: str) -> str:
        payload = self.platform.memory_store.trace_payload(trace_id)
        if payload is None:
            raise KeyError(f"Trace `{trace_id}` was not found.")
        incident_id = str(payload.get("incident", {}).get("id") or "").strip()
        if not incident_id:
            raise ValueError(f"Trace `{trace_id}` is missing incident information.")
        return incident_id

    def terminate_incident(self, incident_id: str) -> dict[str, Any]:
        affected = 0
        tasks = self._load_json(self.config.paths.tasks_path, [])
        for task in tasks:
            if task.get("incident_id") == incident_id and task.get("status") in {"queued", "running"}:
                task["status"] = "terminated"
                task["status_label"] = self.task_status_label("terminated")
                affected += 1
        self._save_json(self.config.paths.tasks_path, tasks)
        return {"terminated_tasks": affected}

    def delete_incidents(self, incident_ids: list[str]) -> dict[str, Any]:
        ids = {str(item) for item in incident_ids if str(item).strip()}
        deleted_incidents = 0
        trace_ids = self.platform.memory_store.trace_ids_for_incidents(sorted(ids))
        for incident_id in ids:
            target = self.config.paths.incidents_dir / f"{incident_id}.json"
            if target.exists():
                target.unlink()
                deleted_incidents += 1
                continue
            for candidate in self.config.paths.incidents_dir.glob("*.json"):
                try:
                    payload = json.loads(candidate.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    continue
                if str(payload.get("id", "")) == incident_id:
                    candidate.unlink()
                    deleted_incidents += 1
                    break
        existing_tasks = self._load_json(self.config.paths.tasks_path, [])
        tasks = [item for item in existing_tasks if str(item.get("incident_id", "")) not in ids]
        self._save_json(self.config.paths.tasks_path, tasks)
        deleted_reports = 0
        for trace_id in trace_ids:
            report_path = self.config.paths.reports_dir / f"{trace_id}.md"
            if report_path.exists():
                report_path.unlink()
                deleted_reports += 1
        deleted_traces = self.platform.memory_store.delete_traces_for_incidents(sorted(ids))
        return {
            "deleted_incidents": deleted_incidents,
            "deleted_traces": deleted_traces,
            "deleted_reports": deleted_reports,
            "deleted_tasks": len(existing_tasks) - len(tasks),
        }

    def report_exists(self, trace_id: str) -> bool:
        return bool(trace_id) and (self.config.paths.reports_dir / f"{trace_id}.md").exists()

    def get_report(self, trace_id: str) -> dict[str, str]:
        report_path = self.config.paths.reports_dir / f"{trace_id}.md"
        if not report_path.exists():
            raise KeyError(f"Report `{trace_id}` was not found.")
        return {"trace_id": trace_id, "markdown": report_path.read_text(encoding="utf-8")}

    def list_skills(self, limit: int = 100) -> list[dict[str, Any]]:
        items = [self.serialize_skill(skill) for skill in self.platform.skill_repository.all_skills()]
        items.sort(key=lambda item: (item["status_order"], -item["usage_count"], item["name"]))
        return items[:limit]

    def get_skill_detail(self, skill_id: str) -> dict[str, Any]:
        skill = self.platform.skill_repository.get(skill_id)
        if skill is None:
            raise KeyError(f"Skill `{skill_id}` was not found.")
        payload = self.serialize_skill(skill)
        payload["reuse_records"] = [trace for trace in self.platform.memory_store.trace_payloads(limit=50) if skill_id in trace.get("matched_skill_ids", [])][:10]
        return payload

    def create_skill(self, payload: dict[str, Any]) -> dict[str, Any]:
        skill = Skill(
            id=payload.get("id") or slugify(payload.get("name", "skill")),
            name=payload.get("name", "Unnamed Skill"),
            description=payload.get("description", ""),
            triggers=list(payload.get("triggers", [])),
            error_types=list(payload.get("error_types", [])),
            action_template=payload.get("action_template", ""),
            active=payload.get("active", True),
        )
        self.platform.skill_repository.upsert(skill)
        return self.serialize_skill(skill)

    def update_skill(self, skill_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        skill = self.platform.skill_repository.get(skill_id)
        if skill is None:
            raise KeyError(f"Skill `{skill_id}` was not found.")
        if payload.get("name") is not None:
            skill.name = str(payload["name"])
        if payload.get("description") is not None:
            skill.description = str(payload["description"])
        if payload.get("action_template") is not None:
            skill.action_template = str(payload["action_template"])
        if payload.get("triggers") is not None:
            skill.triggers = list(payload["triggers"])
        if payload.get("error_types") is not None:
            skill.error_types = list(payload["error_types"])
        self.platform.skill_repository.upsert(skill)
        return self.serialize_skill(skill)

    def retire_skill(self, skill_id: str, reason: str) -> dict[str, Any]:
        skill = self.platform.skill_repository.get(skill_id)
        if skill is None:
            raise KeyError(f"Skill `{skill_id}` was not found.")
        skill.active = False
        skill.description = f"{skill.description}\n\nRetired reason: {reason}".strip()
        self.platform.skill_repository.upsert(skill)
        return self.serialize_skill(skill)

    def restore_skill(self, skill_id: str) -> dict[str, Any]:
        skill = self.platform.skill_repository.get(skill_id)
        if skill is None:
            raise KeyError(f"Skill `{skill_id}` was not found.")
        skill.active = True
        self.platform.skill_repository.upsert(skill)
        return self.serialize_skill(skill)

    def dashboard_overview(self, days: int = 7) -> dict[str, Any]:
        records = self.platform.metrics_collector.records(limit=max(days * 8, 30))
        repos = self.list_repositories()
        incidents = self.list_incidents(limit=5)
        tasks = self.list_tasks(limit=20, sync=False)
        skills = self.list_skills(limit=5)
        today = datetime.now(UTC).date()
        today_records = [record for record in records if parse_dt(record.get("created_at")).date() == today]
        summary = self.platform.metrics_summary()
        trend = self.build_trends(records, max(days, 7))
        return {
            "hero_metrics": [
                {"key": "online_agents", "label": "Agent在线数", "value": self.system_status()["online_agents"], "status": "info", "route": "/task-scheduling"},
                {"key": "current_tasks", "label": "当前任务数", "value": sum(1 for task in tasks if task["status"] in {"queued", "running"}), "status": "warning", "route": "/task-scheduling"},
                {"key": "ci_exceptions", "label": "CI异常数", "value": sum(1 for item in incidents if item["status"] in {"pending", "processing", "repair_failed"}), "status": "danger", "route": "/ci-autofix"},
                {"key": "today_repairs", "label": "今日修复成功数", "value": sum(1 for record in today_records if record.get("success")), "status": "success", "route": "/observability"},
                {"key": "skill_total", "label": "Skill总数量", "value": len(self.platform.skill_repository.all_skills()), "status": "info", "route": "/skill-library"},
                {"key": "today_tokens", "label": "Token今日消耗", "value": int(sum(record.get("token_usage", 0) for record in today_records)), "status": "warning", "route": "/observability"},
            ],
            "health_score": {"value": self.health_score(summary, incidents, repos), "summary": f"当前健康度 {self.health_score(summary, incidents, repos)} 分，修复成功率与 Skill 命中表现稳定。"},
            "trend_bundle": trend,
            "dream_loop": {
                "nodes": [
                    {"name": "任务执行", "value": len(tasks)},
                    {"name": "历史复盘", "value": len(self.platform.memory_store.successful_traces(limit=6))},
                    {"name": "Skill生成", "value": sum(1 for skill in skills if skill["origin"] == "learned")},
                    {"name": "Skill治理", "value": len([skill for skill in skills if skill["status"] != "active"])},
                    {"name": "Skill复用", "value": sum(skill["usage_count"] for skill in skills)},
                    {"name": "优化迭代", "value": max(1, len(records) // 2)},
                ]
            },
            "recent_incidents": incidents,
            "top_skills": skills,
            "system_status": self.system_status(),
            "notifications": self.notifications(),
            "provider_snapshot": current_provider_snapshot(self.config),
        }

    def context_overview(self, repo_id: str | None = None) -> dict[str, Any]:
        repo = self.find_repo(repo_id) if repo_id else (self.list_repositories(limit=1)[0] if self.list_repositories(limit=1) else None)
        traces = self.platform.memory_store.trace_payloads(limit=20)
        snapshot = self.safe_snapshot(repo.get("local_path") if repo else "")
        return {
            "selected_repo_id": repo.get("id") if repo else "",
            "repos": self.list_repositories(limit=20),
            "working_contexts": [
                {
                    "context_id": trace.get("id"),
                    "task_id": trace.get("incident", {}).get("id"),
                    "repo_name": self.repo_name_from_incident(trace.get("incident", {})),
                    "created_at": trace.get("created_at"),
                    "status": "in_use" if trace.get("execution_mode") == "repair" else "idle",
                    "size": len(json.dumps(trace.get("context_digest", {}), ensure_ascii=False)),
                    "payload": trace.get("context_digest", {}),
                }
                for trace in traces[:8]
            ],
            "short_term_contexts": [
                {
                    "context_id": item.get("trace_id"),
                    "agent": "maintainer-orchestrator",
                    "summary": item.get("repair_summary"),
                    "created_at": item.get("created_at"),
                    "expires_at": (parse_dt(item.get("created_at")) + timedelta(days=7)).isoformat(),
                }
                for item in self.platform.memory_store.recent(limit=12)
            ],
            "long_term_contexts": [
                {
                    "context_id": item.get("id"),
                    "repo_name": self.repo_name_from_incident(item.get("incident", {})),
                    "summary": item.get("repair_summary", ""),
                    "created_at": item.get("created_at", ""),
                    "updated_at": item.get("created_at", ""),
                }
                for item in self.platform.memory_store.successful_traces(limit=12)
            ],
            "dynamic_graph": snapshot,
        }

    def clear_expired_contexts(self) -> dict[str, Any]:
        recent = self.platform.memory_store.recent(limit=100)
        expired = [item for item in recent if parse_dt(item.get("created_at")) + timedelta(days=7) < datetime.now(UTC)]
        return {"cleared": len(expired), "mode": "derived-preview"}

    def observability_snapshot(self, days: int = 30) -> dict[str, Any]:
        records = self.platform.metrics_collector.records(limit=max(days * 8, 60))
        summary = self.platform.metrics_summary()
        thresholds = self._load_json(self.config.paths.thresholds_path, default_thresholds())
        return {
            "summary": summary,
            "charts": self.build_trends(records, days),
            "thresholds": thresholds,
            "anomalies": self.detect_anomalies(summary, thresholds),
            "learning_status": self.platform.learning_status(),
        }

    def update_thresholds(self, payload: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
        thresholds = self._load_json(self.config.paths.thresholds_path, default_thresholds())
        for key, values in payload.items():
            thresholds[key] = {
                "warning": float(values.get("warning", thresholds.get(key, {}).get("warning", 0.0))),
                "danger": float(values.get("danger", thresholds.get(key, {}).get("danger", 0.0))),
            }
        self._save_json(self.config.paths.thresholds_path, thresholds)
        return thresholds

    def provider_options(self) -> dict[str, Any]:
        return {"catalog": provider_catalog(), "current": current_provider_snapshot(self.config)}

    def ci_provider_options(self) -> dict[str, Any]:
        providers = [
            {
                "key": "github_actions",
                "label": "GitHub Actions",
                "enabled": self.platform.github_client.is_configured(),
                "defaults": {
                    "owner": self.config.github.default_owner,
                    "repo": self.config.github.default_repo,
                },
            },
            {
                "key": "gitlab_ci",
                "label": "GitLab CI",
                "enabled": self.platform.gitlab_client.is_configured(),
                "defaults": {
                    "project_id": self.config.gitlab.default_project_id,
                },
            },
            {
                "key": "jenkins",
                "label": "Jenkins",
                "enabled": self.platform.jenkins_client.is_configured(),
                "defaults": {
                    "job_name": self.config.jenkins.default_job,
                },
            },
        ]
        default_provider = next((item["key"] for item in providers if item["enabled"]), "github_actions")
        return {"providers": providers, "default": default_provider}

    def list_jenkins_jobs(self) -> list[dict[str, Any]]:
        return self.platform.list_jenkins_jobs()

    def sync_ci_provider(
        self,
        provider: str,
        *,
        owner: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        project_id: str | None = None,
        job_name: str | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        flow_log(
            "service.sync_ci_provider.start",
            provider=provider,
            owner=owner,
            repo=repo,
            branch=branch,
            project_id=project_id,
            job_name=job_name,
            limit=limit,
        )
        result = self.platform.sync_ci_failures(
            provider,
            owner=owner,
            repo=repo,
            branch=branch,
            project_id=project_id,
            job_name=job_name,
            limit=limit,
        )
        flow_log("service.sync_ci_provider.platform_done", provider=result.provider, runs=len(result.runs), incidents=len(result.incidents))
        latest = self.latest_trace_map()
        incidents = [
            self.serialize_incident(incident, latest.get(incident.id), self.resolve_repo_for_incident(incident), include_details=False)
            for incident in result.incidents
        ]
        incidents.sort(key=lambda item: parse_dt(item.get("created_at")), reverse=True)
        return {
            "provider": result.provider,
            "runs": [self.serialize_ci_run(run) for run in result.runs],
            "incidents": incidents[: limit * 4],
            "latest_logs_excerpt": result.latest_logs_excerpt,
        }

    def rerun_ci_run(
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
        return self.platform.rerun_ci_workflow(
            provider,
            run_id=run_id,
            owner=owner,
            repo=repo,
            project_id=project_id,
            job_name=job_name,
            failed_only=failed_only,
        )

    def latest_trace_map(self) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for payload in self.platform.memory_store.trace_payloads(limit=200):
            incident_id = payload.get("incident", {}).get("id")
            if not incident_id:
                continue
            current = latest.get(incident_id)
            if current is None or parse_dt(payload.get("created_at")) > parse_dt(current.get("created_at")):
                latest[incident_id] = payload
        return latest

    def load_runtime_incident(self, incident_id: str) -> IncidentReport:
        path = self.config.paths.incidents_dir / f"{incident_id}.json"
        if not path.exists():
            for candidate in self.config.paths.incidents_dir.glob("*.json"):
                try:
                    payload = json.loads(candidate.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    continue
                if str(payload.get("id", "")) == incident_id:
                    return IncidentReport.from_dict(payload)
            raise KeyError(f"Incident `{incident_id}` was not found.")
        return IncidentReport.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save_runtime_incident(self, incident: IncidentReport) -> None:
        path = self.config.paths.incidents_dir / f"{incident.id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(to_jsonable(incident), ensure_ascii=False, indent=2), encoding="utf-8")

    def refresh_ci_incident(self, incident: IncidentReport) -> IncidentReport:
        provider = str(incident.metadata.get("provider", "")).strip().lower()
        if provider != "jenkins":
            flow_log("service.refresh_ci_incident.skip", incident_id=incident.id, provider=provider)
            return incident
        try:
            flow_log("service.refresh_ci_incident.jenkins.start", incident_id=incident.id, metadata=incident.metadata)
            refreshed = self.platform.jenkins_client.refresh_incident(incident)
        except Exception as exc:
            flow_log("service.refresh_ci_incident.jenkins.failed", incident_id=incident.id, error=str(exc))
            if not str(incident.metadata.get("repo_url") or "").strip():
                raise RuntimeError(
                    "Jenkins incident is missing repository metadata and the refresh from Jenkins failed. "
                    "Make sure the Jenkins job exposes SCM remote URL/build metadata before retrying repair."
                ) from exc
            incident.metadata["ci_refresh_warning"] = str(exc)
            self.save_runtime_incident(incident)
            return incident
        self.save_runtime_incident(refreshed)
        flow_log("service.refresh_ci_incident.jenkins.done", incident_id=refreshed.id, metadata=refreshed.metadata)
        return refreshed

    def resolve_repo_for_incident(self, incident: IncidentReport) -> dict[str, Any] | None:
        repo_id = str(incident.metadata.get("repo_id", "")).strip()
        if repo_id:
            repo = self.find_repo(repo_id)
            if repo is not None:
                return repo
        repo_url = self.normalize_repo_url(str(incident.metadata.get("repo_url", "")).strip())
        repo_slug = self.repo_slug_from_incident(incident)
        for repo in self._load_json(self.config.paths.repos_path, []):
            candidate_url = self.normalize_repo_url(str(repo.get("url", "")).strip())
            if repo_url and candidate_url and repo_url == candidate_url:
                return repo
            if repo_slug and self.normalize_repo_url(str(repo.get("url", "")).strip()).endswith(repo_slug):
                return repo
        return None

    def find_repo(self, repo_id: str | None) -> dict[str, Any] | None:
        if not repo_id:
            return None
        for repo in self._load_json(self.config.paths.repos_path, []):
            if repo.get("id") == repo_id:
                return repo
        return None

    def serialize_incident(self, incident: IncidentReport, trace: dict[str, Any] | None, repo: dict[str, Any] | None, include_details: bool) -> dict[str, Any]:
        error_type = trace.get("compressed_error", {}).get("error_type", "unknown") if trace else incident.metadata.get("error_type", "unknown")
        status = self.incident_status(trace)
        provider = str(incident.metadata.get("provider", "local")).strip() or "local"
        repo_label = repo.get("name") if repo else self.repo_label_from_incident(incident)
        review_status = incident.metadata.get("review_status") or ""
        review_document = incident.metadata.pop("_review_document", None)
        extraction_confidence = incident.metadata.pop("_extraction_confidence", None)
        payload = {
            "source_type": incident.source_type,
            "pr_id": incident.pr_id,
            "commit_sha": incident.commit_sha,
            "id": incident.id,
            "title": incident.title,
            "repo_id": repo.get("id") if repo else "",
            "repo_name": repo_label,
            "review_status": review_status,
            "error_type": error_type,
            "error_type_label": self.error_type_label(error_type),
            "status": status,
            "status_label": self.incident_status_label(status),
            "created_at": incident.reported_at,
            "agent_name": "maintainer-orchestrator",
            "estimated_completion_time": (parse_dt(incident.reported_at) + timedelta(minutes=5)).isoformat(),
            "semantic_summary": trace.get("compressed_error", {}).get("semantic_summary", incident.description) if trace else incident.description,
            "provider": provider,
            "provider_label": self.ci_provider_label(provider),
            "ci_run_id": str(incident.metadata.get("run_id") or incident.metadata.get("pipeline_id") or incident.metadata.get("build_number") or ""),
            "ci_ref": str(incident.metadata.get("head_branch") or incident.metadata.get("ref") or ""),
            "ci_revision": str(incident.metadata.get("head_sha") or incident.metadata.get("sha") or ""),
            "ci_web_url": str(incident.metadata.get("html_url") or incident.metadata.get("web_url") or ""),
            "ci_rerun_target": str(incident.metadata.get("rerun_url") or incident.metadata.get("rerun_target") or ""),
            "ci_subject": "/".join(
                [str(item) for item in [incident.metadata.get("owner"), incident.metadata.get("repo")] if str(item or "").strip()]
            )
            or str(incident.metadata.get("project_id") or incident.metadata.get("job_name") or ""),
            "trace_id": trace.get("id") if trace else "",
            "report_available": self.report_exists(str(trace.get("id", ""))) if trace else False,
            "commit_hash": trace.get("commit_hash") if trace else None,
            "delivery": to_jsonable(trace.get("delivery", {})) if trace else {},
            "remote_delivery_pending": self._remote_delivery_pending(
                bool(trace.get("success")) if trace else False,
                str(trace.get("commit_hash") or "") if trace else None,
                trace.get("delivery", {}) if trace else {},
            ),
        }
        if include_details:
            key_frames = trace.get("compressed_error", {}).get("key_stack_frames", []) if trace else []
            payload.update(
                {
                    "review_document": review_document,
                    "extraction_confidence": extraction_confidence,
                    "description": incident.description,
                    "full_logs": incident.logs,
                    "logs_excerpt": incident.logs[:1500],
                    "changed_files": incident.changed_files,
                    "suspected_modules": incident.suspected_modules,
                    "key_stack_frames": key_frames,
                    "root_cause_cluster": trace.get("compressed_error", {}).get("root_cause_cluster", "") if trace else "",
                    "progress_logs": [
                        {"timestamp": trace.get("created_at"), "step": execution.get("role"), "summary": execution.get("reasoning_summary")}
                        for execution in (trace.get("executions", []) if trace else [])
                    ],
                    "repair_steps": [to_jsonable(step) for execution in (trace.get("executions", []) if trace else []) for step in execution.get("repair_steps", [])],
                    "report_available": self.report_exists(str(trace.get("id", ""))) if trace else False,
                    "delivery": to_jsonable(trace.get("delivery", {})) if trace else {},
                    "root_cause_graph": self.build_root_cause_graph(incident, trace),
                    "provider_metadata": to_jsonable(incident.metadata),
                }
            )
        return payload

    def build_root_cause_graph(self, incident: IncidentReport, trace: dict[str, Any] | None) -> dict[str, Any]:
        compressed = dict(trace.get("compressed_error") or {}) if trace else {}
        context = dict(trace.get("context_digest") or {}) if trace else {}
        nodes: list[dict[str, Any]] = []
        links: list[dict[str, Any]] = []
        seen_nodes: set[str] = set()
        seen_links: set[str] = set()

        def add_node(node_id: str, name: str, category: str, symbol_size: int = 56, **extra: Any) -> str:
            if node_id in seen_nodes:
                return node_id
            seen_nodes.add(node_id)
            nodes.append({"id": node_id, "name": name[:72], "category": category, "symbolSize": symbol_size, **extra})
            return node_id

        def add_link(source: str, target: str, label: str) -> None:
            if source == target:
                return
            link_id = f"{source}->{target}:{label}"
            if link_id in seen_links:
                return
            seen_links.add(link_id)
            links.append({"id": link_id, "source": source, "target": target, "label": label})

        error_type = str(compressed.get("error_type") or incident.metadata.get("error_type") or "unknown")
        error_name = str(compressed.get("error_name") or error_type)
        summary = str(compressed.get("semantic_summary") or incident.description or incident.title)
        cluster = str(compressed.get("root_cause_cluster") or error_type or "unknown")
        keywords = [str(item) for item in compressed.get("keywords", []) if str(item).strip()]
        key_frames = [str(item) for item in compressed.get("key_stack_frames", []) if str(item).strip()]
        relevant_files = [str(item) for item in context.get("relevant_files", []) if str(item).strip()]
        if not relevant_files:
            relevant_files = [str(item) for item in incident.changed_files + incident.suspected_modules if str(item).strip()]

        error_id = add_node("error", error_name or "错误", "error", 72, detail=summary)
        summary_id = add_node("summary", summary[:48] or incident.title, "symptom", 62)
        cluster_id = add_node("cluster", cluster[:54] or "根因聚类", "cause", 68)
        add_link(error_id, summary_id, "表现")
        add_link(summary_id, cluster_id, "归因")

        previous_frame_id = error_id
        for index, frame in enumerate(key_frames[:5]):
            frame_id = add_node(f"frame:{index}", frame, "stack", 50)
            add_link(previous_frame_id, frame_id, "栈帧" if index == 0 else "调用")
            previous_frame_id = frame_id
            frame_file = frame.split(":", 1)[0].strip()
            if frame_file:
                file_id = add_node(f"file:{frame_file}", frame_file, "file", 54)
                add_link(frame_id, file_id, "定位文件")
                add_link(file_id, cluster_id, "指向根因")

        root_paths = [item for item in context.get("root_cause_paths", []) if isinstance(item, dict)]
        for path_index, path_item in enumerate(root_paths[:4]):
            path = [str(item) for item in path_item.get("path", []) if str(item).strip()]
            previous_id = cluster_id
            for file_path in path[:5]:
                file_id = add_node(f"path:{file_path}", file_path, "file", 52)
                add_link(previous_id, file_id, "依赖链" if previous_id == cluster_id else "关联")
                previous_id = file_id
            if path and key_frames:
                add_link(f"path:{path[-1]}", "frame:0", "触发")
            if path_index >= 2 and len(nodes) > 14:
                break

        for file_path in relevant_files[:6]:
            file_id = add_node(f"file:{file_path}", file_path, "file", 52)
            add_link(cluster_id, file_id, "相关文件")

        for index, keyword in enumerate(keywords[:4]):
            keyword_id = add_node(f"keyword:{index}", keyword, "signal", 42)
            add_link(summary_id, keyword_id, "信号")

        if trace:
            for index, execution in enumerate((trace.get("executions") or [])[:2]):
                reasoning = str(execution.get("reasoning_summary") or "").strip()
                if not reasoning:
                    continue
                diagnosis_id = add_node(f"diagnosis:{index}", reasoning[:58], "diagnosis", 50)
                add_link(cluster_id, diagnosis_id, "Agent诊断")

        if len(nodes) <= 3 and incident.logs:
            excerpt = " ".join(line.strip() for line in incident.logs.splitlines() if line.strip())[:80]
            log_id = add_node("log-signal", excerpt or "日志信号", "signal", 48)
            add_link(error_id, log_id, "日志")
            add_link(log_id, cluster_id, "推断")

        return {"nodes": nodes, "links": links}

    def repo_label_from_incident(self, incident: IncidentReport) -> str:
        slug = self.repo_slug_from_incident(incident)
        if slug:
            return slug
        repo_url = str(incident.metadata.get("repo_url", "")).strip()
        if repo_url:
            normalized = self.normalize_repo_url(repo_url)
            return normalized.rsplit("/", 1)[-1] if "/" in normalized else normalized
        return "未关联仓库"

    def repo_slug_from_incident(self, incident: IncidentReport) -> str:
        owner = str(incident.metadata.get("owner", "")).strip()
        repo = str(incident.metadata.get("repo", "")).strip()
        if owner and repo:
            return f"{owner}/{repo}"
        repo_url = self.normalize_repo_url(str(incident.metadata.get("repo_url", "")).strip())
        parts = [item for item in repo_url.split("/") if item]
        if len(parts) >= 2:
            return "/".join(parts[-2:])
        return ""

    def normalize_repo_url(self, value: str) -> str:
        normalized = value.strip().replace("\\", "/").lower()
        if normalized.endswith(".git"):
            normalized = normalized[:-4]
        return normalized.rstrip("/")

    def serialize_ci_run(self, run: Any) -> dict[str, Any]:
        return {
            "provider": getattr(run, "provider", ""),
            "provider_label": self.ci_provider_label(getattr(run, "provider", "")),
            "run_id": getattr(run, "run_id", ""),
            "name": getattr(run, "name", ""),
            "status": getattr(run, "status", ""),
            "conclusion": getattr(run, "conclusion", ""),
            "web_url": getattr(run, "web_url", ""),
            "logs_url": getattr(run, "logs_url", ""),
            "rerun_target": getattr(run, "rerun_target", ""),
            "ref": getattr(run, "ref", ""),
            "revision": getattr(run, "revision", ""),
            "created_at": getattr(run, "created_at", ""),
            "updated_at": getattr(run, "updated_at", ""),
            "metadata": to_jsonable(getattr(run, "metadata", {})),
        }

    def serialize_skill(self, skill: Skill) -> dict[str, Any]:
        status = "retired" if not skill.active else ("needs_optimization" if skill.usage_count > 0 and skill.success_rate < 0.6 else "active")
        return {
            "id": skill.id,
            "name": skill.name,
            "description": skill.description,
            "skill_type": skill.error_types[0] if skill.error_types else "unknown",
            "skill_type_label": self.error_type_label(skill.error_types[0] if skill.error_types else "unknown"),
            "usage_count": skill.usage_count,
            "success_rate": round(skill.success_rate, 3),
            "hit_rate": round(skill.success_rate if skill.usage_count else 0.66, 3),
            "decay_factor": max(0.25, round(1.0 - min(skill.usage_count, 20) * 0.02, 2)),
            "status": status,
            "status_label": {"active": "生效", "needs_optimization": "待优化", "retired": "淘汰"}[status],
            "status_order": {"active": 1, "needs_optimization": 2, "retired": 3}[status],
            "origin": "learned" if skill.id.startswith("learned-") else "core",
            "last_used_at": skill.last_used_at,
            "triggers": skill.triggers,
            "error_types": skill.error_types,
            "action_template": skill.action_template,
            "source_trace_ids": skill.source_trace_ids,
        }

    def safe_snapshot(self, local_path: str | None) -> dict[str, Any]:
        if not local_path:
            return {"nodes": [], "links": [], "graph_metadata": {}, "language_breakdown": {}}
        path = Path(local_path)
        if not path.exists():
            return {"nodes": [], "links": [], "graph_metadata": {}, "language_breakdown": {}}
        try:
            snapshot = self.platform.indexer.index(path)
        except Exception:
            return {"nodes": [], "links": [], "graph_metadata": {}, "language_breakdown": {}}
        return {
            "nodes": [{"id": symbol.symbol_id, "name": symbol.name, "kind": symbol.kind, "file_path": symbol.file_path, "language": symbol.language} for symbol in snapshot.symbols[:40]],
            "links": [{"source": edge.source, "target": edge.target, "edge_type": edge.edge_type} for edge in snapshot.graph_edges[:60]],
            "graph_metadata": to_jsonable(snapshot.graph_metadata),
            "language_breakdown": snapshot.language_breakdown,
        }

    def build_trends(self, records: list[dict[str, Any]], days: int) -> dict[str, Any]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            grouped.setdefault(parse_dt(record.get("created_at")).date().isoformat(), []).append(record)
        labels = sorted(grouped.keys())[-days:] or [datetime.now(UTC).date().isoformat()]
        return {
            "labels": labels,
            "repair_success_rate": [round(sum(1 for item in grouped.get(label, []) if item.get("success")) / max(len(grouped.get(label, [])), 1), 3) for label in labels],
            "skill_hit_rate": [round(sum(1 for item in grouped.get(label, []) if item.get("skill_hits", 0) > 0) / max(len(grouped.get(label, [])), 1), 3) for label in labels],
            "avg_retrieval_score": [round(mean([float(item.get("avg_retrieval_score", 0.0)) for item in grouped.get(label, [])]) if grouped.get(label) else 0.0, 6) for label in labels],
            "agent_llm_reasoning_rate": [round(sum(1 for item in grouped.get(label, []) if int(item.get("llm_reasoned_tasks", 0)) > 0) / max(len(grouped.get(label, [])), 1), 3) for label in labels],
            "auto_dream_rate": [round(sum(1 for item in grouped.get(label, []) if item.get("dream_run_triggered")) / max(len(grouped.get(label, [])), 1), 3) for label in labels],
            "reflection_rate": [round(sum(1 for item in grouped.get(label, []) if int(item.get("reflective_tasks", 0)) > 0) / max(len(grouped.get(label, [])), 1), 3) for label in labels],
            "token_usage": [sum(int(item.get("token_usage", 0)) for item in grouped.get(label, [])) for label in labels],
            "task_chain_length": [round(mean([int(item.get("task_chain_length", 0)) for item in grouped.get(label, [])]) if grouped.get(label) else 0.0, 2) for label in labels],
        }

    def detect_anomalies(self, summary: dict[str, Any], thresholds: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
        anomalies: list[dict[str, Any]] = []
        labels = {
            "repair_success_rate": "修复成功率",
            "skill_hit_rate": "Skill命中率",
            "avg_retrieval_score": "检索平均得分",
            "auto_dream_rate": "自动学习触发率",
            "avg_token_usage": "Token消耗",
            "avg_task_chain_length": "任务链路长度",
        }
        for key, limit in thresholds.items():
            current = float(summary.get(key, 0.0))
            warning = float(limit.get("warning", 0.0))
            danger = float(limit.get("danger", 0.0))
            inverse = key in {"repair_success_rate", "skill_hit_rate", "avg_retrieval_score", "auto_dream_rate"}
            if (inverse and current <= warning) or ((not inverse) and current >= warning):
                anomalies.append(
                    {
                        "metric_key": key,
                        "metric_label": labels.get(key, key),
                        "current_value": current,
                        "warning_threshold": warning,
                        "danger_threshold": danger,
                        "level": "danger" if ((inverse and current <= danger) or ((not inverse) and current >= danger)) else "warning",
                        "created_at": iso_now(),
                    }
                )
        return anomalies

    def health_score(self, summary: dict[str, Any], incidents: list[dict[str, Any]], repos: list[dict[str, Any]]) -> int:
        failure_pressure = sum(1 for item in incidents if item["status"] == "repair_failed")
        unhealthy_repos = sum(1 for item in repos if item.get("status") != "normal")
        score = 35 + int(summary.get("repair_success_rate", 0.0) * 35) + int(summary.get("skill_hit_rate", 0.0) * 15)
        score += max(0, 10 - failure_pressure * 3) + max(0, 10 - unhealthy_repos * 2)
        return max(0, min(100, score))

    def repo_name_from_incident(self, incident: dict[str, Any]) -> str:
        repo = self.find_repo(incident.get("metadata", {}).get("repo_id"))
        return repo.get("name") if repo else "未关联仓库"

    def ci_provider_label(self, provider: str) -> str:
        return {
            "github_actions": "GitHub Actions",
            "gitlab_ci": "GitLab CI",
            "jenkins": "Jenkins",
            "local": "Local / Demo",
        }.get(provider, provider or "Unknown")

    def incident_status(self, trace: dict[str, Any] | None) -> str:
        if trace is None:
            return "pending"
        if trace.get("execution_mode") == "repair":
            if trace.get("validation_passed") is True and trace.get("success"):
                return "repair_success"
            if trace.get("validation_passed") is False:
                return "repair_failed"
            return "processing"
        return "processing" if trace.get("success") else "pending"

    def task_status_from_trace(self, trace: dict[str, Any]) -> str:
        if trace.get("execution_mode") == "repair":
            if trace.get("validation_passed") is True and trace.get("success"):
                return "completed"
            if trace.get("validation_passed") is False:
                return "failed"
            return "running"
        return "completed" if trace.get("success") else "queued"

    def task_lifecycle_status(self, trace: dict[str, Any], status: str) -> str:
        if status == "terminated":
            return "repair_failed"
        if trace.get("execution_mode") == "repair":
            if trace.get("validation_passed") is True and trace.get("success"):
                return "repair_success"
            if trace.get("validation_passed") is False:
                return "repair_failed"
        return "repairing"

    def task_lifecycle_status_label(self, status: str) -> str:
        return {"repairing": "修复中", "repair_success": "修复成功", "repair_failed": "修复失败"}.get(status, status)

    def task_topology(self, trace: dict[str, Any]) -> dict[str, Any]:
        executions = trace.get("executions", [])
        incident_id = trace.get("incident", {}).get("id", trace.get("id", "task"))
        nodes = [
            {"id": f"master:{incident_id}", "name": "主Agent", "category": "master"},
            {"id": f"task:{incident_id}", "name": str(incident_id), "category": "task"},
        ]
        links = [{"source": f"master:{incident_id}", "target": f"task:{incident_id}"}]
        for execution in executions:
            role = str(execution.get("role", "agent"))
            node_id = f"{trace.get('id')}:{role}:{execution.get('task_id', role)}"
            nodes.append({"id": node_id, "name": role, "category": "sub_agent"})
            links.append({"source": f"task:{incident_id}", "target": node_id})
        return {"nodes": nodes, "links": links}

    def default_priority(self, trace: dict[str, Any]) -> str:
        error_type = trace.get("compressed_error", {}).get("error_type", "")
        if error_type in {"cross_module_defect", "dependency_conflict"}:
            return "high"
        if error_type == "test_regression":
            return "medium"
        return "low"

    def repo_status_label(self, status: str) -> str:
        return {"normal": "正常", "abnormal": "异常", "maintenance": "维护中"}.get(status, status)

    def ci_status_label(self, status: str) -> str:
        return {"normal": "正常", "failed": "失败", "pending": "待执行"}.get(status, status)

    def incident_status_label(self, status: str) -> str:
        return {"pending": "待处理", "processing": "处理中", "repair_success": "修复成功", "repair_failed": "修复失败"}.get(status, status)

    def task_status_label(self, status: str) -> str:
        return {"queued": "待调度", "running": "调度中", "completed": "已完成", "failed": "失败", "terminated": "已终止"}.get(status, status)

    def priority_label(self, priority: str) -> str:
        return {"high": "高", "medium": "中", "low": "低"}.get(priority, priority)

    def error_type_label(self, error_type: str) -> str:
        return {"ci_failure": "CI失败", "dependency_conflict": "依赖冲突", "test_regression": "测试回归", "cross_module_defect": "跨模块缺陷", "unknown": "未知问题"}.get(error_type, error_type)

    def _ensure_demo_incidents(self) -> None:
        for source in sorted((self.config.root_dir / "examples" / "incidents").glob("*.json")):
            payload = json.loads(source.read_text(encoding="utf-8"))
            metadata = dict(payload.get("metadata", {}))
            metadata.setdefault("repo_id", "repo-sample")
            payload["metadata"] = metadata
            incident_id = str(payload.get("id", source.stem))
            target = self.config.paths.incidents_dir / f"{incident_id}.json"
            if not target.exists():
                target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _agent_flow_events_for_incident(self, incident_id: str) -> list[dict[str, Any]]:
        if not self.agent_flow_events_path.exists():
            return []
        events: list[dict[str, Any]] = []
        for raw_line in self.agent_flow_events_path.read_text(encoding="utf-8").splitlines()[-5000:]:
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if str(event.get("incident_id") or "") == incident_id:
                events.append(event)
        events.sort(key=lambda item: parse_dt(str(item.get("ts") or "")))
        return events

    def _agent_workflow_graph(self, events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        nodes: list[dict[str, Any]] = [
            {
                "id": "main-agent",
                "name": "主Agent",
                "category": "agent",
                "status": "running" if events and not self._workflow_finished(events) else "completed",
                "symbolSize": 78,
                "x": 0,
                "y": 0,
            }
        ]
        links: list[dict[str, Any]] = []
        seen_nodes = {"main-agent"}
        last_main = "main-agent"
        last_child: dict[str, str] = {}

        for index, event in enumerate(events):
            step = str(event.get("step") or "")
            label = self._workflow_event_label(event)
            if not label:
                continue
            task_id = str(event.get("task_id") or "")
            role = str(event.get("role") or "")
            category = self._workflow_event_category(step)
            status = self._workflow_event_status(step)

            if task_id:
                agent_id = f"agent:{task_id}"
                if agent_id not in seen_nodes:
                    seen_nodes.add(agent_id)
                    nodes.append(
                        {
                            "id": agent_id,
                            "name": f"{role or task_id} 子Agent",
                            "category": "sub_agent",
                            "status": "running" if status == "running" else "completed",
                            "symbolSize": 64,
                        }
                    )
                    links.append({"source": last_main, "target": agent_id, "label": "派生"})
                node_id = f"event:{index}"
                nodes.append({"id": node_id, "name": label, "category": category, "status": status, "symbolSize": 48, "value": event})
                links.append({"source": last_child.get(task_id, agent_id), "target": node_id, "label": self._workflow_edge_label(step)})
                last_child[task_id] = node_id
                continue

            node_id = f"event:{index}"
            nodes.append({"id": node_id, "name": label, "category": category, "status": status, "symbolSize": 50, "value": event})
            links.append({"source": last_main, "target": node_id, "label": self._workflow_edge_label(step)})
            last_main = node_id

        for link in links:
            link.setdefault("id", self._workflow_link_id(link))
        return nodes, links

    def _workflow_finished(self, events: list[dict[str, Any]]) -> bool:
        return any(str(event.get("step") or "") in {"platform.repair.done", "service.repair_job.finished", "service.repair_job.failed"} for event in events)

    def _workflow_link_id(self, link: dict[str, Any]) -> str:
        return f"{link.get('source', '')}->{link.get('target', '')}:{link.get('label', '')}"

    def _workflow_node_changed(self, previous: dict[str, Any], current: dict[str, Any]) -> bool:
        for key in ("name", "category", "status", "symbolSize"):
            if previous.get(key) != current.get(key):
                return True
        return False

    def _workflow_event_label(self, event: dict[str, Any]) -> str:
        step = str(event.get("step") or "")
        mapping = {
            "service.repair_job.queued": "接收修复请求",
            "service.repair_job.running": "开始后台修复",
            "service.refresh_ci_incident.jenkins.start": "刷新Jenkins元数据",
            "service.refresh_ci_incident.jenkins.done": "更新仓库与构建信息",
            "platform.repair.start": "分析任务",
            "repo_checkout.resolve.metadata": "解析仓库地址",
            "repo_checkout.resolve.remote_read_authorize.start": "授权读取远程仓库",
            "repo_checkout.resolve.fetch.start": "拉取远程代码",
            "repo_checkout.resolve.materialize_from_local": "准备仓库工作区",
            "repo_checkout.resolve.refresh_from_local": "刷新仓库工作区",
            "repo_checkout.resolve.done": "定位修复版本",
            "platform.repair.prepare.start": "组装上下文",
            "platform.repair.prepare.done": "完成上下文与技能检索",
            "platform.repair.sandbox_create.start": "创建修复沙箱",
            "platform.repair.sandbox_create.done": "沙箱基线就绪",
            "platform.repair.baseline_validation.start": "运行基线验证",
            "platform.repair.baseline_validation.done": "读取失败信号",
            "platform.repair.attempt.generate_patch.start": "生成修复方案",
            "platform.repair.attempt.generate_patch.done": "生成补丁计划",
            "platform.repair.attempt.apply_patch.start": "写入修复代码",
            "platform.repair.attempt.apply_patch.done": "代码修改完成",
            "platform.repair.attempt.validation.start": "验证修复结果",
            "platform.repair.attempt.validation.done": "验证完成",
            "platform.repair.attempt.commit.start": "提交修复代码",
            "platform.repair.attempt.commit.done": "生成沙箱提交",
            "platform.repair.remote_delivery.pending_confirmation": "等待确认远程交付",
            "platform.repair.remote_delivery.confirmed": "确认上传代码",
            "platform.deliver_trace.remote_delivery.start": "开始远程交付",
            "remote_delivery.start": "读取远程交付目标",
            "remote_delivery.branch_plan": "规划远程分支",
            "remote_delivery.authorize_write.start": "授权写入远程仓库",
            "remote_delivery.authorize_write.ok": "远程写入授权通过",
            "remote_delivery.git_push.start": "执行Git Push",
            "remote_delivery.git_push.ok": "Git Push完成",
            "remote_delivery.git_push.failed": "Git Push失败",
            "remote_delivery.git_checkout_branch.start": "切换修复分支",
            "remote_delivery.git_checkout_branch.ok": "修复分支就绪",
            "remote_delivery.git_push_command.start": "Push远程分支",
            "remote_delivery.git_push_command.ok": "Push成功",
            "remote_delivery.git_push_command.failed": "Push失败",
            "remote_delivery.github_api_fallback.start": "切换API上传",
            "remote_delivery.github_api_fallback.ok": "API上传完成",
            "remote_delivery.github_api.base_sha.start": "读取远程基线",
            "remote_delivery.github_api.base_sha.ok": "远程基线就绪",
            "remote_delivery.github_api.branch.ok": "创建远程修复分支",
            "remote_delivery.github_api.file.start": "上传变更文件",
            "remote_delivery.github_api.file.deleted": "删除远程文件",
            "remote_delivery.github_api.file.upserted": "写入远程文件",
            "remote_delivery.create_pr.start": "创建GitHub PR",
            "remote_delivery.create_mr.start": "创建GitLab MR",
            "remote_delivery.created": "创建PR/MR成功",
            "remote_delivery.failed": "远程交付失败",
            "platform.deliver_trace.done": "远程交付流程结束",
            "platform.repair.done": "修复流程结束",
            "service.repair_job.finished": "后台任务完成",
            "service.repair_job.failed": "后台任务失败",
        }
        if step == "agent.skill.selected":
            return f"选择技能：{event.get('skill_name') or event.get('skill_id')}"
        if step == "agent.sub_agent.derived":
            return f"派生子Agent：{event.get('role') or event.get('task_id')}"
        if step == "agent.tool.allowed":
            return f"授权工具：{event.get('tool')}"
        if step == "agent.execution.planned":
            return "规划子Agent修复步骤"
        return mapping.get(step, "")

    def _workflow_event_category(self, step: str) -> str:
        if "failed" in step or step.endswith(".failed"):
            return "error"
        if "skill" in step:
            return "skill"
        if "tool" in step or "validation" in step:
            return "tool"
        if "remote_delivery" in step or "git_push" in step:
            return "delivery"
        if step.startswith("agent."):
            return "sub_step"
        return "step"

    def _workflow_event_status(self, step: str) -> str:
        if "failed" in step:
            return "failed"
        if step.endswith(".start") or step in {"service.repair_job.queued", "service.repair_job.running"}:
            return "running"
        return "completed"

    def _workflow_edge_label(self, step: str) -> str:
        if "tool" in step or "validation" in step:
            return "调用"
        if "skill" in step:
            return "选择"
        if "remote_delivery" in step or "git_push" in step:
            return "上传"
        return "下一步"

    def _create_repair_job(
        self,
        *,
        incident_id: str,
        action: str,
        action_label: str,
        max_attempts: int,
        remote_delivery_confirmed: bool,
        instructions: str = "",
        trace_id: str = "",
    ) -> dict[str, Any]:
        now = iso_now()
        job = {
            "id": f"repair-job-{uuid4().hex[:12]}",
            "incident_id": incident_id,
            "trace_id": trace_id,
            "action": action,
            "action_label": action_label,
            "status": "queued",
            "max_attempts": max_attempts,
            "remote_delivery_confirmed": remote_delivery_confirmed,
            "instructions": instructions,
            "created_at": now,
            "updated_at": now,
            "started_at": "",
            "finished_at": "",
            "result": {},
            "error": "",
        }
        jobs = self._load_json(self.repair_jobs_path, [])
        jobs.append(job)
        self._save_json(self.repair_jobs_path, jobs[-200:])
        return job

    def _update_repair_job(self, job_id: str, **updates: Any) -> dict[str, Any]:
        jobs = self._load_json(self.repair_jobs_path, [])
        for job in jobs:
            if job.get("id") != job_id:
                continue
            job.update({key: to_jsonable(value) for key, value in updates.items()})
            job["updated_at"] = iso_now()
            self._save_json(self.repair_jobs_path, jobs)
            return job
        raise KeyError(f"Repair job `{job_id}` was not found.")

    def _repair_job_submission_payload(self, job: dict[str, Any]) -> dict[str, Any]:
        return {
            "accepted": True,
            "job_id": job["id"],
            "status": job["status"],
            "incident_id": job["incident_id"],
            "action_label": job["action_label"],
            "success": False,
            "trace_id": job.get("trace_id") or "",
            "report_path": "",
            "commit_hash": None,
            "sandbox_path": "",
            "diff_summary": "",
            "delivery": {},
            "remote_delivery_pending": False,
        }

    def _load_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return deepcopy(default)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return deepcopy(default)

    def _save_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
