import asyncio
from pathlib import Path

import pytest

from work_harness.config.loader import load_app_config
from work_harness.config.models import ModelCandidateConfig
from work_harness.domain.models import ModelMessage, ModelRequest, Plan
from work_harness.llm.gateway import ModelGateway
from work_harness.llm.mock import MockProvider

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_logical_model_routes_to_mock_provider() -> None:
    config = load_app_config(PROJECT_ROOT)
    gateway = ModelGateway(config.models, {"mock": MockProvider()}, config.runtime)
    request = ModelRequest(
        run_id="run_test",
        trace_id="trace_test",
        node_name="plan",
        agent_role="planner",
        logical_model="planner-strong",
        messages=[ModelMessage(role="user", content="plan")],
        required_capabilities={"chat", "structured_output"},
        response_schema=Plan.model_json_schema(),
        metadata={
            "operation": "plan",
            "payload": {
                "task": {
                    "goal": "test",
                }
            },
        },
    )
    response = await gateway.complete(request)
    plan = Plan.model_validate(response.structured_output)
    assert response.provider == "mock"
    assert len(plan.subtasks) == 2


@pytest.mark.asyncio
async def test_request_can_pin_compatible_provider_candidate() -> None:
    config = load_app_config(PROJECT_ROOT)
    gateway = ModelGateway(
        config.models,
        {
            "mock": MockProvider("mock"),
            "mock-secondary": MockProvider("mock-secondary"),
        },
        config.runtime,
    )
    request = ModelRequest(
        run_id="run_provider_selection",
        trace_id="trace_provider_selection",
        node_name="worker",
        agent_role="worker-01",
        logical_model="worker-fast",
        messages=[ModelMessage(role="user", content="plan")],
        required_capabilities={"chat", "structured_output", "tool_calling"},
        provider_preferences=["mock-secondary"],
        provider_allowlist={"mock-secondary"},
        response_schema=Plan.model_json_schema(),
        metadata={"operation": "plan", "payload": {"task": {"goal": "test"}}},
    )

    response = await gateway.complete(request)

    assert response.provider == "mock-secondary"


@pytest.mark.asyncio
async def test_agent_route_prefers_provider_for_one_subagent() -> None:
    config = load_app_config(PROJECT_ROOT)
    models = config.models.model_copy(
        update={
            "agent_routes": {
                "worker-02": ModelCandidateConfig(
                    provider="mock-secondary", model="worker-02-model"
                )
            }
        }
    )
    gateway = ModelGateway(
        models,
        {
            "mock": MockProvider("mock"),
            "mock-secondary": MockProvider("mock-secondary"),
        },
        config.runtime,
    )
    request = ModelRequest(
        run_id="run_agent_route",
        trace_id="trace_agent_route",
        node_name="worker",
        agent_role="worker-02",
        logical_model="worker-fast",
        messages=[ModelMessage(role="user", content="plan")],
        required_capabilities={"chat", "structured_output"},
        response_schema=Plan.model_json_schema(),
        metadata={"operation": "plan", "payload": {"task": {"goal": "test"}}},
    )

    response = await gateway.complete(request)

    assert response.provider == "mock-secondary"
    assert response.model == "worker-02-model"

    explicitly_pinned = request.model_copy(
        update={
            "provider_allowlist": {"mock"},
            "provider_preferences": ["mock"],
        }
    )
    pinned_response = await gateway.complete(explicitly_pinned)

    assert pinned_response.provider == "mock"


@pytest.mark.asyncio
async def test_provider_concurrency_is_bounded() -> None:
    class SlowMockProvider(MockProvider):
        def __init__(self) -> None:
            super().__init__()
            self.active = 0
            self.max_active = 0

        async def complete(self, request, *, model):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.02)
                return await super().complete(request, model=model)
            finally:
                self.active -= 1

    config = load_app_config(PROJECT_ROOT)
    mock_config = config.models.providers["mock"].model_copy(
        update={"max_concurrency": 1}
    )
    models = config.models.model_copy(
        update={
            "providers": {
                **config.models.providers,
                "mock": mock_config,
            }
        }
    )
    provider = SlowMockProvider()
    gateway = ModelGateway(models, {"mock": provider}, config.runtime)
    request = ModelRequest(
        run_id="run_bounded",
        trace_id="trace_bounded",
        node_name="plan",
        agent_role="planner",
        logical_model="planner-strong",
        messages=[ModelMessage(role="user", content="plan")],
        required_capabilities={"chat", "structured_output"},
        response_schema=Plan.model_json_schema(),
        metadata={"operation": "plan", "payload": {"task": {"goal": "test"}}},
    )

    await asyncio.gather(*(gateway.complete(request) for _ in range(3)))

    assert provider.max_active == 1
