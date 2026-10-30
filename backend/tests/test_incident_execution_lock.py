"""Incident execution exclusion; PostgreSQL checks require an isolated test DSN."""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from repo_maintainer.retrospective_store import InMemoryRetrospectiveStore, PostgresRetrospectiveStore


class InMemoryExecutionLockTests(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryRetrospectiveStore()

    def test_same_incident_excludes_nested_execution_and_releases(self):
        with self.store.execution_lock("incident-one") as acquired:
            self.assertTrue(acquired)
            with self.store.execution_lock("incident-one") as duplicate:
                self.assertFalse(duplicate)
            # A failed contender must not release the owner's lock.
            with self.store.execution_lock("incident-one") as duplicate:
                self.assertFalse(duplicate)
        with self.store.execution_lock("incident-one") as acquired:
            self.assertTrue(acquired)

    def test_other_incidents_are_independent(self):
        with self.store.execution_lock("incident-one") as first:
            with self.store.execution_lock("incident-two") as second:
                self.assertTrue(first)
                self.assertTrue(second)

    def test_exception_releases_execution(self):
        with self.assertRaisesRegex(RuntimeError, "generation failed"):
            with self.store.execution_lock("incident-one") as acquired:
                self.assertTrue(acquired)
                raise RuntimeError("generation failed")
        with self.store.execution_lock("incident-one") as acquired:
            self.assertTrue(acquired)

    def test_same_incident_excludes_other_thread_without_blocking(self):
        def try_execution(incident_id):
            with self.store.execution_lock(incident_id) as acquired:
                return acquired

        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.store.execution_lock("incident-one") as acquired:
                self.assertTrue(acquired)
                self.assertFalse(executor.submit(try_execution, "incident-one").result(timeout=2))
                self.assertTrue(executor.submit(try_execution, "incident-two").result(timeout=2))
            self.assertTrue(executor.submit(try_execution, "incident-one").result(timeout=2))


@unittest.skipUnless(os.getenv("TEST_CONTEXT_DATABASE_URL"), "isolated PostgreSQL not configured")
class PostgresExecutionLockTests(unittest.TestCase):
    def setUp(self):
        # Advisory locks require no tables, migrations, or persistent test rows.
        self.first = PostgresRetrospectiveStore(os.environ["TEST_CONTEXT_DATABASE_URL"])
        self.second = PostgresRetrospectiveStore(os.environ["TEST_CONTEXT_DATABASE_URL"])
        self.incident_id = f"execution-lock-test-{uuid4()}"

    def test_distinct_connections_exclude_same_incident_and_release(self):
        with self.first.execution_lock(self.incident_id) as acquired:
            self.assertTrue(acquired)
            with self.second.execution_lock(self.incident_id) as duplicate:
                self.assertFalse(duplicate)
            with self.second.execution_lock(f"{self.incident_id}-other") as independent:
                self.assertTrue(independent)
            with self.second.execution_lock(self.incident_id) as duplicate:
                self.assertFalse(duplicate)
        with self.second.execution_lock(self.incident_id) as acquired:
            self.assertTrue(acquired)

    def test_exception_releases_lock_for_another_connection(self):
        with self.assertRaisesRegex(RuntimeError, "generation failed"):
            with self.first.execution_lock(self.incident_id) as acquired:
                self.assertTrue(acquired)
                raise RuntimeError("generation failed")
        with self.second.execution_lock(self.incident_id) as acquired:
            self.assertTrue(acquired)


if __name__ == "__main__":
    unittest.main()
