from __future__ import annotations

import asyncio
import os
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection, sql
from psycopg.errors import CheckViolation

from work_harness.domain.enums import RunJobStatus, RunStatus
from work_harness.domain.models import TaskRequest
from work_harness.llm.mock import MockProvider
from work_harness.llm.registry import ProviderAdapterRegistry
from work_harness.runtime import build_runtime
from work_harness.service import RunManager
from work_harness.storage import (
    PostgresRunStore,
    RunExecutionVersion,
    RunQueueCapacityError,
    RunRecord,
)

TEST_DSN = os.getenv("WORK_HARNESS_TEST_DATABASE_URL", "").strip()
PROJECT_ROOT = Path(__file__).resolve().parents[1]
pytestmark = [
    pytest.mark.usefixtures("legacy_engine"),
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not TEST_DSN,
        reason="WORK_HARNESS_TEST_DATABASE_URL is not configured",
    ),
]


async def clean_database() -> None:
    async with await AsyncConnection.connect(TEST_DSN, autocommit=True) as connection:
        await connection.execute(
            "TRUNCATE work_harness_run_jobs, work_harness_run_index, "
            "work_harness_worker_heartbeats CASCADE"
        )


@pytest.fixture
async def stores():
    first = PostgresRunStore(TEST_DSN, pool_max_size=8)
    second = PostgresRunStore(TEST_DSN, pool_max_size=8)
    await first.setup()
    async with AsyncPostgresSaver.from_conn_string(TEST_DSN) as saver:
        await saver.setup()
    await first.initialize()
    await second.initialize()
    await clean_database()
    try:
        yield first, second
    finally:
        await clean_database()
        await first.close()
        await second.close()


async def submit(store: PostgresRunStore, index: int, *, capacity: int = 200):
    return await store.submit(
        RunRecord(
            run_id=f"run_pg_{index}",
            idempotency_key_hash=f"{index:064x}",
            request_fingerprint=f"{index + 1:064x}",
        ),
        {"goal": f"PostgreSQL job {index}"},
        capacity=capacity,
        max_attempts=2,
    )


async def test_concurrent_idempotency_and_capacity_are_global(stores) -> None:
    first, second = stores

    duplicates = await asyncio.gather(
        *(
            (first if index % 2 else second).submit(
                RunRecord(
                    run_id=f"run_duplicate_{index}",
                    idempotency_key_hash="a" * 64,
                    request_fingerprint="b" * 64,
                ),
                {"goal": "same request"},
                capacity=100,
                max_attempts=3,
            )
            for index in range(50)
        )
    )
    assert len({item.record.run_id for item in duplicates}) == 1
    assert sum(item.created for item in duplicates) == 1
    assert (await first.queue_stats()).available_jobs == 1
    assert (await first.queue_stats(graph_revision="a" * 64)).incompatible_active_jobs == 1

    await clean_database()
    results = await asyncio.gather(
        *(submit(first if index % 2 else second, index, capacity=10) for index in range(50)),
        return_exceptions=True,
    )
    accepted = [item for item in results if not isinstance(item, Exception)]
    rejected = [item for item in results if isinstance(item, RunQueueCapacityError)]
    assert len(accepted) == 10
    assert len(rejected) == 40
    assert len(await first.list(limit=100)) == 10
    assert (await first.queue_stats()).available_jobs == 10


async def test_skip_locked_claims_each_job_once_across_workers(stores) -> None:
    first, second = stores
    await asyncio.gather(*(submit(first, index) for index in range(100)))

    async def drain(store: PostgresRunStore, owner: str) -> list[str]:
        claimed: list[str] = []
        while True:
            job = await store.claim_job(owner=owner, lease_seconds=30)
            if job is None:
                return claimed
            assert job.lease_token is not None
            claimed.append(job.job_id)
            assert await store.complete_job(
                job.job_id,
                owner=owner,
                lease_token=job.lease_token,
                run_status=RunStatus.SUCCEEDED,
            )

    claimed_a, claimed_b = await asyncio.gather(
        drain(first, "worker-a"),
        drain(second, "worker-b"),
    )
    claimed = claimed_a + claimed_b
    assert len(claimed) == 100
    assert len(set(claimed)) == 100
    stats = await first.queue_stats()
    assert stats.available_jobs == 0
    assert stats.leased_jobs == 0
    assert stats.completed_jobs == 100


