from pathlib import Path

import pytest

from work_harness.config.loader import load_app_config
from work_harness.tools.base import ToolPermissionError
from work_harness.tools.builtin import EchoReadTool, WorkspaceReadTool
from work_harness.tools.gateway import ToolGateway
from work_harness.tools.registry import ToolRegistry

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def gateway() -> ToolGateway:
    config = load_app_config(PROJECT_ROOT)
    registry = ToolRegistry(config.tools)
    registry.register(EchoReadTool())
    registry.register(WorkspaceReadTool(PROJECT_ROOT))
    registry.validate_complete()
    return ToolGateway(registry)


@pytest.mark.asyncio
async def test_tool_validates_role_and_payload(gateway: ToolGateway) -> None:
    result = await gateway.execute(
        run_id="run_test",
        role="worker-01",
        tool_name="echo_read",
        arguments={"text": "hello"},
    )
    assert result.output == {"text": "hello"}


@pytest.mark.asyncio
async def test_tool_rejects_unauthorized_role(gateway: ToolGateway) -> None:
    with pytest.raises(ToolPermissionError):
        await gateway.execute(
            run_id="run_test",
            role="planner",
            tool_name="echo_read",
            arguments={"text": "hello"},
        )


@pytest.mark.asyncio
async def test_workspace_tool_reads_searches_and_blocks_escape(gateway: ToolGateway) -> None:
    read = await gateway.execute(
        run_id="run_test",
        role="worker-01",
        tool_name="workspace_read",
        arguments={"operation": "read", "path": "README.md", "max_lines": 3},
    )
    assert "Work Harness" in str(read.output["content"])

    search = await gateway.execute(
        run_id="run_test",
        role="worker-01",
        tool_name="workspace_read",
        arguments={
            "operation": "search",
            "path": "src",
            "query": "class RunManager",
        },
    )
    assert any(item["path"].endswith("service.py") for item in search.output["matches"])

    with pytest.raises(ToolPermissionError):
        await gateway.execute(
            run_id="run_test",
            role="worker-01",
            tool_name="workspace_read",
            arguments={"operation": "read", "path": "../outside.txt"},
        )


def test_tool_definitions_are_limited_by_role(gateway: ToolGateway) -> None:
    definitions = gateway.definitions(role="worker-01", names=["workspace_read"])
    function = definitions[0]["function"]
    assert isinstance(function, dict)
    assert function["name"] == "workspace_read"
    with pytest.raises(ValueError):
        gateway.definitions(role="planner", names=["workspace_read"])


@pytest.mark.asyncio
async def test_idempotent_tool_results_are_cached_per_run() -> None:
    config = load_app_config(PROJECT_ROOT)
    registry = ToolRegistry(config.tools)
    registry.register(EchoReadTool())
    registry.register(WorkspaceReadTool(PROJECT_ROOT))
    registry.validate_complete()
    cached_gateway = ToolGateway(registry, cache_max_entries=4)

    first = await cached_gateway.execute(
        run_id="run_cached",
        role="worker-01",
        tool_name="echo_read",
        arguments={"text": "stable"},
    )
    repeated = await cached_gateway.execute(
        run_id="run_cached",
        role="worker-01",
        tool_name="echo_read",
        arguments={"text": "stable"},
    )
    other_run = await cached_gateway.execute(
        run_id="run_other",
        role="worker-01",
        tool_name="echo_read",
        arguments={"text": "stable"},
    )

    assert first.cache_hit is False
    assert repeated.cache_hit is True
    assert repeated.output == first.output
    assert other_run.cache_hit is False

    await cached_gateway.clear_run("run_cached")
    after_clear = await cached_gateway.execute(
        run_id="run_cached",
        role="worker-01",
        tool_name="echo_read",
        arguments={"text": "stable"},
    )
    assert after_clear.cache_hit is False
