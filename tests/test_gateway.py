from pathlib import Path

import pytest

from work_harness.config.loader import load_app_config
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
