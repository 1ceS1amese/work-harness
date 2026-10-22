from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from work_harness.domain.enums import RunStatus
from work_harness.domain.models import utc_now

if TYPE_CHECKING:
    from work_harness.storage.queue import (
        RunJob,
        RunQueueStats,
        RunSubmission,
        WorkerHeartbeat,
    )


class RunExecutionVersion(BaseModel):
    """Immutable execution identity; contains no prompts, inputs or credentials.

    The schema version identifies the engine's public state contract, independently
    of storage migrations. The fingerprint binds the effective execution policies.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    engine: Literal["legacy", "blackboard"]
    workflow_revision: str = Field(min_length=1, max_length=128)
    schema_version: int = Field(ge=1, strict=True)
    configuration_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class RunRecord(BaseModel):
    """Non-sensitive, durable index metadata for one graph run."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True, frozen=True)

    run_id: str = Field(min_length=1, max_length=80)
    status: RunStatus = RunStatus.PENDING
    idempotency_key_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    request_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    # Absence identifies pre-versioning legacy runs. Never infer their engine from
    # the service's current default or fabricate an execution fingerprint for them.
    execution: RunExecutionVersion | None = None
    lease_owner: str | None = Field(default=None, max_length=120)
    lease_expires_at: datetime | None = None
    attempt_count: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def engine(self) -> Literal["legacy", "blackboard"]:
        return self.execution.engine if self.execution is not None else "legacy"


class RunStoreNotFoundError(KeyError):
    pass


class RunStore(Protocol):
    backend: str

    async def initialize(self) -> None: ...

    async def close(self) -> None: ...

    async def reserve(
        self, record: RunRecord, *, initial_input: dict[str, Any] | None = None
    ) -> RunRecord:
        """Atomically save the index and private input, or return the existing run."""

    async def get_initial_input(self, run_id: str) -> dict[str, Any] | None: ...

    async def get(self, run_id: str) -> RunRecord | None: ...

    async def get_by_idempotency_key_hash(self, idempotency_key_hash: str) -> RunRecord | None: ...

    async def update_status(self, run_id: str, status: RunStatus) -> RunRecord: ...

    async def list(self, *, limit: int = 50, offset: int = 0) -> list[RunRecord]: ...

    async def claim(self, run_id: str, *, owner: str, lease_seconds: float) -> RunRecord | None: ...

    async def heartbeat(self, run_id: str, *, owner: str, lease_seconds: float) -> bool: ...

    async def release(self, run_id: str, *, owner: str) -> None: ...

    async def list_recoverable(self, *, limit: int = 200) -> list[RunRecord]: ...


class ReliableRunStore(RunStore, Protocol):
    """Run store extension used by PostgreSQL API/Worker execution roles."""

    async def submit(
        self,
        record: RunRecord,
        payload: dict[str, Any],
        *,
        capacity: int,
        max_attempts: int,
        graph_revision: str = "0" * 64,
    ) -> RunSubmission: ...

    async def enqueue_resume(
        self,
        run_id: str,
        payload: dict[str, Any],
        *,
        capacity: int,
        max_attempts: int,
        graph_revision: str = "0" * 64,
    ) -> RunJob: ...

    async def claim_job(
        self,
        *,
        owner: str,
        lease_seconds: float,
        graph_revision: str = "0" * 64,
    ) -> RunJob | None: ...

    async def heartbeat_job(
        self,
        job_id: UUID,
        *,
        owner: str,
        lease_token: UUID,
        lease_seconds: float,
    ) -> bool: ...

    async def complete_job(
        self,
        job_id: UUID,
        *,
        owner: str,
        lease_token: UUID,
        run_status: RunStatus,
    ) -> bool: ...

    async def retry_job(
        self,
        job_id: UUID,
        *,
        owner: str,
        lease_token: UUID,
        error_code: str,
        error_message: str,
        delay_seconds: float,
    ) -> RunJob | None: ...

    async def cancel_jobs(self, run_id: str) -> int: ...

    async def queue_stats(
        self,
        *,
        graph_revision: str | None = None,
    ) -> RunQueueStats: ...

    async def list_dead_jobs(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RunJob]: ...

    async def replay_dead_job(
        self,
        job_id: UUID,
        *,
        capacity: int,
        max_attempts: int,
        graph_revision: str = "0" * 64,
    ) -> RunJob: ...

    async def heartbeat_worker(self, heartbeat: WorkerHeartbeat) -> None: ...

    async def remove_worker(self, worker_id: str) -> None: ...

    async def list_live_workers(
        self,
        *,
        stale_seconds: float,
    ) -> list[WorkerHeartbeat]: ...

    async def start_event_listener(self, callback: Callable[[str], None]) -> None: ...

    async def stop_event_listener(self, callback: Callable[[str], None]) -> None: ...

    async def notify_run(self, run_id: str) -> None: ...
