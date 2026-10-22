"""Queue ownership fences real PostgreSQL knowledge and settlement transactions."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from uuid import uuid4

import pytest
from test_blackboard_execution_storage import LIMITS, Execution, setup
from test_blackboard_settlement import commit, ready
from test_blackboard_storage import SOURCE, hint_delta

from work_harness.blackboard.kernel import BlackboardKernel
from work_harness.blackboard.protocol import BoardClosedError, StaleWriterError
from work_harness.blackboard.verification import VerificationRegistry
from work_harness.domain.enums import RunStatus
from work_harness.knowledge_sources.base import UsageRecord
from work_harness.storage import PostgresRunStore, RunExecutionVersion, RunRecord
from work_harness.storage.blackboard_execution_postgres import PostgresActivationStore
from work_harness.storage.blackboard_postgres import PostgresBlackboardStore
from work_harness.storage.queue import RunJob, RunJobLease, RunQueueStateError


@pytest.mark.parametrize("operation", ("checkpoint", "writes"))
@pytest.mark.parametrize("preemption", ("takeover", "expire_during_write"))
async def test_checkpoints_share_the_queue_fence(leased_board, monkeypatch, operation, preemption):
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from work_harness.storage.blackboard_checkpoint_postgres import LeasedPostgresSaver

    board = leased_board
    config = {"configurable": {"thread_id": "board", "checkpoint_ns": ""}}
    async with AsyncPostgresSaver.from_conn_string(board.root._pool.conninfo) as reader:
        await reader.setup()
        saver = LeasedPostgresSaver(board.execution.store, reader)
        saved = await saver.aput(config, empty_checkpoint(), {"source": "input", "step": -1}, {})
        initial = await reader.aget_tuple(config)
        if preemption == "takeover":
            await board.takeover()
        else:
            method = "aput" if operation == "checkpoint" else "aput_writes"
            original = getattr(AsyncPostgresSaver, method)

            async def expire(self, *args, **kwargs):
                result = await original(self, *args, **kwargs)
                # Same connection: simulate expiry after SQL but before the final fence.
                await self.conn.execute(
                    "UPDATE work_harness_run_jobs SET lease_expires_at = clock_timestamp() "
                    "- interval '1 second' WHERE job_id = %s",
                    (board.job.job_id,),
                )
                return result

            monkeypatch.setattr(AsyncPostgresSaver, method, expire)
        with pytest.raises(StaleWriterError):
            if operation == "checkpoint":
                await saver.aput(config, empty_checkpoint(), {"source": "loop", "step": 0}, {})
            else:
                await saver.aput_writes(saved, [("status", "succeeded")], "late-task")
        assert await reader.aget_tuple(config) == initial
        assert len([checkpoint async for checkpoint in reader.alist(config)]) == 1
        with pytest.raises(StaleWriterError, match="different leased run"):
            await saver.aput(
                {"configurable": {"thread_id": "other", "checkpoint_ns": ""}},
                empty_checkpoint(),
                {},
                {},
            )


@dataclass
class LeasedBoard:
    root: PostgresBlackboardStore
    queue: PostgresRunStore
    job: RunJob
    execution: Execution

    async def expire(self):
        async with self.queue._pool.connection() as conn:
            await conn.execute(
                "UPDATE work_harness_run_jobs SET lease_expires_at = clock_timestamp() "
                "- interval '1 second' WHERE job_id = %s",
                (self.job.job_id,),
            )

    async def takeover(self):
        await self.expire()
        job = await self.queue.claim_job(owner="worker-b", lease_seconds=60)
        assert job and job.job_id == self.job.job_id and job.lease_token != self.job.lease_token
        return job


@pytest.fixture
async def leased_board(postgres_blackboard_factory):
    root = await postgres_blackboard_factory()
    queue = PostgresRunStore(root._pool.conninfo)
    try:
        await queue.setup()
        await queue.submit(
            RunRecord(
                run_id="board",
                execution=RunExecutionVersion(
                    engine="blackboard",
                    workflow_revision="blackboard-v7",
                    schema_version=1,
                    configuration_fingerprint="a" * 64,
                ),
            ),
            {"goal": "Verify the answer"},
            capacity=10,
            max_attempts=3,
        )
        job = await queue.claim_job(owner="worker-a", lease_seconds=60)
        assert job
        scoped = root.for_lease(job.lease_ref())
        execution = await setup(scoped)
        execution.kernel.clock = lambda: execution.now
        yield LeasedBoard(root, queue, job, execution)
        await scoped.close()
    finally:
        await queue.close()


@pytest.mark.parametrize("operation", ("publish", "settle"))
@pytest.mark.parametrize("preemption", ("takeover", "cancel", "expire"))
async def test_preflight_success_cannot_authorize_later_transaction(
    leased_board,
    monkeypatch,
    operation,
    preemption,
):
    board = leased_board
    execution = board.execution
    record = await ready(execution)
    initial = await board.root.snapshot("board")
    original_control = await board.root.control("board")
    before_usage = await execution.ledger.summary("board")
    entered, proceed = asyncio.Event(), asyncio.Event()
    target = execution.store if operation == "publish" else execution.ledger
    method = "commit" if operation == "publish" else "commit_activation"
    original = getattr(target, method)

    async def delayed(*args, **kwargs):
        entered.set()
        await proceed.wait()
        return await original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(target, method, delayed)
        task = asyncio.create_task(
            execution.kernel.commit("board", hint_delta(initial), context=SOURCE)
            if operation == "publish"
            else commit(execution, record)
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
            if preemption == "takeover":
                await board.takeover()
            elif preemption == "cancel":
                await board.queue.cancel_jobs("board")
            else:
                await board.expire()
            # No new writer generation is needed for the queue token to fence the old worker.
            assert await board.root.control("board") == original_control
            proceed.set()
            with pytest.raises(BoardClosedError if preemption == "cancel" else StaleWriterError):
                await asyncio.wait_for(task, 5)
        finally:
            proceed.set()
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
    assert await board.root.snapshot("board") == initial
    assert len(await board.root.events("board")) == 1
    assert await board.root.receipt("board", "first") is None
    assert await board.root.receipt("board", f"{record.candidate.id}:1") is None
    assert await execution.ledger.get(record.candidate.id) == record
    assert await execution.ledger.summary("board") == before_usage


async def test_new_lease_requires_new_board_generation_then_can_settle_saved_result(leased_board):
    board = leased_board
    execution = board.execution
    record = await ready(execution)
    old_writer = await execution.kernel.writer("board")
    new_job = await board.takeover()
    new_store = board.root.for_lease(new_job.lease_ref())
    # A fresh queue token alone cannot reuse the old board writer binding.
    with pytest.raises(StaleWriterError, match="earlier job lease"):
        await new_store.check_writer(old_writer)
    recovered = await setup(new_store, create=False)
    recovered.kernel.clock = lambda: execution.now
    new_writer = await recovered.kernel.writer("board", takeover=True)
    assert new_writer.generation == old_writer.generation + 1
    adopted = await recovered.ledger.adopt(record.ref, new_writer, recovering=True)
    receipt = await commit(recovered, adopted)
    assert (await board.root.snapshot("board")).revision == receipt.board_revision
    assert (await recovered.ledger.summary("board")).reserved_tokens == 0
    with pytest.raises(StaleWriterError):
        await commit(execution, record)
    await execution.store.release_writer(old_writer)
    await new_store.check_writer(new_writer)
    await new_store.close()
    assert (await board.root.snapshot("board")).revision == receipt.board_revision


@pytest.mark.parametrize("revocation", ("takeover", "cancel"))
async def test_late_cost_is_recorded_without_authorizing_any_other_write(leased_board, revocation):
    board = leased_board
    execution = board.execution
    record = await ready(execution, complete=False)
    if revocation == "takeover":
        await board.takeover()
    else:
        await board.queue.cancel_jobs("board")
    before = await board.root.snapshot("board")
    usage = UsageRecord(call_id="late", output_tokens=5, cost_usd=0.005)
    await execution.ledger.record_usage(record.candidate.id, record.attempt, usage)
    await execution.ledger.record_usage(record.candidate.id, record.attempt, usage)
    assert (await execution.ledger.summary("board")).tokens == 15
    assert (await execution.ledger.get(record.candidate.id)).status == "result_ready"
    error = BoardClosedError if revocation == "cancel" else StaleWriterError
    with pytest.raises(error):
        await execution.reserve("forbidden")
    with pytest.raises(error):
        await execution.kernel.put_content("board", "late evidence", media_type="text/plain")
    with pytest.raises(error):
        await execution.ledger.save_output(record.candidate.id, record.attempt, record.output)
    assert await board.root.snapshot("board") == before
    assert len(await board.root.events("board")) == 1


async def test_expiry_after_sql_writes_rolls_back_publication_and_settlement(
    leased_board, monkeypatch
):
    board = leased_board
    execution = board.execution
    record = await ready(execution)
    before = await board.root.snapshot("board")
    summary = await execution.ledger.summary("board")
    original = execution.ledger._save
    async with board.queue._pool.connection() as conn:
        await conn.execute(
            "UPDATE work_harness_run_jobs SET lease_expires_at = clock_timestamp() "
            "+ interval '0.3 seconds' WHERE job_id = %s",
            (board.job.job_id,),
        )
    reached = False

    async def wait_for_expiry(conn, *args):
        nonlocal reached
        await original(conn, *args)
        reached = True
        # The transaction owns the job row. Let database time cross its expiry after
        # publication and ledger updates, without changing the token in another session.
        await conn.execute(
            "SELECT pg_sleep(GREATEST(0, EXTRACT(EPOCH FROM "
            "(lease_expires_at - clock_timestamp()))) + 0.02) "
            "FROM work_harness_run_jobs WHERE job_id = %s",
            (board.job.job_id,),
        )

    with monkeypatch.context() as patch:
        patch.setattr(execution.ledger, "_save", wait_for_expiry)
        with pytest.raises(StaleWriterError):
            await commit(execution, record)
    assert reached
    assert await board.root.snapshot("board") == before
    assert await execution.ledger.summary("board") == summary
    assert await execution.ledger.get(record.candidate.id) == record
    assert await board.root.receipt("board", f"{record.candidate.id}:1") is None


@pytest.mark.parametrize(
    "mutation",
    (
        "cancel_marker",
        "terminal",
        "wrong_owner",
        "wrong_token",
        "wrong_run",
        "wrong_job",
        "wrong_engine",
        "reconcile_only",
    ),
)
async def test_each_lease_dimension_is_checked_against_database(leased_board, mutation):
    board = leased_board
    execution = board.execution
    writer = await execution.kernel.writer("board")
    lease = board.job.lease_ref()
    if mutation in {"cancel_marker", "terminal", "reconcile_only", "wrong_engine"}:
        async with board.queue._pool.connection() as conn:
            if mutation == "cancel_marker":
                await conn.execute(
                    "UPDATE work_harness_run_index SET cancel_requested_at = clock_timestamp() "
                    "WHERE run_id = 'board'"
                )
            elif mutation == "terminal":
                await conn.execute(
                    "UPDATE work_harness_run_index SET status = 'succeeded' WHERE run_id = 'board'"
                )
            elif mutation == "wrong_engine":
                await conn.execute(
                    "UPDATE work_harness_run_index SET execution = "
                    "jsonb_set(execution, '{engine}', '\"legacy\"') WHERE run_id = 'board'"
                )
            else:
                await conn.execute(
                    "UPDATE work_harness_run_jobs SET last_error_code = 'queue_reconcile_only' "
                    "WHERE run_id = 'board'"
                )
    else:
        field, value = {
            "wrong_owner": ("owner", "intruder"),
            "wrong_token": ("token", uuid4()),
            "wrong_run": ("run_id", "another"),
            "wrong_job": ("job_id", uuid4()),
        }[mutation]
        lease = RunJobLease.model_validate({**lease.model_dump(), field: value})
    scoped = board.root.for_lease(lease)
    kernel = BlackboardKernel(scoped, execution.kernel.verification)
    initial = await board.root.snapshot("board")
    with pytest.raises((BoardClosedError, StaleWriterError)):
        await kernel.commit("board", hint_delta(initial), context=SOURCE, writer=writer)
    assert await board.root.snapshot("board") == initial


async def test_unscoped_store_cannot_bypass_lease_but_can_seal_durable_cancellation(leased_board):
    board = leased_board
    execution = board.execution
    writer = await execution.kernel.writer("board")
    root_kernel = BlackboardKernel(board.root, VerificationRegistry())
    with pytest.raises(StaleWriterError, match="requires a job lease"):
        await board.root.claim_writer("board", "unscoped", takeover=True)
    with pytest.raises(StaleWriterError, match="requires a job lease"):
        await board.root.put_content("board", "unfenced", media_type="text/plain", writer=writer)
    with pytest.raises(StaleWriterError, match="requires a job lease"):
        await PostgresActivationStore(board.root).start_run("board", LIMITS)
    with pytest.raises(RunQueueStateError, match="not been persisted"):
        await root_kernel.cancel("board")
    assert (await board.root.snapshot("board")).revision == 1
    await board.queue.cancel_jobs("board")
    final = await root_kernel.cancel("board")
    assert final.status == "cancelled"
    assert (await board.root.control("board")).status_pending
    assert (await board.queue.get("board")).status == RunStatus.CANCELLED
    with pytest.raises(BoardClosedError):
        await execution.kernel.put_content("board", "late", media_type="text/plain")
    assert await root_kernel.cancel("board") == final
    await board.root.acknowledge_finalization("board", final.finalized_at_revision)
    assert await board.root.pending_reconciliations() == ()


async def test_queue_cancel_waits_for_publication_transaction_lock(leased_board, monkeypatch):
    board = leased_board
    execution = board.execution
    record = await ready(execution)
    entered, release = asyncio.Event(), asyncio.Event()
    original = execution.ledger._save
    original_queue_lock = board.queue._queue_lock
    cancel_pid = None

    async def identify_cancel(connection):
        nonlocal cancel_pid
        cancel_pid = (
            await (await connection.execute("SELECT pg_backend_pid() AS pid")).fetchone()
        )["pid"]
        await original_queue_lock(connection)

    async def held(*args):
        await original(*args)
        entered.set()
        await release.wait()

    with monkeypatch.context() as patch:
        patch.setattr(execution.ledger, "_save", held)
        patch.setattr(board.queue, "_queue_lock", identify_cancel)
        publication = asyncio.create_task(commit(execution, record))
        cancellation = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            cancellation = asyncio.create_task(board.queue.cancel_jobs("board"))

            # Observe an actual database lock wait, not just an arbitrary sleep.
            async def waiting():
                for _ in range(200):
                    async with board.root._pool.connection() as conn:
                        row = await (
                            await conn.execute(
                                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                                "WHERE wait_event_type = 'Lock' AND pid = %s) AS waiting",
                                (cancel_pid,),
                            )
                        ).fetchone()
                    if row["waiting"]:
                        return
                    await asyncio.sleep(0.01)
                raise AssertionError("cancel did not wait on the publication transaction")

            await asyncio.wait_for(waiting(), 5)
            assert not cancellation.done()
            release.set()
            receipt = await asyncio.wait_for(publication, 5)
            assert await asyncio.wait_for(cancellation, 5) == 1
        finally:
            release.set()
            for task in (publication, cancellation):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
    assert (await board.root.snapshot("board")).revision == receipt.board_revision
    assert (await execution.ledger.get(record.candidate.id)).status == "completed"
    assert (await execution.ledger.summary("board")).reserved_tokens == 0
    with pytest.raises(BoardClosedError):
        await execution.kernel.writer("board", takeover=True)


async def test_sealing_wins_cancel_without_rewriting_immutable_terminal_state(
    leased_board, monkeypatch
):
    board = leased_board
    execution = board.execution
    await execution.kernel.writer("board")
    entered, release = asyncio.Event(), asyncio.Event()
    original = execution.store._save_control

    async def pause_seal(conn, control):
        await original(conn, control)
        if control.finalization is not None:
            entered.set()
            await release.wait()

    with monkeypatch.context() as patch:
        patch.setattr(execution.store, "_save_control", pause_seal)
        task = asyncio.create_task(
            execution.kernel.finalize("board", status="partial", reason="no_more_evidence")
        )
        cancellation = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            cancellation = asyncio.create_task(board.queue.cancel_jobs("board"))
            release.set()
            final = await asyncio.wait_for(task, 5)
            assert await asyncio.wait_for(cancellation, 5) == 0
        finally:
            release.set()
            for pending in (task, cancellation):
                if pending is not None and not pending.done():
                    pending.cancel()
                    with suppress(asyncio.CancelledError):
                        await pending
    assert (await board.root.snapshot("board")).finalization == final
    assert (await board.queue.get("board")).status == RunStatus.RUNNING
    assert await board.queue.complete_job(
        board.job.job_id,
        owner=board.job.lease_owner,
        lease_token=board.job.lease_token,
        run_status=RunStatus.PARTIAL,
    )
    assert (await board.queue.get("board")).status == RunStatus.PARTIAL


async def test_cancel_first_prevents_sealing_as_success_or_partial(leased_board):
    board = leased_board
    await board.queue.cancel_jobs("board")
    with pytest.raises(BoardClosedError):
        await board.execution.kernel.finalize("board", status="partial", reason="late_result")
    assert (await board.root.snapshot("board")).finalization is None
    assert (await board.root.snapshot("board")).revision == 1


async def test_sealed_board_blocks_ledger_progress_even_before_queue_acknowledgment(leased_board):
    board = leased_board
    execution = board.execution
    record = await ready(execution)
    await execution.kernel.finalize("board", status="partial", reason="enough_exploration")
    assert (await board.queue.get("board")).status == RunStatus.RUNNING
    with pytest.raises(BoardClosedError):
        await execution.ledger.save_output(record.candidate.id, record.attempt, record.output)
    with pytest.raises(BoardClosedError):
        await execution.ledger.start_run("board", LIMITS)
    await execution.ledger.record_usage(
        record.candidate.id, record.attempt, UsageRecord(call_id="late", output_tokens=1)
    )
    assert (await execution.ledger.summary("board")).tokens == 11
    assert (await execution.ledger.get(record.candidate.id)).status == "result_ready"


async def test_managed_marker_and_writer_binding_survive_new_client(
    leased_board, postgres_blackboard_factory
):
    board = leased_board
    writer = await board.execution.kernel.writer("board")
    fresh = await postgres_blackboard_factory()
    with pytest.raises(StaleWriterError, match="requires a job lease"):
        await fresh.check_writer(writer)
    bound = fresh.for_lease(board.job.lease_ref())
    await bound.check_writer(writer)
    new_job = await board.takeover()
    with pytest.raises(StaleWriterError):
        await bound.check_writer(writer)
    replacement = fresh.for_lease(new_job.lease_ref())
    with pytest.raises(StaleWriterError, match="earlier job lease"):
        await replacement.check_writer(writer)


async def test_resume_job_cannot_reuse_writer_bound_to_previous_job(leased_board):
    board = leased_board
    writer = await board.execution.kernel.writer("board")
    assert await board.queue.complete_job(
        board.job.job_id,
        owner=board.job.lease_owner,
        lease_token=board.job.lease_token,
        run_status=RunStatus.WAITING_HUMAN,
    )
    await board.queue.enqueue_resume("board", {}, capacity=10, max_attempts=3)
    resume = await board.queue.claim_job(owner="worker-b", lease_seconds=60)
    assert resume and resume.job_id != board.job.job_id
    scoped = board.root.for_lease(resume.lease_ref())
    with pytest.raises(StaleWriterError, match="earlier job lease"):
        await scoped.check_writer(writer)
    current = await scoped.claim_writer("board", "next-controller", takeover=True)
    kernel = BlackboardKernel(scoped, board.execution.kernel.verification)
    initial = await scoped.snapshot("board")
    await kernel.commit("board", hint_delta(initial), context=SOURCE, writer=current)
    with pytest.raises(StaleWriterError):
        await board.execution.store.check_writer(writer)
