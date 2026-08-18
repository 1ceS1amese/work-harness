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
        assert request.url.path == "/v1/chat/completions"
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
async def test_deepseek_chat_uses_json_object_and_max_tokens(monkeypatch) -> None:
    monkeypatch.setenv("TEST_MODEL_BASE_URL", "https://api.deepseek.test/v1")
    monkeypatch.setenv("TEST_MODEL_API_KEY", "secret-for-test")

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert body["max_tokens"] == 4_000
        assert "max_completion_tokens" not in body
        assert body["response_format"] == {"type": "json_object"}
        assert "json" in body["messages"][0]["content"]
        assert "JSON Schema" in body["messages"][0]["content"]
        assert body["messages"][1] == {"role": "user", "content": "return JSON"}
        return httpx.Response(
            200,
            headers={"x-ds-request-id": "deepseek-request-1"},
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": '{"ok":true}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 3},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        "deepseek",
        provider_config(protocol_variant="deepseek"),
        client=client,
    )
    response = await provider.complete(model_request(), model="test-model")

    assert response.structured_output == {"ok": True}
    assert response.provider_request_id == "deepseek-request-1"
    await client.aclose()


@pytest.mark.asyncio
async def test_deepseek_chat_replays_reasoning_content_for_tool_turn(
    monkeypatch,
) -> None:
    monkeypatch.setenv("TEST_MODEL_BASE_URL", "https://custom.deepseek.test/v1")
    monkeypatch.setenv("TEST_MODEL_API_KEY", "secret-for-test")
    request_model = model_request().model_copy(
        update={
            "response_schema": None,
            "messages": [
                ModelMessage(role="user", content="inspect"),
                ModelMessage(
                    role="assistant",
                    content="checking",
                    reasoning_content="need the file",
                    tool_calls=[
                        {
                            "id": "call_reasoning",
                            "type": "function",
                            "function": {"name": "workspace_read", "arguments": "{}"},
                        }
                    ],
                ),
                ModelMessage(
                    role="tool",
                    tool_call_id="call_reasoning",
                    content='{"text":"ok"}',
                ),
            ],
        }
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["messages"][1]["reasoning_content"] == "need the file"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "done",
                            "reasoning_content": "file checked",
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        "custom-deepseek",
        provider_config(protocol_variant="deepseek"),
        client=client,
    )
    response = await provider.complete(request_model, model="test-model")

    assert response.reasoning_content == "file checked"
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol_variant", ["openai", "deepseek"])
async def test_responses_api_converts_messages_tools_and_schema(
    monkeypatch, protocol_variant: str
) -> None:
    monkeypatch.setenv(
        "TEST_MODEL_BASE_URL", f"https://api.{protocol_variant}.test/v1"
    )
    monkeypatch.setenv("TEST_MODEL_API_KEY", "secret-for-test")
    request_model = model_request().model_copy(
        update={
            "messages": [
                ModelMessage(role="user", content="inspect"),
                ModelMessage(
                    role="assistant",
                    tool_calls=[
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "workspace_read",
                                "arguments": '{"path":"README.md"}',
                            },
                        }
                    ],
                ),
                ModelMessage(role="tool", tool_call_id="call_1", content='{"text":"ok"}'),
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "workspace_read",
                        "description": "Read one file",
                        "parameters": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                        },
                    },
                }
            ],
        }
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/responses"
        body = json.loads(request.content)
        assert body["store"] is False
        assert body["input"] == [
            {"role": "user", "content": "inspect"},
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "workspace_read",
                "arguments": '{"path":"README.md"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": '{"text":"ok"}',
            },
        ]
        assert body["tools"][0]["name"] == "workspace_read"
        assert "function" not in body["tools"][0]
        assert body["text"]["format"]["type"] == "json_schema"
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": '{"ok":true}'}],
                    }
                ],
                "usage": {
                    "input_tokens": 14,
                    "output_tokens": 4,
                    "input_tokens_details": {"cached_tokens": 5},
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        protocol_variant,
        provider_config(protocol_variant=protocol_variant, api_family="responses"),
        client=client,
    )
    response = await provider.complete(request_model, model="test-model")

    assert response.structured_output == {"ok": True}
    assert response.usage.cached_tokens == 5
    assert response.provider_request_id == "resp_1"
    assert response.response_items[0]["type"] == "message"
    await client.aclose()


@pytest.mark.asyncio
async def test_responses_api_replays_typed_output_items(monkeypatch) -> None:
    monkeypatch.setenv("TEST_MODEL_BASE_URL", "https://custom.responses.test/v1")
    monkeypatch.setenv("TEST_MODEL_API_KEY", "secret-for-test")
    previous_items = [
        {"type": "reasoning", "id": "rs_1", "summary": []},
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "workspace_read",
            "arguments": '{"path":"README.md"}',
        },
    ]
    request_model = model_request().model_copy(
        update={
            "response_schema": None,
            "messages": [
                ModelMessage(role="user", content="inspect"),
                ModelMessage(
                    role="assistant",
                    tool_calls=[
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "workspace_read",
                                "arguments": '{"path":"README.md"}',
                            },
                        }
                    ],
                    response_items=previous_items,
                ),
                ModelMessage(role="tool", tool_call_id="call_1", content="ok"),
            ],
        }
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["input"] == [
            {"role": "user", "content": "inspect"},
            *previous_items,
            {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
        ]
        return httpx.Response(
            200,
            json={
                "id": "resp_2",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "done"}],
                    }
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        "custom-responses",
        provider_config(api_family="responses"),
        client=client,
    )
    response = await provider.complete(request_model, model="test-model")

    assert response.content == "done"
    await client.aclose()


@pytest.mark.asyncio
async def test_responses_api_normalizes_function_calls(monkeypatch) -> None:
    monkeypatch.setenv("TEST_MODEL_BASE_URL", "https://api.openai.test/v1")
    monkeypatch.setenv("TEST_MODEL_API_KEY", "secret-for-test")
    request_model = model_request().model_copy(update={"response_schema": None})
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "id": "resp_tool",
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call_2",
                            "name": "workspace_read",
                            "arguments": '{"path":"README.md"}',
                        }
                    ],
                },
            )
        )
    )
    provider = OpenAICompatibleProvider(
        "openai",
        provider_config(protocol_variant="openai", api_family="responses"),
        client=client,
    )
    response = await provider.complete(request_model, model="test-model")

    assert response.finish_reason == "tool_calls"
    assert response.tool_calls == [
        {
            "id": "call_2",
            "type": "function",
            "function": {
                "name": "workspace_read",
                "arguments": '{"path":"README.md"}',
            },
        }
    ]
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