async def test_expired_lease_fencing_retry_dead_letter_and_cancel(stores) -> None:
    first, second = stores
    await submit(first, 1)
    original = await first.claim_job(owner="worker-a", lease_seconds=0.1)
    assert original is not None and original.lease_token is not None
    await asyncio.sleep(0.2)
    reclaimed = await second.claim_job(owner="worker-b", lease_seconds=30)
    assert reclaimed is not None and reclaimed.lease_token is not None
    assert reclaimed.job_id == original.job_id
    assert reclaimed.lease_token != original.lease_token
    assert not await first.complete_job(
        original.job_id,
        owner="worker-a",
        lease_token=original.lease_token,
        run_status=RunStatus.SUCCEEDED,
    )

    retry = await second.retry_job(
        reclaimed.job_id,
        owner="worker-b",
        lease_token=reclaimed.lease_token,
        error_code="temporary",
        error_message="retry once",
        delay_seconds=0,
    )
    assert retry is not None
    assert retry.status == RunJobStatus.DEAD  # max_attempts=2 and this is claim two.
    assert await first.cancel_jobs(retry.run_id) == 0
    terminal_record = await first.get(retry.run_id)
    assert terminal_record is not None and terminal_record.status == RunStatus.FAILED

    await submit(first, 2)
    cancelled = await first.claim_job(owner="worker-a", lease_seconds=30)
    assert cancelled is not None and cancelled.lease_token is not None
    assert await second.cancel_jobs(cancelled.run_id) == 1
    assert not await first.heartbeat_job(
        cancelled.job_id,
        owner="worker-a",
        lease_token=cancelled.lease_token,
        lease_seconds=30,
    )
    record = await first.get(cancelled.run_id)
    assert record is not None
    assert record.status == RunStatus.CANCELLED
    dead_jobs = await first.list_dead_jobs()
    assert [item.job_id for item in dead_jobs] == [retry.job_id]
    replay = await second.replay_dead_job(
        retry.job_id,
        capacity=10,
        max_attempts=3,
    )
    assert replay.job_id != retry.job_id
    assert replay.status == RunJobStatus.AVAILABLE


async def test_postgres_notify_wakes_listener_on_another_instance(stores) -> None:
    first, second = stores
    observed: list[str] = []
    received = asyncio.Event()

    def capture(run_id: str) -> None:
        observed.append(run_id)
        received.set()

    await first.start_event_listener(capture)
    try:
        await second.notify_run("run_cross_instance_event")
        await asyncio.wait_for(received.wait(), timeout=2)

        observed.clear()
        received.clear()
        previous_connection = first._event_connection
        assert previous_connection is not None
        await previous_connection.close()
        for _ in range(80):
            current_connection = first._event_connection
            if (
                current_connection is not None
                and current_connection is not previous_connection
                and not current_connection.closed
            ):
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("PostgreSQL event listener did not reconnect")
        await second.notify_run("run_after_listener_reconnect")
        await asyncio.wait_for(received.wait(), timeout=2)
    finally:
        await first.stop_event_listener(capture)

    assert observed == ["run_after_listener_reconnect"]


@pytest.mark.parametrize("target_table", ("work_harness_run_jobs", "work_harness_run_inputs"))
async def test_submit_rolls_back_run_when_job_insert_fails(stores, target_table) -> None:
    first, _ = stores
    async with await AsyncConnection.connect(TEST_DSN, autocommit=True) as connection:
        await connection.execute(
            """
            CREATE OR REPLACE FUNCTION work_harness_test_reject_job()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'injected job insert failure';
            END;
            $$
            """
        )
        await connection.execute(
            sql.SQL("""
            CREATE TRIGGER work_harness_test_reject_job
            BEFORE INSERT ON {}
            FOR EACH ROW EXECUTE FUNCTION work_harness_test_reject_job()
            """).format(sql.Identifier(target_table))
        )
    try:
        with pytest.raises(Exception, match="injected job insert failure"):
            await first.submit(
                RunRecord(
                    run_id="run_atomic_rollback",
                    idempotency_key_hash="e" * 64,
                    request_fingerprint="f" * 64,
                    execution=RunExecutionVersion(
                        engine="legacy",
                        workflow_revision="agent-subgraphs-v1",
                        schema_version=1,
                        configuration_fingerprint="a" * 64,
                    ),
                ),
                {"goal": "must roll back"},
                capacity=10,
                max_attempts=3,
            )
        assert await first.get("run_atomic_rollback") is None
        assert await first.get_initial_input("run_atomic_rollback") is None
    finally:
        async with await AsyncConnection.connect(TEST_DSN, autocommit=True) as connection:
            await connection.execute(
                sql.SQL("DROP TRIGGER IF EXISTS work_harness_test_reject_job ON {}").format(
                    sql.Identifier(target_table)
                )
            )
            await connection.execute("DROP FUNCTION IF EXISTS work_harness_test_reject_job()")


