from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Any

from work_harness.domain.enums import RunStatus
from work_harness.domain.models import utc_now
from work_harness.storage.base import RunExecutionVersion, RunRecord, RunStoreNotFoundError


class SQLiteRunStore:
    backend = "sqlite"

    def __init__(self, path: Path) -> None:
        self.path = path
        self._write_lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS run_index (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    idempotency_key_hash TEXT,
                    request_fingerprint TEXT,
                    execution TEXT,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(run_index)")}
            migrations = {
                "execution": "ALTER TABLE run_index ADD COLUMN execution TEXT",
                "lease_owner": "ALTER TABLE run_index ADD COLUMN lease_owner TEXT",
                "lease_expires_at": "ALTER TABLE run_index ADD COLUMN lease_expires_at TEXT",
                "attempt_count": (
                    "ALTER TABLE run_index ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0"
                ),
            }
            for column, statement in migrations.items():
                if column not in columns:
                    connection.execute(statement)
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_run_index_idempotency
                ON run_index(idempotency_key_hash)
                WHERE idempotency_key_hash IS NOT NULL
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_run_index_recovery "
                "ON run_index(status, lease_expires_at)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS run_inputs ("
                "run_id TEXT PRIMARY KEY REFERENCES run_index(run_id), payload TEXT NOT NULL)"
            )
            connection.commit()

    async def close(self) -> None:
        return None

    async def reserve(
        self, record: RunRecord, *, initial_input: dict[str, Any] | None = None
    ) -> RunRecord:
        payload = (
            json.dumps(initial_input, ensure_ascii=False) if initial_input is not None else None
        )
        async with self._write_lock:
            return await asyncio.to_thread(self._reserve_sync, record, payload)

    def _reserve_sync(self, record: RunRecord, initial_input: str | None) -> RunRecord:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO run_index (
                    run_id, status, idempotency_key_hash, request_fingerprint,
                    lease_owner, lease_expires_at, attempt_count, created_at, updated_at,
                    execution
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.run_id,
                    str(record.status),
                    record.idempotency_key_hash,
                    record.request_fingerprint,
                    record.lease_owner,
                    record.lease_expires_at.isoformat() if record.lease_expires_at else None,
                    record.attempt_count,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                    record.execution.model_dump_json() if record.execution else None,
                ),
            )
            if cursor.rowcount and initial_input is not None:
                connection.execute(
                    "INSERT INTO run_inputs (run_id, payload) VALUES (?, ?)",
                    (record.run_id, initial_input),
                )
            connection.commit()
            if record.idempotency_key_hash:
                row = connection.execute(
                    "SELECT * FROM run_index WHERE idempotency_key_hash = ?",
                    (record.idempotency_key_hash,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM run_index WHERE run_id = ?", (record.run_id,)
                ).fetchone()
            if row is None:
                raise RuntimeError("run reservation was not persisted")
            return self._record(row)

    async def get_initial_input(self, run_id: str) -> dict[str, Any] | None:
        def read() -> dict[str, Any] | None:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT payload FROM run_inputs WHERE run_id = ?", (run_id,)
                ).fetchone()
                return json.loads(row["payload"]) if row else None

        return await asyncio.to_thread(read)

    async def get(self, run_id: str) -> RunRecord | None:
        return await asyncio.to_thread(self._get_sync, run_id)

    def _get_sync(self, run_id: str) -> RunRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM run_index WHERE run_id = ?", (run_id,)
            ).fetchone()
            return self._record(row) if row else None

    async def get_by_idempotency_key_hash(self, idempotency_key_hash: str) -> RunRecord | None:
        return await asyncio.to_thread(
            self._get_by_idempotency_key_hash_sync,
            idempotency_key_hash,
        )

    def _get_by_idempotency_key_hash_sync(self, idempotency_key_hash: str) -> RunRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM run_index WHERE idempotency_key_hash = ?",
                (idempotency_key_hash,),
            ).fetchone()
            return self._record(row) if row else None

    async def update_status(self, run_id: str, status: RunStatus) -> RunRecord:
        async with self._write_lock:
            return await asyncio.to_thread(self._update_status_sync, run_id, status)

    def _update_status_sync(self, run_id: str, status: RunStatus) -> RunRecord:
        updated_at = utc_now()
        with closing(self._connect()) as connection:
            clear_lease = status not in {RunStatus.PENDING, RunStatus.RUNNING}
            cursor = connection.execute(
                """
                UPDATE run_index
                SET status = ?, updated_at = ?,
                    lease_owner = CASE WHEN ? THEN NULL ELSE lease_owner END,
                    lease_expires_at = CASE WHEN ? THEN NULL ELSE lease_expires_at END
                WHERE run_id = ?
                """,
                (
                    str(status),
                    updated_at.isoformat(),
                    clear_lease,
                    clear_lease,
                    run_id,
                ),
            )
            connection.commit()
            if cursor.rowcount == 0:
                raise RunStoreNotFoundError(run_id)
            row = connection.execute(
                "SELECT * FROM run_index WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise RunStoreNotFoundError(run_id)
            return self._record(row)

    async def list(self, *, limit: int = 50, offset: int = 0) -> list[RunRecord]:
        return await asyncio.to_thread(self._list_sync, limit, offset)

    def _list_sync(self, limit: int, offset: int) -> list[RunRecord]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM run_index
                ORDER BY created_at DESC, run_id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
            return [self._record(row) for row in rows]

    async def claim(self, run_id: str, *, owner: str, lease_seconds: float) -> RunRecord | None:
        async with self._write_lock:
            return await asyncio.to_thread(self._claim_sync, run_id, owner, lease_seconds)

    def _claim_sync(self, run_id: str, owner: str, lease_seconds: float) -> RunRecord | None:
        now = utc_now()
        expires = now + timedelta(seconds=lease_seconds)
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE run_index
                SET lease_owner = ?, lease_expires_at = ?,
                    attempt_count = attempt_count + 1, updated_at = ?
                WHERE run_id = ?
                  AND status IN (?, ?)
                  AND (
                    lease_owner IS NULL OR lease_expires_at IS NULL
                    OR lease_expires_at <= ? OR lease_owner = ?
                  )
                """,
                (
                    owner,
                    expires.isoformat(),
                    now.isoformat(),
                    run_id,
                    str(RunStatus.PENDING),
                    str(RunStatus.RUNNING),
                    now.isoformat(),
                    owner,
                ),
            )
            connection.commit()
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM run_index WHERE run_id = ?", (run_id,)
            ).fetchone()
            return self._record(row) if row else None

    async def heartbeat(self, run_id: str, *, owner: str, lease_seconds: float) -> bool:
        async with self._write_lock:
            return await asyncio.to_thread(self._heartbeat_sync, run_id, owner, lease_seconds)

    def _heartbeat_sync(self, run_id: str, owner: str, lease_seconds: float) -> bool:
        now = utc_now()
        expires = now + timedelta(seconds=lease_seconds)
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE run_index
                SET lease_expires_at = ?, updated_at = ?
                WHERE run_id = ? AND lease_owner = ? AND status IN (?, ?)
                """,
                (
                    expires.isoformat(),
                    now.isoformat(),
                    run_id,
                    owner,
                    str(RunStatus.PENDING),
                    str(RunStatus.RUNNING),
                ),
            )
            connection.commit()
            return cursor.rowcount == 1

    async def release(self, run_id: str, *, owner: str) -> None:
        async with self._write_lock:
            await asyncio.to_thread(self._release_sync, run_id, owner)

    def _release_sync(self, run_id: str, owner: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE run_index
                SET lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE run_id = ? AND lease_owner = ?
                """,
                (utc_now().isoformat(), run_id, owner),
            )
            connection.commit()

    async def list_recoverable(self, *, limit: int = 200) -> list[RunRecord]:
        return await asyncio.to_thread(self._list_recoverable_sync, limit)

    def _list_recoverable_sync(self, limit: int) -> list[RunRecord]:
        now = utc_now().isoformat()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM run_index
                WHERE status IN (?, ?)
                  AND (lease_owner IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= ?)
                ORDER BY updated_at ASC, run_id ASC
                LIMIT ?
                """,
                (str(RunStatus.PENDING), str(RunStatus.RUNNING), now, limit),
            ).fetchall()
            return [self._record(row) for row in rows]

    @staticmethod
    def _record(row: sqlite3.Row | dict[str, Any]) -> RunRecord:
        payload = dict(row)
        if payload.get("execution") is not None:
            payload["execution"] = RunExecutionVersion.model_validate_json(payload["execution"])
        return RunRecord.model_validate(payload)
