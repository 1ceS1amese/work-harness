from pathlib import Path

import pytest

from work_harness.domain.enums import RunStatus
from work_harness.storage import MemoryRunStore, RunRecord, SQLiteRunStore


@pytest.mark.asyncio
async def test_memory_store_reserves_idempotency_atomically() -> None:
    store = MemoryRunStore()
    await store.initialize()
    first = RunRecord(
        run_id="run_first",
        idempotency_key_hash="a" * 64,
        request_fingerprint="b" * 64,
    )
    duplicate = RunRecord(
        run_id="run_duplicate",
        idempotency_key_hash="a" * 64,
        request_fingerprint="b" * 64,
    )

    assert await store.reserve(first) == first
    assert await store.reserve(duplicate) == first
    assert [item.run_id for item in await store.list()] == ["run_first"]


@pytest.mark.asyncio
async def test_sqlite_store_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    first_store = SQLiteRunStore(path)
    await first_store.initialize()
    await first_store.reserve(
        RunRecord(
            run_id="run_persisted",
            idempotency_key_hash="c" * 64,
            request_fingerprint="d" * 64,
        )
    )
    await first_store.update_status("run_persisted", RunStatus.WAITING_HUMAN)
    await first_store.close()

    reopened = SQLiteRunStore(path)
    await reopened.initialize()
    recovered = await reopened.get("run_persisted")

    assert recovered is not None
    assert recovered.status == RunStatus.WAITING_HUMAN
    assert [item.run_id for item in await reopened.list()] == ["run_persisted"]
    await reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_run_lease_prevents_duplicate_owner(tmp_path: Path, backend: str) -> None:
    store = MemoryRunStore() if backend == "memory" else SQLiteRunStore(tmp_path / "lease.sqlite")
    await store.initialize()
    await store.reserve(RunRecord(run_id="run_leased"))

    first = await store.claim("run_leased", owner="owner-a", lease_seconds=30)
    blocked = await store.claim("run_leased", owner="owner-b", lease_seconds=30)
    assert first is not None
    assert first.attempt_count == 1
    assert blocked is None
    assert await store.heartbeat("run_leased", owner="owner-b", lease_seconds=30) is False

    await store.release("run_leased", owner="owner-a")
    second = await store.claim("run_leased", owner="owner-b", lease_seconds=30)
    assert second is not None
    assert second.attempt_count == 2
    assert [item.run_id for item in await store.list_recoverable()] == []

    await store.update_status("run_leased", RunStatus.SUCCEEDED)
    finished = await store.get("run_leased")
    assert finished is not None
    assert finished.lease_owner is None
    await store.close()
