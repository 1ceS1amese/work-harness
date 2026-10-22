"""Real PostgreSQL contracts for shared clients, rollback, isolation and migration."""

from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest
from psycopg.errors import DivisionByZero
from test_blackboard_execution_storage import LIMITS, setup
from test_blackboard_settlement import commit, ready
from test_blackboard_storage import SOURCE, create_board, hint_delta

from work_harness.blackboard.controller import BlackboardController
from work_harness.blackboard.execution import ActivationRecord, BudgetExceeded
from work_harness.blackboard.kernel import BlackboardKernel
from work_harness.blackboard.protocol import (
    BlackboardError,
    CommitReceipt,
    StaleSnapshotError,
    StaleWriterError,
)
from work_harness.knowledge_sources.registry import KnowledgeSourceRegistry
from work_harness.storage import PostgresRunStore, PostgresSchemaError
from work_harness.storage import blackboard_postgres_migrations as migrations
from work_harness.storage.blackboard_execution_postgres import (
    PostgresActivationStore,
    PostgresVerificationReceiptStore,
)
from work_harness.storage.blackboard_memory import MemoryBlackboardStore
from work_harness.storage.blackboard_postgres import one


@pytest.mark.parametrize("same_request", (False, True))
async def test_two_clients_serialize_cas_and_idempotency(postgres_blackboard_factory, same_request):
    first = await postgres_blackboard_factory()
    second = await postgres_blackboard_factory()
    kernel, initial = await create_board(first)
    writer = await kernel.writer("board")
    other = BlackboardKernel(second, kernel.verification)
    results = await asyncio.gather(
        kernel.commit("board", hint_delta(initial), context=SOURCE),
        other.commit(
            "board",
            hint_delta(initial, "first" if same_request else "second"),
            context=SOURCE,
            writer=writer,
        ),
        return_exceptions=True,
    )
    if same_request:
        assert isinstance(results[0], CommitReceipt) and results[0] == results[1]
    else:
        assert sum(isinstance(result, StaleSnapshotError) for result in results) == 1
        assert sum(isinstance(result, CommitReceipt) for result in results) == 1
    assert [event.sequence for event in await second.events("board")] == [1, 2]
    assert len((await second.snapshot("board")).objects) == 2


async def test_live_owner_requires_explicit_takeover_and_close_cannot_revoke_it(
    postgres_blackboard_factory,
):
    first = await postgres_blackboard_factory()
    second = await postgres_blackboard_factory()
    kernel, initial = await create_board(first)
    original = await kernel.writer("board")
    with pytest.raises(StaleWriterError):
        await second.claim_writer("board", original.owner_id)
    current = await second.claim_writer("board", "next", takeover=True)
    assert current.generation == original.generation + 1
    with pytest.raises(StaleWriterError):
        await kernel.commit("board", hint_delta(initial), context=SOURCE)
    await first.release_writer(original)
    await first.close()
    await second.check_writer(current)
    recovered = BlackboardKernel(second, kernel.verification)
    await recovered.commit("board", hint_delta(initial), context=SOURCE, writer=current)
    with pytest.raises(StaleWriterError):
        await second.check_writer(original)


async def test_shared_activation_limit_cannot_be_exceeded(postgres_blackboard_factory):
    first = await postgres_blackboard_factory()
    second = await postgres_blackboard_factory()
    execution = await setup(first)
    left = await execution.candidate("one")
    right = await execution.candidate("two")
    limits = LIMITS.model_copy(update={"max_activations": 1})
    results = await asyncio.gather(
        execution.ledger.reserve(*left, limits),
        PostgresActivationStore(second).reserve(*right, limits),
        return_exceptions=True,
    )
    assert sum(isinstance(result, BudgetExceeded) for result in results) == 1
    assert sum(isinstance(result, ActivationRecord) for result in results) == 1
    assert (await PostgresActivationStore(second).summary("board")).reserved_tokens == 0


