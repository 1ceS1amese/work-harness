from __future__ import annotations

import ipaddress
import json
import os
import time
from urllib.parse import urlparse

import httpx

from work_harness.config.models import ProviderConfig
from work_harness.domain.models import ModelRequest, ModelResponse, Usage
from work_harness.llm.base import (
    AuthenticationModelError,
    InvalidModelRequestError,
    TransientModelError,
)


class OpenAICompatibleProvider:
    """Minimal Chat Completions adapter behind the project's stable provider contract."""

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
        self._base_url = os.getenv(config.base_url_env or "", "").rstrip("/")
        self._api_key = os.getenv(config.api_key_env or "", "")
        if not self._base_url or not self._api_key:
            raise ValueError(
                f"provider {name} is enabled but required endpoint or credential is missing"
            )
        self._validate_base_url(self._base_url)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=config.timeout_seconds)

    def _validate_base_url(self, value: str) -> None:
        parsed = urlparse(value)
        allowed_schemes = (
            {"https", "http"} if self._config.allow_insecure_http else {"https"}
        )
        if parsed.scheme not in allowed_schemes:
            raise ValueError("model base URL must use an allowed HTTP scheme")
        if not parsed.hostname:
            raise ValueError("model base URL must include a hostname")
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
        payload: dict[str, object] = {
            "model": model,
            "messages": [
                message.model_dump(mode="json", exclude_none=True)
                for message in request.messages
            ],
            "max_completion_tokens": request.token_budget,
        }
        if request.tools:
            payload["tools"] = request.tools
        if request.response_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.node_name.replace("-", "_")[:64],
                    "strict": True,
                    "schema": request.response_schema,
                },
            }

        started = time.perf_counter()
        try:
            response = await self._client.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientModelError("model transport failed") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        if response.status_code in {401, 403}:
            raise AuthenticationModelError("model provider authentication failed")
        if response.status_code in {408, 429} or response.status_code >= 500:
            raise TransientModelError(f"model provider temporary failure: {response.status_code}")
        if response.status_code >= 400:
            raise InvalidModelRequestError(f"model request rejected: {response.status_code}")

        try:
            data = response.json()
        except ValueError as exc:
            raise InvalidModelRequestError("provider returned a non-JSON response") from exc
        if not isinstance(data, dict):
            raise InvalidModelRequestError("provider returned an invalid response envelope")
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
        structured_output = None
        if request.response_schema:
            parsed = message.get("parsed")
            if isinstance(parsed, dict):
                structured_output = parsed
            elif content:
                try:
                    structured_output = json.loads(content)
                except json.JSONDecodeError as exc:
                    raise InvalidModelRequestError(
                        "provider returned invalid structured output"
                    ) from exc

        usage = data.get("usage", {})
        return ModelResponse(
            content=content,
            structured_output=structured_output,
            tool_calls=(
                message.get("tool_calls")
                if isinstance(message.get("tool_calls"), list)
                else []
            ),
            finish_reason=choice.get("finish_reason") or "stop",
            usage=Usage(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                cached_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
            ),
            latency_ms=latency_ms,
            provider=self.name,
            model=model,
            provider_request_id=response.headers.get("x-request-id"),
        )
