import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from work_harness.api.app import create_app
from work_harness.config.loader import load_app_config
from work_harness.config.models import ProviderConfig
from work_harness.config.reasoning import ModelSettings, ModelVerification, ReasoningCheck
from work_harness.domain.models import ModelMessage, ModelRequest, ModelResponse, ModelSelection
from work_harness.llm.anthropic import AnthropicProvider
from work_harness.llm.base import ModelGatewayError
from work_harness.llm.gateway import ModelGateway
from work_harness.llm.openai_compatible import OpenAICompatibleProvider
from work_harness.llm.verification import VerificationCache, connection_fingerprint, verify_model

ROOT = Path(__file__).resolve().parents[1]


def connection(family="responses", **updates):
    config = ProviderConfig(
        adapter="anthropic" if family == "messages" else "openai_compatible",
        api_family=family,
        base_url="https://models.example",
        credential_source="stored",
        allowed_models=["model-a", "model-b"],
        capabilities={"chat", "structured_output", "tool_calling"},
        **updates,
    )
    config.set_runtime_api_key("test-key")
    return config


def report(config, model="model-a", efforts=("high", "xhigh", "max")):
    return ModelVerification(
        id="test-report-" + model,
        fingerprint=connection_fingerprint(config, model),
        checked_at=datetime.now(UTC),
        model_available=True,
        checks=[ReasoningCheck(effort=None, status="supported", detail="ok")]
        + [ReasoningCheck(effort=effort, status="supported", detail="ok") for effort in efforts],
    )


def model_request(**updates):
    return ModelRequest(
        run_id="r",
        trace_id="t",
        node_name="test",
        agent_role="worker-01",
        logical_model="worker-fast",
        messages=[ModelMessage(role="user", content="test")],
        **updates,
    )


