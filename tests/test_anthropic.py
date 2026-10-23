import json

import httpx
import pytest
from pydantic import ValidationError

from work_harness.config.models import ProviderConfig
from work_harness.domain.models import ModelMessage, ModelRequest
from work_harness.llm.anthropic import AnthropicProvider
from work_harness.llm.base import (
    AuthenticationModelError,
    InvalidModelRequestError,
    TransientModelError,
)
from work_harness.llm.openai_compatible import OpenAICompatibleProvider
from work_harness.llm.registry import build_default_provider_registry


def config(**overrides):
    values = dict(
        adapter="anthropic",
        api_family="messages",
        credential_source="stored",
        base_url="https://claude.example",
        allowed_models=["claude-test"],
        capabilities=["chat", "structured_output", "tool_calling"],
    )
    values.update(overrides)
    result = ProviderConfig(**values)
    result.set_runtime_api_key("test-secret")
    return result


def request(**overrides):
    values = dict(
        run_id="r",
        trace_id="t",
        node_name="worker",
        agent_role="worker-01",
        logical_model="worker-fast",
        max_output_tokens=128,
        messages=[
            ModelMessage(role="system", content="Work carefully"),
            ModelMessage(role="user", content="Find evidence"),
        ],
    )
    values.update(overrides)
    return ModelRequest(**values)


@pytest.mark.parametrize("mode", [None, "json_object", "json_schema"])
async def test_claude_structured_output_and_cache_usage(mode):
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}

    def handler(req):
        assert req.url.path == "/v1/messages"
        assert req.headers["x-api-key"] == "test-secret"
        assert req.headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in req.headers
        body = json.loads(req.content)
        assert body["max_tokens"] == 128
        assert body["messages"] == [
            {"role": "user", "content": [{"type": "text", "text": "Find evidence"}]},
        ]
        if mode == "json_schema":
            assert body["output_config"]["format"]["schema"] == schema
        else:
            assert "output_config" not in body
            assert "JSON Schema" in body["system"]
        return httpx.Response(
            200,
            headers={"request-id": "claude-request"},
            json={
                "content": [{"type": "text", "text": '{"ok": true}'}],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 30,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AnthropicProvider("claude", config(structured_output_mode=mode), client=client)
        result = await provider.complete(request(response_schema=schema), model="claude-test")
    assert result.structured_output == {"ok": True}
    assert result.usage.input_tokens == 60
    assert result.usage.cached_tokens == 20
    assert result.usage.output_tokens == 5
    assert result.usage.usage_complete
    assert result.provider_request_id == "claude-request"


async def test_claude_tool_round_trip_and_cross_protocol_history():
    thinking = {"type": "thinking", "thinking": "Inspect evidence", "signature": "signed"}
    block = {"type": "tool_use", "id": "tool-1", "name": "read", "input": {"path": "note"}}
    tool = {"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}
    calls = []

    def handler(req):
        body = json.loads(req.content)
        calls.append(body)
        assert body["tools"][0]["input_schema"] == {"type": "object"}
        if len(calls) == 1:
            return httpx.Response(
                200, json={"content": [thinking, block], "stop_reason": "tool_use"}
            )
        assert body["messages"][-2]["content"] == [thinking, block]
        assert body["messages"][-1] == {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tool-1", "content": "Evidence"},
            ],
        }
        return httpx.Response(200, json={"content": [{"type": "text", "text": "Done"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AnthropicProvider("claude", config(), client=client)
        initial = request(tools=[tool])
        first = await provider.complete(initial, model="claude-test")
        assistant = ModelMessage(
            role="assistant", tool_calls=first.tool_calls, response_items=first.response_items
        )
        messages = initial.messages + [
            assistant,
            ModelMessage(
                role="tool",
                tool_call_id="tool-1",
                content="Evidence",
            ),
        ]
        final = await provider.complete(
            request(messages=messages, tools=[tool]), model="claude-test"
        )
        assert final.content == "Done"
        assert not final.usage.usage_complete
        # Combining consecutive assistant turns must not mutate persisted signed blocks.
        provider._payload(
            request(
                messages=initial.messages
                + [
                    assistant,
                    ModelMessage(role="assistant", content="Another note"),
                ]
            ),
            "claude-test",
        )
        assert assistant.response_items == [thinking, block]
        # A fallback to Responses must translate Claude blocks into portable calls.
        converted = OpenAICompatibleProvider._responses_input(messages)
        assert converted[-2]["type"] == "function_call"
        assert converted[-2]["call_id"] == "tool-1"
        assert converted[-1]["type"] == "function_call_output"
        # Portable OpenAI history must also work when switching to Claude.
        assistant.response_items = [{"type": "function_call", "call_id": "tool-1"}]
        assert provider._payload(request(messages=messages), "claude-test")["messages"][-2][
            "content"
        ] == [block]


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, AuthenticationModelError),
        (429, TransientModelError),
        (502, TransientModelError),
        (400, InvalidModelRequestError),
        (302, InvalidModelRequestError),
    ],
)
async def test_claude_errors_do_not_expose_upstream_body(status, error):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, json={"message": "test-secret"}),
        )
    ) as client:
        provider = AnthropicProvider("claude", config(auth_scheme="bearer"), client=client)
        with pytest.raises(error) as exc:
            await provider.complete(request(), model="claude-test")
        assert "test-secret" not in str(exc.value)


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"input_tokens": True, "output_tokens": 2},
        {"input_tokens": 2, "output_tokens": 1, "cache_read_input_tokens": -1},
    ],
)
def test_claude_unknown_usage_is_not_reported_as_complete(usage):
    assert not AnthropicProvider._usage(usage).usage_complete


async def test_registry_and_protocol_contract():
    provider = build_default_provider_registry().build("claude", config())
    assert isinstance(provider, AnthropicProvider)
    await provider.close()
    with pytest.raises(ValidationError, match="requires messages"):
        config(api_family="responses")
    with pytest.raises(ValidationError, match="requires anthropic"):
        config(adapter="openai_compatible")