@pytest.mark.parametrize("cancel", (False, True))
async def test_settlement_failure_rolls_back_every_write_and_releases_lock(
    postgres_blackboard_factory,
    monkeypatch,
    cancel,
):
    first = await postgres_blackboard_factory()
    second = await postgres_blackboard_factory()
    execution = await setup(first)
    execution.kernel.clock = lambda: execution.now
    record = await ready(execution)
    before = await second.snapshot("board")
    summary = await PostgresActivationStore(second).summary("board")
    entered, release = asyncio.Event(), asyncio.Event()
    original_save = execution.ledger._save

    async def fail_after_save(*args):
        await original_save(*args)
        entered.set()
        await release.wait()
        raise RuntimeError("after ledger write")

    with monkeypatch.context() as patch:
        patch.setattr(execution.ledger, "_save", fail_after_save)
        task = asyncio.create_task(commit(execution, record))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            # Another client observes the complete old state even after all SQL writes ran.
            assert await second.snapshot("board") == before
            assert await PostgresActivationStore(second).summary("board") == summary
            if cancel:
                task.cancel()
            else:
                release.set()
            with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
                await asyncio.wait_for(task, 5)
        finally:
            release.set()
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
    assert await second.snapshot("board") == before
    assert await second.receipt("board", f"{record.candidate.id}:1") is None
    assert await PostgresActivationStore(second).get(record.candidate.id) == record
    assert await PostgresActivationStore(second).summary("board") == summary
    receipt = await asyncio.wait_for(commit(execution, record), 5)
    assert (await second.snapshot("board")).revision == receipt.board_revision
    assert (await PostgresActivationStore(second).summary("board")).reserved_tokens == 0


async def test_restart_preserves_receipts_contents_history_ledger_and_usage(
    postgres_blackboard_factory,
):
    first = await postgres_blackboard_factory()
    execution = await setup(first)
    execution.kernel.clock = lambda: execution.now
    record = await ready(execution)
    attachment = await execution.kernel.put_content("board", "原始证据 42", media_type="text/plain")
    receipt = await commit(execution, record)
    board = await first.snapshot("board")
    events = await first.events("board")
    summary = await execution.ledger.summary("board")
    verification_id = record.output.proposals[0].verification_receipt_id
    verification = await PostgresVerificationReceiptStore(first).get("board", verification_id)
    await first.close()
    reopened = await postgres_blackboard_factory()
    assert await reopened.snapshot("board") == board
    assert (await reopened.snapshot("board", revision=1)).revision == 1
    assert await reopened.events("board") == events
    assert await reopened.receipt("board", f"{record.candidate.id}:1") == receipt
    assert await PostgresActivationStore(reopened).summary("board") == summary
    assert (
        await PostgresVerificationReceiptStore(reopened).get("board", verification_id)
        == verification
    )
    assert (await reopened.content("board", attachment)).body == "原始证据 42"
    recovered = await setup(reopened, create=False)
    writer = await recovered.kernel.writer("board", takeover=True)
    await recovered.ledger.adopt(record.ref, writer, recovering=True)
    assert await commit(recovered, await recovered.ledger.get(record.candidate.id)) == receipt
    assert await reopened.events("board") == events
    assert await PostgresActivationStore(reopened).summary("board") == summary


