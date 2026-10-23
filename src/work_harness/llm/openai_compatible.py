from __future__ import annotations

import json
import time
from typing import Any

import httpx

from work_harness.domain.enums import MessageRole
from work_harness.domain.models import ModelMessage, ModelRequest, ModelResponse, Usage
from work_harness.llm.base import (
    AuthenticationModelError,
    InvalidModelRequestError,
    TransientModelError,
)
from work_harness.llm.http_provider import HTTPModelProvider


class OpenAICompatibleProvider(HTTPModelProvider):
    """Custom OpenAI-protocol adapter with an optional DeepSeek dialect."""

    capabilities = frozenset({"chat", "structured_output", "tool_calling"})
    protocol_version = "1.0"

    async def complete(self, request: ModelRequest, *, model: str) -> ModelResponse:
        if not self._base_url or not self._api_key:
            raise AuthenticationModelError(
                f"provider {self.name} is waiting for endpoint or credential configuration"
            )
        if model not in self._config.allowed_models:
            raise InvalidModelRequestError(f"model is not allowlisted: {model}")
        if self._config.api_family == "responses":
            endpoint = self._endpoint("responses")
            payload = self._responses_payload(request, model)
        else:
            endpoint = self._endpoint("chat/completions")
            payload = self._chat_payload(request, model)

        started = time.perf_counter()
        try:
            response = await self._client.post(
                endpoint,
                headers=self._headers(),
                follow_redirects=False,
                json=payload,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientModelError("model transport failed") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)
        self._raise_for_status(response)
        data = self._json_envelope(response)
        if self._config.api_family == "responses":
            return self._parse_responses(data, response, request, model, latency_ms)
        return self._parse_chat(data, response, request, model, latency_ms)

    def _chat_payload(self, request: ModelRequest, model: str) -> dict[str, object]:
        messages = [self._chat_message(message) for message in request.messages]
        payload: dict[str, object] = {"model": model, "messages": messages}
        token_parameter = self._config.chat_token_parameter
        if token_parameter is None:
            token_parameter = (
                "max_tokens"
                if self._config.protocol_variant == "deepseek"
                else "max_completion_tokens"
            )
        payload[token_parameter] = request.max_output_tokens
        if request.reasoning_effort is not None:
            payload["reasoning_effort"] = request.reasoning_effort
        if request.tools:
            payload["tools"] = request.tools
        if request.response_schema:
            output_mode = self._config.structured_output_mode
            if output_mode is None:
                output_mode = (
                    "json_object"
                    if self._config.protocol_variant == "deepseek"
                    else "json_schema"
                )
            if output_mode == "json_object":
                payload["response_format"] = {"type": "json_object"}
                messages.insert(
                    0,
                    {
                        "role": "system",
                        "content": self._schema_instruction(request),
                    },
                )
            else:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": self._json_schema_format(request),
                }
        return payload

    def _chat_message(self, message: ModelMessage) -> dict[str, object]:
        payload = message.model_dump(
            mode="json",
            exclude_none=True,
            exclude={"reasoning_content", "response_items"},
        )
        if self._config.protocol_variant == "deepseek" and message.reasoning_content:
            payload["reasoning_content"] = message.reasoning_content
        return payload

    def _responses_payload(self, request: ModelRequest, model: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": model,
            "input": self._responses_input(request.messages),
            "max_output_tokens": request.max_output_tokens,
            "store": self._config.store_responses,
        }
        if request.reasoning_effort is not None:
            payload["reasoning"] = {"effort": request.reasoning_effort}
        if request.tools:
            payload["tools"] = [self._responses_tool(tool) for tool in request.tools]
        if request.response_schema:
            output_mode = self._config.structured_output_mode or "json_schema"
            if output_mode == "json_object":
                payload["text"] = {"format": {"type": "json_object"}}
                inputs = payload["input"]
                if not isinstance(inputs, list):
                    raise InvalidModelRequestError("responses input must be a list")
                inputs.insert(
                    0,
                    {
                        "role": "system",
                        "content": self._schema_instruction(request),
                    },
                )
            else:
                payload["text"] = {
                    "format": {
                        "type": "json_schema",
                        **self._json_schema_format(request),
                    }
                }
        return payload

    @staticmethod
    def _responses_input(messages: list[ModelMessage]) -> list[dict[str, object]]:
        inputs: list[dict[str, object]] = []
        for message in messages:
            if message.role == MessageRole.TOOL:
                inputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.tool_call_id or "",
                        "output": message.content,
                    }
                )
                continue
            if message.response_items and not any(
                item.get("type") in {"text", "tool_use", "thinking", "redacted_thinking"}
                for item in message.response_items
            ):
                inputs.extend(dict(item) for item in message.response_items)
                continue
            if message.content:
                inputs.append({"role": str(message.role), "content": message.content})
            for raw_call in message.tool_calls or []:
                function = raw_call.get("function")
                call_id = raw_call.get("id")
                if not isinstance(function, dict) or not isinstance(call_id, str):
                    raise InvalidModelRequestError("invalid assistant tool call")
                name = function.get("name")
                arguments = function.get("arguments", "{}")
                if not isinstance(name, str):
                    raise InvalidModelRequestError("assistant tool call is missing a name")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
                inputs.append(
                    {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": name,
                        "arguments": arguments,
                    }
                )
        return inputs

    @staticmethod
    def _responses_tool(tool: dict[str, Any]) -> dict[str, Any]:
        if tool.get("type") != "function" or not isinstance(tool.get("function"), dict):
            raise InvalidModelRequestError("unsupported Responses API tool definition")
        function = tool["function"]
        name = function.get("name")
        parameters = function.get("parameters")
        if not isinstance(name, str) or not isinstance(parameters, dict):
            raise InvalidModelRequestError("invalid Responses API function definition")
        converted: dict[str, Any] = {
            "type": "function",
            "name": name,
            "parameters": parameters,
        }
        description = function.get("description")
        if isinstance(description, str):
            converted["description"] = description
        strict = function.get("strict")
        if isinstance(strict, bool):
            converted["strict"] = strict
        return converted

    def _parse_chat(
        self,
        data: dict[str, Any],
        response: httpx.Response,
        request: ModelRequest,
        model: str,
        latency_ms: int,
    ) -> ModelResponse:
        try:
            choice = data["choices"][0]
            message = choice["message"]
            if not isinstance(message, dict):
                raise TypeError("message must be an object")
            content = message.get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise InvalidModelRequestError(
                "provider returned an invalid response envelope"
            ) from exc
        if not isinstance(content, str):
            raise InvalidModelRequestError("provider returned an invalid message")
        if message.get("refusal"):
            raise InvalidModelRequestError("model provider refused the structured request")
        structured_output = self._structured_output(request, message.get("parsed"), content)
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        prompt_details = usage.get("prompt_tokens_details")
        if not isinstance(prompt_details, dict):
            prompt_details = {}
        raw_tool_calls = message.get("tool_calls")
        return ModelResponse(
            content=content,
            structured_output=structured_output,
            tool_calls=raw_tool_calls if isinstance(raw_tool_calls, list) else [],
            reasoning_content=(
                message.get("reasoning_content")
                if isinstance(message.get("reasoning_content"), str)
                else None
            ),
            finish_reason=choice.get("finish_reason") or "stop",
            usage=Usage(
                **self._usage_fields(usage, "prompt_tokens", "completion_tokens", prompt_details),
            ),
            latency_ms=latency_ms,
            provider=self.name,
            model=model,
            provider_request_id=self._request_id(response, data),
        )

    def _parse_responses(
        self,
        data: dict[str, Any],
        response: httpx.Response,
        request: ModelRequest,
        model: str,
        latency_ms: int,
    ) -> ModelResponse:
        output = data.get("output")
        if not isinstance(output, list):
            raise InvalidModelRequestError("provider returned an invalid Responses envelope")
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for item in output:
            if not isinstance(item, dict):
                raise InvalidModelRequestError("provider returned an invalid output item")
            if item.get("type") == "function_call":
                call_id = item.get("call_id") or item.get("id")
                name = item.get("name")
                arguments = item.get("arguments", "{}")
                if not isinstance(call_id, str) or not isinstance(name, str):
                    raise InvalidModelRequestError("provider returned an invalid function call")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
                tool_calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": arguments},
                    }
                )
                continue
            if item.get("type") != "message":
                continue
            content_items = item.get("content")
            if not isinstance(content_items, list):
                raise InvalidModelRequestError("provider returned an invalid message content")
            for content_item in content_items:
                if not isinstance(content_item, dict):
                    raise InvalidModelRequestError("provider returned an invalid content item")
                if content_item.get("type") == "refusal":
                    raise InvalidModelRequestError("model provider refused the structured request")
                if content_item.get("type") == "output_text":
                    text = content_item.get("text")
                    if not isinstance(text, str):
                        raise InvalidModelRequestError("provider returned invalid output text")
                    text_parts.append(text)
        content = "".join(text_parts)
        if not content and isinstance(data.get("output_text"), str):
            content = data["output_text"]
        structured_output = self._structured_output(request, None, content)
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        input_details = usage.get("input_tokens_details")
        if not isinstance(input_details, dict):
            input_details = {}
        status = data.get("status")
        incomplete = data.get("incomplete_details")
        if tool_calls:
            finish_reason = "tool_calls"
        elif status == "incomplete" and isinstance(incomplete, dict):
            finish_reason = str(incomplete.get("reason") or "incomplete")
        else:
            finish_reason = "stop" if status in {None, "completed"} else str(status)
        return ModelResponse(
            content=content,
            structured_output=structured_output,
            tool_calls=tool_calls,
            response_items=output,
            finish_reason=finish_reason,
            usage=Usage(
                **self._usage_fields(usage, "input_tokens", "output_tokens", input_details),
            ),
            latency_ms=latency_ms,
            provider=self.name,
            model=model,
            provider_request_id=self._request_id(response, data),
        )
