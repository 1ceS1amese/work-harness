from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from work_harness.domain.enums import RunStatus
from work_harness.domain.models import utc_now


class RunRecord(BaseModel):
    """Non-sensitive, durable index metadata for one graph run."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True, frozen=True)

    run_id: str = Field(min_length=1, max_length=80)
    status: RunStatus = RunStatus.PENDING
    idempotency_key_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    request_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    lease_owner: str | None = Field(default=None, max_length=120)
    lease_expires_at: datetime | None = None
    attempt_count: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class RunStoreNotFoundError(KeyError):
    pass


class RunStore(Protocol):
    backend: str

    async def initialize(self) -> None: ...

    async def close(self) -> None: ...

    async def reserve(self, record: RunRecord) -> RunRecord:
        """Atomically create a run or return the matching idempotent record."""

    async def get(self, run_id: str) -> RunRecord | None: ...

    async def update_status(self, run_id: str, status: RunStatus) -> RunRecord: ...

    async def list(self, *, limit: int = 50, offset: int = 0) -> list[RunRecord]: ...

    async def claim(
        self, run_id: str, *, owner: str, lease_seconds: float
    ) -> RunRecord | None: ...

    async def heartbeat(
        self, run_id: str, *, owner: str, lease_seconds: float
    ) -> bool: ...

    async def release(self, run_id: str, *, owner: str) -> None: ...

    async def list_recoverable(self, *, limit: int = 200) -> list[RunRecord]: ...
