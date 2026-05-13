from __future__ import annotations

import sys
import time
import unittest
from dataclasses import replace
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from repo_maintainer.config import PlatformConfig
from repo_maintainer.platform import RepoAutonomyPlatform


class DreamLearnTests(unittest.TestCase):
    def test_dream_loop_reviews_successful_traces(self) -> None:
        test_root = ROOT / ".test_runs" / f"case-{uuid4().hex}"
        test_root.mkdir(parents=True, exist_ok=False)
        config = PlatformConfig.default(test_root)
        platform = RepoAutonomyPlatform(config)
        incident_path = ROOT / "examples" / "incidents" / "dependency_conflict.json"
        repo_path = ROOT / "examples" / "sample_repo"
        trace = platform.analyze_incident_file(incident_path=incident_path, repo_root=repo_path)
        self.assertTrue(trace.success)

        report = platform.run_dream_loop()
        self.assertGreaterEqual(report["reviewed_traces"], 1)
        self.assertTrue(report["learned_skill_ids"])

    def test_persistent_learning_jobs_recover_after_restart(self) -> None:
        test_root = ROOT / ".test_runs" / f"case-{uuid4().hex}"
        test_root.mkdir(parents=True, exist_ok=False)
        base_config = PlatformConfig.default(test_root)
        first_config = replace(
            base_config,
            learning=replace(base_config.learning, auto_run_on_successful_repair=False, async_queue_enabled=False),
        )
        first_platform = RepoAutonomyPlatform(first_config)
        incident_path = ROOT / "examples" / "incidents" / "dependency_conflict.json"
        repo_path = ROOT / "examples" / "sample_repo"
        trace = first_platform.analyze_incident_file(incident_path=incident_path, repo_root=repo_path)
        first_platform.memory_store.enqueue_learning_job(trace.id, priority=90, source="restart_test")
        claimed = first_platform.memory_store.claim_learning_jobs("stale-worker", limit=1, lease_timeout_seconds=99999)
        self.assertEqual(len(claimed), 1)
        time.sleep(1.1)

        second_config = replace(
            base_config,
            learning=replace(
                base_config.learning,
                auto_run_on_successful_repair=True,
                async_queue_enabled=True,
                embedded_workers_enabled=False,
                lease_timeout_seconds=1.0,
                idle_poll_seconds=0.2,
                batch_window_seconds=0.1,
            ),
        )
        second_platform = RepoAutonomyPlatform(second_config)
        run_result = second_platform.run_learning_daemon_once()
        self.assertGreaterEqual(run_result["processed_jobs"], 1)
        self.assertTrue(second_platform.wait_for_learning_idle(1.0))
        job = second_platform.memory_store.learning_job(trace.id)
        self.assertIsNotNone(job)
        self.assertEqual(job["status"], "completed")
        stored_trace = second_platform.memory_store.trace_payload(trace.id)
        self.assertEqual(stored_trace["auto_learning_status"], "completed")

    def test_learning_daemon_elects_single_leader(self) -> None:
        test_root = ROOT / ".test_runs" / f"case-{uuid4().hex}"
        test_root.mkdir(parents=True, exist_ok=False)
        base_config = PlatformConfig.default(test_root)
        config = replace(
            base_config,
            learning=replace(
                base_config.learning,
                auto_run_on_successful_repair=True,
                async_queue_enabled=True,
                embedded_workers_enabled=False,
            ),
        )
        leader_platform = RepoAutonomyPlatform(config)
        follower_platform = RepoAutonomyPlatform(config)

        leader_result = leader_platform.run_learning_daemon_once()
        follower_result = follower_platform.run_learning_daemon_once()

        self.assertTrue(leader_result["leader"])
        self.assertFalse(follower_result["leader"])
        status = follower_platform.learning_status()
        self.assertTrue(status["leader_owner"])
        self.assertNotEqual(status["leader_owner"], follower_platform.auto_learning.worker_id)

    def test_dead_letter_jobs_require_manual_recovery(self) -> None:
        test_root = ROOT / ".test_runs" / f"case-{uuid4().hex}"
        test_root.mkdir(parents=True, exist_ok=False)
        base_config = PlatformConfig.default(test_root)
        config = replace(
            base_config,
            learning=replace(
                base_config.learning,
                auto_run_on_successful_repair=True,
                async_queue_enabled=True,
                embedded_workers_enabled=False,
                max_job_failures=1,
                retry_delay_seconds=0.1,
                max_retry_delay_seconds=0.2,
            ),
        )
        platform = RepoAutonomyPlatform(config)
        incident_path = ROOT / "examples" / "incidents" / "dependency_conflict.json"
        repo_path = ROOT / "examples" / "sample_repo"
        trace = platform.analyze_incident_file(incident_path=incident_path, repo_root=repo_path)
        platform.memory_store.enqueue_learning_job(trace.id, priority=95, source="dead_letter_test")

        original_run = platform.auto_learning.dream_loop.run

        def broken_run(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("forced learning failure")

        platform.auto_learning.dream_loop.run = broken_run  # type: ignore[assignment]
        platform.run_learning_daemon_once()

        job = platform.memory_store.learning_job(trace.id)
        self.assertIsNotNone(job)
        self.assertEqual(job["status"], "dead_letter")
        self.assertTrue(job["payload"]["manual_recovery_required"])

        platform.retry_learning_job(trace.id, actor="tester", reason="approved retry", priority=88)
        platform.auto_learning.dream_loop.run = original_run  # type: ignore[assignment]
        run_result = platform.run_learning_daemon_once()

        self.assertEqual(run_result["processed_jobs"], 1)
        recovered_job = platform.memory_store.learning_job(trace.id)
        self.assertIsNotNone(recovered_job)
        self.assertEqual(recovered_job["status"], "completed")
        stored_trace = platform.memory_store.trace_payload(trace.id)
        self.assertEqual(stored_trace["auto_learning_status"], "completed")