def envelope(family):
    if family == "messages":
        return {"content": [{"type": "text", "text": '{"ok":true}'}], "stop_reason": "end_turn"}
    if family == "responses":
        return {
            "status": "completed",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": '{"ok":true}'}]}
            ],
        }
    return {"choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}]}


def effort_from(body, family):
    if family == "messages":
        return body.get("output_config", {}).get("effort")
    if family == "responses":
        return body.get("reasoning", {}).get("effort")
    return body.get("reasoning_effort")


@pytest.mark.parametrize("family", ["responses", "chat_completions", "messages"])
@pytest.mark.parametrize(
    "effort", [None, "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
)
async def test_protocol_preserves_exact_reasoning_field(family, effort):
    config = connection(family, structured_output_mode="json_schema")

    def handle(req):
        body = json.loads(req.content)
        assert effort_from(body, family) == effort
        if family == "messages":
            assert body["output_config"]["format"]["type"] == "json_schema"
            if effort is not None:
                assert body["thinking"] == {"type": "adaptive"}
        return httpx.Response(200, json=envelope(family))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = (AnthropicProvider if family == "messages" else OpenAICompatibleProvider)(
            "test", config, client=client
        )
        await provider.complete(
            model_request(reasoning_effort=effort, response_schema={"type": "object"}),
            model="model-a",
        )


@pytest.mark.parametrize("family", ["responses", "chat_completions", "messages"])
async def test_probe_filters_unsupported_and_inconclusive_efforts(family):
    def handle(req):
        body = json.loads(req.content)
        assert body["tools"]
        effort = effort_from(body, family)
        if effort in {"invalid_probe_effort", "max"}:
            return httpx.Response(400)
        if effort == "high":
            return httpx.Response(429)
        return httpx.Response(200, json=envelope(family))

    config = connection(family)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await verify_model(
            config, "model-a", ["low", "high", "xhigh", "max", "ultra"], client=client
        )
    assert result.model_available
    assert result.supported_efforts == ["low", "xhigh", "ultra"]
    assert {item.effort: item.status for item in result.checks}["high"] == "error"
    with pytest.raises(ValidationError):
        ModelSettings(verification=result, default_effort="max")


@pytest.mark.parametrize("failure", ["ignored", "auth", "timeout", "malformed", "truncated"])
async def test_probe_never_treats_unknown_as_supported(failure):
    def handle(req):
        if failure == "auth":
            return httpx.Response(401)
        if failure == "timeout":
            raise httpx.ReadTimeout("secret")
        if failure == "malformed":
            return httpx.Response(200, json={})
        data = envelope("responses")
        if failure == "truncated":
            data.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        return httpx.Response(200, json=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await verify_model(connection(), "model-a", ["low", "max"], client=client)
    assert result.supported_efforts == []
    assert result.model_available == (failure == "ignored")
    assert "secret" not in result.model_dump_json()


def test_verification_evidence_cannot_be_forged_or_reused_after_connection_change():
    config = connection()
    proof = report(config)
    config.model_settings["model-a"] = ModelSettings(verification=proof, default_effort="high")
    cache = VerificationCache()
    with pytest.raises(ValueError, match="验证记录"):
        cache.validate(config, None)
    cache.add(proof)
    cache.validate(config, None)
    # Persisted evidence remains valid after a server restart; only defaults may change.
    previous = config.model_copy(deep=True)
    VerificationCache().validate(config, previous)
    config.model_settings["model-a"].default_effort = "max"
    VerificationCache().validate(config, previous)
    config.set_runtime_api_key("replacement-key")
    with pytest.raises(ValueError, match="连接信息"):
        cache.validate(config, previous)


async def test_explicit_model_route_is_isolated_and_respects_skill_allowlist():
    app_config = load_app_config(ROOT)
    config = connection()
    for model in config.allowed_models:
        config.model_settings[model] = ModelSettings(
            verification=report(config, model), default_effort="high"
        )
    app_config.models.providers["test-provider"] = config
    seen = []

    class Provider:
        capabilities = frozenset({"chat", "structured_output", "tool_calling"})

        async def complete(self, request, *, model):
            await asyncio.sleep(0)
            seen.append((model, request.reasoning_effort))
            return ModelResponse(provider="test-provider", model=model)

    gateway = ModelGateway(app_config.models, {"test-provider": Provider()}, app_config.runtime)

    async def call(model, effort):
        return await gateway.complete(
            model_request(
                model_selection=ModelSelection(
                    provider="test-provider", model=model, reasoning_effort=effort
                )
            )
        )

    await asyncio.gather(call("model-a", "xhigh"), call("model-b", "max"))
    assert sorted(seen) == [("model-a", "xhigh"), ("model-b", "max")]
    assert (
        gateway.resolve_selection(
            ModelSelection(provider="test-provider", model="model-a")
        ).reasoning_effort
        == "high"
    )
    with pytest.raises(ModelGatewayError, match="限制冲突"):
        await gateway.complete(
            model_request(
                model_selection=ModelSelection(provider="test-provider", model="model-a"),
                provider_allowlist={"mock"},
            )
        )
    with pytest.raises(ModelGatewayError, match="推理档位"):
        await call("model-a", "low")
    assert len(gateway.model_choices()) == 2
    config.base_url = "https://another.example"
    assert gateway.model_choices() == []


@pytest.mark.usefixtures("legacy_engine")
async def test_verification_api_reuses_key_and_rejects_forged_or_stale_reports(
    tmp_path, monkeypatch
):
    for kind in ("CHECKPOINT", "RUN_STORE", "PROVIDER_STORE"):
        monkeypatch.setenv(f"WORK_HARNESS_{kind}_PATH", str(tmp_path / f"{kind}.sqlite"))
    seen = []

    async def verify(config, model, efforts):
        seen.append(config.runtime_api_key())
        return report(config, model, efforts)

    monkeypatch.setattr("work_harness.api.app.verify_model", verify)
    app = create_app(ROOT)
    config = connection().model_dump(mode="json")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (
            await client.put(
                "/v1/providers/test-provider", json={"config": config, "api_key": "stored-key"}
            )
        ).status_code == 200
        result = await client.post(
            "/v1/providers/models/verify",
            json={
                "config": config,
                "provider_name": "test-provider",
                "model": "model-a",
                "efforts": ["high", "max"],
            },
        )
        assert result.status_code == 200 and seen == ["stored-key"]
        assert "stored-key" not in result.text
        config["model_settings"] = {
            "model-a": {"verification": result.json(), "default_effort": "max"}
        }
        assert (
            await client.put("/v1/providers/test-provider", json={"config": config})
        ).status_code == 200
        config["model_settings"]["model-a"]["verification"]["checks"][0]["detail"] = "forged"
        assert (
            await client.put("/v1/providers/test-provider", json={"config": config})
        ).status_code == 409
        config["model_settings"]["model-a"]["verification"] = result.json()
        assert (
            await client.put(
                "/v1/providers/test-provider", json={"config": config, "api_key": "changed"}
            )
        ).status_code == 409
        # Saved profiles are not selectable until the loaded runtime has been restarted.
        assert (await client.get("/v1/models")).json() == {"items": []}


@pytest.mark.parametrize("engine", ["legacy", "blackboard"])
async def test_selected_model_and_effort_reach_all_agents_and_saved_task(engine, monkeypatch):
    from work_harness.domain.models import TaskRequest
    from work_harness.knowledge_sources.catalog import draft_task_input
    from work_harness.llm.mock import MockProvider
    from work_harness.runtime import build_runtime
    from work_harness.service import RunManager

    seen = []
    original = MockProvider.complete

    async def complete(self, request, *, model):
        seen.append((self.name, model, request.reasoning_effort))
        return await original(self, request, model=model)

    monkeypatch.setattr(MockProvider, "complete", complete)
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        config = runtime.config.models.providers["mock"]
        config.allowed_models.append("pinned-model")
        config.model_settings["pinned-model"] = ModelSettings(
            verification=report(config, "pinned-model"), default_effort="max"
        )
        manager = RunManager(runtime)
        try:
            task = TaskRequest(
                goal="Create a launch brief",
                engine=engine,
                blackboard=draft_task_input() if engine == "blackboard" else None,
                model_selection=ModelSelection(provider="mock", model="pinned-model"),
                idempotency_key="pinned-task",
            )
            run_id = await manager.start(task)
            result = await manager.wait(run_id)
            assert result.status == "succeeded"
            assert seen and all(item == ("mock", "pinned-model", "max") for item in seen)
            saved = await runtime.run_store.get_initial_input(run_id)
            assert saved["model_selection"]["reasoning_effort"] == "max"
            assert result.task.model_selection.model == "pinned-model"
            config.model_settings["pinned-model"].default_effort = "high"
            assert await manager.start(task) == run_id
        finally:
            await manager.close()


async def test_channel_confirmation_only_enables_successful_requested_efforts():
    def handle(req):
        effort = effort_from(json.loads(req.content), "responses")
        if effort == "max":
            return httpx.Response(503)
        return httpx.Response(200, json=envelope("responses"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await verify_model(
            connection(),
            "model-a",
            ["low", "high", "max"],
            client=client,
            confirmed_efforts=["high", "max"],
        )
    assert result.supported_efforts == ["high"]
    assert result.checks[2].source == "channel_confirmation"
    assert result.checks[1].status == "unverified"
    assert result.checks[3].status == "error"


async def test_retesting_does_not_invalidate_execution_identity():
    from work_harness.runtime import build_runtime
    from work_harness.service import RunManager

    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        manager = RunManager(runtime)
        config = runtime.config.models.providers["mock"]
        config.allowed_models.append("verified-model")
        original = manager.execution_fingerprint
        model = "verified-model"
        config.model_settings[model] = ModelSettings(verification=report(config, model))
        assert manager.execution_fingerprint == original
        config.model_settings[model].verification.id = "fresh-evidence"
        assert manager.execution_fingerprint == original
        config.model_settings[model].default_effort = "high"
        assert manager.execution_fingerprint != original
        await manager.close()
