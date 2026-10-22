"""Distributed host commands; API processes publish inputs but never run knowledge sources."""

from __future__ import annotations

from typing import TYPE_CHECKING

from work_harness.blackboard.inputs import HumanInput, HumanInputResult, prior_input, submit_input
from work_harness.blackboard.kernel import BlackboardKernel
from work_harness.blackboard.models import IntentContent
from work_harness.blackboard.protocol import BlackboardError
from work_harness.blackboard.verification import VerificationRegistry
from work_harness.domain.enums import RunStatus
from work_harness.domain.models import TaskRequest
from work_harness.storage.blackboard_postgres import PostgresBlackboardStore
from work_harness.storage.blackboard_postgres_inputs import publish_input
from work_harness.storage.blackboard_postgres_reconcile import reconcile_terminal
from work_harness.storage.queue import RunQueueStateError

if TYPE_CHECKING:
    from work_harness.service import RunManager


async def reconcile_distributed_run(manager: RunManager, run_id: str) -> bool:
    runtime = manager.runtime
    request = TaskRequest.model_validate(await runtime.run_store.get_initial_input(run_id))
    if request.blackboard is None:
        raise BlackboardError("blackboard run is missing its accepted goal")
    store = await runtime.blackboard_store()
    if not isinstance(store, PostgresBlackboardStore):
        raise BlackboardError("distributed reconciliation requires PostgreSQL")
    reconciled = await reconcile_terminal(
        store,
        run_id,
        accepted_goal=IntentContent(
            objective=request.goal,
            topic=request.blackboard.topic,
            acceptance=request.blackboard.acceptance,
        ),
    )
    if reconciled:
        manager._publish(run_id)
        await manager._notify_distributed_run(run_id)
    return reconciled


async def cancel_distributed_run(manager: RunManager, run_id: str) -> None:
    await manager.runtime.run_store.cancel_jobs(run_id)
    await reconcile_distributed_run(manager, run_id)
    await manager.runtime.tool_gateway.clear_run(run_id)


async def reconcile_distributed_runs(manager: RunManager) -> None:
    for run_id in await manager.runtime.run_store.blackboard_reconciliations(
        limit=manager.runtime.config.runtime.recovery_scan_limit
    ):
        try:
            await reconcile_distributed_run(manager, run_id)
        except Exception:
            manager._logger.exception("blackboard_reconciliation_failed", extra={"run_id": run_id})


async def submit_distributed_input(
    manager: RunManager, run_id: str, request: HumanInput
) -> HumanInputResult:
    runtime = manager.runtime
    if runtime.config.runtime.execution.role == "worker":
        raise BlackboardError("worker role cannot accept API input")
    record = await runtime.run_store.get(run_id)
    if record is None or record.engine != "blackboard":
        raise BlackboardError("typed input requires an existing blackboard run")
    store = await runtime.blackboard_store()
    if not isinstance(store, PostgresBlackboardStore):
        raise BlackboardError("distributed input requires PostgreSQL")
    prior = await prior_input(BlackboardKernel(store, VerificationRegistry()), run_id, request)
    if prior is not None:
        return HumanInputResult(
            receipt=prior, wakeup=await resume_distributed_input(manager, run_id)
        )
    payload = await runtime.run_store.get_initial_input(run_id)
    accepted = TaskRequest.model_validate(payload)

    async def publish(scoped, writer):
        engine = await runtime.blackboard_engine(accepted, store=scoped)
        await manager._check_execution_version(record, engine=engine)
        return await submit_input(
            engine.kernel,
            run_id,
            request,
            writer=writer,
            max_objects=engine.limits.max_objects,
            max_child_intents=engine.limits.max_child_intents,
        )

    config = runtime.config.runtime
    try:
        receipt, wakeup = await publish_input(
            store,
            run_id,
            key=request.storage_key,
            request_digest=request.request_digest,
            owner=manager._owner,
            lease_seconds=config.run_lease_seconds,
            capacity=config.run_queue_capacity,
            max_attempts=config.execution.job_max_attempts,
            graph_revision=manager.execution_fingerprint,
            publish=publish,
        )
    except RunQueueStateError as exc:
        raise BlackboardError(str(exc)) from exc
    manager._publish(run_id)
    await manager._notify_distributed_run(run_id)
    return HumanInputResult(receipt=receipt, wakeup=wakeup)


async def resume_distributed_input(manager: RunManager, run_id: str) -> str:
    runtime = manager.runtime
    record = await runtime.run_store.get(run_id)
    if record is None or record.engine != "blackboard":
        raise BlackboardError("continuation requires an existing blackboard run")
    control = await (await runtime.blackboard_store()).control(run_id)
    if control.finalization is not None:
        return "closed"
    if not control.wakeup_revision:
        return "settled"
    # Normal acceptance persisted its queue wakeup in the very same transaction.
    # This fallback also repairs a waiting index with a durable, uncovered input.
    if record.status == RunStatus.WAITING_HUMAN:
        await manager._check_execution_version(record)
        config = runtime.config.runtime
        try:
            await runtime.run_store.enqueue_resume(
                run_id,
                {},
                capacity=config.run_queue_capacity,
                max_attempts=config.execution.job_max_attempts,
                graph_revision=manager.execution_fingerprint,
            )
        except RunQueueStateError:
            pass  # Another API/Worker already owns the durable wakeup.
        await manager._notify_distributed_run(run_id)
        return "scheduled"
    return "active" if record.status == RunStatus.RUNNING else "pending"
