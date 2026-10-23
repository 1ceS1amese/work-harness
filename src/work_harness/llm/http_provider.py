from __future__ import annotations

import ipaddress
import json
import math
import os
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qsl, urlparse, urlunparse

import httpx

from work_harness.config.endpoints import normalize_model_url
from work_harness.config.models import ProviderConfig
from work_harness.domain.models import ModelRequest
from work_harness.llm.base import (
    AuthenticationModelError,
    InvalidModelRequestError,
    TransientModelError,
)


class HTTPModelProvider:
    """Shared HTTP policy, endpoint construction and response validation."""

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
        address = env_base_url or config.base_url
        self._base_url = normalize_model_url(address) if address else ""
        self._api_key = config.runtime_api_key()
        if self._base_url:
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
            {"https"} if self._config.allow_insecure_http is False else {"https", "http"}
        )
        if parsed.scheme not in allowed_schemes:
            raise ValueError("model base URL must use an allowed HTTP scheme")
        if not parsed.hostname:
            raise ValueError("model base URL must include a hostname")
        try:
            _ = parsed.port
        except ValueError:
            raise ValueError("model base URL must use a valid port") from None
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
            if (
                parsed.hostname.lower().rstrip(".") == "localhost"
                and self._config.allow_private_network is False
            ):
                raise ValueError("localhost model endpoint is blocked by provider policy") from None
        else:
            if (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_reserved
            ) and self._config.allow_private_network is False:
                raise ValueError("private model endpoint is blocked by provider policy")

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def healthcheck(self) -> bool:
        return bool(self._base_url and self._api_key)

    def _endpoint(self, suffix: str) -> str:
        """Append an API path without accidentally appending it to a query string."""

        parsed = urlparse(self._base_url)
        base_path = parsed.path.rstrip("/")
        for endpoint in ("/chat/completions", "/responses", "/messages", "/models"):
            if base_path.endswith(endpoint):
                base_path = base_path[: -len(endpoint)]
                break
        path = f"{base_path or '/v1'}/{suffix}"
        return urlunparse(parsed._replace(path=path, fragment=""))

    @staticmethod
    def _usage_fields(usage, input_key, output_key, details) -> dict[str, Any]:
        # Missing, null or malformed provider counts are unknown, never evidence of zero use.
        def valid(value):
            return type(value) is int and value >= 0

        input_count, output_count = usage.get(input_key), usage.get(output_key)
        cached = details.get("cached_tokens", 0)
        complete = valid(input_count) and valid(output_count)
        cache_valid = valid(cached) and valid(input_count) and cached <= input_count
        return {
            "input_tokens": input_count if valid(input_count) else 0,
            "output_tokens": output_count if valid(output_count) else 0,
            "cached_tokens": cached if cache_valid else 0,
            "usage_complete": complete and cache_valid,
        }

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
            raise InvalidModelRequestError("provider returned invalid structured output") from exc
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
                f"model provider temporary failure: {response.status_code}",
                retry_after_seconds=HTTPModelProvider._retry_after(
                    response.headers.get("retry-after")
                ),
            )
        if response.status_code >= 300:
            raise InvalidModelRequestError(f"model request rejected: {response.status_code}")

    @staticmethod
    def _retry_after(value: str | None) -> float | None:
        if value is None:
            return None
        try:
            seconds = float(value)
        except ValueError:
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    return None
                seconds = (date - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                return None
        return max(0.0, seconds) if math.isfinite(seconds) else None

    @staticmethod
    def _request_id(response: httpx.Response, data: dict[str, Any]) -> str | None:
        header_id = (
            response.headers.get("request-id")
            or response.headers.get("x-request-id")
            or response.headers.get("x-ds-request-id")
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

    def _headers(self) -> dict[str, str]:
        scheme = self._config.auth_scheme
        claude = self._config.api_family == "messages"
        if scheme == "x_api_key" or (scheme == "auto" and claude):
            headers = {"x-api-key": self._api_key}
        else:
            headers = {"Authorization": f"Bearer {self._api_key}"}
        if claude:
            headers["anthropic-version"] = "2023-06-01"
        return headers
