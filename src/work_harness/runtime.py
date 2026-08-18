from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from work_harness.config.loader import load_app_config
from work_harness.config.models import AppConfig
from work_harness.llm.base import ModelProvider
from work_harness.llm.compatibility import (
    ProviderCompatibilityReport,
    inspect_provider_compatibility,
)
from work_harness.llm.gateway import ModelGateway
from work_harness.llm.registry import (
    ProviderAdapterRegistry,
    build_default_provider_registry,
)
from work_harness.observability import TraceRecorder, configure_logging
from work_harness.prompts import PromptRegistry
from work_harness.skills import SkillRegistry
from work_harness.storage import RunStore, build_run_store
from work_harness.tools.adapters import (
    ToolAdapterRegistry,
    build_default_tool_adapter_registry,
)
from work_harness.tools.gateway import ToolGateway
from work_harness.tools.registry import ToolRegistry
from work_harness.workflow.graph import build_root_graph


@dataclass(slots=True)
class HarnessRuntime:
    config: AppConfig
    graph: Any
    model_gateway: ModelGateway
    tool_gateway: ToolGateway
    tool_registry: ToolRegistry
    run_store: RunStore
    skill_registry: SkillRegistry
    provider_compatibility: ProviderCompatibilityReport
    provider_adapters: ProviderAdapterRegistry
    tool_adapters: ToolAdapterRegistry


def _build_providers(
    config: AppConfig,
    adapters: ProviderAdapterRegistry,
) -> dict[str, ModelProvider]:
    providers: dict[str, ModelProvider] = {}
    for name, provider_config in config.models.providers.items():
        if not provider_config.enabled:
            continue
        providers[name] = adapters.build(name, provider_config)
    return providers


def _build_runtime(
    config: AppConfig,
    checkpointer: Any,
    run_store: RunStore,
    provider_adapters: ProviderAdapterRegistry,
    tool_adapters: ToolAdapterRegistry,
) -> HarnessRuntime:
    providers = _build_providers(config, provider_adapters)
    provider_compatibility = inspect_provider_compatibility(config.models, providers)
    if not provider_compatibility.compatible:
        issues = [
            f"{row.provider}: {', '.join(row.issues)}"
            for row in provider_compatibility.providers
            if not row.compatible
        ]
        raise ValueError(f"provider compatibility check failed: {'; '.join(issues)}")
    gateway = ModelGateway(config.models, providers, config.runtime)
    registry = ToolRegistry(config.tools)
    for tool_name in config.tools.tools:
        registry.register(tool_adapters.build(tool_name, config.project_root))
    registry.validate_complete()
    tool_gateway = ToolGateway(
        registry,
        global_max_result_chars=config.runtime.max_tool_result_chars,
    )
    prompts = PromptRegistry(config.project_root / "prompts")
    skill_registry = SkillRegistry(config.project_root / "skills")
    skill_registry.load()
    assignable_roles = [name for name, role in config.agents.agents.items() if role.assignable]
    skill_registry.validate_compatibility(
        config.models,
        registry,
        assignable_roles=assignable_roles,
    )
    trace = TraceRecorder(enabled=config.runtime.trace.enabled)
    graph = build_root_graph(
        config=config,
        gateway=gateway,
        prompts=prompts,
        trace=trace,
        tool_gateway=tool_gateway,
        skills=skill_registry,
        checkpointer=checkpointer,
    )
    return HarnessRuntime(
        config=config,
        graph=graph,
        model_gateway=gateway,
        tool_gateway=tool_gateway,
        tool_registry=registry,
        run_store=run_store,
        skill_registry=skill_registry,
        provider_compatibility=provider_compatibility,
        provider_adapters=provider_adapters,
        tool_adapters=tool_adapters,
    )


async def _close_providers(runtime: HarnessRuntime) -> None:
    await runtime.model_gateway.close()


@asynccontextmanager
async def build_runtime(
    project_root: Path | str | None = None,
    *,
    checkpoint_backend: str | None = None,
    run_store_backend: str | None = None,
    provider_adapters: ProviderAdapterRegistry | None = None,
    tool_adapters: ToolAdapterRegistry | None = None,
) -> AsyncIterator[HarnessRuntime]:
    config = load_app_config(project_root)
    configure_logging(config.runtime.trace.log_level)
    backend = checkpoint_backend or config.runtime.checkpoint.backend
    selected_run_store = run_store_backend
    if selected_run_store is None and checkpoint_backend == "memory":
        selected_run_store = "memory"
    run_store = build_run_store(
        config.runtime.run_store,
        config.project_root,
        backend=selected_run_store,
    )
    await run_store.initialize()
    adapters = provider_adapters or build_default_provider_registry()
    selected_tool_adapters = tool_adapters or build_default_tool_adapter_registry()
    try:
        if backend == "memory":
            runtime = _build_runtime(
                config,
                InMemorySaver(),
                run_store,
                adapters,
                selected_tool_adapters,
            )
            try:
                yield runtime
            finally:
                await _close_providers(runtime)
            return
        if backend != "sqlite":
            raise ValueError(f"unsupported checkpoint backend: {backend}")

        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        checkpoint_path = Path(config.runtime.checkpoint.path)
        if not checkpoint_path.is_absolute():
            checkpoint_path = config.project_root / checkpoint_path
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
            runtime = _build_runtime(
                config,
                saver,
                run_store,
                adapters,
                selected_tool_adapters,
            )
            try:
                yield runtime
            finally:
                await _close_providers(runtime)
    finally:
        await run_store.close()
