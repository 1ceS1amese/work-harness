"""Bounded, explicit inference probes. Catalog discovery alone proves no capability."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections import OrderedDict
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from pydantic import Field

from work_harness.config.endpoints import normalize_model_url
from work_harness.config.models import ProviderConfig
from work_harness.config.reasoning import (
    COMMON_EFFORTS,
    Effort,
    ModelSettings,
    ModelVerification,
    ReasoningCheck,
)
from work_harness.domain.models import ModelMessage, ModelRequest
from work_harness.llm.anthropic import AnthropicProvider
from work_harness.llm.base import InvalidModelRequestError
from work_harness.llm.discovery import ModelDiscoveryRequest
from work_harness.llm.openai_compatible import OpenAICompatibleProvider


class ModelVerificationRequest(ModelDiscoveryRequest):
    model: str = Field(min_length=1, max_length=512)
    confirmed_efforts: list[Effort] = Field(default_factory=list, max_length=12)
    efforts: list[Effort] = Field(default_factory=lambda: list(COMMON_EFFORTS), max_length=12)


def connection_fingerprint(config: ProviderConfig, model: str) -> str:
    data = config.model_dump(
        mode="json",
        exclude={
            "model_settings",
            "allowed_models",
            "enabled",
            "timeout_seconds",
            "max_concurrency",
        },
    )
    address = os.getenv(config.base_url_env or "", "").strip() or config.base_url
    data["effective_address"] = normalize_model_url(address) if address else ""
    data["credential_digest"] = hashlib.sha256(config.runtime_api_key().encode()).hexdigest()
    data["capabilities"] = sorted(config.capabilities)
    data["model"] = model
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def current_settings(config: ProviderConfig, model: str) -> ModelSettings | None:
    settings = config.model_settings.get(model)
    if settings and settings.verification.fingerprint == connection_fingerprint(config, model):
        return settings
    return None


class VerificationCache:
    """Only server-produced evidence may enter a saved Provider profile."""

    def __init__(self) -> None:
        self.records: OrderedDict[str, tuple[float, ModelVerification]] = OrderedDict()

    def add(self, result: ModelVerification) -> None:
        self.records[result.id] = (time.monotonic(), result.model_copy(deep=True))
        while len(self.records) > 256:
            self.records.popitem(last=False)

    def validate(self, config: ProviderConfig, previous: ProviderConfig | None) -> None:
        for model, setting in config.model_settings.items():
            if model not in config.allowed_models:
                raise ValueError("推理设置只能引用已选模型")
            report = setting.verification
            if report.fingerprint != connection_fingerprint(config, model):
                raise ValueError("连接信息已改变，请重新测试模型与推理档位")
            old = previous.model_settings.get(model) if previous else None
            if old and old.verification == report:
                continue
            cached = self.records.get(report.id)
            if not cached or time.monotonic() - cached[0] > 3600 or cached[1] != report:
                raise ValueError("验证记录无效或已过期，请重新测试后保存")


async def verify_model(
    config: ProviderConfig,
    model: str,
    efforts: list[str],
    *,
    client: httpx.AsyncClient | None = None,
    confirmed_efforts: list[str] | None = None,
) -> ModelVerification:
    if model not in config.allowed_models:
        raise ValueError("请先将模型加入已选模型")
    adapter = AnthropicProvider if config.api_family == "messages" else OpenAICompatibleProvider
    provider = adapter("verification", config, client=client)
    report = ModelVerification(
        id=uuid4().hex,
        fingerprint=connection_fingerprint(config, model),
        checked_at=datetime.now(UTC),
        model_available=False,
        checks=[],
    )
    try:
        baseline = await _probe(provider, model, None)
        report.checks.append(baseline)
        report.model_available = baseline.status == "supported"
        if not report.model_available:
            return report
        if not efforts:
            return report
        # A gateway that accepts arbitrary effort strings may silently discard the field.
        control = await _probe(provider, model, "invalid_probe_effort")
        validates_parameter = control.status == "unsupported"
        slots = asyncio.Semaphore(2)

        async def check(effort: str) -> ReasoningCheck:
            async with slots:
                result = await _probe(provider, model, effort)
            if result.status == "supported":
                if validates_parameter:
                    result.source = "parameter_validation"
                elif effort in (confirmed_efforts or []):
                    result.source = "channel_confirmation"
                    result.detail = "调用通过；推理参数由渠道使用者确认"
                else:
                    result.status = "unverified"
                    result.detail = "请求成功，但上游未证明校验推理参数；保留默认"
            return result

        report.checks.extend(await asyncio.gather(*(check(e) for e in dict.fromkeys(efforts))))
        return report
    finally:
        await provider.close()


async def _probe(provider, model: str, effort: str | None) -> ReasoningCheck:
    request = ModelRequest(
        run_id="model-verification",
        trace_id=uuid4().hex,
        node_name="verify",
        agent_role="worker",
        logical_model="verification",
        reasoning_effort=effort,
        messages=[
            ModelMessage(role="user", content='Return exactly {"ok":true}. Do not call tools.')
        ],
        max_output_tokens=2048,
        response_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "verification_noop",
                    "description": "Do not invoke this tool.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
            }
        ],
    )
    family = provider._config.api_family
    payload = (
        provider._payload(request, model)
        if family == "messages"
        else provider._responses_payload(request, model)
        if family == "responses"
        else provider._chat_payload(request, model)
    )
    suffix = {
        "messages": "messages",
        "responses": "responses",
        "chat_completions": "chat/completions",
    }[family]
    try:
        if not await provider.healthcheck():
            return ReasoningCheck(effort=effort, status="error", detail="请填写接口地址与密钥")
        async with asyncio.timeout(min(provider._config.timeout_seconds, 20)):
            async with provider._client.stream(
                "POST",
                provider._endpoint(suffix),
                json=payload,
                headers=provider._headers(),
                follow_redirects=False,
            ) as response:
                code = response.status_code
                if code in {400, 404, 422}:
                    return ReasoningCheck(
                        effort=effort, status="unsupported", detail=f"上游拒绝该配置（HTTP {code}）"
                    )
                if code >= 300:
                    return ReasoningCheck(
                        effort=effort, status="error", detail=f"上游请求失败（HTTP {code}），可重试"
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 2_097_152:
                        raise ValueError("response too large")
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError("invalid envelope")
            parser = (
                provider._parse
                if family == "messages"
                else provider._parse_responses
                if family == "responses"
                else provider._parse_chat
            )
            parsed = parser(data, response, request, model, 0)
            if (
                parsed.structured_output != {"ok": True}
                or parsed.tool_calls
                or parsed.finish_reason not in {"stop", "end_turn"}
            ):
                raise ValueError("incomplete probe")
        return ReasoningCheck(
            effort=effort,
            status="supported",
            accepted=True,
            source="default" if effort is None else "unknown",
            detail="默认调用通过" if effort is None else "参数校验与调用通过",
        )
    except (TimeoutError, httpx.TransportError):
        return ReasoningCheck(effort=effort, status="error", detail="连接失败或测试超时，可重试")
    except (ValueError, KeyError, TypeError, InvalidModelRequestError):
        return ReasoningCheck(
            effort=effort, status="unverified", detail="响应不完整或格式不符，无法确认兼容性"
        )
