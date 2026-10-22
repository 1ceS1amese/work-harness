from __future__ import annotations

import asyncio
import hmac
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from work_harness import __version__
from work_harness.api.blackboard import blackboard_router, stream_blackboard
from work_harness.api.middleware import RequestGuardMiddleware
from work_harness.blackboard.inputs import HumanInput, HumanInputResult
from work_harness.blackboard.protocol import BlackboardError
from work_harness.config.models import AppConfig, ProviderConfig
from work_harness.domain.enums import AgentFunction, RunStatus
from work_harness.domain.models import RunResult, TaskRequest
from work_harness.knowledge_sources.catalog import draft_task_input
from work_harness.llm.profiles import (
    apply_provider_profiles,
    provider_agent_bindings,
    provider_model_bindings,
)
from work_harness.prompts import PromptRegistry
from work_harness.runtime import HarnessRuntime, build_runtime
from work_harness.service import (
    IdempotencyConflictError,
    RunCapacityError,
    RunExecutionMismatchError,
    RunManager,
    RunNotFoundError,
)
from work_harness.skills import SkillError
from work_harness.storage import (
    ApplicationSettings,
    EditableRuntimeSettings,
    ProviderProfileRecord,
    ProviderProfileSpec,
    ProviderProfileStoreNotFoundError,
    RunQueueCapacityError,
    RunQueueStateError,
    apply_application_settings,
)
from work_harness.storage.base import RunExecutionVersion, RunStoreNotFoundError


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunAccepted(ApiModel):
    run_id: str
    status_url: str


class ResumeRequest(ApiModel):
    approved: bool
    note: str | None = None


class BlackboardResumeRequest(ApiModel):
    engine: Literal["blackboard"]


class RunSummary(ApiModel):
    run_id: str
    status: str
    created_at: datetime
    updated_at: datetime
    engine: Literal["legacy", "blackboard"] = "legacy"
    execution: RunExecutionVersion | None = None


class RunIndexPage(ApiModel):
    items: list[RunSummary]
    limit: int
    offset: int


class ProviderDescriptor(ApiModel):
    name: str
    source: Literal["yaml", "persistent", "removed"]
    config: ProviderConfig
    model_bindings: dict[str, str]
    agent_bindings: dict[str, str]
    loaded: bool
    endpoint_available: bool | None
    credential_available: bool | None


class ProviderList(ApiModel):
    items: list[ProviderDescriptor]
    restart_required: bool


class ProviderSaved(ApiModel):
    provider: ProviderDescriptor
    restart_required: bool


class ProviderSaveRequest(ProviderProfileSpec):
    api_key: SecretStr | None = Field(default=None, min_length=1, max_length=65_536)
    clear_api_key: bool = False

    @model_validator(mode="after")
    def validate_credential_update(self) -> ProviderSaveRequest:
        if self.api_key is not None and self.clear_api_key:
            raise ValueError("cannot set and clear a Provider credential together")
        return self

    def profile_spec(self) -> ProviderProfileSpec:
        return ProviderProfileSpec(
            config=self.config,
            model_bindings=self.model_bindings,
            agent_bindings=self.agent_bindings,
        )


class PromptSettingDescriptor(ApiModel):
    name: str
    display_name: str
    description: str
    version: str
    body: str
    customized: bool
    loaded: bool


class ApplicationSettingsDescriptor(ApiModel):
    prompts: list[PromptSettingDescriptor]
    runtime: EditableRuntimeSettings
    restart_required: bool
    storage: str


class PromptUpdateRequest(ApiModel):
    body: str = Field(min_length=1, max_length=50_000)


class DeadJobDescriptor(ApiModel):
    job_id: UUID
    run_id: str
    kind: str
    attempt_count: int
    max_attempts: int
    last_error_code: str | None
    last_error_message: str | None
    created_at: datetime
    completed_at: datetime | None


class DeadJobPage(ApiModel):
    items: list[DeadJobDescriptor]
    limit: int
    offset: int


class DeadJobReplayAccepted(ApiModel):
    run_id: str
    job_id: UUID


class AppState:
    runtime: HarnessRuntime
    runs: RunManager


