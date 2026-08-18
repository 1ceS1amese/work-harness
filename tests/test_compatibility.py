from pathlib import Path

import pytest
from pydantic import ValidationError

from work_harness.config.loader import load_app_config
from work_harness.config.models import AppConfig
from work_harness.llm.compatibility import inspect_provider_compatibility
from work_harness.llm.mock import MockProvider
from work_harness.llm.registry import ProviderAdapterRegistry
from work_harness.runtime import build_runtime
from work_harness.tools.adapters import ToolAdapterRegistry
from work_harness.tools.builtin import EchoReadTool

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_runtime_exposes_equal_provider_baseline() -> None:
    async with build_runtime(PROJECT_ROOT, checkpoint_backend="memory") as runtime:
        report = runtime.provider_compatibility

    assert report.compatible is True
    assert {item.provider for item in report.providers} == {"mock", "mock-secondary"}
    assert all(
        set(report.baseline_capabilities) <= set(item.capabilities)
        for item in report.providers
    )


def test_compatibility_report_detects_untruthful_runtime_capabilities() -> None:
    config = load_app_config(PROJECT_ROOT)
    provider = MockProvider("mock")
    provider.capabilities = {"chat"}

    report = inspect_provider_compatibility(config.models, {"mock": provider})

    mock = next(item for item in report.providers if item.provider == "mock")
    assert report.compatible is False
    assert mock.compatible is False
    assert any("baseline" in issue for issue in mock.issues)


def test_configuration_rejects_provider_capability_drift() -> None:
    payload = load_app_config(PROJECT_ROOT).model_dump()
    payload["models"]["providers"]["mock-secondary"]["capabilities"].add("vision")

    with pytest.raises(ValidationError, match="identical capabilities"):
        AppConfig.model_validate(payload)


def test_provider_and_tool_adapters_are_modular() -> None:
    config = load_app_config(PROJECT_ROOT)
    providers = ProviderAdapterRegistry()
    providers.register("custom", lambda name, _: MockProvider(name))
    custom_config = config.models.providers["mock"].model_copy(update={"adapter": "custom"})
    assert providers.build("custom-provider", custom_config).name == "custom-provider"

    tools = ToolAdapterRegistry()
    tools.register("echo_read", lambda _: EchoReadTool())
    assert tools.build("echo_read", PROJECT_ROOT).name == "echo_read"
