from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from repo_maintainer.config import PlatformConfig
from repo_maintainer.error_semantics import ErrorSemanticCompressor
from repo_maintainer.models import IncidentReport
from repo_maintainer.platform import RepoAutonomyPlatform
from repo_maintainer.scheduler import CostAwareScheduler


class ErrorSemanticTests(unittest.TestCase):
    def test_dependency_conflict_classification(self) -> None:
        incident = IncidentReport(
            id="dep",
            title="Dependency conflict",
            description="",
            logs="Cannot install foo and bar because these package versions have conflicting dependencies. ERROR: ResolutionImpossible",
        )
        compressed = ErrorSemanticCompressor().compress(incident)
        self.assertEqual(compressed.error_type.value, "dependency_conflict")
        self.assertIn("resolutionimpossible", [keyword.lower() for keyword in compressed.keywords])


class SchedulerTests(unittest.TestCase):
    def test_cross_module_prefers_multi_agent(self) -> None:
        test_root = ROOT / ".test_runs" / f"case-{uuid4().hex}"
        test_root.mkdir(parents=True, exist_ok=False)
        incident = IncidentReport(
            id="cross",
            title="Cross module defect",
            description="service and repository both fail",
            logs='Traceback (most recent call last):\n  File "app/service.py", line 9, in run\n  File "app/repository.py", line 4, in fetch\nAssertionError\n',
            changed_files=["app/service.py", "app/repository.py", "tests/test_service.py"],
        )
        compressed = ErrorSemanticCompressor().compress(incident)
        decision = CostAwareScheduler(PlatformConfig.default(test_root)).decide(
            incident=incident,
            compressed_error=compressed,
            relevant_files=["app/service.py", "app/repository.py", "tests/test_service.py"],
            matched_skills=[],
        )
        self.assertEqual(decision.mode, "multi_agent")


class EndToEndTests(unittest.TestCase):
    def test_platform_config_auto_loads_root_dotenv(self) -> None:
        test_root = ROOT / ".test_runs" / f"case-{uuid4().hex}"
        test_root.mkdir(parents=True, exist_ok=False)
        dotenv_path = test_root / ".env"
        dotenv_path.write_text(
            "\n".join(
                [
                    "API_PORT=9123",
                    "JENKINS_JOB_NAME=dotenv-job",
                    "VECTOR_BACKEND=faiss_flat",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        original_api_port = os.environ.pop("API_PORT", None)
        original_job = os.environ.pop("JENKINS_JOB_NAME", None)
        original_vector = os.environ.pop("VECTOR_BACKEND", None)
        try:
            config = PlatformConfig.default(test_root)
        finally:
            for key, value in {
                "API_PORT": original_api_port,
                "JENKINS_JOB_NAME": original_job,
                "VECTOR_BACKEND": original_vector,
            }.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertEqual(config.api.port, 9123)
        self.assertEqual(config.jenkins.default_job, "dotenv-job")
        self.assertEqual(config.retrieval.vector_backend, "faiss_flat")

    def test_demo_pipeline_persists_trace(self) -> None:
        test_root = ROOT / ".test_runs" / f"case-{uuid4().hex}"
        test_root.mkdir(parents=True, exist_ok=False)
        config = PlatformConfig.default(test_root)
        platform = RepoAutonomyPlatform(config)
        incident_path = ROOT / "examples" / "incidents" / "ci_failure.json"
        repo_path = ROOT / "examples" / "sample_repo"
        trace = platform.analyze_incident_file(incident_path=incident_path, repo_root=repo_path)
        self.assertTrue(trace.id.startswith("trace-ci-failure-001-"))
        self.assertGreaterEqual(trace.token_usage, 1)
        self.assertTrue(platform.config.paths.memory_db_path.exists())
