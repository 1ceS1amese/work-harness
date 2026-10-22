from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from work_harness.domain.enums import RunJobKind, RunJobStatus, RunStatus
from work_harness.domain.models import utc_now
from work_harness.storage.base import RunRecord, RunStoreNotFoundError
from work_harness.storage.queue import (
    QUEUE_RECONCILE_ERROR_CODE,
    RunJob,
    RunQueueCapacityError,
    RunQueueStateError,
    RunQueueStats,
    RunSubmission,
    WorkerHeartbeat,
)


class MemoryRunStore:
    backend = "memory"

    def __init__(self) -> None:
        self._records: dict[str, RunRecord] = {}
        self._inputs: dict[str, dict[str, Any]] = {}
        self._idempotency: dict[str, str] = {}
        self._jobs: dict[UUID, RunJob] = {}
        self._workers: dict[str, WorkerHeartbeat] = {}
        self._event_callbacks: set[Callable[[str], None]] = set()
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def reserve(
        self, record: RunRecord, *, initial_input: dict[str, Any] | None = None
    ) -> RunRecord:
        async with self._lock:
            if record.idempotency_key_hash:
                existing_id = self._idempotency.get(record.idempotency_key_hash)
                if existing_id:
                    return self._records[existing_id]
            existing = self._records.get(record.run_id)
            if existing:
                return existing
            saved_input = deepcopy(initial_input)
            self._records[record.run_id] = record
            if saved_input is not None:
                self._inputs[record.run_id] = saved_input
            if record.idempotency_key_hash:
                self._idempotency[record.idempotency_key_hash] = record.run_id
            return record

    async def get_initial_input(self, run_id: str) -> dict[str, Any] | None:
        async with self._lock:
            return deepcopy(self._inputs.get(run_id))

    async def get(self, run_id: str) -> RunRecord | None:
        async with self._lock:
            return self._records.get(run_id)

    async def get_by_idempotency_key_hash(self, idempotency_key_hash: str) -> RunRecord | None:
        async with self._lock:
            run_id = self._idempotency.get(idempotency_key_hash)
            return self._records.get(run_id) if run_id else None

    async def update_status(self, run_id: str, status: RunStatus) -> RunRecord:
        async with self._lock:
            record = self._records.get(run_id)
            if record is None:
                raise RunStoreNotFoundError(run_id)
            update = {"status": status, "updated_at": utc_now()}
            if status not in {RunStatus.PENDING, RunStatus.RUNNING}:
                update.update({"lease_owner": None, "lease_expires_at": None})
            updated = record.model_copy(update=update)
            self._records[run_id] = updated
            return updated

    async def list(self, *, limit: int = 50, offset: int = 0) -> list[RunRecord]:
        async with self._lock:
            ordered = sorted(
                self._records.values(),
                key=lambda item: (item.created_at, item.run_id),
                reverse=True,
            )
            return ordered[offset : offset + limit]

    async def claim(self, run_id: str, *, owner: str, lease_seconds: float) -> RunRecord | None:
        async with self._lock:
            record = self._records.get(run_id)
            now = utc_now()
            if record is None or record.status not in {RunStatus.PENDING, RunStatus.RUNNING}:
                return None
            if (
                record.lease_owner
                and record.lease_owner != owner
                and record.lease_expires_at
                and record.lease_expires_at > now
            ):
                return None
            updated = record.model_copy(
                update={
                    "lease_owner": owner,
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                    "attempt_count": record.attempt_count + 1,
                    "updated_at": now,
                }
            )
            self._records[run_id] = updated
            return updated

    async def heartbeat(self, run_id: str, *, owner: str, lease_seconds: float) -> bool:
        async with self._lock:
            record = self._records.get(run_id)
            if (
                record is None
                or record.lease_owner != owner
                or record.status not in {RunStatus.PENDING, RunStatus.RUNNING}
            ):
                return False
            now = utc_now()
            self._records[run_id] = record.model_copy(
                update={
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                    "updated_at": now,
                }
            )
            return True

    async def release(self, run_id: str, *, owner: str) -> None:
        async with self._lock:
            record = self._records.get(run_id)
            if record is None or record.lease_owner != owner:
                return
            self._records[run_id] = record.model_copy(
                update={"lease_owner": None, "lease_expires_at": None, "updated_at": utc_now()}
            )

    async def list_recoverable(self, *, limit: int = 200) -> list[RunRecord]:
        async with self._lock:
            now = utc_now()
            records = [
                record
                for record in self._records.values()
                if record.status in {RunStatus.PENDING, RunStatus.RUNNING}
                and (record.lease_expires_at is None or record.lease_expires_at <= now)
            ]
            return sorted(
                records,
                key=lambda item: (item.updated_at, item.run_id),
            )[:limit]

    async def submit(
        self,
        record: RunRecord,
        payload: dict[str, Any],
        *,
        capacity: int,
        max_attempts: int,
        graph_revision: str = "0" * 64,
    ) -> RunSubmission:
        async with self._lock:
            if record.idempotency_key_hash:
                existing_id = self._idempotency.get(record.idempotency_key_hash)
                if existing_id:
                    return RunSubmission(
                        record=self._records[existing_id],
                        created=False,
                    )
            existing = self._records.get(record.run_id)
            if existing:
                return RunSubmission(record=existing, created=False)
            self._ensure_queue_capacity(capacity)
            job = RunJob(
                run_id=record.run_id,
                kind=RunJobKind.START,
                payload=payload,
                max_attempts=max_attempts,
                graph_revision=graph_revision,
            )
            saved_input = deepcopy(payload)
            self._records[record.run_id] = record
            self._inputs[record.run_id] = saved_input
            self._jobs[job.job_id] = job
            if record.idempotency_key_hash:
                self._idempotency[record.idempotency_key_hash] = record.run_id
            return RunSubmission(record=record, job=job, created=True)

    async def enqueue_resume(
        self,
        run_id: str,
        payload: dict[str, Any],
        *,
        capacity: int,
        max_attempts: int,
        graph_revision: str = "0" * 64,
    ) -> RunJob:
        async with self._lock:
            record = self._records.get(run_id)
            if record is None:
                raise RunStoreNotFoundError(run_id)
            if any(
                item.run_id == run_id
                and item.status in {RunJobStatus.AVAILABLE, RunJobStatus.LEASED}
                for item in self._jobs.values()
            ):
                raise RunQueueStateError("run already has an active job")
            if record.status != RunStatus.WAITING_HUMAN:
                raise RunQueueStateError(f"run is not waiting for human input: {record.status}")
            self._ensure_queue_capacity(capacity)
            job = RunJob(
                run_id=run_id,
                kind=RunJobKind.RESUME,
                payload=payload,
                max_attempts=max_attempts,
                graph_revision=graph_revision,
            )
            self._jobs[job.job_id] = job
            now = utc_now()
            self._records[run_id] = record.model_copy(
                update={"status": RunStatus.PENDING, "updated_at": now}
            )
            return job

    def _ensure_queue_capacity(self, capacity: int) -> None:
        available = sum(item.status == RunJobStatus.AVAILABLE for item in self._jobs.values())
        if available >= capacity:
            raise RunQueueCapacityError("run queue is full")

    async def claim_job(
        self,
        *,
        owner: str,
        lease_seconds: float,
        graph_revision: str = "0" * 64,
    ) -> RunJob | None:
        async with self._lock:
            now = utc_now()
            eligible = [
                job
                for job in self._jobs.values()
                if job.graph_revision == graph_revision
                if (
                    job.status == RunJobStatus.AVAILABLE
                    and job.available_at <= now
                    and job.attempt_count < job.max_attempts
                )
                or (
                    job.status == RunJobStatus.LEASED
                    and job.lease_expires_at is not None
                    and job.lease_expires_at <= now
                )
            ]
            if not eligible:
                return None
            selected = min(
                eligible,
                key=lambda item: (-item.priority, item.available_at, item.created_at),
            )
            reconciliation_only = (
                selected.status == RunJobStatus.LEASED
                and selected.attempt_count >= selected.max_attempts
            )
            claimed = selected.model_copy(
                update={
                    "status": RunJobStatus.LEASED,
                    "lease_owner": owner,
                    "lease_token": uuid4(),
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                    "attempt_count": (
                        selected.attempt_count
                        if reconciliation_only
                        else selected.attempt_count + 1
                    ),
                    "last_error_code": (
                        QUEUE_RECONCILE_ERROR_CODE
                        if reconciliation_only
                        else selected.last_error_code
                    ),
                    "last_error_message": (
                        "Final lease is limited to checkpoint reconciliation."
                        if reconciliation_only
                        else selected.last_error_message
                    ),
                    "updated_at": now,
                }
            )
            self._jobs[selected.job_id] = claimed
            record = self._records[selected.run_id]
            self._records[selected.run_id] = record.model_copy(
                update={"status": RunStatus.RUNNING, "updated_at": now}
            )
            return claimed

    async def heartbeat_job(
        self,
        job_id: UUID,
        *,
        owner: str,
        lease_token: UUID,
        lease_seconds: float,
    ) -> bool:
        async with self._lock:
            job = self._jobs.get(job_id)
            record = self._records.get(job.run_id) if job else None
            if (
                job is None
                or record is None
                or record.status == RunStatus.CANCELLED
                or job.status != RunJobStatus.LEASED
                or job.lease_owner != owner
                or job.lease_token != lease_token
            ):
                return False
            now = utc_now()
            self._jobs[job_id] = job.model_copy(
                update={
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                    "updated_at": now,
                }
            )
            return True

    async def complete_job(
        self,
        job_id: UUID,
        *,
        owner: str,
        lease_token: UUID,
        run_status: RunStatus,
    ) -> bool:
        if run_status in {RunStatus.PENDING, RunStatus.RUNNING}:
            raise RunQueueStateError(
                f"cannot complete a queue job with non-terminal status: {run_status}"
            )
        async with self._lock:
            job = self._jobs.get(job_id)
            if not self._owns_job(job, owner, lease_token):
                return False
            now = utc_now()
            self._jobs[job_id] = job.model_copy(
                update={
                    "status": RunJobStatus.COMPLETED,
                    "lease_owner": None,
                    "lease_token": None,
                    "lease_expires_at": None,
                    "updated_at": now,
                    "completed_at": now,
                }
            )
            record = self._records[job.run_id]
            self._records[job.run_id] = record.model_copy(
                update={"status": run_status, "updated_at": now}
            )
            return True

    async def retry_job(
        self,
        job_id: UUID,
        *,
        owner: str,
        lease_token: UUID,
        error_code: str,
        error_message: str,
        delay_seconds: float,
    ) -> RunJob | None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not self._owns_job(job, owner, lease_token):
                return None
            now = utc_now()
            dead = job.attempt_count >= job.max_attempts
            updated = job.model_copy(
                update={
                    "status": (RunJobStatus.DEAD if dead else RunJobStatus.AVAILABLE),
                    "available_at": now + timedelta(seconds=max(0, delay_seconds)),
                    "lease_owner": None,
                    "lease_token": None,
                    "lease_expires_at": None,
                    "last_error_code": error_code[:120],
                    "last_error_message": error_message[:4_000],
                    "updated_at": now,
                    "completed_at": now if dead else None,
                }
            )
            self._jobs[job_id] = updated
            record = self._records[job.run_id]
            self._records[job.run_id] = record.model_copy(
                update={
                    "status": RunStatus.FAILED if dead else RunStatus.PENDING,
                    "updated_at": now,
                }
            )
            return updated

    @staticmethod
    def _owns_job(job: RunJob | None, owner: str, lease_token: UUID) -> bool:
        return bool(
            job
            and job.status == RunJobStatus.LEASED
            and job.lease_owner == owner
            and job.lease_token == lease_token
        )

    async def cancel_jobs(self, run_id: str) -> int:
        async with self._lock:
            record = self._records.get(run_id)
            if record is None:
                raise RunStoreNotFoundError(run_id)
            if record.status in {
                RunStatus.SUCCEEDED,
                RunStatus.PARTIAL,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }:
                return 0
            now = utc_now()
            cancelled = 0
            for job_id, job in tuple(self._jobs.items()):
                if job.run_id != run_id or job.status not in {
                    RunJobStatus.AVAILABLE,
                    RunJobStatus.LEASED,
                }:
                    continue
                self._jobs[job_id] = job.model_copy(
                    update={
                        "status": RunJobStatus.CANCELLED,
                        "lease_owner": None,
                        "lease_token": None,
                        "lease_expires_at": None,
                        "updated_at": now,
                        "completed_at": now,
                    }
                )
                cancelled += 1
            self._records[run_id] = record.model_copy(
                update={"status": RunStatus.CANCELLED, "updated_at": now}
            )
            return cancelled

    async def queue_stats(
        self,
        *,
        graph_revision: str | None = None,
    ) -> RunQueueStats:
        async with self._lock:
            now = utc_now()
            counts = {
                status: sum(job.status == status for job in self._jobs.values())
                for status in RunJobStatus
            }
            available = [job for job in self._jobs.values() if job.status == RunJobStatus.AVAILABLE]
            oldest = (
                max(0.0, (now - min(job.created_at for job in available)).total_seconds())
                if available
                else 0.0
            )
            return RunQueueStats(
                available_jobs=counts[RunJobStatus.AVAILABLE],
                leased_jobs=counts[RunJobStatus.LEASED],
                dead_jobs=counts[RunJobStatus.DEAD],
                completed_jobs=counts[RunJobStatus.COMPLETED],
                cancelled_jobs=counts[RunJobStatus.CANCELLED],
                incompatible_active_jobs=(
                    sum(
                        job.status in {RunJobStatus.AVAILABLE, RunJobStatus.LEASED}
                        and job.graph_revision != graph_revision
                        for job in self._jobs.values()
                    )
                    if graph_revision is not None
                    else 0
                ),
                oldest_available_seconds=oldest,
            )

    async def list_dead_jobs(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RunJob]:
        async with self._lock:
            jobs = sorted(
                (job for job in self._jobs.values() if job.status == RunJobStatus.DEAD),
                key=lambda item: (item.updated_at, item.job_id),
                reverse=True,
            )
            return jobs[offset : offset + limit]

    async def replay_dead_job(
        self,
        job_id: UUID,
        *,
        capacity: int,
        max_attempts: int,
        graph_revision: str = "0" * 64,
    ) -> RunJob:
        async with self._lock:
            dead = self._jobs.get(job_id)
            if dead is None or dead.status != RunJobStatus.DEAD:
                raise RunQueueStateError("dead job not found")
            if any(
                item.run_id == dead.run_id
                and item.status in {RunJobStatus.AVAILABLE, RunJobStatus.LEASED}
                for item in self._jobs.values()
            ):
                raise RunQueueStateError("run already has an active job")
            self._ensure_queue_capacity(capacity)
            replay = RunJob(
                run_id=dead.run_id,
                kind=dead.kind,
                payload=dead.payload,
                priority=dead.priority,
                max_attempts=max_attempts,
                graph_revision=graph_revision,
            )
            self._jobs[replay.job_id] = replay
            record = self._records[dead.run_id]
            self._records[dead.run_id] = record.model_copy(
                update={"status": RunStatus.PENDING, "updated_at": utc_now()}
            )
            return replay

    async def heartbeat_worker(self, heartbeat: WorkerHeartbeat) -> None:
        async with self._lock:
            self._workers[heartbeat.worker_id] = heartbeat

    async def remove_worker(self, worker_id: str) -> None:
        async with self._lock:
            self._workers.pop(worker_id, None)

    async def list_live_workers(
        self,
        *,
        stale_seconds: float,
    ) -> list[WorkerHeartbeat]:
        async with self._lock:
            cutoff = utc_now() - timedelta(seconds=stale_seconds)
            return sorted(
                (
                    heartbeat
                    for heartbeat in self._workers.values()
                    if heartbeat.last_seen_at >= cutoff
                ),
                key=lambda item: item.worker_id,
            )

    async def start_event_listener(self, callback: Callable[[str], None]) -> None:
        self._event_callbacks.add(callback)

    async def stop_event_listener(self, callback: Callable[[str], None]) -> None:
        self._event_callbacks.discard(callback)

    async def notify_run(self, run_id: str) -> None:
        for callback in tuple(self._event_callbacks):
            callback(run_id)
