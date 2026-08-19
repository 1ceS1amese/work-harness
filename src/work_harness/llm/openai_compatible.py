from __future__ import annotations

import ipaddress
import json
import os
import re
import time
from typing import Any
from urllib.parse import parse_qsl, urlparse

import httpx

from work_harness.config.models import ProviderConfig
from work_harness.domain.enums import MessageRole
from work_harness.domain.models import ModelMessage, ModelRequest, ModelResponse, Usage
from work_harness.llm.base import (
    AuthenticationModelError,
    InvalidModelRequestError,
    TransientModelError,
)


class OpenAICompatibleProvider:
    """Custom OpenAI-protocol adapter with an optional DeepSeek dialect."""

    capabilities = frozenset({"chat", "structured_output", "tool_calling"})
    protocol_version = "1.0"

    def __init__(
        self,
        name: str,
        config: ProviderConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self._config = config
        env_base_url = os.getenv(config.base_url_env or "", "").strip()
        self._base_url = (env_base_url or config.base_url or "").rstrip("/")
        self._api_key = os.getenv(config.api_key_env or "", "")
        if not self._base_url or not self._api_key:
            raise ValueError(
                f"provider {name} is enabled but required endpoint or credential is missing"
            )
        self._validate_base_url(self._base_url)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=config.timeout_seconds,
            limits=httpx.Limits(
                max_connections=config.max_concurrency,
                max_keepalive_connections=config.max_concurrency,
            ),
        )

    def _validate_base_url(self, value: str) -> None:
        parsed = urlparse(value)
        allowed_schemes = (
            {"https", "http"} if self._config.allow_insecure_http else {"https"}
        )
        if parsed.scheme not in allowed_schemes:
            raise ValueError("model base URL must use an allowed HTTP scheme")
        if not parsed.hostname:
            raise ValueError("model base URL must include a hostname")
        if parsed.username or parsed.password:
            raise ValueError("model base URL cannot contain credentials")
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
            normalized = key.strip().lower().replace("-", "_")
            if normalized == "authorization" or normalized.endswith(
                ("api_key", "token", "secret", "password")
            ):
                raise ValueError("model base URL cannot contain credential query fields")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            if parsed.hostname.lower() == "localhost" and not self._config.allow_private_network:
                raise ValueError(
                    "localhost model endpoint is blocked by provider policy"
                ) from None
        else:
            if (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_reserved
            ) and not self._config.allow_private_network:
                raise ValueError("private model endpoint is blocked by provider policy")

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def healthcheck(self) -> bool:
        return bool(self._base_url and self._api_key)

    async def complete(self, request: ModelRequest, *, model: str) -> ModelResponse:
        if model not in self._config.allowed_models:
            raise InvalidModelRequestError(f"model is not allowlisted: {model}")
        if self._config.api_family == "responses":
            endpoint = f"{self._base_url}/responses"
            payload = self._responses_payload(request, model)
        else:
            endpoint = f"{self._base_url}/chat/completions"
            payload = self._chat_payload(request, model)

        started = time.perf_counter()
        try:
            response = await self._client.post(
                endpoint,
                headers={"Authorization": f"Bearer {self._api_key}"},
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
        payload[token_parameter] = request.token_budget
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
            "max_output_tokens": request.token_budget,
            "store": self._config.store_responses,
        }
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
            if message.response_items:
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
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                cached_tokens=prompt_details.get("cached_tokens", 0),
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
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cached_tokens=input_details.get("cached_tokens", 0),
            ),
            latency_ms=latency_ms,
            provider=self.name,
            model=model,
            provider_request_id=self._request_id(response, data),
        )

    @staticmethod
    def _structured_output(
        request: ModelRequest, parsed: Any, content: str
    ) -> dict[str, Any] | None:
        if request.response_schema is None:
            return None
        if isinstance(parsed, dict):
            return parsed
        if not content:
            return None
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise InvalidModelRequestError(
                "provider returned invalid structured output"
            ) from exc
        if not isinstance(value, dict):
            raise InvalidModelRequestError("provider structured output must be an object")
        return value

    @staticmethod
    def _json_envelope(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise InvalidModelRequestError("provider returned a non-JSON response") from exc
        if not isinstance(data, dict):
            raise InvalidModelRequestError("provider returned an invalid response envelope")
        return data

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code in {401, 403}:
            raise AuthenticationModelError("model provider authentication failed")
        if response.status_code in {408, 409, 429} or response.status_code >= 500:
            raise TransientModelError(
                f"model provider temporary failure: {response.status_code}"
            )
        if response.status_code >= 400:
            raise InvalidModelRequestError(
                f"model request rejected: {response.status_code}"
            )

    @staticmethod
    def _request_id(response: httpx.Response, data: dict[str, Any]) -> str | None:
        header_id = response.headers.get("x-request-id") or response.headers.get(
            "x-ds-request-id"
        )
        response_id = data.get("id")
        return header_id or (response_id if isinstance(response_id, str) else None)

    @staticmethod
    def _schema_name(request: ModelRequest) -> str:
        value = re.sub(r"[^A-Za-z0-9_-]", "_", request.node_name)[:64]
        return value or "structured_output"

    def _json_schema_format(self, request: ModelRequest) -> dict[str, object]:
        return {
            "name": self._schema_name(request),
            "strict": True,
            "schema": request.response_schema or {},
        }

    def _schema_instruction(self, request: ModelRequest) -> str:
        schema = json.dumps(
            request.response_schema or {},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"Return only one valid json object matching this JSON Schema: {schema}"
