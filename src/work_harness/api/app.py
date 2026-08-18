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

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from work_harness.config.models import AppConfig, ProviderConfig
from work_harness.domain.enums import AgentFunction, RunStatus
from work_harness.domain.models import RunResult, TaskRequest
from work_harness.llm.profiles import (
    apply_provider_profiles,
    provider_agent_bindings,
    provider_model_bindings,
)
from work_harness.runtime import HarnessRuntime, build_runtime
from work_harness.service import IdempotencyConflictError, RunManager, RunNotFoundError
from work_harness.skills import SkillError
from work_harness.storage import (
    ProviderProfileRecord,
    ProviderProfileSpec,
    ProviderProfileStoreNotFoundError,
)


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunAccepted(ApiModel):
    run_id: str
    status_url: str


class ResumeRequest(ApiModel):
    approved: bool
    note: str | None = None


class RunSummary(ApiModel):
    run_id: str
    status: str
    created_at: datetime
    updated_at: datetime


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


class AppState:
    runtime: HarnessRuntime
    runs: RunManager


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
        version="0.1.0",
        docs_url="/docs" if preview.api.expose_docs else None,
        redoc_url=None,
        lifespan=lifespan,
        dependencies=[Depends(authorize)],
    )

    @app.get("/health/live", include_in_schema=False)
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "providers": await state_holder.runtime.model_gateway.health(),
            "checkpoint": state_holder.runtime.config.runtime.checkpoint.backend,
            "run_store": state_holder.runtime.run_store.backend,
            "provider_store": state_holder.runtime.provider_store.backend,
            "provider_compatible": state_holder.runtime.provider_compatibility.compatible,
        }

    def provider_readiness(config: ProviderConfig) -> tuple[bool | None, bool | None]:
        if config.adapter != "openai_compatible":
            return None, None
        endpoint = bool(
            (config.base_url_env and os.getenv(config.base_url_env, "").strip())
            or config.base_url
        )
        credential = bool(config.api_key_env and os.getenv(config.api_key_env, ""))
        return endpoint, credential

    async def provider_state() -> tuple[ProviderList, AppConfig]:
        profiles = await state_holder.runtime.provider_store.list()
        profiles_by_name = {item.name: item for item in profiles}
        persisted_names = {item.name for item in profiles}
        base = load_app_config(state_holder.runtime.config.project_root)
        configured = apply_provider_profiles(base, profiles)
        loaded = state_holder.runtime.config
        names = sorted(
            set(configured.models.providers) | set(loaded.models.providers)
        )
        items: list[ProviderDescriptor] = []
        for name in names:
            is_removed = name not in configured.models.providers
            selected = (
                loaded.models.providers[name]
                if is_removed
                else configured.models.providers[name]
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
                and selected == loaded.models.providers[name]
                and effective_bindings == provider_model_bindings(loaded, name)
                and (
                    not selected.enabled
                    or active_agent_bindings == provider_agent_bindings(loaded, name)
                )
            )
            endpoint, credential = provider_readiness(selected)
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

    @app.get("/v1/capabilities")
    async def capabilities() -> dict[str, Any]:
        function_labels = {
            AgentFunction.ANALYSIS: ("分析", "梳理需求、比较方案与验证假设"),
            AgentFunction.RESEARCH: ("研究", "收集资料、证据与可追溯来源"),
            AgentFunction.EXECUTION: ("执行", "实现方案并形成可交付结果"),
            AgentFunction.RISK: ("风险", "识别安全、合规与交付风险"),
        }
        return {
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
        try:
            run_id = await state_holder.runs.start(request)
        except IdempotencyConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SkillError as exc:
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
        profile: ProviderProfileSpec,
    ) -> ProviderSaved:
        existing = await state_holder.runtime.provider_store.list()
        prospective = [item for item in existing if item.name != provider_name]
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
            await state_holder.runtime.provider_store.upsert(provider_name, profile)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        result, _ = await provider_state()
        saved = next(item for item in result.items if item.name == provider_name)
        return ProviderSaved(provider=saved, restart_required=result.restart_required)

    @app.delete("/v1/providers/{provider_name}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_provider(provider_name: str) -> Response:
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
                )
                for item in records
            ],
            limit=limit,
            offset=offset,
        )

    @app.get("/v1/runs/{run_id}", response_model=RunResult)
    async def get_run(run_id: str) -> RunResult:
        try:
            return await state_holder.runs.get(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @app.get("/v1/runs/{run_id}/events")
    async def stream_run(run_id: str, request: Request) -> StreamingResponse:
        try:
            initial = await state_holder.runs.get(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

        async def events() -> AsyncIterator[str]:
            previous_fingerprint = ""
            current = initial
            sequence = 0
            keepalive_ticks = 0
            while True:
                fingerprint = ":".join(
                    (
                        str(current.status),
                        str(len(current.artifacts)),
                        str(len(current.reviews)),
                        str(len(current.model_calls)),
                        str(len(current.tool_calls)),
                        str(len(current.errors)),
                    )
                )
                if fingerprint != previous_fingerprint:
                    sequence += 1
                    payload = json.dumps(
                        current.model_dump(mode="json"),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    yield f"id: {sequence}\nevent: run\ndata: {payload}\n\n"
                    previous_fingerprint = fingerprint
                    keepalive_ticks = 0
                if current.status not in {RunStatus.PENDING, RunStatus.RUNNING}:
                    return
                if await request.is_disconnected():
                    return
                await asyncio.sleep(0.25)
                keepalive_ticks += 1
                if keepalive_ticks >= 60:
                    yield ": keepalive\n\n"
                    keepalive_ticks = 0
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

    @app.post("/v1/runs/{run_id}/resume", status_code=status.HTTP_202_ACCEPTED)
    async def resume_run(run_id: str, request: ResumeRequest) -> dict[str, str]:
        try:
            await state_holder.runs.resume(run_id, approved=request.approved, note=request.note)
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
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")

    return app
