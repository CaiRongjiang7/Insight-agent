"""Deterministic GitHub, Feishu, and OpenAI-compatible services for Docker E2E tests."""

from __future__ import annotations

import hashlib
import io
import json
import time
import zipfile
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response


app = FastAPI(title="Insight Agent E2E external-service simulator")

STATE: dict[str, list[dict[str, Any]]] = {
    "wiki_nodes": [],
    "document_writes": [],
    "model_requests": [],
    "embedding_requests": [],
}


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


# GitHub API fixtures -------------------------------------------------------


@app.get("/github/repos/{owner}/{repo}/pulls/{number}")
def github_pull(owner: str, repo: str, number: int) -> dict[str, Any]:
    return {
        "number": number,
        "title": "fix: prevent duplicate payment capture",
        "body": "Payment callbacks could be retried concurrently and charge one order twice.",
        "html_url": f"https://github.example/{owner}/{repo}/pull/{number}",
        "user": {"login": "e2e-developer"},
    }


@app.get("/github/repos/{owner}/{repo}/pulls/{number}/commits")
def github_commits(owner: str, repo: str, number: int) -> list[dict[str, Any]]:
    return [
        {
            "sha": "e2eabc1234567890",
            "html_url": f"https://github.example/{owner}/{repo}/commit/e2eabc1234567890",
            "commit": {"message": "fix(payment): add request_id idempotency guard"},
        }
    ]


@app.get("/github/repos/{owner}/{repo}/pulls/{number}/files")
def github_files(owner: str, repo: str, number: int) -> list[dict[str, Any]]:
    return [
        {
            "filename": "src/payment/callback.py",
            "status": "modified",
            "changes": 8,
            "blob_url": f"https://github.example/{owner}/{repo}/blob/e2e/src/payment/callback.py",
            "patch": (
                "@@ -18,6 +18,10 @@ def capture_payment(request_id, order_id):\n"
                "+    if request_id in processed_requests:\n"
                "+        return processed_requests[request_id]\n"
                "     result = gateway.capture(order_id)\n"
                "+    processed_requests[request_id] = result\n"
                "     return result\n"
            ),
        }
    ]


@app.get("/github/repos/{owner}/{repo}/issues/{number}/comments")
def github_comments(owner: str, repo: str, number: int) -> list[dict[str, Any]]:
    return [
        {
            "id": 42001,
            "body": "Added a concurrency regression test and verified only one capture call is emitted.",
            "html_url": f"https://github.example/{owner}/{repo}/pull/{number}#issuecomment-42001",
        }
    ]


@app.get("/github/repos/{owner}/{repo}/actions/runs")
def github_runs(owner: str, repo: str) -> dict[str, Any]:
    return {
        "workflow_runs": [
            {
                "id": 9001,
                "conclusion": "failure",
                "html_url": f"https://github.example/{owner}/{repo}/actions/runs/9001",
            }
        ]
    }


@app.get("/github/repos/{owner}/{repo}/actions/runs/{run_id}/logs")
def github_logs(owner: str, repo: str, run_id: int) -> Response:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "payment-tests.txt",
            (
                "INFO running payment callback regression\n"
                "ERROR DuplicateChargeError: order order-e2e-42 charged twice\n"
                "FAILED tests/test_payment_callback.py::test_concurrent_retry\n"
            ),
        )
    return Response(content=buffer.getvalue(), media_type="application/zip")


# Feishu Open Platform fixtures --------------------------------------------


@app.post("/feishu/open-apis/auth/v3/tenant_access_token/internal")
def feishu_token() -> dict[str, Any]:
    return {"code": 0, "tenant_access_token": "tenant-e2e-token", "expire": 7200}


@app.get("/feishu/open-apis/im/v1/messages")
def feishu_messages() -> dict[str, Any]:
    now_ms = str(int(time.time() * 1000))
    return {
        "code": 0,
        "data": {
            "has_more": False,
            "items": [
                {
                    "message_id": "om_e2e_payment_001",
                    "create_time": now_ms,
                    "body": {
                        "content": json.dumps(
                            {
                                "text": (
                                    "值班群确认：支付回调发生并发重试，缺少基于 request_id 的幂等保护，"
                                    "同一订单被重复扣款。"
                                )
                            },
                            ensure_ascii=False,
                        )
                    },
                },
                {
                    "message_id": "om_e2e_payment_002",
                    "create_time": now_ms,
                    "body": {
                        "content": json.dumps(
                            {"text": "修复上线后连续观察 30 分钟，没有新增重复扣款告警。"},
                            ensure_ascii=False,
                        )
                    },
                },
            ],
        },
    }


@app.get("/feishu/open-apis/docx/v1/documents/{token}/raw_content")
def feishu_document(token: str) -> dict[str, Any]:
    return {
        "code": 0,
        "data": {
            "content": (
                "支付系统值班记录：告警由 payment-callback 触发。临时处置为暂停自动重试，"
                "长期修复要求增加 request_id 唯一约束和并发回归测试。"
            )
        },
    }


