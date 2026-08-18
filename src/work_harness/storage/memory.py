from __future__ import annotations

import asyncio
from datetime import timedelta

from work_harness.domain.enums import RunStatus
from work_harness.domain.models import utc_now
from work_harness.storage.base import RunRecord, RunStoreNotFoundError


class MemoryRunStore:
    backend = "memory"

    def __init__(self) -> None:
        self._records: dict[str, RunRecord] = {}
        self._idempotency: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def reserve(self, record: RunRecord) -> RunRecord:
        async with self._lock:
            if record.idempotency_key_hash:
                existing_id = self._idempotency.get(record.idempotency_key_hash)
                if existing_id:
                    return self._records[existing_id]
            existing = self._records.get(record.run_id)
            if existing:
                return existing
            self._records[record.run_id] = record
            if record.idempotency_key_hash:
                self._idempotency[record.idempotency_key_hash] = record.run_id
            return record

    async def get(self, run_id: str) -> RunRecord | None:
        async with self._lock:
            return self._records.get(run_id)

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

    async def claim(
        self, run_id: str, *, owner: str, lease_seconds: float
    ) -> RunRecord | None:
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

    async def heartbeat(
        self, run_id: str, *, owner: str, lease_seconds: float
    ) -> bool:
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
