"""Execute a leased blackboard Job and reconcile knowledge, queue and checkpoints."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING

from work_harness.blackboard.protocol import BoardClosedError, BoardNotFoundError, StaleWriterError
from work_harness.domain.enums import RunStatus
from work_harness.domain.models import TaskRequest
from work_harness.storage.blackboard_execution_postgres import PostgresActivationStore
from work_harness.storage.blackboard_postgres import PostgresBlackboardStore
from work_harness.storage.queue import RunJob, RunLeaseClosedError, RunLeaseLostError

if TYPE_CHECKING:
    from work_harness.service import RunManager


async def execute_blackboard_job(manager: RunManager, job: RunJob) -> None:
    """A fresh engine pins this attempt's lease and never inherits an older worker's cache."""
    runtime = manager.runtime
    queue = runtime.run_store
    invocation = None
    heartbeat = None
    engine = None
    async with manager._run_slots:
        manager._executing.add(job.run_id)
        try:
            record = await queue.get(job.run_id)
            payload = await queue.get_initial_input(job.run_id)
            if record is None or payload is None:
                raise ValueError("blackboard job has no accepted run input")
            engine = await runtime.blackboard_engine(
                TaskRequest.model_validate(payload), lease=job.lease_ref()
            )
            await manager._check_execution_version(record, engine=engine)
            manager._blackboard_engines[job.run_id] = engine
            store = engine.kernel.store
            if not isinstance(store, PostgresBlackboardStore):
                raise TypeError("distributed blackboard requires PostgreSQL")

            async def advance() -> None:
                try:
                    board = await store.snapshot(job.run_id)
                except BoardNotFoundError:
                    board = None
                if board is not None and board.finalization is not None:
                    await PostgresActivationStore(store).reconcile_terminal(job.run_id)
                    await store.complete_job(observed_revision=board.revision)
                    return
                if job.reconciliation_only:
                    checkpoint = await runtime.checkpointer.aget_tuple(
                        engine.graph_config(job.run_id)
                    )
                    values = checkpoint.checkpoint.get("channel_values", {}) if checkpoint else {}
                    if values.get("status") == "waiting_human":
                        if await store.complete_job(
                            observed_revision=values.get("observed_revision", 0)
                        ):
                            return
                    raise RuntimeError(
                        "job attempts exhausted before a settled blackboard checkpoint"
                    )
                await engine.prepare(job.run_id)
                while True:
                    result = await engine.runner.run(job.run_id, engine.graph_config(job.run_id))
                    control = await store.control(job.run_id)
                    if control.finalization is not None:
                        await PostgresActivationStore(store).reconcile_terminal(job.run_id)
                    elif result.get("status") != "waiting_human":
                        raise RuntimeError(
                            "blackboard stopped without a finalization or human wait"
                        )
                    if await store.complete_job(
                        observed_revision=result.get("observed_revision", 0)
                    ):
                        return
                    # Input committed between the checkpoint and queue acknowledgment.
                    # The same lease continues from the new board, without a lost wakeup.

            invocation = asyncio.create_task(advance(), name=f"{job.run_id}:blackboard-job")
            heartbeat = asyncio.create_task(manager._heartbeat_job(job, invocation))
            await asyncio.wait_for(invocation, timeout=engine.limits.timeout_seconds + 2)
        except (StaleWriterError, RunLeaseLostError):
            # The queue's current owner decides recovery; never rewrite its run status.
            pass
        except (BoardClosedError, RunLeaseClosedError):
            latest = await queue.get(job.run_id)
            if latest is not None and latest.status != RunStatus.CANCELLED:
                # Sealing may have won before a checkpoint write. Keep the finalization
                # recoverable even if this attempt could not finish its acknowledgment.
                await manager._retry_distributed_job(
                    job,
                    RuntimeError("blackboard sealed before queue acknowledgment"),
                    delay_seconds=0,
                )
        except asyncio.CancelledError:
            latest = await queue.get(job.run_id)
            if latest is not None and latest.status != RunStatus.CANCELLED:
                await manager._retry_distributed_job(
                    job,
                    RuntimeError("worker stopped before blackboard completion"),
                    delay_seconds=0,
                )
            raise
        except Exception as exc:
            manager._logger.exception("blackboard_job_failed", extra={"run_id": job.run_id})
            await manager._retry_distributed_job(job, exc)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                outcome = (await asyncio.gather(heartbeat, return_exceptions=True))[0]
                if isinstance(outcome, Exception):
                    manager._logger.error(
                        "blackboard_job_heartbeat_failed",
                        exc_info=outcome,
                        extra={"run_id": job.run_id},
                    )
            if invocation is not None and not invocation.done():
                invocation.cancel()
                with suppress(asyncio.CancelledError):
                    await invocation
            if engine is not None:
                if manager._blackboard_engines.get(job.run_id) is engine:
                    manager._blackboard_engines.pop(job.run_id, None)
                await engine.kernel.store.close()
            await runtime.tool_gateway.clear_run(job.run_id)
            manager._executing.discard(job.run_id)
            manager._publish(job.run_id)
            await manager._notify_distributed_run(job.run_id)
