from __future__ import annotations

import hashlib
import hmac
import io
import json
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path

from repo_maintainer.config import GraphConfig
from repo_maintainer.repo_indexer import RepositoryIndexer
from repo_maintainer.retrospective_code_context import GitHubArchiveSource, RepositoryCodeContext, RepositoryContextError
from repo_maintainer.retrospective_connectors import _portable_tool_schema
from repo_maintainer.retrospective_models import (
    Citation,
    Evidence,
    EvidenceBackedText,
    EvidenceSource,
    GenerationMetadata,
    ImpactSection,
    IncidentCreate,
    IncidentRecord,
    IncidentStatus,
    KnowledgeHit,
    PublishResult,
    QualityReport,
    RetrospectiveDocument,
    RetrospectiveVersion,
)
from repo_maintainer.retrospective_pipeline import ContextSelector, PipelineError, QualityValidator, RetrospectivePipeline, _knowledge_chunks, _rerank_knowledge_hits
from repo_maintainer.retrospective_store import InMemoryRetrospectiveStore


class FakeGit:
    token = "fake-token"

    def collect(self, incident):
        return [
            Evidence(
                id=f"ev_code_{incident.id}",
                incident_id=incident.id,
                source_type=EvidenceSource.GITHUB_DIFF,
                content="upload_handler catches ValueError and returns HTTP 500 before the fix",
                location="app/upload.py @@ -10,6 +10,8 @@",
                repo_id=incident.repo_id,
            ),
            Evidence(
                id=f"ev_log_{incident.id}",
                incident_id=incident.id,
                source_type=EvidenceSource.CI_LOG,
                content="INFO boot\nERROR ValueError: invalid upload payload\nFAILED tests/test_upload.py",
                location="GitHub Actions run 42",
                repo_id=incident.repo_id,
            ),
        ]


class FakeCollaboration:
    def __init__(self):
        self.publish_calls = 0

    def collect(self, incident):
        return [
            Evidence(
                id=f"ev_chat_{incident.id}",
                incident_id=incident.id,
                source_type=EvidenceSource.FEISHU_MESSAGE,
                content="已确认上传参数校验缺失，补充校验后恢复正常",
                location="Feishu chat test message m1",
                repo_id=incident.repo_id,
            )
        ]

    def publish(self, incident, document, idempotency_key):
        self.publish_calls += 1
        return PublishResult(remote_id=f"wiki_{idempotency_key}", url=f"https://feishu.test/wiki/{idempotency_key}")

    def status(self):
        return {"configured": True, "wiki_configured": True}


class FakeEmbedder:
    def embed_texts(self, texts):
        vectors = []
        for text in texts:
            lowered = text.lower()
            vectors.append(
                [
                    float(len(lowered.split())),
                    float(lowered.count("upload") + lowered.count("上传")),
                    float(lowered.count("valueerror")),
                    1.0,
                ]
            )
        return vectors


class FakeGenerator:
    def status(self):
        return {"configured": True, "model": "fake-structured-model", "sdk": "fake"}

    def generate(self, incident, evidence, history):
        code = next(item for item in evidence if item.source_type == EvidenceSource.GITHUB_DIFF)
        log = next(item for item in evidence if item.source_type == EvidenceSource.CI_LOG)
        chat = next(item for item in evidence if item.source_type == EvidenceSource.FEISHU_MESSAGE)
        document = RetrospectiveDocument(
            title="上传参数校验缺失导致 500",
            summary="上传接口对非法参数处理不完整。",
            impact=ImpactSection(
                scope="文件上传接口",
                severity="P1",
                citations=[Citation(evidence_id=log.id, quote="ERROR ValueError: invalid upload payload")],
            ),
            root_cause=EvidenceBackedText(
                content="上传参数校验缺失。",
                citations=[Citation(evidence_id=chat.id, quote="上传参数校验缺失")],
            ),
            fix_solution=EvidenceBackedText(
                content="增加参数校验并修正异常映射。",
                citations=[Citation(evidence_id=code.id, quote="catches ValueError and returns HTTP 500")],
            ),
            prevention=["增加非法上传参数测试", "统一异常映射"],
            related_modules=["upload_handler"],
            keywords=["upload", "ValueError", "HTTP 500"],
        )
        return document, GenerationMetadata(model="fake-structured-model", input_tokens=120, output_tokens=80, duration_ms=10)


