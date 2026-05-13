from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import AgentExecution, AgentRole, AgentTask, CompressedError, ContextBundle, IncidentReport, RepairStep

if TYPE_CHECKING:
    from .agent_tools import AgentToolExecutor


class AgentReasoningError(RuntimeError):
    pass


class LLMReasoningClient:
    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = settings or {}

    def enabled(self) -> bool:
        return bool(self.settings.get("enabled")) and bool(self.settings.get("api_key")) and bool(self.settings.get("model"))

    def reason(
        self,
        task: AgentTask,
        incident: IncidentReport,
        compressed_error: CompressedError,
        context: ContextBundle,
        fallback_steps: list[RepairStep],
        *,
        tool_executor: "AgentToolExecutor | None" = None,
        initial_tool_results: list[dict[str, Any]] | None = None,
    ) -> AgentExecution:
        if not self.enabled():
            raise AgentReasoningError("LLM agent reasoning is disabled.")

        tool_results = list(initial_tool_results or [])
        payload: dict[str, Any] = {}
        response: dict[str, Any] = {}
        max_tool_rounds = max(int(self.settings.get("max_tool_rounds", 2)), 0)
        for _ in range(max_tool_rounds + 1):
            response = self._dispatch_request(task, incident, compressed_error, context, fallback_steps, tool_results=tool_results)
            raw_text = self._extract_output_text(response)
            payload = self._extract_json_object(raw_text)
            tool_calls = self._parse_tool_calls(payload)
            if tool_calls and tool_executor is not None:
                new_results = [tool_executor.execute(call["tool_name"], call["arguments"]) for call in tool_calls]
                tool_results.extend(new_results)
                if not str(payload.get("summary", "")).strip():
                    continue
            break

        summary = str(payload.get("summary", "")).strip()
        if not summary:
            raise AgentReasoningError("LLM agent reasoning returned an empty summary.")

        steps = self._parse_steps(payload, context, fallback_steps)
        likelihood = min(max(float(payload.get("success_likelihood", 0.72)), 0.05), 0.99)
        usage = self._usage_payload(response)
        token_estimate = max(
            usage.get("total_tokens", 0),
            max(len(summary) // 4, 1) + sum(len(step.description) // 4 for step in steps),
        )
        return AgentExecution(
            task_id=task.id,
            role=task.role,
            reasoning_summary=summary,
            repair_steps=steps,
            token_estimate=min(token_estimate, task.budget),
            success_likelihood=likelihood,
            metadata={
                "reasoning_source": "llm",
                "llm_provider": self.settings.get("provider", ""),
                "llm_model": self.settings.get("model", ""),
                "tool_results": tool_results,
                "executed_tool_count": len(tool_results),
            },
        )

    def _dispatch_request(
        self,
        task: AgentTask,
        incident: IncidentReport,
        compressed_error: CompressedError,
        context: ContextBundle,
        fallback_steps: list[RepairStep],
        *,
        tool_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        system_prompt = (
            "You are one specialist sub-agent in an autonomous repository maintenance system. "
            f"Your role is `{task.role.value}`. Return only a JSON object. "
            "Think narrowly, use the provided context, and avoid repository-wide edits when possible."
        )
        user_payload = {
            "role": task.role.value,
            "negotiation_round": task.negotiation_round,
            "allowed_tools": task.allowed_tools,
            "sandbox_policy": task.sandbox_policy,
            "objective": task.objective,
            "incident": {
                "title": incident.title,
                "description": incident.description,
            },
            "compressed_error": {
                "error_type": compressed_error.error_type.value,
                "error_name": compressed_error.error_name,
                "semantic_summary": compressed_error.semantic_summary,
                "root_cause_cluster": compressed_error.root_cause_cluster,
                "keywords": compressed_error.keywords[:8],
                "key_stack_frames": compressed_error.key_stack_frames[:4],
            },
            "context": {
                "relevant_files": context.relevant_files[:6],
                "semantic_focus": context.semantic_focus[:8],
                "working_context": json.dumps(context.working_context, ensure_ascii=False)[: int(self.settings.get("max_context_chars", 6000))],
                "short_term_memories": context.short_term_memories[:3],
                "long_term_memories": context.long_term_memories[:3],
                "matched_skills": [
                    {
                        "name": skill.name,
                        "description": skill.description,
                        "action_template": skill.action_template,
                        "success_rate": round(skill.success_rate, 3),
                    }
                    for skill in context.matched_skills[:3]
                ],
                "tool_results": tool_results[-8:],
            },
            "fallback_steps": [
                {
                    "title": step.title,
                    "description": step.description,
                    "target_files": step.target_files,
                    "confidence": step.confidence,
                }
                for step in fallback_steps[:4]
            ],
            "response_schema": {
                "summary": "string",
                "success_likelihood": "number between 0 and 1",
                "steps": [
                    {
                        "title": "string",
                        "description": "string",
                        "target_files": ["relative/path.py"],
                        "confidence": "number between 0 and 1",
                    }
                ],
                "tool_calls": [
                    {
                        "tool_name": "string from allowed_tools",
                        "arguments": {"files": ["relative/path.py"]},
                    }
                ],
            },
        }
        mode = str(self.settings.get("compatibility_mode", "")).strip().lower()
        if mode == "openai_responses":
            payload = {
                "model": self.settings["model"],
                "input": [
                    {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                    {"role": "user", "content": [{"type": "input_text", "text": json.dumps(user_payload, ensure_ascii=False)}]},
                ],
                "text": {"format": {"type": "json_object"}},
                "max_output_tokens": int(self.settings.get("max_output_tokens", 2000)),
            }
            return self._post_json(self._endpoint("/responses"), payload, self._auth_headers())
        if mode == "openai_compatible_chat":
            payload = {
                "model": self.settings["model"],
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
                "max_tokens": int(self.settings.get("max_output_tokens", 2000)),
            }
            return self._post_json(self._endpoint("/chat/completions"), payload, self._auth_headers())
        if mode == "google_gemini":
            query = urlencode({"key": self.settings["api_key"]})
            payload = {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"role": "user", "parts": [{"text": json.dumps(user_payload, ensure_ascii=False)}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "maxOutputTokens": int(self.settings.get("max_output_tokens", 2000)),
                    "temperature": 0.1,
                },
            }
            endpoint = f"{str(self.settings.get('base_url', '')).rstrip('/')}/models/{self.settings['model']}:generateContent?{query}"
            return self._post_json(endpoint, payload, {"Content-Type": "application/json", **dict(self.settings.get('extra_headers', {}))})
        raise AgentReasoningError(f"Unsupported LLM compatibility mode: {self.settings.get('compatibility_mode')}")

    def _parse_steps(self, payload: dict[str, Any], context: ContextBundle, fallback_steps: list[RepairStep]) -> list[RepairStep]:
        raw_steps = payload.get("steps", [])
        if not isinstance(raw_steps, list):
            raw_steps = []
        parsed: list[RepairStep] = []
        for item in raw_steps[: int(self.settings.get("max_steps", 4))]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            description = str(item.get("description", "")).strip()
            if not title or not description:
                continue
            target_files = [str(path).replace("\\", "/") for path in item.get("target_files", []) if str(path).strip()]
            if not target_files:
                target_files = context.relevant_files[:3]
            parsed.append(
                RepairStep(
                    title=title,
                    description=description,
                    target_files=target_files,
                    confidence=min(max(float(item.get("confidence", 0.72)), 0.05), 0.99),
                )
            )
        return parsed or fallback_steps

    def _parse_tool_calls(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        raw_calls = payload.get("tool_calls", [])
        if not isinstance(raw_calls, list):
            return []
        calls: list[dict[str, Any]] = []
        for item in raw_calls[: int(self.settings.get("max_tool_rounds", 2)) + 2]:
            if not isinstance(item, dict):
                continue
            tool_name = str(item.get("tool_name", "")).strip()
            arguments = item.get("arguments", {})
            if not tool_name:
                continue
            calls.append(
                {
                    "tool_name": tool_name,
                    "arguments": arguments if isinstance(arguments, dict) else {},
                }
            )
        return calls

    def _endpoint(self, suffix: str) -> str:
        return str(self.settings.get("base_url", "")).rstrip("/") + suffix

    def _post_json(self, endpoint: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        request = Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with urlopen(request, timeout=int(self.settings.get("timeout_seconds", 60))) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise AgentReasoningError(f"LLM agent reasoning returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise AgentReasoningError(f"Failed to reach LLM agent reasoning endpoint: {exc}") from exc

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **dict(self.settings.get("extra_headers", {}))}
        api_key = self.settings.get("api_key")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _extract_output_text(self, response: dict[str, Any]) -> str:
        mode = str(self.settings.get("compatibility_mode", "")).strip().lower()
        if mode == "openai_responses":
            direct_text = response.get("output_text")
            if isinstance(direct_text, str) and direct_text.strip():
                return direct_text
            for item in response.get("output", []):
                if item.get("type") != "message":
                    continue
                for content in item.get("content", []):
                    if isinstance(content, dict):
                        text = content.get("text")
                        if isinstance(text, str) and text.strip():
                            return text
            raise AgentReasoningError("Responses API returned no text output for agent reasoning.")
        if mode == "openai_compatible_chat":
            choices = response.get("choices", [])
            if choices:
                message = choices[0].get("message", {})
                content = message.get("content", "")
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and isinstance(item.get("text"), str):
                            return item["text"]
                if isinstance(content, str) and content.strip():
                    return content
            raise AgentReasoningError("Chat completions API returned no text output for agent reasoning.")
        if mode == "google_gemini":
            candidates = response.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                text_parts = [str(part.get("text", "")) for part in parts if isinstance(part, dict) and part.get("text")]
                if text_parts:
                    return "\n".join(text_parts)
            raise AgentReasoningError("Google Gemini API returned no text output for agent reasoning.")
        raise AgentReasoningError(f"Unsupported LLM compatibility mode: {self.settings.get('compatibility_mode')}")

    def _usage_payload(self, response: dict[str, Any]) -> dict[str, int]:
        usage = response.get("usage", {}) if isinstance(response.get("usage"), dict) else {}
        mode = str(self.settings.get("compatibility_mode", "")).strip().lower()
        if mode == "google_gemini":
            metadata = response.get("usageMetadata", {})
            return {
                "input_tokens": int(metadata.get("promptTokenCount", 0)),
                "output_tokens": int(metadata.get("candidatesTokenCount", 0)),
                "total_tokens": int(metadata.get("totalTokenCount", 0)),
            }
        if mode == "openai_compatible_chat":
            return {
                "input_tokens": int(usage.get("prompt_tokens", 0)),
                "output_tokens": int(usage.get("completion_tokens", 0)),
                "total_tokens": int(usage.get("total_tokens", 0)),
            }
        return {
            "input_tokens": int(usage.get("input_tokens", 0)),
            "output_tokens": int(usage.get("output_tokens", 0)),
            "total_tokens": int(usage.get("total_tokens", 0)),
        }

    def _extract_json_object(self, raw_text: str) -> dict[str, Any]:
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            start = raw_text.find("{")
            end = raw_text.rfind("}")
            if start < 0 or end <= start:
                raise AgentReasoningError("Model did not return valid JSON for agent reasoning.")
            payload = json.loads(raw_text[start : end + 1])
        if not isinstance(payload, dict):
            raise AgentReasoningError("Agent reasoning response JSON must be an object.")
        return payload
