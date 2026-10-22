from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from work_harness.blackboard.store import BlackboardStore
from work_harness.blackboard.verification import MemoryVerificationReceiptStore
from work_harness.config.loader import load_app_config
from work_harness.config.models import AppConfig
from work_harness.domain.models import TaskRequest
from work_harness.llm.base import ModelProvider
from work_harness.llm.compatibility import (
    ProviderCompatibilityReport,
    inspect_provider_compatibility,
)
from work_harness.llm.gateway import ModelGateway
from work_harness.llm.profiles import apply_provider_profiles
from work_harness.llm.registry import (
    ProviderAdapterRegistry,
    build_default_provider_registry,
)
from work_harness.observability import TraceRecorder, configure_logging
from work_harness.prompts import PromptRegistry
from work_harness.skills import SkillRegistry
from work_harness.storage import (
    ApplicationSettings,
    ApplicationSettingsStore,
    ProviderProfileStore,
    RunStore,
    apply_application_settings,
    build_provider_profile_store,
    build_run_store,
    default_settings_path,
)
from work_harness.storage.blackboard_checkpoint_postgres import LeasedPostgresSaver
from work_harness.storage.blackboard_factory import build_verification_receipt_store
from work_harness.storage.blackboard_lock import BlackboardProcessLock
from work_harness.storage.blackboard_memory import MemoryBlackboardStore
from work_harness.storage.blackboard_postgres import PostgresBlackboardStore
from work_harness.storage.blackboard_sqlite import SQLiteBlackboardStore
from work_harness.storage.queue import RunJobLease
from work_harness.tools.adapters import (
    ToolAdapterRegistry,
    build_default_tool_adapter_registry,
)
from work_harness.tools.gateway import ToolGateway
from work_harness.tools.registry import ToolRegistry
from work_harness.workflow.agent_services import AgentServices
from work_harness.workflow.blackboard_engine import BlackboardEngine
from work_harness.workflow.graph import build_root_graph


@dataclass(slots=True)
class HarnessRuntime:
    config: AppConfig
    graph: Any
    model_gateway: ModelGateway
    tool_gateway: ToolGateway
    tool_registry: ToolRegistry
    run_store: RunStore
    provider_store: ProviderProfileStore
    settings_store: ApplicationSettingsStore
    settings_snapshot: ApplicationSettings
    prompt_registry: PromptRegistry
    skill_registry: SkillRegistry
    provider_compatibility: ProviderCompatibilityReport
    provider_adapters: ProviderAdapterRegistry
    tool_adapters: ToolAdapterRegistry
    checkpointer: Any
    provider_target_fingerprints: dict[str, str]
    _blackboard_store: BlackboardStore | None = None
    _blackboard_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _blackboard_receipts: MemoryVerificationReceiptStore = field(
        default_factory=MemoryVerificationReceiptStore
    )

    async def blackboard_store(self) -> BlackboardStore:
        async with self._blackboard_lock:
            if self._blackboard_store is None:
                if self.run_store.backend == "postgres":
                    config = self.config.runtime.run_store
                    store = PostgresBlackboardStore(
                        os.environ[config.dsn_env].strip(),
                        pool_min_size=config.pool_min_size,
                        pool_max_size=config.pool_max_size,
                    )
                elif self.run_store.backend == "memory":
                    store = MemoryBlackboardStore()
                else:
                    store = SQLiteBlackboardStore(
                        self.config.project_root / self.config.runtime.workflow.blackboard.path
                    )
                try:
                    await store.initialize()
                except BaseException:
                    await store.close()
                    raise
                self._blackboard_store = store
            return self._blackboard_store

    async def existing_blackboard_store(self) -> BlackboardStore | None:
        if self._blackboard_store is not None:
            return self._blackboard_store
        path = self.config.project_root / self.config.runtime.workflow.blackboard.path
        if self.run_store.backend == "sqlite" and path.is_file():
            return await self.blackboard_store()
        return None

    async def blackboard_engine(
        self,
        request: TaskRequest,
        *,
        lease: RunJobLease | None = None,
        store: BlackboardStore | None = None,
    ) -> BlackboardEngine:
        if (
            self.config.runtime.execution.role != "embedded"
            and self.run_store.backend != "postgres"
        ):
            raise ValueError("distributed blackboard execution requires PostgreSQL")
        if (
            self.run_store.backend == "postgres"
            and self.config.runtime.execution.role == "embedded"
        ):
            raise ValueError("PostgreSQL blackboard execution requires API/Worker roles")
        store = store or await self.blackboard_store()
        if lease is not None:
            if not isinstance(store, PostgresBlackboardStore):
                raise ValueError("job leases require PostgreSQL blackboard storage")
            store = store.for_lease(lease)
        receipts = (
            build_verification_receipt_store(store)
            if not isinstance(store, MemoryBlackboardStore)
            else self._blackboard_receipts
        )
        return BlackboardEngine(
            store,
            receipts,
            AgentServices(
                config=self.config.model_copy(deep=True),
                gateway=self.model_gateway,
                prompts=self.prompt_registry,
                trace=TraceRecorder(
                    enabled=self.config.runtime.trace.enabled,
                    sample_rate=self.config.runtime.trace.sample_rate,
                ),
                tool_gateway=self.tool_gateway,
                skills=self.skill_registry,
            ),
            request,
            LeasedPostgresSaver(store, self.checkpointer)
            if lease is not None
            else self.checkpointer,
        )


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
    provider_store: ProviderProfileStore,
    settings_store: ApplicationSettingsStore,
    settings_snapshot: ApplicationSettings,
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
        cache_max_entries=config.runtime.tool_cache_max_entries,
    )
    prompts = PromptRegistry(
        config.project_root / "prompts",
        overrides=settings_snapshot.prompts,
    )
    skill_registry = SkillRegistry(config.project_root / "skills")
    skill_registry.load()
    assignable_roles = [name for name, role in config.agents.agents.items() if role.assignable]
    skill_registry.validate_compatibility(
        config.models,
        registry,
        assignable_roles=assignable_roles,
    )
    trace = TraceRecorder(
        enabled=config.runtime.trace.enabled,
        sample_rate=config.runtime.trace.sample_rate,
    )
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
        provider_store=provider_store,
        settings_store=settings_store,
        settings_snapshot=settings_snapshot,
        prompt_registry=prompts,
        skill_registry=skill_registry,
        provider_compatibility=provider_compatibility,
        provider_adapters=provider_adapters,
        tool_adapters=tool_adapters,
        checkpointer=checkpointer,
        provider_target_fingerprints={
            name: hashlib.sha256(
                (os.getenv(provider.base_url_env or "", "").strip() or provider.base_url or "")
                .rstrip("/")
                .encode("utf-8")
            ).hexdigest()
            for name, provider in config.models.providers.items()
            if provider.enabled
        },
    )


