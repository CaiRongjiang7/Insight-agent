"""Per-investigation loopback MCP and metered model gateway.

Only the pipeline thread executes domain tools or writes model request snapshots.
The Harness receives a short-lived loopback credential, never upstream secrets.
"""

from __future__ import annotations

import asyncio
import json
import queue
import secrets
import socket
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field

from .retrospective_compaction import CompactionContractError, prepare_compaction, reported_summary_usage, summary_from_response, validate_summary


@dataclass
class GatewayRequest:
    kind: str
    payload: dict
    future: Future = field(default_factory=Future)


class InvestigationGateway:
    def __init__(self, session, config, events: queue.Queue):
        self.session, self.config, self.events = session, config, events
        self.token = secrets.token_urlsafe(32)
        self.deadline = time.monotonic() + config.timeout_seconds
        self.model_requests = 0
        self.compaction_requests = 0
        self.compaction_errors = 0
        self.compaction_usage: list[dict] = []
        self.stop_reason = ""
        self.closed = False
        self.pending: set[Future] = set()
        self._pending_lock = threading.Lock()
        self._thread = self._server = self._socket = None

    async def submit(self, kind, payload):
        request = GatewayRequest(kind, payload)
        with self._pending_lock:
            if self.closed:
                raise RuntimeError("Investigation closed")
            self.pending.add(request.future)
        self.events.put(request)
        try:
            return await asyncio.wrap_future(request.future)
        finally:
            with self._pending_lock:
                self.pending.discard(request.future)

    def dispatch(self, request: GatewayRequest):
        if not request.future.set_running_or_notify_cancel():
            return
        try:
            if request.kind == "tool":
                payload = request.payload
                result = self.session.execute(payload["name"], payload["arguments"], payload["bridge_call_id"])
                result = {**result.model_dump(mode="json"), "bridge_call_id": payload["bridge_call_id"]}
            elif request.kind == "model_request":
                compact = request.payload.get('purpose') == 'compaction'
                self.session.record('harness_compaction_request' if compact else "tool_exploration", request.payload, scope="provider_request",
                                    round=request.payload["round"])
                result = True
            elif request.kind == 'compaction_prepare':
                try:
                    result = prepare_compaction(request.payload, list(self.session.executor._raw.values()))
                except CompactionContractError:
                    result = {'error': 'Compaction preservation input is unsupported or too large'}
            elif request.kind == 'compaction_result':
                self.session.record('harness_compaction_result', request.payload, scope='compaction_result')
                result = True
            else:
                raise ValueError("Unknown gateway request")
        except BaseException as exc:
            request.future.set_exception(RuntimeError("Application execution failed"))
            raise exc
        request.future.set_result(result)

    def _app(self):
        import httpx
        import mcp_types as types
        from mcp.server.lowlevel import Server
        from starlette.requests import Request
        from starlette.responses import JSONResponse, StreamingResponse, Response
        from starlette.routing import Route

        definitions = self.session.executor.definitions()
        allowed = {"mcp__retrospective__" + spec["name"] for spec in definitions}

        async def list_tools(_ctx, _params):
            return types.ListToolsResult(tools=[types.Tool(
                name=spec["name"], description=spec["description"], inputSchema=spec["parameters"],
                annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False),
            ) for spec in definitions])

        async def call_tool(_ctx, params):
            result = await self.submit("tool", {
                "name": params.name, "arguments": params.arguments or {},
                "bridge_call_id": "bridge_" + secrets.token_hex(12),
            })
            return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                                        isError=result["status"] == "error")

        async def proxy(request: Request):
            compact = request.url.path.startswith('/compaction-model/')
            if compact and not self.config.compaction_enabled:
                return JSONResponse({'error': {'message': 'Compaction disabled'}}, status_code=404)
            expected = "chat/completions" if self.config.api == "openai-completions" else "responses"
            if request.path_params["path"] != expected or self.closed:
                return JSONResponse({"error": {"message": "Route unavailable"}}, status_code=404)
            count = self.compaction_requests if compact else self.model_requests
            limit = self.config.compaction_max_requests if compact else self.config.max_rounds
            if count >= limit or time.monotonic() >= self.deadline:
                self.stop_reason = ('compaction_limit' if compact else "round_limit") if count >= limit else "deadline"
                return JSONResponse({"error": {"message": "Investigation budget exhausted"}}, status_code=429)
            raw = bytearray()
            async for block in request.stream():
                raw.extend(block)
                if len(raw) > self.config.request_max_bytes:
                    return JSONResponse({"error": {"message": "Request too large"}}, status_code=413)
            try:
                body = json.loads(raw)
                names = {t.get("function", t).get("name") for t in body.get("tools", [])}
                if body.get("model") != self.config.model or (names != allowed and not (compact and not names)):
                    raise ValueError("Model or tool scope changed")
                output_limit = self.config.compaction_max_tokens if compact else self.config.max_output_tokens
                if any(isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= output_limit
                       for key, value in body.items() if key in {"max_tokens", "max_completion_tokens", "max_output_tokens"}):
                    raise ValueError("Output budget exceeded")
            except (ValueError, TypeError, AttributeError):
                return JSONResponse({"error": {"message": "Invalid model/tool request"}}, status_code=400)
            count = self.compaction_requests if compact else self.model_requests
            if count >= limit or time.monotonic() >= self.deadline:
                self.stop_reason = ('compaction_limit' if compact else "round_limit") if count >= limit else "deadline"
                return JSONResponse({"error": {"message": "Investigation budget exhausted"}}, status_code=429)
            if not any(key in body for key in ("max_tokens", "max_completion_tokens", "max_output_tokens")):
                body["max_output_tokens" if self.config.api == "openai-responses" else "max_tokens"] = output_limit
            # The proxy is the dispatch barrier: the next call cannot be billed
            # until the application has saved this exact logical request.
            if compact:
                self.compaction_requests += 1
                # Caller-thread-owned source ledger; no database/session access
                # from the ASGI thread. Contract errors are retrieval failures,
                # while persistence errors still propagate via dispatch().
                prepared = await self.submit('compaction_prepare', body)
                if isinstance(prepared, dict):
                    self.compaction_errors += 1
                    await self.submit('compaction_result', {'accepted': False, 'request': self.compaction_requests,
                                      'error_type': 'CompactionContractError'})
                    return JSONResponse({'error': {'message': prepared['error']}}, status_code=422)
                body, anchors = prepared
            else:
                self.model_requests += 1
            if self.config.enable_thinking is not None:
                body["enable_thinking"] = self.config.enable_thinking
            if len(json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()) > self.config.request_max_bytes:
                if compact:
                    self.compaction_errors += 1
                    await self.submit('compaction_result', {'accepted': False, 'request': self.compaction_requests,
                                      'error_type': 'PreparedRequestTooLarge'})
                return JSONResponse({'error': {'message': 'Prepared model request too large'}}, status_code=413)
            await self.submit("model_request", {"round": self.compaction_requests if compact else self.model_requests,
                              'purpose': 'compaction' if compact else 'investigation', "model": self.config.model,
                              "base_url": self.config.base_url, "api": self.config.api, "body": body})
            if self.closed or time.monotonic() >= self.deadline:
                return JSONResponse({"error": {"message": "Investigation closed"}}, status_code=503)
            client = httpx.AsyncClient(timeout=min(self.config.model_timeout_seconds, max(0.1, self.deadline - time.monotonic())), trust_env=False)
            try:
                upstream = await client.send(client.build_request(
                    "POST", self.config.base_url.rstrip("/") + "/" + expected, json=body,
                    headers={"Authorization": "Bearer " + self.config.api_key}), stream=True)
            except httpx.HTTPError:
                await client.aclose()
                if compact:
                    self.compaction_errors += 1
                    await self.submit('compaction_result', {'accepted': False, 'request': self.compaction_requests,
                                      'error_type': 'ModelTransportError'})
                return JSONResponse({"error": {"message": "Model transport failed"}}, status_code=502)
            if upstream.status_code >= 400:
                code = upstream.status_code
                await upstream.aclose()
                await client.aclose()
                if compact:
                    self.compaction_errors += 1
                    await self.submit('compaction_result', {'accepted': False, 'request': self.compaction_requests,
                                      'error_type': 'ModelProviderError', 'status': code})
                return JSONResponse({"error": {"message": "Model request rejected", "status": code}}, status_code=code)

            if compact:
                # Buffer only the bounded auxiliary response. Rejected output
                # never reaches DSH, so its native engine cannot commit it.
                raw = bytearray()
                content_type = upstream.headers.get('content-type', 'application/json')
                usage = None
                try:
                    async for chunk in upstream.aiter_bytes():
                        if time.monotonic() >= self.deadline:
                            raise CompactionContractError('Investigation deadline reached')
                        raw.extend(chunk)
                        if len(raw) > 16_000_000:
                            raise CompactionContractError('Compaction response exceeds size limit')
                    usage = reported_summary_usage(bytes(raw), content_type)
                    if usage is not None:
                        self.compaction_usage.append(usage)
                    summary = summary_from_response(bytes(raw), content_type)
                    validate_summary(summary, anchors)
                except (ValueError, TypeError, KeyError, UnicodeError, httpx.HTTPError) as exc:
                    self.compaction_errors += 1
                    await self.submit('compaction_result', {'accepted': False, 'request': self.compaction_requests,
                                      'error_type': type(exc).__name__, 'usage': usage,
                                      'preserved_evidence_ids': [a['evidence_id'] for a in anchors]})
                    return JSONResponse({'error': {'message': 'Compaction preservation contract failed'}}, status_code=422)
                finally:
                    await upstream.aclose()
                    await client.aclose()
                await self.submit('compaction_result', {'accepted': True, 'request': self.compaction_requests,
                                  'preserved_evidence_ids': [a['evidence_id'] for a in anchors], 'summary': summary, 'usage': usage})
                return Response(bytes(raw), media_type=content_type)

            async def cleanup():
                await upstream.aclose()
                await client.aclose()

            async def response_body():
                try:
                    async for chunk in upstream.aiter_raw():
                        yield chunk
                finally:
                    await cleanup()

            return StreamingResponse(response_body(), status_code=upstream.status_code,
                                     media_type=upstream.headers.get("content-type", "text/event-stream"))

        app = Server("retrospective", on_list_tools=list_tools, on_call_tool=call_tool,
                     get_tool_input_schema=lambda name: next(
                         (spec["parameters"] for spec in definitions if spec["name"] == name), None),
        ).streamable_http_app(json_response=True, stateless_http=True, max_request_body_size=16_384,
                              custom_starlette_routes=[Route("/model/{path:path}", proxy, methods=["POST"]),
                                  Route('/compaction-model/{path:path}', proxy, methods=['POST'])])
        token = self.token

        async def authenticated(scope, receive, send):
            if scope["type"] == "http":
                headers = dict(scope["headers"])
                expected = ("Bearer " + token).encode()
                host = headers.get(b"host", b"").split(b":")[0]
                if host != b"127.0.0.1" or not secrets.compare_digest(headers.get(b"authorization", b""), expected):
                    await JSONResponse({"error": "Unauthorized"}, status_code=401)(scope, receive, send)
                    return
            await app(scope, receive, send)
        return authenticated

    def __enter__(self):
        import uvicorn
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", 0))
            sock.listen(32)
            self.url = f"http://127.0.0.1:{sock.getsockname()[1]}"
            self._socket = sock
            self._server = uvicorn.Server(uvicorn.Config(self._app(), log_level="critical", access_log=False,
                                          timeout_graceful_shutdown=1, log_config=None))
            self._thread = threading.Thread(target=self._server.run, kwargs={"sockets": [sock]}, daemon=True)
            self._thread.start()
            started = time.monotonic()
            while not self._server.started:
                if not self._thread.is_alive() or time.monotonic() - started > 5:
                    raise RuntimeError("Investigation gateway failed to start")
                time.sleep(0.01)
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        with self._pending_lock:
            self.closed = True
            for future in list(self.pending):
                if not future.done() and not future.running():
                    future.set_exception(RuntimeError("Investigation closed"))
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=3)
        if self._socket:
            self._socket.close()

    def __exit__(self, *_args):
        self.close()