@app.post("/feishu/open-apis/wiki/v2/spaces/{space_id}/nodes")
async def feishu_create_wiki(space_id: str, request: Request) -> dict[str, Any]:
    payload = await request.json()
    STATE["wiki_nodes"].append(
        {
            "space_id": space_id,
            "idempotency_key": request.headers.get("X-Idempotency-Key", ""),
            "payload": payload,
        }
    )
    return {
        "code": 0,
        "data": {"node": {"node_token": "wikcn_e2e_retrospective", "obj_token": "doccn_e2e_retrospective"}},
    }


@app.post("/feishu/open-apis/docx/v1/documents/{document_id}/blocks/{block_id}/children")
async def feishu_write_document(document_id: str, block_id: str, request: Request) -> dict[str, Any]:
    payload = await request.json()
    STATE["document_writes"].append(
        {"document_id": document_id, "block_id": block_id, "children": payload.get("children", [])}
    )
    return {"code": 0, "data": {"children": payload.get("children", [])}}


# OpenAI-compatible fixtures ------------------------------------------------


def _embedding(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    values = [round((digest[index] + 1) / 256, 6) for index in range(8)]
    values[0] = 1.0
    return values


@app.post("/openai/v1/embeddings")
async def embeddings(request: Request) -> dict[str, Any]:
    payload = await request.json()
    inputs = payload.get("input", [])
    if isinstance(inputs, str):
        inputs = [inputs]
    STATE["embedding_requests"].append({"count": len(inputs), "model": payload.get("model", "")})
    return {
        "object": "list",
        "model": payload.get("model", "e2e-embedding"),
        "data": [
            {"object": "embedding", "index": index, "embedding": _embedding(str(text))}
            for index, text in enumerate(inputs)
        ],
        "usage": {"prompt_tokens": len(inputs), "total_tokens": len(inputs)},
    }


def _pick(evidence: list[dict[str, Any]], source_type: str) -> dict[str, Any]:
    return next((item for item in evidence if item.get("source_type") == source_type), evidence[0])


def _quote(item: dict[str, Any], preferred: str) -> str:
    content = str(item.get("content") or "")
    if preferred in content:
        return preferred
    first_line = next((line.strip() for line in content.splitlines() if line.strip()), content)
    return first_line[:160]


@app.post("/openai/v1/responses")
async def responses(request: Request) -> dict[str, Any]:
    payload = await request.json()
    input_payload = payload.get("input") or "{}"
    context = json.loads(input_payload) if isinstance(input_payload, str) else {}
    evidence = list(context.get("evidence") or [])
    impact = _pick(evidence, "ci_log")
    root_cause = _pick(evidence, "feishu_message")
    fix = _pick(evidence, "github_diff")
    document = {
        "title": "支付回调并发重试导致重复扣款",
        "summary": "支付回调缺少 request_id 幂等保护，并发重试导致同一订单发生两次扣款。",
        "impact": {
            "scope": "支付回调与自动重试链路",
            "severity": "P1",
            "affected_users": "收到并发回调的订单用户",
            "citations": [
                {
                    "evidence_id": impact["evidence_id"],
                    "quote": _quote(impact, "ERROR DuplicateChargeError: order order-e2e-42 charged twice"),
                }
            ],
        },
        "root_cause": {
            "content": "支付回调入口没有按照 request_id 做幂等控制。",
            "citations": [
                {
                    "evidence_id": root_cause["evidence_id"],
                    "quote": _quote(root_cause, "缺少基于 request_id 的幂等保护"),
                }
            ],
        },
        "fix_solution": {
            "content": "在扣款前检查 request_id，并复用已完成请求的结果。",
            "citations": [
                {
                    "evidence_id": fix["evidence_id"],
                    "quote": _quote(fix, "+    if request_id in processed_requests:"),
                }
            ],
        },
        "prevention": ["增加并发重试回归测试", "为 request_id 增加唯一约束", "监控重复扣款告警"],
        "related_modules": ["payment-callback", "payment-gateway"],
        "keywords": ["重复扣款", "request_id", "幂等", "并发重试"],
    }
    STATE["model_requests"].append(
        {"model": payload.get("model", ""), "evidence_count": len(evidence), "history_count": len(context.get("historical_cases") or [])}
    )
    return {
        "id": "resp_e2e_retrospective",
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": payload.get("model", "e2e-review-model"),
        "output": [
            {
                "id": "fc_e2e_retrospective",
                "type": "function_call",
                "status": "completed",
                "name": "submit_retrospective",
                "call_id": "call_e2e_retrospective",
                "arguments": json.dumps(document, ensure_ascii=False),
            }
        ],
        "usage": {"input_tokens": 200, "output_tokens": 120, "total_tokens": 320},
    }


@app.get("/mock/state")
def mock_state() -> dict[str, Any]:
    return STATE
