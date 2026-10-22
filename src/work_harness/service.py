from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any
from uuid import uuid4

from langgraph.types import Command

from work_harness import __version__
from work_harness.blackboard.controller import BLACKBOARD_WORKFLOW_REVISION
from work_harness.blackboard.inputs import HumanInput, HumanInputResult, prior_input
from work_harness.blackboard.kernel import BlackboardKernel
from work_harness.blackboard.models import IntentContent
from work_harness.blackboard.protocol import (
    BlackboardError,
    BoardClosedError,
    BoardNotFoundError,
    InputPendingError,
)
from work_harness.blackboard.verification import VerificationRegistry
from work_harness.domain.enums import RunJobKind, RunJobStatus, RunStatus
from work_harness.domain.models import (
    AgentInteraction,
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
    utc_now,
)
from work_harness.observability.logging import get_logger
from work_harness.runtime import HarnessRuntime
from work_harness.storage import (
    RunExecutionVersion,
    RunJob,
    RunQueueCapacityError,
    RunQueueStateError,
    RunRecord,
    WorkerHeartbeat,
)
from work_harness.storage.blackboard_factory import build_activation_store
from work_harness.workflow.blackboard_engine import BlackboardEngine
from work_harness.workflow.contracts import WORKFLOW_REVISION


class RunNotFoundError(KeyError):
    pass


class IdempotencyConflictError(ValueError):
    pass


class RunExecutionMismatchError(ValueError):
    pass


