"""Execution identity survives index updates, idempotent admission and migration."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from work_harness.domain.enums import RunStatus
from work_harness.storage import MemoryRunStore, RunExecutionVersion, RunRecord, SQLiteRunStore


def version(engine: str = "blackboard") -> RunExecutionVersion:
    return RunExecutionVersion(
        engine=engine,
        workflow_revision="blackboard-v6" if engine == "blackboard" else "agent-subgraphs-v1",
        schema_version=1,
        configuration_fingerprint="c" * 64,
    )


@pytest.fixture(params=("memory", "sqlite"))
async def store(request, tmp_path: Path) -> AsyncIterator[MemoryRunStore | SQLiteRunStore]:
    instance = (
        MemoryRunStore() if request.param == "memory" else SQLiteRunStore(tmp_path / "runs.sqlite")
    )
    await instance.initialize()
    try:
        yield instance
    finally:
        await instance.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ("legacy", "blackboard"))
async def test_execution_version_survives_run_lifecycle(store, engine: str) -> None:
    execution = version(engine)
    record = RunRecord(run_id="run_versioned", execution=execution, idempotency_key_hash="a" * 64)
    assert await store.reserve(record) == record
    assert (await store.get(record.run_id)).execution == execution
    assert (await store.list())[0].execution == execution
    assert (await store.list_recoverable())[0].execution == execution
    assert (await store.get_by_idempotency_key_hash("a" * 64)).execution == execution

    claimed = await store.claim(record.run_id, owner="first", lease_seconds=30)
    assert claimed.execution == execution
    assert claimed.engine == engine
    assert await store.heartbeat(record.run_id, owner="first", lease_seconds=30)
    await store.release(record.run_id, owner="first")
    for status in (RunStatus.RUNNING, RunStatus.WAITING_HUMAN, RunStatus.SUCCEEDED):
        updated = await store.update_status(record.run_id, status)
        assert updated.execution == execution
    assert (await store.get(record.run_id)).execution == execution


@pytest.mark.asyncio
async def test_idempotent_retry_cannot_rebind_engine_or_configuration(store) -> None:
    original = RunRecord(run_id="first", execution=version(), idempotency_key_hash="a" * 64)
    await store.reserve(original)
    changed = version("legacy").model_copy(update={"configuration_fingerprint": "b" * 64})
    duplicate = RunRecord(run_id="second", execution=changed, idempotency_key_hash="a" * 64)
    assert await store.reserve(duplicate) == original
    assert await store.get("second") is None
    same_id = RunRecord(run_id="first", execution=changed)
    assert await store.reserve(same_id) == original


@pytest.mark.asyncio
async def test_concurrent_admission_keeps_the_winning_version(store) -> None:
    records = [
        RunRecord(
            run_id=f"run_{index}",
            execution=version("legacy" if index % 2 else "blackboard"),
            idempotency_key_hash="d" * 64,
        )
        for index in range(12)
    ]
    admitted = await asyncio.gather(*(store.reserve(record) for record in records))
    assert all(record == admitted[0] for record in admitted)
    assert await store.list() == [admitted[0]]


@pytest.mark.asyncio
async def test_old_records_remain_legacy_without_inventing_version_metadata(store) -> None:
    old = RunRecord(run_id="historical", idempotency_key_hash="a" * 64)
    await store.reserve(old)
    assert old.engine == "legacy"
    assert old.execution is None
    new = RunRecord(run_id="new", execution=version(), idempotency_key_hash="a" * 64)
    duplicate = await store.reserve(new)
    assert duplicate.engine == "legacy"
    assert duplicate.execution is None


@pytest.mark.asyncio
async def test_accepted_input_is_private_immutable_and_idempotent(store) -> None:
    record = RunRecord(run_id="input", execution=version(), idempotency_key_hash="a" * 64)
    payload = {"goal": "original", "context": {"notes": ["original input"]}}
    await store.reserve(record, initial_input=payload)
    payload["context"]["notes"].append("later mutation")
    assert (await store.get_initial_input(record.run_id))["context"]["notes"] == ["original input"]
    duplicate = RunRecord(
        run_id="duplicate", execution=version("legacy"), idempotency_key_hash="a" * 64
    )
    assert await store.reserve(duplicate, initial_input={"goal": "changed"}) == record
    assert await store.get_initial_input("duplicate") is None
    recovered = await store.get_initial_input(record.run_id)
    recovered["goal"] = "changed after read"
    assert (await store.get_initial_input(record.run_id))["goal"] == "original"
    assert "original input" not in (await store.get(record.run_id)).model_dump_json()


@pytest.mark.asyncio
async def test_sqlite_input_failure_rolls_back_the_run_and_version(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path / "atomic.sqlite")
    await store.initialize()
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_input BEFORE INSERT ON run_inputs "
            "BEGIN SELECT RAISE(ABORT, 'injected input failure'); END"
        )
    try:
        with pytest.raises(sqlite3.IntegrityError, match="injected input failure"):
            await store.reserve(
                RunRecord(run_id="failed", execution=version()), initial_input={"goal": "test"}
            )
        assert await store.get("failed") is None
        assert await store.get_initial_input("failed") is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_reopen_preserves_execution_identity(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    original = RunRecord(run_id="run_persisted", execution=version())
    first = SQLiteRunStore(path)
    await first.initialize()
    await first.reserve(original)
    await first.close()
    reopened = SQLiteRunStore(path)
    await reopened.initialize()
    try:
        assert await reopened.get(original.run_id) == original
        assert (await reopened.list_recoverable())[0].engine == "blackboard"
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_independent_sqlite_handles_cannot_replace_the_winning_version(
    tmp_path: Path,
) -> None:
    path = tmp_path / "shared.sqlite"
    first, second = SQLiteRunStore(path), SQLiteRunStore(path)
    await first.initialize()
    await second.initialize()
    try:
        blackboard = RunRecord(run_id="board", execution=version(), idempotency_key_hash="f" * 64)
        legacy = RunRecord(
            run_id="legacy", execution=version("legacy"), idempotency_key_hash="f" * 64
        )
        admitted = await asyncio.gather(first.reserve(blackboard), second.reserve(legacy))
        assert admitted[0] == admitted[1]
        assert await first.list() == await second.list() == [admitted[0]]
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_memory_queue_admission_preserves_execution_version() -> None:
    store = MemoryRunStore()
    record = RunRecord(run_id="queued", execution=version("legacy"), idempotency_key_hash="e" * 64)
    submitted = await store.submit(record, {"goal": "queued"}, capacity=1, max_attempts=1)
    assert submitted.created
    assert submitted.record.execution == record.execution
    repeated = await store.submit(
        record.model_copy(update={"execution": None, "run_id": "duplicate"}),
        {"goal": "queued"},
        capacity=1,
        max_attempts=1,
    )
    assert not repeated.created
    assert repeated.record.execution == record.execution
    job = await store.claim_job(owner="worker", lease_seconds=30)
    assert job.run_id == record.run_id
    assert (await store.get(record.run_id)).execution == record.execution


@pytest.mark.asyncio
async def test_sqlite_migrates_unversioned_index_without_relabeling_history(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    old = RunRecord(run_id="run_old")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE run_index (run_id TEXT PRIMARY KEY, status TEXT NOT NULL, "
            "idempotency_key_hash TEXT, request_fingerprint TEXT, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO run_index VALUES (?, ?, NULL, NULL, ?, ?)",
            (old.run_id, str(old.status), old.created_at.isoformat(), old.updated_at.isoformat()),
        )
    migrated = SQLiteRunStore(path)
    await migrated.initialize()
    await migrated.initialize()
    try:
        recovered = await migrated.get(old.run_id)
        assert recovered == old
        assert recovered.engine == "legacy"
        assert recovered.execution is None
        new = RunRecord(run_id="run_new", execution=version())
        assert await migrated.reserve(new) == new
    finally:
        await migrated.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ('{"engine":"unknown"}', "null", "[]", "{}"))
async def test_invalid_stored_version_does_not_fall_back_to_legacy(
    tmp_path: Path, payload: str
) -> None:
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    await store.initialize()
    await store.reserve(RunRecord(run_id="corrupt", execution=version()))
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE run_index SET execution = ?", (payload,))
    try:
        with pytest.raises(ValidationError):
            await store.get("corrupt")
    finally:
        await store.close()


def test_execution_version_is_frozen_and_contains_only_identity() -> None:
    execution = version()
    with pytest.raises(ValidationError, match="frozen"):
        execution.engine = "legacy"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        RunExecutionVersion(**execution.model_dump(), prompts={"worker": "private"})
    with pytest.raises(ValidationError):
        RunExecutionVersion(**{**execution.model_dump(), "schema_version": True})