async def test_schema_migrates_old_columns_and_rejects_newer_version(stores) -> None:
    first, _ = stores
    historical = RunRecord(run_id="before_versioned_index")
    await first.reserve(historical)
    async with await AsyncConnection.connect(TEST_DSN, autocommit=True) as connection:
        await connection.execute("DROP INDEX IF EXISTS ix_work_harness_run_jobs_revision_claim")
        await connection.execute("ALTER TABLE work_harness_run_jobs DROP COLUMN graph_revision")
        await connection.execute(
            "ALTER TABLE work_harness_worker_heartbeats DROP COLUMN config_fingerprint"
        )
        await connection.execute("ALTER TABLE work_harness_run_index DROP COLUMN execution")
        await connection.execute("DROP TABLE work_harness_run_inputs")
        await connection.execute(
            "UPDATE work_harness_schema_version SET version = 1 WHERE component = 'run_queue'"
        )

    await first.setup()
    await first.initialize()
    recovered = await first.get(historical.run_id)
    assert recovered == historical
    assert recovered.engine == "legacy"
    assert recovered.execution is None
    assert await first.get_initial_input(historical.run_id) is None
    async with await AsyncConnection.connect(TEST_DSN, autocommit=True) as connection:
        columns = await (
            await connection.execute(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND (
                    (table_name = 'work_harness_run_jobs'
                     AND column_name = 'graph_revision')
                    OR
                    (table_name = 'work_harness_worker_heartbeats'
                     AND column_name = 'config_fingerprint')
                    OR
                    (table_name = 'work_harness_run_index'
                     AND column_name = 'execution')
                  )
                """
            )
        ).fetchall()
        assert {tuple(row) for row in columns} == {
            ("work_harness_run_jobs", "graph_revision"),
            ("work_harness_worker_heartbeats", "config_fingerprint"),
            ("work_harness_run_index", "execution"),
        }
        await connection.execute(
            "UPDATE work_harness_schema_version SET version = 999 WHERE component = 'run_queue'"
        )
    try:
        with pytest.raises(Exception, match="unsupported run queue schema version"):
            await first.initialize()
    finally:
        async with await AsyncConnection.connect(TEST_DSN, autocommit=True) as connection:
            await connection.execute(
                "UPDATE work_harness_schema_version SET version = %s WHERE component = 'run_queue'",
                (first.schema_version,),
            )


async def test_run_execution_version_is_persisted_atomically_with_queue_admission(stores) -> None:
    first, second = stores
    execution = RunExecutionVersion(
        engine="legacy",
        workflow_revision="agent-subgraphs-v1",
        schema_version=1,
        configuration_fingerprint="e" * 64,
    )
    record = RunRecord(run_id="versioned", execution=execution, idempotency_key_hash="f" * 64)
    submission = await first.submit(record, {"goal": "versioned"}, capacity=5, max_attempts=2)
    assert submission.created
    assert (await second.get(record.run_id)).execution == execution
    assert await second.get_initial_input(record.run_id) == {"goal": "versioned"}
    retry = await second.submit(
        record.model_copy(update={"execution": None, "run_id": "duplicate"}),
        {"goal": "versioned"},
        capacity=5,
        max_attempts=2,
    )
    assert not retry.created
    assert retry.record.execution == execution
    assert await first.get("duplicate") is None
    assert await first.get_initial_input("duplicate") is None
    await first.update_status(record.run_id, RunStatus.RUNNING)
    assert (await second.get(record.run_id)).execution == execution
    reserved = RunRecord(run_id="reserve_versioned", execution=execution)
    assert await first.reserve(reserved, initial_input={"goal": "reserved"}) == reserved
    assert await second.reserve(reserved.model_copy(update={"execution": None})) == reserved
    assert await second.get_initial_input(reserved.run_id) == {"goal": "reserved"}
    async with await AsyncConnection.connect(TEST_DSN, autocommit=True) as connection:
        with pytest.raises(CheckViolation):
            await connection.execute(
                "UPDATE work_harness_run_index SET execution = 'null'::jsonb WHERE run_id = %s",
                (record.run_id,),
            )
    assert (await first.get(record.run_id)).execution == execution


async def test_expired_lease_is_dead_after_attempt_limit_and_not_reclaimed(
    stores,
) -> None:
    first, second = stores
    submission = await first.submit(
        RunRecord(
            run_id="run_pg_crash_loop",
            idempotency_key_hash="c" * 64,
            request_fingerprint="d" * 64,
        ),
        {"goal": "never retry forever"},
        capacity=10,
        max_attempts=1,
    )
    claimed = await first.claim_job(owner="worker-a", lease_seconds=0.1)
    assert claimed is not None and submission.job is not None
    await asyncio.sleep(0.2)

    reconciliation = await second.claim_job(owner="worker-b", lease_seconds=30)
    assert reconciliation is not None and reconciliation.reconciliation_only
    assert reconciliation.lease_token is not None
    dead_job = await second.retry_job(
        reconciliation.job_id,
        owner="worker-b",
        lease_token=reconciliation.lease_token,
        error_code="queue_attempts_exhausted",
        error_message="checkpoint was not terminal",
        delay_seconds=0,
    )
    assert dead_job is not None and dead_job.status == RunJobStatus.DEAD
    dead = await first.list_dead_jobs()
    assert [item.job_id for item in dead] == [claimed.job_id]
    assert dead[0].last_error_code == "queue_attempts_exhausted"
    record = await first.get(claimed.run_id)
    assert record is not None and record.status == RunStatus.FAILED


async def test_separate_api_and_worker_runtimes_share_queue_and_checkpoints(
    stores,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORK_HARNESS_PROFILE", "distributed")
    monkeypatch.setenv("WORK_HARNESS_DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("MODEL_BASE_URL", "https://models.example.test/v1")
    monkeypatch.setenv("MODEL_API_KEY", "integration-provider-key")
    monkeypatch.setenv("MODEL_ID", "integration-model")
    providers = ProviderAdapterRegistry()
    providers.register("openai_compatible", lambda name, _: MockProvider(name))

    async with AsyncExitStack() as stack:
        monkeypatch.setenv("WORK_HARNESS_EXECUTION_ROLE", "api")
        api_runtime = await stack.enter_async_context(
            build_runtime(PROJECT_ROOT, provider_adapters=providers)
        )
        monkeypatch.setenv("WORK_HARNESS_EXECUTION_ROLE", "worker")
        worker_a_runtime = await stack.enter_async_context(
            build_runtime(PROJECT_ROOT, provider_adapters=providers)
        )
        worker_b_runtime = await stack.enter_async_context(
            build_runtime(PROJECT_ROOT, provider_adapters=providers)
        )
        api = RunManager(api_runtime)
        worker_a = RunManager(worker_a_runtime)
        worker_b = RunManager(worker_b_runtime)
        await api.initialize()
        await worker_a.initialize()
        await worker_b.initialize()
        try:
            run_ids = [
                await api.start(TaskRequest(goal=f"PostgreSQL graph run {index}"))
                for index in range(6)
            ]
            for _ in range(400):
                queue = await api_runtime.run_store.queue_stats()
                if queue.completed_jobs == len(run_ids):
                    break
                await asyncio.sleep(0.025)
            else:
                pytest.fail("PostgreSQL workers did not complete the shared queue")
            results = [await api.get(run_id) for run_id in run_ids]

            gated_run_id = await api.start(
                TaskRequest(
                    goal="PostgreSQL human approval run",
                    risk_level="high",
                )
            )
            for _ in range(400):
                gated = await api.get(gated_run_id)
                if gated.status == RunStatus.WAITING_HUMAN:
                    break
                await asyncio.sleep(0.025)
            else:
                pytest.fail("PostgreSQL run did not reach the human gate")
            await api.resume(gated_run_id, approved=True, note="integration approval")
            queued_resume = await api.get(gated_run_id)
            assert queued_resume.status == RunStatus.PENDING
            for _ in range(400):
                gated = await api.get(gated_run_id)
                if gated.status == RunStatus.SUCCEEDED:
                    break
                await asyncio.sleep(0.025)
            else:
                pytest.fail("PostgreSQL resume job did not complete")
            results.append(gated)
        finally:
            await worker_a.close()
            await worker_b.close()
            await api.close()

    assert all(result.status == RunStatus.SUCCEEDED for result in results)
    assert all(result.output for result in results)
