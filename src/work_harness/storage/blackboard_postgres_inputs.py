"""Publish a human command and arrange its Worker wakeup in one PostgreSQL transaction."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import uuid4

from work_harness.blackboard.protocol import (
    BlackboardError,
    BoardClosedError,
    CommitReceipt,
    IdempotencyConflictError,
    StaleWriterError,
    WriterToken,
)
from work_harness.domain.enums import RunJobKind, RunJobStatus, RunStatus
from work_harness.storage.blackboard_postgres import Connection, PostgresBlackboardStore, one
from work_harness.storage.postgres import PostgresRunStore
from work_harness.storage.queue import RunJob, RunQueueStateError


async def publish_input(
    database: PostgresBlackboardStore,
    run_id: str,
    *,
    key: str,
    request_digest: str,
    owner: str,
    lease_seconds: float,
    capacity: int,
    max_attempts: int,
    graph_revision: str,
    publish: Callable[[PostgresBlackboardStore, WriterToken], Awaitable[CommitReceipt]],
) -> tuple[CommitReceipt, str]:
    async def transact(connection: Connection) -> tuple[CommitReceipt, str]:
        await PostgresRunStore._queue_lock(connection)
        job_row = await one(
            connection,
            "SELECT * FROM work_harness_run_jobs WHERE run_id = %s "
            "AND status IN ('available', 'leased') ORDER BY job_id FOR UPDATE",
            (run_id,),
        )
        run = await one(
            connection,
            "SELECT * FROM work_harness_run_index WHERE run_id = %s FOR UPDATE",
            (run_id,),
        )
        if run is None or (run["execution"] or {}).get("engine") != "blackboard":
            raise BlackboardError("typed input requires a blackboard run")
        await database.lock_for_write(connection, run_id, execution_write=False)
        board = await database._board(connection, run_id)
        if not board["queue_managed"]:
            raise BlackboardError("distributed input requires a queue-managed blackboard")
        control = await database._control(connection, run_id)
        prior = await database._receipt(connection, run_id, key)
        if prior is not None:
            if prior.request_digest != request_digest:
                raise IdempotencyConflictError("input key was reused with different content")
            return (
                prior,
                "closed"
                if control.finalization
                else "pending"
                if control.wakeup_revision
                else "settled",
            )
        if (
            control.finalization is not None
            or run["cancel_requested_at"] is not None
            or run["status"] == RunStatus.CANCELLED
        ):
            raise BoardClosedError("new input requires an open run")
        if await database._receipt(connection, run_id, "initial-user-input-v1") is None:
            raise BlackboardError("blackboard initialization must complete before human input")
        borrowed = job_row is not None and job_row["status"] == RunJobStatus.LEASED
        if job_row is None:
            await PostgresRunStore._check_capacity(connection, capacity)
            job = RunJob(
                run_id=run_id,
                kind=RunJobKind.RESUME,
                payload={},
                max_attempts=max_attempts,
                graph_revision=graph_revision,
            )
            await PostgresRunStore._insert_job(connection, job)
        else:
            job = PostgresRunStore._job(job_row)
            if job.graph_revision != graph_revision:
                raise RunQueueStateError("input cannot rebind an incompatible job")
        if not borrowed:
            job_row = await one(
                connection,
                "UPDATE work_harness_run_jobs SET status = 'leased', lease_owner = %s, "
                "lease_token = %s, lease_expires_at = clock_timestamp() "
                "+ make_interval(secs => %s), "
                "updated_at = clock_timestamp() WHERE job_id = %s RETURNING *",
                (owner, uuid4(), lease_seconds, job.job_id),
            )
            job = PostgresRunStore._job(job_row)
            await connection.execute(
                "UPDATE work_harness_run_index SET status = 'running', "
                "updated_at = clock_timestamp() WHERE run_id = %s",
                (run_id,),
            )
        # Borrowing a live job is only for this authenticated host command. It does
        # not replace the worker's writer generation or mutate its lease identity.
        await PostgresRunStore.check_job_lease_in_transaction(
            connection, job.lease_ref(), engine="blackboard"
        )
        scoped = database.for_lease(job.lease_ref(), connection=connection)
        try:
            if borrowed:
                if not control.owner_id or not control.session_id:
                    raise StaleWriterError("worker has not initialized its writer yet")
                writer = WriterToken(
                    board_id=run_id,
                    owner_id=control.owner_id,
                    session_id=control.session_id,
                    generation=control.generation,
                )
                await scoped.check_writer(writer)
            else:
                writer = await scoped.claim_writer(run_id, owner, takeover=True)
            receipt = await publish(scoped, writer)
            await PostgresRunStore.check_job_lease_in_transaction(
                connection, job.lease_ref(), engine="blackboard"
            )
            if not borrowed:
                await scoped.release_writer(writer)
                await connection.execute(
                    "UPDATE work_harness_run_jobs SET status = 'available', lease_owner = NULL, "
                    "lease_token = NULL, lease_expires_at = NULL, "
                    "available_at = clock_timestamp(), "
                    "updated_at = clock_timestamp() WHERE job_id = %s",
                    (job.job_id,),
                )
                await connection.execute(
                    "UPDATE work_harness_run_index SET status = 'pending', "
                    "updated_at = clock_timestamp() WHERE run_id = %s",
                    (run_id,),
                )
            return receipt, "active" if borrowed else "scheduled"
        finally:
            await scoped.close()

    return await database.transaction(transact, write=True, execution_write=False)
