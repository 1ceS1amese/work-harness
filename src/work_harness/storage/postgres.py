from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from typing import Any
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from work_harness.domain.enums import RunJobKind, RunJobStatus, RunStatus
from work_harness.observability.logging import get_logger
from work_harness.storage.base import RunRecord, RunStoreNotFoundError
from work_harness.storage.queue import (
    QUEUE_RECONCILE_ERROR_CODE,
    RunJob,
    RunJobLease,
    RunLeaseClosedError,
    RunLeaseLostError,
    RunQueueCapacityError,
    RunQueueStateError,
    RunQueueStats,
    RunSubmission,
    WorkerHeartbeat,
)

SCHEMA_VERSION = 5
QUEUE_ADVISORY_LOCK_ID = 1_983_052_177
EVENT_LISTENER_RETRY_SECONDS = 1.0


class PostgresSchemaError(RuntimeError):
    pass


class PostgresRunStore:
    """Shared run index and durable at-least-once queue for multi-instance mode."""

    backend = "postgres"
    schema_version = SCHEMA_VERSION

    def __init__(
        self,
        dsn: str,
        *,
        pool_min_size: int = 1,
        pool_max_size: int = 10,
    ) -> None:
        self._dsn = dsn
        self._pool = AsyncConnectionPool(
            conninfo=dsn,
            min_size=pool_min_size,
            max_size=pool_max_size,
            open=False,
            kwargs={"row_factory": dict_row},
        )
        self._opened = False
        self._event_connection: AsyncConnection[Any] | None = None
        self._event_task: asyncio.Task[Any] | None = None
        self._event_callbacks: set[Callable[[str], None]] = set()
        self._logger = get_logger("work_harness.postgres_run_store")

    async def initialize(self) -> None:
        await self._open()
        async with self._pool.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT to_regclass('work_harness_schema_version') AS name"
                )
            ).fetchone()
            if not row or row["name"] is None:
                raise PostgresSchemaError(
                    "PostgreSQL schema is not initialized; run `work-harness migrate`"
                )
            version = await (
                await connection.execute(
                    "SELECT version FROM work_harness_schema_version WHERE component = %s",
                    ("run_queue",),
                )
            ).fetchone()
            if not version:
                raise PostgresSchemaError("run queue schema version is missing")
            if version["version"] != SCHEMA_VERSION:
                raise PostgresSchemaError(
                    "unsupported run queue schema version: "
                    f"database={version['version']} application={SCHEMA_VERSION}"
                )

    async def setup(self) -> None:
        """Create or migrate queue tables; intended for the one-shot migrate role."""
        await self._open()
        statements = (
            """
            CREATE TABLE IF NOT EXISTS work_harness_schema_version (
                component TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS work_harness_run_index (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                idempotency_key_hash CHAR(64),
                request_fingerprint CHAR(64),
                execution JSONB CHECK (
                    execution IS NULL OR jsonb_typeof(execution) = 'object'
                ),
                lease_owner TEXT,
                lease_expires_at TIMESTAMPTZ,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                cancel_requested_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                CONSTRAINT ck_work_harness_run_status CHECK (
                    status IN (
                        'pending', 'running', 'waiting_human', 'succeeded',
                        'partial', 'failed', 'cancelled'
                    )
                )
            )
            """,
            """
            ALTER TABLE work_harness_run_index
            ADD COLUMN IF NOT EXISTS execution JSONB CHECK (
                execution IS NULL OR jsonb_typeof(execution) = 'object'
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_work_harness_run_idempotency
            ON work_harness_run_index(idempotency_key_hash)
            WHERE idempotency_key_hash IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS ix_work_harness_run_recovery
            ON work_harness_run_index(status, lease_expires_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS work_harness_run_inputs (
                run_id TEXT PRIMARY KEY REFERENCES work_harness_run_index(run_id) ON DELETE CASCADE,
                payload JSONB NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS work_harness_run_jobs (
                job_id UUID PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES work_harness_run_index(run_id),
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                payload JSONB NOT NULL,
                graph_revision CHAR(64) NOT NULL,
                priority SMALLINT NOT NULL DEFAULT 0,
                available_at TIMESTAMPTZ NOT NULL,
                lease_owner TEXT,
                lease_token UUID,
                lease_expires_at TIMESTAMPTZ,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL,
                last_error_code TEXT,
                last_error_message TEXT,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                completed_at TIMESTAMPTZ,
                CONSTRAINT ck_work_harness_job_kind CHECK (kind IN ('start', 'resume')),
                CONSTRAINT ck_work_harness_job_status CHECK (
                    status IN ('available', 'leased', 'completed', 'cancelled', 'dead')
                ),
                CONSTRAINT ck_work_harness_job_attempts CHECK (
                    attempt_count >= 0 AND max_attempts >= 1
                )
            )
            """,
            """
            ALTER TABLE work_harness_run_jobs
            ADD COLUMN IF NOT EXISTS graph_revision CHAR(64)
            """,
            """
            UPDATE work_harness_run_jobs
            SET graph_revision = repeat('0', 64)
            WHERE graph_revision IS NULL
            """,
            """
            ALTER TABLE work_harness_run_jobs
            ALTER COLUMN graph_revision SET NOT NULL
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_work_harness_run_jobs_active
            ON work_harness_run_jobs(run_id)
            WHERE status IN ('available', 'leased')
            """,
            """
            CREATE INDEX IF NOT EXISTS ix_work_harness_run_jobs_claim
            ON work_harness_run_jobs(priority DESC, available_at ASC, created_at ASC)
            WHERE status IN ('available', 'leased')
            """,
            """
            CREATE INDEX IF NOT EXISTS ix_work_harness_run_jobs_revision_claim
            ON work_harness_run_jobs(
                graph_revision, priority DESC, available_at ASC, created_at ASC
            )
            WHERE status IN ('available', 'leased')
            """,
            """
            CREATE TABLE IF NOT EXISTS work_harness_worker_heartbeats (
                worker_id TEXT PRIMARY KEY,
                instance_id TEXT NOT NULL,
                capacity INTEGER NOT NULL,
                active_jobs INTEGER NOT NULL,
                started_at TIMESTAMPTZ NOT NULL,
                last_seen_at TIMESTAMPTZ NOT NULL,
                build_version TEXT NOT NULL,
                config_fingerprint CHAR(64) NOT NULL
            )
            """,
            """
            ALTER TABLE work_harness_worker_heartbeats
            ADD COLUMN IF NOT EXISTS config_fingerprint CHAR(64)
            """,
            """
            UPDATE work_harness_worker_heartbeats
            SET config_fingerprint = repeat('0', 64)
            WHERE config_fingerprint IS NULL
            """,
            """
            ALTER TABLE work_harness_worker_heartbeats
            ALTER COLUMN config_fingerprint SET NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS ix_work_harness_worker_heartbeats_seen
            ON work_harness_worker_heartbeats(last_seen_at)
            """,
        )
        async with self._pool.connection() as connection:
            async with connection.transaction():
                # Serialize bootstrap with the blackboard's component migrations.
                await self._queue_lock(connection)
                for statement in statements:
                    await connection.execute(statement)
                row = await (
                    await connection.execute(
                        "SELECT version FROM work_harness_schema_version "
                        "WHERE component = %s FOR UPDATE",
                        ("run_queue",),
                    )
                ).fetchone()
                if row and row["version"] > SCHEMA_VERSION:
                    raise PostgresSchemaError(
                        "database schema is newer than this application: "
                        f"{row['version']} > {SCHEMA_VERSION}"
                    )
                await connection.execute(
                    """
                    INSERT INTO work_harness_schema_version(component, version)
                    VALUES (%s, %s)
                    ON CONFLICT(component) DO UPDATE
                    SET version = EXCLUDED.version, updated_at = clock_timestamp()
                    """,
                    ("run_queue", SCHEMA_VERSION),
                )

    async def _open(self) -> None:
        if not self._opened:
            await self._pool.open(wait=True)
            self._opened = True

    async def close(self) -> None:
        if self._event_task:
            self._event_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._event_task
            self._event_task = None
        if self._event_connection:
            await self._event_connection.close()
            self._event_connection = None
        self._event_callbacks.clear()
        if self._opened:
            await self._pool.close()
            self._opened = False

    async def reserve(
        self, record: RunRecord, *, initial_input: dict[str, Any] | None = None
    ) -> RunRecord:
        async with self._pool.connection() as connection:
            async with connection.transaction():
                cursor = await connection.execute(
                    """
                    INSERT INTO work_harness_run_index(
                        run_id, status, idempotency_key_hash, request_fingerprint,
                        lease_owner, lease_expires_at, attempt_count, created_at, updated_at,
                        execution
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    self._record_values(record),
                )
                if cursor.rowcount and initial_input is not None:
                    await connection.execute(
                        "INSERT INTO work_harness_run_inputs (run_id, payload) VALUES (%s, %s)",
                        (record.run_id, Jsonb(initial_input)),
                    )
                if record.idempotency_key_hash:
                    row = await (
                        await connection.execute(
                            "SELECT * FROM work_harness_run_index WHERE idempotency_key_hash = %s",
                            (record.idempotency_key_hash,),
                        )
                    ).fetchone()
                else:
                    row = await (
                        await connection.execute(
                            "SELECT * FROM work_harness_run_index WHERE run_id = %s",
                            (record.run_id,),
                        )
                    ).fetchone()
                if not row:
                    raise RuntimeError("run reservation was not persisted")
                return self._record(row)

    async def get_initial_input(self, run_id: str) -> dict[str, Any] | None:
        async with self._pool.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT payload FROM work_harness_run_inputs WHERE run_id = %s", (run_id,)
                )
            ).fetchone()
            return row["payload"] if row else None

    async def get(self, run_id: str) -> RunRecord | None:
        async with self._pool.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT * FROM work_harness_run_index WHERE run_id = %s",
                    (run_id,),
                )
            ).fetchone()
            return self._record(row) if row else None

    async def get_by_idempotency_key_hash(self, idempotency_key_hash: str) -> RunRecord | None:
        async with self._pool.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT * FROM work_harness_run_index WHERE idempotency_key_hash = %s",
                    (idempotency_key_hash,),
                )
            ).fetchone()
            return self._record(row) if row else None

    async def update_status(self, run_id: str, status: RunStatus) -> RunRecord:
        clear_lease = status not in {RunStatus.PENDING, RunStatus.RUNNING}
        async with self._pool.connection() as connection:
            row = await (
                await connection.execute(
                    """
                    UPDATE work_harness_run_index
                    SET status = %s, updated_at = clock_timestamp(),
                        lease_owner = CASE WHEN %s THEN NULL ELSE lease_owner END,
                        lease_expires_at = CASE WHEN %s THEN NULL ELSE lease_expires_at END
                    WHERE run_id = %s
                    RETURNING *
                    """,
                    (str(status), clear_lease, clear_lease, run_id),
                )
            ).fetchone()
            if not row:
                raise RunStoreNotFoundError(run_id)
            return self._record(row)

    async def list(self, *, limit: int = 50, offset: int = 0) -> list[RunRecord]:
        async with self._pool.connection() as connection:
            rows = await (
                await connection.execute(
                    """
                    SELECT * FROM work_harness_run_index
                    ORDER BY created_at DESC, run_id DESC LIMIT %s OFFSET %s
                    """,
                    (limit, offset),
                )
            ).fetchall()
            return [self._record(row) for row in rows]

    async def claim(self, run_id: str, *, owner: str, lease_seconds: float) -> RunRecord | None:
        async with self._pool.connection() as connection:
            row = await (
                await connection.execute(
                    """
                    UPDATE work_harness_run_index
                    SET lease_owner = %s,
                        lease_expires_at = clock_timestamp() + make_interval(secs => %s),
                        attempt_count = attempt_count + 1,
                        updated_at = clock_timestamp()
                    WHERE run_id = %s
                      AND status IN ('pending', 'running')
                      AND (
                        lease_owner IS NULL OR lease_expires_at IS NULL
                        OR lease_expires_at <= clock_timestamp() OR lease_owner = %s
                      )
                    RETURNING *
                    """,
                    (owner, lease_seconds, run_id, owner),
                )
            ).fetchone()
            return self._record(row) if row else None

    async def heartbeat(self, run_id: str, *, owner: str, lease_seconds: float) -> bool:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE work_harness_run_index
                SET lease_expires_at = clock_timestamp() + make_interval(secs => %s),
                    updated_at = clock_timestamp()
                WHERE run_id = %s AND lease_owner = %s
                  AND status IN ('pending', 'running')
                """,
                (lease_seconds, run_id, owner),
            )
            return cursor.rowcount == 1

    async def release(self, run_id: str, *, owner: str) -> None:
        async with self._pool.connection() as connection:
            await connection.execute(
                """
                UPDATE work_harness_run_index
                SET lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = clock_timestamp()
                WHERE run_id = %s AND lease_owner = %s
                """,
                (run_id, owner),
            )

    async def list_recoverable(self, *, limit: int = 200) -> list[RunRecord]:
        async with self._pool.connection() as connection:
            rows = await (
                await connection.execute(
                    """
                    SELECT * FROM work_harness_run_index
                    WHERE status IN ('pending', 'running')
                      AND (
                        lease_owner IS NULL OR lease_expires_at IS NULL
                        OR lease_expires_at <= clock_timestamp()
                      )
                    ORDER BY updated_at ASC, run_id ASC LIMIT %s
                    """,
                    (limit,),
                )
            ).fetchall()
            return [self._record(row) for row in rows]

    async def submit(
        self,
        record: RunRecord,
        payload: dict[str, Any],
        *,
        capacity: int,
        max_attempts: int,
        graph_revision: str = "0" * 64,
    ) -> RunSubmission:
        async with self._pool.connection() as connection:
            async with connection.transaction():
                await self._queue_lock(connection)
                if record.idempotency_key_hash:
                    existing = await (
                        await connection.execute(
                            "SELECT * FROM work_harness_run_index WHERE idempotency_key_hash = %s",
                            (record.idempotency_key_hash,),
                        )
                    ).fetchone()
                    if existing:
                        return RunSubmission(
                            record=self._record(existing),
                            created=False,
                        )
                await self._check_capacity(connection, capacity)
                await connection.execute(
                    """
                    INSERT INTO work_harness_run_index(
                        run_id, status, idempotency_key_hash, request_fingerprint,
                        lease_owner, lease_expires_at, attempt_count, created_at, updated_at,
                        execution
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    self._record_values(record),
                )
                job = RunJob(
                    run_id=record.run_id,
                    kind=RunJobKind.START,
                    payload=payload,
                    max_attempts=max_attempts,
                    graph_revision=graph_revision,
                )
                await self._insert_job(connection, job)
                await connection.execute(
                    "INSERT INTO work_harness_run_inputs (run_id, payload) VALUES (%s, %s)",
                    (record.run_id, Jsonb(payload)),
                )
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
        async with self._pool.connection() as connection:
            async with connection.transaction():
                await self._queue_lock(connection)
                record = await (
                    await connection.execute(
                        "SELECT * FROM work_harness_run_index WHERE run_id = %s FOR UPDATE",
                        (run_id,),
                    )
                ).fetchone()
                if not record:
                    raise RunStoreNotFoundError(run_id)
                active = await (
                    await connection.execute(
                        "SELECT 1 FROM work_harness_run_jobs "
                        "WHERE run_id = %s AND status IN ('available', 'leased')",
                        (run_id,),
                    )
                ).fetchone()
                if active:
                    raise RunQueueStateError("run already has an active job")
                if record["status"] != RunStatus.WAITING_HUMAN:
                    raise RunQueueStateError(
                        f"run is not waiting for human input: {record['status']}"
                    )
                await self._check_capacity(connection, capacity)
                job = RunJob(
                    run_id=run_id,
                    kind=RunJobKind.RESUME,
                    payload=payload,
                    max_attempts=max_attempts,
                    graph_revision=graph_revision,
                )
                await self._insert_job(connection, job)
                await connection.execute(
                    """
                    UPDATE work_harness_run_index
                    SET status = 'pending', updated_at = clock_timestamp()
                    WHERE run_id = %s
                    """,
                    (run_id,),
                )
                return job

    async def claim_job(
        self,
        *,
        owner: str,
        lease_seconds: float,
        graph_revision: str = "0" * 64,
    ) -> RunJob | None:
        lease_token = uuid4()
        async with self._pool.connection() as connection:
            async with connection.transaction():
                row = await (
                    await connection.execute(
                        """
                        WITH candidate AS (
                            SELECT job_id FROM work_harness_run_jobs
                            WHERE graph_revision = %s AND (
                                status = 'available'
                                AND available_at <= clock_timestamp()
                                AND attempt_count < max_attempts
                              OR
                                status = 'leased'
                                AND lease_expires_at <= clock_timestamp()
                            )
                            ORDER BY priority DESC, available_at ASC, created_at ASC
                            FOR UPDATE SKIP LOCKED
                            LIMIT 1
                        )
                        UPDATE work_harness_run_jobs AS job
                        SET status = 'leased', lease_owner = %s, lease_token = %s,
                            lease_expires_at = (
                                clock_timestamp() + make_interval(secs => %s)
                            ),
                            attempt_count = CASE
                                WHEN job.status = 'leased'
                                  AND job.attempt_count >= job.max_attempts
                                THEN job.attempt_count
                                ELSE job.attempt_count + 1
                            END,
                            last_error_code = CASE
                                WHEN job.status = 'leased'
                                  AND job.attempt_count >= job.max_attempts
                                THEN %s
                                ELSE job.last_error_code
                            END,
                            last_error_message = CASE
                                WHEN job.status = 'leased'
                                  AND job.attempt_count >= job.max_attempts
                                THEN 'Final lease is limited to checkpoint reconciliation.'
                                ELSE job.last_error_message
                            END,
                            updated_at = clock_timestamp()
                        FROM candidate
                        WHERE job.job_id = candidate.job_id
                        RETURNING job.*
                        """,
                        (
                            graph_revision,
                            owner,
                            lease_token,
                            lease_seconds,
                            QUEUE_RECONCILE_ERROR_CODE,
                        ),
                    )
                ).fetchone()
                if not row:
                    return None
                await connection.execute(
                    """
                    UPDATE work_harness_run_index
                    SET status = 'running', updated_at = clock_timestamp()
                    WHERE run_id = %s AND status != 'cancelled'
                    """,
                    (row["run_id"],),
                )
                return self._job(row)

    @staticmethod
    async def check_job_lease_in_transaction(
        connection: AsyncConnection,
        lease: RunJobLease,
        *,
        lock: bool = True,
        engine: str | None = None,
        allow_reconciliation: bool = False,
    ) -> None:
        """Validate on the caller's connection; locks survive until its commit/rollback.

        Order is Job -> Run -> caller's board lock, matching claim/complete/retry/cancel.
        A non-locking call is only a preflight read and cannot authorize publication.
        """
        suffix = " FOR UPDATE" if lock else ""
        job = await (
            await connection.execute(
                "SELECT * FROM work_harness_run_jobs WHERE job_id = %s" + suffix,
                (lease.job_id,),
            )
        ).fetchone()
        if job is None or job["run_id"] != lease.run_id:
            raise RunLeaseLostError("job does not belong to this run")
        run = await (
            await connection.execute(
                "SELECT status, cancel_requested_at, execution FROM work_harness_run_index "
                "WHERE run_id = %s" + suffix,
                (lease.run_id,),
            )
        ).fetchone()
        if run is None:
            raise RunLeaseLostError("leased run is missing")
        if run["cancel_requested_at"] is not None or run["status"] == RunStatus.CANCELLED:
            raise RunLeaseClosedError("run cancellation prevents execution writes")
        if run["status"] != RunStatus.RUNNING:
            raise RunLeaseClosedError("run is not executing")
        if engine is not None and (run["execution"] or {}).get("engine", "legacy") != engine:
            raise RunLeaseLostError("lease belongs to a different execution engine")
        # Read the clock after lock waits; transaction_timestamp() would be too early.
        now = await (await connection.execute("SELECT clock_timestamp() AS now")).fetchone()
        if (
            job["status"] != RunJobStatus.LEASED
            or job["lease_owner"] != lease.owner
            or job["lease_token"] != lease.token
            or job["lease_expires_at"] is None
            or job["lease_expires_at"] <= now["now"]
            or (not allow_reconciliation and job["last_error_code"] == QUEUE_RECONCILE_ERROR_CODE)
        ):
            raise RunLeaseLostError("job lease is expired, replaced or limited to reconciliation")

    @staticmethod
    async def check_cancelled_run_in_transaction(connection: AsyncConnection, run_id: str) -> None:
        """Host sealing after durable cancellation; it grants no execution authority."""
        # Lock jobs first even though cancellation usually already invalidated them.
        await connection.execute(
            "SELECT job_id FROM work_harness_run_jobs WHERE run_id = %s ORDER BY job_id FOR UPDATE",
            (run_id,),
        )
        run = await (
            await connection.execute(
                "SELECT status, cancel_requested_at FROM work_harness_run_index "
                "WHERE run_id = %s FOR UPDATE",
                (run_id,),
            )
        ).fetchone()
        if run is None or (
            run["status"] != RunStatus.CANCELLED and run["cancel_requested_at"] is None
        ):
            raise RunQueueStateError("run cancellation has not been persisted")

    async def heartbeat_job(
        self,
        job_id: UUID,
        *,
        owner: str,
        lease_token: UUID,
        lease_seconds: float,
    ) -> bool:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE work_harness_run_jobs AS job
                SET lease_expires_at = clock_timestamp() + make_interval(secs => %s),
                    updated_at = clock_timestamp()
                WHERE job_id = %s AND status = 'leased'
                  AND lease_owner = %s AND lease_token = %s
                  AND EXISTS (
                    SELECT 1 FROM work_harness_run_index AS run
                    WHERE run.run_id = job.run_id AND run.status != 'cancelled'
                  )
                """,
                (lease_seconds, job_id, owner, lease_token),
            )
            return cursor.rowcount == 1

    async def complete_job(
        self, job_id: UUID, *, owner: str, lease_token: UUID, run_status: RunStatus
    ) -> bool:
        async with self._pool.connection() as connection, connection.transaction():
            return await self.complete_job_in_transaction(
                connection, job_id, owner=owner, lease_token=lease_token, run_status=run_status
            )

    @staticmethod
    async def complete_job_in_transaction(
        connection: AsyncConnection,
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
        row = await (
            await connection.execute(
                """
                UPDATE work_harness_run_jobs
                SET status = 'completed', lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, updated_at = clock_timestamp(),
                    completed_at = clock_timestamp()
                WHERE job_id = %s AND status = 'leased'
                  AND lease_owner = %s AND lease_token = %s
                RETURNING run_id
                """,
                (job_id, owner, lease_token),
            )
        ).fetchone()
        if not row:
            return False
        await connection.execute(
            """
            UPDATE work_harness_run_index
            SET status = %s, updated_at = clock_timestamp(),
                lease_owner = NULL, lease_expires_at = NULL
            WHERE run_id = %s
            """,
            (str(run_status), row["run_id"]),
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
        async with self._pool.connection() as connection:
            async with connection.transaction():
                current = await (
                    await connection.execute(
                        """
                        SELECT * FROM work_harness_run_jobs
                        WHERE job_id = %s AND status = 'leased'
                          AND lease_owner = %s AND lease_token = %s
                        FOR UPDATE
                        """,
                        (job_id, owner, lease_token),
                    )
                ).fetchone()
                if not current:
                    return None
                dead = current["attempt_count"] >= current["max_attempts"]
                row = await (
                    await connection.execute(
                        """
                        UPDATE work_harness_run_jobs
                        SET status = %s,
                            available_at = (
                                clock_timestamp() + make_interval(secs => %s)
                            ),
                            lease_owner = NULL, lease_token = NULL,
                            lease_expires_at = NULL, last_error_code = %s,
                            last_error_message = %s, updated_at = clock_timestamp(),
                            completed_at = CASE WHEN %s THEN clock_timestamp() ELSE NULL END
                        WHERE job_id = %s
                        RETURNING *
                        """,
                        (
                            str(RunJobStatus.DEAD if dead else RunJobStatus.AVAILABLE),
                            max(0, delay_seconds),
                            error_code[:120],
                            error_message[:4_000],
                            dead,
                            job_id,
                        ),
                    )
                ).fetchone()
                await connection.execute(
                    """
                    UPDATE work_harness_run_index
                    SET status = %s, updated_at = clock_timestamp()
                    WHERE run_id = %s
                    """,
                    (
                        str(RunStatus.FAILED if dead else RunStatus.PENDING),
                        current["run_id"],
                    ),
                )
                return self._job(row)

    async def blackboard_reconciliations(self, *, limit: int = 200) -> tuple[str, ...]:
        """Durable terminal work; optional tables let legacy-only Workers keep running."""
        async with self._pool.connection() as connection:
            tables = await (
                await connection.execute("SELECT to_regclass('work_harness_board_control') AS name")
            ).fetchone()
            if tables["name"] is None:
                return ()
            rows = await (
                await connection.execute(
                    """SELECT r.run_id FROM work_harness_run_index AS r
                   LEFT JOIN work_harness_board_control AS c ON c.board_id = r.run_id
                   WHERE r.execution->>'engine' = 'blackboard'
                   AND (c.status_pending OR
                       ((r.status = 'cancelled' OR r.cancel_requested_at IS NOT NULL)
                         AND c.finalization IS NULL))
                   ORDER BY r.updated_at, r.run_id LIMIT %s""",
                    (limit,),
                )
            ).fetchall()
            return tuple(row["run_id"] for row in rows)

    async def cancel_jobs(self, run_id: str) -> int:
        async with self._pool.connection() as connection:
            async with connection.transaction():
                await self._queue_lock(connection)
                # Match publication's Job -> Run order. Once the run row is locked,
                # a queue-bound board cannot seal concurrently with this cancellation.
                await connection.execute(
                    "SELECT job_id FROM work_harness_run_jobs WHERE run_id = %s "
                    "AND status IN ('available', 'leased') ORDER BY job_id FOR UPDATE",
                    (run_id,),
                )
                record = await (
                    await connection.execute(
                        "SELECT status, execution FROM work_harness_run_index "
                        "WHERE run_id = %s FOR UPDATE",
                        (run_id,),
                    )
                ).fetchone()
                if not record:
                    raise RunStoreNotFoundError(run_id)
                if record["status"] in {
                    RunStatus.SUCCEEDED,
                    RunStatus.PARTIAL,
                    RunStatus.FAILED,
                    RunStatus.CANCELLED,
                }:
                    return 0
                if (record["execution"] or {}).get("engine") == "blackboard":
                    sealed = await (
                        await connection.execute(
                            "SELECT finalization FROM work_harness_board_control "
                            "WHERE board_id = %s",
                            (run_id,),
                        )
                    ).fetchone()
                    if sealed and sealed["finalization"] is not None:
                        # Board finalization already won. Preserve its lease so the
                        # worker/reconciler can acknowledge that immutable result.
                        return 0
                cursor = await connection.execute(
                    """
                    UPDATE work_harness_run_jobs
                    SET status = 'cancelled', lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL, updated_at = clock_timestamp(),
                        completed_at = clock_timestamp()
                    WHERE run_id = %s AND status IN ('available', 'leased')
                    """,
                    (run_id,),
                )
                await connection.execute(
                    """
                    UPDATE work_harness_run_index
                    SET status = 'cancelled', cancel_requested_at = clock_timestamp(),
                        lease_owner = NULL, lease_expires_at = NULL,
                        updated_at = clock_timestamp()
                    WHERE run_id = %s
                      AND status IN ('pending', 'running', 'waiting_human')
                    """,
                    (run_id,),
                )
                return cursor.rowcount

    async def queue_stats(
        self,
        *,
        graph_revision: str | None = None,
    ) -> RunQueueStats:
        async with self._pool.connection() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT
                      count(*) FILTER (WHERE status = 'available') AS available_jobs,
                      count(*) FILTER (WHERE status = 'leased') AS leased_jobs,
                      count(*) FILTER (WHERE status = 'dead') AS dead_jobs,
                      count(*) FILTER (WHERE status = 'completed') AS completed_jobs,
                      count(*) FILTER (WHERE status = 'cancelled') AS cancelled_jobs,
                      count(*) FILTER (
                        WHERE status IN ('available', 'leased')
                          AND graph_revision <> coalesce(%s, graph_revision)
                      ) AS incompatible_active_jobs,
                      coalesce(max(
                        CASE WHEN status = 'available'
                          THEN greatest(0, extract(epoch FROM (
                            clock_timestamp() - created_at
                          )))
                        END
                      ), 0) AS oldest_available_seconds
                    FROM work_harness_run_jobs
                    """,
                    (graph_revision,),
                )
            ).fetchone()
            return RunQueueStats.model_validate(row)

    async def list_dead_jobs(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RunJob]:
        async with self._pool.connection() as connection:
            rows = await (
                await connection.execute(
                    """
                    SELECT * FROM work_harness_run_jobs
                    WHERE status = 'dead'
                    ORDER BY updated_at DESC, job_id DESC
                    LIMIT %s OFFSET %s
                    """,
                    (limit, offset),
                )
            ).fetchall()
            return [self._job(row) for row in rows]

    async def replay_dead_job(
        self,
        job_id: UUID,
        *,
        capacity: int,
        max_attempts: int,
        graph_revision: str = "0" * 64,
    ) -> RunJob:
        async with self._pool.connection() as connection:
            async with connection.transaction():
                await self._queue_lock(connection)
                row = await (
                    await connection.execute(
                        """
                        SELECT * FROM work_harness_run_jobs
                        WHERE job_id = %s AND status = 'dead' FOR UPDATE
                        """,
                        (job_id,),
                    )
                ).fetchone()
                if not row:
                    raise RunQueueStateError("dead job not found")
                active = await (
                    await connection.execute(
                        "SELECT 1 FROM work_harness_run_jobs "
                        "WHERE run_id = %s AND status IN ('available', 'leased')",
                        (row["run_id"],),
                    )
                ).fetchone()
                if active:
                    raise RunQueueStateError("run already has an active job")
                await self._check_capacity(connection, capacity)
                replay = RunJob(
                    run_id=row["run_id"],
                    kind=row["kind"],
                    payload=row["payload"],
                    priority=row["priority"],
                    max_attempts=max_attempts,
                    graph_revision=graph_revision,
                )
                await self._insert_job(connection, replay)
                await connection.execute(
                    """
                    UPDATE work_harness_run_index
                    SET status = 'pending', cancel_requested_at = NULL,
                        updated_at = clock_timestamp()
                    WHERE run_id = %s
                    """,
                    (row["run_id"],),
                )
                return replay

    async def heartbeat_worker(self, heartbeat: WorkerHeartbeat) -> None:
        async with self._pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO work_harness_worker_heartbeats(
                        worker_id, instance_id, capacity, active_jobs,
                        started_at, last_seen_at, build_version,
                        config_fingerprint
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(worker_id) DO UPDATE SET
                        instance_id = EXCLUDED.instance_id,
                        capacity = EXCLUDED.capacity,
                        active_jobs = EXCLUDED.active_jobs,
                        started_at = EXCLUDED.started_at,
                        last_seen_at = EXCLUDED.last_seen_at,
                        build_version = EXCLUDED.build_version,
                        config_fingerprint = EXCLUDED.config_fingerprint
                    """,
                    (
                        heartbeat.worker_id,
                        heartbeat.instance_id,
                        heartbeat.capacity,
                        heartbeat.active_jobs,
                        heartbeat.started_at,
                        heartbeat.last_seen_at,
                        heartbeat.build_version,
                        heartbeat.config_fingerprint,
                    ),
                )
                await connection.execute(
                    """
                    DELETE FROM work_harness_worker_heartbeats
                    WHERE last_seen_at < clock_timestamp() - interval '24 hours'
                    """
                )

    async def remove_worker(self, worker_id: str) -> None:
        async with self._pool.connection() as connection:
            await connection.execute(
                "DELETE FROM work_harness_worker_heartbeats WHERE worker_id = %s",
                (worker_id,),
            )

    async def list_live_workers(
        self,
        *,
        stale_seconds: float,
    ) -> list[WorkerHeartbeat]:
        async with self._pool.connection() as connection:
            rows = await (
                await connection.execute(
                    """
                    SELECT * FROM work_harness_worker_heartbeats
                    WHERE last_seen_at >= (
                        clock_timestamp() - make_interval(secs => %s)
                    )
                    ORDER BY worker_id ASC
                    """,
                    (stale_seconds,),
                )
            ).fetchall()
            return [WorkerHeartbeat.model_validate(row) for row in rows]

    async def start_event_listener(self, callback: Callable[[str], None]) -> None:
        self._event_callbacks.add(callback)
        if self._event_task is not None:
            return
        try:
            await self._connect_event_listener()
        except Exception:
            self._event_callbacks.discard(callback)
            raise
        self._event_task = asyncio.create_task(
            self._listen_for_run_events(),
            name="work-harness:postgres-run-events",
        )

    async def stop_event_listener(self, callback: Callable[[str], None]) -> None:
        self._event_callbacks.discard(callback)
        if self._event_callbacks or self._event_task is None:
            return
        self._event_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._event_task
        self._event_task = None
        if self._event_connection:
            await self._event_connection.close()
            self._event_connection = None

    async def _listen_for_run_events(self) -> None:
        while self._event_callbacks:
            try:
                if self._event_connection is None or self._event_connection.closed:
                    await self._connect_event_listener()
                connection = self._event_connection
                if connection is None:  # pragma: no cover - guarded by connect.
                    continue
                async for notification in connection.notifies():
                    for callback in tuple(self._event_callbacks):
                        callback(notification.payload)
                if self._event_callbacks:
                    raise ConnectionError("PostgreSQL notification connection closed")
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("run_event_listener_disconnected")
                if self._event_connection:
                    with suppress(Exception):
                        await self._event_connection.close()
                self._event_connection = None
                await asyncio.sleep(EVENT_LISTENER_RETRY_SECONDS)

    async def _connect_event_listener(self) -> None:
        connection = await AsyncConnection.connect(
            self._dsn,
            autocommit=True,
        )
        try:
            await connection.execute("LISTEN work_harness_run_events")
        except Exception:
            await connection.close()
            raise
        self._event_connection = connection

    async def notify_run(self, run_id: str) -> None:
        async with self._pool.connection() as connection:
            await connection.execute(
                "SELECT pg_notify('work_harness_run_events', %s)",
                (run_id,),
            )

    @staticmethod
    async def _queue_lock(connection: Any) -> None:
        await connection.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (QUEUE_ADVISORY_LOCK_ID,),
        )

    @staticmethod
    async def _check_capacity(connection: Any, capacity: int) -> None:
        row = await (
            await connection.execute(
                "SELECT count(*) AS count FROM work_harness_run_jobs WHERE status = 'available'"
            )
        ).fetchone()
        if row["count"] >= capacity:
            raise RunQueueCapacityError("run queue is full")

    @staticmethod
    async def _insert_job(connection: Any, job: RunJob) -> None:
        await connection.execute(
            """
            INSERT INTO work_harness_run_jobs(
                job_id, run_id, kind, status, payload, graph_revision,
                priority, available_at, lease_owner, lease_token,
                lease_expires_at, attempt_count, max_attempts,
                last_error_code, last_error_message, created_at,
                updated_at, completed_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                job.job_id,
                job.run_id,
                str(job.kind),
                str(job.status),
                Jsonb(job.payload),
                job.graph_revision,
                job.priority,
                job.available_at,
                job.lease_owner,
                job.lease_token,
                job.lease_expires_at,
                job.attempt_count,
                job.max_attempts,
                job.last_error_code,
                job.last_error_message,
                job.created_at,
                job.updated_at,
                job.completed_at,
            ),
        )

    @staticmethod
    def _record_values(record: RunRecord) -> tuple[Any, ...]:
        return (
            record.run_id,
            str(record.status),
            record.idempotency_key_hash,
            record.request_fingerprint,
            record.lease_owner,
            record.lease_expires_at,
            record.attempt_count,
            record.created_at,
            record.updated_at,
            Jsonb(record.execution.model_dump(mode="json")) if record.execution else None,
        )

    @staticmethod
    def _record(row: dict[str, Any]) -> RunRecord:
        return RunRecord.model_validate(
            {key: row[key] for key in RunRecord.model_fields if key in row}
        )

    @staticmethod
    def _job(row: dict[str, Any]) -> RunJob:
        return RunJob.model_validate({key: row[key] for key in RunJob.model_fields if key in row})
