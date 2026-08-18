from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import suppress
from typing import Any
from uuid import uuid4

from langgraph.types import Command

from work_harness.domain.enums import RunStatus
from work_harness.domain.models import (
    Artifact,
    Decision,
    ModelCallRecord,
    Plan,
    Review,
    RunError,
    RunResult,
    TaskRequest,
    TaskSpec,
    ToolCallRecord,
    Usage,
)
from work_harness.observability.logging import get_logger
from work_harness.runtime import HarnessRuntime
from work_harness.storage import RunRecord


class RunNotFoundError(KeyError):
    pass


class IdempotencyConflictError(ValueError):
    pass


class RunManager:
    def __init__(self, runtime: HarnessRuntime) -> None:
        self.runtime = runtime
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._owner = f"manager_{uuid4().hex}"
        self._recovery_task: asyncio.Task[Any] | None = None
        self._closing = False
        self._logger = get_logger("work_harness.run_manager")

    async def initialize(self) -> None:
        if not self.runtime.config.runtime.recovery_enabled:
            return
        await self.recover_incomplete()
        self._recovery_task = asyncio.create_task(
            self._recovery_loop(), name=f"{self._owner}:recovery"
        )

    async def close(self) -> None:
        self._closing = True
        if self._recovery_task:
            self._recovery_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._recovery_task
            self._recovery_task = None
        running = [task for task in self._tasks.values() if not task.done()]
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        self._tasks.clear()

    async def _recovery_loop(self) -> None:
        interval = self.runtime.config.runtime.recovery_scan_interval_seconds
        while not self._closing:
            await asyncio.sleep(interval)
            try:
                await self.recover_incomplete()
            except Exception:
                self._logger.exception("recovery_scan_failed")

    async def recover_incomplete(self) -> list[str]:
        recovered: list[str] = []
        records = await self.runtime.run_store.list_recoverable(
            limit=self.runtime.config.runtime.recovery_scan_limit
        )
        for record in records:
            if record.run_id in self._tasks:
                continue
            claimed = await self.runtime.run_store.claim(
                record.run_id,
                owner=self._owner,
                lease_seconds=self.runtime.config.runtime.run_lease_seconds,
            )
            if claimed is None:
                continue
            snapshot = await self.runtime.graph.aget_state(
                self._graph_config(record.run_id)
            )
            values = snapshot.values or {}
            if not values:
                await self.runtime.run_store.update_status(record.run_id, RunStatus.FAILED)
                continue
            state_status = RunStatus(values.get("status", RunStatus.RUNNING))
            if state_status not in {RunStatus.PENDING, RunStatus.RUNNING}:
                await self.runtime.run_store.update_status(record.run_id, state_status)
                continue
            self._spawn_execute(record.run_id, None, lease_claimed=True, suffix="recover")
            recovered.append(record.run_id)
        return recovered

    def _graph_config(self, run_id: str) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": run_id},
            "recursion_limit": self.runtime.config.runtime.max_node_steps
            * (self.runtime.config.runtime.max_repair_rounds + 2),
        }

    async def start(self, request: TaskRequest) -> str:
        run_id = f"run_{uuid4().hex}"
        request_payload = request.model_dump(mode="json", exclude={"idempotency_key"})
        request_fingerprint = self._fingerprint(request_payload)
        idempotency_key_hash = (
            self._fingerprint(request.idempotency_key) if request.idempotency_key else None
        )
        stored = await self.runtime.run_store.reserve(
            RunRecord(
                run_id=run_id,
                status=RunStatus.PENDING,
                idempotency_key_hash=idempotency_key_hash,
                request_fingerprint=request_fingerprint,
            )
        )
        if stored.run_id != run_id:
            if stored.request_fingerprint and stored.request_fingerprint != request_fingerprint:
                raise IdempotencyConflictError(
                    "idempotency key was already used with a different request"
                )
            return stored.run_id
        initial = {
            "run_id": run_id,
            "raw_input": request_payload,
            "artifacts": [],
            "reviews": [],
            "errors": [],
            "model_calls": [],
            "tool_calls": [],
            "repair_round": 0,
            "status": RunStatus.PENDING.value,
        }
        self._spawn_execute(run_id, initial)
        return run_id

    def _spawn_execute(
        self,
        run_id: str,
        input_value: Any,
        *,
        lease_claimed: bool = False,
        suffix: str | None = None,
    ) -> None:
        name = f"{run_id}:{suffix}" if suffix else run_id
        self._tasks[run_id] = asyncio.create_task(
            self._execute(run_id, input_value, lease_claimed=lease_claimed),
            name=name,
        )

    @staticmethod
    def _fingerprint(value: Any) -> str:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    async def _execute(
        self, run_id: str, input_value: Any, *, lease_claimed: bool = False
    ) -> None:
        if not lease_claimed:
            claimed = await self.runtime.run_store.claim(
                run_id,
                owner=self._owner,
                lease_seconds=self.runtime.config.runtime.run_lease_seconds,
            )
            if claimed is None:
                return
        await self.runtime.run_store.update_status(run_id, RunStatus.RUNNING)
        invocation = asyncio.create_task(
            self.runtime.graph.ainvoke(input_value, self._graph_config(run_id)),
            name=f"{run_id}:graph",
        )
        heartbeat = asyncio.create_task(
            self._heartbeat(run_id, invocation), name=f"{run_id}:heartbeat"
        )
        try:
            await asyncio.wait_for(
                invocation,
                timeout=self.runtime.config.runtime.run_timeout_seconds,
            )
        except asyncio.CancelledError:
            invocation.cancel()
            raise
        except TimeoutError:
            error = RunError(
                code="run_timeout",
                message="Run exceeded the configured deadline.",
                retryable=False,
            )
            await self.runtime.graph.aupdate_state(
                self._graph_config(run_id),
                {
                    "status": RunStatus.FAILED.value,
                    "errors": [error.model_dump(mode="json")],
                },
            )
        except Exception as exc:  # A failed background run must remain observable.
            error = RunError(
                code="run_failed",
                message=f"{type(exc).__name__}: {exc}",
                retryable=False,
            )
            await self.runtime.graph.aupdate_state(
                self._graph_config(run_id),
                {
                    "status": RunStatus.FAILED.value,
                    "errors": [error.model_dump(mode="json")],
                },
            )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            await self._sync_persisted_status(run_id)
            await self.runtime.run_store.release(run_id, owner=self._owner)

    async def _heartbeat(self, run_id: str, invocation: asyncio.Task[Any]) -> None:
        interval = self.runtime.config.runtime.heartbeat_interval_seconds
        while not invocation.done():
            await asyncio.sleep(interval)
            try:
                active = await self.runtime.run_store.heartbeat(
                    run_id,
                    owner=self._owner,
                    lease_seconds=self.runtime.config.runtime.run_lease_seconds,
                )
            except Exception:
                invocation.cancel()
                raise
            if not active:
                invocation.cancel()
                return

    async def _sync_persisted_status(self, run_id: str) -> None:
        snapshot = await self.runtime.graph.aget_state(self._graph_config(run_id))
        values = snapshot.values or {}
        if values.get("status"):
            await self.runtime.run_store.update_status(
                run_id, RunStatus(values["status"])
            )

    async def wait(self, run_id: str) -> RunResult:
        task = self._tasks.get(run_id)
        if task:
            await task
            self._tasks.pop(run_id, None)
        return await self.get(run_id)

    async def get(self, run_id: str) -> RunResult:
        record = await self.runtime.run_store.get(run_id)
        snapshot = await self.runtime.graph.aget_state(self._graph_config(run_id))
        values = snapshot.values or {}
        if not values:
            if record:
                return RunResult(run_id=run_id, status=record.status)
            raise RunNotFoundError(run_id)
        status = RunStatus(values.get("status", RunStatus.RUNNING))
        if record is None:
            record = await self.runtime.run_store.reserve(
                RunRecord(run_id=run_id, status=status)
            )
        elif record.status != status:
            await self.runtime.run_store.update_status(run_id, status)
        model_calls = [
            ModelCallRecord.model_validate(item) for item in values.get("model_calls", [])
        ]
        usage = Usage(
            input_tokens=sum(item.input_tokens for item in model_calls),
            output_tokens=sum(item.output_tokens for item in model_calls),
            cached_tokens=sum(item.cached_tokens for item in model_calls),
            estimated_cost_usd=sum(item.estimated_cost_usd for item in model_calls),
        )
        return RunResult(
            run_id=run_id,
            status=status,
            output=values.get("final_output", ""),
            task=TaskSpec.model_validate(values["task"]) if values.get("task") else None,
            plan=Plan.model_validate(values["plan"]) if values.get("plan") else None,
            artifacts=[Artifact.model_validate(item) for item in values.get("artifacts", [])],
            reviews=[Review.model_validate(item) for item in values.get("reviews", [])],
            decision=(
                Decision.model_validate(values["decision"]) if values.get("decision") else None
            ),
            errors=[RunError.model_validate(item) for item in values.get("errors", [])],
            repair_rounds=values.get("repair_round", 0),
            usage=usage,
            model_calls=model_calls,
            tool_calls=[
                ToolCallRecord.model_validate(item) for item in values.get("tool_calls", [])
            ],
        )

    async def list(self, *, limit: int = 50, offset: int = 0) -> list[RunRecord]:
        return await self.runtime.run_store.list(limit=limit, offset=offset)

    async def resume(self, run_id: str, *, approved: bool, note: str | None = None) -> None:
        result = await self.get(run_id)
        if result.status != RunStatus.WAITING_HUMAN:
            raise ValueError(f"run is not waiting for human input: {result.status}")
        await self.runtime.run_store.update_status(run_id, RunStatus.RUNNING)
        self._spawn_execute(
            run_id,
            Command(resume={"approved": approved, "note": note}),
            suffix="resume",
        )

    async def cancel(self, run_id: str) -> None:
        await self.get(run_id)
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.runtime.graph.aupdate_state(
            self._graph_config(run_id),
            {"status": RunStatus.CANCELLED.value},
        )
        await self.runtime.run_store.update_status(run_id, RunStatus.CANCELLED)
