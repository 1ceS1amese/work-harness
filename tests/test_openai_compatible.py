import json

import httpx
import pytest

from work_harness.config.models import ProviderConfig
from work_harness.domain.models import ModelMessage, ModelRequest
from work_harness.llm.base import (
    AuthenticationModelError,
    InvalidModelRequestError,
    TransientModelError,
)
from work_harness.llm.openai_compatible import OpenAICompatibleProvider


def provider_config(**overrides) -> ProviderConfig:
    values = {
        "adapter": "openai_compatible",
        "enabled": True,
        "base_url_env": "TEST_MODEL_BASE_URL",
        "api_key_env": "TEST_MODEL_API_KEY",
        "allowed_models": ["test-model"],
        "capabilities": ["chat", "structured_output", "tool_calling"],
        "timeout_seconds": 5,
    }
    values.update(overrides)
    return ProviderConfig.model_validate(values)


def model_request() -> ModelRequest:
    return ModelRequest(
        run_id="run_test",
        trace_id="trace_test",
        node_name="plan",
        agent_role="planner",
        logical_model="planner-strong",
        messages=[ModelMessage(role="user", content="return JSON")],
        response_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
    )


@pytest.mark.asyncio
async def test_provider_sends_schema_and_parses_usage(monkeypatch) -> None:
    monkeypatch.setenv("TEST_MODEL_BASE_URL", "https://models.example.test/v1")
    monkeypatch.setenv("TEST_MODEL_API_KEY", "secret-for-test")

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret-for-test"
        body = json.loads(request.content)
        assert body["model"] == "test-model"
        assert body["response_format"]["type"] == "json_schema"
        assert body["messages"] == [{"role": "user", "content": "return JSON"}]
        return httpx.Response(
            200,
            headers={"x-request-id": "request-1"},
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": '{"ok":true}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 3,
                    "prompt_tokens_details": {"cached_tokens": 2},
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        "compatible", provider_config(), client=client
    )
    response = await provider.complete(model_request(), model="test-model")

    assert response.structured_output == {"ok": True}
    assert response.usage.input_tokens == 12
    assert response.usage.cached_tokens == 2
    assert response.provider_request_id == "request-1"
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (401, AuthenticationModelError),
        (408, TransientModelError),
        (429, TransientModelError),
        (503, TransientModelError),
        (400, InvalidModelRequestError),
    ],
)
async def test_provider_maps_http_failures(monkeypatch, status_code, error_type) -> None:
    monkeypatch.setenv("TEST_MODEL_BASE_URL", "https://models.example.test/v1")
    monkeypatch.setenv("TEST_MODEL_API_KEY", "secret-for-test")
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status_code))
    )
    provider = OpenAICompatibleProvider(
        "compatible", provider_config(), client=client
    )
    with pytest.raises(error_type):
        await provider.complete(model_request(), model="test-model")
    await client.aclose()


def test_provider_blocks_private_endpoint_by_default(monkeypatch) -> None:
    monkeypatch.setenv("TEST_MODEL_BASE_URL", "https://127.0.0.1/v1")
    monkeypatch.setenv("TEST_MODEL_API_KEY", "secret-for-test")
    with pytest.raises(ValueError, match="private model endpoint"):
        OpenAICompatibleProvider("compatible", provider_config())