async def test_snapshot_uses_one_mvcc_view_during_concurrent_commit(
    postgres_blackboard_factory, monkeypatch
):
    first = await postgres_blackboard_factory()
    second = await postgres_blackboard_factory()
    kernel, initial = await create_board(first)
    entered, release = asyncio.Event(), asyncio.Event()
    original = second._board

    async def paused(*args):
        board = await original(*args)
        entered.set()
        await release.wait()
        return board

    with monkeypatch.context() as patch:
        patch.setattr(second, "_board", paused)
        read = asyncio.create_task(second.snapshot("board"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await kernel.commit("board", hint_delta(initial), context=SOURCE)
        finally:
            release.set()
        # Revision was read before commit; objects/control must come from that same snapshot.
        assert await asyncio.wait_for(read, 5) == initial
    assert (await second.snapshot("board")).revision == 2


async def test_unrelated_board_is_not_blocked_by_held_write_transaction(
    postgres_blackboard_factory,
):
    first = await postgres_blackboard_factory()
    second = await postgres_blackboard_factory()
    await create_board(first)
    other, initial = await create_board(second, "other")
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(connection):
        entered.set()
        await release.wait()

    task = asyncio.create_task(first.transaction(hold, board_id="board", write=True))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        receipt = await asyncio.wait_for(
            other.commit("other", hint_delta(initial), context=SOURCE), 5
        )
        assert receipt.board_revision == 2
    finally:
        release.set()
        await asyncio.wait_for(task, 5)


async def test_migration_upgrade_failure_rolls_back_and_preserves_v1_data(
    postgres_blackboard_factory, monkeypatch
):
    store = await postgres_blackboard_factory(migrate=False, initialize=False)
    # Build a prior schema using its immutable migration step, not a duplicate test DDL.
    with monkeypatch.context() as patch:
        patch.setattr(migrations, "SCHEMA_VERSION", 1)
        await store.setup()
    memory = MemoryBlackboardStore()
    _, initial = await create_board(memory)
    event = (await memory.events("board"))[0]
    async with store._pool.connection() as conn:
        await conn.execute(
            "INSERT INTO work_harness_boards VALUES (%s, %s, %s)",
            ("board", initial.revision, initial.root_intent_id),
        )
        await conn.execute("INSERT INTO work_harness_board_control(board_id) VALUES ('board')")
        await store._publish_objects(conn, initial, event)
    with pytest.raises(PostgresSchemaError, match="migrate"):
        await store.initialize()
    with monkeypatch.context() as patch:
        patch.setattr(
            migrations,
            "MIGRATIONS",
            (migrations.MIGRATIONS[0], (*migrations.MIGRATIONS[1], "SELECT 1 / 0")),
        )
        with pytest.raises(DivisionByZero):
            await store.setup()
    async with store._pool.connection() as conn:
        assert await migrations.schema_version(conn) == 1
        row = await one(
            conn,
            "SELECT count(*) AS n FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'work_harness_board_control' AND column_name = 'payload'",
        )
        assert row["n"] == 0
    await store.setup()
    await store.initialize()
    assert await store.snapshot("board") == initial
    assert await store.events("board") == (event,)
    assert not (await store.control("board")).status_pending
    await PostgresActivationStore(store).start_run("board", LIMITS)
    await store.setup()
    assert (await PostgresActivationStore(store).run("board")).limits == LIMITS


async def test_bootstrap_is_serialized_with_queue_and_newer_version_is_rejected(
    postgres_blackboard_factory,
):
    first = await postgres_blackboard_factory(migrate=False, initialize=False)
    second = await postgres_blackboard_factory(migrate=False, initialize=False)
    queue = PostgresRunStore(first._pool.conninfo)
    try:
        await asyncio.gather(first.setup(), second.setup(), queue.setup())
        await first.initialize()
        await second.initialize()
        async with first._pool.connection() as conn:
            assert await migrations.schema_version(conn) == migrations.SCHEMA_VERSION
            assert (
                await one(
                    conn,
                    "SELECT version FROM work_harness_schema_version WHERE component = 'run_queue'",
                )
            )["version"] == queue.schema_version
            await conn.execute(
                "UPDATE work_harness_schema_version SET version = 999 "
                "WHERE component = 'blackboard'"
            )
        for operation in (second.setup, second.initialize):
            with pytest.raises(PostgresSchemaError):
                await operation()
        async with first._pool.connection() as conn:
            assert await migrations.schema_version(conn) == 999
    finally:
        await queue.close()


async def test_controller_selects_matching_ledger_and_rejects_cross_store_settlement(
    postgres_blackboard_factory,
):
    first = await postgres_blackboard_factory()
    second = await postgres_blackboard_factory()
    execution = await setup(first)
    registry = KnowledgeSourceRegistry()
    assert isinstance(
        BlackboardController(execution.kernel, registry).ledger, PostgresActivationStore
    )
    with pytest.raises(BlackboardError, match="must share"):
        BlackboardController(execution.kernel, registry, ledger=PostgresActivationStore(second))