async def _close_providers(runtime: HarnessRuntime) -> None:
    try:
        await runtime.model_gateway.close()
    finally:
        if runtime._blackboard_store is not None:
            await runtime._blackboard_store.close()


async def _attach_stored_provider_credentials(
    config: AppConfig,
    provider_store: ProviderProfileStore,
) -> None:
    for name, provider in config.models.providers.items():
        if provider.credential_source != "stored":
            continue
        provider.set_runtime_api_key(await provider_store.get_api_key(name))


@asynccontextmanager
async def build_runtime(
    project_root: Path | str | None = None,
    *,
    checkpoint_backend: str | None = None,
    run_store_backend: str | None = None,
    provider_store_backend: str | None = None,
    provider_adapters: ProviderAdapterRegistry | None = None,
    tool_adapters: ToolAdapterRegistry | None = None,
) -> AsyncIterator[HarnessRuntime]:
    config = load_app_config(project_root)
    backend = checkpoint_backend or config.runtime.checkpoint.backend
    selected_run_store = run_store_backend
    if selected_run_store is None and checkpoint_backend == "memory":
        selected_run_store = "memory"
    run_store = build_run_store(
        config.runtime.run_store,
        config.project_root,
        backend=selected_run_store,
    )
    selected_provider_store = provider_store_backend
    if selected_provider_store is None and checkpoint_backend == "memory":
        selected_provider_store = "memory"
    provider_store = build_provider_profile_store(
        config.runtime.provider_store,
        config.project_root,
        backend=selected_provider_store,
    )
    settings_store = ApplicationSettingsStore(
        None if selected_provider_store == "memory" else default_settings_path(config)
    )
    adapters = provider_adapters or build_default_provider_registry()
    selected_tool_adapters = tool_adapters or build_default_tool_adapter_registry()
    # An embedded runtime holds this for its entire lifetime, including shutdown.
    # Offline backup takes the same lock before snapshotting the related databases.
    storage_lock = (
        BlackboardProcessLock(config.project_root / config.runtime.run_store.path)
        if run_store.backend == "sqlite"
        else None
    )
    try:
        if storage_lock is not None:
            storage_lock.acquire()
        await run_store.initialize()
        await provider_store.initialize()
        await settings_store.initialize()
        settings_snapshot = await settings_store.get()
        config = apply_application_settings(config, settings_snapshot)
        config = apply_provider_profiles(config, await provider_store.list())
        await _attach_stored_provider_credentials(config, provider_store)
        configure_logging(config.runtime.trace.log_level)
        if backend == "memory":
            runtime = _build_runtime(
                config,
                InMemorySaver(),
                run_store,
                provider_store,
                settings_store,
                settings_snapshot,
                adapters,
                selected_tool_adapters,
            )
            try:
                yield runtime
            finally:
                await _close_providers(runtime)
            return
        if backend == "sqlite":
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
                    provider_store,
                    settings_store,
                    settings_snapshot,
                    adapters,
                    selected_tool_adapters,
                )
                try:
                    yield runtime
                finally:
                    await _close_providers(runtime)
            return
        if backend == "postgres":
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            dsn_env = config.runtime.checkpoint.dsn_env
            dsn = os.getenv(dsn_env, "").strip()
            if not dsn:
                raise ValueError(f"PostgreSQL checkpoint requires {dsn_env} to be configured")
            os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
            async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
                runtime = _build_runtime(
                    config,
                    saver,
                    run_store,
                    provider_store,
                    settings_store,
                    settings_snapshot,
                    adapters,
                    selected_tool_adapters,
                )
                try:
                    yield runtime
                finally:
                    await _close_providers(runtime)
            return
        raise ValueError(f"unsupported checkpoint backend: {backend}")
    finally:
        try:
            await settings_store.close()
            await provider_store.close()
            await run_store.close()
        finally:
            if storage_lock is not None:
                storage_lock.release()
