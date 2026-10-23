from __future__ import annotations

import json
import time
from typing import Any

import httpx

from work_harness.domain.enums import MessageRole
from work_harness.domain.models import ModelRequest, ModelResponse, Usage
from work_harness.llm.base import (
    AuthenticationModelError,
    InvalidModelRequestError,
    TransientModelError,
)
from work_harness.llm.http_provider import HTTPModelProvider


class AnthropicProvider(HTTPModelProvider):
    """Claude Messages adapter, including portable function/tool-result history."""

    capabilities = frozenset({"chat", "structured_output", "tool_calling"})
    protocol_version = "1.0"

    async def complete(self, request: ModelRequest, *, model: str) -> ModelResponse:
        if not self._base_url or not self._api_key:
            raise AuthenticationModelError("provider requires endpoint and credential")
        if model not in self._config.allowed_models:
            raise InvalidModelRequestError(f"model is not allowlisted: {model}")
        payload = self._payload(request, model)
        started = time.perf_counter()
        try:
            response = await self._client.post(
                self._endpoint("messages"),
                headers=self._headers(),
                json=payload,
                follow_redirects=False,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientModelError("model transport failed") from exc
        self._raise_for_status(response)
        return self._parse(
            self._json_envelope(response),
            response,
            request,
            model,
            int((time.perf_counter() - started) * 1000),
        )

    def _payload(self, request: ModelRequest, model: str) -> dict[str, Any]:
        system: list[str] = []
        messages: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role == MessageRole.SYSTEM:
                system.append(message.content)
                continue
            role = "assistant" if message.role == MessageRole.ASSISTANT else "user"
            blocks: list[dict[str, Any]] = []
            if message.role == MessageRole.TOOL:
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id,
                        "content": message.content,
                    }
                )
            elif message.response_items and all(
                item.get("type") in {"text", "tool_use", "thinking", "redacted_thinking"}
                for item in message.response_items
            ):
                # Preserve signed thinking blocks during a Claude tool round-trip.
                blocks = [dict(item) for item in message.response_items]
            else:
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                for call in message.tool_calls or []:
                    try:
                        function = call["function"]
                        arguments = json.loads(function["arguments"])
                        if not isinstance(arguments, dict):
                            raise ValueError("tool arguments must be an object")
                        blocks.append(
                            {
                                "type": "tool_use",
                                "id": call["id"],
                                "name": function["name"],
                                "input": arguments,
                            }
                        )
                    except (KeyError, TypeError, ValueError) as exc:
                        raise InvalidModelRequestError("invalid tool call history") from exc
            if blocks:
                if messages and messages[-1]["role"] == role:
                    messages[-1]["content"].extend(blocks)
                else:
                    messages.append({"role": role, "content": blocks})
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": request.max_output_tokens,
        }
        if request.tools:
            tools = []
            for tool in request.tools:
                function = tool.get("function")
                if tool.get("type") != "function" or not isinstance(function, dict):
                    raise InvalidModelRequestError("Claude supports function tools only")
                if not isinstance(function.get("name"), str):
                    raise InvalidModelRequestError("tool requires a name")
                tools.append(
                    {
                        "name": function["name"],
                        "description": function.get("description", ""),
                        "input_schema": function.get("parameters", {"type": "object"}),
                    }
                )
            payload["tools"] = tools
        if request.response_schema:
            # Prompt JSON is portable across gateways and older Claude models.
            # Native schema enforcement is explicit, because not all channels offer it.
            if self._config.structured_output_mode == "json_schema":
                payload["output_config"] = {
                    "format": {"type": "json_schema", "schema": request.response_schema},
                }
            else:
                system.append(self._schema_instruction(request))
        if request.reasoning_effort is not None:
            payload.setdefault("output_config", {})["effort"] = request.reasoning_effort
            payload["thinking"] = {"type": "adaptive"}
        if system:
            payload["system"] = "\n\n".join(system)
        return payload

    def _parse(self, data, response, request, model, latency_ms) -> ModelResponse:
        blocks = data.get("content")
        if not isinstance(blocks, list) or not blocks:
            raise InvalidModelRequestError("provider returned an invalid Messages envelope")
        text: list[str] = []
        calls: list[dict[str, Any]] = []
        for block in blocks:
            if not isinstance(block, dict):
                raise InvalidModelRequestError("provider returned an invalid content block")
            if block.get("type") == "text":
                if not isinstance(block.get("text"), str):
                    raise InvalidModelRequestError("provider returned invalid text")
                text.append(block["text"])
            elif block.get("type") == "tool_use":
                if (
                    not isinstance(block.get("id"), str)
                    or not isinstance(block.get("name"), str)
                    or not isinstance(block.get("input"), dict)
                ):
                    raise InvalidModelRequestError("provider returned an invalid tool use")
                calls.append(
                    {
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block["input"]),
                        },
                    }
                )
        if data.get("stop_reason") == "refusal":
            raise InvalidModelRequestError("model provider refused the request")
        content = "".join(text)
        return ModelResponse(
            content=content,
            structured_output=None if calls else self._structured_output(request, None, content),
            tool_calls=calls,
            response_items=blocks,
            finish_reason=(
                "tool_calls"
                if calls
                else {
                    "end_turn": "stop",
                    "stop_sequence": "stop",
                    "max_tokens": "length",
                }.get(data.get("stop_reason"), data.get("stop_reason") or "stop")
            ),
            usage=self._usage(data.get("usage")),
            latency_ms=latency_ms,
            provider=self.name,
            model=model,
            provider_request_id=self._request_id(response, data),
        )

    @staticmethod
    def _usage(raw: Any) -> Usage:
        usage = raw if isinstance(raw, dict) else {}
        counts = [
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            usage.get("cache_read_input_tokens", 0),
            usage.get("cache_creation_input_tokens", 0),
        ]
        valid = [type(value) is int and value >= 0 for value in counts]
        normal, output, cached, created = [
            v if ok else 0 for v, ok in zip(counts, valid, strict=True)
        ]
        # Anthropic reports uncached input separately. Our input total includes all input.
        return Usage(
            input_tokens=normal + cached + created,
            output_tokens=output,
            cached_tokens=cached,
            usage_complete=all(valid),
        )
