from __future__ import annotations

import hashlib
import configparser
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from .flow_debug import flow_log
from .models import IncidentReport
from .security import SecurityManager


class RepoCheckoutError(RuntimeError):
    pass


@dataclass(slots=True)
class RepoWorkspace:
    root: Path
    repo_url: str | None
    branch: str | None
    revision: str | None
    source: str


def _slugify(value: str) -> str:
    lowered = value.strip().lower()
    lowered = re.sub(r"[^a-z0-9]+", "-", lowered)
    return lowered.strip("-") or "repo"


class RepoCheckoutManager:
    def __init__(
        self,
        checkouts_dir: str | Path,
        default_root: str | Path,
        security_manager: SecurityManager,
        *,
        github_token: str | None = None,
        gitlab_token: str | None = None,
    ) -> None:
        self.checkouts_dir = Path(checkouts_dir).resolve()
        self.default_root = Path(default_root).resolve()
        self.security_manager = security_manager
        self.github_token = github_token
        self.gitlab_token = gitlab_token
        self.checkouts_dir.mkdir(parents=True, exist_ok=True)

    def resolve(self, incident: IncidentReport, repo_root: str | Path | None = None) -> RepoWorkspace:
        flow_log("repo_checkout.resolve.start", incident_id=incident.id, repo_root=repo_root, metadata=incident.metadata)
        if repo_root is not None:
            resolved = Path(repo_root).resolve()
            if not resolved.exists():
                raise RepoCheckoutError(f"Repository path `{resolved}` does not exist.")
            workspace = RepoWorkspace(
                root=resolved,
                repo_url=self._repo_url_from_metadata(incident),
                branch=self._branch_from_metadata(incident),
                revision=self._revision_from_metadata(incident),
                source="explicit",
            )
            flow_log(
                "repo_checkout.resolve.explicit",
                incident_id=incident.id,
                root=workspace.root,
                repo_url=workspace.repo_url,
                branch=workspace.branch,
                revision=workspace.revision,
            )
            return workspace

        repo_url = self._repo_url_from_metadata(incident)
        branch = self._branch_from_metadata(incident)
        revision = self._revision_from_metadata(incident)
        flow_log("repo_checkout.resolve.metadata", incident_id=incident.id, repo_url=repo_url, branch=branch, revision=revision)
        if not repo_url:
            provider = str(incident.metadata.get("provider", "local")).strip().lower() or "local"
            if provider != "local":
                raise RepoCheckoutError(
                    f"Incident `{incident.id}` is missing repository clone metadata; "
                    "expected `repo_url` or provider-specific owner/repo information."
                )
            workspace = RepoWorkspace(
                root=self.default_root,
                repo_url=None,
                branch=branch,
                revision=revision,
                source="default_root",
            )
            flow_log("repo_checkout.resolve.default_root", incident_id=incident.id, root=workspace.root)
            return workspace

        checkout_root = self._checkout_root(repo_url)
        local_source = self._local_source_path(repo_url)
        if local_source is None:
            local_source = self._discover_local_mirror(repo_url, exclude=checkout_root)
        flow_log(
            "repo_checkout.resolve.checkout_plan",
            incident_id=incident.id,
            checkout_root=checkout_root,
            local_source=local_source,
        )

        if local_source is not None and local_source.resolve() == checkout_root.resolve():
            flow_log("repo_checkout.resolve.use_existing_checkout", incident_id=incident.id, checkout_root=checkout_root)
            self._checkout_existing(checkout_root, branch=branch, revision=revision)
        elif local_source is not None:
            if (checkout_root / ".git").exists():
                flow_log("repo_checkout.resolve.refresh_from_local", incident_id=incident.id, checkout_root=checkout_root, local_source=local_source)
                self._refresh_checkout_from_local_source(checkout_root, local_source, branch=branch, revision=revision)
            else:
                flow_log("repo_checkout.resolve.materialize_from_local", incident_id=incident.id, checkout_root=checkout_root, local_source=local_source)
                self._materialize_local_checkout(checkout_root, local_source, branch=branch, revision=revision)
        else:
            flow_log("repo_checkout.resolve.remote_read_authorize.start", incident_id=incident.id, repo_url=repo_url)
            self.security_manager.authorize_remote_read("git", repo_url)
            auth_source = self._authenticated_source(repo_url)
            flow_log("repo_checkout.resolve.remote_read_authorize.ok", incident_id=incident.id, repo_url=repo_url)
            self._ensure_checkout(checkout_root, repo_url, auth_source)
            flow_log("repo_checkout.resolve.fetch.start", incident_id=incident.id, checkout_root=checkout_root)
            self._fetch(checkout_root, repo_url, auth_source)
            target = self._checkout_target(checkout_root, branch=branch, revision=revision)
            flow_log("repo_checkout.resolve.checkout_target", incident_id=incident.id, target=target)
            self._run_git(["checkout", "--force", "--detach", target], cwd=checkout_root)
            self._run_git(["clean", "-fd"], cwd=checkout_root)
        resolved_revision = self._run_git(["rev-parse", "HEAD"], cwd=checkout_root).stdout.strip() or revision
        workspace = RepoWorkspace(
            root=checkout_root,
            repo_url=repo_url,
            branch=branch,
            revision=resolved_revision,
            source="ci_metadata",
        )
        flow_log(
            "repo_checkout.resolve.done",
            incident_id=incident.id,
            root=workspace.root,
            repo_url=workspace.repo_url,
            branch=workspace.branch,
            revision=workspace.revision,
            source=workspace.source,
        )
        return workspace

    def _ensure_checkout(self, checkout_root: Path, repo_url: str, auth_source: str) -> None:
        if checkout_root.exists() and (checkout_root / ".git").exists():
            self._ensure_origin_remote(checkout_root, repo_url)
            return
        if checkout_root.exists():
            shutil.rmtree(checkout_root)
        checkout_root.mkdir(parents=True, exist_ok=True)
        self._run_git(["init"], cwd=checkout_root)
        self._ensure_origin_remote(checkout_root, repo_url)

    def _fetch(self, checkout_root: Path, repo_url: str, auth_source: str) -> None:
        if auth_source == repo_url:
            self._run_git(["fetch", "--prune", "--tags", "origin"], cwd=checkout_root)
            return
        self._run_git(
            [
                "fetch",
                "--prune",
                "--tags",
                auth_source,
                "+refs/heads/*:refs/remotes/origin/*",
            ],
            cwd=checkout_root,
        )

    def _checkout_target(self, checkout_root: Path, *, branch: str | None, revision: str | None) -> str:
        if revision:
            return revision
        if branch:
            return f"origin/{branch}"
        symbolic = self._run_git(
            ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
            cwd=checkout_root,
            check=False,
        )
        if symbolic.returncode == 0 and symbolic.stdout.strip():
            return symbolic.stdout.strip()
        branches = self._run_git(
            ["for-each-ref", "--format=%(refname:short)", "refs/remotes/origin"],
            cwd=checkout_root,
        ).stdout.splitlines()
        for candidate in branches:
            stripped = candidate.strip()
            if stripped and stripped != "origin/HEAD":
                return stripped
        raise RepoCheckoutError("Unable to determine a checkout target for the cloned repository.")

    def _checkout_root(self, repo_url: str) -> Path:
        normalized = repo_url.replace("\\", "/").rstrip("/")
        name = normalized.rsplit("/", 1)[-1]
        if name.endswith(".git"):
            name = name[:-4]
        digest = hashlib.sha1(repo_url.encode("utf-8")).hexdigest()[:10]
        return self.checkouts_dir / f"{_slugify(name)}-{digest}"

    def _repo_url_from_metadata(self, incident: IncidentReport) -> str | None:
        metadata = incident.metadata
        for key in ("repo_url", "clone_url", "git_url", "http_url_to_repo"):
            value = str(metadata.get(key, "")).strip()
            if value:
                return value
        owner = str(metadata.get("owner", "")).strip()
        repo = str(metadata.get("repo", "")).strip()
        if owner and repo:
            return f"https://github.com/{owner}/{repo}.git"
        return None

    def _branch_from_metadata(self, incident: IncidentReport) -> str | None:
        metadata = incident.metadata
        for key in ("head_branch", "ref", "branch", "default_branch"):
            value = self._normalize_branch_name(str(metadata.get(key, "")).strip())
            if value:
                return value
        return None

    def _revision_from_metadata(self, incident: IncidentReport) -> str | None:
        metadata = incident.metadata
        for key in ("head_sha", "sha", "revision", "commit_sha", "commit_id"):
            value = str(metadata.get(key, "")).strip()
            if value:
                return value
        return None

    def _normalize_branch_name(self, branch: str) -> str | None:
        value = branch.strip()
        if not value:
            return None
        for prefix in ("refs/remotes/origin/", "refs/heads/", "origin/", "remotes/origin/"):
            if value.startswith(prefix):
                value = value[len(prefix) :]
        return value or None

    def _authenticated_source(self, repo_url: str) -> str:
        parsed = urlparse(repo_url)
        if parsed.scheme != "https":
            return repo_url
        path = parsed.path.rstrip("/")
        host = parsed.netloc.lower()
        if "github.com" in host and self.github_token:
            return f"https://x-access-token:{self.github_token}@{parsed.netloc}{path}"
        if "gitlab" in host and self.gitlab_token:
            return f"https://oauth2:{self.gitlab_token}@{parsed.netloc}{path}"
        return repo_url

    def _local_source_path(self, repo_url: str) -> Path | None:
        if re.match(r"^[a-zA-Z]:[\\/]", repo_url):
            candidate = Path(repo_url)
            return candidate.resolve() if candidate.exists() else None
        parsed = urlparse(repo_url)
        if parsed.scheme == "file":
            candidate_path = unquote(parsed.path)
            if re.match(r"^/[a-zA-Z]:[\\/]", candidate_path):
                candidate_path = candidate_path[1:]
            candidate = Path(candidate_path)
            return candidate.resolve() if candidate.exists() else None
        if parsed.scheme:
            return None
        candidate = Path(repo_url)
        return candidate.resolve() if candidate.exists() else None

    def _discover_local_mirror(self, repo_url: str, *, exclude: Path | None = None) -> Path | None:
        repo_name = self._repo_name_from_url(repo_url)
        normalized_target = self._normalize_repo_url(repo_url)
        excluded = exclude.resolve() if exclude is not None else None
        if not repo_name:
            return None
        search_roots: list[Path] = []
        for candidate_root in [self.default_root, self.default_root.parent, self.default_root.parent.parent]:
            resolved_root = candidate_root.resolve()
            if resolved_root.exists() and resolved_root not in search_roots:
                search_roots.append(resolved_root)

        fallback_match: Path | None = None
        for root in search_roots:
            for candidate in self._candidate_repo_dirs(root, repo_name):
                if excluded is not None and candidate.resolve() == excluded:
                    continue
                if not (candidate / ".git").exists():
                    continue
                origin_url = self._origin_remote_url(candidate)
                normalized_origin = self._normalize_repo_url(origin_url) if origin_url else ""
                if normalized_target and normalized_origin and normalized_origin == normalized_target:
                    return candidate.resolve()
                if fallback_match is None and candidate.name.lower() == repo_name.lower():
                    fallback_match = candidate.resolve()
        return fallback_match

    def _candidate_repo_dirs(self, root: Path, repo_name: str) -> list[Path]:
        candidates: list[Path] = []
        direct_names = {
            repo_name,
            repo_name.replace("-", "_"),
            repo_name.replace("_", "-"),
        }
        for name in direct_names:
            candidate = root / name
            if candidate.is_dir():
                candidates.append(candidate)
        try:
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                lowered = child.name.lower()
                if lowered == repo_name.lower() or lowered.startswith(f"{repo_name.lower()}-"):
                    candidates.append(child)
        except OSError:
            return candidates
        unique: list[Path] = []
        seen: set[Path] = set()
        for item in candidates:
            resolved = item.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            unique.append(resolved)
        return unique

    def _origin_remote_url(self, repo_root: Path) -> str | None:
        config_path = repo_root / ".git" / "config"
        if not config_path.exists():
            return None
        parser = configparser.ConfigParser()
        try:
            parser.read(config_path, encoding="utf-8")
        except (configparser.Error, OSError):
            return None
        if parser.has_option('remote "origin"', "url"):
            return parser.get('remote "origin"', "url")
        return None

    def _repo_name_from_url(self, repo_url: str) -> str:
        normalized = repo_url.replace("\\", "/").rstrip("/")
        name = normalized.rsplit("/", 1)[-1]
        if name.endswith(".git"):
            name = name[:-4]
        return name.strip().lower()

    def _normalize_repo_url(self, value: str | None) -> str:
        if not value:
            return ""
        candidate = value.strip()
        ssh_match = re.match(r"^git@([^:]+):(.+)$", candidate)
        if ssh_match:
            candidate = f"https://{ssh_match.group(1)}/{ssh_match.group(2)}"
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https", "ssh"}:
            normalized = f"{parsed.netloc}{parsed.path}"
        elif parsed.scheme == "file":
            normalized = unquote(parsed.path)
        else:
            normalized = candidate
        normalized = normalized.replace("\\", "/").lower().rstrip("/")
        if normalized.endswith(".git"):
            normalized = normalized[:-4]
        if normalized.startswith("/"):
            normalized = normalized[1:]
        return normalized

    def _run_git(self, args: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
        if check and completed.returncode != 0:
            raise RepoCheckoutError(
                f"Git command failed in `{cwd}`: git {' '.join(args)}\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}"
            )
        return completed

    def _ensure_origin_remote(self, checkout_root: Path, repo_url: str) -> None:
        remote_check = self._run_git(["remote"], cwd=checkout_root, check=False)
        remotes = {item.strip() for item in remote_check.stdout.splitlines() if item.strip()}
        if "origin" in remotes:
            self._run_git(["remote", "set-url", "origin", repo_url], cwd=checkout_root)
            return
        self._run_git(["remote", "add", "origin", repo_url], cwd=checkout_root)

    def _materialize_local_checkout(
        self,
        checkout_root: Path,
        local_source: Path,
        *,
        branch: str | None,
        revision: str | None,
    ) -> None:
        if checkout_root.exists():
            shutil.rmtree(checkout_root)
        shutil.copytree(local_source, checkout_root)
        if not (checkout_root / ".git").exists():
            raise RepoCheckoutError(f"Local repository source `{local_source}` is not a git repository.")
        if revision:
            self._run_git(["checkout", "--force", "--detach", revision], cwd=checkout_root)
        elif branch:
            self._run_git(["checkout", "--force", branch], cwd=checkout_root)
        self._run_git(["clean", "-fd"], cwd=checkout_root)

    def _refresh_checkout_from_local_source(
        self,
        checkout_root: Path,
        local_source: Path,
        *,
        branch: str | None,
        revision: str | None,
    ) -> None:
        if not (checkout_root / ".git").exists():
            raise RepoCheckoutError(f"Checkout `{checkout_root}` is not a git repository.")
        self._run_git(
            [
                "fetch",
                "--prune",
                "--tags",
                str(local_source),
                "+refs/heads/*:refs/remotes/origin/*",
            ],
            cwd=checkout_root,
        )
        target = self._checkout_target(checkout_root, branch=branch, revision=revision)
        self._run_git(["checkout", "--force", "--detach", target], cwd=checkout_root)
        self._run_git(["clean", "-fd"], cwd=checkout_root)

    def _checkout_existing(
        self,
        checkout_root: Path,
        *,
        branch: str | None,
        revision: str | None,
    ) -> None:
        if not (checkout_root / ".git").exists():
            raise RepoCheckoutError(f"Checkout `{checkout_root}` is not a git repository.")
        target = self._checkout_target(checkout_root, branch=branch, revision=revision)
        self._run_git(["checkout", "--force", "--detach", target], cwd=checkout_root)
        self._run_git(["clean", "-fd"], cwd=checkout_root)
