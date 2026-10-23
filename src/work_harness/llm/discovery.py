from __future__ import annotations

import asyncio
import json

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from work_harness.config.models import ProviderConfig
from work_harness.llm.base import (
    AuthenticationModelError,
    InvalidModelRequestError,
    TransientModelError,
)
from work_harness.llm.http_provider import HTTPModelProvider


class ModelDiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: ProviderConfig
    provider_name: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    api_key: SecretStr | None = Field(default=None, min_length=1, max_length=65_536)
    clear_api_key: bool = False

    @model_validator(mode="after")
    def validate_connection(self) -> ModelDiscoveryRequest:
        if self.config.adapter not in {"openai_compatible", "anthropic"}:
            raise ValueError("model discovery requires an HTTP provider")
        if self.api_key and self.clear_api_key:
            raise ValueError("cannot set and clear a credential together")
        return self


class UpstreamModel(BaseModel):
    id: str
    name: str


class ModelDiscoveryResult(BaseModel):
    items: list[UpstreamModel]
    truncated: bool = False


async def discover_models(
    config: ProviderConfig,
    *,
    client: httpx.AsyncClient | None = None,
) -> ModelDiscoveryResult:
    """Read the inference credential's model catalog; never persist or infer capabilities."""
    provider = HTTPModelProvider("discovery", config, client=client)
    try:
        if not await provider.healthcheck():
            raise AuthenticationModelError("请填写上游地址和 API Key 后获取模型")
        async with asyncio.timeout(min(config.timeout_seconds, 30)):
            return await _read_pages(provider)
    except (TimeoutError, httpx.TimeoutException, httpx.TransportError) as exc:
        raise TransientModelError("获取模型超时或连接失败，请检查上游地址和网络") from exc
    finally:
        await provider.close()


async def _read_pages(provider: HTTPModelProvider) -> ModelDiscoveryResult:
    items: dict[str, UpstreamModel] = {}
    cursor: str | None = None
    cursors: set[str] = set()
    for _ in range(20):
        # Use only the configured origin/path, never a pagination URL supplied upstream.
        url = httpx.URL(provider._endpoint("models"))
        if cursor:
            url = url.copy_merge_params({"after_id": cursor})
        async with provider._client.stream(
            "GET",
            url,
            headers=provider._headers(),
            follow_redirects=False,
        ) as response:
            if response.status_code in {404, 405}:
                raise InvalidModelRequestError(
                    "上游未开放 /models 接口或基础路径不正确，可手动添加模型 ID"
                )
            provider._raise_for_status(response)
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 2_097_152:
                    raise InvalidModelRequestError("上游模型列表超过 2 MiB，无法读取")
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError) as exc:
            raise InvalidModelRequestError("上游未返回 JSON 模型列表，请检查 API 地址") from exc
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise InvalidModelRequestError("上游模型列表格式不符合 OpenAI / Claude 协议")
        for item in data["data"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise InvalidModelRequestError("上游模型列表包含无效模型 ID")
            model_id = item["id"].strip()
            if not model_id or len(model_id) > 512:
                raise InvalidModelRequestError("上游模型 ID 为空或过长")
            label = item.get("display_name")
            items[model_id] = UpstreamModel(
                id=model_id,
                name=label[:512] if isinstance(label, str) and label else model_id,
            )
            if len(items) >= 5000:
                return ModelDiscoveryResult(items=list(items.values()), truncated=True)
        if not data.get("has_more"):
            return ModelDiscoveryResult(items=list(items.values()))
        cursor = data.get("last_id")
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise InvalidModelRequestError("上游模型分页游标无效，请手动填写模型 ID")
        cursors.add(cursor)
    return ModelDiscoveryResult(items=list(items.values()), truncated=True)
