from __future__ import annotations

import hashlib
import hmac
import json
import unittest

from repo_maintainer.retrospective_connectors import _portable_tool_schema
from repo_maintainer.retrospective_models import (
    Citation,
    Evidence,
    EvidenceBackedText,
    EvidenceSource,
    GenerationMetadata,
    ImpactSection,
    IncidentCreate,
    IncidentStatus,
    PublishResult,
    RetrospectiveDocument,
)
from repo_maintainer.retrospective_pipeline import ContextSelector, PipelineError, QualityValidator, RetrospectivePipeline
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


def make_pipeline(store=None, collaboration=None):
    return RetrospectivePipeline(
        store=store or InMemoryRetrospectiveStore(),
        git=FakeGit(),
        collaboration=collaboration or FakeCollaboration(),
        generator=FakeGenerator(),
        embedder=FakeEmbedder(),
        github_webhook_secret="secret",
        selector=ContextSelector(budget_chars=4000, max_item_chars=2000),
    )


class RetrospectivePipelineTests(unittest.TestCase):
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
