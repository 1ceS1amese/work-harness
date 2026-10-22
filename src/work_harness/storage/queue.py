from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from work_harness.domain.enums import RunJobKind, RunJobStatus
from work_harness.domain.models import utc_now
from work_harness.storage.base import RunRecord

DEFAULT_GRAPH_REVISION = "0" * 64
QUEUE_RECONCILE_ERROR_CODE = "queue_reconcile_only"


class RunQueueCapacityError(RuntimeError):
    pass


class RunQueueStateError(RuntimeError):
    pass


class RunLeaseLostError(RunQueueStateError):
    """The supplied lease cannot authorize another execution write."""


class RunLeaseClosedError(RunQueueStateError):
    """The run is cancelled or no longer executing."""


class RunJobLease(BaseModel):
    """Immutable job identity; expiry is always read from the database, never this value."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    run_id: str = Field(min_length=1, max_length=80)
    job_id: UUID
    owner: str = Field(min_length=1, max_length=120)
    token: UUID


class RunJob(BaseModel):
    """One durable, independently leased execution attempt source."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True, frozen=True)

    job_id: UUID = Field(default_factory=uuid4)
    run_id: str = Field(min_length=1, max_length=80)
    kind: RunJobKind
    status: RunJobStatus = RunJobStatus.AVAILABLE
    payload: dict[str, Any]
    graph_revision: str = Field(
        default=DEFAULT_GRAPH_REVISION,
        pattern=r"^[0-9a-f]{64}$",
    )
    priority: int = Field(default=0, ge=-32_768, le=32_767)
    available_at: datetime = Field(default_factory=utc_now)
    lease_owner: str | None = Field(default=None, max_length=120)
    lease_token: UUID | None = None
    lease_expires_at: datetime | None = None
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, ge=1, le=100)
    last_error_code: str | None = Field(default=None, max_length=120)
    last_error_message: str | None = Field(default=None, max_length=4_000)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None

    def lease_ref(self) -> RunJobLease:
        if self.status != RunJobStatus.LEASED or not self.lease_owner or not self.lease_token:
            raise RunLeaseLostError("job has no execution lease")
        return RunJobLease(
            run_id=self.run_id, job_id=self.job_id, owner=self.lease_owner, token=self.lease_token
        )

    @property
    def reconciliation_only(self) -> bool:
        """True when a final lease may inspect, but must not advance, the graph."""
        return self.last_error_code == QUEUE_RECONCILE_ERROR_CODE


class RunSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record: RunRecord
    job: RunJob | None = None
    created: bool


class RunQueueStats(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    available_jobs: int = Field(ge=0)
    leased_jobs: int = Field(ge=0)
    dead_jobs: int = Field(ge=0)
    completed_jobs: int = Field(ge=0)
    cancelled_jobs: int = Field(ge=0)
    incompatible_active_jobs: int = Field(default=0, ge=0)
    oldest_available_seconds: float = Field(ge=0)


class WorkerHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    worker_id: str = Field(min_length=1, max_length=120)
    instance_id: str = Field(min_length=1, max_length=120)
    capacity: int = Field(ge=1, le=1_000)
    active_jobs: int = Field(ge=0, le=1_000)
    started_at: datetime = Field(default_factory=utc_now)
    last_seen_at: datetime = Field(default_factory=utc_now)
    build_version: str = Field(min_length=1, max_length=80)
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