class FrontendStaticFiles(StaticFiles):
    """Serve the SPA entry point for known browser routes without masking API 404s."""

    async def get_response(self, path: str, scope: dict[str, Any]) -> Response:
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or not (path == "settings" or path.startswith("settings/")):
                raise
        else:
            if response.status_code != 404 or not (
                path == "settings" or path.startswith("settings/")
            ):
                return response
        return await super().get_response("index.html", scope)


def create_app(project_root: Path | str | None = None) -> FastAPI:
    state_holder = AppState()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with build_runtime(project_root) as runtime:
            state_holder.runtime = runtime
            state_holder.runs = RunManager(runtime)
            await state_holder.runs.initialize()
            try:
                yield
            finally:
                await state_holder.runs.close()

    preview_config_root = Path(project_root).resolve() if project_root else None
    from work_harness.config.loader import load_app_config

    preview = load_app_config(preview_config_root)
    if preview.runtime.execution.role == "worker":
        raise ValueError(
            "the worker execution role cannot expose the API; use `work-harness worker`"
        )
    if preview.api.auth_enabled and not os.getenv(preview.api.api_key_env, ""):
        raise ValueError(
            f"API authentication is enabled but {preview.api.api_key_env} is not configured"
        )

    async def authorize(
        request: Request,
        x_api_key: Annotated[str | None, Header()] = None,
    ) -> None:
        if request.url.path == "/health/live":
            return
        config = state_holder.runtime.config.api
        if not config.auth_enabled:
            return
        expected = os.getenv(config.api_key_env, "")
        if not expected or not x_api_key or not hmac.compare_digest(expected, x_api_key):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    app = FastAPI(
        title="Work Harness API",
        version=__version__,
        docs_url="/docs" if preview.api.expose_docs else None,
        redoc_url=None,
        lifespan=lifespan,
        dependencies=[Depends(authorize)],
    )
    app.add_middleware(
        RequestGuardMiddleware,
        max_body_bytes=preview.api.request_max_bytes,
        rate_limit_per_minute=preview.api.rate_limit_per_minute,
    )
    app.include_router(blackboard_router(lambda: state_holder.runs))

    @app.get("/health/live", include_in_schema=False)
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    async def runtime_readiness() -> tuple[bool, dict[str, bool]]:
        providers = await state_holder.runtime.model_gateway.health()
        models = state_holder.runtime.config.models
        logical_models_ready = bool(models.logical_models) and all(
            any(
                models.providers[candidate.provider].enabled
                and providers.get(candidate.provider, False)
                and candidate.model != "configured-model"
                for candidate in logical.candidates
            )
            for logical in models.logical_models.values()
        )
        ready = state_holder.runtime.provider_compatibility.compatible and logical_models_ready
        runtime = state_holder.runtime.config.runtime
        if runtime.execution.role == "api":
            queue_compatible = False
            try:
                workers = await state_holder.runtime.run_store.list_live_workers(
                    stale_seconds=runtime.execution.worker_stale_seconds
                )
                queue = await state_holder.runtime.run_store.queue_stats(
                    graph_revision=state_holder.runs.execution_fingerprint
                )
            except Exception:
                workers = []
            else:
                queue_compatible = queue.incompatible_active_jobs == 0
            ready = (
                ready
                and bool(workers)
                and all(
                    item.config_fingerprint == state_holder.runs.execution_fingerprint
                    for item in workers
                )
                and queue_compatible
            )
        return ready, providers

    async def health_payload() -> dict[str, Any]:
        ready, providers = await runtime_readiness()
        scheduler: dict[str, Any] = state_holder.runs.stats()
        runtime = state_holder.runtime.config.runtime
        if runtime.execution.role != "embedded":
            try:
                queue = await state_holder.runtime.run_store.queue_stats(
                    graph_revision=state_holder.runs.execution_fingerprint
                )
                workers = await state_holder.runtime.run_store.list_live_workers(
                    stale_seconds=runtime.execution.worker_stale_seconds
                )
            except Exception:
                ready = False
                scheduler["shared_queue_healthy"] = False
            else:
                scheduler.update(
                    {
                        "shared_queue_healthy": True,
                        "shared_queue": queue.model_dump(mode="json"),
                        "live_workers": len(workers),
                        "worker_capacity": sum(item.capacity for item in workers),
                        "worker_active_jobs": sum(item.active_jobs for item in workers),
                        "worker_config_consistent": bool(workers)
                        and all(
                            item.config_fingerprint == state_holder.runs.execution_fingerprint
                            for item in workers
                        ),
                    }
                )
        return {
            "status": "ok" if ready else "degraded",
            "providers": providers,
            "checkpoint": state_holder.runtime.config.runtime.checkpoint.backend,
            "run_store": state_holder.runtime.run_store.backend,
            "provider_store": state_holder.runtime.provider_store.backend,
            "settings_store": state_holder.runtime.settings_store.backend,
            "provider_compatible": state_holder.runtime.provider_compatibility.compatible,
            "scheduler": scheduler,
        }

    def require_local_mutable_settings() -> None:
        if state_holder.runtime.config.runtime.execution.role != "embedded":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "多实例模式要求各实例使用一致的部署配置，"
                    "暂不允许通过单实例页面修改 Provider 或运行设置"
                ),
            )

    def require_distributed_queue() -> None:
        if state_holder.runtime.config.runtime.execution.role == "embedded":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="死信管理只在 PostgreSQL 多实例队列模式中可用",
            )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return await health_payload()

    @app.get("/health/ready", response_model=None)
    async def readiness() -> JSONResponse:
        payload = await health_payload()
        return JSONResponse(
            payload,
            status_code=200 if payload["status"] == "ok" else 503,
        )

    def provider_readiness(
        config: ProviderConfig,
        profile: ProviderProfileRecord | None,
    ) -> tuple[bool | None, bool | None]:
        if config.adapter != "openai_compatible":
            return None, None
        endpoint = bool(
            (config.base_url_env and os.getenv(config.base_url_env, "").strip()) or config.base_url
        )
        credential = (
            bool(profile and profile.credential_configured)
            if config.credential_source == "stored"
            else bool(config.api_key_env and os.getenv(config.api_key_env, ""))
        )
        return endpoint, credential

    async def provider_state() -> tuple[ProviderList, AppConfig]:
        profiles = await state_holder.runtime.provider_store.list()
        profiles_by_name = {item.name: item for item in profiles}
        persisted_names = {item.name for item in profiles}
        base = load_app_config(state_holder.runtime.config.project_root)
        configured = apply_provider_profiles(base, profiles)
        loaded = state_holder.runtime.config
        names = sorted(set(configured.models.providers) | set(loaded.models.providers))
        items: list[ProviderDescriptor] = []
        for name in names:
            is_removed = name not in configured.models.providers
            selected = (
                loaded.models.providers[name] if is_removed else configured.models.providers[name]
            )
            effective_bindings = provider_model_bindings(
                loaded if is_removed else configured,
                name,
            )
            selected_bindings = (
                profiles_by_name[name].model_bindings
                if name in profiles_by_name
                else effective_bindings
            )
            active_agent_bindings = provider_agent_bindings(
                loaded if is_removed else configured,
                name,
            )
            selected_agent_bindings = (
                profiles_by_name[name].agent_bindings
                if name in profiles_by_name
                else active_agent_bindings
            )
            is_loaded = (
                name in loaded.models.providers
                and selected.model_dump(mode="python")
                == loaded.models.providers[name].model_dump(mode="python")
                and effective_bindings == provider_model_bindings(loaded, name)
                and (
                    not selected.enabled
                    or active_agent_bindings == provider_agent_bindings(loaded, name)
                )
            )
            endpoint, credential = provider_readiness(
                selected,
                profiles_by_name.get(name),
            )
            items.append(
                ProviderDescriptor(
                    name=name,
                    source=(
                        "removed"
                        if is_removed
                        else "persistent"
                        if name in persisted_names
                        else "yaml"
                    ),
                    config=selected,
                    model_bindings=selected_bindings,
                    agent_bindings=selected_agent_bindings,
                    loaded=is_loaded,
                    endpoint_available=endpoint,
                    credential_available=credential,
                )
            )
        return ProviderList(
            items=items,
            restart_required=any(not item.loaded for item in items),
        ), configured

    async def application_settings_state() -> ApplicationSettingsDescriptor:
        saved = await state_holder.runtime.settings_store.get()
        loaded = state_holder.runtime.settings_snapshot
        base = load_app_config(state_holder.runtime.config.project_root)
        runtime_settings = saved.runtime or EditableRuntimeSettings.from_runtime(base.runtime)
        prompt_copy = {
            "normalizer": ("需求整理员", "读懂输入并整理成明确任务，不直接解决任务"),
            "planner": ("任务协调员", "拆分任务、安排依赖和成员分工"),
            "worker": ("智能成员", "四名可分配成员共同使用的执行提示词"),
            "reviewer": ("交叉复核组", "独立检查事实、逻辑和要求是否满足"),
            "judge": ("质量裁决员", "决定通过、返工、询问用户或安全结束"),
            "synthesizer": ("成果汇总员", "只根据通过检查的产物整理最终回复"),
        }
        defaults = PromptRegistry(base.project_root / "prompts")
        prompts: list[PromptSettingDescriptor] = []
        for name, (display_name, description) in prompt_copy.items():
            template = defaults.get(name)
            prompts.append(
                PromptSettingDescriptor(
                    name=name,
                    display_name=display_name,
                    description=description,
                    version=template.version,
                    body=saved.prompts.get(name, template.body),
                    customized=name in saved.prompts,
                    loaded=saved.prompts.get(name) == loaded.prompts.get(name),
                )
            )
        return ApplicationSettingsDescriptor(
            prompts=prompts,
            runtime=runtime_settings,
            restart_required=saved != loaded,
            storage=state_holder.runtime.settings_store.backend,
        )

    @app.get("/v1/capabilities")
    async def capabilities() -> dict[str, Any]:
        function_labels = {
            AgentFunction.ANALYSIS: ("分析", "梳理需求、比较方案与验证假设"),
            AgentFunction.RESEARCH: ("研究", "收集资料、证据与可追溯来源"),
            AgentFunction.EXECUTION: ("执行", "实现方案并形成可交付结果"),
            AgentFunction.RISK: ("风险", "识别安全、合规与交付风险"),
        }
        return {
            "workflow": {
                "default_engine": state_holder.runtime.config.runtime.workflow.engine,
                "accept_new_blackboard_runs": (
                    state_holder.runtime.config.runtime.workflow.accept_new_blackboard_runs
                ),
                "blackboard_templates": [
                    {
                        "id": "saved-draft",
                        "title": "保存候选草稿",
                        "description": (
                            "完成条件是草稿已保存且可读取；"
                            "内容准确性与实际任务结果仍需核验。"
                        ),
                        "input": draft_task_input().model_dump(mode="json"),
                    }
                ],
            },
            "logical_models": state_holder.runtime.model_gateway.logical_models,
            "tools": state_holder.runtime.tool_registry.names,
            "tool_adapters": state_holder.runtime.tool_adapters.adapters,
            "provider_adapters": state_holder.runtime.provider_adapters.adapters,
            "provider_contract": {
                "protocol_version": (state_holder.runtime.provider_compatibility.protocol_version),
                "baseline_capabilities": (
                    state_holder.runtime.provider_compatibility.baseline_capabilities
                ),
                "compatible": state_holder.runtime.provider_compatibility.compatible,
            },
            "providers": [
                item.model_dump(mode="json")
                for item in state_holder.runtime.provider_compatibility.providers
            ],
            "skills": [
                item.model_dump(mode="json")
                for item in state_holder.runtime.skill_registry.descriptors()
            ],
            "task_types": ["general", "planning", "decision", "research", "code"],
            "allocation_modes": ["auto", "direct", "cross_functional"],
            "runtime_policy": {
                "review_strategy": state_holder.runtime.config.runtime.review_strategy,
                "deep_review_risk_levels": sorted(
                    state_holder.runtime.config.runtime.deep_review_risk_levels
                ),
                "deep_review_task_types": sorted(
                    state_holder.runtime.config.runtime.deep_review_task_types
                ),
                "run_max_concurrency": (state_holder.runtime.config.runtime.run_max_concurrency),
            },
            "functions": [
                {"id": function, "display_name": label, "description": description}
                for function, (label, description) in function_labels.items()
            ],
            "agents": [
                {
                    "id": name,
                    "display_name": role.display_name or name,
                    "responsibility": role.responsibility,
                    "specialty": role.specialty,
                    "assignable": role.assignable,
                    "model_route": (
                        state_holder.runtime.config.models.agent_routes[name].model_dump(
                            mode="json"
                        )
                        if name in state_holder.runtime.config.models.agent_routes
                        else None
                    ),
                }
                for name, role in state_holder.runtime.config.agents.agents.items()
                if role.assignable
            ],
        }

    @app.post("/v1/runs", response_model=RunAccepted, status_code=status.HTTP_202_ACCEPTED)
    async def create_run(request: TaskRequest) -> RunAccepted:
        ready, _ = await runtime_readiness()
        if not ready:
            detail = (
                "模型服务尚未就绪，请先在设置页面完成配置并重启服务"
                if state_holder.runtime.config.runtime.execution.role == "embedded"
                else ("分布式服务尚未就绪，请检查数据库、真实模型配置、Worker 心跳和配置摘要")
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=detail,
            )
        try:
            run_id = await state_holder.runs.start(request)
        except RunCapacityError as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="运行队列已满，请稍后重试",
                headers={"Retry-After": str(exc.retry_after_seconds)},
            ) from exc
        except IdempotencyConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SkillError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return RunAccepted(run_id=run_id, status_url=f"/v1/runs/{run_id}")

    @app.get("/v1/compatibility")
    async def compatibility() -> dict[str, Any]:
        return {
            "providers": state_holder.runtime.provider_compatibility.model_dump(mode="json"),
            "provider_adapters": state_holder.runtime.provider_adapters.adapters,
            "tool_adapters": state_holder.runtime.tool_adapters.adapters,
            "agent_routes": {
                name: route.model_dump(mode="json")
                for name, route in state_holder.runtime.config.models.agent_routes.items()
            },
            "skills": [
                item.model_dump(mode="json")
                for item in state_holder.runtime.skill_registry.descriptors()
            ],
        }

    @app.get("/v1/providers", response_model=ProviderList)
    async def list_providers() -> ProviderList:
        result, _ = await provider_state()
        return result

    @app.put("/v1/providers/{provider_name}", response_model=ProviderSaved)
    async def save_provider(
        provider_name: str,
        request: ProviderSaveRequest,
    ) -> ProviderSaved:
        require_local_mutable_settings()
        existing = await state_holder.runtime.provider_store.list()
        prospective = [item for item in existing if item.name != provider_name]
        previous = next((item for item in existing if item.name == provider_name), None)
        api_key = request.api_key.get_secret_value() if request.api_key else None
        clear_api_key = request.clear_api_key or request.config.credential_source != "stored"
        credential_configured = bool(api_key) or bool(
            previous and previous.credential_configured and not clear_api_key
        )
        if (
            request.config.adapter == "openai_compatible"
            and request.config.enabled
            and request.config.credential_source == "stored"
            and not credential_configured
        ):
            raise HTTPException(
                status_code=409,
                detail="enabled stored credential Provider requires an API Key",
            )
        profile = request.profile_spec()
        try:
            prospective.append(
                ProviderProfileRecord(
                    name=provider_name,
                    config=profile.config,
                    model_bindings=profile.model_bindings,
                    agent_bindings=profile.agent_bindings,
                )
            )
            base = load_app_config(state_holder.runtime.config.project_root)
            apply_provider_profiles(base, prospective)
            await state_holder.runtime.provider_store.upsert(
                provider_name,
                profile,
                api_key=api_key,
                clear_api_key=clear_api_key,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        result, _ = await provider_state()
        saved = next(item for item in result.items if item.name == provider_name)
        return ProviderSaved(provider=saved, restart_required=result.restart_required)

    @app.delete("/v1/providers/{provider_name}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_provider(provider_name: str) -> Response:
        require_local_mutable_settings()
        existing = await state_holder.runtime.provider_store.list()
        prospective = [item for item in existing if item.name != provider_name]
        try:
            base = load_app_config(state_holder.runtime.config.project_root)
            apply_provider_profiles(base, prospective)
            await state_holder.runtime.provider_store.delete(provider_name)
        except ProviderProfileStoreNotFoundError as exc:
            raise HTTPException(status_code=404, detail="provider profile not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/v1/settings", response_model=ApplicationSettingsDescriptor)
    async def get_application_settings() -> ApplicationSettingsDescriptor:
        return await application_settings_state()

    @app.put(
        "/v1/settings/prompts/{prompt_name}",
        response_model=ApplicationSettingsDescriptor,
    )
    async def save_prompt_setting(
        prompt_name: str,
        request: PromptUpdateRequest,
    ) -> ApplicationSettingsDescriptor:
        require_local_mutable_settings()

        def update_prompt(current: ApplicationSettings) -> ApplicationSettings:
            prompts = dict(current.prompts)
            prompts[prompt_name] = request.body
            return ApplicationSettings(prompts=prompts, runtime=current.runtime)

        try:
            await state_holder.runtime.settings_store.update(update_prompt)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return await application_settings_state()

    @app.delete(
        "/v1/settings/prompts/{prompt_name}",
        response_model=ApplicationSettingsDescriptor,
    )
    async def reset_prompt_setting(prompt_name: str) -> ApplicationSettingsDescriptor:
        require_local_mutable_settings()

        def reset_prompt(current: ApplicationSettings) -> ApplicationSettings:
            if prompt_name not in current.prompts:
                raise HTTPException(status_code=404, detail="prompt override not found")
            prompts = dict(current.prompts)
            del prompts[prompt_name]
            return ApplicationSettings(prompts=prompts, runtime=current.runtime)

        await state_holder.runtime.settings_store.update(reset_prompt)
        return await application_settings_state()

    @app.put("/v1/settings/runtime", response_model=ApplicationSettingsDescriptor)
    async def save_runtime_settings(
        request: EditableRuntimeSettings,
    ) -> ApplicationSettingsDescriptor:
        require_local_mutable_settings()

        def update_runtime(current: ApplicationSettings) -> ApplicationSettings:
            prospective = ApplicationSettings(prompts=current.prompts, runtime=request)
            base = load_app_config(state_holder.runtime.config.project_root)
            apply_application_settings(base, prospective)
            return prospective

        try:
            await state_holder.runtime.settings_store.update(update_runtime)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return await application_settings_state()

    @app.get("/v1/runs", response_model=RunIndexPage)
    async def list_runs(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> RunIndexPage:
        records = await state_holder.runs.list(limit=limit, offset=offset)
        return RunIndexPage(
            items=[
                RunSummary(
                    run_id=item.run_id,
                    status=item.status,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                    engine=item.engine,
                    execution=item.execution,
                )
                for item in records
            ],
            limit=limit,
            offset=offset,
        )

    @app.get("/v1/queue/dead", response_model=DeadJobPage)
    async def list_dead_jobs(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> DeadJobPage:
        require_distributed_queue()
        jobs = await state_holder.runtime.run_store.list_dead_jobs(
            limit=limit,
            offset=offset,
        )
        return DeadJobPage(
            items=[
                DeadJobDescriptor(
                    job_id=job.job_id,
                    run_id=job.run_id,
                    kind=str(job.kind),
                    attempt_count=job.attempt_count,
                    max_attempts=job.max_attempts,
                    last_error_code=job.last_error_code,
                    last_error_message=job.last_error_message,
                    created_at=job.created_at,
                    completed_at=job.completed_at,
                )
                for job in jobs
            ],
            limit=limit,
            offset=offset,
        )

    @app.post(
        "/v1/queue/dead/{job_id}/replay",
        response_model=DeadJobReplayAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def replay_dead_job(job_id: UUID) -> DeadJobReplayAccepted:
        require_distributed_queue()
        runtime = state_holder.runtime.config.runtime
        try:
            replay = await state_holder.runtime.run_store.replay_dead_job(
                job_id,
                capacity=runtime.run_queue_capacity,
                max_attempts=runtime.execution.job_max_attempts,
                graph_revision=state_holder.runs.execution_fingerprint,
            )
        except RunQueueCapacityError as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="运行队列已满，请稍后重试",
                headers={"Retry-After": str(runtime.run_queue_retry_after_seconds)},
            ) from exc
        except RunQueueStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return DeadJobReplayAccepted(run_id=replay.run_id, job_id=replay.job_id)

    @app.get("/v1/runs/{run_id}", response_model=RunResult)
    async def get_run(run_id: str) -> RunResult:
        try:
            return await state_holder.runs.get(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @app.get("/v1/runs/{run_id}/events")
    async def stream_run(
        run_id: str,
        request: Request,
        after: Annotated[str | None, Query(min_length=1, max_length=16384)] = None,
        last_event_id: Annotated[str | None, Header(min_length=1, max_length=16384)] = None,
    ) -> StreamingResponse:
        try:
            record = await state_holder.runtime.run_store.get(run_id)
        except RunStoreNotFoundError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        if record is None:
            raise HTTPException(status_code=404, detail="run not found")
        if record.engine == "blackboard":
            return await stream_blackboard(
                state_holder.runs,
                run_id,
                request,
                after=after,
                last_event_id=last_event_id,
            )

        async def events() -> AsyncIterator[str]:
            previous_payload = ""
            sequence = 0
            keepalive_seconds = state_holder.runtime.config.runtime.sse_keepalive_seconds
            async with state_holder.runs.subscribe(run_id) as updates:
                current = await state_holder.runs.get(run_id)
                while True:
                    payload = json.dumps(
                        current.model_dump(mode="json"),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    if payload != previous_payload:
                        sequence += 1
                        yield f"id: {sequence}\nevent: run\ndata: {payload}\n\n"
                        previous_payload = payload
                    if current.status not in {RunStatus.PENDING, RunStatus.RUNNING}:
                        return
                    if await request.is_disconnected():
                        return
                    try:
                        await asyncio.wait_for(
                            updates.get(),
                            timeout=keepalive_seconds,
                        )
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        try:
                            current = await state_holder.runs.get(run_id)
                        except RunNotFoundError:
                            return
                        continue
                    try:
                        current = await state_holder.runs.get(run_id)
                    except RunNotFoundError:
                        return

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/v1/runs/{run_id}/inputs", response_model=HumanInputResult)
    async def submit_blackboard_input(run_id: str, request: HumanInput) -> HumanInputResult:
        try:
            return await state_holder.runs.submit_blackboard_input(run_id, request)
        except RunNotFoundError as exc:
            raise HTTPException(404, detail="run not found") from exc
        except (BlackboardError, RunExecutionMismatchError) as exc:
            raise HTTPException(409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc

    @app.post("/v1/runs/{run_id}/resume", status_code=status.HTTP_202_ACCEPTED)
    async def resume_run(
        run_id: str, request: ResumeRequest | BlackboardResumeRequest
    ) -> dict[str, str]:
        try:
            if isinstance(request, BlackboardResumeRequest):
                wakeup = await state_holder.runs.resume_blackboard(run_id)
                return {"run_id": run_id, "status": wakeup}
            await state_holder.runs.resume(run_id, approved=request.approved, note=request.note)
        except RunCapacityError as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="运行队列已满，请稍后重试",
                headers={"Retry-After": str(exc.retry_after_seconds)},
            ) from exc
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"run_id": run_id, "status": "resuming"}

    @app.delete("/v1/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def cancel_run(run_id: str) -> Response:
        try:
            await state_holder.runs.cancel(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    frontend_dist = preview.project_root / "frontend" / "dist"
    if frontend_dist.is_dir():
        app.mount(
            "/",
            FrontendStaticFiles(directory=frontend_dist, html=True),
            name="frontend",
        )

    return app