def make_pipeline(store=None, collaboration=None, git=None, code_context=None, generator=None):
    return RetrospectivePipeline(
        store=store or InMemoryRetrospectiveStore(),
        git=git or FakeGit(),
        code_context=code_context,
        collaboration=collaboration or FakeCollaboration(),
        generator=generator or FakeGenerator(),
        embedder=FakeEmbedder(),
        github_webhook_secret="secret",
        selector=ContextSelector(budget_chars=4000, max_item_chars=2000),
    )


class RetrospectivePipelineTests(unittest.TestCase):
    def test_generation_and_regeneration_retry_with_citation_validation_feedback(self):
        class FeedbackGenerator(FakeGenerator):
            def __init__(self):
                self.feedback = []

            def generate(self, incident, evidence, history):
                errors = incident.metadata.get("generation_validation_errors", [])
                self.feedback.append(list(errors))
                document, metadata = super().generate(incident, evidence, history)
                if not errors:
                    document.root_cause.citations[0].quote = "fabricated quotation"
                return document, metadata

        generator = FeedbackGenerator()
        pipeline = make_pipeline(generator=generator)
        incident = pipeline.create_incident(IncidentCreate(title="upload failure"))
        first = pipeline.run(incident.id)
        self.assertEqual(first.version, 1)
        self.assertEqual(pipeline.store.get_incident(incident.id).status, IncidentStatus.PENDING_REVIEW)
        self.assertEqual(generator.feedback[0], [])
        self.assertTrue(any("Quote not found" in error for error in generator.feedback[1]))
        self.assertNotIn("generation_validation_errors", pipeline.store.get_incident(incident.id).metadata)

        pipeline.reject(incident.id, "reviewer", "regenerate for verification")
        second = pipeline.regenerate(incident.id)
        self.assertEqual(second.version, 2)
        self.assertEqual(generator.feedback[2], [])
        self.assertTrue(any("Quote not found" in error for error in generator.feedback[3]))
        self.assertEqual(len(pipeline.store.list_versions(incident.id)), 2)

    def test_error_stack_selects_source_outside_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "jobs.py").write_text(
                'def run_job(value):\n    return int(value)\n', encoding="utf-8"
            )

            class LocalSource:
                @contextmanager
                def open(self, incident):
                    yield root

            incident = make_pipeline().create_incident(IncidentCreate(
                title="job conversion failed", logs='File "jobs.py", line 2, in run_job\nValueError: invalid literal',
                repo_id="team/jobs", repo_name="team/jobs", commit_sha="b" * 40,
            ))
            collector = RepositoryCodeContext(
                LocalSource(), indexer=RepositoryIndexer(GraphConfig(enable_treesitter=False))
            )
            code = collector.collect(incident, [])
            self.assertEqual(len(code), 1)
            self.assertIn("return int(value)", code[0].content)
            self.assertIn("error_stack_frame", code[0].metadata["relation"])

    def test_repository_archive_only_extracts_safe_source_paths(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("repo/src/service.py", "def handle(): pass\n")
            archive.writestr("repo/../../escaped.py", "unsafe\n")
            archive.writestr("repo/node_modules/generated.py", "ignored\n")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            GitHubArchiveSource("token")._extract(data.getvalue(), root)
            self.assertTrue((root / "src" / "service.py").exists())
            self.assertFalse((root / "node_modules" / "generated.py").exists())
            self.assertFalse((root.parent / "escaped.py").exists())

    def test_context_selection_keeps_code_and_collaboration_beside_large_logs(self):
        incident = IncidentCreate(title="upload ValueError", repo_id="team/upload")
        record = make_pipeline().create_incident(incident)
        evidence = [
            Evidence(incident_id=record.id, source_type=EvidenceSource.CI_LOG,
                     content="ERROR ValueError upload\n" + "trace line\n" * 500, location="ci"),
            Evidence(incident_id=record.id, source_type=EvidenceSource.GITHUB_DIFF,
                     content="@@ upload ValueError\n" + "+changed line\n" * 500, location="app/service.py"),
            Evidence(incident_id=record.id, source_type=EvidenceSource.REPOSITORY_CODE,
                     content="def submit(value):\n    return validate_payload(value)", location="app/api.py#L3-L4"),
            Evidence(incident_id=record.id, source_type=EvidenceSource.FEISHU_MESSAGE,
                     content="上传请求在入口处失败", location="chat"),
        ]
        selection = ContextSelector(budget_chars=2_200, max_item_chars=2_000).select(record, evidence)
        self.assertLessEqual(selection.used_chars, 2_200)
        self.assertEqual({item.source_type for item in selection.evidence}, {item.source_type for item in evidence})
        self.assertIn("ERROR ValueError", selection.evidence[0].content)

    def test_repository_context_failure_keeps_other_evidence_usable(self):
        class FailingContext:
            def collect(self, incident, evidence):
                raise RepositoryContextError("archive unavailable")

        store = InMemoryRetrospectiveStore()
        pipeline = make_pipeline(store=store, code_context=FailingContext())
        incident = pipeline.create_incident(IncidentCreate(
            title="upload 500", repo_id="team/upload", repo_name="team/upload", commit_sha="a" * 40
        ))
        version = pipeline.run(incident.id)
        self.assertTrue(version.quality.hard_passed)
        self.assertEqual(store.get_incident(incident.id).metadata["code_context_warning"], "archive unavailable")

    def test_unchanged_caller_is_added_as_citable_code_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app").mkdir()
            (root / "app" / "service.py").write_text(
                'def validate_payload(value):\n    if not value:\n        raise ValueError("missing")\n    return value\n',
                encoding="utf-8",
            )
            (root / "app" / "api.py").write_text(
                'from .service import validate_payload\n\ndef submit(value):\n    return validate_payload(value)\n',
                encoding="utf-8",
            )
            (root / "app" / "other.py").write_text(
                'def format_error(value):\n    return str(value)\n', encoding="utf-8"
            )

            class LocalSource:
                @contextmanager
                def open(self, incident):
                    yield root

            class DiffGit(FakeGit):
                def collect(self, incident):
                    items = super().collect(incident)
                    items[0] = items[0].model_copy(update={
                        "location": "app/service.py",
                        "content": '@@ -1,2 +1,4 @@\n def validate_payload(value):\n+    if not value:\n+        raise ValueError("missing")\n     return value',
                    })
                    items.append(Evidence(
                        incident_id=incident.id, source_type=EvidenceSource.GITHUB_DIFF,
                        location="app/other.py", content="@@ -1,2 +1,2 @@\n def format_error(value):\n+    return str(value)",
                    ))
                    return items

            class DiffGenerator(FakeGenerator):
                def generate(self, incident, evidence, history):
                    document, metadata = super().generate(incident, evidence, history)
                    document.fix_solution.citations[0].quote = 'raise ValueError("missing")'
                    return document, metadata

            code_context = RepositoryCodeContext(
                LocalSource(), indexer=RepositoryIndexer(GraphConfig(enable_treesitter=False)), max_files=2
            )
            store = InMemoryRetrospectiveStore()
            pipeline = make_pipeline(store=store, git=DiffGit(), code_context=code_context, generator=DiffGenerator())
            sha = "a" * 40
            incident = pipeline.create_incident(IncidentCreate(
                title="upload ValueError", description="submit rejects empty payload",
                repo_id="team/upload-service", repo_name="team/upload-service",
                commit_sha=sha,
            ))
            pipeline.run(incident.id)

            code = [
                item for item in store.list_evidence(incident.id)
                if item.source_type == EvidenceSource.REPOSITORY_CODE
            ]
            caller = next(item for item in code if item.metadata["file_path"] == "app/api.py")
            self.assertTrue(caller.selected)
            self.assertIn("return validate_payload(value)", caller.content)
            self.assertIn("calls_changed_symbol", caller.metadata["relation"])
            self.assertIn(sha, caller.source_url)
            self.assertIn("#L3-L4", caller.location)

    def test_multiple_diff_files_expand_beyond_base_limit_and_keep_caller(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            diffs = []
            for index in range(7):
                path = f"module_{index}.py"
                (root / path).write_text(
                    f"def changed_{index}(value):\n    return value + {index}\n", encoding="utf-8"
                )
                diffs.append(Evidence(
                    incident_id="multi-diff", source_type=EvidenceSource.GITHUB_DIFF,
                    location=path,
                    content=f"@@ -1,2 +1,2 @@\n def changed_{index}(value):\n+    return value + {index}",
                ))
            (root / "consumer.py").write_text(
                "from module_0 import changed_0\n\ndef use(value):\n    return changed_0(value)\n",
                encoding="utf-8",
            )

            class LocalSource:
                @contextmanager
                def open(self, incident):
                    yield root

            incident = make_pipeline().create_incident(IncidentCreate(
                title="multiple changed modules", repo_id="team/multi", repo_name="team/multi",
                commit_sha="c" * 40,
            ))
            collector = RepositoryCodeContext(
                LocalSource(), indexer=RepositoryIndexer(GraphConfig(enable_treesitter=False)),
                max_files=4, max_expanded_files=16,
            )
            code = collector.collect(incident, diffs)
            paths = {item.metadata["file_path"] for item in code}
            self.assertTrue({f"module_{index}.py" for index in range(7)} <= paths)
            self.assertIn("consumer.py", paths)
            self.assertIn(
                "calls_changed_symbol",
                next(item for item in code if item.metadata["file_path"] == "consumer.py").metadata["relation"],
            )

    def test_separate_diff_hunks_in_one_file_produce_separate_code_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "service.py").write_text(
                "def first(value):\n    return value + 1\n\n\ndef second(value):\n    return value + 2\n",
                encoding="utf-8",
            )

            class LocalSource:
                @contextmanager
                def open(self, incident):
                    yield root

            incident = make_pipeline().create_incident(IncidentCreate(
                title="two changed functions", repo_id="team/service", repo_name="team/service",
                commit_sha="d" * 40,
            ))
            diff = Evidence(
                incident_id=incident.id, source_type=EvidenceSource.GITHUB_DIFF, location="service.py",
                content="@@ -1,2 +1,2 @@\n def first(value):\n+    return value + 1\n@@ -5,2 +5,2 @@\n def second(value):\n+    return value + 2",
            )
            collector = RepositoryCodeContext(LocalSource(), max_regions_per_file=2)
            code = collector.collect(incident, [diff])
            self.assertEqual(len(code), 2)
            self.assertEqual({item.metadata["anchor_line"] for item in code}, {2, 6})
            self.assertEqual(len({item.location for item in code}), 2)

    def test_two_hop_graph_finds_unchanged_entry_point(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "core.py").write_text("def changed(value):\n    return value + 1\n", encoding="utf-8")
            (root / "middle.py").write_text(
                "from core import changed\n\ndef middle(value):\n    return changed(value)\n",
                encoding="utf-8",
            )
            (root / "entry.py").write_text(
                "from middle import middle\n\ndef entry(value):\n    return middle(value)\n",
                encoding="utf-8",
            )

            class LocalSource:
                @contextmanager
                def open(self, incident):
                    yield root

            incident = make_pipeline().create_incident(IncidentCreate(
                title="entry path fails", repo_id="team/graph", repo_name="team/graph",
                commit_sha="e" * 40,
            ))
            diff = Evidence(
                incident_id=incident.id, source_type=EvidenceSource.GITHUB_DIFF,
                location="core.py", content="@@ -1,2 +1,2 @@\n def changed(value):\n+    return value + 1",
            )
            collector = RepositoryCodeContext(
                LocalSource(), indexer=RepositoryIndexer(GraphConfig(enable_treesitter=False)),
            )
            code = collector.collect(incident, [diff])
            entry = next(item for item in code if item.metadata["file_path"] == "entry.py")
            self.assertIn("graph_two_hop", entry.metadata["relation"])
            self.assertIn("return middle(value)", entry.content)

    def test_context_selection_covers_distinct_changed_code_files(self):
        record = make_pipeline().create_incident(IncidentCreate(
            title="many changed files", repo_id="team/multi",
        ))
        evidence = [
            Evidence(incident_id=record.id, source_type=EvidenceSource.CI_LOG,
                     content="ERROR: failed\n" + "trace\n" * 200, location="ci"),
            Evidence(incident_id=record.id, source_type=EvidenceSource.GITHUB_DIFF,
                     content="@@ -1 +1 @@\n+changed\n" * 100, location="module_0.py"),
            Evidence(incident_id=record.id, source_type=EvidenceSource.REPOSITORY_CODE,
                     content="def caller(value):\n    return changed_0(value)\n" * 20,
                     location="consumer.py#L1-L2",
                     metadata={"file_path": "consumer.py", "relation": ["calls_changed_symbol"]}),
        ]
        for index in range(6):
            evidence.append(Evidence(
                incident_id=record.id, source_type=EvidenceSource.REPOSITORY_CODE,
                content=f"def changed_{index}(value):\n    return value + {index}\n" * 20,
                location=f"module_{index}.py#L1-L2",
                metadata={"file_path": f"module_{index}.py", "relation": ["changed_file"]},
            ))
        selection = ContextSelector(budget_chars=6_000, max_item_chars=2_000).select(record, evidence)
        paths = {item.metadata["file_path"] for item in selection.evidence
                 if item.source_type == EvidenceSource.REPOSITORY_CODE}
        self.assertTrue({f"module_{index}.py" for index in range(6)} <= paths)
        self.assertIn("consumer.py", paths)
        self.assertLessEqual(selection.used_chars, 6_000)

    def test_qwen_tool_schema_is_inlined_and_keeps_document_fields(self):
        schema = _portable_tool_schema(RetrospectiveDocument)

        self.assertNotIn("$defs", schema)
        self.assertNotIn("$ref", json.dumps(schema))
        self.assertEqual(
            set(schema["properties"]),
            {"title", "summary", "impact", "root_cause", "fix_solution", "prevention", "related_modules", "keywords"},
        )

    def test_complete_review_publish_and_retrieval_loop(self):
        store = InMemoryRetrospectiveStore()
        collaboration = FakeCollaboration()
        pipeline = make_pipeline(store, collaboration)
        incident = pipeline.create_incident(
            IncidentCreate(
                title="upload endpoint returns 500",
                description="invalid payload triggers ValueError",
                repo_id="team/upload-service",
                repo_name="team/upload-service",
                incident_type="upload_failure",
                feishu_chat_ids=["test-chat"],
            )
        )

        version = pipeline.run(incident.id)
        self.assertTrue(version.quality.hard_passed)
        self.assertEqual(store.get_incident(incident.id).status, IncidentStatus.PENDING_REVIEW)
        self.assertTrue(all(item.selected for item in store.list_evidence(incident.id)))
        self.assertFalse(pipeline.search_knowledge("upload", repo_id="team/upload-service"))

        pipeline.approve(incident.id, "alice")
        result = pipeline.publish(incident.id, "alice")
        self.assertEqual(store.get_incident(incident.id).status, IncidentStatus.PUBLISHED)
        self.assertFalse(result["idempotent"])
        self.assertEqual(collaboration.publish_calls, 1)

        duplicate = pipeline.publish(incident.id, "alice")
        self.assertTrue(duplicate["idempotent"])
        self.assertEqual(collaboration.publish_calls, 1)

        hits = pipeline.search_knowledge("upload ValueError", repo_id="team/upload-service", incident_type="upload_failure")
        self.assertTrue(hits)
        self.assertEqual(hits[0].incident_id, incident.id)
        self.assertIn("feishu.test", hits[0].source_url)

    def test_knowledge_search_returns_distinct_cases_with_structured_card(self):
        store = InMemoryRetrospectiveStore()
        pipeline = make_pipeline(store=store)
        for number in range(2):
            incident = pipeline.create_incident(IncidentCreate(
                title=f"upload failure {number}", repo_id="team/upload-service",
                incident_type="upload_failure",
            ))
            pipeline.run(incident.id)
            pipeline.approve(incident.id, "alice")
            pipeline.publish(incident.id, "alice")

        hits = pipeline.search_knowledge(
            "upload ValueError", repo_id="team/upload-service", incident_type="upload_failure", limit=5
        )
        self.assertEqual(
            {item["metadata"]["section"] for item in store._knowledge},
            {"case_card", "symptoms", "root_cause", "fix_solution", "prevention"},
        )
        self.assertTrue(all(item["metadata"]["format_version"] == "v3" for item in store._knowledge))
        self.assertEqual(len(hits), 2)
        self.assertEqual(len({item.knowledge_id for item in hits}), 2)
        for hit in hits:
            self.assertIn("根因：", hit.content)
            self.assertIn("修复：", hit.content)
            self.assertIn("matched_section", hit.metadata)
            self.assertTrue(hit.metadata["source_evidence_ids"])
            self.assertTrue(hit.metadata["case_source_evidence_ids"])

    def test_long_structured_section_is_split_without_losing_source_ids(self):
        document = RetrospectiveDocument(
            title="upload failure", summary="invalid payload", impact=ImpactSection(
                scope="upload API", citations=[Citation(evidence_id="ev_impact", quote="500")]
            ),
            root_cause=EvidenceBackedText(
                content="first fact。" + "long explanation " * 120 + "last fact。",
                citations=[Citation(evidence_id="ev_cause", quote="first fact")],
            ),
            fix_solution=EvidenceBackedText(
                content="validate payload", citations=[Citation(evidence_id="ev_fix", quote="validate")]
            ),
            prevention=["test invalid payload"],
        )
        chunks = _knowledge_chunks(document)
        causes = [(content, metadata) for content, metadata in chunks if metadata["section"] == "root_cause"]
        self.assertGreater(len(causes), 1)
        self.assertTrue(all(len(content) <= 1_200 for content, _ in causes))
        self.assertTrue(all(metadata["source_evidence_ids"] == ["ev_cause"] for _, metadata in causes))
        self.assertIn("first fact", causes[0][0])
        self.assertIn("last fact", causes[-1][0])

    def test_rrf_recall_keeps_lexical_and_vector_only_cases(self):
        store = InMemoryRetrospectiveStore(channel_pool=2)
        document = RetrospectiveDocument(
            title="example", summary="summary",
            impact=ImpactSection(scope="API", citations=[Citation(evidence_id="ev_1", quote="failure")]),
            root_cause=EvidenceBackedText(content="cause", citations=[Citation(evidence_id="ev_1", quote="failure")]),
            fix_solution=EvidenceBackedText(content="fix", citations=[Citation(evidence_id="ev_1", quote="failure")]),
            prevention=["test"],
        )
        for title, content, vector in [
            ("lexical", "ValueError upload", [0.0, 1.0]),
            ("semantic", "payload validation", [1.0, 0.0]),
        ]:
            incident = IncidentRecord(id=f"inc_{title}", title=title, repo_id="team/upload")
            version = RetrospectiveVersion(
                id=f"retro_{title}", incident_id=incident.id, version=1,
                document=document, quality=QualityReport(),
            )
            store.index_knowledge(
                incident=incident, version=version,
                publication=PublishResult(remote_id=title, url=f"https://example.test/{title}"),
                chunks=[(content, vector, {"section": "case_card", "source_evidence_ids": ["ev_1"]})],
            )
        hits = store.search_knowledge("ValueError upload", [1.0, 0.0], repo_id="team/upload", limit=2)
        self.assertEqual({hit.incident_id for hit in hits}, {"inc_lexical", "inc_semantic"})
        self.assertTrue(all(hit.metadata["retrieval_strategy"] == "rrf" for hit in hits))

    def test_rrf_channel_pool_counts_cases_instead_of_chunks(self):
        store = InMemoryRetrospectiveStore(channel_pool=2)
        document = RetrospectiveDocument(
            title="example", summary="summary",
            impact=ImpactSection(scope="API", citations=[Citation(evidence_id="ev_1", quote="failure")]),
            root_cause=EvidenceBackedText(content="cause", citations=[Citation(evidence_id="ev_1", quote="failure")]),
            fix_solution=EvidenceBackedText(content="fix", citations=[Citation(evidence_id="ev_1", quote="failure")]),
            prevention=["test"],
        )
        for title, chunks in [
            ("long", [("upload failure", [1.0, 0.0], {"section": "case_card"})] * 6),
            ("short", [("upload", [1.0, 0.0], {"section": "case_card"})]),
        ]:
            incident = IncidentRecord(id=f"inc_{title}", title=title, repo_id="team/upload")
            version = RetrospectiveVersion(
                id=f"retro_{title}", incident_id=incident.id, version=1,
                document=document, quality=QualityReport(),
            )
            store.index_knowledge(
                incident=incident, version=version,
                publication=PublishResult(remote_id=title, url=f"https://example.test/{title}"),
                chunks=[(content, vector, {**metadata, "source_evidence_ids": ["ev_1"]})
                        for content, vector, metadata in chunks],
            )
        hits = store.search_knowledge("upload failure", [1.0, 0.0], repo_id="team/upload", limit=2)
        self.assertEqual({hit.incident_id for hit in hits}, {"inc_long", "inc_short"})

    def test_structured_rerank_promotes_matching_module_and_keyword(self):
        hits = [
            KnowledgeHit(
                knowledge_id="generic", incident_id="inc_generic", title="generic failure",
                content="generic", score=0.020, metadata={"related_modules": [], "keywords": []},
            ),
            KnowledgeHit(
                knowledge_id="relevant", incident_id="inc_relevant", title="upload failure",
                content="relevant", score=0.019,
                metadata={"related_modules": ["app/upload.py"], "keywords": ["ValueError"]},
            ),
        ]
        ranked = _rerank_knowledge_hits("app/upload.py ValueError", hits, 2)
        self.assertEqual(ranked[0].knowledge_id, "relevant")
        self.assertEqual(ranked[0].metadata["rerank_strategy"], "structured_v1")
        self.assertEqual(ranked[0].metadata["retrieval_score"], 0.019)

    def test_publish_retry_indexes_saved_publication_after_index_failure(self):
        class FailOnceStore(InMemoryRetrospectiveStore):
            def __init__(self):
                super().__init__()
                self.fail_once = True

            def index_knowledge(self, **kwargs):
                if self.fail_once:
                    self.fail_once = False
                    raise RuntimeError("temporary index failure")
                return super().index_knowledge(**kwargs)

        store = FailOnceStore()
        collaboration = FakeCollaboration()
        pipeline = make_pipeline(store=store, collaboration=collaboration)
        incident = pipeline.create_incident(IncidentCreate(
            title="upload failure", repo_id="team/upload-service", incident_type="upload_failure"
        ))
        pipeline.run(incident.id)
        pipeline.approve(incident.id, "alice")
        with self.assertRaisesRegex(RuntimeError, "temporary index failure"):
            pipeline.publish(incident.id, "alice")
        self.assertEqual(store.get_incident(incident.id).status, IncidentStatus.APPROVED)
        self.assertEqual(collaboration.publish_calls, 1)

        result = pipeline.publish(incident.id, "alice")
        self.assertTrue(result["idempotent"])
        self.assertEqual(collaboration.publish_calls, 1)
        self.assertEqual(store.get_incident(incident.id).status, IncidentStatus.PUBLISHED)
        self.assertEqual(len(pipeline.search_knowledge("upload", repo_id="team/upload-service")), 1)

    def test_github_webhook_signature_merge_filter_and_idempotency(self):
        pipeline = make_pipeline()
        payload = {
            "action": "closed",
            "number": 7,
            "repository": {"full_name": "team/service"},
            "pull_request": {
                "number": 7,
                "merged": True,
                "title": "fix: upload validation",
                "body": "Handle invalid payload",
                "merge_commit_sha": "abc123",
                "html_url": "https://github.test/team/service/pull/7",
                "merged_at": "2026-09-20T00:00:00Z",
                "user": {"login": "alice"},
            },
        }
        raw = json.dumps(payload).encode("utf-8")
        signature = "sha256=" + hmac.new(b"secret", raw, hashlib.sha256).hexdigest()
        incident, duplicate = pipeline.ingest_github_webhook(
            delivery_id="delivery-1",
            event_type="pull_request",
            signature=signature,
            raw_body=raw,
        )
        self.assertIsNotNone(incident)
        self.assertFalse(duplicate)
        again, duplicate = pipeline.ingest_github_webhook(
            delivery_id="delivery-1",
            event_type="pull_request",
            signature=signature,
            raw_body=raw,
        )
        self.assertTrue(duplicate)
        self.assertEqual(again.id, incident.id)

        payload["pull_request"]["merged"] = False
        raw_unmerged = json.dumps(payload).encode("utf-8")
        unmerged_signature = "sha256=" + hmac.new(b"secret", raw_unmerged, hashlib.sha256).hexdigest()
        ignored, duplicate = pipeline.ingest_github_webhook(
            delivery_id="delivery-2",
            event_type="pull_request",
            signature=unmerged_signature,
            raw_body=raw_unmerged,
        )
        self.assertIsNone(ignored)
        self.assertFalse(duplicate)

        with self.assertRaises(PipelineError):
            pipeline.ingest_github_webhook(
                delivery_id="delivery-3",
                event_type="pull_request",
                signature="sha256=bad",
                raw_body=raw,
            )

    def test_quality_validator_rejects_unknown_or_fabricated_citations(self):
        evidence = Evidence(
            id="ev_real",
            incident_id="inc_test",
            source_type=EvidenceSource.MANUAL,
            content="real evidence text",
            location="manual",
        )
        document = RetrospectiveDocument(
            title="test",
            summary="summary",
            impact=ImpactSection(scope="api", citations=[Citation(evidence_id="ev_missing", quote="fake")]),
            root_cause=EvidenceBackedText(content="cause", citations=[Citation(evidence_id="ev_real", quote="not present")]),
            fix_solution=EvidenceBackedText(content="fix", citations=[Citation(evidence_id="ev_real", quote="real evidence")]),
            prevention=["test"],
        )
        quality = QualityValidator().validate(document, [evidence], [])
        self.assertFalse(quality.hard_passed)
        self.assertEqual(len(quality.hard_errors), 2)

    def test_rejected_review_can_be_regenerated_from_persisted_evidence(self):
        store = InMemoryRetrospectiveStore()
        pipeline = make_pipeline(store)
        incident = pipeline.create_incident(
            IncidentCreate(title="upload 500", description="ValueError", repo_id="team/upload-service")
        )
        first = pipeline.run(incident.id)
        pipeline.reject(incident.id, "alice", "根因表述需要重写")

        second = pipeline.regenerate(incident.id)

        self.assertEqual(second.version, first.version + 1)
        self.assertEqual(store.get_incident(incident.id).status, IncidentStatus.PENDING_REVIEW)

    def test_context_selector_limits_logs_and_records_reason(self):
        incident = make_pipeline().create_incident(
            IncidentCreate(title="upload ValueError", description="500", repo_id="r")
        )
        long_log = "\n".join([f"INFO line {index}" for index in range(200)]) + "\nERROR ValueError upload failed"
        selection = ContextSelector(budget_chars=1200, max_item_chars=900).select(
            incident,
            [
                Evidence(
                    incident_id=incident.id,
                    source_type=EvidenceSource.CI_LOG,
                    content=long_log,
                    location="ci",
                )
            ],
        )
        self.assertLessEqual(selection.used_chars, 1200)
        self.assertIn("ValueError", selection.evidence[0].content)
        self.assertTrue(selection.evidence[0].selection_reason)


if __name__ == "__main__":
    unittest.main()
