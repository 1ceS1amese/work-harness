"""Distributed blackboard runtime on real PostgreSQL, using deterministic test providers."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from test_blackboard_runtime import ROOT, draft_request

from work_harness.domain.enums import RunStatus
from work_harness.llm.mock import MockProvider
from work_harness.llm.registry import ProviderAdapterRegistry
from work_harness.runtime import build_runtime
from work_harness.service import RunManager
from work_harness.storage import PostgresRunStore


@pytest.fixture
async def distributed_database(postgres_blackboard_factory, monkeypatch, tmp_path):
    store = await postgres_blackboard_factory()
    dsn = store._pool.conninfo
    queue = PostgresRunStore(dsn)
    try:
        await queue.setup()
    finally:
        await queue.close()
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        await saver.setup()
    monkeypatch.setenv("WORK_HARNESS_PROFILE", "distributed")
    monkeypatch.setenv("WORK_HARNESS_DATABASE_URL", dsn)
    monkeypatch.setenv("MODEL_BASE_URL", "https://models.example.test/v1")
    monkeypatch.setenv("MODEL_API_KEY", "integration-provider-key")
    monkeypatch.setenv("MODEL_ID", "integration-model")
    monkeypatch.setenv("WORK_HARNESS_RUN_LEASE_SECONDS", "5")
    monkeypatch.setenv("WORK_HARNESS_HEARTBEAT_INTERVAL_SECONDS", "1")
    monkeypatch.setenv("WORK_HARNESS_EXECUTION_POLL_INTERVAL_SECONDS", "0.05")
    yield dsn


def mock_providers():
    providers = ProviderAdapterRegistry()
    providers.register("openai_compatible", lambda name, _: MockProvider(name))
    return providers


@pytest.fixture
async def distributed_managers(distributed_database, monkeypatch):
    async with AsyncExitStack() as stack:
        managers = []
        for role in ("api", "worker"):
            monkeypatch.setenv("WORK_HARNESS_EXECUTION_ROLE", role)
            runtime = await stack.enter_async_context(
                build_runtime(ROOT, provider_adapters=mock_providers())
            )
            runtime.config.runtime.recovery_scan_interval_seconds = 0.05
            manager = RunManager(runtime)
            stack.push_async_callback(manager.close)
            managers.append(manager)
        yield tuple(managers)


async def wait_result(manager, run_id, *, deadline_seconds=15):
    async def poll():
        while True:
            result = await manager.get(run_id)
            if result.status in {"succeeded", "partial", "failed", "cancelled", "waiting_human"}:
                return result
            await asyncio.sleep(0.025)

    return await asyncio.wait_for(poll(), deadline_seconds)


async def test_api_and_worker_complete_blackboard_and_atomically_acknowledge_queue(
    distributed_database, monkeypatch
):
    async with AsyncExitStack() as stack:
        monkeypatch.setenv("WORK_HARNESS_EXECUTION_ROLE", "api")
        api_runtime = await stack.enter_async_context(
            build_runtime(ROOT, provider_adapters=mock_providers())
        )
        api = RunManager(api_runtime)
        stack.push_async_callback(api.close)
        await api.initialize()
        monkeypatch.setenv("WORK_HARNESS_EXECUTION_ROLE", "worker")
        runtime = await stack.enter_async_context(
            build_runtime(ROOT, provider_adapters=mock_providers())
        )
        worker = RunManager(runtime)
        stack.push_async_callback(worker.close)
        await worker.initialize()
        run_id = await api.start(draft_request())
        result = await wait_result(api, run_id)
        assert result.status == RunStatus.SUCCEEDED, await api_runtime.run_store.list_dead_jobs()
        for _ in range(200):
            record = await api_runtime.run_store.get(run_id)
            if record.status == RunStatus.SUCCEEDED:
                break
            await asyncio.sleep(0.01)
        assert record.engine == "blackboard" and record.status == RunStatus.SUCCEEDED
        assert result.usage.input_tokens > 0 and result.output
        assert (await api_runtime.run_store.queue_stats()).completed_jobs == 1
        store = await api_runtime.blackboard_store()
        assert not (await store.control(run_id)).status_pending
        assert (await store.snapshot(run_id)).finalization.status == "succeeded"


async def test_second_worker_process_recovers_saved_output_after_sigkill(
    distributed_database, monkeypatch, tmp_path
):
    import os
    import sys

    from work_harness.storage.blackboard_execution_postgres import PostgresActivationStore

    monkeypatch.setenv("WORK_HARNESS_EXECUTION_ROLE", "api")
    async with build_runtime(ROOT, provider_adapters=mock_providers()) as runtime:
        api = RunManager(runtime)
        await api.initialize()
        processes = []
        logs = []

        async def launch(name, *, pause=False):
            marker = tmp_path / f"{name}.ready"
            env = dict(
                os.environ,
                WORK_HARNESS_EXECUTION_ROLE="worker",
                WORK_HARNESS_INSTANCE_ID=name,
                BLACKBOARD_TEST_READY_FILE=str(marker),
            )
            if pause:
                env["BLACKBOARD_TEST_PAUSE_BEFORE_COMMIT"] = str(tmp_path / "paused")
            log = (tmp_path / f"{name}.log").open("wb")
            logs.append(log)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(ROOT / "tests/helpers/blackboard_worker_process.py"),
                env=env,
                cwd=ROOT,
                stdout=log,
                stderr=log,
            )
            processes.append(process)
            for _ in range(400):
                if marker.exists():
                    return process, marker.read_text()
                assert process.returncode is None, (tmp_path / f"{name}.log").read_text()
                await asyncio.sleep(0.025)
            raise AssertionError("worker process did not become ready")

        try:
            first, first_owner = await launch("first", pause=True)
            run_id = await api.start(draft_request())
            paused = tmp_path / "paused"
            for _ in range(400):
                if paused.exists():
                    break
                assert first.returncode is None, (tmp_path / "first.log").read_text()
                await asyncio.sleep(0.025)
            assert paused.exists(), (tmp_path / "first.log").read_text()
            assert paused.read_text() == run_id
            store = await runtime.blackboard_store()
            ledger = PostgresActivationStore(store)
            before = await ledger.summary(run_id)
            assert before.tokens > 0
            records = await ledger.records(run_id)
            assert any(item.status == "result_ready" for item in records)
            second, second_owner = await launch("second")
            assert second_owner != first_owner and second.returncode is None
            first.kill()
            await asyncio.wait_for(first.wait(), 5)
            result = await wait_result(api, run_id, deadline_seconds=20)
            assert result.status == RunStatus.SUCCEEDED, (tmp_path / "second.log").read_text()
            for _ in range(200):
                if (await runtime.run_store.queue_stats()).completed_jobs == 1:
                    break
                await asyncio.sleep(0.01)
            after = await ledger.summary(run_id)
            assert (
                after.tokens == before.tokens
            )  # Saved output is committed without rerunning its model.
            assert after.reserved_tokens == 0
            assert (await runtime.run_store.get(run_id)).status == RunStatus.SUCCEEDED
            assert (await store.control(run_id)).generation > 1
            assert not (await store.control(run_id)).status_pending
            events = await store.events(run_id)
            assert [event.sequence for event in events] == list(range(1, len(events) + 1))
            assert (await runtime.run_store.queue_stats()).dead_jobs == 0
        finally:
            for process in processes:
                if process.returncode is None:
                    process.kill()
                await asyncio.wait_for(process.wait(), 5)
            for log in logs:
                log.close()
            await api.close()


async def test_typed_input_and_resume_job_commit_together(distributed_database, monkeypatch):
    from test_blackboard_inputs import confirmation, human_request

    async with AsyncExitStack() as stack:
        monkeypatch.setenv("WORK_HARNESS_EXECUTION_ROLE", "api")
        runtime = await stack.enter_async_context(
            build_runtime(ROOT, provider_adapters=mock_providers())
        )
        api = RunManager(runtime)
        stack.push_async_callback(api.close)
        await api.initialize()
        monkeypatch.setenv("WORK_HARNESS_EXECUTION_ROLE", "worker")
        worker_runtime = await stack.enter_async_context(
            build_runtime(ROOT, provider_adapters=mock_providers())
        )
        worker = RunManager(worker_runtime)
        stack.push_async_callback(worker.close)
        await worker.initialize()
        run_id = await api.start(human_request().model_copy(update={"engine": "blackboard"}))
        assert (await wait_result(api, run_id)).status == RunStatus.WAITING_HUMAN
        store = await runtime.blackboard_store()
        request = await confirmation(store, run_id)
        accepted = await api.submit_blackboard_input(run_id, request)
        assert accepted.wakeup == "scheduled"
        assert (await wait_result(api, run_id)).status == RunStatus.SUCCEEDED
        retry = await api.submit_blackboard_input(run_id, request)
        assert retry.receipt == accepted.receipt
        for _ in range(200):
            if (await runtime.run_store.queue_stats()).completed_jobs == 2:
                break
            await asyncio.sleep(0.01)
        assert (await runtime.run_store.queue_stats()).completed_jobs == 2
        assert await store.pending_reconciliations() == ()


@pytest.mark.parametrize("started", (False, True))
async def test_cancel_before_execution_or_at_human_wait(distributed_managers, started):
    from test_blackboard_inputs import human_request

    api, worker = distributed_managers
    if started:
        await worker.initialize()
    run_id = await api.start(human_request())
    if started:
        assert (await wait_result(api, run_id)).status == RunStatus.WAITING_HUMAN
    await api.cancel(run_id)
    store = await api.runtime.blackboard_store()
    board = await store.snapshot(run_id)
    assert board.finalization.status == "cancelled"
    assert (await api.runtime.run_store.get(run_id)).status == RunStatus.CANCELLED
    assert not (await store.control(run_id)).status_pending
    revision = board.revision
    await api.cancel(run_id)
    assert (await store.snapshot(run_id)).revision == revision
    assert (await api.runtime.run_store.queue_stats()).leased_jobs == 0


async def test_worker_repairs_cancel_persisted_before_api_crash(distributed_managers):
    from test_blackboard_inputs import human_request

    api, worker = distributed_managers
    run_id = await api.start(human_request())
    # The API died after durable cancellation, before the board existed or was sealed.
    await api.runtime.run_store.cancel_jobs(run_id)
    await worker.initialize()
    store = await api.runtime.blackboard_store()
    from work_harness.blackboard.protocol import BoardNotFoundError

    async def repaired():
        while True:
            try:
                control = await store.control(run_id)
            except BoardNotFoundError:
                pass
            else:
                if control.finalization is not None and not control.status_pending:
                    return control
            await asyncio.sleep(0.025)

    control = await asyncio.wait_for(repaired(), 5)
    assert control.finalization.status == "cancelled"
    assert (await store.snapshot(run_id)).objects[0].objective == human_request().goal
    assert await api.runtime.run_store.blackboard_reconciliations() == ()


async def test_worker_reconciles_finalization_without_queue_ack(distributed_managers, monkeypatch):
    from work_harness.storage.blackboard_postgres import PostgresBlackboardStore
    from work_harness.workflow import blackboard_host

    api, worker = distributed_managers
    reached = asyncio.Event()
    release = asyncio.Event()
    original = PostgresBlackboardStore.complete_job
    original_scan = blackboard_host.reconcile_distributed_runs

    async def scan_after_interruption(manager):
        # Establish the injected failure before allowing recovery. Otherwise a scan can
        # settle the sealed board before complete_job is reached and cancel its owner.
        await reached.wait()
        await original_scan(manager)

    async def interrupted(self, **kwargs):
        reached.set()
        await release.wait()
        return await original(self, **kwargs)

    monkeypatch.setattr(PostgresBlackboardStore, "complete_job", interrupted)
    monkeypatch.setattr(blackboard_host, "reconcile_distributed_runs", scan_after_interruption)
    await worker.initialize()
    try:
        run_id = await api.start(draft_request())
        await asyncio.wait_for(reached.wait(), 5)
        store = await api.runtime.blackboard_store()
        final = (await store.snapshot(run_id)).finalization
        assert final.status == "succeeded"
        for _ in range(200):
            if (await api.runtime.run_store.get(run_id)).status == RunStatus.SUCCEEDED:
                break
            await asyncio.sleep(0.025)
        assert (await api.runtime.run_store.get(run_id)).status == RunStatus.SUCCEEDED
        assert not (await store.control(run_id)).status_pending
        assert (await api.runtime.run_store.queue_stats()).completed_jobs == 1
        # Completion wins even when a cancel command arrives before the index was read.
        await api.cancel(run_id)
        assert (await store.snapshot(run_id)).finalization == final
    finally:
        release.set()


@pytest.mark.parametrize("fault", ("stale_revision", "after_verification"))
async def test_rejected_input_rolls_back_job_writer_and_receipts(
    distributed_managers, monkeypatch, fault
):
    from test_blackboard_inputs import confirmation, human_request

    from work_harness.blackboard.protocol import BlackboardError
    from work_harness.storage.blackboard_execution_postgres import PostgresVerificationReceiptStore

    api, worker = distributed_managers
    await worker.initialize()
    run_id = await api.start(human_request())
    assert (await wait_result(api, run_id)).status == RunStatus.WAITING_HUMAN
    store = await api.runtime.blackboard_store()
    request = await confirmation(store, run_id)
    before = await store.control(run_id)
    async with store._pool.connection() as connection:
        receipts_before = await (
            await connection.execute(
                "SELECT receipt_id FROM work_harness_board_verification_receipts "
                "ORDER BY receipt_id"
            )
        ).fetchall()
    if fault == "stale_revision":
        request = request.model_copy(update={"expected_revision": 999})
    else:
        original = PostgresVerificationReceiptStore.put

        async def fail_after_verification(self, *args, **kwargs):
            await original(self, *args, **kwargs)
            raise BlackboardError("injected failure after verification")

        monkeypatch.setattr(PostgresVerificationReceiptStore, "put", fail_after_verification)
    with pytest.raises(BlackboardError):
        await api.submit_blackboard_input(run_id, request)
    assert await store.control(run_id) == before
    assert await store.receipt(run_id, request.storage_key) is None
    assert (await api.runtime.run_store.get(run_id)).status == RunStatus.WAITING_HUMAN
    stats = await api.runtime.run_store.queue_stats()
    assert stats.completed_jobs == 1 and stats.available_jobs == 0 and stats.leased_jobs == 0
    async with store._pool.connection() as connection:
        receipts_after = await (
            await connection.execute(
                "SELECT receipt_id FROM work_harness_board_verification_receipts "
                "ORDER BY receipt_id"
            )
        ).fetchall()
    assert receipts_after == receipts_before


async def test_duplicate_input_creates_one_resume_job(distributed_managers):
    from test_blackboard_inputs import confirmation, human_request

    api, worker = distributed_managers
    await worker.initialize()
    run_id = await api.start(human_request())
    assert (await wait_result(api, run_id)).status == RunStatus.WAITING_HUMAN
    request = await confirmation(await api.runtime.blackboard_store(), run_id)
    first, second = await asyncio.gather(
        api.submit_blackboard_input(run_id, request),
        api.submit_blackboard_input(run_id, request),
    )
    assert first.receipt == second.receipt
    assert (await wait_result(api, run_id)).status == RunStatus.SUCCEEDED
    for _ in range(200):
        if (await api.runtime.run_store.queue_stats()).completed_jobs == 2:
            break
        await asyncio.sleep(0.01)
    assert (await api.runtime.run_store.queue_stats()).completed_jobs == 2


async def test_input_between_checkpoint_and_ack_keeps_current_lease(
    distributed_managers, monkeypatch
):
    from test_blackboard_inputs import confirmation, human_request

    from work_harness.storage.blackboard_postgres import PostgresBlackboardStore

    api, worker = distributed_managers
    reached, release = asyncio.Event(), asyncio.Event()
    original = PostgresBlackboardStore.complete_job
    scopes = []

    async def paused(self, **kwargs):
        if not reached.is_set():
            scopes.append(self)
            reached.set()
            await release.wait()
        return await original(self, **kwargs)

    monkeypatch.setattr(PostgresBlackboardStore, "complete_job", paused)
    await worker.initialize()
    try:
        run_id = await api.start(human_request())
        await asyncio.wait_for(reached.wait(), 5)
        store = await api.runtime.blackboard_store()
        before = await store.control(run_id)
        lease = scopes[0]._lease
        request = await confirmation(store, run_id)
        accepted = await api.submit_blackboard_input(run_id, request)
        assert accepted.wakeup == "active"
        after = await store.control(run_id)
        assert (after.generation, after.owner_id, after.session_id) == (
            before.generation,
            before.owner_id,
            before.session_id,
        )
        assert after.wakeup_revision == accepted.receipt.board_revision
        # An observation from before input cannot turn the Run into a sleeping wait.
        assert not await original(scopes[0], observed_revision=request.expected_revision)
        assert (await api.runtime.run_store.get(run_id)).status == RunStatus.RUNNING
        async with store._pool.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT lease_token FROM work_harness_run_jobs WHERE job_id = %s",
                    (lease.job_id,),
                )
            ).fetchone()
        assert row["lease_token"] == lease.token
        release.set()
        assert (await wait_result(api, run_id)).status == RunStatus.SUCCEEDED
    finally:
        release.set()


async def test_event_replay_and_worker_polling_survive_all_notifications_lost(
    distributed_managers, monkeypatch
):
    import json

    from test_blackboard_query import ConnectedRequest, frames

    from work_harness.api.blackboard import stream_blackboard

    api, worker = distributed_managers

    async def dropped(*args, **kwargs):
        pass

    for manager in (api, worker):
        monkeypatch.setattr(manager.runtime.run_store, "notify_run", dropped)
        monkeypatch.setattr(manager, "_publish", lambda *_: None)
    run_id = await api.start(draft_request())
    response = await stream_blackboard(api, run_id, ConnectedRequest())

    async def collect():
        return frames("".join([item async for item in response.body_iterator]))

    streaming = asyncio.create_task(collect())
    try:
        await asyncio.sleep(0)
        await worker.initialize()
        items = await asyncio.wait_for(streaming, 10)
        events = [json.loads(item["data"]) for item in items]
        assert events[-1]["event_type"] == "finalized"
        assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
        replay = await stream_blackboard(api, run_id, ConnectedRequest(), after=items[1]["id"])
        remaining = frames("".join([item async for item in replay.body_iterator]))
        assert remaining == items[2:]
    finally:
        streaming.cancel()
        await asyncio.gather(streaming, return_exceptions=True)
