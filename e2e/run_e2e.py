"""Exercise the complete Docker E2E loop through the public Nginx entrypoint."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = os.getenv("E2E_BASE_URL", "http://127.0.0.1:18080").rstrip("/")
WEBHOOK_SECRET = os.getenv("E2E_WEBHOOK_SECRET", "e2e-webhook-secret")


def request_json(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    expected: set[int] | None = None,
) -> tuple[int, Any]:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if payload is not None else None
    request = Request(
        f"{BASE_URL}{path}",
        data=raw,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urlopen(request, timeout=15) as response:
            status = response.status
            body = response.read()
    except HTTPError as exc:
        status = exc.code
        body = exc.read()
    if expected is not None and status not in expected:
        raise AssertionError(f"{method} {path}: expected {sorted(expected)}, got {status}: {body.decode('utf-8', 'replace')}")
    return status, json.loads(body.decode("utf-8")) if body else None


def signed_headers(raw: bytes, delivery_id: str, *, valid: bool = True) -> dict[str, str]:
    digest = hmac.new(WEBHOOK_SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return {
        "X-GitHub-Delivery": delivery_id,
        "X-GitHub-Event": "pull_request",
        "X-Hub-Signature-256": f"sha256={digest if valid else '0' * 64}",
        "Content-Type": "application/json",
    }


def post_webhook(payload: dict[str, Any], delivery_id: str, *, valid: bool = True, expected: set[int]) -> tuple[int, Any]:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(
        f"{BASE_URL}/webhooks/github",
        data=raw,
        method="POST",
        headers=signed_headers(raw, delivery_id, valid=valid),
    )
    try:
        with urlopen(request, timeout=15) as response:
            status, body = response.status, response.read()
    except HTTPError as exc:
        status, body = exc.code, exc.read()
    if status not in expected:
        raise AssertionError(f"webhook expected {sorted(expected)}, got {status}: {body.decode('utf-8', 'replace')}")
    return status, json.loads(body.decode("utf-8")) if body else None


def wait_for_incident(incident_id: str, timeout: int = 90) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        _, last = request_json("GET", f"/api/retrospective/incidents/{incident_id}", expected={200})
        status = last["incident"]["status"]
        if status == "pending_review":
            return last
        if status == "failed":
            raise AssertionError(
                f"pipeline failed at {last['incident']['failed_stage']}: {last['incident']['error']}"
            )
        time.sleep(1)
    raise TimeoutError(f"incident did not reach pending_review: {last}")


def main() -> None:
    run_id = int(time.time())
    pr_number = 100000 + (run_id % 800000)
    delivery = f"e2e-delivery-{run_id}"
    payload = {
        "action": "closed",
        "number": pr_number,
        "repository": {"full_name": "e2e/payment-service"},
        "pull_request": {
            "number": pr_number,
            "merged": True,
            "title": "fix: prevent duplicate payment capture",
            "body": "Payment callbacks could be retried concurrently and charge one order twice.",
            "merge_commit_sha": "e2eabc1234567890",
            "html_url": f"https://github.example/e2e/payment-service/pull/{pr_number}",
            "merged_at": "2026-09-22T02:00:00Z",
            "user": {"login": "e2e-developer"},
        },
    }

    _, liveness = request_json("GET", "/api/liveness", expected={200})
    _, health = request_json("GET", "/api/health", expected={200})
    if not liveness.get("ok") or not health.get("ok"):
        raise AssertionError(f"stack is not ready: liveness={liveness}, health={health}")

    post_webhook(payload, f"{delivery}-bad-signature", valid=False, expected={401})
    ignored_payload = json.loads(json.dumps(payload))
    ignored_payload["pull_request"]["merged"] = False
    _, ignored = post_webhook(ignored_payload, f"{delivery}-unmerged", expected={202})
    if ignored.get("status") != "ignored":
        raise AssertionError(f"unmerged PR was not ignored: {ignored}")

    _, accepted = post_webhook(payload, delivery, expected={202})
    if accepted.get("status") != "accepted":
        raise AssertionError(f"merged PR was not accepted: {accepted}")
    incident_id = accepted["incident_id"]

    _, duplicate = post_webhook(payload, delivery, expected={202})
    if duplicate.get("status") != "duplicate" or not duplicate.get("duplicate"):
        raise AssertionError(f"webhook idempotency failed: {duplicate}")

    detail = wait_for_incident(incident_id)
    evidence_types = {item["source_type"] for item in detail["evidence"]}
    expected_types = {
        "github_pr",
        "github_commit",
        "github_diff",
        "ci_log",
        "feishu_message",
        "feishu_doc",
    }
    missing = expected_types - evidence_types
    if missing:
        raise AssertionError(f"missing evidence types: {sorted(missing)}")
    retrospective = detail.get("retrospective") or {}
    quality = retrospective.get("quality") or {}
    if quality.get("hard_errors"):
        raise AssertionError(f"quality validation failed: {quality}")

    _, approved = request_json(
        "POST",
        f"/api/retrospective/incidents/{incident_id}/approve",
        {"actor": "e2e-reviewer", "reason": "E2E evidence verified"},
        expected={200},
    )
    if approved.get("status") != "approved":
        raise AssertionError(f"approval failed: {approved}")

    _, publication = request_json(
        "POST",
        f"/api/retrospective/incidents/{incident_id}/publish",
        {"actor": "e2e-reviewer", "reason": "publish deterministic fixture"},
        expected={200},
    )
    if publication.get("idempotent") or not publication.get("knowledge_id"):
        raise AssertionError(f"first publication failed: {publication}")

    _, repeated_publication = request_json(
        "POST",
        f"/api/retrospective/incidents/{incident_id}/publish",
        {"actor": "e2e-reviewer", "reason": "idempotency check"},
        expected={200},
    )
    if not repeated_publication.get("idempotent"):
        raise AssertionError(f"publication idempotency failed: {repeated_publication}")

    query = urlencode(
        {
            "q": "支付回调 request_id 幂等 重复扣款",
            "repo_id": "e2e/payment-service",
            "incident_type": "bug_fix",
            "limit": 5,
        }
    )
    _, hits = request_json("GET", f"/api/retrospective/knowledge/search?{query}", expected={200})
    if not hits or hits[0].get("incident_id") != incident_id:
        raise AssertionError(f"published knowledge was not retrieved: {hits}")

    result = {
        "result": "PASS",
        "incident_id": incident_id,
        "trace_id": detail["incident"]["trace_id"],
        "job_id": accepted["job_id"],
        "evidence_types": sorted(evidence_types),
        "quality_score": quality.get("score"),
        "publication_url": publication["publication"]["url"],
        "knowledge_id": publication["knowledge_id"],
        "retrieval_score": hits[0].get("score"),
        "checks": [
            "invalid webhook signature rejected",
            "unmerged PR ignored",
            "delivery idempotency enforced",
            "GitHub PR/commit/diff/CI evidence collected",
            "Feishu chat/document evidence collected",
            "LLM structured retrospective passed citation validation",
            "human approval recorded",
            "Feishu Wiki publication simulated",
            "publication idempotency enforced",
            "PostgreSQL/pgvector knowledge retrieved",
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"result": "FAIL", "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        raise
