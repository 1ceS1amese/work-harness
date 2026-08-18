from __future__ import annotations

import asyncio
import hmac
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from work_harness.domain.enums import AgentFunction, RunStatus
from work_harness.domain.models import RunResult, TaskRequest
from work_harness.runtime import HarnessRuntime, build_runtime
from work_harness.service import IdempotencyConflictError, RunManager, RunNotFoundError
from work_harness.skills import SkillError


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

    async def authorize(x_api_key: Annotated[str | None, Header()] = None) -> None:
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

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "providers": await state_holder.runtime.model_gateway.health(),
            "checkpoint": state_holder.runtime.config.runtime.checkpoint.backend,
            "run_store": state_holder.runtime.run_store.backend,
            "provider_compatible": state_holder.runtime.provider_compatibility.compatible,
        }

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
            "skills": [
                item.model_dump(mode="json")
                for item in state_holder.runtime.skill_registry.descriptors()
            ],
        }

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
