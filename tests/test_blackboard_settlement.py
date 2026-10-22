"""Knowledge publication and accepted-activation settlement share a transaction."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import suppress
from datetime import timedelta

import pytest
from test_blackboard_execution_storage import LIMITS, Execution, setup

from work_harness.blackboard.controller import BlackboardController
from work_harness.blackboard.execution import ActivationRef, LedgerState, MemoryActivationStore
from work_harness.blackboard.protocol import BlackboardError, BoardClosedError, StaleWriterError
from work_harness.knowledge_sources.base import KnowledgeOutput, UsageRecord
from work_harness.knowledge_sources.registry import KnowledgeSourceRegistry
from work_harness.storage.blackboard_execution_sqlite import SQLiteActivationStore
from work_harness.storage.blackboard_memory import MemoryBlackboardStore
from work_harness.storage.blackboard_sqlite import SQLiteBlackboardStore


class Crash(BaseException):
    pass


@pytest.fixture
async def execution(blackboard_store):
    execution = await setup(blackboard_store)
    execution.ledger.bind(blackboard_store)
    execution.kernel.clock = lambda: execution.now
    return execution


async def ready(execution: Execution, *, complete=True, empty=False):
    record = await execution.reserve()
    writer = await execution.kernel.writer("board")
    await execution.ledger.adopt(record.ref, writer)
    await execution.ledger.record_usage(
        record.candidate.id,
        record.attempt,
        UsageRecord(call_id="observed", input_tokens=10, cost_usd=0.01),
    )
    output = KnowledgeOutput(
        proposals=() if empty else (await execution.fact(record),),
        usage_complete=complete,
        reason="verified" if not empty else "no evidence",
    )
    await execution.ledger.save_output(
        record.candidate.id,
        record.attempt,
        output,
        writer=writer,
        now=execution.now,
    )
    return await execution.ledger.get(record.candidate.id, record.attempt)


async def commit(execution: Execution, record):
    delta, context = record.proposal(LIMITS)
    return await execution.kernel.commit(
        "board",
        delta,
        context=context,
        settlement=execution.ledger.settlement(record.ref, execution.kernel.clock),
    )


@pytest.mark.parametrize("empty", (False, True))
async def test_completion_and_reservation_release_are_idempotent(execution, empty):
    record = await ready(execution, empty=empty)
    before = await execution.ledger.summary("board")
    assert before.tokens == 10 and before.reserved_tokens == 0
    assert before.reserved_cost_usd == 0
    receipt = await commit(execution, record)
    settled = await execution.ledger.get(record.candidate.id)
    summary = await execution.ledger.summary("board")
    assert settled.status == ("no_progress" if empty else "completed")
    assert summary.tokens == 10 and summary.cost_usd == 0.01
    assert summary.reserved_tokens == 0 and summary.reserved_cost_usd == 0
    assert summary.no_progress == int(empty)
    events = await execution.store.events("board")
    assert await commit(execution, record) == receipt
    assert await execution.ledger.summary("board") == summary
    assert await execution.store.events("board") == events


async def test_unknown_usage_survives_commit_and_releases_only_after_reconciliation(execution):
    record = await ready(execution, complete=False)
    await commit(execution, record)
    assert (await execution.ledger.get(record.candidate.id)).status == "completed"
    assert (await execution.ledger.summary("board")).reserved_tokens == 0
    late = UsageRecord(call_id="late", output_tokens=5, cost_usd=0.005)
    await execution.ledger.record_usage(record.candidate.id, record.attempt, late)
    await execution.ledger.record_usage(record.candidate.id, record.attempt, late)
    summary = await execution.ledger.summary("board")
    assert summary.tokens == 15 and summary.reserved_tokens == 0
    assert summary.reserved_cost_usd == 0
    await execution.ledger.complete_usage(record.candidate.id, record.attempt)
    assert (await execution.ledger.summary("board")).reserved_tokens == 0


async def test_failure_after_prepared_settlement_rolls_back_both_sides(execution, monkeypatch):
    record = await ready(execution)
    board = await execution.store.snapshot("board")
    summary = await execution.ledger.summary("board")
    original = LedgerState.finish

    def crash(state, *args, **kwargs):
        original(state, *args, **kwargs)
        raise Crash("completion prepared")

    with monkeypatch.context() as patch:
        patch.setattr(LedgerState, "finish", crash)
        with pytest.raises(Crash):
            await commit(execution, record)
    assert await execution.store.snapshot("board") == board
    assert len(await execution.store.events("board")) == 1
    assert await execution.store.receipt("board", f"{record.candidate.id}:1") is None
    assert await execution.ledger.get(record.candidate.id) == record
    assert await execution.ledger.summary("board") == summary
    await commit(execution, record)
    assert (await execution.ledger.summary("board")).tokens == 10
    assert (await execution.ledger.get(record.candidate.id)).status == "completed"


@pytest.mark.parametrize("empty", (False, True))
async def test_lost_commit_response_reuses_completion_and_receipt(execution, monkeypatch, empty):
    record = await ready(execution, empty=empty)
    original = execution.ledger.commit_activation

    async def crash(*args, **kwargs):
        await original(*args, **kwargs)
        raise Crash("response lost")

    with monkeypatch.context() as patch:
        patch.setattr(execution.ledger, "commit_activation", crash)
        with pytest.raises(Crash):
            await commit(execution, record)
    receipt = await execution.store.receipt("board", f"{record.candidate.id}:1")
    assert receipt is not None
    summary = await execution.ledger.summary("board")
    assert summary.no_progress == int(empty) and summary.reserved_tokens == 0
    assert await commit(execution, record) == receipt
    assert await execution.ledger.summary("board") == summary
    assert len(await execution.store.events("board")) == 2


@pytest.mark.parametrize("changed", ("source", "limits", "inputs", "key", "attempt"))
async def test_settlement_is_bound_to_exact_persisted_proposal(execution, changed):
    record = await ready(execution)
    delta, context = record.proposal(LIMITS)
    ref = record.ref
    if changed == "source":
        context = context.model_copy(update={"source_id": "forged"})
    elif changed == "limits":
        context = context.model_copy(update={"max_objects": 10000})
    elif changed == "inputs":
        delta = delta.model_copy(update={"preconditions": ()})
    elif changed == "key":
        delta = delta.model_copy(update={"idempotency_key": "unbound"})
    else:
        ref = ActivationRef(activation_id=ref.activation_id, attempt=2)
    with pytest.raises(BlackboardError, match="persisted activation|unknown activation"):
        await execution.kernel.commit(
            "board",
            delta,
            context=context,
            settlement=execution.ledger.settlement(ref, execution.kernel.clock),
        )
    assert (await execution.store.snapshot("board")).revision == 1
    assert await execution.ledger.get(record.candidate.id) == record
    assert (await execution.ledger.summary("board")).reserved_tokens == 0


async def test_stopped_attempt_cannot_publish_or_settle_as_a_new_attempt(execution):
    old = await ready(execution)
    await execution.ledger.finish(
        old.candidate.id,
        attempt=old.attempt,
        status="retryable",
        reason="retry",
        progress=False,
        writer=old.writer,
        now=execution.now,
    )
    new = await execution.reserve()
    assert new.attempt == 2
    with pytest.raises(BlackboardError, match="outdated"):
        await commit(execution, old)
    assert (await execution.store.snapshot("board")).revision == 1
    assert (await execution.ledger.get(old.candidate.id)).status == "running"


async def test_late_output_cannot_bypass_deadline_via_atomic_commit(execution):
    record = await execution.reserve()
    writer = await execution.kernel.writer("board")
    await execution.ledger.adopt(record.ref, writer)
    await execution.ledger.save_output(
        record.candidate.id,
        record.attempt,
        KnowledgeOutput(proposals=(await execution.fact(record),)),
        writer=writer,
        now=record.deadline,
    )
    record = await execution.ledger.get(record.candidate.id)
    with pytest.raises(BlackboardError, match="deadline"):
        await commit(execution, record)
    assert (await execution.store.snapshot("board")).revision == 1


@pytest.mark.parametrize("preemption", ("cancel", "takeover"))
async def test_writer_fence_is_checked_inside_combined_transaction(
    execution, monkeypatch, preemption
):
    record = await ready(execution)
    before = await execution.store.snapshot("board")
    entered, proceed = asyncio.Event(), asyncio.Event()
    original = execution.ledger.commit_activation

    async def delayed(*args, **kwargs):
        entered.set()
        await proceed.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(execution.ledger, "commit_activation", delayed)
    task = asyncio.create_task(commit(execution, record))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if preemption == "cancel":
            await execution.kernel.cancel("board")
        else:
            await execution.store.claim_writer("board", "replacement", takeover=True)
        proceed.set()
        with pytest.raises(BoardClosedError if preemption == "cancel" else StaleWriterError):
            await asyncio.wait_for(task, 5)
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    assert (await execution.store.snapshot("board")).objects == before.objects
    assert await execution.store.receipt("board", f"{record.candidate.id}:1") is None
    assert await execution.ledger.get(record.candidate.id) == record
    assert (await execution.ledger.summary("board")).tokens == 10
    assert (await execution.ledger.summary("board")).reserved_tokens == 0


async def test_sqlite_failure_after_ledger_write_rolls_back_and_reopens(tmp_path):
    path = tmp_path / "board.sqlite"
    first = SQLiteBlackboardStore(path)
    await first.initialize()
    try:
        execution = await setup(first)
        record = await ready(execution)

        def fail_completion(connection):
            connection.execute("""CREATE TRIGGER reject_completion
                AFTER UPDATE ON board_activation_attempts
                WHEN json_extract(NEW.payload, '$.status') = 'completed'
                BEGIN SELECT RAISE(ABORT, 'completion failure'); END""")

        await first.transaction(fail_completion, write=True)
        with pytest.raises(sqlite3.IntegrityError, match="completion failure"):
            await commit(execution, record)
    finally:
        await first.close()
    second = SQLiteBlackboardStore(path)
    await second.initialize()
    try:
        recovered = await setup(second, create=False)
        assert (await second.snapshot("board")).revision == 1
        assert len(await second.events("board")) == 1
        assert await second.receipt("board", f"{record.candidate.id}:1") is None
        assert await recovered.ledger.get(record.candidate.id) == record
        summary = await recovered.ledger.summary("board")
        assert summary.tokens == 10 and summary.reserved_tokens == 0
        await second.transaction(
            lambda connection: connection.execute("DROP TRIGGER reject_completion"),
            write=True,
        )
        writer = await recovered.kernel.writer("board", takeover=True)
        record = await recovered.ledger.adopt(record.ref, writer, recovering=True)
        # The output completed in time; reopening later must not require executing it again.
        recovered.kernel.clock = lambda: record.deadline + timedelta(seconds=1)
        await commit(recovered, record)
        assert (await recovered.ledger.get(record.candidate.id)).status == "completed"
        assert (await recovered.ledger.summary("board")).reserved_tokens == 0
    finally:
        await second.close()


async def test_sqlite_two_handles_observe_one_atomic_outcome(tmp_path):
    first = SQLiteBlackboardStore(tmp_path / "board.sqlite")
    second = SQLiteBlackboardStore(first.path)
    await first.initialize()
    await second.initialize()
    try:
        execution = await setup(first)
        record = await ready(execution, empty=True)
        execution.ledger = SQLiteActivationStore(second)
        receipts = await asyncio.gather(commit(execution, record), commit(execution, record))
        assert receipts[0] == receipts[1]
        assert (await first.snapshot("board")).revision == 2
        assert len(await second.events("board")) == 2
        summary = await SQLiteActivationStore(first).summary("board")
        assert summary.tokens == 10 and summary.reserved_tokens == 0 and summary.no_progress == 1
    finally:
        await second.close()
        await first.close()


async def test_memory_ledger_handles_share_completion_and_budget():
    store = MemoryBlackboardStore()
    execution = await setup(store)
    execution.ledger.bind(store)
    record = await ready(execution, empty=True)
    first = execution.ledger
    second = MemoryActivationStore(store)
    assert await second.get(record.candidate.id) == record
    execution.ledger = second
    receipts = await asyncio.gather(commit(execution, record), commit(execution, record))
    assert receipts[0] == receipts[1]
    assert (await first.get(record.candidate.id)).status == "no_progress"
    assert await first.summary("board") == await second.summary("board")
    assert (await first.summary("board")).reserved_tokens == 0
    assert (await first.summary("board")).no_progress == 1
    assert len(await store.events("board")) == 2


async def test_controller_rejects_incompatible_transaction_backends(tmp_path):
    memory = await setup(MemoryBlackboardStore())
    database = SQLiteBlackboardStore(tmp_path / "one.sqlite")
    await database.initialize()
    sqlite = await setup(database)
    other = SQLiteBlackboardStore(tmp_path / "other.sqlite")
    try:
        registry = KnowledgeSourceRegistry()
        for kernel, ledger in (
            (memory.kernel, sqlite.ledger),
            (sqlite.kernel, MemoryActivationStore()),
            (sqlite.kernel, SQLiteActivationStore(other)),
            (memory.kernel, MemoryActivationStore(MemoryBlackboardStore())),
        ):
            with pytest.raises(BlackboardError, match="same memory|share the blackboard"):
                BlackboardController(kernel, registry, ledger=ledger)
        assert isinstance(
            BlackboardController(sqlite.kernel, registry).ledger, SQLiteActivationStore
        )
    finally:
        await sqlite.store.close()