class RunCapacityError(RuntimeError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("run admission queue is full")
        self.retry_after_seconds = retry_after_seconds


class RunManager:
    def __init__(self, runtime: HarnessRuntime) -> None:
        self.runtime = runtime
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._blackboard_engines: dict[str, BlackboardEngine] = {}
        self._run_slots = asyncio.Semaphore(runtime.config.runtime.run_max_concurrency)
        self._admission_lock = asyncio.Lock()
        self._executing: set[str] = set()
        self._subscribers: dict[str, set[asyncio.Queue[None]]] = {}
        self._instance_id = (
            os.getenv("WORK_HARNESS_INSTANCE_ID", "").strip()
            or os.getenv("HOSTNAME", "").strip()
            or uuid4().hex
        )[:80]
        self._owner = (
            f"worker_{self._instance_id}_{uuid4().hex}"
            if runtime.config.runtime.execution.role == "worker"
            else f"manager_{uuid4().hex}"
        )[:120]
        self._recovery_task: asyncio.Task[Any] | None = None
        self._consumer_task: asyncio.Task[Any] | None = None
        self._worker_heartbeat_task: asyncio.Task[Any] | None = None
        self._worker_started_at = utc_now()
        self._dispatcher_wakeup = asyncio.Event()
        self._closing = False
        self._admitted_runs_total = 0
        self._rejected_runs_total = 0
        self._recovered_runs_total = 0
        self._recovery_backlog_observed = 0
        self._logger = get_logger("work_harness.run_manager")

    async def initialize(self) -> None:
        role = self.runtime.config.runtime.execution.role
        if role == "api":
            await self.runtime.run_store.start_event_listener(self._publish)
            return
        if role == "worker":
            await self._heartbeat_worker_once()
            self._consumer_task = asyncio.create_task(
                self._consumer_loop(), name=f"{self._owner}:consumer"
            )
            self._worker_heartbeat_task = asyncio.create_task(
                self._worker_heartbeat_loop(),
                name=f"{self._owner}:worker-heartbeat",
            )
            self._recovery_task = asyncio.create_task(
                self._distributed_recovery_loop(), name=f"{self._owner}:blackboard-recovery"
            )
            return
        if not self.runtime.config.runtime.recovery_enabled:
            return
        await self.recover_incomplete()
        self._recovery_task = asyncio.create_task(
            self._recovery_loop(), name=f"{self._owner}:recovery"
        )

    async def close(self) -> None:
        self._closing = True
        if self.runtime.config.runtime.execution.role == "api":
            with suppress(Exception):
                await self.runtime.run_store.stop_event_listener(self._publish)
        if self._worker_heartbeat_task:
            self._worker_heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker_heartbeat_task
            self._worker_heartbeat_task = None
        if self.runtime.config.runtime.execution.role == "worker":
            with suppress(Exception):
                await self.runtime.run_store.remove_worker(self._owner)
        if self._consumer_task:
            self._consumer_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._consumer_task
            self._consumer_task = None
        if self._recovery_task:
            self._recovery_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._recovery_task
            self._recovery_task = None
        running = [task for task in self._tasks.values() if not task.done()]
        if running:
            if self.runtime.config.runtime.execution.role == "worker":
                _, pending = await asyncio.wait(
                    running,
                    timeout=self.runtime.config.runtime.run_timeout_seconds + 5,
                )
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            else:
                for task in running:
                    task.cancel()
                await asyncio.gather(*running, return_exceptions=True)
        self._tasks.clear()
        self._blackboard_engines.clear()

    async def _distributed_recovery_loop(self) -> None:
        from work_harness.workflow.blackboard_host import reconcile_distributed_runs

        interval = self.runtime.config.runtime.recovery_scan_interval_seconds
        while not self._closing:
            try:
                await reconcile_distributed_runs(self)
            except Exception:
                self._logger.exception("blackboard_reconciliation_scan_failed")
            await asyncio.sleep(interval)

    async def _recovery_loop(self) -> None:
        interval = self.runtime.config.runtime.recovery_scan_interval_seconds
        while not self._closing:
            try:
                await asyncio.wait_for(self._dispatcher_wakeup.wait(), timeout=interval)
            except TimeoutError:
                pass
            self._dispatcher_wakeup.clear()
            try:
                await self.recover_incomplete()
            except Exception:
                self._logger.exception("recovery_scan_failed")

    async def recover_incomplete(self) -> list[str]:
        recovered = await self._recover_blackboard_controls()
        records = await self.runtime.run_store.list_recoverable(
            limit=self.runtime.config.runtime.recovery_scan_limit
        )
        self._recovery_backlog_observed = 0
        for index, record in enumerate(records):
            async with self._admission_lock:
                if self._at_capacity():
                    self._recovery_backlog_observed = len(records) - index
                    break
                if record.run_id in self._tasks:
                    continue
                claimed = await self.runtime.run_store.claim(
                    record.run_id,
                    owner=self._owner,
                    lease_seconds=self.runtime.config.runtime.run_lease_seconds,
                )
                if claimed is None:
                    continue
                input_value = None
                if record.engine == "blackboard":
                    result = await self._get_blackboard(record)
                    state_status = RunStatus(result.status)
                else:
                    snapshot = await self.runtime.graph.aget_state(
                        self._graph_config(record.run_id)
                    )
                    values = snapshot.values or {}
                    if not values:
                        payload = await self.runtime.run_store.get_initial_input(record.run_id)
                        if payload is None:
                            await self.runtime.run_store.update_status(
                                record.run_id, RunStatus.FAILED
                            )
                            continue
                        input_value = self._initial_state(record.run_id, payload)
                    state_status = RunStatus(values.get("status", RunStatus.RUNNING))
                if state_status not in {RunStatus.PENDING, RunStatus.RUNNING}:
                    await self.runtime.run_store.update_status(record.run_id, state_status)
                    await self.runtime.run_store.release(record.run_id, owner=self._owner)
                    continue
                try:
                    await self._check_execution_version(record)
                except ValueError as exc:
                    self._blackboard_engines.pop(record.run_id, None)
                    await self.runtime.run_store.release(record.run_id, owner=self._owner)
                    self._logger.warning(
                        "run_execution_incompatible",
                        extra={"run_id": record.run_id, "reason": str(exc)},
                    )
                    continue
                self._spawn_execute(
                    record.run_id,
                    input_value,
                    lease_claimed=True,
                    suffix="recover",
                )
                self._recovered_runs_total += 1
                recovered.append(record.run_id)
        return recovered

    def _graph_config(self, run_id: str) -> dict[str, Any]:
        policy = self.runtime.config.runtime
        return {
            "configurable": {"thread_id": run_id},
            "max_concurrency": max(
                self.runtime.config.runtime.worker_max_concurrency,
                self.runtime.config.runtime.reviewer_max_concurrency,
            ),
            "recursion_limit": max(
                policy.max_node_steps * (policy.max_repair_rounds + 2),
                (2 * policy.max_planned_subtasks + 6) * (policy.max_repair_rounds + 1) + 4,
                2 * policy.max_node_steps + 4,
            ),
        }

    async def start(self, request: TaskRequest) -> str:
        run_id = f"run_{uuid4().hex}"
        excluded = {"idempotency_key"}
        if request.engine is None:
            excluded.add("engine")
        if request.blackboard is None:
            excluded.add("blackboard")
        request_payload = request.model_dump(mode="json", exclude=excluded)
        request_fingerprint = self._fingerprint(request_payload)
        idempotency_key_hash = (
            self._fingerprint(request.idempotency_key) if request.idempotency_key else None
        )
        # A retry belongs to the original Run even if the default engine changed.
        if idempotency_key_hash:
            existing = await self.runtime.run_store.get_by_idempotency_key_hash(
                idempotency_key_hash
            )
            if existing is not None:
                if (
                    existing.request_fingerprint
                    and existing.request_fingerprint != request_fingerprint
                ):
                    raise IdempotencyConflictError(
                        "idempotency key was already used with a different request"
                    )
                return existing.run_id
        self.runtime.skill_registry.validate_selections(request.skills)
        engine_name = request.engine or self.runtime.config.runtime.workflow.engine
        blackboard = None
        if engine_name == "blackboard":
            if not self.runtime.config.runtime.workflow.accept_new_blackboard_runs:
                raise ValueError("new blackboard runs are disabled; use the legacy engine")
            blackboard = await self.runtime.blackboard_engine(request)
        elif request.blackboard is not None:
            raise ValueError("blackboard acceptance criteria require the blackboard engine")
        execution = self._execution_version(blackboard)
        if self.runtime.config.runtime.execution.role != "embedded":
            if self.runtime.config.runtime.execution.role == "worker":
                raise RuntimeError("worker execution role cannot submit runs")
            return await self._submit_distributed(
                run_id=run_id,
                request_payload=request_payload,
                request_fingerprint=request_fingerprint,
                idempotency_key_hash=idempotency_key_hash,
                execution=execution,
            )
        async with self._admission_lock:
            if self._at_capacity():
                if idempotency_key_hash:
                    existing = await self.runtime.run_store.get_by_idempotency_key_hash(
                        idempotency_key_hash
                    )
                    if existing is not None:
                        if (
                            existing.request_fingerprint
                            and existing.request_fingerprint != request_fingerprint
                        ):
                            raise IdempotencyConflictError(
                                "idempotency key was already used with a different request"
                            )
                        return existing.run_id
                self._rejected_runs_total += 1
                raise RunCapacityError(self.runtime.config.runtime.run_queue_retry_after_seconds)
            stored = await self.runtime.run_store.reserve(
                RunRecord(
                    run_id=run_id,
                    status=RunStatus.PENDING,
                    idempotency_key_hash=idempotency_key_hash,
                    request_fingerprint=request_fingerprint,
                    execution=execution,
                ),
                initial_input=request_payload,
            )
            if stored.run_id != run_id:
                if stored.request_fingerprint and stored.request_fingerprint != request_fingerprint:
                    raise IdempotencyConflictError(
                        "idempotency key was already used with a different request"
                    )
                return stored.run_id
            initial = self._initial_state(run_id, request_payload)
            if blackboard is not None:
                self._blackboard_engines[run_id] = blackboard
            self._spawn_execute(run_id, initial)
            self._admitted_runs_total += 1
        return run_id

    async def _submit_distributed(
        self,
        *,
        run_id: str,
        request_payload: dict[str, Any],
        request_fingerprint: str,
        idempotency_key_hash: str | None,
        execution: RunExecutionVersion,
    ) -> str:
        runtime = self.runtime.config.runtime
        try:
            submission = await self.runtime.run_store.submit(
                RunRecord(
                    run_id=run_id,
                    status=RunStatus.PENDING,
                    idempotency_key_hash=idempotency_key_hash,
                    request_fingerprint=request_fingerprint,
                    execution=execution,
                ),
                request_payload,
                capacity=runtime.run_queue_capacity,
                max_attempts=runtime.execution.job_max_attempts,
                graph_revision=self.execution_fingerprint,
            )
        except RunQueueCapacityError as exc:
            self._rejected_runs_total += 1
            raise RunCapacityError(runtime.run_queue_retry_after_seconds) from exc
        if not submission.created:
            if (
                submission.record.request_fingerprint
                and submission.record.request_fingerprint != request_fingerprint
            ):
                raise IdempotencyConflictError(
                    "idempotency key was already used with a different request"
                )
            return submission.record.run_id
        self._admitted_runs_total += 1
        await self._notify_distributed_run(submission.record.run_id)
        return submission.record.run_id

    async def _consumer_loop(self) -> None:
        runtime = self.runtime.config.runtime
        poll_interval = runtime.execution.poll_interval_seconds
        poll_jitter = (
            int.from_bytes(hashlib.sha256(self._owner.encode()).digest()[:2], "big") / 65_535
        )
        poll_timeout = poll_interval * (0.9 + 0.2 * poll_jitter)
        while not self._closing:
            claimed_any = False
            available_slots = runtime.run_max_concurrency - self._inflight_count()
            for _ in range(max(0, available_slots)):
                job = await self.runtime.run_store.claim_job(
                    owner=self._owner,
                    lease_seconds=runtime.run_lease_seconds,
                    graph_revision=self.execution_fingerprint,
                )
                if job is None:
                    break
                claimed_any = True
                self._spawn_job(job)
            if claimed_any:
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(
                    self._dispatcher_wakeup.wait(),
                    timeout=poll_timeout,
                )
            except TimeoutError:
                pass
            self._dispatcher_wakeup.clear()

    async def _worker_heartbeat_loop(self) -> None:
        stale_seconds = self.runtime.config.runtime.execution.worker_stale_seconds
        interval = max(1.0, min(10.0, stale_seconds / 3))
        while not self._closing:
            await asyncio.sleep(interval)
            try:
                await self._heartbeat_worker_once()
            except Exception:
                self._logger.exception("worker_registry_heartbeat_failed")

    async def _heartbeat_worker_once(self) -> None:
        runtime = self.runtime.config.runtime
        await self.runtime.run_store.heartbeat_worker(
            WorkerHeartbeat(
                worker_id=self._owner,
                instance_id=self._instance_id,
                capacity=runtime.run_max_concurrency,
                active_jobs=len(self._executing),
                started_at=self._worker_started_at,
                last_seen_at=utc_now(),
                build_version=__version__,
                config_fingerprint=self.execution_fingerprint,
            )
        )

    @property
    def execution_fingerprint(self) -> str:
        return self._execution_version().configuration_fingerprint

    def _execution_version(self, blackboard: BlackboardEngine | None = None) -> RunExecutionVersion:
        payload = self.runtime.config.model_dump(
            # Keep sets intact until _fingerprint canonicalizes them. Pydantic's
            # JSON mode turns a set into a process-order-dependent list, which
            # made otherwise identical API and Worker containers disagree.
            mode="python",
            exclude={"project_root", "api"},
        )
        payload["runtime"]["execution"]["role"] = "distributed"
        # Admission defaults and database placement do not alter a Run's engine.
        # Blackboard execution limits and source choices are in its manifest.
        payload["runtime"].pop("workflow", None)
        payload["application_version"] = __version__
        payload["provider_targets"] = self.runtime.provider_target_fingerprints
        revision = BLACKBOARD_WORKFLOW_REVISION if blackboard is not None else WORKFLOW_REVISION
        payload["workflow_revision"] = revision
        payload["prompts"] = {
            name: {
                "body": self.runtime.prompt_registry.get(name).body,
                "version": self.runtime.prompt_registry.get(name).version,
            }
            for name in ("normalizer", "planner", "worker", "reviewer", "judge", "synthesizer")
        }
        payload["skills"] = {
            name: {
                "description": self.runtime.skill_registry.get(name).description,
                "instructions": self.runtime.skill_registry.get(name).instructions,
                "manifest": self.runtime.skill_registry.get(name).manifest.model_dump(
                    mode="python"
                ),
            }
            for name in sorted(self.runtime.skill_registry.names)
        }
        if blackboard is not None:
            payload["blackboard"] = blackboard.manifest()
        return RunExecutionVersion(
            engine="blackboard" if blackboard is not None else "legacy",
            workflow_revision=revision,
            schema_version=1,
            configuration_fingerprint=self._fingerprint(payload),
        )

    async def _check_execution_version(
        self, record: RunRecord, *, engine: BlackboardEngine | None = None
    ) -> BlackboardEngine | None:
        if record.engine == "blackboard":
            engine = engine or self._blackboard_engines.get(record.run_id)
            if engine is None:
                payload = await self.runtime.run_store.get_initial_input(record.run_id)
                if payload is None:
                    raise RunExecutionMismatchError("blackboard run is missing its accepted input")
                try:
                    engine = await self.runtime.blackboard_engine(
                        TaskRequest.model_validate(payload)
                    )
                except ValueError as exc:
                    raise RunExecutionMismatchError(
                        f"cannot restore blackboard execution: {exc}"
                    ) from exc
                self._blackboard_engines[record.run_id] = engine
        if record.execution is not None and record.execution != self._execution_version(engine):
            raise RunExecutionMismatchError(
                "run execution version is incompatible; restore its original engine, "
                "schema, prompts and execution configuration before resuming"
            )
        return engine

    @staticmethod
    def _initial_state(run_id: str, request_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "raw_input": {
                key: value
                for key, value in request_payload.items()
                if key not in {"engine", "blackboard"}
            },
            "artifacts": [],
            "reviews": [],
            "errors": [],
            "model_calls": [],
            "tool_calls": [],
            "repair_round": 0,
            "status": RunStatus.PENDING.value,
        }

    async def wait_worker(self) -> None:
        if self._consumer_task is None:
            raise RuntimeError("worker consumer is not initialized")
        await self._consumer_task

    def _spawn_job(self, job: RunJob) -> None:
        task = asyncio.create_task(
            self._execute_job(job),
            name=f"{job.run_id}:job:{job.job_id}",
        )
        self._tasks[job.run_id] = task
        task.add_done_callback(
            lambda completed, current_run_id=job.run_id: self._discard_task(
                current_run_id,
                completed,
            )
        )

    def _inflight_count(self) -> int:
        return sum(not task.done() for task in self._tasks.values())

    def _inflight_capacity(self) -> int:
        runtime = self.runtime.config.runtime
        return runtime.run_max_concurrency + runtime.run_queue_capacity

    def _at_capacity(self) -> bool:
        return self._inflight_count() >= self._inflight_capacity()

    def _spawn_execute(
        self,
        run_id: str,
        input_value: Any,
        *,
        lease_claimed: bool = False,
        suffix: str | None = None,
    ) -> None:
        name = f"{run_id}:{suffix}" if suffix else run_id
        task = asyncio.create_task(
            self._execute(run_id, input_value, lease_claimed=lease_claimed),
            name=name,
        )
        self._tasks[run_id] = task
        task.add_done_callback(
            lambda completed, current_run_id=run_id: self._discard_task(
                current_run_id,
                completed,
            )
        )

    def _discard_task(self, run_id: str, task: asyncio.Task[Any]) -> None:
        if self._tasks.get(run_id) is task:
            self._tasks.pop(run_id, None)
            self._blackboard_engines.pop(run_id, None)
        self._publish(run_id)
        self._dispatcher_wakeup.set()

    @staticmethod
    def _fingerprint(value: Any) -> str:
        def canonicalize(item: Any) -> Any:
            if isinstance(item, dict):
                return {str(key): canonicalize(nested) for key, nested in item.items()}
            if isinstance(item, (set, frozenset)):
                normalized = [canonicalize(nested) for nested in item]
                return sorted(
                    normalized,
                    key=lambda nested: json.dumps(
                        nested,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ),
                )
            if isinstance(item, (list, tuple)):
                return [canonicalize(nested) for nested in item]
            return item

        serialized = json.dumps(
            canonicalize(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    async def _execute(self, run_id: str, input_value: Any, *, lease_claimed: bool = False) -> None:
        async with self._run_slots:
            self._executing.add(run_id)
            try:
                try:
                    record = await self.runtime.run_store.get(run_id)
                    if record is None:
                        raise RunNotFoundError(run_id)
                    engine = await self._check_execution_version(record)
                    if engine is not None:
                        await self._execute_blackboard(run_id, engine, lease_claimed=lease_claimed)
                    else:
                        await self._execute_admitted(
                            run_id,
                            input_value,
                            lease_claimed=lease_claimed,
                        )
                except asyncio.CancelledError:
                    raise
                except RunExecutionMismatchError as exc:
                    self._logger.warning(
                        "run_execution_incompatible", extra={"run_id": run_id, "reason": str(exc)}
                    )
                    if lease_claimed:
                        await self.runtime.run_store.release(run_id, owner=self._owner)
                except Exception as exc:
                    self._logger.exception(
                        "run_infrastructure_failed",
                        extra={"run_id": run_id},
                    )
                    await self._record_infrastructure_failure(run_id, exc)
            finally:
                self._executing.discard(run_id)

    async def _execute_job(self, job: RunJob) -> None:
        if not job.lease_token:
            raise RuntimeError("claimed queue job is missing a lease token")
        record = await self.runtime.run_store.get(job.run_id)
        if record is not None and record.engine == "blackboard":
            from work_harness.workflow.blackboard_worker import execute_blackboard_job

            await execute_blackboard_job(self, job)
            return
        if job.reconciliation_only:
            snapshot = await self.runtime.graph.aget_state(self._graph_config(job.run_id))
            values = snapshot.values or {}
            checkpoint_status = RunStatus(values.get("status", RunStatus.RUNNING))
            if values and checkpoint_status not in {
                RunStatus.PENDING,
                RunStatus.RUNNING,
            }:
                completed = await self.runtime.run_store.complete_job(
                    job.job_id,
                    owner=self._owner,
                    lease_token=job.lease_token,
                    run_status=checkpoint_status,
                )
                if completed:
                    await self._notify_distributed_run(job.run_id)
                return
            await self._retry_distributed_job(
                job,
                RuntimeError("job lease expired after the maximum number of attempts"),
                delay_seconds=0,
            )
            return
        if job.kind == RunJobKind.START:
            snapshot = await self.runtime.graph.aget_state(self._graph_config(job.run_id))
            values = snapshot.values or {}
            if values:
                checkpoint_status = RunStatus(values.get("status", RunStatus.RUNNING))
                if checkpoint_status not in {RunStatus.PENDING, RunStatus.RUNNING}:
                    completed = await self.runtime.run_store.complete_job(
                        job.job_id,
                        owner=self._owner,
                        lease_token=job.lease_token,
                        run_status=checkpoint_status,
                    )
                    if completed:
                        await self._notify_distributed_run(job.run_id)
                    return
                input_value: Any = None
            else:
                input_value = {
                    "run_id": job.run_id,
                    "raw_input": job.payload,
                    "artifacts": [],
                    "reviews": [],
                    "errors": [],
                    "model_calls": [],
                    "tool_calls": [],
                    "repair_round": 0,
                    "status": RunStatus.PENDING.value,
                }
        elif job.kind == RunJobKind.RESUME:
            input_value = Command(resume=job.payload)
        else:  # pragma: no cover - RunJob validation prevents unknown kinds.
            raise ValueError(f"unsupported run job kind: {job.kind}")
        async with self._run_slots:
            self._executing.add(job.run_id)
            try:
                await self._execute_admitted(
                    job.run_id,
                    input_value,
                    job=job,
                )
            except asyncio.CancelledError:
                await self._handle_cancelled_job(job)
            except Exception as exc:
                self._logger.exception(
                    "distributed_run_infrastructure_failed",
                    extra={
                        "run_id": job.run_id,
                        "job_id": job.job_id,
                        "worker_id": self._owner,
                        "attempt": job.attempt_count,
                    },
                )
                await self._retry_distributed_job(job, exc)
            finally:
                self._executing.discard(job.run_id)

    async def _handle_cancelled_job(self, job: RunJob) -> None:
        record = await self.runtime.run_store.get(job.run_id)
        if record and record.status == RunStatus.CANCELLED:
            with suppress(Exception):
                snapshot = await self.runtime.graph.aget_state(self._graph_config(job.run_id))
                if snapshot.values:
                    await self.runtime.graph.aupdate_state(
                        self._graph_config(job.run_id),
                        {"status": RunStatus.CANCELLED.value},
                    )
            return
        await self._retry_distributed_job(
            job,
            RuntimeError("worker stopped before the job completed"),
            delay_seconds=0,
        )

    async def _retry_distributed_job(
        self,
        job: RunJob,
        exc: Exception,
        *,
        delay_seconds: float | None = None,
    ) -> None:
        if not job.lease_token:
            return
        if delay_seconds is None:
            execution = self.runtime.config.runtime.execution
            exponential = execution.retry_base_seconds * (2 ** max(0, job.attempt_count - 1))
            base_delay = min(execution.retry_max_seconds, exponential)
            digest = hashlib.sha256(f"{job.job_id}:{job.attempt_count}".encode()).digest()
            jitter_ratio = int.from_bytes(digest[:2], "big") / 65_535
            jitter_room = max(0.0, execution.retry_max_seconds - base_delay)
            delay_seconds = base_delay + min(
                jitter_room,
                execution.retry_base_seconds * 0.2 * jitter_ratio,
            )
        updated = await self.runtime.run_store.retry_job(
            job.job_id,
            owner=self._owner,
            lease_token=job.lease_token,
            error_code="run_infrastructure_failed",
            error_message=f"{type(exc).__name__}: {exc}",
            delay_seconds=delay_seconds,
        )
        if updated:
            await self._notify_distributed_run(job.run_id)
        if updated and updated.status == RunJobStatus.DEAD:
            await self._record_dead_letter_failure(job.run_id, exc)

    async def _record_dead_letter_failure(
        self,
        run_id: str,
        exc: Exception,
    ) -> None:
        """Expose the failure without making a resumable checkpoint terminal."""
        record = await self.runtime.run_store.get(run_id)
        if record is not None and record.engine == "blackboard":
            return
        error = RunError(
            code="queue_attempts_exhausted",
            message=f"{type(exc).__name__}: {exc}",
            retryable=True,
        )
        with suppress(Exception):
            snapshot = await self.runtime.graph.aget_state(self._graph_config(run_id))
            if snapshot.values:
                await self.runtime.graph.aupdate_state(
                    self._graph_config(run_id),
                    {"errors": [error.model_dump(mode="json")]},
                )

    async def _record_infrastructure_failure(
        self,
        run_id: str,
        exc: Exception,
    ) -> None:
        record = await self.runtime.run_store.get(run_id)
        if record is not None and record.engine == "blackboard":
            await self.runtime.run_store.update_status(run_id, RunStatus.FAILED)
            self._publish(run_id)
            return
        error = RunError(
            code="run_infrastructure_failed",
            message=f"{type(exc).__name__}: {exc}",
            retryable=True,
        )
        with suppress(Exception):
            snapshot = await self.runtime.graph.aget_state(self._graph_config(run_id))
            if snapshot.values:
                await self.runtime.graph.aupdate_state(
                    self._graph_config(run_id),
                    {
                        "status": RunStatus.FAILED.value,
                        "errors": [error.model_dump(mode="json")],
                    },
                )
        with suppress(Exception):
            await self.runtime.run_store.update_status(run_id, RunStatus.FAILED)
        self._publish(run_id)

    async def _execute_admitted(
        self,
        run_id: str,
        input_value: Any,
        *,
        lease_claimed: bool = False,
        job: RunJob | None = None,
    ) -> None:
        lease_held = lease_claimed or job is not None
        if not lease_claimed and job is None:
            claimed = await self.runtime.run_store.claim(
                run_id,
                owner=self._owner,
                lease_seconds=self.runtime.config.runtime.run_lease_seconds,
            )
            if claimed is None:
                return
            lease_held = True
        try:
            await self.runtime.run_store.update_status(run_id, RunStatus.RUNNING)
            self._publish(run_id)

            invocation = asyncio.create_task(
                self._invoke_graph(run_id, input_value),
                name=f"{run_id}:graph",
            )
            heartbeat = asyncio.create_task(
                (
                    self._heartbeat_job(job, invocation)
                    if job is not None
                    else self._heartbeat(run_id, invocation)
                ),
                name=f"{run_id}:heartbeat",
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
                self._publish(run_id)
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
                self._publish(run_id)
            finally:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
            final_status = await self._sync_persisted_status(
                run_id,
                persist=job is None,
            )
            if job is not None and job.lease_token:
                completed = await self.runtime.run_store.complete_job(
                    job.job_id,
                    owner=self._owner,
                    lease_token=job.lease_token,
                    run_status=final_status,
                )
                if not completed:
                    self._logger.warning(
                        "job_completion_lease_lost",
                        extra={
                            "run_id": run_id,
                            "job_id": job.job_id,
                            "worker_id": self._owner,
                            "attempt": job.attempt_count,
                        },
                    )
                else:
                    await self._notify_distributed_run(run_id)
        finally:
            if lease_held and job is None:
                await self.runtime.run_store.release(run_id, owner=self._owner)
            await self.runtime.tool_gateway.clear_run(run_id)
            self._publish(run_id)

    async def _execute_blackboard(
        self, run_id: str, engine: BlackboardEngine, *, lease_claimed: bool
    ) -> None:
        if not lease_claimed:
            claimed = await self.runtime.run_store.claim(
                run_id,
                owner=self._owner,
                lease_seconds=self.runtime.config.runtime.run_lease_seconds,
            )
            if claimed is None:
                return
        heartbeat = None
        invocation = None
        try:
            await self.runtime.run_store.update_status(run_id, RunStatus.RUNNING)
            self._publish(run_id)
            invocation = asyncio.create_task(engine.run(run_id), name=f"{run_id}:blackboard")
            heartbeat = asyncio.create_task(self._heartbeat(run_id, invocation))
            try:
                while True:
                    result = await asyncio.wait_for(
                        invocation, timeout=engine.limits.timeout_seconds + 2
                    )
                    control = await engine.kernel.store.control(run_id)
                    if control.finalization is not None:
                        status = RunStatus(control.finalization.status)
                        break
                    observed = result.get("observed_revision", 0)
                    if control.wakeup_revision > observed:
                        invocation = asyncio.create_task(
                            engine.runner.run(run_id, engine.graph_config(run_id)),
                            name=f"{run_id}:blackboard-input",
                        )
                        # Each continuation needs a heartbeat bound to its own invocation.
                        heartbeat.cancel()
                        with suppress(asyncio.CancelledError):
                            await heartbeat
                        heartbeat = asyncio.create_task(self._heartbeat(run_id, invocation))
                        continue
                    status = RunStatus(result["status"])
                    if status == RunStatus.WAITING_HUMAN:
                        await engine.kernel.store.acknowledge_wakeup(
                            run_id,
                            observed,
                            writer=await engine.controller.execution.writer(run_id),
                        )
                    break
            except asyncio.CancelledError:
                invocation.cancel()
                raise
            except Exception as exc:
                self._logger.exception("blackboard_run_failed", extra={"run_id": run_id})
                try:
                    final = await engine.kernel.finalize(
                        run_id,
                        status="failed",
                        reason=f"{type(exc).__name__}: {exc}"[:500],
                    )
                    status = RunStatus(final.status)
                except BoardNotFoundError:
                    status = RunStatus.FAILED
                except InputPendingError:
                    status = RunStatus.PENDING
            await self.runtime.run_store.update_status(run_id, status)
            with suppress(BoardNotFoundError):
                control = await engine.kernel.store.control(run_id)
                if control.finalization is not None:
                    await engine.kernel.store.acknowledge_finalization(
                        run_id,
                        control.finalization.finalized_at_revision,
                    )
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
            if invocation is not None and not invocation.done():
                invocation.cancel()
                with suppress(asyncio.CancelledError):
                    await invocation
            await self.runtime.run_store.release(run_id, owner=self._owner)
            await self.runtime.tool_gateway.clear_run(run_id)
            self._publish(run_id)

    async def _check_workflow_revision(self, run_id: str) -> None:
        record = await self.runtime.run_store.get(run_id)
        if record is not None:
            await self._check_execution_version(record)
        snapshot = await self.runtime.graph.aget_state(self._graph_config(run_id))
        values = snapshot.values or {}
        if values.get("plan") and values.get("workflow_revision") != WORKFLOW_REVISION:
            raise ValueError(
                "checkpoint workflow revision is incompatible; finish the run with "
                "its original application version or submit a new run"
            )

    async def _invoke_graph(self, run_id: str, input_value: Any) -> None:
        await self._check_workflow_revision(run_id)
        stream = getattr(self.runtime.graph, "astream", None)
        if stream is None:
            await self.runtime.graph.ainvoke(input_value, self._graph_config(run_id))
            self._publish(run_id)
            await self._notify_distributed_run(run_id)
            return
        async for _ in stream(
            input_value,
            self._graph_config(run_id),
            stream_mode="updates",
        ):
            self._publish(run_id)
            await self._notify_distributed_run(run_id)

    async def _notify_distributed_run(self, run_id: str) -> None:
        if self.runtime.config.runtime.execution.role == "embedded":
            return
        try:
            await self.runtime.run_store.notify_run(run_id)
        except Exception:
            self._logger.exception(
                "run_event_notification_failed",
                extra={"run_id": run_id},
            )

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

    async def _heartbeat_job(
        self,
        job: RunJob,
        invocation: asyncio.Task[Any],
    ) -> None:
        if not job.lease_token:
            invocation.cancel()
            return
        runtime = self.runtime.config.runtime
        while not invocation.done():
            await asyncio.sleep(runtime.heartbeat_interval_seconds)
            try:
                active = await self.runtime.run_store.heartbeat_job(
                    job.job_id,
                    owner=self._owner,
                    lease_token=job.lease_token,
                    lease_seconds=runtime.run_lease_seconds,
                )
            except Exception:
                invocation.cancel()
                raise
            if not active:
                invocation.cancel()
                return

    async def _sync_persisted_status(
        self,
        run_id: str,
        *,
        persist: bool = True,
    ) -> RunStatus:
        snapshot = await self.runtime.graph.aget_state(self._graph_config(run_id))
        values = snapshot.values or {}
        status = RunStatus(values.get("status", RunStatus.FAILED))
        if persist and values.get("status"):
            await self.runtime.run_store.update_status(run_id, status)
        return status

    async def wait(self, run_id: str) -> RunResult:
        task = self._tasks.get(run_id)
        if task:
            await task
            self._tasks.pop(run_id, None)
        return await self.get(run_id)

    async def _get_blackboard(self, record: RunRecord) -> RunResult:
        store = await self.runtime.blackboard_store()
        try:
            board = await store.snapshot(record.run_id)
        except BoardNotFoundError:
            return RunResult(run_id=record.run_id, engine="blackboard", status=record.status)
        checkpoint = await self.runtime.checkpointer.aget_tuple(
            {"configurable": {"thread_id": record.run_id}}
        )
        values = checkpoint.checkpoint.get("channel_values", {}) if checkpoint else {}
        task = self._tasks.get(record.run_id)
        active = task is not None and not task.done()
        final = board.finalization
        control = await store.control(record.run_id)
        distributed = self.runtime.config.runtime.execution.role != "embedded"
        status = (
            final.status
            if final is not None
            else record.status
            if distributed
            else RunStatus.PENDING
            if control.wakeup_revision and not active
            else record.status
            if active or record.status in {RunStatus.FAILED, RunStatus.CANCELLED}
            else values.get("status", record.status)
        )
        if final is not None and record.status != final.status and not distributed:
            await self.runtime.run_store.update_status(record.run_id, RunStatus(final.status))
        if final is not None and control.status_pending and not distributed:
            await store.acknowledge_finalization(record.run_id, final.finalized_at_revision)
        ledger = build_activation_store(store)
        ledger.bind(store)
        usage_summary = await ledger.summary(record.run_id)
        root = board.by_id()[board.root_intent_id]
        payload = await self.runtime.run_store.get_initial_input(record.run_id)
        task_spec = None
        if payload is not None:
            task_spec = TaskSpec.model_validate(
                {
                    **{
                        key: value
                        for key, value in payload.items()
                        if key not in {"engine", "blackboard"}
                    },
                    "task_id": root.id,
                    "goal": root.objective,
                    "created_at": root.created_at,
                }
            )
        return RunResult(
            run_id=record.run_id,
            engine="blackboard",
            pending_intents=values.get("pending_intents", [])
            if status == RunStatus.WAITING_HUMAN
            else [],
            status=status,
            task=task_spec,
            output=final.output if final is not None else values.get("final_output", ""),
            usage=Usage(
                input_tokens=usage_summary.input_tokens,
                output_tokens=usage_summary.output_tokens,
                cached_tokens=usage_summary.cached_tokens,
                usage_complete=usage_summary.usage_complete,
                estimated_cost_usd=usage_summary.cost_usd,
            ),
            errors=[RunError(code="blackboard_failed", message=final.reason)]
            if final is not None and final.status == "failed"
            else [],
        )

    async def get(self, run_id: str) -> RunResult:
        record = await self.runtime.run_store.get(run_id)
        if record is not None and record.engine == "blackboard":
            return await self._get_blackboard(record)
        snapshot = await self.runtime.graph.aget_state(self._graph_config(run_id))
        values = snapshot.values or {}
        if not values:
            if record:
                return RunResult(run_id=run_id, status=record.status)
            raise RunNotFoundError(run_id)
        state_status = RunStatus(values.get("status", RunStatus.RUNNING))
        distributed = self.runtime.config.runtime.execution.role != "embedded"
        task = self._tasks.get(run_id)
        resume_in_progress = (
            not distributed
            and record is not None
            and record.status == RunStatus.RUNNING
            and state_status == RunStatus.WAITING_HUMAN
            and task is not None
            and not task.done()
        )
        # A LangGraph resume starts from the interrupted checkpoint, so the
        # checkpoint can remain WAITING_HUMAN until the resumed invocation gets
        # its first turn.  Do not expose that stale state while the local resume
        # task is alive: SSE clients treat WAITING_HUMAN as inactive and would
        # otherwise disconnect before the terminal update arrives.
        status = (
            record.status
            if (distributed and record is not None) or resume_in_progress
            else state_status
        )
        if record is None:
            record = await self.runtime.run_store.reserve(RunRecord(run_id=run_id, status=status))
        elif not distributed and record.status != status:
            await self.runtime.run_store.update_status(run_id, status)
        model_calls = [
            ModelCallRecord.model_validate(item) for item in values.get("model_calls", [])
        ]
        usage = Usage(
            input_tokens=sum(item.input_tokens for item in model_calls),
            output_tokens=sum(item.output_tokens for item in model_calls),
            cached_tokens=sum(item.cached_tokens for item in model_calls),
            usage_complete=all(item.usage_complete for item in model_calls)
            and not any(
                e.get("code") in {"run_failed", "run_timeout"}
                for e in values.get("errors", [])
            ),
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
            agent_interactions=[
                AgentInteraction.model_validate(item)
                for item in values.get("agent_interactions", [])
            ],
        )

    async def list(self, *, limit: int = 50, offset: int = 0) -> list[RunRecord]:
        return await self.runtime.run_store.list(limit=limit, offset=offset)

    @asynccontextmanager
    async def subscribe(self, run_id: str) -> AsyncIterator[asyncio.Queue[None]]:
        queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        subscribers = self._subscribers.setdefault(run_id, set())
        subscribers.add(queue)
        try:
            yield queue
        finally:
            subscribers.discard(queue)
            if not subscribers:
                self._subscribers.pop(run_id, None)

    def _publish(self, run_id: str) -> None:
        for queue in tuple(self._subscribers.get(run_id, ())):
            if queue.empty():
                queue.put_nowait(None)

    def stats(self) -> dict[str, int]:
        running = self._inflight_count()
        capacity = self._inflight_capacity()
        return {
            "active_runs": len(self._executing),
            "queued_runs": max(0, running - len(self._executing)),
            "inflight_runs": running,
            "stream_subscribers": sum(
                len(subscribers) for subscribers in self._subscribers.values()
            ),
            "run_max_concurrency": self.runtime.config.runtime.run_max_concurrency,
            "run_queue_capacity": self.runtime.config.runtime.run_queue_capacity,
            "run_queue_retry_after_seconds": (
                self.runtime.config.runtime.run_queue_retry_after_seconds
            ),
            "inflight_capacity": capacity,
            "available_admission_slots": max(0, capacity - running),
            "admitted_runs_total": self._admitted_runs_total,
            "rejected_runs_total": self._rejected_runs_total,
            "recovered_runs_total": self._recovered_runs_total,
            "recovery_backlog_observed": self._recovery_backlog_observed,
        }

    async def _recover_blackboard_controls(self) -> list[str]:
        store = await self.runtime.existing_blackboard_store()
        if store is None:
            return []
        recovered = []
        for run_id in await store.pending_reconciliations(
            limit=self.runtime.config.runtime.recovery_scan_limit
        ):
            record = await self.runtime.run_store.get(run_id)
            if record is None or record.engine != "blackboard":
                continue
            try:
                if await self.resume_blackboard(run_id) == "scheduled":
                    recovered.append(run_id)
            except ValueError as exc:
                self._logger.warning(
                    "blackboard_reconciliation_deferred",
                    extra={
                        "run_id": run_id,
                        "reason": str(exc),
                    },
                )
        return recovered

    async def _resume_blackboard_locked(self, run_id: str) -> str:
        record = await self.runtime.run_store.get(run_id)
        if record is None:
            raise RunNotFoundError(run_id)
        if record.engine != "blackboard":
            raise BlackboardError("typed blackboard continuation requires a blackboard run")
        store = await self.runtime.blackboard_store()
        control = await store.control(run_id)
        if control.finalization is not None:
            await self.runtime.run_store.update_status(
                run_id, RunStatus(control.finalization.status)
            )
            await store.acknowledge_finalization(run_id, control.finalization.finalized_at_revision)
            return "closed"
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            return "active"
        if not control.wakeup_revision:
            return "settled"
        if self._at_capacity() or self._closing:
            return "pending"
        self._blackboard_engines.pop(run_id, None)
        await self._check_execution_version(record)
        # Do not overwrite an active owner's index merely to acquire a short input lease.
        if (
            record.lease_owner
            and record.lease_owner != self._owner
            and record.lease_expires_at
            and record.lease_expires_at > utc_now()
        ):
            return "pending"
        await self.runtime.run_store.update_status(run_id, RunStatus.PENDING)
        claimed = await self.runtime.run_store.claim(
            run_id,
            owner=self._owner,
            lease_seconds=self.runtime.config.runtime.run_lease_seconds,
        )
        if claimed is None:
            return "pending"
        self._spawn_execute(run_id, None, lease_claimed=True, suffix="input")
        self._publish(run_id)
        return "scheduled"

    async def resume_blackboard(self, run_id: str) -> str:
        if self.runtime.config.runtime.execution.role != "embedded":
            from work_harness.workflow.blackboard_host import resume_distributed_input

            return await resume_distributed_input(self, run_id)
        async with self._admission_lock:
            return await self._resume_blackboard_locked(run_id)

    async def submit_blackboard_input(self, run_id: str, request: HumanInput) -> HumanInputResult:
        request = HumanInput.model_validate(request.model_dump())
        if self.runtime.config.runtime.execution.role != "embedded":
            from work_harness.workflow.blackboard_host import submit_distributed_input

            return await submit_distributed_input(self, run_id, request)
        async with self._admission_lock:
            record = await self.runtime.run_store.get(run_id)
            if record is None:
                raise RunNotFoundError(run_id)
            if record.engine != "blackboard":
                raise BlackboardError("typed input requires a blackboard run")
            store = await self.runtime.blackboard_store()
            # An accepted retry remains readable after finalization or a configuration change.
            prior = await prior_input(
                BlackboardKernel(store, VerificationRegistry()), run_id, request
            )
            if prior is not None:
                try:
                    wakeup = await self._resume_blackboard_locked(run_id)
                except ValueError:
                    wakeup = "pending"
                return HumanInputResult(receipt=prior, wakeup=wakeup)
            control = await store.control(run_id)
            if control.finalization is not None:
                raise BoardClosedError("new input requires a follow-up run after finalization")
            if await store.receipt(run_id, "initial-user-input-v1") is None:
                raise BlackboardError("blackboard initialization must complete before human input")
            task = self._tasks.get(run_id)
            active = task is not None and not task.done()
            if not active:
                self._blackboard_engines.pop(run_id, None)
            engine = await self._check_execution_version(record)
            assert engine is not None
            if active:
                if run_id not in self._executing or record.lease_owner != self._owner:
                    raise BlackboardError("run is starting; retry input after initialization")
                receipt = await engine.controller.submit_input(run_id, request)
                wakeup = "active"
            else:
                if (
                    record.lease_owner
                    and record.lease_owner != self._owner
                    and record.lease_expires_at
                    and record.lease_expires_at > utc_now()
                ):
                    raise BlackboardError("another owner is executing this run")
                await self.runtime.run_store.update_status(run_id, RunStatus.PENDING)
                claimed = await self.runtime.run_store.claim(
                    run_id,
                    owner=self._owner,
                    lease_seconds=self.runtime.config.runtime.run_lease_seconds,
                )
                if claimed is None:
                    raise BlackboardError("could not acquire the input lease")
                input_writer = None
                try:
                    input_writer = await engine.kernel.writer(run_id, takeover=True)
                    receipt = await engine.controller.submit_input(run_id, request)
                except BaseException:
                    latest = await store.control(run_id)
                    if latest.finalization is not None:
                        await self.runtime.run_store.update_status(
                            run_id, RunStatus(latest.finalization.status)
                        )
                    elif not latest.wakeup_revision:
                        await self.runtime.run_store.update_status(run_id, RunStatus(record.status))
                    raise
                finally:
                    try:
                        if input_writer is not None:
                            await store.release_writer(input_writer)
                    finally:
                        await self.runtime.run_store.release(run_id, owner=self._owner)
                        self._blackboard_engines.pop(run_id, None)
                wakeup = await self._resume_blackboard_locked(run_id)
            self._dispatcher_wakeup.set()
            self._publish(run_id)
            return HumanInputResult(receipt=receipt, wakeup=wakeup)

    async def resume(self, run_id: str, *, approved: bool, note: str | None = None) -> None:
        result = await self.get(run_id)
        if result.engine == "blackboard":
            raise ValueError(
                "blackboard continuation requires typed input; "
                "approval-only resume is not supported"
            )
        if result.status != RunStatus.WAITING_HUMAN:
            raise ValueError(f"run is not waiting for human input: {result.status}")
        await self._check_workflow_revision(run_id)
        if self.runtime.config.runtime.execution.role != "embedded":
            if self.runtime.config.runtime.execution.role == "worker":
                raise RuntimeError("worker execution role cannot resume runs")
            runtime = self.runtime.config.runtime
            try:
                await self.runtime.run_store.enqueue_resume(
                    run_id,
                    {"approved": approved, "note": note},
                    capacity=runtime.run_queue_capacity,
                    max_attempts=runtime.execution.job_max_attempts,
                    graph_revision=self.execution_fingerprint,
                )
            except RunQueueCapacityError as exc:
                self._rejected_runs_total += 1
                raise RunCapacityError(runtime.run_queue_retry_after_seconds) from exc
            except RunQueueStateError as exc:
                raise ValueError(str(exc)) from exc
            await self._notify_distributed_run(run_id)
            return
        async with self._admission_lock:
            if run_id in self._tasks:
                raise ValueError("run is already resuming")
            if self._at_capacity():
                self._rejected_runs_total += 1
                raise RunCapacityError(self.runtime.config.runtime.run_queue_retry_after_seconds)
            await self.runtime.run_store.update_status(run_id, RunStatus.RUNNING)
            self._publish(run_id)
            self._spawn_execute(
                run_id,
                Command(resume={"approved": approved, "note": note}),
                suffix="resume",
            )

    async def cancel(self, run_id: str) -> None:
        result = await self.get(run_id)
        if result.engine != "blackboard" and result.status in {
            RunStatus.SUCCEEDED,
            RunStatus.PARTIAL,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            return
        if result.engine == "blackboard":
            if self.runtime.config.runtime.execution.role != "embedded":
                from work_harness.workflow.blackboard_host import cancel_distributed_run

                await cancel_distributed_run(self, run_id)
                return
            store = await self.runtime.blackboard_store()
            kernel = BlackboardKernel(store, VerificationRegistry())
            try:
                await store.snapshot(run_id)
            except BoardNotFoundError:
                payload = await self.runtime.run_store.get_initial_input(run_id)
                request = TaskRequest.model_validate(payload)
                if request.blackboard is None:
                    raise ValueError("blackboard run is missing its accepted goal") from None
                try:
                    await kernel.create(
                        run_id,
                        IntentContent(
                            objective=request.goal,
                            topic=request.blackboard.topic,
                            acceptance=request.blackboard.acceptance,
                        ),
                        source_id="user",
                    )
                except BlackboardError:
                    # Initialization may have won the race. A failed creation is
                    # safe to ignore only if a board actually exists now.
                    await store.snapshot(run_id)
            final = await kernel.cancel(run_id)
            task = self._tasks.get(run_id)
            if task and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await self.runtime.run_store.update_status(run_id, RunStatus(final.status))
            await store.acknowledge_finalization(run_id, final.finalized_at_revision)
            await self.runtime.tool_gateway.clear_run(run_id)
            self._publish(run_id)
            return
        if self.runtime.config.runtime.execution.role != "embedded":
            await self.runtime.run_store.cancel_jobs(run_id)
            self._publish(run_id)
            await self._notify_distributed_run(run_id)
            return
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        snapshot = await self.runtime.graph.aget_state(self._graph_config(run_id))
        if snapshot.values:
            await self.runtime.graph.aupdate_state(
                self._graph_config(run_id),
                {"status": RunStatus.CANCELLED.value},
            )
        await self.runtime.run_store.update_status(run_id, RunStatus.CANCELLED)
        await self.runtime.tool_gateway.clear_run(run_id)
        self._publish(run_id)
